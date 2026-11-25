# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's memory tools: remember, forget, edit and search long-term memories.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.events import broadcast_event
from accuretta.core.memories import _memory_store


def _load_memories() -> list[dict]:
    return _memory_store.read()


def _save_memories(memories: list[dict]) -> None:
    _memory_store.save(memories)


def tool_remember(args: dict) -> dict:
    result = _memory_store.put(args)
    if result.get("saved"):
        broadcast_event({"type": "memories:update"})
    return result


def tool_forget(args: dict) -> dict:
    if not args.get("id"):
        return {"error": "Memory ID is required."}
    result = _memory_store.forget(args["id"])
    broadcast_event({"type": "memories:update"})
    return result


def tool_edit_memory(args: dict) -> dict:
    result = _memory_store.put(args, edit=True)
    if result.get("saved"):
        broadcast_event({"type": "memories:update"})
    return result


def tool_search_memories(args: dict) -> dict:
    return _memory_store.search(args.get("query", ""), args.get("kind", "all"),
                                args.get("limit", 5), args.get("offset", 0))
