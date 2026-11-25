# SPDX-License-Identifier: AGPL-3.0-only
"""Shorten long tool output for the model and say what was cut.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
from typing import Any


def _elide_text(s: str, cap: int) -> str:
    """Head/tail line-aware elision. Keep ~half the budget on each end with
    a one-line summary marker between them. cap is in characters (consistent
    with the rest of the tool-result pipeline). Returns s unchanged when
    s already fits."""
    if not isinstance(s, str) or len(s) <= cap:
        return s
    lines = s.split("\n")
    # Single-blob with no newlines (e.g. a giant base64 PNG): char-level
    # slice is the only sane fallback.
    if len(lines) <= 4:
        half = max(1, (cap - 80) // 2)
        return s[:half] + f"\n… [{len(s) - cap} chars elided] …\n" + s[-half:]
    # Line-aware elision: greedily fill head and tail keeping them roughly
    # balanced by char count. Tokens scale ~linearly with chars for typical
    # text, so this also balances by token cost.
    target_each = max(256, (cap - 80) // 2)
    head_lines: list[str] = []
    tail_lines: list[str] = []
    head_chars = 0
    tail_chars = 0
    i = 0
    j = len(lines) - 1
    while i <= j and (head_chars < target_each or tail_chars < target_each):
        if head_chars <= tail_chars:
            head_lines.append(lines[i])
            head_chars += len(lines[i]) + 1
            i += 1
        else:
            tail_lines.append(lines[j])
            tail_chars += len(lines[j]) + 1
            j -= 1
        if i > j:
            break
    omitted = j - i + 1
    if omitted <= 0:
        return s  # ate the whole thing, no elision needed
    omitted_chars = sum(len(lines[k]) + 1 for k in range(i, j + 1))
    marker = f"… [{omitted} of {len(lines)} lines elided · {omitted_chars} chars] …"
    return "\n".join(head_lines) + "\n" + marker + "\n" + "\n".join(reversed(tail_lines))


def _truncation_envelope(result: Any, cap: int, original_chars: int) -> str:
    """Last-resort fallback. Returns a tiny, *valid* JSON object describing
    that the result was too large even after compression. Never produces
    malformed JSON — the model treats this as a structured "I tried, it
    didn't fit" message instead of walking off a cliff into truncated bytes."""
    keys = list(result.keys()) if isinstance(result, dict) else None
    return json.dumps({
        "_truncated": True,
        **({"research_source_id": result["research_source_id"],
            "research_recovery": "Read stored source text using research_notebook(source_id=research_source_id, offset=0); follow next_offset."}
           if isinstance(result, dict) and result.get("research_source_id") else {}),
        **({"evidence_receipt": {"id": result["evidence_receipt"]["id"]},
            "evidence_recovery": "Read the recorded result with security_coverage(evidence_id=..., offset=0); do not repeat network requests to recover output."}
           if isinstance(result, dict) and isinstance(result.get("evidence_receipt"), dict) and result["evidence_receipt"].get("id") else {}),
        "_note": (
            f"tool result exceeded {cap} chars even after head/tail elision "
            f"(original ~{original_chars} chars). "
            f"Re-run the tool with a tighter scope or a smaller range."
        ),
        "keys_present": keys,
    }, ensure_ascii=False)
