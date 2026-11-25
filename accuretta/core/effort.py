# SPDX-License-Identifier: AGPL-3.0-only
"""Reasoning-effort names: the composer's choices and the low/medium/high tier
each maps to.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


_REASONING_LEVELS = ("auto", "low", "medium", "high")


# Effort names templates use, weakest first. The composer shows a model's own
# names (Qwen3.8: low/medium/xhigh; gpt-oss: low/medium/high); internally each
# maps to one of the low/medium/high tiers for response room and verification.
_EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}


_EFFORT_CHOICES = frozenset(_EFFORT_RANK) | {"auto"}


def _normalize_effort_choice(value) -> str:
    """A composer choice: "auto" or any known native effort name."""
    value = str(value or "auto").strip().lower()
    aliases = {"med": "medium", "default": "auto", "normal": "medium", "x-high": "xhigh",
               "extra-high": "xhigh", "extra_high": "xhigh", "deep": "high"}
    value = aliases.get(value, value)
    return value if value in _EFFORT_CHOICES else "auto"


def _effort_tier(value) -> str:
    """auto / low / medium / high for any choice (xhigh and max count as high)."""
    choice = _normalize_effort_choice(value)
    if choice == "auto":
        return "auto"
    rank = _EFFORT_RANK[choice]
    return "low" if rank <= _EFFORT_RANK["low"] else "medium" if rank == _EFFORT_RANK["medium"] else "high"


def _normalize_reasoning_effort(value) -> str:
    """The tier of a composer choice (kept for callers that think in tiers)."""
    return _effort_tier(value)
