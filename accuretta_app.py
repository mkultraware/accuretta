"""
Accuretta desktop launcher.

Turns Accuretta into a real windowed application with NO stray console windows:
it starts the existing bridge in-process (a background thread, not a separate
console) and opens the live app in a native OS webview. The window loads the
exact same URL your browser loads, rendered by the same engine (WebView2 =
Chromium on Windows 11), so it looks and behaves pixel-for-pixel identical to
the browser. No frontend or bridge code is reimplemented here.

Run it directly during development (no console window):
    pythonw accuretta_app.py

Package it into a single windowed .exe (no console) with PyInstaller:
    pip install pywebview pyinstaller
    pyinstaller --noconfirm --windowed --name Accuretta ^
        --add-data "index.html;." --add-data "app.js;." --add-data "app.css;." ^
        --add-data "colors_and_type.css;." --add-data "manifest.webmanifest;." ^
        --add-data "accuMOTION;accuMOTION" --add-data "media;media" ^
        --add-data "logo-mark-light.png;." --add-data "logo-mark-dark.png;." ^
        --add-data "favicon.png;." --add-data "app-icon-512.png;." ^
        --icon accuretta.ico accuretta_app.py

Note: the bridge serves its static files relative to its own directory. When
frozen by PyInstaller (onefile), those live under sys._MEIPASS, so the bridge's
static-file root should resolve there when frozen. That is the one packaging
touch-up; running directly with python needs nothing.
"""

import base64
import os
import re
import socket
import sys
import threading
import time

# App mode: hide the llama-server console (bridge reads this), and do not let the
# bridge auto-open a browser -- the webview window IS the UI.
os.environ.setdefault("ACCURETTA_APP", "1")
os.environ.setdefault("ACCURETTA_BROWSER", "none")

# When frozen, run from the extracted bundle dir so bridge finds its assets.
if getattr(sys, "frozen", False):
    os.chdir(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))

import webview  # pip install pywebview
import bridge   # the existing server; module-level init runs on import

PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))


def _wait_ready(host: str = "127.0.0.1", port: int = PORT, timeout: float = 40.0) -> bool:
    """Block until the bridge is accepting connections (or give up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _run_bridge() -> None:
    # bridge.main() creates the HTTP server and blocks in serve_forever(). It runs
    # in a daemon thread so it dies with the app; the bridge's own atexit handlers
    # clean up llama-server on shutdown.
    try:
        bridge.main()
    except Exception as exc:  # keep the window usable even if the bridge stumbles
        print(f"bridge exited: {exc}", file=sys.stderr)


# Single-instance lock: hold a bound socket for the process lifetime so a second
# launch detects the first and bows out, instead of two windows fighting over the
# bridge port (the class of bug behind the earlier "window lingered" shutdown).
_lock_sock = None


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _acquire_single_instance(lock_port: int = 8799) -> bool:
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", lock_port))  # fails if another instance holds it
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


# Themes whose splash background needs the dark-colored logo mark. Keep this in
# sync with the actual palette luminance, including the softer and pastel sets.
_LIGHT_THEMES = {"", "light", "soft", "pastel", "retro", "neumorphic", "neobrutalism", "aperture"}


def _read_saved_theme() -> str:
    """The last theme the user saved (settings.json). '' / unknown -> light."""
    try:
        return (bridge.get_settings().get("theme") or "").strip().lower()
    except Exception:
        return ""


def _splash_palette(theme: str) -> dict:
    """Pull bg/fg/muted/accent for `theme` straight from colors_and_type.css so
    the splash matches the app rather than hardcoding a second copy of the palette.
    Falls back to the light default if the file or a token can't be read."""
    pal = {"bg": "#FAFAFB", "fg": "#09090B", "muted": "#52525B", "accent": "#0066FF"}
    try:
        css = (bridge.ROOT / "colors_and_type.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # strip comments so [^{}] is safe
        blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)

        # Merge every custom property whose selector matches the scope, in document
        # order (later rules win) — so the theme's palette block overrides the
        # earlier `body` gradient rule, and :root supplies shared shades.
        def vars_for(target: str) -> dict:
            out = {}
            for sel, body in blocks:
                if target in sel:
                    for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
                        out[name.strip()] = val.strip()
            return out

        vmap = vars_for(":root")
        if theme:
            vmap.update(vars_for(f'[data-theme="{theme}"]'))

        # Follow var(--x) references (some accents alias a shared shade) to a literal.
        def resolve(val: str, depth: int = 0) -> str:
            val = (val or "").strip()
            m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)", val)
            if m and depth < 8:
                if m.group(1) in vmap:
                    return resolve(vmap[m.group(1)], depth + 1)
                if m.group(2):  # var(--x, fallback)
                    return resolve(m.group(2), depth + 1)
            return val

        for key, var in (("bg", "--bg"), ("fg", "--fg"),
                         ("muted", "--fg-muted"), ("accent", "--accent")):
            if var in vmap:
                pal[key] = resolve(vmap[var])
    except Exception:
        pass
    return pal


