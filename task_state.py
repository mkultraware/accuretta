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


_REVIEW_TEXT_LIMIT = 2 * 1024 * 1024


def _capture_file(path: str) -> dict:
    file = Path(path)
    if not file.exists():
        return {"exists": False, "hash": None, "text": "", "size": 0, "kind": "text"}
    try:
        size = file.stat().st_size
        if size > _REVIEW_TEXT_LIMIT:
            return {"exists": True, "hash": file_revision(file), "text": None,
                    "size": size, "kind": "large"}
        data = file.read_bytes()
        try:
            content = data.decode("utf-8") if b"\x00" not in data else None
        except UnicodeError:
            content = None
        return {"exists": True, "hash": hashlib.sha256(data).hexdigest(),
                "text": content, "size": len(data), "kind": "text" if content is not None else "binary"}
    except OSError:
        return {"exists": True, "hash": None, "text": None, "size": None, "kind": "unavailable"}


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
            before = _capture_file(path)
            state["entries"][path] = {"path": path, "existed": before["exists"],
                                       "prior": before["text"], "before": before,
                                       "restorable": before["kind"] == "text", "checkpointed": False}
            self._save(state)

    def checkpoint(self, path: str) -> None:
        state = self.current.get()
        if state is None:
            return
        path = str(Path(path).resolve())
        with self.lock:
            entry = state["entries"].get(path)
            if entry is not None:
                after = _capture_file(path)
                entry.update(after_hash=after["hash"], after_exists=after["exists"],
                             after=after, checkpointed=True)
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
                after = entry.get("after") or {}
                before = entry.get("before") or {}
                current = after.get("text")
                prior = entry["prior"] or ""
                if (before.get("hash") == after.get("hash")
                        and entry["existed"] == entry["after_exists"]
                        and after.get("kind") != "unavailable"):
                    continue
                text_diff = current is not None and entry["prior"] is not None
                lines = list(difflib.unified_diff(prior.splitlines(), current.splitlines(), n=0)) if text_diff else []
                files.append({"path": path, "name": Path(path).name,
                              "added": sum(line.startswith("+") for line in lines[2:]),
                              "deleted": sum(line.startswith("-") for line in lines[2:]),
                              "created": not entry["existed"], "removed": not entry["after_exists"],
                              "text_diff": text_diff, "after_hash": entry.get("after_hash"),
                              "restorable": entry.get("restorable", True),
                              "before_size": before.get("size"), "after_size": after.get("size")})
            self._save(state)
            self.current.set(None)
            if not files:
                return None
            review = {"turn_id": state["turn_id"], "chat_id": state["chat_id"],
                      "files": [{**item, "before": state["entries"][item["path"]].get("before"),
                                 "after": state["entries"][item["path"]].get("after")}
                                for item in files]}
            _atomic_json(self.directory / "reviews" / f"{state['turn_id']}.json", review)
            return {"turn_id": state["turn_id"], "chat_id": state["chat_id"], "files": files,
                    "added": sum(f["added"] for f in files),
                    "deleted": sum(f["deleted"] for f in files)}

    def review(self, turn_id: str, chat_id: str, index: int | None = None) -> dict:
        if (not isinstance(turn_id, str) or len(turn_id) != 32
                or any(c not in "0123456789abcdef" for c in turn_id)):
            return {"error": "Invalid review identifier"}
        with self.lock:
            try:
                record = json.loads((self.directory / "reviews" / f"{turn_id}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"error": "Recorded changes are unavailable for this older task."}
            if not chat_id or record.get("chat_id") != chat_id:
                return {"error": "Recorded changes are unavailable for this task."}
            files = record.get("files", [])
            if index is None:
                return {"turn_id": turn_id, "files": [{k: v for k, v in item.items()
                                                         if k not in {"before", "after"}} for item in files]}
            if type(index) is not int or not 0 <= index < len(files):
                return {"error": "Invalid file selection"}
            item = files[index]
            before, after = item.get("before") or {}, item.get("after") or {}
            result = {k: v for k, v in item.items() if k not in {"before", "after"}}
            result["changed_since"] = (Path(item["path"]).exists() != after.get("exists")
                                       or file_revision(item["path"]) != after.get("hash"))
            if before.get("text") is None or after.get("text") is None:
                result["notice"] = "No text diff is available for binary, oversized, or unreadable files."
            else:
                diff = difflib.unified_diff(before["text"].splitlines(), after["text"].splitlines(),
                                           fromfile="Before", tofile="After", n=3)
                lines, size = [], 0
                for line in diff:
                    if len(lines) >= 4000 or size + len(line) > 250000:
                        result["truncated"] = True
                        break
                    lines.append(line)
                    size += len(line)
                result["diff"] = lines
                if not lines:
                    result["notice"] = "Only file existence, line endings, or the final newline changed."
            return result

    def restore(self, turn_id: str, *, chat_id: str = "", file_index: int | None = None) -> dict:
        if not turn_id or any(c not in "0123456789abcdef" for c in turn_id) or len(turn_id) != 32:
            return {"error": "Invalid undo identifier"}
        journal = self.directory / f"{turn_id}.json"
        with self.lock:
            selected_path = None
            if file_index is not None:
                selected = self.review(turn_id, chat_id, file_index)
                if selected.get("error"):
                    return selected
                selected_path = selected["path"]
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"error": "Undo record is unavailable"}
            if selected_path and not any(entry["path"] == selected_path for entry in payload.get("entries", [])):
                return {"error": "This file has already been restored or has no undo snapshot."}
            retained, errors, restored = [], [], 0
            for entry in payload.get("entries", []):
                if selected_path and entry["path"] != selected_path:
                    retained.append(entry)
                    continue
                path = Path(entry["path"])
                if not entry.get("restorable", True):
                    errors.append(f"{path.name}: No restorable text snapshot. File preserved.")
                    continue
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
            return {"ok": not errors if selected_path else not retained, "restored": restored, "errors": errors,
                    "error": "; ".join(errors) if errors else None,
                    "remaining": len(retained)}
