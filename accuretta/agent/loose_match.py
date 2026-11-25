# SPDX-License-Identifier: AGPL-3.0-only
"""Finding the lines an edit means when the model's copy differs in indentation or blank
lines.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _indent_of(line: str) -> str:
    return line[:len(line) - len(line.lstrip(" \t"))]


def _indent_width(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip(" "))


def _level_map(widths) -> dict:
    """Indent width -> nesting level, from the widths a block uses."""
    return {w: i for i, w in enumerate(sorted(set(widths)))}


def _loose_block_key(lines: list[str], levels: dict | None = None) -> list[tuple]:
    """Lines compared for a whitespace-tolerant match: each line as (nesting
    level, text without surrounding spaces), so 2- vs 4-space indents, tabs vs
    spaces, a different base depth and trailing spaces all compare equal while
    the block's structure must still agree."""
    if levels is None:
        levels = _level_map(_indent_width(ln) for ln in lines if ln.strip())
    return [(levels.get(_indent_width(ln), -1), ln.strip()) if ln.strip() else (None, "")
            for ln in lines]


def _trim_blank_edges(lines: list[str]) -> list[str]:
    lines = list(lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _loose_line_match(text: str, old: str, new: str) -> tuple[str | None, str]:
    """Apply one edit whose old text matches the file except for whitespace:
    indentation depth or width, tabs vs spaces, trailing spaces. Matches whole
    lines only, and only when exactly one block matches. The new text is
    written in the file's own indentation (levels mapped to the file's widths,
    its tab or space style), so a block quoted at the wrong depth or width
    still lands where it belongs. Returns (new file text, "") or (None, reason)."""
    old_lines = _trim_blank_edges(old.split("\n"))
    if not old_lines:
        return None, "empty"
    new_lines = _trim_blank_edges(new.split("\n"))
    # One level map over the model's old and new text, so a level the new text
    # adds still ranks against the old one.
    model_levels = _level_map(_indent_width(ln) for ln in old_lines + new_lines if ln.strip())
    want = _loose_block_key(old_lines, model_levels)
    old_ranks = sorted({lvl for lvl, txt in want if txt})
    want = [(old_ranks.index(lvl) if txt else None, txt) for lvl, txt in want]
    file_lines = text.split("\n")
    n = len(old_lines)
    hits = [i for i in range(len(file_lines) - n + 1)
            if file_lines[i].strip() == old_lines[0].strip()
            and _loose_block_key(file_lines[i:i + n]) == want]
    if len(hits) != 1:
        return None, ("not found" if not hits else f"appears {len(hits)} times")
    start = hits[0]
    block = file_lines[start:start + n]
    # The file's leading whitespace for each nesting level of the matched block.
    file_ws: dict[int, str] = {}
    for (lvl, txt), ln in zip(want, block):
        if txt and lvl not in file_ws:
            file_ws[lvl] = _indent_of(ln)
    use_tabs = any("\t" in ws for ws in file_ws.values())
    widths = sorted({len(ws.expandtabs(4)) for ws in file_ws.values()})
    unit = "\t" if use_tabs else " " * ((widths[1] - widths[0]) if len(widths) > 1 else 4)
    out = []
    for ln in new_lines:
        if not ln.strip():
            out.append("")
            continue
        model_rank = model_levels[_indent_width(ln)]
        # Rank relative to the old block's levels; below or above it, extend
        # from the nearest level the file shows.
        rel = sum(1 for r in old_ranks if r < model_rank) if model_rank not in old_ranks \
            else old_ranks.index(model_rank)
        if model_rank not in old_ranks and model_rank > max(old_ranks):
            rel = len(old_ranks) - 1 + (model_rank - max(old_ranks))
        top = max(file_ws)
        lead = file_ws[rel] if rel in file_ws else file_ws[top] + unit * max(0, rel - top)
        out.append(lead + ln.strip())
    return "\n".join(file_lines[:start] + out + file_lines[start + n:]), ""
