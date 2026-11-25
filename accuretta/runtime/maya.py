# SPDX-License-Identifier: AGPL-3.0-only
"""Maya engine helpers: KV-cache size per token, the matching drafter, CUDA folders, child processes.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.processes import _process_parent_map
from pathlib import Path


def _maya_kv_bytes_per_token(meta: dict) -> int:
    """Bytes of maya's FP8 KV cache per token of context: K and V of every
    layer visit without a sliding window (the windowed ones are a fixed ring,
    counted in _MAYA_FIXED_GIB). Inkling 2 Mini: 8 of 48 visits x 8 KV heads x
    (128 + 128) = 16 KiB, as the engine reports."""
    keys = meta.get("keys") or {}
    arch = str(meta.get("architecture") or "")
    kv_heads = int(meta.get("head_count_kv") or 8)
    key_len = int(meta.get("key_length") or 128)
    width = key_len + int(meta.get("value_length") or key_len)
    pattern = keys.get(f"{arch}.attention.sliding_window_pattern")
    if isinstance(pattern, list) and pattern:
        full = sum(1 for x in pattern if not x)
    else:
        full = int(meta.get("block_count") or 48) // 6
    return max(1, full) * kv_heads * width


def _maya_drafter_for(model_path: str) -> str:
    """Thinking Machines' drafter beside the model: mtp.safetensors, or another
    *mtp*.safetensors in the same folder. "" when there is none."""
    folder = Path(model_path).parent
    exact = folder / "mtp.safetensors"
    if exact.is_file():
        return str(exact)
    try:
        found = sorted(p for p in folder.glob("*mtp*.safetensors") if p.is_file())
    except OSError:
        found = []
    return str(found[0]) if found else ""


def _maya_cuda_dirs(engine: dict) -> list[str]:
    """Folders with the CUDA runtime DLLs maya-ik2 links (cudart64_*, cublas64_*),
    newest toolkit first. A toolkit installed after the bridge started isn't on
    its PATH yet, so look where NVIDIA's installer puts it. [] off Windows or
    when the DLLs sit beside the engine."""
    if os.name != "nt":
        return []
    try:
        if any(Path(engine.get("engine") or "").parent.glob("cudart64_*.dll")):
            return []
    except OSError:
        pass
    bases = []
    for name, value in os.environ.items():
        if name.upper() == "CUDA_PATH" or name.upper().startswith("CUDA_PATH_V"):
            bases.append(Path(value))
    toolkit = Path(os.environ.get("ProgramW6432") or os.environ.get("PROGRAMFILES") or "C:/Program Files") \
        / "NVIDIA GPU Computing Toolkit" / "CUDA"
    try:
        bases += sorted((p for p in toolkit.glob("v*") if p.is_dir()), reverse=True)
    except OSError:
        pass
    out: list[str] = []
    for base in bases:
        for sub in (base / "bin" / "x64", base / "bin"):
            try:
                if any(sub.glob("cudart64_*.dll")) and str(sub) not in out:
                    out.append(str(sub))
            except OSError:
                continue
    return out


def _process_children(pid: int) -> list[int]:
    """Every process below `pid` (children, grandchildren). [] when it can't be told."""
    parents: dict[int, list[int]] = {}
    for child, ppid in _process_parent_map().items():
        parents.setdefault(ppid, []).append(child)
    out: list[int] = []
    todo = [int(pid)]
    while todo:
        for child in parents.get(todo.pop(), []):
            if child != pid and child not in out:
                out.append(child)
                todo.append(child)
    return out
