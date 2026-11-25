# SPDX-License-Identifier: AGPL-3.0-only
"""Finding desktop windows by title or selector, and capturing the intended input
target.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.state import STATE
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from accuretta.security.desktop_intent import _desktop_target_lock
from accuretta.security.desktop_intent import _is_accuretta_window
from accuretta.tools.desktop_procs import _process_path
import sys


def _find_window(substring: str):
    """Fuzzy find a single window by title substring (case-insensitive).
    Returns pygetwindow handle or None."""
    if not _HAVE_PGW or not substring:
        return None
    needle = substring.strip().lower()
    try:
        candidates = [w for w in _pgw.getAllWindows() if (w.title or "").lower().find(needle) >= 0]
        candidates = [w for w in candidates if (w.title or "").strip()]
        if not candidates:
            return None
        # prefer active, then largest
        candidates.sort(key=lambda w: (not getattr(w, "isActive", False), -(w.width * w.height)))
        return candidates[0]
    except Exception:
        return None


def _window_by_selector(selector: str):
    """A pygetwindow window by 'hwnd:<hex>' id or title substring, else None."""
    if not _HAVE_PGW or not selector:
        return None
    if selector.lower().startswith("hwnd:"):
        try:
            hwnd = int(selector.split(":", 1)[1], 16)
        except ValueError:
            return None
        for w in _pgw.getAllWindows():
            if int(getattr(w, "_hWnd", 0) or 0) == hwnd:
                return w
        return None
    return _find_window(selector)


def _capture_target_window(selector: str = ""):
    """The window a capture should show: the named one, else the one the agent
    is working in (last focused or opened), else the foreground window unless
    that is Accuretta itself. None means: capture the screen."""
    if not _HAVE_PGW:
        return None
    if selector:
        return _window_by_selector(selector)
    with _desktop_target_lock:
        w = STATE.desktop_target_window
    if w is not None:
        try:
            alive = any(int(getattr(x, "_hWnd", 0) or 0) == int(getattr(w, "_hWnd", -1) or -1)
                        for x in _pgw.getAllWindows())
        except Exception:
            alive = False
        if alive:
            return w
    try:
        w = _pgw.getActiveWindow()
    except Exception:
        w = None
    if w is not None and not _is_accuretta_window(getattr(w, "title", "")):
        return w
    return None


def _list_windows_raw() -> list[dict]:
    """Return visible top-level windows with title + bbox. Requires pygetwindow."""
    if not _HAVE_PGW:
        return []
    out = []
    try:
        for w in _pgw.getAllWindows():
            title = (w.title or "").strip()
            if not title:
                continue
            try:
                if w.width <= 0 or w.height <= 0:
                    continue
                hwnd = int(getattr(w, "_hWnd", 0) or 0)
                pid = _window_process_id(hwnd)
                out.append({
                    "window_id": _window_id(hwnd) if hwnd else "",
                    "title": title,
                    "x": int(w.left), "y": int(w.top),
                    "w": int(w.width), "h": int(w.height),
                    "process_id": pid,
                    "process_path": _process_path(pid),
                    "active": bool(w.isActive),
                    "minimized": bool(getattr(w, "isMinimized", False)),
                })
            except Exception:
                continue
    except Exception:
        return []
    return out


def _window_id(hwnd: int) -> str:
    return f"hwnd:{int(hwnd):x}" if hwnd else ""


def _window_handle_from_id(value: str) -> int:
    raw = str(value or "").strip().lower()
    if not raw.startswith("hwnd:"):
        return 0
    try:
        return int(raw[5:], 16)
    except ValueError:
        return 0


def _window_process_id(hwnd: int) -> int:
    if sys.platform != "win32" or not hwnd:
        return 0
    try:
        import ctypes
        from ctypes import wintypes
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0
