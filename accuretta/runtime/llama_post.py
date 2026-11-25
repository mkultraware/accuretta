# SPDX-License-Identifier: AGPL-3.0-only
"""Plain requests to llama-server for side calls (summaries, image descriptions,
security notes), with their tokens added to the savings counter.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.endpoints import LLAMA
from accuretta.core.savings import _savings_add


def _savings_note_completion(data) -> None:
    """Count a non-streamed llama-server completion (compaction summary,
    image description, sub-agent step, ...) into the lifetime counter.
    llama-server's usage.prompt_tokens is the whole prompt, cached prefix
    included; timings are the fallback for builds that omit usage."""
    try:
        if not isinstance(data, dict):
            return
        usage = data.get("usage") or {}
        timings = data.get("timings") or {}
        tok_in = int(usage.get("prompt_tokens") or 0)
        if not tok_in and timings:
            tok_in = int(timings.get("cache_n") or 0) + int(timings.get("prompt_n") or 0)
        tok_out = int(usage.get("completion_tokens") or timings.get("predicted_n") or 0)
        if tok_in or tok_out:
            _savings_add(tok_in, tok_out, count_turn=False)
    except Exception:
        pass


def llama_post(path: str, payload: dict, base: str | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base or LLAMA}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode("utf-8"))
    if path.endswith("/completions") or path.endswith("/completion"):
        # Every non-streamed generation (compaction, image descriptions,
        # security summaries, ...) counts toward the lifetime token counter.
        _savings_note_completion(out)
    return out
