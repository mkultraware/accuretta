# SPDX-License-Identifier: AGPL-3.0-only
"""Per-chat live event emitters, so tools can stream progress to the chat that called
them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.chats import _current_chat_id


# per-chat SSE emitter so tools can stream progress without plumbing emit through every call
_chat_emitters: dict[str, callable] = {}


def _emit_chat_event(evt: dict) -> None:
    """Emit an SSE event on the active chat's live stream, if one is running."""
    try:
        cid = _current_chat_id.get()
        emit = _chat_emitters.get(cid) if cid else None
        if emit:
            emit(evt)
    except Exception:
        pass
