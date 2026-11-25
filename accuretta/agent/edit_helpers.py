# SPDX-License-Identifier: AGPL-3.0-only
"""Edit helpers: line endings for a new file, and the closest region when an edit doesn't match.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path


def _new_file_eol(path: str) -> str:
    """Line ending for a new file: CRLF for batch files, else what a sibling
    with the same extension uses, else LF."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".bat", ".cmd"):
        return "\r\n"
    if ext:
        try:
            for sibling in sorted(Path(path).parent.glob(f"*{ext}"))[:5]:
                if sibling.is_file():
                    sample = sibling.read_bytes()[:65536]
                    if b"\n" in sample:
                        return "\r\n" if sample.count(b"\r\n") * 2 > sample.count(b"\n") else "\n"
        except OSError:
            pass
    return "\n"


def _closest_region(text: str, needle: str, context: int = 2, max_lines: int = 12) -> str:
    """Numbered file lines that best match the first line of a missed old_text,
    so the model can correct the edit without reading the whole file again."""
    lines = text.splitlines()
    want = needle.strip().splitlines() or [needle.strip()]
    first = want[0].strip()
    if not first:
        return ""
    best_i, best_score = -1, 0.0
    for i, line in enumerate(lines):
        score = difflib.SequenceMatcher(None, line.strip(), first).ratio()
        if score > best_score:
            best_i, best_score = i, score
    if best_i < 0 or best_score < 0.6:
        return ""
    lo = max(0, best_i - context)
    hi = min(len(lines), best_i + len(want) + context, lo + max_lines)
    return "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