def _logo_data_uri(theme: str) -> str:
    """Base64-embed the theme-appropriate logo. The splash renders before the
    bridge serves files, so an http path (/logo-mark-*.png) can't load here."""
    fname = "logo-mark-dark.png" if theme in _LIGHT_THEMES else "logo-mark-light.png"
    try:
        raw = (bridge.ROOT / fname).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _build_splash_html() -> str:
    """Theme-aware boot splash — quiet, flat, typographic.

    Design rules: the saved theme supplies the entire palette (bg / fg /
    muted / accent) and the matching logo variant; every surface is FLAT —
    no blur, no translucency, no glow, no gradient field, no film grain.
    One accent use only (the progress fill). The dot grid glitters like
    stars: three sparse, off-phase twinkle layers with non-square tiles and
    co-prime periods composite into an asymmetric scatter of blinking dots
    over the static field. window.__splashStage(label, pct) stays the live
    progress contract.
    """
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo = _logo_data_uri(theme)
    accent = pal["accent"]
    is_light = theme in _LIGHT_THEMES
    edge = "rgba(18, 18, 20, 0.16)" if is_light else "rgba(255, 255, 255, 0.14)"
    theme_label = re.sub(r"[^a-z0-9 -]", "", (theme or "light").replace("-", "")).strip() or "light"
    logo_html = (f'<img src="{logo}" class="logo-img" alt="">' if logo
                 else '<span class="logo-fallback">a</span>')

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
  <div class="grid" aria-hidden="true">
    <i class="tw t1"></i><i class="tw t2"></i><i class="tw t3"></i>
  </div>

  <header class="chrome mast">
    <span class="mast-item"><i class="dot"></i>local runtime</span>
    <span class="mast-item dim">profile / {theme_label}</span>
  </header>

  <main class="lockup">
    <div class="mark">{logo_html}</div>
    <h1>accuretta</h1>
    <section class="boot" aria-live="polite">
      <div class="readout">
        <span id="status">Preparing local runtime</span>
        <span class="pct" id="pct">0%</span>
      </div>
      <div class="rail"><div class="fill" id="fill"></div></div>
    </section>
  </main>

  <footer class="chrome foot">
    <span>private by location</span>
    <span>your model · your machine</span>
  </footer>

  <style>
    :root {{
      --bg: {pal['bg']}; --fg: {pal['fg']}; --muted: {pal['muted']}; --accent: {accent};
      --edge: {edge};
      --grid-dot: {'rgba(18, 18, 20, 0.10)' if is_light else 'rgba(255, 255, 255, 0.09)'};
      --star: {'rgba(18, 18, 20, 0.38)' if is_light else 'rgba(255, 255, 255, 0.42)'};
      --star-accent: {accent}{'66' if is_light else '59'};
      --ease: cubic-bezier(.22, 1, .36, 1);
      --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; }}
    body {{
      margin: 0; overflow: hidden; display: grid; place-items: center;
      color: var(--fg); background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable", "Segoe UI", sans-serif;
      user-select: none; -webkit-font-smoothing: antialiased;
    }}

    /* background pulse #1 — a fine dot grid, drifting one cell per 30s.
       Masked to the center so the corners stay clean; the motion sits right
       at the edge of perception and just keeps the surface from feeling dead. */
    .grid {{
      position: absolute; inset: 0; z-index: 0; pointer-events: none;
      background-image: radial-gradient(var(--grid-dot) 1px, transparent 1.15px);
      background-size: 30px 30px;
      -webkit-mask-image: radial-gradient(ellipse 64% 58% at 50% 46%, #000 26%, transparent 76%);
      mask-image: radial-gradient(ellipse 64% 58% at 50% 46%, #000 26%, transparent 76%);
      animation: grid-drift 90s linear infinite;
    }}
    @keyframes grid-drift {{ to {{ background-position: 30px 30px; }} }}

    /* star glitter — three sparse twinkle layers over the static grid.
       Non-square tiles + off-grid origins + co-prime periods (6.4 / 8.9 /
       11.7s) composite into an asymmetric scatter: dots blink as
       individuals, never as a uniform wave. The parent's radial mask
       applies to the layers, so glitter fades toward the corners too. */
    .tw {{
      position: absolute; inset: 0;
      animation: grid-drift 90s linear infinite, twinkle var(--tw-period) ease-in-out infinite;
      animation-delay: 0s, var(--tw-delay);
    }}
    .t1 {{
      --tw-period: 6.4s; --tw-delay: -1.2s;
      background-image: radial-gradient(var(--star) 1.1px, transparent 1.7px);
      background-size: 140px 170px;
      background-position: 17px 43px;
    }}
    .t2 {{
      --tw-period: 8.9s; --tw-delay: -4.4s;
      background-image: radial-gradient(var(--star-accent) 1.2px, transparent 1.9px);
      background-size: 190px 130px;
      background-position: 101px 77px;
    }}
    .t3 {{
      --tw-period: 11.7s; --tw-delay: -7.8s;
      background-image: radial-gradient(var(--star) 1px, transparent 1.6px);
      background-size: 230px 260px;
      background-position: 59px 149px;
    }}
    /* blink hard once, flicker down, long dark wait — a twinkle, not a pulse */
    @keyframes twinkle {{
      0%   {{ opacity: 0; }}
      5%   {{ opacity: .9; }}
      12%  {{ opacity: .15; }}
      18%  {{ opacity: .55; }}
      28%  {{ opacity: 0; }}
      100% {{ opacity: 0; }}
    }}

    /* corner chrome — small caps mono, appears last, never competes */
    .chrome {{
      position: absolute; left: 0; right: 0; z-index: 2;
      display: flex; align-items: center; justify-content: space-between;
      padding: 26px 30px;
      color: var(--muted);
      font: 500 9px/1 var(--mono);
      letter-spacing: .18em; text-transform: uppercase;
      opacity: 0; animation: chrome-in .7s ease .62s forwards;
    }}
    .mast {{ top: 0; }}
    .foot {{ bottom: 0; }}
    .mast-item {{ display: inline-flex; align-items: center; gap: 8px; }}
    .mast-item.dim {{ opacity: .66; }}
    .dot {{
      width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
    }}

    /* the lockup — logo, wordmark, one hairline of progress */
    .lockup {{
      position: relative; z-index: 1;
      width: min(300px, calc(100vw - 56px));
      display: flex; flex-direction: column; align-items: center;
      transform: translateY(-1.2vh);
    }}
    .mark {{
      width: 64px; height: 64px; display: grid; place-items: center;
      opacity: 0; animation: rise .55s var(--ease) .06s forwards;
    }}
    .logo-img {{ width: 64px; height: 64px; object-fit: contain; display: block; }}
    .logo-fallback {{
      font-size: 44px; font-weight: 700; color: var(--accent); line-height: 1;
    }}
    h1 {{
      margin: 14px 0 0;
      font-size: 31px; line-height: 1; font-weight: 640;
      letter-spacing: -.045em; text-indent: -.045em;
      opacity: 0; animation: rise .55s var(--ease) .13s forwards;
    }}

    .boot {{
      width: 100%; margin-top: 46px;
      opacity: 0; animation: rise .55s var(--ease) .21s forwards;
    }}
    .readout {{
      display: flex; align-items: baseline; justify-content: space-between; gap: 16px;
      min-height: 15px; margin-bottom: 9px;
      font: 500 10px/1.4 var(--mono);
      letter-spacing: .08em; text-transform: uppercase;
      color: var(--muted);
    }}
    #status {{
      min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      transition: opacity .15s ease;
    }}
    #status::after {{
      content: ""; display: inline-block; width: 6px; height: 10px;
      margin-left: 7px; vertical-align: -1px; background: var(--accent);
      animation: caret 1.1s steps(1) infinite;
    }}
    #status.swap {{ opacity: 0; }}
    .pct {{
      color: var(--fg); font-variant-numeric: tabular-nums; letter-spacing: .05em;
    }}
    .rail {{
      width: 100%; height: 2px; background: var(--edge); border-radius: 1px;
      overflow: hidden;
    }}
    .fill {{
      width: 3%; height: 100%; background: var(--accent); border-radius: 1px;
      transition: width .55s var(--ease);
    }}

    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(7px); }}
      to   {{ opacity: 1; transform: translateY(0); }}
    }}
    @keyframes chrome-in {{ to {{ opacity: 1; }} }}
    @keyframes caret {{
      0%, 55% {{ opacity: 1; }}
      56%, 100% {{ opacity: 0; }}
    }}

    @media (max-width: 640px) {{
      .chrome {{ padding: 20px; }}
      .lockup {{ width: min(280px, calc(100vw - 40px)); transform: translateY(-2vh); }}
      h1 {{ font-size: 27px; }}
      .boot {{ margin-top: 38px; }}
    }}
    @media (max-height: 560px) {{
      .lockup {{ transform: scale(.88); }}
      .boot {{ margin-top: 30px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition-duration: .01ms !important; }}
      .mark, h1, .boot, .mast, .foot {{ opacity: 1; transform: none; }}
    }}
  </style>
  <script>
    window.__splashStage = function (label, pct) {{
      var status = document.getElementById('status');
      var fill = document.getElementById('fill');
      var readout = document.getElementById('pct');
      if (status && label && status.textContent !== label) {{
        status.classList.add('swap');
        setTimeout(function () {{
          status.textContent = label;
          status.classList.remove('swap');
        }}, 150);
      }}
      if (typeof pct === 'number') {{
        var value = Math.max(0, Math.min(100, pct));
        if (fill) fill.style.width = Math.max(3, value) + '%';
        if (readout) readout.textContent = Math.round(value) + '%';
      }}
    }};
  </script>
