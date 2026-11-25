# SPDX-License-Identifier: AGPL-3.0-only
"""A quick look at the desktop right after an action: the active window and the cursor, without asking a model.

Moved out of accuretta/agent/tool_dispatch.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import time
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import pyautogui
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from typing import Any


def _desktop_post_state() -> dict:
    """Cheap immediate observation after an action; never invokes a model."""
    state: dict[str, Any] = {"observed_at": int(time.time())}
    if _HAVE_PGW:
        try:
            w = _pgw.getActiveWindow()
            if w:
                state["active_window"] = str(getattr(w, "title", ""))[:240]
                state["window_box"] = [int(w.left), int(w.top), int(w.width), int(w.height)]
        except Exception:
            pass
    if _HAVE_PYAUTOGUI:
        try:
            pos = pyautogui.position()
            state["cursor"] = [int(pos.x), int(pos.y)]
        except Exception:
            pass
    state["note"] = "immediate host observation; use desktop_snapshot for semantic verification"
    return state
