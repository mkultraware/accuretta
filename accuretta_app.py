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
        --add-data "bridge-client.js;." --add-data "preview-runtime.js;." --add-data "appearance.js;." ^
        --add-data "signal-field.js;." --add-data "dot-matrix.js;." --add-data "security-scan-field.js;." ^
        --add-data "agent-orb.js;." --add-data "ui-refresh.css;." ^
        --add-data "assets;assets" ^
        --add-data "colors_and_type.css;." --add-data "manifest.webmanifest;." ^
        --icon assets/icons/accuretta.ico accuretta_app.py

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
import json
import urllib.request
from pathlib import Path

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


def _wait_ready(host: str = "127.0.0.1", port: int = PORT, timeout: float = 180.0) -> bool:
    """Block until the bridge is accepting connections (or give up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as response:
                if json.load(response).get("application") == "accuretta":
                    return True
                return False
        except (OSError, ValueError):
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
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
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
_LIGHT_THEMES = {"", "light", "soft", "pastel", "retro", "neumorphic", "neobrutalism", "aperture", "folio"}
_KNOWN_THEMES = {
    "light", "dark", "dim", "retro", "aurora", "nebula", "operator",
    "neumorphic", "neobrutalism", "aperture", "aperture-dark", "atelier", "soft",
    "pastel", "velvet", "cartograph", "amaranth", "folio",
}
_THEME_ALIASES = {
    "neobrutalism": "amaranth",
    "neobrutalism-dark": "amaranth",
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
    try:
        colors = bridge.get_settings().get("custom_palettes", {}).get(theme)
        if isinstance(colors, dict) and all(isinstance(colors.get(k), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", colors[k]) for k in ("background", "surface", "accent")):
            pal["bg"] = colors["background"]
            pal["accent"] = colors["accent"]
            rgb = [int(pal["bg"][i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
            pal["fg"] = "#202024" if sum(v * w for v, w in zip(linear, (0.2126, 0.7152, 0.0722))) > 0.179 else "#faf9f6"
            pal["muted"] = "#" + "".join(format(int(int(pal["bg"][i:i + 2], 16) * 0.28 + int(pal["fg"][i:i + 2], 16) * 0.72 + 0.5), "02x") for i in (1, 3, 5))
    except (AttributeError, TypeError, ValueError):
        pass
    return pal


def _logo_data_uri(theme: str) -> str:
    """Base64-embed the theme-appropriate logo. The splash renders before the
    bridge serves files, so an HTTP asset path cannot load here."""
    fname = Path("assets/brand") / ("logo-mark-light.png" if theme in _LIGHT_THEMES else "logo-mark-dark.png")
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
    return "0.8.3"


def _signal_field_js() -> str:
    """The splash-only dot field, inlined into the intro. The splash is a
    self-contained HTML string with no http origin, so it can't <script src>
    the bridge. '' if the file is missing (splash simply runs without it)."""
    try:
        return (bridge.ROOT / "signal-field.js").read_text(encoding="utf-8")
    except Exception:
        return ""


def _dot_matrix_js() -> str:
    """dot-matrix.js, inlined so the splash draws the same matrix A the app
    does (one source for its geometry). '' if missing: the splash falls back
    to the PNG mark."""
    try:
        return (bridge.ROOT / "dot-matrix.js").read_text(encoding="utf-8")
    except Exception:
        return ""


def _is_dark(hex_color: str) -> bool:
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", (hex_color or "").strip())
    if not m:
        return False
    r, g, b = (int(m.group(1)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 0.5


def _build_splash_html() -> str:
    """Theme-aware boot splash, in the app's dot-matrix language.

    The saved theme supplies the palette (bg / fg / muted / accent), so the
    splash opens in the theme the app was last closed in. The matrix A
    (dot-matrix.js, inlined) is the progress indicator: its dots light from
    bottom left to top right as boot advances, the next few pulse at the
    front, and the whole A flashes once at 100%. Behind it, signal-field.js
    draws the same twinkling dot pool as the welcome screen, so the hand-off
    to the app reads as one surface.
    window.__splashStage(label, pct) stays the live progress contract.
    """
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo_theme = "light" if pal["fg"].lower() == "#202024" else "dark" if pal["fg"].lower() == "#faf9f6" else theme
    accent = pal["accent"]
    version = _read_app_version()
    dark = _is_dark(pal["bg"]) if pal["bg"].startswith("#") else theme not in _LIGHT_THEMES
    dm_l = "70%" if dark else "58%"
    signal_js = _signal_field_js()
    matrix_js = _dot_matrix_js()
    if matrix_js:
        mark_html = '<svg class="mx" id="mx" aria-hidden="true"></svg>'
    else:
        logo = _logo_data_uri(logo_theme)
        mark_html = (f'<img src="{logo}" class="logo-img" alt="">' if logo
                     else '<span class="logo-fallback">a</span>')
    scripts = "".join(f"  <script>\n{js}\n  </script>\n" for js in (signal_js, matrix_js) if js)

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: {pal['bg']}; --fg: {pal['fg']}; --muted: {pal['muted']}; --accent: {accent};
      --dm-l: {dm_l};
      --ease: cubic-bezier(.2, .8, .2, 1);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; }}
    body {{
      margin: 0; overflow: hidden; display: grid; place-items: center;
      color: var(--fg); background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable", "Segoe UI", sans-serif;
      user-select: none; -webkit-font-smoothing: antialiased;
    }}
    #signal-field {{ position: absolute; inset: 0; z-index: 0; pointer-events: none; }}

    .lockup {{
      position: relative; z-index: 1;
      display: flex; flex-direction: column; align-items: center;
      transform: translateY(-2vh);
    }}
    .mark {{
      width: 120px; height: 90px; display: grid; place-items: center;
      opacity: 0; animation: rise .6s var(--ease) .05s forwards;
    }}
    .mx {{ width: 120px; height: 90px; overflow: visible; }}
    .mx circle {{
      transform-box: fill-box; transform-origin: center;
      opacity: .13; transform: scale(.78);
      transition: opacity .32s ease var(--lag, 0s), transform .32s var(--ease) var(--lag, 0s);
    }}
    .mx circle.next {{ animation: frontier 1.3s ease-in-out var(--fd, 0s) infinite; }}
    .mx circle.lit {{
      opacity: 1; transform: none;
      animation: twinkle var(--p) ease-in-out calc(var(--d) + var(--lag, 0s)) infinite;
    }}
    body.ready .mx circle {{
      opacity: 1; transform: none;
      animation: ready .7s var(--ease) calc(var(--k) * .35s) both;
    }}
    .logo-img {{ width: 64px; height: 64px; object-fit: contain; display: block; }}
    .logo-fallback {{ font-size: 44px; font-weight: 700; color: var(--accent); line-height: 1; }}

    h1 {{
      margin: 22px 0 0;
      font-size: 22px; line-height: 1.2; font-weight: 500; letter-spacing: -.02em;
      opacity: 0; animation: rise .6s var(--ease) .14s forwards;
    }}
    .readout {{
      display: flex; align-items: baseline; justify-content: center; gap: 8px;
      min-height: 18px; margin-top: 10px;
      font-size: 12px; color: var(--muted);
      opacity: 0; animation: rise .6s var(--ease) .22s forwards;
    }}
    #status {{ max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: opacity .15s ease; }}
    #status.swap {{ opacity: 0; }}
    .sep {{ opacity: .5; }}
    .pct {{ font-variant-numeric: tabular-nums; color: var(--fg); opacity: .72; }}

    .foot {{
      position: absolute; left: 0; right: 0; bottom: 22px; z-index: 1;
      text-align: center; font-size: 11px; color: var(--muted); opacity: 0;
      animation: fade .7s ease .5s forwards;
    }}

    @keyframes rise {{
      from {{ opacity: 0; transform: translateY(6px); }}
      to   {{ opacity: 1; transform: none; }}
    }}
    @keyframes fade {{ to {{ opacity: .8; }} }}
    @keyframes twinkle {{
      0%, 100% {{ opacity: var(--lo); transform: scale(.88); }}
      12% {{ opacity: 1; transform: scale(1.12); }}
      40% {{ opacity: .8; transform: scale(1); }}
    }}
    @keyframes frontier {{
      0%, 100% {{ opacity: .13; transform: scale(.78); }}
      45% {{ opacity: .7; transform: scale(1.05); }}
    }}
    @keyframes ready {{
      0%   {{ opacity: .6; transform: scale(.9); }}
      35%  {{ opacity: 1;  transform: scale(1.3); }}
      100% {{ opacity: 1;  transform: scale(1); }}
    }}
    @media (max-height: 560px) {{ .lockup {{ transform: scale(.9); }} }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition-duration: .01ms !important; }}
      .mark, h1, .readout, .foot {{ opacity: 1; transform: none; }}
    }}
  </style>
</head>
<body>
  <canvas id="signal-field" aria-hidden="true"></canvas>

  <main class="lockup">
    <div class="mark">{mark_html}</div>
    <h1>Accuretta</h1>
    <div class="readout" aria-live="polite">
      <span id="status">Preparing local runtime</span><span class="sep">·</span><span class="pct" id="pct">0%</span>
    </div>
  </main>

  <footer class="foot">v{version}</footer>

{scripts}  <script>
    (function () {{
      // Order the A's dots along the notch's power-up diagonal (bottom left
      // to top right) so progress fills the logo in that direction.
      var svg = document.getElementById('mx');
      var dots = [];
      if (svg && window.AccDots) {{
        window.AccDots.buildA(svg, 16, {{ lo: [.6, .85] }});
        var vb = svg.viewBox.baseVal;
        dots = Array.prototype.slice.call(svg.querySelectorAll('circle')).map(function (c) {{
          var t = +c.getAttribute('cx') / vb.width, u = +c.getAttribute('cy') / vb.height;
          var k = (1 - u) * .5 + t * .5;
          c.style.setProperty('--k', k.toFixed(3));
          return {{ el: c, k: k }};
        }}).sort(function (a, b) {{ return a.k - b.k; }});
      }}
      var shown = 0;
      function paint(pct) {{
        if (!dots.length) return;
        var n = Math.round(Math.max(0, Math.min(1, pct / 100)) * dots.length);
        for (var i = 0; i < dots.length; i++) {{
          var el = dots[i].el;
          // Newly lit dots cascade in order instead of popping at once.
          if (i < n && !el.classList.contains('lit')) el.style.setProperty('--lag', ((i - shown) * .018).toFixed(3) + 's');
          el.classList.toggle('lit', i < n);
          var front = i >= n && i < n + 4;
          el.classList.toggle('next', front);
          if (front) el.style.setProperty('--fd', ((i - n) * .12).toFixed(2) + 's');
        }}
        shown = Math.max(shown, n);
        if (pct >= 100) document.body.classList.add('ready');
      }}
      window.__splashReadyAt = performance.now();
      window.__splashStage = function (label, pct) {{
        var status = document.getElementById('status');
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
          if (readout) readout.textContent = Math.round(value) + '%';
          paint(value);
        }}
      }};
      paint(3);
    }})();
  </script>
</body></html>"""


