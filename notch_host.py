"""
Accuretta Notch host — a transparent, always-on-top, click-through overlay
window that renders accuretta-notch.html on top of the desktop.

Why not pywebview: on Windows pywebview hosts WebView2 inside a WinForms
window, which cannot do per-pixel transparency (it showed a solid grey
rectangle around the notch) and, per its own release notes, transparent
Edge-Chromium windows do not receive mouse/keyboard input. This host instead
creates a RAW WS_POPUP window via ctypes and hands its HWND to the WebView2
.NET API (the assemblies pywebview already bundles, driven through pythonnet),
which composites per-pixel alpha through DirectComposition. No second
runtime, no Electron, no colour-key tricks.

Window contract (from the notch build brief):
  - frameless, transparent, always on top, not in taskbar, not in Alt-Tab
  - ONE fixed size: 680 x 560 DIP, top edge of the primary display, centred
  - never steals keyboard focus: gates may arrive while the owner types in
    another app; focus only moves in when the owner clicks the island
  - click-through: only the island itself captures the pointer; everything
    else passes to the window underneath (a window REGION tracks the island
    rect reported by notch-wire.js)

The host dies with the bridge: it polls /api/health and exits after a few
misses, and bridge.py also terminates this process directly on shutdown or
when the "notch" setting is turned off.
"""

import ctypes
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))
LOOPBACK = ".".join(("127", "0", "0", "1"))
BASE = f"http://{LOOPBACK}:{PORT}"
PAGE_URL = f"{BASE}/notch"
USER_DATA = ROOT / "data" / "notch-webview2"
LOG_FILE = ROOT / "data" / "notch_host.log"

WIDTH_DIP = 680
HEIGHT_DIP = 560

POLL_MS = 40                # cursor / click / capture polling
HEALTH_EVERY = 50           # ticks between bridge health checks (~2 s)
HEALTH_MAX_MISS = 3
NAV_RETRY_S = 5.0           # the bridge may spend minutes loading a model
                            # before it serves — retry navigation patiently

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
gdi32 = ctypes.windll.gdi32

# ---- win32 constants ---------------------------------------------------------
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

GWL_EXSTYLE = -20
RGN_OR = 0x00000002
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_SHOWWINDOW = 0x0040
HWND_TOPMOST = -1
SM_CXSCREEN = 0
SM_CYSCREEN = 1
VK_LBUTTON = 0x01
VK_SPACE = 0x20
VK_MENU = 0x12             # Alt
VK_CONTROL = 0x11
SW_HIDE = 0
SW_SHOWNA = 8
SW_RESTORE = 9
MAIN_WINDOW_TITLE = "Accuretta"   # pywebview window created in accuretta_app.py
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# ---- window messages --------------------------------------------------------
WM_DESTROY = 0x0002
WM_SETFOCUS = 0x0007
WM_KILLFOCUS = 0x0008
WM_MOUSEACTIVATE = 0x0021
WM_MOUSELEAVE = 0x02A3
WM_KEYDOWN = 0x0100
WM_CHAR = 0x0102
WM_SYSKEYDOWN = 0x0101
WM_HOTKEY = 0x0312
WM_ACTIVATE = 0x0006
WM_ACTIVATEAPP = 0x001C
MA_ACTIVATE = 0x0001
MA_NOACTIVATE = 0x0003
WA_INACTIVE = 0
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000
GWL_WNDPROC = -4
HOTKEY_ID = 0xB0C4

# Prototypes matter on Win64: without them ctypes treats every argument and
# return value as a 32-bit int and truncates handles (HWND, HINSTANCE...).
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_longlong]
user32.SetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_longlong
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SetFocus.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wintypes.ATOM
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetKeyState.argtypes = [ctypes.c_int]
user32.GetKeyState.restype = ctypes.c_short
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                   wintypes.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_longlong
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
user32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int
gdi32.CreateRectRgn.argtypes = [ctypes.c_int] * 4
gdi32.CreateRectRgn.restype = ctypes.c_void_p
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
gdi32.CombineRgn.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.SetActiveWindow.argtypes = [wintypes.HWND]
user32.SetActiveWindow.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _find_main_window() -> int:
    """The desktop app's REAL pywebview window titled 'Accuretta', or 0 when it
    isn't running (headless bridge / browser-only mode).

    FindWindowW alone is not safe here: Windows 11's shell creates a
    `Windows.Internal.Shell.TabProxyWindow` carrying the SAME title for the
    taskbar/Alt-Tab, and FindWindow returns that ghost first — raising it does
    nothing, so "Open in main app" silently went nowhere. Enumerate top-level
    windows, skip shell proxies, and take the first real match in Z-order."""
    found: list[int] = []

    def _visit(hwnd, _lparam):
        try:
            title = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            if title.value != MAIN_WINDOW_TITLE:
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value.startswith("Windows.Internal.Shell"):
                return True              # tab/Alt-Tab proxy, not the app
            found.append(int(hwnd))
            return False                 # first real match wins
        except Exception:
            return True

    try:
        user32.EnumWindows(_ENUM_PROC(_visit), 0)
    except Exception:
        return 0
    return found[0] if found else 0