</body></html>"""


_ALREADY_RUNNING_HTML = """<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#12151c;color:#e6e6e6;font-family:system-ui,sans-serif">
  <div style="font-size:22px;font-weight:600">accuretta</div>
  <div style="margin-top:8px;font-size:13px;color:#8b93a3">already running — check your taskbar.</div>
</body>"""

_FAILED_HTML = ("<body style='font-family:system-ui,sans-serif;background:#12151c;color:#e6e6e6;padding:2rem'>"
                "<h2>Accuretta could not start its engine</h2>"
                f"<p>The local bridge did not come up on port {PORT}. Check the logs, then relaunch.</p></body>")


def _watch_bridge(win) -> None:
    """Close the window (exit the app) once the bridge stops responding, e.g.
    right after the in-app Shutdown button stops it."""
    time.sleep(3)
    misses = 0
    while True:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                misses = 0
        except OSError:
            misses += 1
            if misses >= 2:
                try:
                    win.destroy()
                except Exception:
                    pass
                return
        time.sleep(1)


def _set_app_icon() -> None:
    """Windows: replace the inherited python icon with accuretta.ico on the live
    window (titlebar + taskbar). Packaged builds get this from PyInstaller
    --icon; this covers running from source via pythonw."""
    if sys.platform != "win32":
        return
    ico = os.path.abspath("accuretta.ico")
    if not os.path.exists(ico):
        return
    try:
        import ctypes
        # Give the process its own taskbar identity so it doesn't ride under python.
        try:
            aumid = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            aumid.argtypes = [ctypes.c_wchar_p]
            aumid("Accuretta.Desktop")
        except Exception:
            pass
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.FindWindowW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.LoadImageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        hwnd = None
        for _ in range(80):  # wait up to ~8s for the window to exist
            hwnd = u.FindWindowW(None, "Accuretta")
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return
        big = u.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        small = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    except Exception:
        pass


def main() -> int:
    if not _acquire_single_instance():
        webview.create_window("Accuretta", html=_ALREADY_RUNNING_HTML, width=440, height=220)
        webview.start(gui="edgechromium" if sys.platform == "win32" else None)
        return 0

    # Show the branded splash immediately; boot the bridge in the background and
    # swap to the live app the moment it's ready.
    # text_select=True: pywebview disables page text selection by default, which
    # meant you couldn't highlight-and-copy part of a reply in the desktop app
    # (only the whole message via the copy button). Turning it on restores normal
    # selection + Ctrl+C + right-click copy. UI chrome stays unselectable via the
    # `user-select: none` CSS on buttons, the sidebar, and code line numbers.
    splash_shown_at = time.monotonic()
    win = webview.create_window("Accuretta", html=_build_splash_html(),
                                width=1440, height=920, min_size=(960, 640),
                                text_select=True)

    def _boot() -> None:
        def _stage(label: str, pct: int) -> None:
            # Push a live boot stage into the splash (label + progress fill).
            try:
                win.evaluate_js(f"window.__splashStage && window.__splashStage({label!r}, {pct})")
            except Exception:
                pass

        if not _port_in_use(PORT):
            _stage("Starting local bridge", 18)
            threading.Thread(target=_run_bridge, daemon=True).start()
        else:
            _stage("Bridge already online", 40)
        _stage("Waiting for local engine", 55)
        if _wait_ready():
            _stage("Opening workspace", 100)
            # Warm launches can finish before the opening choreography becomes
            # legible. Guarantee a short total presentation, but never pad a
            # genuinely slow boot beyond the final ready transition.
            elapsed = time.monotonic() - splash_shown_at
            time.sleep(max(0.38, 1.45 - elapsed))
            win.load_url(f"http://127.0.0.1:{PORT}")
            threading.Thread(target=_watch_bridge, args=(win,), daemon=True).start()
        else:
            win.load_html(_FAILED_HTML)

    # Swap the python icon for accuretta.ico once the window exists.
    threading.Thread(target=_set_app_icon, daemon=True).start()

    # gui='edgechromium' forces WebView2 on Windows for identical Chromium rendering.
    # private_mode=False + a stable storage_path: pywebview defaults to private
    # mode, which tells WebView2 to DISCARD localStorage/cookies between launches
    # — that wiped the cost widget's all-time token counters every restart (the
    # card read $0.00 until you chatted again). Pin the profile under the app's
    # own data dir (same place chats.json lives) so it persists like everything else.
    _storage = str(bridge.DATA / "webview")
    try:
        os.makedirs(_storage, exist_ok=True)
    except Exception:
        _storage = None
    webview.start(_boot, gui="edgechromium" if sys.platform == "win32" else None,
                  private_mode=False, storage_path=_storage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