_ALREADY_RUNNING_HTML = """<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#12151c;color:#e6e6e6;font-family:system-ui,sans-serif">
  <div style="font-size:22px;font-weight:600">accuretta</div>
  <div style="margin-top:8px;font-size:13px;color:#8b93a3">already running — check your taskbar.</div>
</body>"""

_FAILED_HTML = ("<body style='font-family:system-ui,sans-serif;background:#12151c;color:#e6e6e6;padding:2rem'>"
                "<h2>Accuretta could not start its engine</h2>"
                f"<p>The local bridge did not come up on port {PORT}. Check the logs, then relaunch.</p></body>")


class DesktopWindowApi:
    def __init__(self):
        self._window = None

    def close_window(self):
        if self._window is not None:
            # Destroying synchronously inside the WebView2 bridge callback can
            # leave the callback waiting on the window it is trying to close.
            # Let the callback return, then close on the GUI thread.
            threading.Timer(0.05, self._window.destroy).start()
        return True


def _watch_bridge(win) -> None:
    """Close the window (exit the app) once the bridge stops responding, e.g.
    right after the in-app Shutdown button stops it."""
    time.sleep(3)
    misses = 0
    while True:
        if bridge._shutdown_ready.is_set():
            try:
                win.destroy()
            except Exception:
                pass
            return
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