def _raise_main_window() -> bool:
    """Restore + foreground the desktop window. False when there is no such
    window (caller then opens the URL in the default browser instead)."""
    hwnd = _find_main_window()
    if not hwnd:
        return False
    try:
        # SW_RESTORE both un-minimizes and activates; harmless on a normal
        # window, and it is what actually brings a minimized app back.
        user32.ShowWindow(hwnd, SW_RESTORE)
        # SetForegroundWindow is ignored unless our thread owns the
        # foreground; attach briefly to whoever does (same dance as activate).
        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = kernel32.GetCurrentThreadId()
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        user32.BringWindowToTop(hwnd)
        raised = user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
        log(f"raised main window {hwnd} (SetForeground={bool(raised)} "
            f"now_fg={user32.GetForegroundWindow() == hwnd} "
            f"iconic={bool(user32.IsIconic(hwnd))})")
        return True
    except Exception as exc:
        log(f"raise main window failed: {exc!r}")
        return False


_LOG_FH = None


def log(msg: str) -> None:
    # One persistent line-buffered handle: the open/write/close cycle ran on
    # the window's UI thread for every page message (each keystroke's diag
    # included), and the file churn was part of the typing lag.
    global _LOG_FH
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _LOG_FH is None or _LOG_FH.closed:
            _LOG_FH = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        _LOG_FH.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        try:
            if _LOG_FH is not None:
                _LOG_FH.close()
        except Exception:
            pass
        _LOG_FH = None


def _webview2_lib_dir() -> Path:
    """pywebview bundles the WebView2 .NET assemblies under webview/lib."""
    spec = importlib.util.find_spec("webview")
    if spec and spec.submodule_search_locations:
        lib = Path(list(spec.submodule_search_locations)[0]) / "lib"
        if (lib / "Microsoft.Web.WebView2.Core.dll").is_file():
            return lib
    raise RuntimeError("WebView2 interop assemblies not found (is pywebview installed?)")


def _make_dpi_aware() -> None:
    try:
        user32.SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


def _window_dpi(hwnd) -> int:
    try:
        return user32.GetDpiForWindow(hwnd) or 96
    except Exception:
        return 96


