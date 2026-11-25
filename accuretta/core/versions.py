# SPDX-License-Identifier: AGPL-3.0-only
"""Saved file versions and snapshots: where they live, saving, reading and listing them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import time
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json


VERSIONS_DIR = DATA / "versions"


SNAPSHOTS_DIR = DATA / "snapshots"


def save_version(chat_id: str, html: str, label: str = "") -> dict:
    folder = VERSIONS_DIR / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    idx = len([f for f in folder.iterdir() if f.suffix == ".html"]) + 1
    name = f"v{idx:04d}.html"
    (folder / name).write_text(html, encoding="utf-8")
    meta_path = folder / "index.json"
    meta = load_json(meta_path, {"versions": []})
    entry = {"id": name, "n": idx, "t": int(time.time()), "label": label, "bytes": len(html.encode("utf-8"))}
    meta["versions"].append(entry)
    save_json(meta_path, meta)
    return entry


def list_versions(chat_id: str) -> list[dict]:
    folder = VERSIONS_DIR / chat_id
    meta = load_json(folder / "index.json", {"versions": []})
    return meta.get("versions", [])


def read_version(chat_id: str, vid: str) -> str | None:
    p = VERSIONS_DIR / chat_id / vid
    if not p.exists() or p.suffix != ".html":
        return None
    return p.read_text(encoding="utf-8")