_APP_ICON = str(bridge.ROOT / "assets" / "icons" / "accuretta.ico")


def _set_app_identity() -> None:
    """Windows: give the process its own taskbar identity so it doesn't group
    under python/pythonw (which also shows Python's icon). Must run before the
    first window is created or the taskbar has already bound it to python."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        aumid = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        aumid.argtypes = [ctypes.c_wchar_p]
        aumid("Accuretta.Desktop")
    except Exception:
        pass


def main() -> int:
    if not _acquire_single_instance():
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.windll.user32
            user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
            user32.FindWindowW.restype = ctypes.c_void_p
            user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
            hwnd = user32.FindWindowW(None, "Accuretta")
            if hwnd:
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
        return 0

    # Show the branded splash immediately; boot the bridge in the background and
    # swap to the live app the moment it's ready.
    # text_select=True: pywebview disables page text selection by default, which
    # meant you couldn't highlight-and-copy part of a reply in the desktop app
    # (only the whole message via the copy button). Turning it on restores normal
    # selection + Ctrl+C + right-click copy. UI chrome stays unselectable via the
    # `user-select: none` CSS on buttons, the sidebar, and code line numbers.
    _set_app_identity()
    splash_created_at = time.monotonic()
    desktop_api = DesktopWindowApi()
    win = webview.create_window("Accuretta", html=_build_splash_html(), js_api=desktop_api,
                                width=1440, height=920, min_size=(960, 640),
                                text_select=True)
    desktop_api._window = win

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
                  private_mode=False, storage_path=_storage,
                  # Without this pywebview stamps python.exe's icon on the window.
                  icon=_APP_ICON if os.path.isfile(_APP_ICON) else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
