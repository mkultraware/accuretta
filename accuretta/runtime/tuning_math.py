# SPDX-License-Identifier: AGPL-3.0-only
"""Auto-tune arithmetic: reasoning overrides, MLA sizes, layer passes, draft choice, RAM bandwidth.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import time


def _reasoning_payload_overrides(config: dict, capability: dict) -> dict:
    """Return only explicit, per-message top-level llama.cpp controls."""
    if config.get("effort") == "auto":
        return {}  # Preserve the painstakingly tuned legacy behavior exactly.
    if capability.get("mode") == "budget":
        # Modern llama.cpp's real per-request budget. The caller also sends a
        # template kwarg for older builds, and the stream guard remains the
        # final backstop if either is ignored.
        return {"thinking_budget_tokens": max(0, int(config.get("budget") or 0))}
    if (config.get("native_effort")
            and (capability.get("native_parameter") or "reasoning_effort") == "reasoning_effort"):
        # OpenAI-compatible clients expect this at top level, while the caller
        # mirrors it into chat_template_kwargs for model-native templates.
        return {"reasoning_effort": config["native_effort"]}
    return {}


def _mla_dims(profile: dict):
    """DeepSeek V2/V3-style MLA detection. Returns (kv_lora_rank,
    qk_rope_head_dim) when the GGUF metadata exposes kv_lora_rank, else None.
    llama.cpp caches the compressed latent for MLA, not per-head K/V."""
    keys = profile.get("keys") or {}
    arch = profile.get("architecture") or ""
    if not arch:
        return None
    kv_lora = keys.get(f"{arch}.attention.kv_lora_rank")
    if not isinstance(kv_lora, (int, float)) or not kv_lora:
        return None
    qk_rope = keys.get(f"{arch}.attention.qk_rope_head_dim")
    qk_rope = int(qk_rope) if isinstance(qk_rope, (int, float)) and qk_rope else 0
    return int(kv_lora), qk_rope


def _layer_passes(profile: dict) -> list[int]:
    """How many times each stored layer runs per token. Looped transformers
    (llama.cpp's `<arch>.loop.count/start/end`, e.g. Inkling 2 Mini: layers
    6-17 run 3 times, 48 passes from 24 layers of weights) keep a KV cache
    per pass and read the looped weights on every pass, so KV size and decode
    cost scale with passes, not stored layers. All ones for ordinary models."""
    n_layer = int(profile.get("block_count", 0) or 0)
    if n_layer <= 0:
        return []
    passes = [1] * n_layer
    keys = profile.get("keys") or {}
    arch = profile.get("architecture") or ""

    def _key(name):
        v = keys.get(f"{arch}.loop.{name}")
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

    count, start, end = _key("count"), _key("start"), _key("end")
    if count > 1 and 0 <= start < end <= n_layer:
        for i in range(start, end):
            passes[i] = count
    return passes


def _speculative_choice(settings: dict, has_mtp: bool, is_moe: bool, nextn: int,
                        pair: dict, draft_kind: str) -> tuple[str, str]:
    """Pick the speculative-decoding strategy and its auto-tune note.

    A MANUAL choice always wins — auto-tuning and the setup advisor must never
    replace what the user explicitly selected. Otherwise a metadata-validated
    DFlash/DSpark pair on disk beats MTP: measured ~+50% decode (112 vs 75
    tok/s) on the reference Qwen3.8-27B + DFlash2 pair at equal context.
    Falls back to MTP heads → n-gram (dense) → off (MoE)."""
    manual = str((settings or {}).get("spec_strategy_source") or "").strip().lower() == "manual"
    saved = str((settings or {}).get("spec_strategy") or "").strip().lower()
    if manual and saved in {"off", "ngram-mod", "draft-mtp", "dflash", "dspark"}:
        return saved, f"speculative decoding: {saved} (manual choice kept)."
    if str((settings or {}).get("spec_draft_mode") or "auto").strip().lower() == "off":
        # Decoder explicitly unloaded — a validated pair on disk must not
        # resurrect it. Fall through to the draft-less policy below.
        pair = None
    if pair:
        kind = draft_kind if draft_kind in {"dflash", "dspark"} else "dflash"
        name = os.path.basename(str(pair.get("path") or "")) or "draft"
        return kind, (f"speculative decoding: {kind} (validated draft {name}; "
                      "measured faster than draft-mtp on this setup).")
    if has_mtp:
        if nextn:
            return "draft-mtp", (
                f"speculative decoding: draft-mtp ({nextn} MTP heads detected, ~1.85x decode win).")
        return "draft-mtp", "speculative decoding: draft-mtp (MTP-capable model by name, ~1.85x decode win)."
    if is_moe:
        return "off", "speculative decoding: OFF (n-gram is net-negative on MoE per public benchmarks)."
    return "ngram-mod", "speculative decoding: ngram-mod (free win on dense models)."


def _probe_ram_bandwidth_gbps() -> float:
    """One-shot system RAM bandwidth estimate via ctypes memcpy (128 MB buffer,
    8 passes, best of 3; read+write both traverse RAM). Returns 0 on failure.
    ~0.3-1s — meant to run once per machine, then be cached in settings."""
    import ctypes
    size = 128 * 1024 * 1024
    try:
        libc = ctypes.CDLL("msvcrt" if os.name == "nt" else None)
        memcpy = libc.memcpy
        memcpy.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        memcpy.restype = ctypes.c_void_p
        src = ctypes.create_string_buffer(size)
        dst = ctypes.create_string_buffer(size)
        best = 0.0
        chunk = ctypes.c_size_t(size)
        for _ in range(3):
            t0 = time.perf_counter()
            for _ in range(8):
                memcpy(dst, src, chunk)
            dt = time.perf_counter() - t0
            if dt > 0:
                best = max(best, (8 * size * 2) / dt)
        return round(best / 1e9, 1) if best > 0 else 0.0
    except Exception:
        return 0.0
