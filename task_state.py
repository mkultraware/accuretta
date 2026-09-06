"""Revision-aware file recovery, independent of the model and integrations."""

from __future__ import annotations

import contextvars
import difflib
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import uuid


def file_revision(path: str | Path) -> str | None:
    try:
        with Path(path).open("rb") as stream:
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            return digest.hexdigest()
    except OSError:
        return None


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class UndoStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.current = contextvars.ContextVar("accuretta_undo", default=None)
        self.lock = threading.RLock()

    def begin(self, chat_id: str) -> str:
        state = {"turn_id": uuid.uuid4().hex, "chat_id": chat_id,
                 "t": int(time.time()), "entries": {}}
        self.current.set(state)
        return state["turn_id"]

    def _save(self, state: dict) -> None:
        value = {**state, "entries": list(state["entries"].values())}
        _atomic_json(self.directory / f"{state['turn_id']}.json", value)

    def snapshot(self, path: str) -> None:
        state = self.current.get()
        if state is None:
            return
        path = str(Path(path).resolve())
        with self.lock:
            if path in state["entries"] or Path(path).is_dir():
                return
            exists = Path(path).is_file()
            try:
                prior = Path(path).read_bytes().decode("utf-8") if exists else None
            except (OSError, UnicodeError):
                return
            state["entries"][path] = {"path": path, "existed": exists,
                                       "prior": prior, "checkpointed": False}
            self._save(state)

    def checkpoint(self, path: str) -> None:
        state = self.current.get()
        if state is None:
            return
        path = str(Path(path).resolve())
        with self.lock:
            entry = state["entries"].get(path)
            if entry is not None:
                entry.update(after_hash=file_revision(path), after_exists=Path(path).exists(), checkpointed=True)
                self._save(state)

    def commit(self) -> dict | None:
        state = self.current.get()
        if state is None:
            return None
        with self.lock:
            files = []
            for path, entry in state["entries"].items():
                if not entry.get("checkpointed"):
                    continue
                try:
                    current = Path(path).read_bytes().decode("utf-8") if Path(path).is_file() else ""
                except (OSError, UnicodeError):
                    current = ""
                prior = entry["prior"] or ""
                if current == prior and entry["existed"] == entry["after_exists"]:
                    continue
                lines = list(difflib.unified_diff(prior.splitlines(), current.splitlines(), n=0))
                files.append({"path": path, "name": Path(path).name,
                              "added": sum(line.startswith("+") and not line.startswith("+++") for line in lines),
                              "deleted": sum(line.startswith("-") and not line.startswith("---") for line in lines),
                              "created": not entry["existed"], "removed": not entry["after_exists"]})
            self._save(state)
            self.current.set(None)
            if not files:
                return None
            return {"turn_id": state["turn_id"], "files": files,
                    "added": sum(f["added"] for f in files),
                    "deleted": sum(f["deleted"] for f in files)}

    def restore(self, turn_id: str) -> dict:
        if not turn_id or any(c not in "0123456789abcdef" for c in turn_id) or len(turn_id) != 32:
            return {"error": "Invalid undo identifier"}
        journal = self.directory / f"{turn_id}.json"
        with self.lock:
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"error": "Undo record is unavailable"}
            retained, errors, restored = [], [], 0
            for entry in payload.get("entries", []):
                path = Path(entry["path"])
                try:
                    if (not entry.get("checkpointed")
                            or (entry.get("after_exists") and not entry.get("after_hash"))
                            or path.exists() != entry.get("after_exists")
                            or file_revision(path) != entry.get("after_hash")):
                        raise ValueError("Changed since the agent edit, or no verified checkpoint. Current file preserved.")
                    if entry["existed"]:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.undo")
                        try:
                            temporary.write_bytes(entry["prior"].encode("utf-8"))
                            temporary.replace(path)
                        finally:
                            temporary.unlink(missing_ok=True)
                    elif path.exists():
                        path.unlink()
                    restored += 1
                except (OSError, ValueError) as exc:
                    retained.append(entry)
                    errors.append(f"{path.name}: {exc}")
            if retained:
                _atomic_json(journal, {**payload, "entries": retained})
            else:
                journal.unlink(missing_ok=True)
            return {"ok": not retained, "restored": restored, "errors": errors,
                    "error": "; ".join(errors) if errors else None,
                    "remaining": len(retained)}
