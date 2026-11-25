# SPDX-License-Identifier: AGPL-3.0-only
"""Finding the GPUs: VRAM per card, memory bandwidth estimates, and the cards the tuner
works with.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from accuretta.core.settings import get_settings
from accuretta.security.child_env import _child_env


def detect_vram_gb() -> dict:
    """Best-effort GPU VRAM probe. Returns {gb, name, source, total_gb, gpus} or
    {gb: 0, name: "", source: "none"}. `gb` is GPU 0's free VRAM (what the tuner
    can budget, since other processes' allocations are otherwise invisible),
    `total_gb` its physical total, and `gpus` lists every GPU as
    {name, total_gb, free_gb, temp_c}. `temp_c` (GPU 0's core temperature in
    °C, for the status bar) rides along in the same query so the driver is
    asked once; it is left out when the card doesn't report one. Uses
    nvidia-smi when present. Never raises."""
    out = {"gb": 0, "name": "", "source": "none"}
    # nvidia-smi --query-gpu=memory.total,memory.free,temperature.gpu,name --format=csv,noheader,nounits
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run(
                [smi, "--query-gpu=memory.total,memory.free,temperature.gpu,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, env=_child_env(keep=("gpu",)),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                # One line per GPU: "12282, 11001, 54, NVIDIA GeForce RTX 4070".
                # Recent drivers print the temperature with decimals ("34.28"),
                # cards without a sensor print "[N/A]"; the name goes last
                # because it may itself contain commas.
                gpus = []
                for line in r.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                        continue
                    temp = None
                    rest = parts[2:]
                    if len(rest) > 1:
                        if re.fullmatch(r"\d+(?:\.\d+)?", rest[0]):
                            temp = int(round(float(rest[0])))
                            rest = rest[1:]
                        elif rest[0].strip("[]").lower() in ("n/a", "not supported", "unknown error"):
                            rest = rest[1:]
                    gpu = {
                        "name": ", ".join(rest) if rest else "NVIDIA GPU",
                        "total_gb": round(int(parts[0]) / 1024, 1),
                        "free_gb": round(int(parts[1]) / 1024, 1),
                    }
                    if temp is not None:
                        gpu["temp_c"] = temp
                    gpus.append(gpu)
                if gpus:
                    # GPU 0 = primary. `gb` is FREE so the tuner never budgets
                    # VRAM another process already holds. `free_total_gb` sums
                    # every GPU: llama.cpp splits layers across all visible
                    # cards, so the tuner budgets all of them.
                    out["gb"] = gpus[0]["free_gb"]
                    out["free_total_gb"] = round(sum(g["free_gb"] for g in gpus), 1)
                    out["total_gb"] = gpus[0]["total_gb"]
                    out["name"] = gpus[0]["name"]
                    if "temp_c" in gpus[0]:
                        out["temp_c"] = gpus[0]["temp_c"]
                    out["gpus"] = gpus
                    out["source"] = "nvidia-smi"
                    return out
        except Exception:
            pass
    return out


# Published memory bandwidth (GB/s). First match wins, so within a family the
# longer name comes first ("4070 ti super" before "4070 ti" before "4070").
_GPU_BANDWIDTH_GBPS = (
    ("rtx pro 6000", 1792), ("5090", 1792), ("5080", 960), ("5070 ti", 896), ("5070", 672),
    ("5060 ti", 448), ("5060", 448),
    ("4090", 1008), ("4080 super", 736), ("4080", 717), ("4070 ti super", 672),
    ("4070 ti", 504), ("4070 super", 504), ("4070", 504), ("4060 ti", 288), ("4060", 272),
    ("3090 ti", 1008), ("3090", 936), ("3080 ti", 912), ("3080", 760), ("3070 ti", 608),
    ("3070", 448), ("3060 ti", 448), ("3060", 360),
    ("2080 ti", 616), ("2080 super", 496), ("2080", 448), ("2070", 448), ("2060", 336),
    ("titan rtx", 672), ("6000 ada", 960), ("a6000", 768), ("a5000", 768), ("a4500", 640),
    ("a4000", 448), ("l40s", 864), ("l40", 864), ("a100", 1555), ("h200", 4800), ("h100", 2000),
    ("r9700", 640), ("7900 xtx", 960), ("7900 xt", 800), ("9070 xt", 640), ("9070", 640),
)


def _gpu_bandwidth_gbps(name: str, total_gb: float) -> tuple[float, str]:
    """(GB/s, source) for a GPU: published figure by name, else a conservative
    guess from its VRAM size. Laptop parts run well below their desktop names."""
    n = " ".join(str(name or "").lower().split())
    for key, gbps in _GPU_BANDWIDTH_GBPS:
        if key in n:
            if "laptop" in n or "mobile" in n:
                return gbps * 0.6, f"{key} laptop (est.)"
            return float(gbps), key
    gb = float(total_gb or 0)
    guess = 900.0 if gb >= 23 else 600.0 if gb >= 15 else 450.0 if gb >= 11 else 300.0 if gb >= 7 else 200.0
    return guess, f"unknown GPU, {gb:.0f} GB class (est.)"


def _tune_card_gb(vram_gb: float) -> float:
    """Size of the GPU(s) being tuned for: the VRAM tier when the user set one
    (it caps the card), else the detected physical total, else `vram_gb`."""
    try:
        tier = float(get_settings().get("vram_tier_gb") or 0)
    except (TypeError, ValueError):
        tier = 0.0
    if tier > 0:
        return tier
    try:
        totals = [float(g.get("total_gb") or 0) for g in (detect_vram_gb().get("gpus") or [])]
    except Exception:
        totals = []
    total = round(sum(totals), 1)
    return total if total > 0 else float(vram_gb or 0)


def _tune_gpus(vram_gb: float) -> list[dict]:
    """Detected GPUs as [{name, total_gb, free_gb, bw_gbps, bw_src}]. Falls back
    to one GPU of `vram_gb` when nvidia-smi isn't available."""
    det = detect_vram_gb()
    gpus = []
    for g in det.get("gpus") or []:
        bw, src = _gpu_bandwidth_gbps(g.get("name", ""), g.get("total_gb", 0))
        gpus.append({**g, "bw_gbps": bw, "bw_src": src})
    if not gpus:
        bw, src = _gpu_bandwidth_gbps("", vram_gb)
        gpus = [{"name": "GPU", "total_gb": vram_gb, "free_gb": vram_gb, "bw_gbps": bw, "bw_src": src}]
    return gpus


