# SPDX-License-Identifier: AGPL-3.0-only
"""Saving and loading Accuretta's JSON files: atomic writes, corrupt-file
salvage, and the chats.json merge that keeps concurrent chat updates.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import threading
import time
from accuretta.core.paths import CHATS_FILE
from accuretta.core.paths import DATA
from accuretta.core.paths import SETTINGS_FILE
from pathlib import Path
from typing import Any


_FILE_LOCK = threading.RLock()


_CHATS_TX_LOCK = threading.RLock()


class _ChatSnapshot(dict):
    """Revision-aware chats.json snapshot.

    Callers treat ``get_chats()`` as a mutable dict and later call
    ``save_json(CHATS_FILE, chats)``. This subclass keeps that contract while
    remembering the baseline needed to merge independent chat updates at save
    time."""
    def __init__(self, value: dict):
        super().__init__(value)
        self._baseline_chats = copy.deepcopy(value.get("chats", {}))
        self._baseline_order = list(value.get("order", []))


def _chat_payload_digest(value: Any) -> str:
    """Stable digest excluding the internal revision counter."""
    if value is None:
        return "<missing>"
    try:
        payload = copy.deepcopy(value)
        if isinstance(payload, dict):
            payload.pop("_rev", None)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, default=str)
    except Exception:
        raw = repr(value)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _normalize_composer_mode(value: Any) -> str:
    """Collapse the retired Auto composer mode to its real behavior: Agent."""
    mode = str(value or "").strip().lower()
    return mode if mode in {"ide", "research"} else "agent"


def _normalize_chats_data(value: Any) -> dict:
    if not isinstance(value, dict):
        value = {"chats": {}, "order": []}
    value.setdefault("chats", {})
    value.setdefault("order", [])
    if not isinstance(value["chats"], dict):
        value["chats"] = {}
    if not isinstance(value["order"], list):
        value["order"] = []
    # Self-heal: drop order ids with no record and collapse duplicate entries to
    # the first occurrence (a duplicate rendered as two rows that deleted together).
    seen: set = set()
    clean_order = []
    for cid in value["order"]:
        if cid in seen or cid not in value["chats"]:
            continue
        seen.add(cid)
        clean_order.append(cid)
    value["order"] = clean_order
    for cid, record in value["chats"].items():
        if not isinstance(record, dict):
            continue
        if not str(record.get("title") or "").strip():
            record["title"] = "new session"
        record.setdefault("id", cid)
        record.setdefault("messages", [])
        if not isinstance(record.get("messages"), list):
            record["messages"] = []
    for record in value["chats"].values():
        if isinstance(record, dict) and "last_mode" in record:
            record["last_mode"] = _normalize_composer_mode(record.get("last_mode"))
    return value


def _write_json_atomic_unlocked(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    # Keep the last good settings.json beside it (.prev): a component that writes
    # a stripped document would otherwise lose the model folder and context.
    if path == SETTINGS_FILE:
        try:
            prev = path.with_suffix(path.suffix + ".prev")
            if path.is_file() and path.stat().st_size > 0:
                content = path.read_text(encoding="utf-8", errors="replace")
                if content.strip() and content != json.dumps(value, ensure_ascii=False, indent=2):
                    prev.write_text(content, encoding="utf-8")
                else:
                    pass
        except Exception:
            pass
    tmp.replace(path)


def _save_chats_snapshot(snapshot: _ChatSnapshot) -> None:
    """Merge only locally-changed chats into the latest on-disk document.

    This prevents chat A's older snapshot from erasing chat B after concurrent
    turns. Same-chat turns are separately single-flight; if a UI mutation races
    an active turn we retain both message histories when one is a prefix and
    otherwise keep the most recent snapshot while recording the conflict.
    """
    with _CHATS_TX_LOCK, _FILE_LOCK:
        current = _normalize_chats_data(load_json(CHATS_FILE, {"chats": {}, "order": []}))
        merged = copy.deepcopy(current)
        base_chats = getattr(snapshot, "_baseline_chats", {}) or {}
        snap_chats = snapshot.get("chats", {}) if isinstance(snapshot.get("chats"), dict) else {}
        current_chats = current.get("chats", {})
        merged_chats = merged.setdefault("chats", {})
        changed_ids = {
            cid for cid in set(base_chats) | set(snap_chats)
            if _chat_payload_digest(base_chats.get(cid)) != _chat_payload_digest(snap_chats.get(cid))
        }
        conflicts = []
        for cid in changed_ids:
            before = base_chats.get(cid)
            proposed = snap_chats.get(cid)
            live = current_chats.get(cid)
            live_changed = _chat_payload_digest(live) != _chat_payload_digest(before)
            if proposed is None:
                # A delete wins only when the chat did not gain new work after
                # this snapshot was loaded. Otherwise preserve the live chat.
                if not live_changed:
                    merged_chats.pop(cid, None)
                else:
                    conflicts.append({"chat_id": cid, "kind": "delete_vs_update"})
                continue
            chosen = copy.deepcopy(proposed)
            if live_changed and isinstance(live, dict):
                pmsgs = chosen.get("messages") if isinstance(chosen.get("messages"), list) else []
                lmsgs = live.get("messages") if isinstance(live.get("messages"), list) else []
                # Preserve an independently-appended tail where histories share
                # an exact prefix. Same-chat generation is single-flight, so
                # this mostly covers rename/pin/settings-style UI mutations.
                if len(pmsgs) <= len(lmsgs) and pmsgs == lmsgs[:len(pmsgs)]:
                    for key, val in chosen.items():
                        if key != "messages":
                            live[key] = copy.deepcopy(val)
                    chosen = copy.deepcopy(live)
                elif len(lmsgs) <= len(pmsgs) and lmsgs == pmsgs[:len(lmsgs)]:
                    # This snapshot extends the live history (a turn adding its
                    # messages). A field changed live while it was out (a project
                    # picked up mid-turn, a rename) wins wherever this snapshot
                    # left that field as it found it.
                    base = before if isinstance(before, dict) else {}
                    for key in (set(live) | set(base)) - {"messages", "_rev"}:
                        untouched = _chat_payload_digest(chosen.get(key)) == _chat_payload_digest(base.get(key))
                        moved = _chat_payload_digest(live.get(key)) != _chat_payload_digest(base.get(key))
                        if untouched and moved:
                            if key in live:
                                chosen[key] = copy.deepcopy(live[key])
                            else:
                                chosen.pop(key, None)
                else:
                    conflicts.append({"chat_id": cid, "kind": "divergent_update"})
            chosen["_rev"] = max(
                int((live or {}).get("_rev", 0) or 0) if isinstance(live, dict) else 0,
                int((before or {}).get("_rev", 0) or 0) if isinstance(before, dict) else 0,
            ) + 1
            merged_chats[cid] = chosen

        base_order = getattr(snapshot, "_baseline_order", []) or []
        snap_order = snapshot.get("order", []) if isinstance(snapshot.get("order"), list) else []
        if snap_order != base_order:
            deleted = set(base_chats) - set(snap_chats)
            order = []
            for cid in snap_order:
                if cid in merged_chats and cid not in order:
                    order.append(cid)
            order.extend(cid for cid in current.get("order", [])
                         if cid in merged_chats and cid not in order and cid not in deleted)
            order.extend(cid for cid in merged_chats if cid not in order)
            merged["order"] = order
        else:
            merged["order"] = [cid for cid in current.get("order", []) if cid in merged_chats]
            merged["order"].extend(cid for cid in merged_chats if cid not in merged["order"])
        if conflicts:
            conflict_file = DATA / "chat_conflicts.jsonl"
            try:
                with conflict_file.open("a", encoding="utf-8") as fh:
                    for row in conflicts:
                        fh.write(json.dumps({**row, "ts": int(time.time())}) + "\n")
            except Exception:
                pass
        _write_json_atomic_unlocked(CHATS_FILE, merged)
        # A caller may save the same snapshot more than once (pre-turn fold,
        # mission seed, final persist). Advance its baseline after each save so
        # later writes contain only changes made since this successful commit.
        for cid, row in merged_chats.items():
            if cid in snap_chats and isinstance(snap_chats[cid], dict):
                snap_chats[cid]["_rev"] = row.get("_rev", snap_chats[cid].get("_rev", 0))
        snapshot._baseline_chats = copy.deepcopy(snap_chats)
        snapshot._baseline_order = list(snap_order)


def load_json(path: Path, default: Any) -> Any:
    with _FILE_LOCK:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
        # Rename a corrupt file aside (.corrupt-<ts>) instead of silently returning
        # the default, so a half-written chats.json can still be salvaged.
            try:
                ts = time.strftime("%Y%m%d-%H%M%S")
                bak = path.with_name(f"{path.name}.corrupt-{ts}")
                path.rename(bak)
                print(f"[load_json] corrupt: {path} -> {bak.name} ({e!r})", file=sys.stderr)
            except Exception as e2:
                print(f"[load_json] corrupt + rename failed: {path} ({e!r}; rename: {e2!r})", file=sys.stderr)
            return default


def save_json(path: Path, value: Any) -> None:
    if path == CHATS_FILE and isinstance(value, _ChatSnapshot):
        _save_chats_snapshot(value)
        return
    with _FILE_LOCK:
        _write_json_atomic_unlocked(path, value)
