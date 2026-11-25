# SPDX-License-Identifier: AGPL-3.0-only
"""Rough token counts without a tokenizer: characters per token, and a message's estimated size.

Moved out of accuretta/agent/token_budget.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import json


# ---- token counting (approximation) ----------------------------------------
# Fast heuristic instead of a bundled tokenizer. Roughly 4 chars/token for
# English, 3 for code, 1.5 for CJK; the blended 3.0 over-counts on purpose so
# the context window is never overflowed.
CHARS_PER_TOKEN = 3.0


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    # byte-length penalises non-ASCII (CJK, emoji) which are multi-byte
    return max(len(text), len(text.encode("utf-8"))) // CHARS_PER_TOKEN


def _count_msg_tokens(msg: dict) -> int:
    content = msg.get("content") or ""
    # Vision content is a list of {type:"text"} / {type:"image_url"} parts. Text
    # counts normally; each image is a flat ~600-token estimate (roughly what
    # llama.cpp's mmproj expands a 336x336 patch grid into).
    if isinstance(content, list):
        text_total = 0
        image_count = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_total += _approx_tokens(part.get("text") or "")
            elif part.get("type") == "image_url":
                image_count += 1
        text_tokens = text_total + image_count * 600
    else:
        text_tokens = _approx_tokens(content)
    # Reasoning sent in its own field (templates that read reasoning_content)
    # is still prompt the model sees.
    if isinstance(msg.get("reasoning_content"), str):
        text_tokens += _approx_tokens(msg["reasoning_content"])
    # tool_call JSON is also text the model sees
    tcs = msg.get("tool_calls") or []
    extra = json.dumps(tcs, ensure_ascii=False) if tcs else ""
    return text_tokens + _approx_tokens(extra) + 4  # role overhead
