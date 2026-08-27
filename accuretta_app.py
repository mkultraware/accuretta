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
        --add-data "signal-field.js;." ^
        --add-data "notification.mp3;." ^
        --add-data "notification-error.wav;." ^
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


# Themes whose splash background needs the dark-ink logo mark. Keep this in
# sync with the actual palette luminance, including the softer and pastel sets.
_LIGHT_THEMES = {"", "light", "soft", "pastel", "retro", "neumorphic", "neobrutalism", "aperture"}
_KNOWN_THEMES = {
    "light", "dark", "dim", "retro", "aurora", "nebula", "operator",
    "neumorphic", "neobrutalism", "aperture", "aperture-dark", "soft",
    "pastel", "velvet", "cartograph",
}
_THEME_ALIASES = {
    "neobrutalism-dark": "aperture",
    "kinetic": "aperture",
}


def _read_saved_theme() -> str:
    """The last theme the user saved (settings.json). '' / unknown -> light."""
    try:
        theme = str(bridge.get_settings().get("theme") or "").strip().lower()
        theme = _THEME_ALIASES.get(theme, theme)
        return theme if theme in _KNOWN_THEMES else ""
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
    fname = "logo-mark-light.png" if theme in _LIGHT_THEMES else "logo-mark-dark.png"
    try:
        raw = (bridge.ROOT / fname).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _read_app_version() -> str:
    """The release number shown in the app (index.html #brand-v). The splash
    footer displays it, so index.html stays the single source of truth.
    Falls back to the last shipped version if the file can't be read."""
    try:
        html = (bridge.ROOT / "index.html").read_text(encoding="utf-8")
        m = re.search(r'id="brand-v">v(\d+\.\d+\.\d+)<', html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "0.7.8"


def _signal_field_js() -> str:
    """The splash-only signal-field effect, inlined into the intro. The splash is a
    self-contained HTML string with no http origin, so it can't <script src>
    the bridge. '' if the file is missing (splash simply runs without it)."""
    try:
        return (bridge.ROOT / "signal-field.js").read_text(encoding="utf-8")
    except Exception:
        return ""


def _build_splash_html() -> str:
    """Theme-aware boot splash — quiet, flat, typographic.

    Design rules: the saved theme supplies the entire palette (bg / fg /
    muted / accent) and the matching logo variant; every surface is FLAT —
    no blur, no translucency, no glow, no gradient field, no film grain.
    The signal field (signal-field.js, inlined) replaces the old static dot
    grid: a live dot field whose points light up with two slow travelling
    waves, tinted by the theme's --fg / --accent.
    Staggered one-shot twinkles cover a warm launch, while eight quiet repeaters
    keep a slow boot alive without turning the background into a light show.
    window.__splashStage(label, pct) stays the live progress contract.
    """
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo = _logo_data_uri(theme)
    accent = pal["accent"]
    theme_label = re.sub(r"[^a-z0-9 -]", "", (theme or "light").replace("-", " ")).strip() or "light"
    version = _read_app_version()
    logo_html = (f'<img src="{logo}" class="logo-img" alt="">' if logo
                 else '<span class="logo-fallback">a</span>')
    signal_js = _signal_field_js()
    signal_script = (f'  <script>\n{signal_js}\n  </script>\n' if signal_js else "")
    stars = (
        (7, 18, 2.1, 0.03, 0.48, "repeat", 4.1),
        (14, 72, 2.3, 0.46, 0.54, "accent repeat", 3.7),
        (21, 31, 1.1, 0.19, 0.43, "", 6.2),
        (27, 84, 1.0, 0.83, 0.49, "", 5.4),
        (33, 15, 1.9, 0.58, 0.46, "accent repeat", 4.5),
        (39, 25, 1.0, 0.31, 0.51, "", 5.8),
        (46, 10, 1.2, 0.86, 0.45, "", 6.3),
        (55, 20, 2.1, 0.11, 0.53, "repeat", 3.9),
        (63, 13, 2.4, 0.70, 0.47, "accent repeat", 4.7),
        (71, 28, 1.0, 0.38, 0.44, "", 5.6),
        (79, 17, 1.2, 0.91, 0.48, "", 6.1),
        (88, 34, 1.1, 0.24, 0.56, "accent", 5.9),
        (94, 61, 1.0, 0.77, 0.43, "", 5.3),
        (85, 79, 2.2, 0.52, 0.50, "repeat", 3.5),
        (75, 90, 1.0, 0.08, 0.46, "accent", 6.4),
        (66, 73, 1.2, 0.89, 0.52, "", 5.8),
        (58, 87, 1.0, 0.34, 0.42, "", 5.6),
        (45, 92, 2.0, 0.64, 0.55, "accent repeat", 4.3),
        (34, 75, 1.0, 0.15, 0.47, "", 5.4),
        (23, 62, 1.2, 0.88, 0.50, "", 6.0),
        (11, 49, 1.0, 0.41, 0.44, "", 5.7),
        (92, 11, 1.9, 0.61, 0.49, "accent repeat", 3.8),
        (18, 44, 1.0, 0.27, 0.48, "", 5.5),
        (30, 56, 1.2, 0.73, 0.45, "accent", 6.0),
        (70, 52, 1.0, 0.47, 0.51, "", 5.3),
        (83, 47, 1.2, 0.17, 0.46, "", 6.4),
    )
    star_html = "".join(
        f'<i class="star {kind}" style="--x:{x}%;--y:{y}%;--s:{size}px;'
        f'--delay:{delay}s;--duration:{duration}s;--cycle:{cycle}s;'
        f'--repeat-delay:{-((x * 0.071 + y * 0.037) % cycle):.2f}s"></i>'
        for x, y, size, delay, duration, kind, cycle in stars
    )

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: {pal['bg']}; --fg: {pal['fg']}; --muted: {pal['muted']}; --accent: {accent};
      --edge: color-mix(in srgb, var(--fg) 15%, transparent);
      --star: color-mix(in srgb, var(--fg) 58%, transparent);
      --star-accent: color-mix(in srgb, var(--accent) 62%, transparent);
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

    /* background pulse #1 — the signal field: a live dot grid whose points
       light up with two slow travelling waves (signal-field.js, inlined).
       Masked to the center so the corners stay clean, like the old grid. */
    #signal-field {{
      position: absolute; inset: 0; z-index: 0; pointer-events: none;
      -webkit-mask-image: radial-gradient(ellipse 64% 58% at 50% 46%, #000 26%, transparent 76%);
      mask-image: radial-gradient(ellipse 64% 58% at 50% 46%, #000 26%, transparent 76%);
    }}

    .stars {{
      position: absolute; inset: 0; z-index: 0; pointer-events: none;
      -webkit-mask-image: radial-gradient(ellipse 92% 88% at 50% 48%, #000 28%, rgba(0, 0, 0, .92) 72%, transparent 100%);
      mask-image: radial-gradient(ellipse 92% 88% at 50% 48%, #000 28%, rgba(0, 0, 0, .92) 72%, transparent 100%);
    }}
    .star {{
      position: absolute;
      left: var(--x); top: var(--y);
      width: var(--s); height: var(--s);
      border-radius: 50%;
      color: var(--star);
      background: currentColor;
      opacity: 0;
      animation: star-twinkle var(--duration) ease-out var(--delay) both;
    }}
    .star.accent {{ color: var(--star-accent); }}
    .star.repeat {{
      box-shadow: 0 0 6px currentColor;
      animation:
        star-twinkle var(--duration) ease-out var(--delay) both,
        star-repeat var(--cycle) linear var(--repeat-delay) infinite;
    }}
    .star.repeat::before,
    .star.repeat::after {{
      content: "";
      position: absolute; left: 50%; top: 50%;
      background: currentColor;
      transform: translate(-50%, -50%);
    }}
    .star.repeat::before {{ width: calc(var(--s) * 3.4); height: 1px; }}
    .star.repeat::after {{ width: 1px; height: calc(var(--s) * 3.4); }}
    @keyframes star-twinkle {{
      0%   {{ opacity: 0; transform: scale(.45); }}
      28%  {{ opacity: .78; transform: scale(1); }}
      48%  {{ opacity: .18; transform: scale(.72); }}
      68%  {{ opacity: .46; transform: scale(.92); }}
      100% {{ opacity: 0; transform: scale(.5); }}
    }}
    @keyframes star-repeat {{
      0%, 100% {{ opacity: 0; transform: scale(.52); }}
      5% {{ opacity: .88; transform: scale(1); }}
      9% {{ opacity: .14; transform: scale(.68); }}
      14% {{ opacity: .52; transform: scale(.92); }}
      21% {{ opacity: 0; transform: scale(.55); }}
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
      .star {{ display: none; }}
    }}
  </style>
</head>
<body>
  <canvas id="signal-field" aria-hidden="true"></canvas>
  <div class="stars" aria-hidden="true">
    {star_html}
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
    <span>version: {version}</span>
    <span>your model · your machine</span>
  </footer>

  {signal_script}  <script>
    window.__splashReadyAt = performance.now();
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
    splash_created_at = time.monotonic()
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
            # Measure from the splash document's final script, not from Python's
            # pre-window setup. That keeps the guaranteed warm-launch interval
            # tied to content the user could actually see.
            try:
                elapsed_ms = win.evaluate_js(
                    "window.__splashReadyAt == null ? null : "
                    "performance.now() - window.__splashReadyAt"
                )
                elapsed = float(elapsed_ms) / 1000.0
                if not 0.0 <= elapsed < 3600.0:
                    raise ValueError("invalid splash clock")
            except Exception:
                elapsed = time.monotonic() - splash_created_at
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
