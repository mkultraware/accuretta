# SPDX-License-Identifier: AGPL-3.0-only
"""What every desktop action passes first: the panic switch, the per-chat off switch,
waking the screen saver, the rate limit, the approval gate, and remembering which window
the agent is working in.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ctypes
import os
import threading
import time
from accuretta.core.chats import _current_chat_id
from accuretta.core.optional_imports import Image
from accuretta.core.optional_imports import _HAVE_PIL
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import _HAVE_UIA
from accuretta.core.optional_imports import pyautogui
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from accuretta.security.approvals import request_approval
from accuretta.security.desktop_intent import _desktop_target_lock
from accuretta.security.desktop_intent import _is_accuretta_window
from accuretta.tools.desktop_procs import _screen_saver_pids
from accuretta.tools.desktop_windows import _capture_target_window
from accuretta.tools.screen_view import _SCREEN_MAX_DIM
from typing import Any


# Kill switch: when set, every desktop action tool refuses immediately.
# The frontend panic button and the user deny-action both flip this via
# `/api/desktop/panic`. Cleared by /api/desktop/resume or a new chat turn.
_desktop_panic = threading.Event()


# Rate limiter for desktop actions — defense in depth. Hard cap regardless
# of what the agent requests. Tunable in settings.
_desktop_action_times: list[float] = []


_desktop_action_lock = threading.Lock()


# per-chat ephemeral desktop kill switch.  lives in memory only — restarting
# the bridge resets every chat to its global setting.
_chat_desktop_disabled: set[str] = set()


def _desktop_preflight(required: tuple[str, ...] = ()) -> dict | None:
    """Return an error when desktop control is disabled or a dependency is absent."""
    s = get_settings()
    if not s.get("desktop_enabled"):
        return {"error": "desktop automation is disabled. Enable it in Settings -> Desktop automation."}
    if _desktop_panic.is_set():
        return {"error": "desktop automation is paused (panic/kill switch active). User must resume in Settings."}
    cid = _current_chat_id.get()
    if cid and cid in _chat_desktop_disabled:
        return {"error": "desktop automation is off for this chat. User must re-enable it on the session header."}
    available = {
        "pyautogui": _HAVE_PYAUTOGUI,
        "pillow": _HAVE_PIL,
        "pygetwindow": _HAVE_PGW,
        "uiautomation": _HAVE_UIA,
    }
    missing = [name for name in required if not available.get(name, False)]
    if missing:
        return {
            "error": f"missing dependencies: {', '.join(missing)}. Install with: pip install {' '.join(missing)}"
        }
    _wake_screen_saver()
    return None


def _wake_screen_saver() -> bool:
    """Dismiss a running screen saver before a desktop tool looks or acts.

    An unattended run on a PC with a short screen-saver delay saw only the
    saver ("abstract neon light trails") between its slow vision steps and
    spent ten minutes lost. A one-pixel nudge ends it, as a person moving the
    mouse would. Returns True when one was running."""
    if os.name != "nt" or not _screen_saver_pids():
        return False
    # A real one-pixel mouse move ends most savers and wakes a sleeping display,
    # as a person moving the mouse would. It goes through mouse_event: Windows
    # doesn't count pyautogui's cursor jumps (SetCursorPos) as input, which is
    # why savers seemed to ignore them. Each step stands alone: once the saver
    # has run a while, the cursor belongs to the saver's own desktop and a
    # nudge can fail, and that must not skip closing it.
    try:
        user32 = ctypes.windll.user32
        user32.mouse_event(0x0001, 1, 0, 0, 0)      # MOUSEEVENTF_MOVE, relative
        user32.mouse_event(0x0001, -1, 0, 0, 0)
    except Exception:
        pass
    try:
        # Ribbons on Windows 11 ignored both synthetic mouse moves and keys (as
        # did every click the run made), so then ask it to close, and end it if
        # it won't. That ends only the saver, as waking the screen does: a lock
        # screen still applies.
        time.sleep(0.5)
        pids = _screen_saver_pids()
        if pids:
            user32 = ctypes.windll.user32
            proc_cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _close(hwnd, _lparam):
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
                if pid.value in pids:
                    user32.PostMessageW(ctypes.c_void_p(hwnd), 0x0010, 0, 0)     # WM_CLOSE
                return True
            user32.EnumWindows(proc_cb(_close), 0)
            time.sleep(0.8)
            for pid in _screen_saver_pids():
                handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
                if handle:
                    ctypes.windll.kernel32.TerminateProcess(handle, 0)
                    ctypes.windll.kernel32.CloseHandle(handle)
        time.sleep(0.6)                           # let the desktop repaint
        return True
    except Exception:
        return False


def _screen_saver_running() -> bool:
    return bool(_screen_saver_pids())


def _desktop_rate_check() -> dict | None:
    """Sliding-window rate limit. Returns error dict if over limit."""
    s = get_settings()
    cap = int(s.get("desktop_max_actions_per_minute") or 30)
    now = time.time()
    with _desktop_action_lock:
        _desktop_action_times[:] = [t for t in _desktop_action_times if now - t < 60.0]
        if len(_desktop_action_times) >= cap:
            oldest = _desktop_action_times[0]
            wait = 60.0 - (now - oldest)
            return {"error": f"rate limit: {cap} actions/min exceeded. Retry in {wait:.0f}s."}
        _desktop_action_times.append(now)
    return None


def _grab_view(args: dict) -> tuple[Any, dict]:
    """Capture what a screen tool should look at. Returns (PIL image at most
    _SCREEN_MAX_DIM on the long edge, frame) where frame says where the image
    sits on screen: {"x", "y", "scale", "width", "height", "window"}."""
    from PIL import ImageGrab
    frame: dict[str, Any] = {"window": ""}
    if all(k in args for k in ("x", "y", "w", "h")):
        box = (int(args["x"]), int(args["y"]), int(args["x"]) + int(args["w"]),
               int(args["y"]) + int(args["h"]))
    else:
        w = _capture_target_window(str(args.get("window") or "").strip())
        if str(args.get("window") or "").strip() and w is None:
            raise RuntimeError(f"no visible window matches '{args.get('window')}'")
        if w is not None:
            try:
                if getattr(w, "isMinimized", False):
                    w.restore()
                if not getattr(w, "isActive", False):
                    w.activate()
                    time.sleep(0.25)   # let it paint in front before the grab
                _remember_desktop_target(w)
            except Exception:
                pass
            box = (int(w.left), int(w.top), int(w.left) + int(w.width), int(w.top) + int(w.height))
            frame["window"] = str(getattr(w, "title", ""))[:200]
        else:
            box = None
    img = ImageGrab.grab(bbox=box, all_screens=True) if box else pyautogui.screenshot()
    x0, y0 = (box[0], box[1]) if box else (0, 0)
    ow, oh = img.size
    scale = 1.0
    if max(ow, oh) > _SCREEN_MAX_DIM:
        scale = _SCREEN_MAX_DIM / max(ow, oh)
        img = img.resize((max(1, int(ow * scale)), max(1, int(oh * scale))), Image.LANCZOS)
    frame.update({"x": x0, "y": y0, "scale": round(scale, 4),
                  "width": img.size[0], "height": img.size[1]})
    return img, frame


def _gate_action(title: str, command: str, details: dict | None = None,
                 required: tuple[str, ...] = ()) -> dict | None:
    """Common gate for every action tool: panic -> rate -> approval.
    Returns an error dict if the caller should abort, else None."""
    err = _desktop_preflight(required)
    if err:
        return err
    rerr = _desktop_rate_check()
    if rerr:
        return rerr
    approval = request_approval(title=title, command=command, details=details or {})
    if approval.get("decision") != "approve":
        return {"error": f"user denied action ({approval.get('status')})"}
    if _desktop_panic.is_set():
        return {"error": "panic/kill switch was flipped after approval — action aborted."}
    return None


def _remember_desktop_target(w) -> None:
    with _desktop_target_lock:
        STATE.desktop_target_window = w


def _capture_desktop_target() -> None:
    """Snapshot the foreground window before an approval card can steal focus."""
    if not _HAVE_PGW:
        return
    try:
        w = _pgw.getActiveWindow()
    except Exception:
        w = None
    if w and not _is_accuretta_window(getattr(w, "title", "")):
        _remember_desktop_target(w)


def _reassert_desktop_target() -> None:
    """Bring the captured target window back to the foreground right before
    input synthesis, undoing any focus the approval card pulled to Accuretta."""
    if not _HAVE_PGW:
        return
    with _desktop_target_lock:
        w = STATE.desktop_target_window
    if not w:
        return
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
        time.sleep(0.06)  # let the window manager settle before typing/clicking
    except Exception:
        pass
