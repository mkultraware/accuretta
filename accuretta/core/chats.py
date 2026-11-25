# SPDX-License-Identifier: AGPL-3.0-only
"""Reading chats.json, and which chat (and tool call) the code running right now
belongs to.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import contextvars
import threading
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import _CHATS_TX_LOCK
from accuretta.core.persistence import _ChatSnapshot
from accuretta.core.persistence import _normalize_chats_data
from accuretta.core.persistence import load_json


_current_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar("_current_chat_id", default="")


_current_rt_chat: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_current_rt_chat", default=None)


_current_tool_name: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_tool_name", default="")


_current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_tool_call_id", default="")


def get_chats() -> dict:
    with _CHATS_TX_LOCK:
        c = _normalize_chats_data(load_json(CHATS_FILE, {"chats": {}, "order": []}))
        return _ChatSnapshot(c)


# Per-turn context: chat_id and similar state reachable deep inside tool
# implementations without threading it through every signature (e.g.
# _run_powershell tags cmd_history.jsonl entries with their chat).
#
# Both a threading.local (main-thread paths) and a contextvar: tools in the
# executor run via `contextvars.copy_context().run(invoke_tool, ...)`, and only
# a contextvar survives that hop.
_chat_ctx = threading.local()


_chat_cv: "contextvars.ContextVar[str]" = contextvars.ContextVar("accuretta_chat_id", default="")


def _get_current_chat() -> str:
    v = _chat_cv.get()
    if v:
        return v
    return getattr(_chat_ctx, "chat_id", "") or ""
