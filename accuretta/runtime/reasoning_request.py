# SPDX-License-Identifier: AGPL-3.0-only
"""Turning the composer's effort choice into what a model's chat template accepts: the
nearest native level, or a thinking budget.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.effort import _EFFORT_RANK
from accuretta.core.effort import _effort_tier
from accuretta.core.effort import _normalize_effort_choice


def _nearest_native_level(choice: str, levels) -> str | None:
    """The model's level closest to a choice; a tie goes to the deeper one
    (asking for "high" on a low/medium/xhigh model gets xhigh)."""
    choice = _normalize_effort_choice(choice)
    levels = [l for l in (levels or []) if l in _EFFORT_RANK]
    if choice == "auto" or not levels:
        return None
    rank = _EFFORT_RANK[choice]
    return min(levels, key=lambda l: (abs(_EFFORT_RANK[l] - rank), -_EFFORT_RANK[l]))


def _reasoning_budget_for_effort(level: str, ctx_limit: int) -> int:
    """Translate friendly effort levels into a context-aware reasoning cap."""
    reserve = max(min(int(max(1024, ctx_limit) * 0.25), 16000), 1024)
    available = max(256, reserve - 1024)
    if level == "low":
        return max(256, min(1024, int(available * 0.16)))
    if level == "high":
        return max(1024, min(8192, int(available * 0.72)))
    return max(512, min(4096, int(available * 0.40)))


def _resolve_reasoning_request(effort, capability: dict, settings: dict,
                               ctx_limit: int) -> dict:
    """Resolve one composer choice into template kwargs + harness limits."""
    choice = _normalize_effort_choice(effort)
    level = _effort_tier(choice)
    if level != "auto" and not capability.get("supported"):
        choice = level = "auto"
    enabled = settings.get("enable_thinking") is not False
    try:
        legacy_budget = int(settings.get("thinking_budget") if settings.get("thinking_budget") is not None else 2048)
    except (TypeError, ValueError):
        legacy_budget = 2048
    native_effort = None
    if level != "auto":
        enabled = True
        effort_headroom = _reasoning_budget_for_effort(level, ctx_limit)
        if capability.get("mode") == "native_effort":
            native_levels = capability.get("native_levels") or []
            if choice in native_levels:
                # The model's own level name (e.g. Qwen's "xhigh") goes
                # through untouched.
                native_effort = choice
            else:
                native_effort = _nearest_native_level(choice, native_levels) \
                    or (capability.get("native_effort_map") or {}).get(level, level)
            # Native-effort models decide how long Low/Medium/Deep should reason.
            # Keep enough response room for that thinking, but do not also send
            # a numeric target or trip the numeric runaway ceiling against it.
            budget = -1
            response_headroom = effort_headroom
        else:
            budget = effort_headroom
            response_headroom = max(0, budget)
    else:
        budget = legacy_budget
        response_headroom = max(0, budget)
    if not enabled:
        budget = 0
        response_headroom = 0
    return {
        "effort": level, "choice": choice, "enabled": bool(enabled), "budget": int(budget),
        "native_effort": native_effort,
        "response_headroom": int(response_headroom),
    }
