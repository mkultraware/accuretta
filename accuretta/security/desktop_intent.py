# SPDX-License-Identifier: AGPL-3.0-only
"""Desktop control and the user's intent: whether the user's own words asked for desktop
input, the unlocked desktop session, and whether the target window is Accuretta's own or
one the agent opened.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ctypes
import os
import re
import threading
from accuretta.core.state import STATE
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from accuretta.security.session_allow import _approval_session_allow
from accuretta.security.stop_concerns import _pid_stop_reason


# Desktop input kinds: the repetitive click/type/keys/scroll/focus actions that
# drive a browser or app. In soft mode they run without a prompt when (A) the
# user's own last message asked for on-screen interaction, or (B) the user
# flipped the per-session desktop switch. Program launch and window close are
# deliberately excluded. Web-tainted desktop actions never reach auto-approve
# (force_prompt is set upstream), so a hostile page cannot drive the mouse.
_DESKTOP_INPUT_KINDS = {
    "desktop.click", "desktop.type", "desktop.keys", "desktop.scroll",
    "desktop.set_value", "desktop.invoke", "desktop.focus",
    "desktop.drag", "desktop.open_url",
}


# One token in the session-allow set unlocks every desktop input kind at once, so
# "Allow desktop input for this session" is a single switch, not per-action.
_DESKTOP_SESSION_SENTINEL = "desktop.input.*"


def _desktop_session_unlocked() -> bool:
    return _DESKTOP_SESSION_SENTINEL in _approval_session_allow


_DESKTOP_INTENT_RE = re.compile(
    r"\b(open|launch|start|focus|close|click|type|press|select|toggle|scroll|"
    r"control|operate|interact|use)\b[^\n]{0,80}\b(app|application|window|"
    r"desktop|screen|computer|notepad|calculator|explorer|browser|excel|word)\b|"
    r"\b(app|application|window|desktop|screen|computer|notepad|calculator|"
    r"explorer|browser|excel|word)\b[^\n]{0,80}\b(open|launch|start|focus|"
    r"close|click|type|press|select|toggle|scroll|control|operate|interact|use)\b|"
    r"(?:^|[.!?]\s+)(?:please\s+)?(?:open|launch|start|close)\s+\S+",
    re.IGNORECASE | re.MULTILINE,
)


def _user_asked_desktop_interaction(text: str) -> bool:
    return bool(_DESKTOP_INTENT_RE.search(str(text or "")))


def _desktop_target_owned() -> bool:
    """The window desktop input is aimed at belongs to a program Accuretta's
    tools started, or one from the workspace (testing the app it just built)."""
    if os.name != "nt":
        return False
    try:
        with _desktop_target_lock:
            w = STATE.desktop_target_window
        hwnd = getattr(w, "_hWnd", None) if w else None
        if not hwnd and _HAVE_PGW:
            active = _pgw.getActiveWindow()
            if active and not _is_accuretta_window(getattr(active, "title", "")):
                hwnd = getattr(active, "_hWnd", None)
        if not hwnd:
            return False
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return bool(pid.value) and pid.value != os.getpid() and _pid_stop_reason(pid.value) is None
    except Exception:
        return False


# ---- desktop input focus re-assert -----------------------------------------
# Synthetic input (pyautogui click/type/keys) lands in the foreground window,
# but clicking Approve briefly brings Accuretta to the foreground, so the input
# would land on Accuretta itself. Snapshot the intended target before the card
# shows and re-assert it right before the input fires. The UIA tools
# (invoke/set_value/scroll) drive an element handle directly and don't need it.
_desktop_target_lock = threading.Lock()


def _is_accuretta_window(title: str) -> bool:
    """True for Accuretta's own main window / notch / browser tab (title starts
    with 'Accuretta'), so we never re-aim synthetic input back onto ourselves."""
    return str(title or "").strip().lower().startswith("accuretta")
