# SPDX-License-Identifier: AGPL-3.0-only
"""One request to llama-server for the agent: sampling options, the no-think prefill
probe, the streaming post with first-token and stall timeouts, and reading its errors
(context overflow, model loading, transient failures).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.endpoints import LLAMA
from accuretta.runtime.llama_post import llama_post
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_stream import _StallGuardedStream


# ---- no-think rounds that keep the prompt prefix ------------------------------
# Some chat templates write the thinking switch into the system block: Qwen3.8
# adds "Reasoning effort is set to ..." only while thinking is on. A round sent
# with enable_thinking=False then changes the very top of the prompt, and a
# hybrid model re-reads all of it (measured: 13,300 of 13,300 tokens). When the
# template's own no-think form is an empty <think></think> block, such a round
# keeps the template flags and starts the reply with that block instead
# (measured: 10 tokens re-read, native tool calls intact). Detected per loaded
# model and flags from the template itself; any other template keeps
# enable_thinking=False exactly as before.
_no_think_prefill_cache: dict[str, str] = {}


_NO_THINK_PROBE_SYS = "QQSYSPROBEQQ"


_NO_THINK_PROBE_USER = "QQUSERPROBEQQ"


def _no_think_prefill(tpl_kwargs: dict) -> str:
    """The assistant prefill that skips thinking without changing earlier
    prompt text, or "" when this template does not need or support one."""
    # Ask only the llama-server this bridge launched and still runs.
    if not _llama.is_running():
        return ""
    flags = {k: v for k, v in (tpl_kwargs or {}).items() if k != "enable_thinking"}
    key = (_llama.loaded_model() or "") + "|" + json.dumps(flags, sort_keys=True, default=str)
    if key in _no_think_prefill_cache:
        return _no_think_prefill_cache[key]
    prefill = ""
    try:
        msgs = [{"role": "system", "content": _NO_THINK_PROBE_SYS},
                {"role": "user", "content": _NO_THINK_PROBE_USER}]
        on = str(llama_post("/apply-template", {"messages": msgs, "chat_template_kwargs":
                            {**flags, "enable_thinking": True}}, timeout=10).get("prompt") or "")
        off = str(llama_post("/apply-template", {"messages": msgs, "chat_template_kwargs":
                             {**flags, "enable_thinking": False}}, timeout=10).get("prompt") or "")
        user_end = off.find(_NO_THINK_PROBE_USER) + len(_NO_THINK_PROBE_USER)
        same = 0
        while same < min(len(on), len(off)) and on[same] == off[same]:
            same += 1
        empty_think = re.search(r"<think>\s*</think>\s*$", off)
        # Only when the switch rewrites text before the reply starts (a real
        # re-read) and the template's no-think form is an empty think block.
        if user_end > len(_NO_THINK_PROBE_USER) and same < user_end and empty_think:
            prefill = off[empty_think.start():]
    except Exception:
        prefill = ""
    _no_think_prefill_cache[key] = prefill
    return prefill


# Two-phase stream timeouts. The first byte can legitimately take minutes on a
# big window (prefill of a 130K prompt at CPU speed); once tokens flow, a
# multi-minute silence means the server is wedged, and the turn should fail
# with a clear message instead of hanging on `for raw in resp`.
_LLAMA_FIRST_TOKEN_TIMEOUT_S = 300


_LLAMA_STALL_TIMEOUT_S = 90


def llama_post_stream(path: str, payload: dict, base: str | None = None,
                      first_token_timeout_s: float | None = None,
                      stall_timeout_s: float | None = None):
    """POST and return the raw response object — caller iterates over SSE lines.

    Two-phase read timeout: urlopen waits up to `first_token_timeout_s` for the
    response to start (connect + prefill), then the per-read socket timeout is
    dropped to `stall_timeout_s` for the body so a wedged server raises
    socket.timeout mid-stream instead of hanging the turn forever. The swap is
    deferred past the first chunk — see _StallGuardedStream."""
    first_t = (first_token_timeout_s if first_token_timeout_s is not None
               else _LLAMA_FIRST_TOKEN_TIMEOUT_S)
    stall_t = (stall_timeout_s if stall_timeout_s is not None
               else _LLAMA_STALL_TIMEOUT_S)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base or LLAMA}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=first_t)
    # Tight inter-token timeout, applied lazily after the first byte — the
    # first read may legitimately take minutes (prefill) and keeps first_t.
    return _StallGuardedStream(resp, stall_t)


_HTTP_ERROR_BODY_CACHE: dict[int, str] = {}


def _http_error_body(e: Exception, limit: int = 60000) -> str:
    """Read + cache the body of an HTTPError so it can be classified and then
    surfaced without double-consuming the response stream (e.read() consumes
    the buffer — a second read returns empty)."""
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return ""
    key = id(e)
    cached = _HTTP_ERROR_BODY_CACHE.get(key)
    if cached is None:
        try:
            cached = e.read().decode("utf-8", "replace")
        except Exception:
            cached = ""
        _HTTP_ERROR_BODY_CACHE[key] = cached
    return cached[:limit]


def _is_ctx_overflow(e: Exception) -> bool:
    """True when a llama-server request failed because the prompt exceeded n_ctx.
    With --no-context-shift the server rejects (HTTP error) rather than sliding
    the window, so we detect it and re-trim + retry instead of failing the turn."""
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return False
    blob = (_http_error_body(e) + " " + str(e)).lower()
    return (("context" in blob and "exceed" in blob)
            or "context shift" in blob
            or "n_ctx" in blob
            or "exceeds the available context" in blob)


# A model load (reload, Auto-tune, load-time speed check) can take minutes on a
# big model; a turn that arrives meanwhile waits up to this long for it.
_MODEL_LOAD_WAIT_S = 300


def _is_model_loading(e: Exception) -> bool:
    """True for llama-server's 503 "Loading model": up, but not ready yet."""
    import urllib.error
    if not (isinstance(e, urllib.error.HTTPError) and e.code == 503):
        return False
    return "loading model" in (_http_error_body(e) + " " + str(e)).lower()


