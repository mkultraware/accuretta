# SPDX-License-Identifier: AGPL-3.0-only
"""Talking to llama-server over HTTP: health and readiness, GET requests, recommended
settings, and counting tokens with its tokenizer.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.tokens import _count_msg_tokens
from accuretta.core.endpoints import LLAMA


def _conversation_token_scale(msgs: list[dict]) -> float:
    """Factor converting the char-heuristic budget into llama-server's real token
    units: one /tokenize call over the same content _count_msg_tokens counts,
    returning real_tokens / char_estimate. Returns 1.0 for trivial conversations
    or when the server is unreachable, so the trimmer falls back to the heuristic."""
    approx = sum(_count_msg_tokens(m) for m in msgs)
    if approx < 512:
        return 1.0
    parts: list[str] = []
    img_extra = 0
    for m in msgs:
        content = m.get("content", "")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append(part.get("text") or "")
                elif part.get("type") == "image_url":
                    img_extra += 600
        else:
            parts.append(str(content or ""))
        if m.get("tool_calls"):
            parts.append(json.dumps(m["tool_calls"], ensure_ascii=False))
        img_extra += 600 * len(m.get("images") or [])
    joined = "\n".join(parts)
    if not joined.strip():
        return 1.0
    exact = _llama_tokenize(joined)
    if not exact or exact <= 0:
        return 1.0
    real = exact + img_extra
    if real <= 0:
        return 1.0
    return real / approx


def llama_get(path: str, base: str | None = None) -> dict:
    with urllib.request.urlopen(f"{base or LLAMA}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _llama_tokenize(text: str) -> int | None:
    """Ask llama-server to tokenize `text` and return the token count.
    Returns None if the server is unreachable or the response is malformed."""
    try:
        req = urllib.request.Request(
            f"{LLAMA}/tokenize",
            data=json.dumps({"content": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        toks = data.get("tokens") or []
        return len(toks) if isinstance(toks, list) else None
    except Exception:
        return None


def recommended_settings(model: str) -> dict:
    """Return heuristic defaults for the UI. llama-server's context window,
    GPU layers, batch size and thread count are server-launch flags, not
    per-request params — so these numbers are informational only; the user
    tunes them on the llama-server command line."""
    native_ctx = 8192
    try:
        info = llama_get("/v1/models")
        # llama-server exposes one model; no size/quant detail in /v1/models.
        # If the user hit a custom /props endpoint we'd use it, but keep simple.
        _ = info
    except Exception:
        pass
    return {
        "model": model or "local",
        "size_b": 0.0,
        "quant": "",
        "est_weights_gb": 0.0,
        "native_ctx": native_ctx,
        "recommended": {
            "num_ctx": native_ctx,
            "num_gpu": 99,
            "num_batch": 512,
            "num_thread": 0,
    "num_predict": -1,              # -1 = no explicit cap (llama-server may then run to n_ctx).
                                    # `max_output_tokens` below is the harness-enforced ceiling.
    "max_output_tokens": 8192,      # Hard cap on a single generation. num_predict=-1 (unbounded) lets a
                                    # model sail to n_ctx ("re-answer forever" loops); this default ceiling
                                    # bounds one answer, and run_chat_turn further clamps it to the reserved
                                    # headroom so it can never overflow the slot. -1 = truly unbounded.
            "temperature": 0.7,
            "top_p": 0.9,
            "keep_alive": "30m",
        },
    }


def llama_ping(timeout: float = 2.0, base_url: str = None) -> bool:
    """Return whether a llama-server process is answering HTTP requests."""
    if base_url is None:
        base_url = LLAMA
    try:
        with urllib.request.urlopen(f"{base_url}/v1/models", timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False


def llama_ready(timeout: float = 2.0, base_url: str = None) -> bool:
    """Return true only after llama.cpp reports that model loading is done."""
    if base_url is None:
        base_url = LLAMA
    try:
        with urllib.request.urlopen(f"{base_url}/health", timeout=timeout) as response:
            payload = response.read(1024)
            if response.status != 200:
                return False
            if not payload:
                return True
            try:
                status = str(json.loads(payload.decode("utf-8")).get("status") or "").lower()
            except Exception:
                return True
            return status in {"", "ok", "ready"}
    except urllib.error.HTTPError as exc:
        # Older llama.cpp builds did not expose /health. Their model-list
        # endpoint is the best readiness signal available.
        if exc.code == 404:
            return llama_ping(timeout=timeout, base_url=base_url)
        return False
    except Exception:
        return False


def wait_for_llama(wait_seconds: int = 30, base_url: str = None, proc=None) -> bool:
    """Poll llama-server up to wait_seconds. Returns True once it's up, False
    as soon as `proc` (the server just spawned) has exited: a crashed load
    used to hold a model switch for the whole wait (6 minutes for 17 GB)."""
    if base_url is None:
        base_url = LLAMA
    if llama_ready(base_url=base_url):
        return True
    t0 = time.time()
    printed = False
    while time.time() - t0 < wait_seconds:
        time.sleep(0.6)
        if llama_ready(timeout=1.5, base_url=base_url):
            print(f"  llama-server up in {time.time() - t0:.1f}s")
            return True
        if proc is not None and proc.poll() is not None:
            return False
        if not printed and time.time() - t0 > 2:
            print(f"  waiting for llama-server at {base_url} ...")
            printed = True
    return False
