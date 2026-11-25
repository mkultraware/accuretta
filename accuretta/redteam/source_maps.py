# SPDX-License-Identifier: AGPL-3.0-only
"""Rebuild original frontend sources from published JavaScript source maps.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json


def _reconstruct_source_maps(source_map_sources: list, max_bytes: int) -> dict:
    """Recover original sources from .map `sourcesContent` already downloaded.
    Returns the full recovered file tree plus inline content up to max_bytes."""
    recovered: list[dict] = []
    contents: list[dict] = []
    seen_paths: set = set()
    map_count = 0
    total = 0
    truncated = False
    for map_url, body in source_map_sources:
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        map_count += 1
        srcs = data.get("sources") or []
        cont = data.get("sourcesContent") or []
        root = str(data.get("sourceRoot") or "")
        for i, raw_path in enumerate(srcs):
            path = (root + str(raw_path)) if root else str(raw_path)
            key = (map_url, path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            c = cont[i] if i < len(cont) and isinstance(cont[i], str) else None
            nbytes = len(c.encode("utf-8", "replace")) if c else 0
            recovered.append({"path": path[:300], "bytes": nbytes, "map": map_url})
            if c:
                if total < max_bytes:
                    take = c[: max(0, max_bytes - total)]
                    if len(take) < len(c):
                        truncated = True
                    contents.append({"path": path[:300], "content": take})
                    total += len(take.encode("utf-8", "replace"))
                else:
                    truncated = True
    return {
        "map_count": map_count,
        "recovered_file_count": len(recovered),
        "files": recovered[:1000],
        "contents": contents,
        "content_truncated": truncated,
    }
