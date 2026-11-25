# SPDX-License-Identifier: AGPL-3.0-only
"""Helpers for launching llama-server: no-mmap and projector-on-CPU flags, the load-time
speed probe, stamping a proven boot, and the speculative strategy in use.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.model_config import MODELS_CONFIG_FILE
from accuretta.runtime.model_config import _models_config


# ---- load-time speed check (dense models) -----------------------------------
# On Windows a dense model whose context doesn't quite fit in VRAM still loads:
# the driver moves part of it into system RAM and decoding slows down with no
# error. Memory counters can't tell that apart reliably; speed can. Right after
# each load a short fixed generation is timed (temperature 0, fresh server) and
# the advisor compares those numbers across context sizes of the same setup.
# Validated on a 4090 with Qwen3.8-27B: healthy loads 82.4-83.2 tok/s at
# 100k-147k context, spilled loads 70.1 and 30.7 tok/s at 160k.
_LOAD_PROBE_PROMPT = ("Write one paragraph, about 120 words, describing an old "
                      "lighthouse on a rocky coast.")


_LOAD_PROBE_TOKENS = 96


def _measure_load_speed(base_url: str) -> float:
    """Decode tok/s of a short fixed generation; 0.0 when it can't be measured."""
    try:
        req = urllib.request.Request(
            f"{base_url}/v1/chat/completions", method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({
                "model": "local", "stream": False, "max_tokens": _LOAD_PROBE_TOKENS,
                "temperature": 0, "seed": 1, "cache_prompt": False,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": _LOAD_PROBE_PROMPT}],
            }).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=60) as resp:
            timings = json.loads(resp.read()).get("timings") or {}
        if int(timings.get("predicted_n") or 0) < _LOAD_PROBE_TOKENS // 2:
            return 0.0
        return round(float(timings.get("predicted_per_second") or 0.0), 1)
    except Exception:
        return 0.0


def _mmproj_on_cpu(caps: dict) -> bool:
    """Run the vision projector on the CPU whenever the build can.

    It only runs when an image arrives. Measured on a 4090 with Qwen3.8-27B
    (F16 mmproj, ~900-token screenshots): CPU frees ~1.1 GB of VRAM (~30k more
    context) for ~7 s more per image; text decode speed is unchanged. The spawn
    and the tuner both ask here, so the VRAM the tuner reserves always matches
    where the projector actually runs. Settings "mmproj_device": "gpu" keeps it
    on the GPU for setups with VRAM to spare (a second card, say)."""
    try:
        if str(get_settings().get("mmproj_device") or "cpu").strip().lower() == "gpu":
            return False
    except Exception:
        pass
    return bool((caps or {}).get("no_mmproj_offload"))


def _runtime_spec_strategy(spec_strategy: str, mmproj_path: str) -> str:
    """Keep the chosen decoder loaded alongside native multimodal support."""
    return str(spec_strategy or "off").strip().lower()


def _no_mmap_flags(caps: dict, ngl: int, n_cpu_moe: int, extra_args: str = "",
                   cpu_share_gb: float = 0.0, ram_free_gb: float = 0.0) -> list[str]:
    """Load the weights without keeping a memory-mapped copy in RAM.

    By default llama.cpp maps the .gguf into memory, copies the weights to the
    GPU, and the mapped pages stay in its working set: measured 12.6 GB of a
    16.35 GB Qwen3.8 quant held in RAM while every layer ran on the GPU, with
    the system at 0.7 GB free. When the whole model is on the GPU nothing reads
    those pages again, so load without mapping and the RAM is free after
    loading (the load itself is a little slower).

    With experts offloaded to the CPU the same happens to every page read
    while the GPU's share uploads: Inkling 2 Mini with 4 of 24 expert layers
    on the CPU held 15.5 GB of RAM for ~3 GB of CPU weights, and llama.cpp
    itself suggests --load-mode none for tensor overrides. So unmap there too
    when the CPU share (`cpu_share_gb`) is small next to free RAM
    (`ram_free_gb`); a large share keeps the map, which lets Windows page it
    instead of running out. Dense partial offload keeps the map. A loading
    flag the user put in extra args always wins."""
    if ngl != -1:
        return []
    if n_cpu_moe > 0 and not (0 < cpu_share_gb and cpu_share_gb + 2.0 <= ram_free_gb * 0.5):
        return []
    low = f" {extra_args or ''} ".lower()
    if any(f in low for f in (" --load-mode", " -lm ", " --no-mmap", " --mmap", " --mlock")):
        return []
    if caps.get("load_mode"):
        return ["--load-mode", "none"]
    if caps.get("no_mmap"):
        return ["--no-mmap"]
    return []


def _stamp_proven_boot(model_path: str, ctx: int) -> None:
    """Record that this model booted successfully on this machine at this ctx.

    The tuner's VRAM budget is an estimate; llama-server starting and answering
    is proof it fits. Boot-time retunes use that proof so grow-only can beat the
    estimate instead of clamping a working config back down."""
    if not model_path or not ctx or ctx <= 0:
        return
    try:
        allc = _models_config()
        ent = allc.get(model_path)
        ent = dict(ent) if isinstance(ent, dict) else {}
        if int(ent.get("proven_ctx") or 0) == int(ctx) and ent.get("booted_at"):
            return                       # nothing to learn — no disk churn per reload
        ent["proven_ctx"] = int(ctx)
        ent["booted_at"] = int(time.time())
        allc[model_path] = ent
        save_json(MODELS_CONFIG_FILE, allc)
    except Exception:
        pass
