# SPDX-License-Identifier: AGPL-3.0-only
"""How much memory a model's KV cache needs: per-token bytes, hybrid and
sliding-window layers, looped models, and the checkpoint flags that follow.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.settings import get_settings
from accuretta.runtime.tuning_math import _layer_passes
from accuretta.runtime.tuning_math import _mla_dims


# KV cache dtype byte costs per element (post block-quant overhead).
# q4_0/q8_0 numbers are real: 0.5 + 1/32 (delta) = ~0.5625; 1 + 1/32 = ~1.0625.
_KV_DTYPE_BYTES = {"f16": 2.0, "bf16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625, "q4_1": 0.625, "q5_0": 0.6875, "q5_1": 0.75}


# Key fragments that mark hybrid recurrent/SSM + full-attention architectures
# (Mamba/SSM blocks interleaved with periodic full-attention layers). Matched
# against the raw GGUF keys dict, so no per-arch hardcoding is needed.
_HYBRID_KEY_FRAGMENTS = (".ssm.", ".recurrent.", ".linear_attention.", ".mamba")


# Candidate suffixes for the "every Nth layer is full attention" interval key.
# Probe several — different converters name it differently.
_HYBRID_INTERVAL_SUFFIXES = (
    "full_attention_interval", "global_attention_interval",
    "hybrid_attention_interval", "attention_interval",
)


def _hybrid_info(profile: dict) -> dict:
    """Detect hybrid SSM/attention models purely from GGUF metadata keys.
    Returns {"hybrid": bool, "interval": int (0 = unknown), "evidence": str}.
    Known hybrid arch names (granitemoehybrid, nemotron-h, jamba, ...) only
    corroborate — key evidence is required before we treat a model as hybrid."""
    keys = profile.get("keys") or {}
    arch = (profile.get("architecture") or "").lower()
    evidence = ""
    for k in keys:
        kl = k.lower()
        if any(frag in kl for frag in _HYBRID_KEY_FRAGMENTS):
            evidence = k
            break
    interval = 0
    interval_key = ""
    for suffix in _HYBRID_INTERVAL_SUFFIXES:
        for k, v in keys.items():
            if k.lower().endswith(suffix) and isinstance(v, (int, float)) and v >= 2:
                interval = int(v)
                interval_key = k
                break
        if interval:
            break
    hybrid = bool(evidence or interval)
    if hybrid and not evidence:
        evidence = interval_key
    # Arch-name corroboration only (never triggers on its own): noted for
    # transparency when the keys and the arch name agree.
    _KNOWN_HYBRID_ARCHES = ("granitemoehybrid", "granitehybrid", "nemotron-h",
                            "nemotron_h", "jamba", "zamba", "falcon-h1", "bamba")
    arch_hit = any(a in arch for a in _KNOWN_HYBRID_ARCHES) or "hybrid" in arch
    return {"hybrid": hybrid, "interval": interval, "evidence": evidence,
            "arch_corroborates": bool(hybrid and arch_hit)}


def _loop_factor(profile: dict) -> float:
    """Layer passes per stored layer (1.0 unless the model loops)."""
    passes = _layer_passes(profile)
    return sum(passes) / len(passes) if passes else 1.0


def _kv_bytes_per_token(profile: dict, kv_dtype: str) -> int:
    """KV cache cost per token using GGUF-derived attention config.
    Standard path: n_layer * head_count_kv * (key_length + value_length) * dtype_bytes
    (value_length used when the parser actually read it; falls back to
    key_length only when absent). MLA path (DeepSeek V2/V3): llama.cpp caches
    the compressed latent, so n_layer * (kv_lora_rank + qk_rope_head_dim) *
    dtype_bytes. Hybrid SSM/attention models with a known full-attention
    interval are scaled by the attention-layer fraction. Returns 0 when GGUF
    metadata wasn't readable (caller falls back to bucket)."""
    n_layer = profile.get("block_count", 0) or 0
    if not n_layer:
        return 0
    db = _KV_DTYPE_BYTES.get(kv_dtype, 1.0625)

    mla = _mla_dims(profile)
    if mla:
        kv_lora, qk_rope = mla
        base = n_layer * (kv_lora + qk_rope) * db
    else:
        n_kv = profile.get("head_count_kv", 0) or 0
        head_dim = profile.get("key_length", 0) or 0
        if not (n_kv and head_dim):
            return 0
        # V dim can differ from K dim; use the parsed value_length, falling
        # back to key_length only when the metadata didn't carry it.
        v_dim = profile.get("value_length", 0) or head_dim
        base = n_layer * n_kv * (head_dim + v_dim) * db

    hy = _hybrid_info(profile)
    if hy["hybrid"] and hy["interval"]:
        # Full attention applies only to every Nth layer — KV cache lives on
        # those layers alone (SSM state is ctx-independent, not per-token KV).
        attn_layers = max(1, n_layer // hy["interval"])
        base *= attn_layers / n_layer
    return int(base * _loop_factor(profile))


def _kv_per_1k_mb(profile: dict, kv_dtype: str, file_size_gb: float) -> float:
    """Either GGUF-exact or size-bucket fallback. Returned in MB per 1k tokens."""
    bpt = _kv_bytes_per_token(profile, kv_dtype)
    if bpt > 0:
        return (bpt * 1000) / (1024 * 1024)
    # Fallback: rough bucketed estimate when GGUF header was unreadable.
    if file_size_gb < 8:
        base = 30.0
    elif file_size_gb < 20:
        base = 60.0
    elif file_size_gb < 40:
        base = 100.0
    else:
        base = 180.0
    if kv_dtype == "q4_0":
        base *= 0.55
    elif kv_dtype == "f16":
        base *= 1.9
    return base


# ---- sliding-window attention (SWA) ----------------------------------------
# Mistral, Gemma 2/3, gpt-oss and others cap the KV cache of local-attention
# layers at the sliding window. Pricing those layers as full attention
# massively overestimates KV at big ctx and starves these models of context
# they can afford. The config comes straight from GGUF keys.
_SWA_PATTERN_SUFFIXES = ("sliding_window_pattern", "swa_pattern")


def _swa_info(profile: dict) -> dict | None:
    """Sliding-window attention config from GGUF metadata keys. Returns
    {"window": w, "pattern": p} or None. pattern p >= 2 means every p-th
    layer is FULL attention and the rest are windowed (Gemma 3 style: 5:1).
    pattern 0 = unknown mix (callers pick a conservative split)."""
    keys = profile.get("keys") or {}
    w = 0
    for k, v in keys.items():
        if k.lower().endswith("attention.sliding_window") and isinstance(v, (int, float)) and v:
            w = int(v)
            break
    if not w:
        return None
    p = 0
    for k, v in keys.items():
        kl = k.lower()
        if any(kl.endswith(suf) for suf in _SWA_PATTERN_SUFFIXES) and isinstance(v, (int, float)) and v >= 2:
            p = int(v)
            break
    return {"window": w, "pattern": p}


def _kv_total_bytes(profile: dict, kv_dtype: str, ctx_tokens: int, v_dtype: str = "") -> int:
    """KV cache size in bytes for a REAL context length — not a linear
    extrapolation. Same GGUF-exact base as _kv_bytes_per_token, but
    sliding-window layers contribute min(ctx, window) tokens instead of ctx.
    Hybrid SSM models keep the attention-layer-fraction scaling (their
    full-attention layers are treated as unwindowed — conservative).
    v_dtype enables split K/V cache (--cache-type-k/--cache-type-v): K is the
    sensitive half (softmax error propagation), V tolerates 4-bit fine.
    Returns 0 when GGUF metadata is unreadable."""
    n_layer = profile.get("block_count", 0) or 0
    if not n_layer or ctx_tokens <= 0:
        return 0
    db_k = _KV_DTYPE_BYTES.get(kv_dtype, 1.0625)
    db_v = _KV_DTYPE_BYTES.get(v_dtype, db_k) if v_dtype else db_k
    # Looped layers keep one KV cache per pass (see _layer_passes).
    passes = _layer_passes(profile)
    lf = sum(passes) / n_layer if passes else 1.0

    mla = _mla_dims(profile)
    hy = _hybrid_info(profile)
    if mla:
        kv_lora, qk_rope = mla
        # MLA stores one combined latent tensor — llama.cpp types it by
        # cache-type-k alone (no separate V cache exists).
        return int(n_layer * (kv_lora + qk_rope) * db_k * ctx_tokens * lf)

    n_kv = profile.get("head_count_kv", 0) or 0
    head_dim = profile.get("key_length", 0) or 0
    if not (n_kv and head_dim):
        return 0
    v_dim = profile.get("value_length", 0) or head_dim
    per_layer_per_tok = n_kv * (head_dim * db_k + v_dim * db_v)

    if hy["hybrid"] and hy["interval"]:
        # Full attention applies only to every Nth layer — KV cache lives on
        # those layers alone (SSM state is ctx-independent, not per-token KV).
        attn_layers = max(1, n_layer // hy["interval"])
        return int(attn_layers * per_layer_per_tok * ctx_tokens * lf)

    swa = _swa_info(profile)
    if swa:
        w = swa["window"]
        # Per-layer array metadata (gemma4-style): sliding_window_pattern is a
        # bool PER LAYER (True = windowed), head_count_kv varies per layer, and
        # windowed layers use smaller head dims (key/value_length_swa). Exact
        # per-layer math — a scalar-pattern guess is wildly wrong here.
        keys = profile.get("keys") or {}
        arch = profile.get("architecture") or ""
        pat = keys.get(f"{arch}.attention.sliding_window_pattern")
        hkv_arr = keys.get(f"{arch}.attention.head_count_kv")
        if isinstance(pat, list) and len(pat) == n_layer:
            k_swa = keys.get(f"{arch}.attention.key_length_swa")
            v_swa = keys.get(f"{arch}.attention.value_length_swa")
            if not isinstance(k_swa, (int, float)) or not k_swa:
                k_swa = head_dim
            if not isinstance(v_swa, (int, float)) or not v_swa:
                v_swa = v_dim
            total = 0
            for i in range(n_layer):
                heads = hkv_arr[i] if (isinstance(hkv_arr, list) and len(hkv_arr) == n_layer) else n_kv
                if pat[i]:
                    total += heads * (k_swa * db_k + v_swa * db_v) * min(ctx_tokens, w) * passes[i]
                else:
                    total += heads * (head_dim * db_k + v_dim * db_v) * ctx_tokens * passes[i]
            return int(total)
        if swa["pattern"]:
            full_layers = max(1, n_layer // swa["pattern"])
        elif "gptoss" in (profile.get("architecture") or "").lower() or "gpt-oss" in (profile.get("architecture") or "").lower():
            full_layers = (n_layer + 1) // 2  # gpt-oss alternates SWA / full
        else:
            # Pattern unknown: assume half the layers are full attention.
            # Deliberately conservative — for all-SWA models (Mistral) this
            # OVERestimates KV (smaller ctx, safe), never under.
            full_layers = (n_layer + 1) // 2
        swa_layers = max(0, n_layer - full_layers)
        total = full_layers * per_layer_per_tok * ctx_tokens
        total += swa_layers * per_layer_per_tok * min(ctx_tokens, w)
        return int(total * lf)

    return int(n_layer * per_layer_per_tok * ctx_tokens * lf)


def _kv_total_mb(profile: dict, kv_dtype: str, ctx_tokens: int, file_size_gb: float, v_dtype: str = "") -> float:
    """Context-exact KV cost in MB. GGUF-exact (SWA/MLA/hybrid aware, split
    K/V dtype aware) when the header was readable; linear size-bucket
    fallback otherwise."""
    b = _kv_total_bytes(profile, kv_dtype, ctx_tokens, v_dtype)
    if b > 0:
        return b / (1024 * 1024)
    return _kv_per_1k_mb(profile, kv_dtype, file_size_gb) * (ctx_tokens / 1000.0)


def _decode_bytes_per_token(profile: dict, size_mb: float, *, kv_dtype: str, v_dtype: str,
                            fill: int, n_cpu_moe: int = 0, num_gpu: int = 99,
                            dense_share=None, active_expert_bytes: float = 0.0) -> tuple[float, float]:
    """(gpu_bytes, cpu_bytes) read per decoded token. MoE reads its dense part
    plus only the active experts; dense models read every weight. The KV read
    grows with how much of the context is filled. Looped layers are read once
    per pass (see _layer_passes): llama.cpp's --n-cpu-moe moves the FIRST n
    layers' experts and -ngl keeps the LAST layers on the GPU, so which layers
    sit in system RAM decides how often their bytes cross it."""
    n_layer = int(profile.get("block_count", 0) or 0)
    model_bytes = max(size_mb, 0.0) * 1048576.0
    passes = _layer_passes(profile) or [1] * max(n_layer, 1)
    try:
        kv_read = float(_kv_total_bytes(profile, kv_dtype, max(int(fill), 1), v_dtype) or 0)
    except Exception:
        kv_read = 0.0
    if profile.get("is_moe") and active_expert_bytes > 0 and dense_share and n_layer:
        n_off = min(max(int(n_cpu_moe), 0), n_layer)
        per_layer = active_expert_bytes / n_layer      # one stored layer's active experts
        dense_bytes = model_bytes * float(dense_share) * sum(passes) / n_layer
        return (dense_bytes + per_layer * sum(passes[n_off:]) + kv_read,
                per_layer * sum(passes[:n_off]))
    per_layer = model_bytes / n_layer if n_layer else model_bytes
    if n_layer and 0 < num_gpu < n_layer:
        n_cpu = n_layer - num_gpu
        return per_layer * sum(passes[n_cpu:]) + kv_read, per_layer * sum(passes[:n_cpu])
    return (per_layer * sum(passes) if n_layer else model_bytes) + kv_read, 0.0


def _checkpoint_spawn_flags(caps: dict, profile: dict) -> list[str]:
    """Explicit compatibility override for hybrid/SWA context checkpoints.

    On the llama.cpp build this was written against (v0.7.9 era, Qwen 3.8 and
    Qwen35 hybrids) these flags disabled ordinary slot-prefix reuse, turning
    warm 32K/62K follow-ups into full prefills. Returning no flags does not
    disable checkpoints: newer builds create their own by default (build 12121:
    32 per slot, 8192 apart). Never enabled just because a model is hybrid."""
    try:
        s = get_settings()
        if not bool(s.get("ctx_checkpoints_enabled", False)):
            return []
        n_ck = int(1 if s.get("ctx_checkpoints") is None else s.get("ctx_checkpoints"))
    except Exception:
        return []
    if n_ck <= 0 or not caps.get("ctx_checkpoints"):
        return []
    hy = _hybrid_info(profile)
    swa = _swa_info(profile)
    if not (hy["hybrid"] or swa):
        return []
    # Host-RAM guard: each checkpoint costs about ctx-filled x KV-bytes/token of
    # host RAM (8 checkpoints at 262k ctx on a hybrid MoE would be ~22 GB). Cap
    # total checkpoint RAM at ~2 GB by shrinking the count for big contexts; even
    # one checkpoint gives the prefix-restore win.
    try:
        ctx_now = max(int(s.get("num_ctx") or 32768), 1)
        kv_t = (s.get("kv_cache_type") or "q8_0").strip().lower()
        kv_v = (s.get("kv_cache_type_v") or "").strip().lower()
        per_tok = _kv_total_bytes(profile, kv_t, 65536, kv_v) / 65536.0
        if per_tok > 0:
            max_ck = int(2_000_000_000 / (per_tok * ctx_now))
            n_ck = max(1, min(n_ck, max_ck)) if max_ck >= 1 else 1
    except Exception:
        pass
    flags = ["--ctx-checkpoints", str(n_ck)]
    try:
        step = int(s.get("checkpoint_min_step") or 4096)
    except Exception:
        step = 4096
    if step > 0:
        if caps.get("checkpoint_min_step"):
            flags += ["--checkpoint-min-step", str(step)]
        elif caps.get("checkpoint_every_n"):
            flags += ["--checkpoint-every-n-tokens", str(step)]
    return flags
