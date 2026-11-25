# SPDX-License-Identifier: AGPL-3.0-only
"""How much disk each kind of app data uses, and clearing one kind on request.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import contextlib
import os
import shutil
import stat
import threading
from accuretta.core.paths import DATA
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from pathlib import Path


_cmd_history_lock = threading.Lock()


# ---- Settings > Storage: what the app keeps on disk ----
# Kinds of app data under DATA with their size, file count and last change.
# Only kinds that are safe to lose can be cleared, and only by these names: a
# clear deletes the listed files and the contents of the listed folders,
# never anything outside DATA, and removes links without following them.
_STORAGE_KINDS = (
    # key, label, entries under DATA ("*" globs files), clearable
    ("chats", "Chats", ("chats.json",), False),
    ("versions", "Preview versions", ("versions",), True),
    ("snapshots", "Undo snapshots", ("snapshots", "undo"), True),
    ("evidence", "Red-team evidence", ("recon_evidence", "redteam"), True),
    ("reports", "Reports and research", ("reports", "research", "renders"), True),
    ("history", "Command history", ("cmd_history.jsonl",), True),
    ("logs", "Logs", ("errors.jsonl", "compaction_debug.log", "notch_host.log", "netsnap-debug.log", "fold_events.jsonl"), True),
    ("backups", "Old settings backups", ("*.bak", "settings.json.pre-*.json"), True),
    ("audit", "Action audit trail", ("action_audit.jsonl",), False),
    ("models", "Model profiles and usage", ("model_runtime_profiles.json", "model_usage_dataset.jsonl", "models_config.json"), False),
    ("security", "Security overview data", ("security_overview.db",), False),
    ("cache", "Window caches", ("webview", "notch-webview2"), False),
    ("journals", "Turn journals", ("turn_journal", "pending"), False),
)


def _storage_entries(patterns) -> list:
    out = []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            out.extend(p for p in DATA.glob(pattern) if p.is_file() and not p.is_symlink())
        else:
            p = DATA / pattern
            if p.exists() or p.is_symlink():
                out.append(p)
    return out


def _storage_measure(paths) -> tuple:
    """(bytes, files, newest mtime) under paths; links are neither followed
    nor counted."""
    size = files = 0
    newest = 0.0
    stack = list(paths)
    while stack:
        p = stack.pop()
        try:
            st = p.lstat()
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            continue
        if stat.S_ISDIR(st.st_mode):
            try:
                stack.extend(Path(e.path) for e in os.scandir(p))
            except OSError:
                pass
            continue
        size += st.st_size
        files += 1
        newest = max(newest, st.st_mtime)
    return size, files, newest


def storage_report() -> dict:
    kinds = []
    total = 0
    for key, label, patterns, clearable in _STORAGE_KINDS:
        size, files, newest = _storage_measure(_storage_entries(patterns))
        kinds.append({"key": key, "label": label, "size": size, "files": files,
                      "modified": int(newest), "clearable": clearable})
        total += size
    # Uploads live in the workspace, not in DATA: shown, never cleared here.
    folders = get_workspace().get("folders") or []
    if folders:
        incoming = Path(normalize_path(folders[0])) / "Incoming"
        size, files, newest = _storage_measure([incoming]) if incoming.is_dir() else (0, 0, 0.0)
        kinds.append({"key": "uploads", "label": "Uploads", "size": size, "files": files,
                      "modified": int(newest), "clearable": False, "path": str(incoming)})
    return {"path": str(DATA), "total": total, "kinds": kinds}


def storage_clear(key: str) -> dict:
    kind = next((k for k in _STORAGE_KINDS if k[0] == key), None)
    if not kind or not kind[3]:
        raise ValueError("That kind of data can't be cleared here.")
    root = DATA.resolve()
    removed = freed = skipped = 0
    # Command history is appended under its own lock; clear it under it too.
    with (_cmd_history_lock if key == "history" else contextlib.nullcontext()):
        for entry in _storage_entries(kind[2]):
            targets = list(entry.iterdir()) if entry.is_dir() and not entry.is_symlink() else [entry]
            for target in targets:
                try:
                    if target.is_symlink():
                        target.unlink()  # the link only, never what it points at
                        removed += 1
                        continue
                    target.resolve().relative_to(root)
                except (OSError, ValueError):
                    skipped += 1
                    continue
                size, files, _ = _storage_measure([target])
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except OSError:
                    skipped += 1  # in use or read-only: left as it was
                    continue
                removed += files or 1
                freed += size
    return {"ok": True, "key": key, "removed": removed, "freed": freed, "skipped": skipped}