def detect_hardware_specs() -> dict:
    """Detect GPU hardware availability on Windows to suggest the best build."""
    gpu_name = "Generic CPU"
    recommended_build = "CPU" # CPU, Vulkan, CUDA
    is_nvidia = False

    try:
        # Check via nvidia-smi command first
        res = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=2,
                             env=_child_env(keep=("gpu",)))
        if res.returncode == 0 and res.stdout.strip():
            gpu_name = res.stdout.strip().split("\n")[0]
            recommended_build = "CUDA"
            is_nvidia = True
    except Exception:
        pass

    if not is_nvidia:
        try:
            # Query via WMI on Windows
            res = subprocess.run(["wmic", "path", "win32_VideoController", "get", "name"],
                                 capture_output=True, text=True, timeout=2, env=_child_env())
            if res.returncode == 0 and res.stdout.strip():
                lines = [l.strip() for l in res.stdout.strip().split("\n")[1:] if l.strip()]
                if lines:
                    gpu_name = lines[0]
                    # AMD, Nvidia or Intel GPUs get Vulkan
                    lower_name = gpu_name.lower()
                    if "nvidia" in lower_name:
                        recommended_build = "CUDA"
                    elif any(kw in lower_name for kw in ["amd", "radeon", "intel", "arc"]):
                        recommended_build = "Vulkan"
        except Exception:
            pass

    return {
        "gpu_name": gpu_name,
        "recommended_build": recommended_build
    }
