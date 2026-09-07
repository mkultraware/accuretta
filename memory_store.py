"""Local preferences and searchable notes, with no automatic eviction."""

import json
import os
from pathlib import Path
import re
import threading
import time
import uuid


PREFERENCE_BUDGET = 6000
TEXT_LIMIT = 2000


def memory_kind(entry):
    return entry.get("kind", "fact")


class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            if not self.path.exists():
                return []
            entries = []
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("A saved memory is damaged. No memories were changed.")
                    entries.append(entry)
            return entries

    def save(self, entries):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8") as stream:
                    for entry in entries:
                        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(self.path)
            finally:
                temporary.unlink(missing_ok=True)

    def profile(self, entries=None):
        entries = self.read() if entries is None else entries
        selected = [entry for entry in entries if memory_kind(entry) == "preference"]
        used = sum(len(str(entry.get("text", ""))) for entry in selected)
        return {"preferences": selected, "characters": used, "budget": PREFERENCE_BUDGET,
                "legacy_count": sum("kind" not in entry for entry in entries),
                "warning": ("Your preference profile is full. Shorten a preference or move facts to searchable memory. "
                            "Existing preferences remain active." if used >= PREFERENCE_BUDGET else "")}

    def put(self, args, *, edit=False):
        with self.lock:
            entries = self.read()
            previous = next((entry for entry in entries if entry.get("id") == args.get("id")), None) if edit else None
            if edit and previous is None:
                return {"error": "Memory not found. Search saved memories for its ID."}
            text = args.get("text", previous.get("text", "") if previous else "")
            kind = args.get("kind", memory_kind(previous) if previous else "fact")
            if kind not in ("preference", "fact"):
                return {"error": "Choose preference or fact."}
            if not isinstance(text, str) or not text.strip():
                return {"error": "Memory text is required."}
            text = text.strip()
            if len(text) > TEXT_LIMIT:
                return {"error": f"Keep each memory within {TEXT_LIMIT} characters. Nothing was shortened or saved."}
            tags = args.get("tags", previous.get("tags", []) if previous else [])
            tags = [tags] if isinstance(tags, str) else tags
            if not isinstance(tags, list):
                return {"error": "Tags must be a list."}
            tags = [str(tag).strip().lower()[:24] for tag in tags if str(tag).strip()][:5]
            if not edit:
                duplicate = next((entry for entry in entries if entry.get("text") == text
                                  and memory_kind(entry) == kind), None)
                if duplicate:
                    return {"saved": False, "reason": "duplicate", "id": duplicate["id"], "kind": kind}
            entry = {**(previous or {}), "id": previous["id"] if previous else uuid.uuid4().hex[:12],
                     "text": text, "kind": kind, "tags": tags, "updated": int(time.time())}
            entry.setdefault("created", int(time.time()))
            candidate = [entry if item is previous else item for item in entries] if edit else entries + [entry]
            old_size = self.profile(entries)["characters"]
            new_size = self.profile(candidate)["characters"]
            if new_size > PREFERENCE_BUDGET and new_size > old_size:
                return {"error": "Preference profile is full. Shorten an existing preference or move a fact "
                                 "to searchable memory. No existing preference was removed.",
                        "profile_full": True, "characters": old_size, "budget": PREFERENCE_BUDGET}
            self.save(candidate)
            return {"saved": True, "id": entry["id"], "kind": kind, "total": len(candidate)}

    def forget(self, memory_id):
        with self.lock:
            entries = self.read()
            remaining = [entry for entry in entries if entry.get("id") != memory_id]
            self.save(remaining)
            return {"removed": len(entries) - len(remaining), "total": len(remaining)}

    def search(self, query="", kind="all", limit=5, offset=0):
        if kind not in ("all", "preference", "fact"):
            return {"error": "Choose all, preference, or fact."}
        query = str(query).strip().casefold()
        words = set(re.findall(r"\w+", query))
        matches = []
        for entry in self.read():
            if kind != "all" and memory_kind(entry) != kind:
                continue
            text = str(entry.get("text", "")) + " " + " ".join(map(str, entry.get("tags", [])))
            tokens = set(re.findall(r"\w+", text.casefold()))
            score = len(words & tokens) + (3 if query and query in text.casefold() else 0)
            if query and not score:
                continue
            matches.append((score, entry))
        matches.sort(key=lambda pair: (pair[0], pair[1].get("updated", pair[1].get("created", 0))), reverse=True)
        limit, offset = max(1, min(10, int(limit))), max(0, int(offset))
        return {"memories": [{**entry, "kind": memory_kind(entry)} for _, entry in matches[offset:offset + limit]],
                "total": len(matches), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(matches) else None,
                "search_method": "Local word and phrase matching; try different words if nothing matches."}
