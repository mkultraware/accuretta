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

import os
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


_SPLASH_HTML = """<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#12151c;color:#e6e6e6;font-family:system-ui,-apple-system,Segoe UI,sans-serif;user-select:none">
  <div style="font-size:30px;font-weight:600;letter-spacing:.01em">accuretta</div>
  <div style="margin-top:10px;font-size:13px;color:#8b93a3">starting your engine…</div>
  <div style="margin-top:20px;width:180px;height:3px;background:#232834;border-radius:3px;overflow:hidden">
    <div style="width:38%;height:100%;background:#c084fc;border-radius:3px;animation:sl 1.1s ease-in-out infinite"></div>
  </div>
  <style>@keyframes sl{0%{transform:translateX(-110%)}100%{transform:translateX(420%)}}</style>
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
    win = webview.create_window("Accuretta", html=_SPLASH_HTML,
                                width=1440, height=920, min_size=(960, 640))

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
    webview.start(_boot, gui="edgechromium" if sys.platform == "win32" else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
