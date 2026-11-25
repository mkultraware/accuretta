# SPDX-License-Identifier: AGPL-3.0-only
"""The context meter: prompt tokens per chat and the breakdown the UI shows.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.token_budget import _count_msg_tokens


def _ctx_prompt_tokens(messages, tools_overhead=0, token_scale=1.0) -> int:
    """Single source of truth for the context gauge's prompt size.

    The pre-round ctx_fill lead and the round-end stats correction MUST both
    use this, or the gauge bounces between the two scales whenever
    token_scale != 1.0 (char heuristic vs /tokenize-derived scale).
    """
    try:
        scale = float(token_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if not (scale > 0):
        scale = 1.0
    return int(sum(_count_msg_tokens(m) for m in messages) * scale) + int(tools_overhead or 0)


def _ctx_breakdown(messages, tools_overhead=0, token_scale=1.0) -> dict:
    """Per-category slice of the context gauge's prompt size, for the ring's
    hover breakdown (system / tool specs / user / assistant / tool results).

    Same scale as _ctx_prompt_tokens, so the parts sum to (within rounding
    of) its total. Plain ints only — must stay JSON-serializable for SSE.
    """
    try:
        scale = float(token_scale)
    except (TypeError, ValueError):
        scale = 1.0
    if not (scale > 0):
        scale = 1.0
    parts = {"system": 0.0, "user": 0.0, "assistant": 0.0, "tool": 0.0}
    try:
        for m in messages or []:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "")
            if role not in parts:
                role = "assistant"  # developer/function/unknown lands model-side
            parts[role] += _count_msg_tokens(m)
    except Exception:
        pass
    out = {k: int(v * scale) for k, v in parts.items()}
    out["tools"] = int(tools_overhead or 0)
    return out
