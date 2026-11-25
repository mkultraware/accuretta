# SPDX-License-Identifier: AGPL-3.0-only
"""The user-anchor note: the user's latest request, kept at the end of the context after
a fold so the model doesn't lose what it was asked.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


_USER_ANCHOR_PREFIX = ("[Your request, repeated: the turns around it are condensed in "
                       "the session summary above.]\n")


_USER_ANCHOR_MAX_CHARS = 6000


def _ensure_user_anchor(wire: list[dict], stored: list[dict], replay_from: int) -> list[dict]:
    """Keep at least one real user message on the wire.

    Qwen-family chat templates raise "No user query found in messages" when
    nothing after the system prompt is a user message, which happens once a fold
    swallows the request that started a long one-shot turn (llama-server answers
    HTTP 500). Re-insert the newest folded user request (text only, capped)
    right after the system prompt; the summary already holds it, so this only
    restates the task."""
    if any(m.get("role") == "user" for m in wire[1:]):
        return wire
    text = ""
    for m in reversed(stored[:max(0, replay_from)]):
        if m.get("role") != "user" or m.get("_internal"):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(part.get("text") or "") for part in content
                                if isinstance(part, dict) and part.get("type") == "text")
        text = str(content or "").strip()
        if text:
            break
    if len(text) > _USER_ANCHOR_MAX_CHARS:
        text = text[:_USER_ANCHOR_MAX_CHARS] + "\n[...trimmed; the full request is in the session summary]"
    anchor = {"role": "user", "content": _USER_ANCHOR_PREFIX + (
        text or "Continue the task described in the session summary.")}
    at = 1 if wire and wire[0].get("role") == "system" else 0
    return wire[:at] + [anchor] + wire[at:]