def _is_transient_server_error(e: Exception) -> bool:
    """True when a llama-server failure is likely transient: a 5xx from its own
    handler, or a body naming GPU/OOM pressure (seen mid-turn on long agentic
    runs, with the next request served fine). Worth a short backoff retry
    before failing the turn."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError) and e.code in (500, 502, 503, 504):
        return True
    blob = (_http_error_body(e) + " " + str(e)).lower()
    return any(frag in blob for frag in (
        "failed to allocate", "out of memory", "out of gpu memory",
        "cuda error", "cuda failure", "cuda out of memory",
        "no memory", "oom"))


def llama_options(settings: dict) -> dict:
    """Map our settings to llama-server /v1/chat/completions top-level params.
    Unlike Ollama, llama-server treats ctx size / GPU layers / batch / threads
    as server-launch flags — not per-request. We only ship per-request
    sampling/predict params here."""
    opt: dict = {}
    t = settings.get("temperature")
    opt["temperature"] = float(t) if t is not None and t != "" else 0.5
    p = settings.get("top_p")
    opt["top_p"] = float(p) if p is not None and p != "" else 0.9
    # Pass through additional sampler params if set. These prevent the model
    # from looping or producing low-diversity output.
    for key, default in (("top_k", 40), ("min_p", 0.05),
                         ("repeat_penalty", 1.1),
                         ("presence_penalty", 0.0),
                         ("frequency_penalty", 0.0)):
        v = settings.get(key)
        if v is not None and v != "":
            try:
                # top_k is a count: maya's server refuses 40.0 ("top_k: an
                # integer", every turn failed with HTTP 400); llama.cpp takes both.
                opt[key] = int(round(float(v))) if key == "top_k" else float(v)
            except (ValueError, TypeError):
                pass
    np = int(settings.get("num_predict") or -1)
    if np > 0:
        opt["max_tokens"] = np
    else:
        # num_predict unset means "no limit" to llama-server, letting a runaway
        # generation roll to n_ctx. Default to max_output_tokens; run_chat_turn also
        # clamps it to the reserved output headroom.
        try:
            _mo = int(settings.get("max_output_tokens") or 8192)
        except (ValueError, TypeError):
            _mo = 8192
        if _mo <= 0:
            return opt  # user explicitly disabled the cap
        opt["max_tokens"] = max(256, _mo)
    return opt
