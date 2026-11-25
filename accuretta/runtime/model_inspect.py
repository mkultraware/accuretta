# SPDX-License-Identifier: AGPL-3.0-only
"""What a model file is: mixture-of-experts or dense, active and total parameters, and
quant, from its name and GGUF metadata.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.runtime.gguf import read_gguf_metadata
from pathlib import Path


# Filename patterns that indicate a Mixture-of-Experts model. Used as a fallback
# when GGUF header parsing fails or the model file is unreachable.
_MOE_FILENAME_HINTS = (
    "moe", "mixtral", "deepseek-v2", "deepseek-v3", "deepseek-coder-v2",
    "qwen3.5-moe", "qwen3-moe", "qwen3.6", "glm-4.7", "glm-4.6", "glm-4.5",
    "phi-3.5-moe", "grok", "dbrx", "jamba", "arctic",
)


# A3B / A13B style suffix: "active 3B" or "active 13B" — also MoE.
_ACTIVE_RE = re.compile(r"-a(\d+)b", re.IGNORECASE)


# Total params: "35B", "47B", "8x7B" (treat as ~47B), etc.
_TOTAL_RE = re.compile(r"\b(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)b\b|\b(\d+(?:\.\d+)?)b\b", re.IGNORECASE)


# Quant suffix in filename, e.g. "Q4_K_M", "Q3_K_S", "IQ4_XS", "F16".
_QUANT_RE = re.compile(r"\b(IQ\d_[A-Z]{1,3}|Q\d_[KS01]?_?[MS]?|Q\d_K|F16|BF16|F32)\b", re.IGNORECASE)


def inspect_model(model_path: str) -> dict:
    """Return a profile of a GGUF model. Reads the GGUF header for accurate
    architecture data (layer count, MoE expert count, GQA config). Falls back
    to filename heuristics when the header is unavailable.
    Never raises — returns sensible defaults if anything fails.
    """
    out = {
        "path": model_path or "",
        "name": "",
        "size_gb": 0.0,
        "is_moe": False,
        "active_params_b": 0,
        "total_params_b": 0,
        "quant": "",
        # GGUF-derived (zero when header unreadable)
        "architecture": "",
        "block_count": 0,
        "expert_count": 0,
        "expert_used_count": 0,
        "head_count_kv": 0,
        "key_length": 0,
        "value_length": 0,
        "embedding_length": 0,
        "feed_forward_length": 0,
        "expert_feed_forward_length": 0,
        "context_length": 0,
        "metadata_source": "filename",  # or "gguf"
        "has_mtp": False,                 # GGUF carries built-in MTP heads (nextn_predict_layers > 0)
        "nextn_predict_layers": 0,
        "keys": {},                       # raw GGUF metadata KV scalars (empty on filename fallback)
    }
    if not model_path:
        return out
    try:
        p = Path(model_path)
        out["name"] = p.name
        out["size_gb"] = round(p.stat().st_size / (1024 ** 3), 2)
    except Exception:
        return out
    name_lc = out["name"].lower()
    qm = _QUANT_RE.search(out["name"])
    if qm:
        out["quant"] = qm.group(1).upper()

    # Try GGUF header first — authoritative when present.
    gg = read_gguf_metadata(model_path)
    if gg.get("ok"):
        out["metadata_source"] = "gguf"
        out["architecture"] = gg["architecture"]
        out["block_count"] = gg["block_count"]
        out["expert_count"] = gg["expert_count"]
        out["expert_used_count"] = gg["expert_used_count"]
        out["head_count_kv"] = gg["head_count_kv"]
        out["key_length"] = gg["key_length"]
        out["value_length"] = gg["value_length"]
        out["embedding_length"] = gg["embedding_length"]
        out["feed_forward_length"] = gg["feed_forward_length"]
        out["expert_feed_forward_length"] = gg["expert_feed_forward_length"]
        out["context_length"] = gg["context_length"]
        out["is_moe"] = gg["expert_count"] > 1
        out["has_mtp"] = bool(gg.get("has_mtp", False))
        out["nextn_predict_layers"] = int(gg.get("nextn_predict_layers", 0) or 0)
        out["keys"] = gg.get("keys") or {}
        # total/active param counts: prefer the size_label like "30B-A3B"
        sl = (gg.get("size_label") or "").lower()
        am = _ACTIVE_RE.search(sl) or _ACTIVE_RE.search(name_lc)
        if am:
            try:
                out["active_params_b"] = int(am.group(1))
            except Exception:
                pass
        for tm in _TOTAL_RE.finditer(sl + " " + name_lc):
            try:
                if tm.group(1) and tm.group(2):
                    v = float(tm.group(1)) * float(tm.group(2))
                elif tm.group(3):
                    v = float(tm.group(3))
                else:
                    continue
            except Exception:
                continue
            if v > out["total_params_b"]:
                out["total_params_b"] = int(round(v))
        return out

    # Filename-only fallback (header parse failed)
    out["is_moe"] = any(h in name_lc for h in _MOE_FILENAME_HINTS) or bool(_ACTIVE_RE.search(name_lc))
    m = _ACTIVE_RE.search(name_lc)
    if m:
        try:
            out["active_params_b"] = int(m.group(1))
        except Exception:
            pass
    largest = 0.0
    for tm in _TOTAL_RE.finditer(name_lc):
        try:
            if tm.group(1) and tm.group(2):
                v = float(tm.group(1)) * float(tm.group(2))
            elif tm.group(3):
                v = float(tm.group(3))
            else:
                continue
        except Exception:
            continue
        if v > largest:
            largest = v
    out["total_params_b"] = int(round(largest)) if largest else 0
    return out
