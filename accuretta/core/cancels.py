# SPDX-License-Identifier: AGPL-3.0-only
"""Per-chat cancellation: the Stop flag and the live llama-server response that
Stop closes.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading


# per-chat cancellation. `cancel` flips when user hits Stop (via /api/cancel or
# client disconnect). `resp` is the live urllib response to llama-server, which
# we close explicitly to abort generation server-side — closing the socket is
# the only reliable way to make llama-server stop emitting tokens.
_chat_cancels: dict[str, dict] = {}


_chat_cancels_lock = threading.Lock()


def _register_cancel(chat_id: str) -> threading.Event | None:
    ev = threading.Event()
    with _chat_cancels_lock:
        # One active turn per chat; a second submission would overwrite the
        # cancellation handle and race chats.json / compaction state.
        if chat_id in _chat_cancels:
            return None
        _chat_cancels[chat_id] = {"cancel": ev, "resp": None}
    return ev


def _set_cancel_resp(chat_id: str, resp) -> None:
    with _chat_cancels_lock:
        if chat_id in _chat_cancels:
            _chat_cancels[chat_id]["resp"] = resp


def _unregister_cancel(chat_id: str) -> None:
    with _chat_cancels_lock:
        _chat_cancels.pop(chat_id, None)
