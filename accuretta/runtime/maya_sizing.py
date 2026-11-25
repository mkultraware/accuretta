# SPDX-License-Identifier: AGPL-3.0-only
"""Sizing the Maya engine: the memory its weights, draft, vision and fixed parts need,
whether a model fits, and trimming the engine's memory.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
from accuretta.core.paths import DATA
from accuretta.runtime.maya import _maya_kv_bytes_per_token
from accuretta.runtime.maya import _process_children
from accuretta.runtime.maya_find import _maya_gguf_meta
from accuretta.runtime.maya_find import _maya_python_cmd
from accuretta.security.child_env import _child_env
from pathlib import Path


_MAYA_FIXED_GIB = 1.85        # work buffers + CUDA context beside weights and KV (4090, 262k: 21.8 GiB in use)


_MAYA_DRAFT_EXTRA_GIB = 0.45  # the drafter's buffers and its growth on the first request


_MAYA_DRAFT_CTX = 65536       # positions the drafter's global layer sees (IK2_DRAFT_CTX)


# Left free on the card: Windows pages a full card to RAM silently (262k with
# the drafter did, and fell to ~70 tok/s). Tested at 0.2 GiB on the 4090 with
# 22.1 GiB free (2026-11-21): 262,144 plain and 180,224 with the drafter both ran
# at full speed (153 / 300 tok/s editing), 320-370 MiB still free, nothing paged
# (maya's shared GPU memory stayed ~100 MB). 0.25 keeps one step past that.
_MAYA_MARGIN_GIB = 0.25


_MAYA_VISION_GPU_GIB = 1.3    # the image encoder on the GPU (mmproj_device = gpu)


_GIB = 1024 ** 3


def maya_needed_message(model_path: str = "") -> str:
    name = Path(model_path).stem if model_path else "This model"
    return (f"{name} is made for the Maya engine, which isn't installed. llama.cpp can open "
            f"this file but can't run it correctly. Install Maya "
            f"(github.com/alesha-pro/maya-inkling-mini), or pick a llama.cpp copy of the model.")


def _maya_weights_gib(model_path: str, meta: dict) -> float:
    """VRAM the weights take: the file minus the token embedding, which the
    engine keeps on the host (Q8_0 blocks: 34 bytes per 32 values)."""
    keys = meta.get("keys") or {}
    arch = str(meta.get("architecture") or "")
    vocab = keys.get(f"{arch}.vocab_size") or 0
    embd = int(meta.get("embedding_length") or 0) * int(vocab if isinstance(vocab, (int, float)) else 0) * 34 / 32
    try:
        size = os.path.getsize(model_path)
    except OSError:
        size = 0
    return max(size - embd, 0) / _GIB


def maya_fit(model_path: str, free_gib: float, drafter: str = "", vision_gpu: bool = False) -> dict:
    """The largest contexts that keep every expert resident in `free_gib` of
    VRAM: {plain_ctx, draft_ctx (0 without a drafter), kv_bytes_per_token,
    trained_ctx}. Multiples of 8192, capped at the trained context. Measured on
    a 4090 with 22.5 GiB free: 262k plain and 192k with the drafter ran;
    262k with the drafter overflowed and Windows paged it to RAM."""
    meta = _maya_gguf_meta(model_path)
    per_tok = _maya_kv_bytes_per_token(meta)
    room = (float(free_gib or 0) - _maya_weights_gib(model_path, meta) - _MAYA_FIXED_GIB - _MAYA_MARGIN_GIB
            - (_MAYA_VISION_GPU_GIB if vision_gpu else 0.0))
    trained = int(meta.get("context_length") or 0) or 1_048_576

    def ctx_for(gib: float) -> int:
        return max(0, min(trained, int(gib * _GIB / per_tok) // 8192 * 8192))

    draft = 0
    if drafter:
        try:
            draft_gib = os.path.getsize(drafter) / _GIB
        except OSError:
            draft_gib = 0.6
        draft = ctx_for(room - draft_gib - _MAYA_DRAFT_EXTRA_GIB - 4096 * _MAYA_DRAFT_CTX / _GIB)
    return {"plain_ctx": ctx_for(room), "draft_ctx": draft, "kv_bytes_per_token": per_tok,
            "trained_ctx": trained}


def _maya_vision_on_gpu(settings: dict) -> bool:
    """The image encoder follows the Projector device setting (CPU by default)."""
    return str((settings or {}).get("mmproj_device") or "cpu").strip().lower() == "gpu"


def _maya_tokenizer_dir(engine: dict, model_path: str, log=None) -> str:
    """The tokenizer files maya's server reads, made once per model file by the
    checkout's tools/strata_tokenizer.py and kept under data/maya/tokenizers."""
    st = os.stat(model_path)
    tag = hashlib.sha1(f"{os.path.normcase(os.path.abspath(model_path))}|{st.st_size}|{int(st.st_mtime)}"
                       .encode("utf-8")).hexdigest()[:12]
    out_dir = DATA / "maya" / "tokenizers" / f"{Path(model_path).stem}-{tag}"
    tok = out_dir / "tokenizer"
    if (tok / "tokenizer.json").is_file():
        return str(tok)
    out_dir.mkdir(parents=True, exist_ok=True)
    tok_args = ["--gguf", model_path, "--out", str(out_dir)]
    if Path(engine.get("chat_template") or "").is_file():
        tok_args += ["--chat-template", engine["chat_template"]]
    tok_args += ["--check"]
    cmd = _maya_python_cmd(engine, engine["tokenizer_tool"], *tok_args)
    r = subprocess.run(cmd, cwd=engine["root"], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=600,
                       env=_child_env({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
                                       "PYTHONPATH": engine.get("pythonpath") or None}),
                       creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    if log:
        for line in ((r.stdout or "") + (r.stderr or "")).splitlines()[-8:]:
            log(f"[maya] {line}")
    if r.returncode != 0 or not (tok / "tokenizer.json").is_file():
        raise RuntimeError(f"maya couldn't read the model's tokenizer (exit code {r.returncode})")
    return str(tok)


def _trim_maya_engine_memory(server_pid: int) -> float:
    """Hand the maya engine's copy of the model file back to Windows once every
    expert is in VRAM. The engine reads the GGUF through a file mapping, and
    those pages stayed in its working set: 17.2 GB on a 32 GB test PC, 2.4 GB
    of 31 left free. Trimming moved them to the standby list (19.2 GB free) and
    left speed unchanged (133 tok/s writing, 3,800 reading). Only maya-ik2: the
    image encoder uses its memory. Returns GB released; 0 off Windows."""
    if os.name != "nt" or not server_pid:
        return 0.0
    released = 0.0
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    for pid in _process_children(server_pid):
        h = k32.OpenProcess(0x0100 | 0x1000, False, pid)   # SET_QUOTA | QUERY_LIMITED_INFORMATION
        if not h:
            continue
        try:
            name = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_ulong(len(name))
            if not k32.QueryFullProcessImageNameW(h, 0, name, ctypes.byref(size)):
                continue
            if not os.path.basename(name.value).lower().startswith("maya-ik2"):
                continue

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            def working_set() -> int:
                pmc = _PMC()
                pmc.cb = ctypes.sizeof(_PMC)
                return pmc.WorkingSetSize if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb) else 0

            before = working_set()
            if psapi.EmptyWorkingSet(h):
                released += max(before - working_set(), 0) / _GIB
        finally:
            k32.CloseHandle(h)
    return released
