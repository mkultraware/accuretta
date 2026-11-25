# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's long-term memories: the store, and picking the ones that fit a prompt.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.paths import DATA
from memory_store import MemoryStore


MEMORIES_FILE = DATA / "memories.jsonl"


_memory_store = MemoryStore(MEMORIES_FILE)


def _select_memories_for_prompt() -> list[dict]:
    return _memory_store.profile()["preferences"]
