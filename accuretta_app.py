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


# Themes whose splash uses a light background + the DARK-colored logo mark.
# Mirrors app.css: :is([data-theme="light"],[data-theme="soft"]) shows .splash-logo-dark.
_LIGHT_THEMES = {"", "light", "soft"}


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
    """Branded boot splash that honors the last saved theme and shows a subtly
    animated logo. Rebuilt each launch, so a theme change is reflected next boot."""
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo = _logo_data_uri(theme)
    accent = pal["accent"]
    
    is_light = theme in _LIGHT_THEMES
    grid_color = "rgba(0, 0, 0, 0.02)" if is_light else "rgba(255, 255, 255, 0.012)"
    
    if accent.startswith("#") and len(accent) == 7:
        glow_color = accent + "44"
        glow_color_faint = accent + "18"
    else:
        glow_color = "rgba(56, 189, 248, 0.25)"
        glow_color_faint = "rgba(56, 189, 248, 0.1)"
        
    loader_track_bg = "rgba(0, 0, 0, 0.05)" if is_light else "rgba(255, 255, 255, 0.06)"
    
    logo_html = (
        f'<div class="logo-wrapper">'
        f'<div class="logo-ring-outer"></div>'
        f'<div class="logo-ring-inner"></div>'
        f'<img src="{logo}" class="logo-img" alt="">'
        f'</div>'
        if logo else ""
    )
    
    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:{pal['bg']};color:{pal['fg']};font-family:system-ui,-apple-system,'Segoe UI',sans-serif;user-select:none;overflow:hidden;position:relative">
  <div class="grid"></div>
  <div class="content">
    {logo_html}
    <h1 class="title">accuretta</h1>
    <p class="subtitle">starting your engine…</p>
    <div class="loader-track">
      <div class="loader-bar"></div>
    </div>
  </div>
  <style>
    .grid {{
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background-image: 
        radial-gradient(circle at center, transparent 30%, {pal['bg']} 85%),
        linear-gradient({grid_color} 1px, transparent 1px),
        linear-gradient(90deg, {grid_color} 1px, transparent 1px);
      background-size: 100% 100%, 36px 36px, 36px 36px;
      background-position: center;
      z-index: 1;
      pointer-events: none;
    }}
    .content {{
      position: relative;
      z-index: 2;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
    }}
    .logo-wrapper {{
      position: relative;
      width: 130px;
      height: 130px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin-bottom: 20px;
      animation: logo-reveal 1.2s cubic-bezier(0.19, 1, 0.22, 1) both;
    }}
    .logo-ring-inner {{
      position: absolute;
      width: 96px;
      height: 96px;
      border-radius: 50%;
      border: 2px solid transparent;
      border-top-color: {accent};
      border-bottom-color: {accent};
      opacity: 0.35;
      animation: spin-clockwise 2.8s linear infinite;
    }}
    .logo-ring-outer {{
      position: absolute;
      width: 122px;
      height: 122px;
      border-radius: 50%;
      border: 1px dashed {accent};
      opacity: 0.12;
      animation: spin-counter-clockwise 10s linear infinite;
    }}
    .logo-img {{
      width: 60px;
      height: 60px;
      object-fit: contain;
      z-index: 3;
      filter: drop-shadow(0 0 16px {glow_color});
      animation: breathe 3s ease-in-out infinite;
    }}
    .title {{
      margin: 16px 0 0 0;
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0.22em;
      text-indent: 0.22em; /* offset letter-spacing on last char */
      text-transform: lowercase;
      text-align: center;
      opacity: 0;
      animation: title-reveal 1.4s cubic-bezier(0.19, 1, 0.22, 1) 0.25s both;
    }}
    .subtitle {{
      margin: 8px 0 0 0;
      font-size: 13px;
      color: {pal['muted']};
      letter-spacing: 0.05em;
      text-align: center;
      opacity: 0;
      animation: fade-in 1s ease-out 0.6s both;
    }}
    .loader-track {{
      margin-top: 24px;
      width: 140px;
      height: 2px;
      background: {loader_track_bg};
      border-radius: 1px;
      overflow: hidden;
      position: relative;
      opacity: 0;
      animation: fade-in 1s ease-out 0.8s both;
    }}
    .loader-bar {{
      position: absolute;
      top: 0;
      left: 0;
      height: 100%;
      width: 50px;
      background: linear-gradient(90deg, transparent, {accent}, transparent);
      animation: loading-slide 1.6s cubic-bezier(0.4, 0, 0.2, 1) infinite;
    }}
    @keyframes spin-clockwise {{
      0% {{ transform: rotate(0deg); }}
      100% {{ transform: rotate(360deg); }}
    }}
    @keyframes spin-counter-clockwise {{
      0% {{ transform: rotate(360deg); }}
      100% {{ transform: rotate(0deg); }}
    }}
    @keyframes logo-reveal {{
      0% {{
        transform: scale(0.85);
        opacity: 0;
      }}
      100% {{
        transform: scale(1);
        opacity: 1;
      }}
    }}
    @keyframes breathe {{
      0%, 100% {{ transform: scale(1); opacity: 0.95; }}
      50% {{ transform: scale(1.05); opacity: 1; }}
    }}
    @keyframes title-reveal {{
      0% {{
        letter-spacing: 0.1em;
        text-indent: 0.1em;
        opacity: 0;
      }}
      100% {{
        letter-spacing: 0.22em;
        text-indent: 0.22em;
        opacity: 1;
      }}
    }}
    @keyframes loading-slide {{
      0% {{
        left: -50px;
      }}
      100% {{
        left: 140px;
      }}
    }}
    @keyframes fade-in {{
      0% {{
        opacity: 0;
      }}
      100% {{
        opacity: 1;
      }}
    }}
  </style>
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
    win = webview.create_window("Accuretta", html=_build_splash_html(),
                                width=1440, height=920, min_size=(960, 640),
                                text_select=True)

    def _boot() -> None:
        if not _port_in_use(PORT):
            threading.Thread(target=_run_bridge, daemon=True).start()
        if _wait_ready():
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