class NotchHost:
    def __init__(self):
        self.hwnd = None
        self.dpi = 96
        self.win_x = 0
        self.win_w = WIDTH_DIP
        self.win_h = HEIGHT_DIP
        # island rect in page CSS px, as reported by notch-wire.js
        self.island = None          # (x, y, w, h)
        self.island_state = "idle"
        self.lmb_was_down = False
        self.activated = False      # True while the overlay owns focus (post-click)
        self.chord_was_down = False # (kept for compatibility; hotkey is now a message)
        self.visible = True         # island shown on screen
        self.sticky = False         # owner asked for it to stay up
        self.gate_visible_until = 0.0
        self.exit_requested = False
        self._stop = threading.Event()
        self._wndproc_cb = None     # keep the subclass alive
        self._old_wndproc = 0
        self._key_events = 0        # WM_KEYDOWN seen at the window
        self._char_events = 0       # WM_CHAR seen at the window
        self._last_win_key = 0
        self._logged_keys = False
        self._revealed_once = False
        self._hide_at = 0.0          # pending collapse-then-hide deadline
        self._hide_reason = ""
        self._leaving = False      # slide-out in flight (window still shown)
        self._leave_started = 0.0
        self.health_misses = 0
        self.seen_alive = False       # health-exit only counts after first contact
        self.tick_count = 0
        self.nav_retry_due = 0.0
        self.last_screen_cx = 0
        self.core = None
        self.controller = None

    # ---- raw window ---------------------------------------------------------
    def create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        wndproc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = wndproc.value
        wc.hInstance = hinst
        wc.lpszClassName = "AccurettaNotchHost"
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = kernel32.GetLastError()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                raise ctypes.WinError(err)

        # Click-through is enforced by a window REGION tracking the island
        # (see _apply_region), not WS_EX_TRANSPARENT: Chromium's child windows
        # ignore WS_EX_TRANSPARENT, but a top-level region clips hit-testing
        # for the whole window tree. Start fully click-through (empty region).
        ex = (WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        hwnd = user32.CreateWindowExW(
            ex, wc.lpszClassName, "Accuretta Notch",
            WS_POPUP, 0, 0, 100, 100,
            None, None, hinst, None,
        )
        if not hwnd:
            raise ctypes.WinError(kernel32.GetLastError())
        self.hwnd = hwnd
        self.dpi = _window_dpi(hwnd)
        self.reposition(initial=True)
        user32.SetWindowRgn(hwnd, gdi32.CreateRectRgn(0, 0, 0, 0), True)
        # Starts tucked away: the island only appears on Ctrl+Alt+Space or when
        # something pops up (an approval gate, model ready, an error).
        self.visible = False
        self.gate_visible_until = time.monotonic() + self.FIRST_SHOW_S
        user32.ShowWindow(hwnd, SW_HIDE)
        self._install_wndproc()

    def reposition(self, initial: bool = False) -> None:
        """Anchor to the top edge of the primary display, centred, in DIPs."""
        scale = self.dpi / 96.0
        self.win_w = int(WIDTH_DIP * scale + 0.5)
        self.win_h = int(HEIGHT_DIP * scale + 0.5)
        cx = user32.GetSystemMetrics(SM_CXSCREEN)
        self.last_screen_cx = cx
        self.win_x = max(0, (cx - self.win_w) // 2)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, self.win_x, 0,
                            self.win_w, self.win_h,
                            SWP_NOACTIVATE | (SWP_SHOWWINDOW if initial else 0))
        self._apply_region()
        if self.controller is not None:
            from System.Drawing import Rectangle
            self.controller.Bounds = Rectangle(0, 0, self.win_w, self.win_h)

    # ---- click-through via window region --------------------------------------
    # The island is the only part of the overlay that may capture the pointer.
    # A top-level window region clips BOTH painting and hit-testing for the
    # whole window tree (Chromium's children ignore WS_EX_TRANSPARENT, which
    # is why the region approach is used at all). The region is inflated by a
    # per-state pad so the island's rim glow / soft shadow is not clipped;
    # clicks in the pad are swallowed, clicks further out pass through.
    _RADIUS_DIP = {"idle": 18, "off": 18, "work": 24, "ready": 24,
                   "alert": 24, "gate": 32, "prompt": 32}
    _PAD_DIP = {"idle": 4, "off": 4, "work": 12, "ready": 12,
                "alert": 40, "gate": 48, "prompt": 60}

    def _island_screen_rect(self):
        if not self.island:
            return None
        scale = self.dpi / 96.0
        x, y, w, h = self.island
        return (self.win_x + int(x * scale), int(y * scale),
                int(w * scale + 0.5), int(h * scale + 0.5))

    def _apply_region(self) -> None:
        if not self.island or self.hwnd is None:
            return
        scale = self.dpi / 96.0
        x, y, w, h = self.island
        state = self.island_state
        pad = int(self._PAD_DIP.get(state, 8) * scale)
        radius = int(self._RADIUS_DIP.get(state, 18) * scale)
        left = max(0, int(x * scale) - pad)
        top = max(0, int(y * scale) - (pad if y > 0 else 0))
        right = int((x + w) * scale + 0.5) + pad
        bottom = int((y + h) * scale + 0.5) + pad
        # The island is a RECTANGLE at the top (flush with the screen edge) with
        # rounded BOTTOM corners only. A plain roundrect region would clip the
        # square top into a pill, so union a full rect with a rounded band.
        # CombineRgn needs a real destination handle (it returns NULL for a
        # NULL dst), so start from an empty region.
        r = max(1, min(radius, (bottom - top) // 2))
        body = gdi32.CreateRectRgn(left, top, right, bottom - r)
        band = gdi32.CreateRoundRectRgn(left, bottom - 2 * r, right, bottom, r * 2, r * 2)
        merged = gdi32.CreateRectRgn(0, 0, 0, 0)
        gdi32.CombineRgn(merged, body, band, RGN_OR)
        gdi32.DeleteObject(body)
        gdi32.DeleteObject(band)
        if merged:
            user32.SetWindowRgn(self.hwnd, merged, True)

    # ---- visibility: hidden unless the owner asks or something pops up -------
    # The island is tucked away by default. Ctrl+Alt+Space toggles it globally
    # (works while another app has focus); approvals, model-ready and errors
    # reveal it on their own, and it fades back once nothing is pending.
    SHOW_GRACE_S = 6.0            # how long a notification keeps it up
    FIRST_SHOW_S = 6.0            # one confirmation when the overlay starts
    COLLAPSE_HIDE_S = 0.62        # widget spring is 0.5s: let it land first
    LEAVE_SLIDE_S = 0.34        # slide-up into the bezel before we hide

    def show_island(self, reason: str = "") -> None:
        if self.visible:
            self.gate_visible_until = max(self.gate_visible_until, time.monotonic() + self.SHOW_GRACE_S)
            return
        # apply the region first, then drop the window down from the bezel
        self._leaving = False
        self._apply_region()
        user32.ShowWindow(self.hwnd, SW_SHOWNA)  # 8: show, no activation
        self.visible = True
        self.gate_visible_until = time.monotonic() + self.SHOW_GRACE_S
        self._to_page({"type": "enter"})
        log(f"shown ({reason})")
        self._request_rect_refresh()

    def hide_island(self, reason: str = "") -> None:
        if not self.visible:
            return
        # Slide up into the bezel rather than blinking out, then hide once the
        # page says the animation has settled.
        self._leaving = True
        self._leave_started = time.monotonic()
        self._to_page({"type": "leave"})
        self.visible = False
        self.gate_visible_until = 0.0
        log(f"leaving ({reason})")

    def _finish_hide(self) -> None:
        """Called after the slide-out completes."""
        if not self._leaving:
            return
        self._leaving = False
        try:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self.island = None
            log("hidden")
        except Exception as exc:
            log(f"hide: {exc!r}")

    def _to_page(self, msg: dict) -> None:
        """Host -> page message (the widget wiring listens for these)."""
        try:
            if self.core is not None:
                self.core.PostWebMessageAsJson(json.dumps(msg))
        except Exception as exc:
            log(f"to_page: {exc!r}")

    def _request_rect_refresh(self) -> None:
        """A hidden page stops sending rAF frames, so ask it to re-report."""
        self._to_page({"type": "refresh-rect"})

    def _reveal_once(self) -> None:
        if self._revealed_once:
            return
        self._revealed_once = True
        self.show_island("first run")

    def _toggle_island(self, reason: str = "") -> None:
        if self.visible:
            self._request_collapse_and_hide(reason or "toggle")
        else:
            # Route through show_island: the page needs the "enter" message or it
            # keeps the `is-leaving` class, which parks the island above the top
            # screen edge at opacity 0. The window comes back but the owner sees
            # nothing — the hotkey looks dead from the second press onwards.
            self.sticky = True
            self.show_island(reason or "hotkey")

    def _request_collapse_and_hide(self, reason: str) -> None:
        """Ctrl+Alt+Space: always a full toggle. The island shrinks to the
        minimal pill, the widget's spring plays, then it retreats into the
        bezel. A pending approval is not held hostage to the overlay: it stays
        pending on the bridge (and still notifies), it just isn't pinned open."""
        self._hide_at = time.monotonic() + self.COLLAPSE_HIDE_S
        self._hide_reason = reason
        # Ask the page to collapse through the same message channel it already
        # listens on (ExecuteScript is not available on this binding).
        self._to_page({"type": "collapse"})
        log(f"collapse requested (state={self.island_state})")

    def _tick_collapse(self) -> None:
        if not self._hide_at:
            return
        if self.island_state in ("idle", "off") and time.monotonic() >= self._hide_at:
            reason, self._hide_at = self._hide_reason, 0.0
            self.sticky = False
            self.hide_island(reason)

    def _auto_hide_check(self) -> None:
        if not self.visible or self.sticky:
            return
        # A pending gate, an open card, or a reply on screen must never be
        # tucked away on a timer: those are dismissed deliberately (Esc, a
        # click outside, or the hotkey).
        if self.island_state in ("alert", "gate", "prompt", "work"):
            self.gate_visible_until = max(self.gate_visible_until,
                                          time.monotonic() + self.SHOW_GRACE_S)
            return
        if time.monotonic() >= self.gate_visible_until:
            self.hide_island(f"idle after {self.SHOW_GRACE_S:.0f}s")

    def _tick_hotkey(self) -> None:
        """Ctrl+Alt+Space arrives as WM_HOTKEY (see _handle_message); this only
        logs keyboard traffic so input problems are visible in the log."""
        if self._key_events and not self._logged_keys:
            self._logged_keys = True
            log(f"window received WM_KEYDOWN (count={self._key_events}, "
                f"WM_CHAR={self._char_events})")

    # ---- focus: only a deliberate click on the island may take focus ---------
    def _focus_webview(self) -> None:
        """The owner clicked the island: activate the overlay so keystrokes
        reach the widget (a prompt/chat field may now need text)."""
        try:
            # WS_EX_NOACTIVATE keeps the overlay from stealing focus on its own
            # (gates, state changes). A real click is consent, so drop it, then
            # hand foreground + keyboard focus to the WebView2 render widget.
            ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
            if ex & WS_EX_NOACTIVATE:
                user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE,
                                         ex & ~WS_EX_NOACTIVATE)
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            child = self._render_widget()
            fg = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            cur_tid = kernel32.GetCurrentThreadId()
            attached = False
            if fg_tid and fg_tid != cur_tid:
                # Attach so SetFocus is honoured while another thread owns
                # the foreground (otherwise Windows ignores it).
                attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
            raised = user32.SetForegroundWindow(self.hwnd)
            active = user32.SetActiveWindow(self.hwnd)
            focused = user32.SetFocus(child) if child else 0
            # This is what actually makes WebView2 hand keystrokes to the
            # page: a raw SetFocus on the Chromium child leaves the renderer
            # without focus and every keydown is dropped (the page logs a
            # window-blur and never gets the keys).
            try:
                from Microsoft.Web.WebView2.Core import CoreWebView2MoveFocusReason
                if self.controller is not None:
                    self.controller.MoveFocus(CoreWebView2MoveFocusReason.Programmatic)
            except Exception as exc:
                log(f"MoveFocus: {exc!r}")
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            self.activated = True
            # MoveFocus parks DOM focus on the container; put it in the field.
            self._page_focus_input()
            log(f"activate: child={child} fg={fg} fg_tid={fg_tid} attached={attached} "
                f"SetForeground={bool(raised)} SetActive={bool(active)} focus={bool(focused)} "
                f"fg_now={user32.GetForegroundWindow()}")
        except Exception as exc:
            log(f"activate failed: {exc!r}")

    def _render_widget(self):
        """The deepest WebView2 window (where keyboard input is delivered)."""
        try:
            c0 = user32.FindWindowExW(self.hwnd, None, "Chrome_WidgetWin_0", None)
            c1 = user32.FindWindowExW(c0, None, "Chrome_WidgetWin_1", None) if c0 else None
            deep = user32.FindWindowExW(c1, None, "Chrome_RenderWidgetHostHWND", None) if c1 else None
            target = deep or c1 or c0
            return target or self.hwnd
        except Exception:
            return self.hwnd

    # ---- window procedure ----------------------------------------------------
    # A real WndProc (subclassed in) so a click on the island takes the NORMAL
    # Windows path: WM_MOUSEACTIVATE decides activation, the click that opened
    # the card is the click that focuses it, and keystrokes need no trickery.
    # Polling from a timer loses that race: a human click arrives after real
    # mouse movement, and focus/activation were landing a frame too late.
    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)

    def _install_wndproc(self) -> None:
        proc_type = self.WNDPROC

        def _wndproc(hwnd, msg, wparam, lparam):
            try:
                return self._handle_message(hwnd, msg, wparam, lparam)
            except Exception as exc:
                log(f"wndproc error on {msg:#x}: {exc!r}")
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        cb = proc_type(_wndproc)
        self._wndproc_cb = cb  # keep alive: a GC'd callback would crash
        self._old_wndproc = user32.SetWindowLongPtrW(self.hwnd, GWL_WNDPROC,
                                                     ctypes.cast(cb, ctypes.c_void_p).value)
        # Ctrl+Alt+Space toggles the island from anywhere. Registered (not
        # polled) so it cannot fire mid-typing or depend on the cursor.
        if not user32.RegisterHotKey(self.hwnd, HOTKEY_ID,
                                     MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, VK_SPACE):
            log("hotkey registration failed (another app owns Ctrl+Alt+Space)")

    def _handle_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEACTIVATE:
            if self._cursor_over_island():
                # The island was clicked: it is allowed to activate, so the
                # click that opens the card is the click that focuses it.
                self._allow_activate()
                return MA_ACTIVATE
            return MA_NOACTIVATE

        if msg == WM_ACTIVATE:
            if wparam in (1, 2):  # WA_ACTIVE / WA_CLICKACTIVE
                self._on_activated()
            return 0

        if msg == WM_ACTIVATEAPP:
            if wparam == WA_INACTIVE:
                # Focus went elsewhere -> become un-activatable again so a
                # later gate can never steal it back.
                self._restore_noactivate()
            return 0

        if msg == WM_HOTKEY and wparam == HOTKEY_ID:
            log(f"WM_HOTKEY (visible={self.visible}, state={self.island_state})")
            self._toggle_island("hotkey")
            return 0

        if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
            self._key_events += 1
            self._last_win_key = int(wparam)
            # The toggle chord must never reach the page: with the chat card
            # open it would type "ctrl alt space" into the prompt (and could
            # submit it).
            if self._chord_pending():
                return 0
        elif msg == WM_CHAR:
            self._char_events += 1

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _chord_pending(self) -> bool:
        """True while Ctrl+Alt+Space is held (its keys are eaten)."""
        return (bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
                and bool(user32.GetAsyncKeyState(VK_MENU) & 0x8000)
                and bool(user32.GetAsyncKeyState(VK_SPACE) & 0x8000))

    def _cursor_over_island(self) -> bool:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        rect = self._island_screen_rect() if self.visible else None
        return bool(rect) and (rect[0] <= pt.x < rect[0] + rect[2]
                               and rect[1] <= pt.y < rect[1] + rect[3])

    def _set_noactivate_style(self, on: bool) -> None:
        try:
            ex = user32.GetWindowLongPtrW(self.hwnd, GWL_EXSTYLE)
            want = (ex | WS_EX_NOACTIVATE) if on else (ex & ~WS_EX_NOACTIVATE)
            if want != ex:
                user32.SetWindowLongPtrW(self.hwnd, GWL_EXSTYLE, want)
                user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                    | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        except Exception:
            pass

    def _allow_activate(self) -> None:
        self._set_noactivate_style(False)

    def _on_activated(self) -> None:
        """The island is now the foreground window (by the owner's own click)."""
        try:
            child = self._render_widget()
            if child:
                user32.SetFocus(child)
            try:
                from Microsoft.Web.WebView2.Core import CoreWebView2MoveFocusReason
                if self.controller is not None:
                    self.controller.MoveFocus(CoreWebView2MoveFocusReason.Programmatic)
            except Exception as exc:
                log(f"MoveFocus: {exc!r}")
            self.activated = True
            # MoveFocus parks DOM focus on the container; let the page put it
            # in its own field (it knows when the card is actually open).
            self._to_page({"type": "focus-input"})
        except Exception as exc:
            log(f"activate failed: {exc!r}")

    def _restore_noactivate(self) -> None:
        if not self.activated:
            return
        self.activated = False
        self._set_noactivate_style(True)
        log("restored WS_EX_NOACTIVATE (focus released)")

    def _tick_focus_guard(self) -> None:
        """Belt-and-braces for a focus change that produced no WM_ACTIVATEAPP
        (some window managers only send it to the thread that lost focus)."""
        if not self.activated:
            return
        fg = user32.GetForegroundWindow()
        if fg and fg != self.hwnd:
            self._restore_noactivate()

    # ---- polling loop ---------------------------------------------------------
    def tick(self, _sender, _args) -> None:
        self.tick_count += 1
        try:
            self._tick_capture()
            self._tick_focus_guard()
            self._tick_hotkey()
            # Drive auto-hide from the timer, not from rect messages: the page
            # stops sending those once the island settles, so an idle island
            # would never tuck itself away.
            self._auto_hide_check()
            self._tick_collapse()
            if self._leaving and time.monotonic() >= self._leave_started + self.LEAVE_SLIDE_S:
                self._finish_hide()
            if self.exit_requested:
                from System.Windows.Forms import Application
                Application.Exit()
                return
            if self.tick_count % HEALTH_EVERY == 0:
                self._tick_display()
            self._tick_nav_retry()
        except Exception as exc:
            log(f"tick error: {exc}")

    def _tick_capture(self) -> None:
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        rect = self._island_screen_rect() if self.visible else None
        inside = bool(rect) and (rect[0] <= pt.x < rect[0] + rect[2]
                                 and rect[1] <= pt.y < rect[1] + rect[3])

        # Be activatable WHILE the pointer is over the island, so the click
        # that opens the card is the click that activates and focuses it.
        # Hover alone never activates anything; WS_EX_NOACTIVATE goes straight
        # back when the pointer leaves. Doing this on the click instead (in
        # WM_MOUSEACTIVATE) is a click too late: Windows has already decided.
        if self.activated:
            pass
        elif inside:
            self._allow_activate()
        else:
            self._set_noactivate_style(True)

        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        pressed = down and not self.lmb_was_down
        self.lmb_was_down = down
        if pressed:
            if not inside and self.island_state in ("gate", "prompt") and self.core is not None:
                # Click landed outside an open card: same as Esc.
                try:
                    self.core.PostWebMessageAsJson(json.dumps({"type": "outside-click"}))
                except Exception:
                    pass

    def _probe_health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{BASE}/api/health", timeout=1.5) as resp:
                return b"accuretta" in resp.read(2048)
        except Exception:
            return False

    def _start_health_thread(self) -> None:
        """Bridge liveness is checked OFF the UI thread: a blocking HTTP call
        on the WebView2 thread stalls the message pump, and a stalled pump is
        what makes the widget feel dead to the keyboard."""
        def _loop() -> None:
            while not self._stop.is_set():
                if self._probe_health():
                    self.health_misses = 0
                    self.seen_alive = True
                elif self.seen_alive:
                    # Only an ALREADY-CONTACTED bridge going silent means
                    # "shut down"; at boot the socket can be bound-but-not-
                    # serving for minutes while a model loads.
                    self.health_misses += 1
                    if self.health_misses >= HEALTH_MAX_MISS:
                        log("bridge unreachable; exiting")
                        self.exit_requested = True
                        return
                self._stop.wait(2.0)

        threading.Thread(target=_loop, daemon=True).start()

    def _tick_display(self) -> None:
        cx = user32.GetSystemMetrics(SM_CXSCREEN)
        if cx != self.last_screen_cx:
            self.dpi = _window_dpi(self.hwnd)
            self.reposition()

    def _tick_nav_retry(self) -> None:
        if self.nav_retry_due and time.monotonic() >= self.nav_retry_due and self.core is not None:
            self.nav_retry_due = 0.0
            try:
                self.core.Navigate(PAGE_URL)
            except Exception as exc:
                log(f"nav retry failed: {exc}")

    # ---- WebView2 bootstrap ---------------------------------------------------
    def start_webview(self) -> None:
        from System import Action, IntPtr
        from System.Drawing import Color, Rectangle
        from System.Threading.Tasks import Task, TaskScheduler
        from System.Windows.Forms import Control  # installs the WF sync context
        from Microsoft.Web.WebView2.Core import (
            CoreWebView2Controller, CoreWebView2Environment)

        Control().Dispose()  # force WindowsFormsSynchronizationContext onto this thread
        scheduler = TaskScheduler.FromCurrentSynchronizationContext()
        USER_DATA.mkdir(parents=True, exist_ok=True)

        def on_env(task):
            if task.IsFaulted:
                log(f"environment failed: {task.Exception}")
                return
            env = task.Result
            ctl_task = env.CreateCoreWebView2ControllerAsync(IntPtr(self.hwnd))
            ctl_task.ContinueWith(
                Action[Task[CoreWebView2Controller]](on_controller), scheduler)

        def on_controller(task):
            if task.IsFaulted:
                log(f"controller failed: {task.Exception}")
                return
            self.controller = task.Result
            self.controller.DefaultBackgroundColor = Color.FromArgb(0, 0, 0, 0)
            self.controller.Bounds = Rectangle(0, 0, self.win_w, self.win_h)
            self.controller.IsVisible = True
            core = self.controller.CoreWebView2
            self.core = core
            s = core.Settings
            s.IsStatusBarEnabled = False
            s.AreDevToolsEnabled = False
            s.AreDefaultContextMenusEnabled = False
            s.IsZoomControlEnabled = False
            s.AreBrowserAcceleratorKeysEnabled = False
            core.WebMessageReceived += self._on_web_message
            core.NavigationCompleted += self._on_nav_completed
            core.Navigate(PAGE_URL)
            log("webview ready; navigating")

        env_task = CoreWebView2Environment.CreateAsync(None, str(USER_DATA), None)
        env_task.ContinueWith(
            Action[Task[CoreWebView2Environment]](on_env), scheduler)

    def _on_web_message(self, _sender, args) -> None:
        try:
            msg = json.loads(args.WebMessageAsJson)
        except Exception:
            return
        kind = msg.get("type")
        if kind == "island-rect":
            new_state = str(msg.get("state") or "idle")
            if self.island is None:
                log(f"first island rect: {msg.get('w')}x{msg.get('h')} ({new_state})")
                # The page is alive: show once so a freshly enabled overlay is
                # visibly working, then the idle timer tucks it away again.
                self._reveal_once()
            elif new_state != self.island_state:
                log(f"island state: {self.island_state} -> {new_state} "
                    f"({msg.get('w')}x{msg.get('h')})")
            self.island = (float(msg.get("x", 0)), float(msg.get("y", 0)),
                           float(msg.get("w", 0)), float(msg.get("h", 0)))
            self.island_state = new_state
            self._apply_region()
            self._auto_hide_check()
        elif kind == "notify":
            # Something worth surfacing popped up (gate, model, error): reveal
            # the island even if the owner had it tucked away.
            self.show_island(reason=str(msg.get("kind") or "notify"))
            log(f"notify: {msg.get('kind')}")
        elif kind == "open-url":
            # "Open in main app" from the notch. Restricted to the bridge's
            # own origin: the page must never become a shell-open primitive
            # for arbitrary URLs. The DESKTOP window is raised when it exists
            # — the bridge broadcast from the page points it at the session.
            # Only when no desktop window is running (headless bridge,
            # browser-only mode) does this fall back to the default browser.
            url = str(msg.get("url") or "")
            if url.startswith(BASE + "/"):
                if not _raise_main_window():
                    try:
                        os.startfile(url)      # no desktop window: browser
                        log(f"open in main app (browser fallback): {url}")
                    except Exception as exc:
                        log(f"open-url failed: {exc}")
            else:
                log(f"open-url refused (not the bridge origin): {url[:120]}")
        elif kind == "diag":
            log("diag " + json.dumps({k: v for k, v in msg.items() if k != "type"}))

    def _on_nav_completed(self, _sender, args) -> None:
        if getattr(args, "IsSuccess", False):
            self.nav_retry_due = 0.0
            log("page loaded")
            return
        # Keep retrying until the bridge starts serving (model load can block
        # it for minutes at boot). The health poll handles real shutdowns.
        self.nav_retry_due = time.monotonic() + NAV_RETRY_S
        log("navigation failed; retrying")


def _single_instance() -> bool:
    kernel32.CreateMutexW(None, False, "AccurettaNotchHost")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main() -> int:
    if sys.platform != "win32":
        print("notch host is Windows-only", file=sys.stderr)
        return 1
    if not _single_instance():
        return 0
    log(f"starting (port {PORT})")

    # Load the CLR and reference assemblies BEFORE importing their namespaces.
    import clr
    clr.AddReference("System.Windows.Forms")
    clr.AddReference("System.Drawing")
    clr.AddReference(str(_webview2_lib_dir() / "Microsoft.Web.WebView2.Core.dll"))
    from System.Threading import ApartmentState, Thread, ThreadStart

    def ui_thread() -> None:
        # WebView2 hard-requires an STA thread; the pythonnet main thread is
        # already claimed as MTA by the CLR loader, so the whole UI lives on a
        # dedicated .NET thread whose apartment we set before Start().
        try:
            _make_dpi_aware()
            host = NotchHost()
            host.create_window()
            from System.Windows.Forms import Application, Timer
            host.start_webview()
            host._start_health_thread()
            timer = Timer()
            timer.Interval = POLL_MS
            timer.Tick += host.tick
            timer.Start()
            Application.Run()
            log("stopped")
        except Exception as exc:
            log(f"ui thread crashed: {exc!r}")

    ui = Thread(ThreadStart(ui_thread))
    ui.SetApartmentState(ApartmentState.STA)
    ui.Start()
    ui.Join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
