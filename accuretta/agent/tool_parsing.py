# SPDX-License-Identifier: AGPL-3.0-only
"""Find tool calls written as JSON in plain text, for models without native tool calls.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _find_balanced_json_objects(text: str) -> list[tuple[int, int]]:
    """Scan `text` for balanced top-level JSON objects. Returns (start, end)
    byte positions of each top-level `{...}` span. String contents (inside
    "...") are respected so braces in string values don't shift the depth
    counter. Used by extract_tool_calls's bare-JSON fallback for models
    that emit raw {"name":...,"arguments":...} without a <tool_call> wrap."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        closed_at = -1
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        closed_at = j
                        break
            j += 1
        if closed_at >= 0:
            spans.append((i, closed_at + 1))
            i = closed_at + 1
        else:
            i += 1
    return spans
