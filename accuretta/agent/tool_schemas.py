# SPDX-License-Identifier: AGPL-3.0-only
"""The tool list as llama-server receives it: each tool's parameters turned into a
schema its grammar can handle.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.system_prompt import _active_tools
from accuretta.agent.tool_sets import _visible_tool_names
from accuretta.core.chats import _get_current_chat


def tools_for_llama(include_exploit: bool = True, chat_id: str | None = None) -> list[dict]:
    """Tool spec for llama-server's OpenAI-compatible /v1/chat/completions.
    Same shape as OpenAI function calling. include_exploit is threaded straight
    to _active_tools for red-team phase gating.

    LAZY BUNDLES: only the core set + this chat's unlocked bundles are shipped
    on the wire (see _CORE_TOOL_NAMES / _TOOL_BUNDLES / list_more_tools). This
    cuts the inlined schema from ~100 tools to ~40 for a normal turn — most of
    that 6-10K token overhead was dead weight the model never touched."""
    chat_id = chat_id or _get_current_chat()
    visible = _visible_tool_names(include_exploit, chat_id)
    out = []
    for name, t in _active_tools(include_exploit, chat_id).items():
        if name not in visible:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t["description"],
                "parameters": _wire_tool_parameters(t["parameters"]),
            },
        })
    return out


# Top-level schema combinators break llama.cpp's tool-call parser: with
# `anyOf: [{required:[content]}, {required:[source]}]` on write_file, its
# grammar can't match any <parameter=...> block and every call arrives as
# `arguments: {}` (reproduced on b11480 with a Qwen3.x 27B), which the model
# then imitates for other tools. Keep the combinators in TOOLS
# (_missing_required_tool_args enforces them bridge-side) but never ship them
# on the wire.
_WIRE_SCHEMA_COMBINATORS = ("anyOf", "oneOf", "allOf", "not")


def _wire_tool_parameters(params: dict) -> dict:
    if not isinstance(params, dict) or not any(k in params for k in _WIRE_SCHEMA_COMBINATORS):
        return params
    return {k: v for k, v in params.items() if k not in _WIRE_SCHEMA_COMBINATORS}
