# SPDX-License-Identifier: AGPL-3.0-only
"""The undo store: snapshots of files before the agent changes them, per turn.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.paths import DATA
from accuretta.core.workspace import normalize_path
from task_state import UndoStore
from accuretta.core.chats import get_chats
from accuretta.core.events import broadcast_event
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json


# ---- per-turn undo journal -------------------------------------------------
# Snapshot a file's prior state right before a mutating tool writes it, so a
# whole turn's file changes can be reverted from the UI in one click. Turns run
# serially, so one in-memory journal plus one on-disk copy per turn is enough.
# Restore writes prior content back or deletes files the turn created;
# directories and binary/unreadable files are not offered for undo.
_UNDO_DIR = DATA / "undo"


_undo_store = UndoStore(_UNDO_DIR)


def _undo_snapshot(path: str) -> None:
    _undo_store.snapshot(normalize_path(path))


def _undo_begin_turn(chat_id: str) -> None:
    _undo_store.begin(chat_id)


def _undo_commit_turn() -> dict | None:
    return _undo_store.commit()


def _undo_restore(turn_id: str, *, chat_id: str = "", file_index: int | None = None) -> dict:
    result = _undo_store.restore(str(turn_id or "").strip(), chat_id=chat_id, file_index=file_index)
    if result.get("ok"):
        snapshot = get_chats()
        for chat in snapshot.get("chats", {}).values():
            for item in chat.get("undo_turns", []):
                if item.get("turn_id") == turn_id:
                    item["undone"] = result.get("remaining", 0) == 0
                    if file_index is not None and 0 <= file_index < len(item.get("files", [])):
                        item["files"][file_index]["restorable"] = False
                        item["files"][file_index]["undone"] = True
        save_json(CHATS_FILE, snapshot)
    broadcast_event({"type": "workspace:update"})
    return result
