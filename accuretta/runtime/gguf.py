# SPDX-License-Identifier: AGPL-3.0-only
"""Read a GGUF model file's header: architecture, layers, context, experts, tokenizer facts.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def read_gguf_metadata(path: str, max_bytes: int = 4 * 1024 * 1024,
                       retry_bytes: int = 64 * 1024 * 1024) -> dict:
    """Parse the GGUF v2/v3 header and return architecture-level metadata.
    Reads at most a few MB from the file's start — the metadata block lives
    right after the magic (if the KV list doesn't fit in `max_bytes`, retries
    once with a `retry_bytes` cap, 64MB by default, 0 for no retry; the
    tensor-info section that follows the KV list is never parsed). Never
    raises; returns {ok: False} on any parse failure so callers can fall back
    to filename heuristics.

    Besides the structured fields below, every scalar KV entry is collected
    verbatim into out["keys"] (full key names, nothing stripped) so tuners can
    probe architecture-specific keys we don't hardcode. Arrays are recorded as
    {"__array_len__": n} — contents are skipped, the length is cheap and useful
    (e.g. tokenizer.ggml.tokens length ≈ vocab size). out["parse_status"] is
    "complete" (whole KV list read), "truncated" (buffer ran out mid-list), or
    "degraded" (unknown/nested value type — clean stop, entries so far kept).

    GGUF spec reference: https://github.com/ggerganov/ggml/blob/master/docs/gguf.md
    Layout: "GGUF" magic, uint32 version, uint64 tensor_count, uint64 kv_count,
    then kv_count entries of (str key, uint32 value_type, value).
    """
    out = {
        "ok": False,
        "architecture": "",
        "size_label": "",
        "block_count": 0,             # n_layer
        "expert_count": 0,            # 0 for dense, > 0 for MoE
        "expert_used_count": 0,       # active experts per token
        "head_count": 0,              # query heads
        "head_count_kv": 0,           # KV heads (smaller for GQA)
        "key_length": 0,              # per-head key dim
        "value_length": 0,            # per-head value dim
        "embedding_length": 0,
        "feed_forward_length": 0,
        "expert_feed_forward_length": 0,  # MoE: per-expert FFN width
        "context_length": 0,              # the model's trained max context
        "nextn_predict_layers": 0,        # >0 means the GGUF carries MTP heads
                                          # (key: "<arch>.nextn_predict_layers"). This is the same
                                          # signal llama.cpp itself uses to enable --spec-type draft-mtp.
        "has_mtp": False,                 # convenience flag: nextn_predict_layers > 0
        "keys": {},                       # all scalar metadata KV entries (see docstring)
        "parse_status": "",               # complete | truncated | degraded
    }
    if not path:
        return out
    try:
        import struct

        # GGUF value type enum
        T_UINT8, T_INT8 = 0, 1
        T_UINT16, T_INT16 = 2, 3
        T_UINT32, T_INT32 = 4, 5
        T_FLOAT32, T_BOOL, T_STRING, T_ARRAY = 6, 7, 8, 9
        T_UINT64, T_INT64, T_FLOAT64 = 10, 11, 12
        scalar_fmt = {
            T_UINT8: ("<B", 1), T_INT8: ("<b", 1),
            T_UINT16: ("<H", 2), T_INT16: ("<h", 2),
            T_UINT32: ("<I", 4), T_INT32: ("<i", 4),
            T_FLOAT32: ("<f", 4), T_BOOL: ("<?", 1),
            T_UINT64: ("<Q", 8), T_INT64: ("<q", 8),
            T_FLOAT64: ("<d", 8),
        }

        class _Degraded(Exception):
            """Unknown/nested value type hit — stop parsing at that entry."""

        def _parse_kv_list(data: bytes):
            """Parse just the metadata KV list from `data`. Returns
            (meta, status). Never raises on malformed input."""
            meta: dict = {}
            if len(data) < 24 or data[:4] != b"GGUF":
                return meta, "degraded"
            pos = 4
            version = struct.unpack_from("<I", data, pos)[0]; pos += 4
            if version < 2:
                return meta, "degraded"  # v1 had a different layout
            pos += 8  # tensor_count, unused
            kv_count = struct.unpack_from("<Q", data, pos)[0]; pos += 8

            def read_str(p):
                n = struct.unpack_from("<Q", data, p)[0]
                p += 8
                s = data[p:p + n].decode("utf-8", errors="replace")
                return s, p + n

            def read_value(p, vtype):
                if vtype in scalar_fmt:
                    fmt, sz = scalar_fmt[vtype]
                    return struct.unpack_from(fmt, data, p)[0], p + sz
                if vtype == T_STRING:
                    return read_str(p)
                if vtype == T_ARRAY:
                    etype = struct.unpack_from("<I", data, p)[0]; p += 4
                    ecount = struct.unpack_from("<Q", data, p)[0]; p += 8
                    if etype in scalar_fmt:
                        efmt, sz = scalar_fmt[etype]
                        # Small numeric arrays carry real tuning signal on newer
                        # archs (gemma4 per-layer head_count_kv / sliding_window_
                        # pattern) — keep them when they're cheap. Big arrays
                        # (tokenizer vocab etc.) stay length-only.
                        if 0 < ecount <= 1024:
                            try:
                                return list(struct.unpack_from("<" + efmt[1] * int(ecount), data, p)), p + sz * int(ecount)
                            except Exception:
                                pass
                        return {"__array_len__": int(ecount)}, p + sz * int(ecount)
                    if etype == T_STRING:
                        for _ in range(ecount):
                            n = struct.unpack_from("<Q", data, p)[0]
                            p += 8 + n
                        return {"__array_len__": int(ecount)}, p
                    # Nested arrays / unknown element type — bail cleanly.
                    raise _Degraded()
                # Unknown scalar type: stop cleanly at this entry, keeping what we have;
                # continuing from an unadvanced position would garbage-parse every later key.
                raise _Degraded()

            status = "complete"
            for _ in range(kv_count):
                if len(meta) >= 4096:
                    # Sane bound — real GGUFs carry ~50-200 KV entries.
                    status = "degraded"
                    break
                try:
                    key, pos = read_str(pos)
                    vtype = struct.unpack_from("<I", data, pos)[0]; pos += 4
                    val, pos = read_value(pos, vtype)
                    if isinstance(val, str) and len(val) > 2048:
                        # Template kwargs are the strongest model-agnostic
                        # signal for adjustable reasoning. Keep a bounded copy
                        # of chat templates; collapse unrelated giant strings.
                        if "chat_template" in key.lower():
                            val = val[:262144]
                        else:
                            val = {"__array_len__": len(val)}
                    meta[key] = val
                except _Degraded:
                    status = "degraded"
                    break
                except (struct.error, IndexError):
                    # Ran past the buffered window.
                    status = "truncated"
                    break
            return meta, status

        # First pass with the small read; if the KV list didn't fit in the
        # buffer, retry once with a much larger cap before giving up.
        meta: dict = {}
        status = "truncated"
        for cap in ((max_bytes, retry_bytes) if retry_bytes > max_bytes else (max_bytes,)):
            with open(path, "rb") as f:
                data = f.read(cap)
            meta, status = _parse_kv_list(data)
            if status != "truncated" or len(data) < cap:
                break
        out["keys"] = meta
        out["parse_status"] = status

        arch = str(meta.get("general.architecture", "")).strip()
        if not arch:
            return out
        out["architecture"] = arch
        out["size_label"] = str(meta.get("general.size_label", "")).strip()

        def num(*keys):
            for k in keys:
                v = meta.get(k)
                if isinstance(v, (int, float)) and v:
                    return int(v)
                # Per-layer arrays (Inkling 2: head_count and head_count_kv list
                # every layer): the largest entry, so KV math stays exact or
                # errs high instead of reading 0 and falling back to a size guess.
                if isinstance(v, list) and v and all(isinstance(x, (int, float)) for x in v) and max(v):
                    return int(max(v))
            return 0

        out["block_count"] = num(f"{arch}.block_count")
        out["expert_count"] = num(f"{arch}.expert_count")
        out["expert_used_count"] = num(f"{arch}.expert_used_count")
        out["head_count"] = num(f"{arch}.attention.head_count")
        out["head_count_kv"] = num(
            f"{arch}.attention.head_count_kv",
            f"{arch}.attention.head_count",  # MHA: kv = q
        )
        out["key_length"] = num(f"{arch}.attention.key_length")
        out["value_length"] = num(f"{arch}.attention.value_length")
        out["embedding_length"] = num(f"{arch}.embedding_length")
        out["feed_forward_length"] = num(f"{arch}.feed_forward_length")
        out["expert_feed_forward_length"] = num(f"{arch}.expert_feed_forward_length")
        out["context_length"] = num(f"{arch}.context_length")

        # Derive missing dims when possible
        if not out["key_length"] and out["embedding_length"] and out["head_count"]:
            out["key_length"] = out["embedding_length"] // out["head_count"]
        if not out["value_length"]:
            out["value_length"] = out["key_length"]

        # MTP detection: llama.cpp tags MTP-capable architectures (Qwen 3.5/3.6,
        # DeepSeek V3/R1, ...) with "<arch>.nextn_predict_layers", which survives
        # renames and repacks. Older GGUFs without the key get has_mtp=False.
        nextn = num(f"{arch}.nextn_predict_layers")
        if not nextn:
            # Some converters drop the arch prefix on this key. Catch the unprefixed
            # variant as a backup before giving up.
            for k, v in meta.items():
                if k.endswith(".nextn_predict_layers") or k == "nextn_predict_layers":
                    if isinstance(v, (int, float)) and v > 0:
                        nextn = int(v)
                        break
        out["nextn_predict_layers"] = int(nextn or 0)
        out["has_mtp"] = out["nextn_predict_layers"] > 0

        out["ok"] = bool(out["architecture"] and out["block_count"])
        return out
    except Exception:
        return out
