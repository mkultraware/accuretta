# SPDX-License-Identifier: AGPL-3.0-only
"""Local-only error log. No prompts, commands, paths beyond basenames, or tool output."""

import json
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path


MAX_ENTRIES = 200
DEDUP_WINDOW_S = 60.0
MESSAGE_LIMIT = 500
TRACE_LIMIT = 4000
KINDS = ("bridge", "interface", "backend")


def _home() -> str:
    try:
        return str(Path.home())
    except Exception:
        return ""


def scrub(text: str) -> str:
    text = str(text or "")
    home = _home()
    if home and len(home) > 3:
        text = text.replace(home, "~")
    return text


def location_of(tb) -> str:
    try:
        frames = traceback.extract_tb(tb)
    except Exception:
        return "unknown"
    root = str(Path(__file__).parent.resolve())
    for frame in reversed(frames):
        path = str(frame.filename or "")
        if path.startswith(root):
            return f"{Path(path).name}:{frame.lineno}"
    if frames:
        last = frames[-1]
        name = str(last.filename or "")
        if "site-packages" in name or "dist-packages" in name:
            return f"dependency:{Path(name).name}:{last.lineno}"
        return f"stdlib:{Path(name).name}:{last.lineno}"
    return "unknown"


class ErrorStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return []
            entries = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    entries.append(entry)
            return entries

    def save(self, entries):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    for entry in entries[-MAX_ENTRIES:]:
                        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def record(self, kind, exc_type="", message="", location="unknown", trace=""):
        if kind not in KINDS:
            kind = "bridge"
        now = int(time.time())
        entry = {
            "id": uuid.uuid4().hex[:12],
            "t": now,
            "kind": kind,
            "type": str(exc_type or "Error")[:80],
            "message": scrub(message)[:MESSAGE_LIMIT],
            "location": str(location or "unknown")[:120],
            "trace": scrub(trace)[:TRACE_LIMIT],
            "count": 1,
        }
        with self.lock:
            entries = self.read()
            for existing in reversed(entries):
                if (existing.get("kind") == kind and existing.get("type") == entry["type"]
                        and existing.get("location") == entry["location"]
                        and now - int(existing.get("t") or 0) <= DEDUP_WINDOW_S):
                    existing["count"] = int(existing.get("count") or 1) + 1
                    existing["t"] = now
                    self.save(entries)
                    return {"deduped": True, "id": existing["id"]}
            entries.append(entry)
            self.save(entries)
            return {"deduped": False, "id": entry["id"]}

    def record_exception(self, kind, exc: BaseException):
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        tb = exc.__traceback__ if isinstance(exc, BaseException) else None
        return self.record(kind, type(exc).__name__, str(exc), location_of(tb), trace)

    def clear(self):
        with self.lock:
            self.save([])
            return {"cleared": True}

    def summary(self):
        entries = self.read()
        counts = {kind: 0 for kind in KINDS}
        for entry in entries:
            if entry.get("kind") in counts:
                counts[entry["kind"]] += int(entry.get("count") or 1)
        recent = []
        for entry in sorted(entries, key=lambda e: int(e.get("t") or 0), reverse=True)[:8]:
            recent.append({k: entry.get(k) for k in ("id", "t", "kind", "type", "message", "location", "count")})
        return {"total": sum(counts.values()), "counts": counts, "recent": recent}

    def report(self, entry_id: str) -> dict:
        for entry in self.read():
            if entry.get("id") == entry_id:
                day = time.strftime("%Y-%m-%d", time.localtime(int(entry.get("t") or 0)))
                lines = [
                    f"accuretta error report ({day})",
                    f"type: {entry.get('type')} (x{int(entry.get('count') or 1)})",
                    f"area: {entry.get('kind')}",
                    f"location: {entry.get('location')}",
                ]
                if entry.get("message"):
                    lines.append(f"detail: {entry['message']}")
                return {"report": "\n".join(lines)}
        return {"error": "Entry not found."}
