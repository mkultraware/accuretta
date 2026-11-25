# SPDX-License-Identifier: AGPL-3.0-only
"""The auto-tuner: choosing context, KV cache type, GPU layers, expert offload, batch
and speculation for a model on this machine, from its metadata, VRAM and measured speed.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.gpu_detect import _tune_card_gb
from accuretta.runtime.gpu_detect import _tune_gpus
from accuretta.runtime.gpu_detect import detect_vram_gb
from accuretta.runtime.kv_sizing import _decode_bytes_per_token
from accuretta.runtime.kv_sizing import _hybrid_info
from accuretta.runtime.kv_sizing import _kv_total_bytes
from accuretta.runtime.kv_sizing import _kv_total_mb
from accuretta.runtime.kv_sizing import _swa_info
from accuretta.runtime.launch_helpers import _mmproj_on_cpu
from accuretta.runtime.llama_bin import _fit_params_bin
from accuretta.runtime.llama_bin import find_llama_bin
from accuretta.runtime.llama_caps import llama_caps
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.maya import _maya_drafter_for
from accuretta.runtime.maya import _maya_kv_bytes_per_token
from accuretta.runtime.maya import _process_children
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.maya_find import _maya_gguf_meta
from accuretta.runtime.maya_sizing import _GIB
from accuretta.runtime.maya_sizing import _MAYA_DRAFT_CTX
from accuretta.runtime.maya_sizing import _MAYA_DRAFT_EXTRA_GIB
from accuretta.runtime.maya_sizing import _MAYA_FIXED_GIB
from accuretta.runtime.maya_sizing import _MAYA_VISION_GPU_GIB
from accuretta.runtime.maya_sizing import _maya_vision_on_gpu
from accuretta.runtime.maya_sizing import _maya_weights_gib
from accuretta.runtime.maya_sizing import maya_fit
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _FIT_CTX_CAP
from accuretta.runtime.model_advisor import _fit_ceiling_for
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_config import _models_config
from accuretta.runtime.model_files import _resolve_mmproj_for_tune
from accuretta.runtime.model_files import _resolve_spec_draft_for_tune
from accuretta.runtime.model_files import _spec_fingerprint
from accuretta.runtime.model_files import find_spec_draft_for
from accuretta.runtime.model_inspect import inspect_model
from accuretta.runtime.system_ram import _system_ram_info
from accuretta.runtime.tuning_math import _layer_passes
from accuretta.runtime.tuning_math import _mla_dims
from accuretta.runtime.tuning_math import _probe_ram_bandwidth_gbps
from accuretta.runtime.tuning_math import _speculative_choice
from accuretta.security.child_env import _child_env
from pathlib import Path


_AUTOTUNE_KV_CHOICES = {"auto", "q8_0", "q4_0", "f16"}


# ---- quant byte costs -------------------------------------------------------
# Average bytes per weight for common GGUF quants (post block-quant overhead).
# Used by the MoE offload speed model to turn "expert layers on CPU" into
# "bytes pulled through RAM per decoded token".
_QUANT_BPW = (
    ("IQ2", 0.35), ("Q2", 0.35),
    ("IQ3", 0.44), ("Q3", 0.44),
    ("IQ4", 0.58), ("Q4", 0.60),
    ("IQ5", 0.70), ("Q5", 0.72),
    ("Q6", 0.85),
    ("Q8", 1.06),
    ("F16", 2.0), ("BF16", 2.0), ("F32", 4.0),
)


def _quant_bpw(quant: str) -> float:
    q = (quant or "").upper()
    for prefix, bpw in _QUANT_BPW:
        if q.startswith(prefix):
            return bpw
    return 0.60  # Q4-ish default — the most common quant class


def _running_instance_mem_mb() -> tuple[float, float]:
    """(ram_mb, vram_mb) held by the llama-server process this bridge most
    recently spawned; (0, 0) when nothing runs or the probe fails.

    Tuning for a model swap must not count the old instance's memory as used by
    someone else, since the swap frees it. Without the add-back, tuning while a
    big MoE is loaded sees ~2 GB free RAM and writes crippled configs (ctx 4096,
    too small for the system prompt + tools)."""
    pid = 0
    tree = False
    try:
        proc = getattr(_llama, "_proc", None)  # defined further down; call-time resolved
        if proc is not None and proc.poll() is None:
            pid = proc.pid
            tree = bool(getattr(proc, "_accuretta_tree", False))
    except Exception:
        pid = 0
    if not pid:
        return 0.0, 0.0
    # maya's server holds no VRAM itself: its engine child does (and the image
    # encoder child holds RAM). Count the whole tree, or a retune while maya runs
    # sees a full card and shrinks the context.
    pids = [pid] + (_process_children(pid) if tree else [])
    ram_mb = 0.0
    try:
        if os.name == "nt":
            import ctypes

            class _PMC(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            for one in pids:
                h = ctypes.windll.kernel32.OpenProcess(0x1000, False, one)  # PROCESS_QUERY_LIMITED_INFORMATION
                if h:
                    pmc = _PMC()
                    pmc.cb = ctypes.sizeof(_PMC)
                    if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                        ram_mb += pmc.WorkingSetSize / (1024 * 1024)
                    ctypes.windll.kernel32.CloseHandle(h)
        else:
            for one in pids:
                with open(f"/proc/{one}/status", "r") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            ram_mb += float(line.split()[1]) / 1024.0
                            break
    except Exception:
        ram_mb = 0.0
    vram_mb = 0.0
    try:
        smi = shutil.which("nvidia-smi")
        if smi:
            r = subprocess.run(
                [smi, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3, env=_child_env(keep=("gpu",)),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                wanted = {str(one) for one in pids}
                for line in r.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) == 2 and parts[0] in wanted and parts[1].isdigit():
                        vram_mb += float(parts[1])
    except Exception:
        vram_mb = 0.0
    if vram_mb <= 0 and os.name == "nt":
        # Under WDDM nvidia-smi reports every process's memory as "[N/A]", so the
        # add-back above reads 0; Windows' own per-process counter (what Task Manager
        # shows) has the real figure.
        vram_mb = _win_gpu_process_mem_mb(pid) if len(pids) == 1 else _win_gpu_tree_mem_mb(pids)
    return ram_mb, vram_mb


def _win_gpu_tree_mem_mb(pids: list[int]) -> float:
    """_win_gpu_process_mem_mb for several processes in one counter query."""
    if os.name != "nt" or not pids:
        return 0.0
    key = tuple(sorted(int(p) for p in pids))
    hit = _WIN_GPU_MEM_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < 10.0:
        return hit[1]
    pattern = "^pid_(" + "|".join(str(p) for p in key) + ")_"
    script = (f"$s=(Get-Counter '\\GPU Process Memory(*)\\Dedicated Usage' -ErrorAction SilentlyContinue)"
              f".CounterSamples | Where-Object {{ $_.InstanceName -match '{pattern}' }}; "
              f"[math]::Round(($s | Measure-Object CookedValue -Sum).Sum/1MB)")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=15, env=_child_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        value = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        mb = float(value) if value.replace(".", "", 1).isdigit() else 0.0
    except Exception:
        mb = 0.0
    _WIN_GPU_MEM_CACHE[key] = (time.monotonic(), mb)
    return mb


def _win_gpu_process_mem_mb(pid: int) -> float:
    """Dedicated GPU memory (MiB) a process holds, from Windows' GPU Process
    Memory performance counter. 0 when unavailable. ~1.5 s (PowerShell)."""
    if os.name != "nt" or not pid:
        return 0.0
    hit = _WIN_GPU_MEM_CACHE.get(pid)
    if hit and time.monotonic() - hit[0] < 10.0:    # one tune asks several times
        return hit[1]
    script = (f"$s=(Get-Counter '\\GPU Process Memory(pid_{int(pid)}_*)\\Dedicated Usage' "
              f"-ErrorAction SilentlyContinue).CounterSamples; "
              f"[math]::Round(($s | Measure-Object CookedValue -Sum).Sum/1MB)")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=15, env=_child_env(),
                           creationflags=subprocess.CREATE_NO_WINDOW)
        value = (r.stdout or "").strip().splitlines()[-1] if (r.stdout or "").strip() else ""
        mb = float(value) if value.replace(".", "", 1).isdigit() else 0.0
    except Exception:
        mb = 0.0
    _WIN_GPU_MEM_CACHE[pid] = (time.monotonic(), mb)
    return mb


_WIN_GPU_MEM_CACHE: dict = {}


def _free_vram_gb_for_tune() -> float:
    """Free VRAM for a tune that precedes a model load/swap: adds back the VRAM
    the running instance holds, which the swap frees."""
    det = detect_vram_gb()
    gb = float(det.get("free_total_gb") or det.get("gb") or 0)
    try:
        _ram_mb, vram_mb = _running_instance_mem_mb()
        if vram_mb > 0:
            gb += vram_mb / 1024.0
    except Exception:
        pass
    return round(gb, 2)


def _ram_bandwidth_gbps() -> float:
    """Resolve the RAM bandwidth for the MoE speed model. A raw settings value
    OTHER than the auto sentinel (30) is a manual override and wins. Otherwise
    use the cached probe; probe once and cache if missing; 30 GB/s fallback."""
    raw = load_json(SETTINGS_FILE, {})
    if isinstance(raw, dict):
        v = raw.get("ram_bandwidth_gbps")
        if isinstance(v, (int, float)) and v > 0 and v != 30:
            return float(v)
        v = raw.get("_ram_bw_probe_gbps")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    bw = _probe_ram_bandwidth_gbps()
    if bw > 0:
        try:
            # Merge into the raw file (not get_settings()) so a concurrent
            # settings write can't be clobbered.
            raw = load_json(SETTINGS_FILE, {})
            if isinstance(raw, dict):
                raw["_ram_bw_probe_gbps"] = bw
                save_json(SETTINGS_FILE, raw)
        except Exception:
            pass
        return bw
    return 30.0


# Share of peak bandwidth llama.cpp decode reaches before calibration kicks in.
_DECODE_BW_EFFICIENCY = 0.72


# CPU-side expert/layer math runs well below a memcpy probe's bandwidth.
# Fitted to a measured run: a Q8 35B-A3B with 23-25 of 41 expert layers on CPU
# decodes ~45-51 tok/s on a 4090 + 82 GB/s RAM.
_CPU_DECODE_EFFICIENCY = 0.4


# Speculative drafts barely help MoE: each drafted token routes to different
# experts, so verifying a draft reads more expert weights.
_SPEC_GAIN_MOE = 1.05


# Speedup each speculative strategy is assumed to give before this model has
# measured turns (calibration then absorbs the real figure). draft-mtp's is at
# draft length 3; other lengths scale by _MTP_LEN_FACTOR, measured back to back
# on a 4090 / 27B Q4 (1 → 70, 2 → 88, 3 → 93, 4 → 92 tok/s).
_SPEC_GAIN_DEFAULT = {"off": 1.0, "ngram-mod": 1.0, "draft-mtp": 1.5, "dflash": 1.8, "dspark": 1.8}


_MTP_LEN_FACTOR = {1: 0.75, 2: 0.94, 3: 1.0, 4: 0.99, 5: 0.97, 6: 0.95}


def _typical_fill_tokens(model_path: str) -> tuple[int, str]:
    """How full this user's chats actually get: the 90th-percentile peak
    prompt size of recorded turns on this model, else across every model,
    else 32k. The speed floor is judged here, because a bigger context window
    costs no decode speed until it's filled — only its VRAM (which on MoE
    pushes experts to RAM) slows every reply."""
    def p90(vals: list[int]) -> int:
        vals = sorted(vals)
        return vals[min(len(vals) - 1, int(len(vals) * 0.9))]
    try:
        models = (load_json(MODEL_RUNTIME_FILE, {}) or {}).get("models") or {}
    except Exception:
        models = {}
    mine_keys = {_model_profile_key(model_path), Path(str(model_path)).stem}
    mine, everyone = [], []
    for key, row in models.items():
        for cfg in (row.get("configs") or {}).values():
            for o in cfg.get("observations") or []:
                peak = int(o.get("peak_context_tokens") or 0)
                if peak > 0:
                    everyone.append(peak)
                    if key in mine_keys:
                        mine.append(peak)
    if len(mine) >= 5:
        return max(8192, p90(mine)), f"your {len(mine)} recorded turns on this model"
    if len(everyone) >= 5:
        return max(8192, p90(everyone)), "your recorded turns across all models"
    return 32768, "default — no recorded turns yet"


def _effective_gpu_bw(gpus: list[dict]) -> float:
    """Layer split runs the GPUs one after another per token, each over its
    share of the weights (llama.cpp splits by free memory) — so the combined
    rate is the share-weighted harmonic mean of their bandwidths."""
    weights = [max(float(g.get("free_gb") or g.get("total_gb") or 0), 0.1) for g in gpus]
    total = sum(weights)
    inv = sum((w / total) / max(float(g["bw_gbps"]), 1.0) for w, g in zip(weights, gpus))
    return 1.0 / inv if inv > 0 else 300.0


def _decode_tok_s(gpu_bytes: float, cpu_bytes: float, *, gpu_bw_gbps: float, ram_bw_gbps: float,
                  spec_gain: float = 1.0, calib: float = 1.0) -> float:
    t = gpu_bytes / (max(gpu_bw_gbps, 1.0) * 1e9 * _DECODE_BW_EFFICIENCY)
    if cpu_bytes > 0:
        t += cpu_bytes / (max(ram_bw_gbps, 1.0) * 1e9 * _CPU_DECODE_EFFICIENCY)
    return (spec_gain * calib / t) if t > 0 else float("inf")


def _decode_observations(model_path: str) -> list[tuple[dict, dict]]:
    """(config settings, observation) pairs with enough output to time decode,
    newest last, for one model from the passive telemetry file."""
    try:
        data = load_json(MODEL_RUNTIME_FILE, {})
        models = (data or {}).get("models") or {}
        # Turns land under the chat's model name — usually the file stem ("X"
        # for X.gguf) — and some callers record the full file name. Read both.
        rows = [models.get(k) or {} for k in
                dict.fromkeys((_model_profile_key(model_path), Path(str(model_path)).stem))]
    except Exception:
        return []
    pairs = []
    for cfg in [c for row in rows for c in (row.get("configs") or {}).values()]:
        settings = cfg.get("settings") or {}
        for o in cfg.get("observations") or []:
            if int(o.get("output_tokens") or 0) >= 64 and float(o.get("eval_ms") or 0) > 0:
                pairs.append((settings, o))
    pairs.sort(key=lambda p: p[1].get("ts", 0))
    return pairs[-80:]


def _decode_calibration(model_path: str, predict) -> tuple[float, int]:
    """Median of measured ÷ predicted decode speed over this model's recorded
    turns (each predicted for its own config and context fill). (1.0, 0) until
    at least 5 usable turns exist. Clamped so one odd week can't run away."""
    ratios = []
    for settings, o in _decode_observations(model_path):
        try:
            measured = float(o["output_tokens"]) / (float(o["eval_ms"]) / 1000.0)
            predicted = predict(settings, int(o.get("peak_context_tokens") or 0))
        except Exception:
            continue
        if predicted and predicted != float("inf") and measured > 0:
            ratios.append(min(max(measured / predicted, 0.2), 5.0))
    if len(ratios) < 5:
        return 1.0, len(ratios)
    ratios.sort()
    mid = len(ratios) // 2
    med = ratios[mid] if len(ratios) % 2 else (ratios[mid - 1] + ratios[mid]) / 2
    return min(max(med, 0.4), 2.5), len(ratios)


def _draft_length_choice(model_path: str, current: int) -> tuple[int, str]:
    """MTP draft length from measured acceptance at the current length. One
    step per tune, toward whichever side the acceptance rate points:
    measured on a 4090 / 27B, 3 drafts at ~62% acceptance beat 1 (83%) by
    33% and 4 (50%) by 1%. Needs >= 200 drafted tokens at that length."""
    cur = current if current > 0 else 3
    drafted = accepted = 0
    for settings, o in _decode_observations(model_path):
        ran_n = int(settings.get("spec_draft_n_max") or settings.get("spec_draft_n_eff") or 0)
        if ran_n != cur or settings.get("spec_strategy") != "draft-mtp":
            continue
        drafted += int(o.get("draft_n") or 0)
        accepted += int(o.get("draft_accepted") or 0)
    if drafted < 200:
        return cur, (f"draft length {cur} (default; acceptance not measured yet)" if cur == 3
                     else f"draft length {cur} (kept; {drafted} drafted tokens measured, need 200)")
    rate = accepted / drafted
    nxt = cur
    if rate < 0.45 and cur > 1:
        nxt = cur - 1
    elif rate > 0.72 and cur < 6:
        nxt = cur + 1
    verdict = "kept" if nxt == cur else f"→ {nxt}"
    return nxt, f"draft length {cur} {verdict} (measured acceptance {rate:.0%} over {drafted:,} drafted tokens)"


# -- exact fit via llama-fit-params --------------------------------------------
_FIT_PARAMS_CACHE: dict = {}


def _fit_params_measure(model_path: str, ctx: int, *, ngl: int, n_cpu_moe: int, kv_k: str,
                        kv_v: str, ubatch: int, batch: int, flash: bool) -> dict | None:
    """llama.cpp's own memory estimate for one configuration, without loading
    the model: {"devices": {name: total MiB}, "ctx": {name: context MiB},
    "compute": {name: compute MiB}, "host": MiB}. None when the tool is
    missing or the run fails. ~0.6 s."""
    exe = _fit_params_bin()
    if not exe or not model_path:
        return None
    args = ["-m", model_path, "-c", str(int(ctx)), "-ngl", str(int(ngl) if ngl < 99 else 999),
            "-ctk", kv_k or "f16", "-ctv", kv_v or kv_k or "f16",
            "-fa", "on" if flash else "off", "-ub", str(int(ubatch)), "-b", str(int(batch)),
            "-fit", "off", "-fitp", "on"]
    if n_cpu_moe > 0:
        args += ["--n-cpu-moe", str(int(n_cpu_moe))]
    try:
        key = (exe, os.path.getmtime(model_path), tuple(args))
    except Exception:
        key = (exe, 0, tuple(args))
    if key in _FIT_PARAMS_CACHE:
        return _FIT_PARAMS_CACHE[key]
    result = None
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, timeout=60,
                           env=_child_env(keep=("gpu",)),
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        devices, ctx_mib, comp_mib, host = {}, {}, {}, 0.0
        for line in (r.stdout or "").splitlines():
            m = re.fullmatch(r"\s*(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s*", line)
            if not m:
                continue
            name, model_mib, context_mib, compute_mib = m.group(1), *map(float, m.groups()[1:])
            if name.lower().startswith("host"):
                host = model_mib + context_mib + compute_mib
            else:
                devices[name] = model_mib + context_mib + compute_mib
                ctx_mib[name] = context_mib
                comp_mib[name] = compute_mib
        if devices:
            result = {"devices": devices, "ctx": ctx_mib, "compute": comp_mib, "host": host}
    except Exception:
        result = None
    _FIT_PARAMS_CACHE[key] = result
    return result


def auto_tune(model_path: str, vram_gb: float, profile: dict = None, min_ctx: int = 0,
              proven_cfg: dict = None) -> dict:
    """Suggest llama-server flags for a (model, VRAM) pair.

    Uses GGUF header metadata (layer count, expert count, GQA config) when
    available for accurate KV cache + MoE expert offload math. Falls back to
    size-bucket heuristics when the header is unreadable. Returns the same
    keys the Settings drawer uses plus `notes` (multi-line reasoning) and
    `quant_downshift` (banner shown when the model needs heavy offload).
    `profile` may be passed in to reuse an existing inspect_model() result.

    The (ctx, n_cpu_moe) pair is chosen by a joint optimizer: KV cache is
    priced at the EXACT context of each tier (sliding-window layers cap at
    their window, MLA caches latents, hybrid SSMs scale by attention-layer
    fraction), and expert offload is gated by a decode-speed model (RAM
    bandwidth ÷ active expert bytes per token) instead of a fixed layer cap —
    so both dense and MoE models get the largest context that still decodes
    at a usable speed. Floors/overrides: `min_tok_s_estimate`,
    `ram_bandwidth_gbps` in settings.

    `min_ctx` makes context grow-only: when it's larger than the picked tier,
    the solver re-runs pinned to the smallest tier >= min_ctx and recomputes
    the offload for THAT ctx. The pinned combo is kept only if it fits within
    the same offload caps the normal solve uses; otherwise the normal pick
    stands and a note explains the saved ctx doesn't fit. Absent/0/smaller
    min_ctx changes nothing.

    `proven_cfg` — a previously STAMPED per-model config whose server boot
    actually succeeded (see _stamp_proven_boot). When its ctx covers min_ctx,
    the budget estimate is outranked: the saved ctx and offload combo are
    honored verbatim, because a config that booted and ran beats any estimate
    of what "should" fit.

    Leaves ~8% VRAM headroom when GGUF math is exact, ~15% when falling back
    to filename heuristics. Disables speculative decoding for MoE because
    independent benchmarks show it's net-negative there.
    """
    if model_path and _maya_engine_for(model_path):
        # maya runs this model (see find_maya_engine): its own memory math.
        return _maya_auto_tune(model_path, vram_gb, min_ctx=min_ctx)
    profile = profile or inspect_model(model_path)
    notes: list[str] = []
    size_gb = profile["size_gb"] or 0.0
    is_moe = profile["is_moe"]
    n_layer = profile.get("block_count", 0) or 0
    src = profile.get("metadata_source", "filename")
    vram = max(float(vram_gb or 0), 0)
    # Card-size decisions (batch, f16 KV, the small-GPU q4 escape) follow the
    # card's size, never how much of it is free right now: callers pass either
    # the VRAM tier or live free VRAM (22.8 GB on a 24 GB card), and one machine
    # must get one answer. `vram` stays the memory budget.
    card = _tune_card_gb(vram)

    # Default flags everyone gets
    out = {
        "num_gpu": 99,                 # all non-MoE layers on GPU
        "n_cpu_moe": 0,                # MoE-only; auto below
        "kv_cache_type": "q8_0",       # K cache — best balance
        "kv_cache_type_v": "",         # V cache — "" = same as K (mirrored at spawn). Tuner never splits
                                       # K/V (measured: q8_0/q4_0 = +1.6% PPL AND slow path on this machine);
                                       # q4_0 only as a ≤12 GB VRAM escape hatch (ladder below)
        "num_ctx": 32768,
        "num_batch": 512,
        "n_ubatch": 0,                 # auto in spawn
        "num_thread": 0,               # auto = let llama-server pick
        "flash_attn": True,
        "spec_strategy": "ngram-mod",   # off | ngram-mod | draft-mtp — set per-model below
        "no_warmup": False,
        "enable_metrics": False,
        "quant_downshift": "",          # banner string; "" = no suggestion
    }

    if vram <= 0:
        out["notes"] = "no VRAM tier set — using safe defaults. set a tier and Suggest again."
        return out
    if size_gb <= 0:
        out["notes"] = "no model selected — pick a model first, then Suggest."
        return out

    # Useable VRAM budget. With exact GGUF-derived KV math we can run hot —
    # ~8% headroom for CUDA workspace + alignment. Filename fallback is fuzzier
    # so we leave more slack. Two SEPARATE reserves:
    #   - vram_reserve_gb: minimal GPU-side safety for the Windows desktop
    #     compositor on single-GPU machines (default 0.25 GB; user-tunable to
    #     0 = give the LLM everything — the headroom still guards CUDA).
    #   - safety_reserve_gb (reserve_gb): SYSTEM RAM only, applied further
    #     down as the floor under CPU offload — the anti-bluescreen guard.
    _s_tune = get_settings()
    try:
        reserve_gb = max(float(_s_tune.get("safety_reserve_gb") or 0), 0.0)
    except Exception:
        reserve_gb = 1.0
    try:
        _vr = _s_tune.get("vram_reserve_gb")
        vram_reserve_gb = max(float(0.25 if _vr is None else _vr), 0.0)
    except Exception:
        vram_reserve_gb = 0.25
    # K / V cache types chosen in the Auto-tune panel ("auto" / "" = tuner's pick).
    kv_pref = str(_s_tune.get("autotune_kv_cache") or "auto").strip().lower()
    if kv_pref not in _AUTOTUNE_KV_CHOICES:
        kv_pref = "auto"
    kv_v_pref = str(_s_tune.get("autotune_kv_cache_v") or "").strip().lower()
    if kv_v_pref not in _AUTOTUNE_KV_CHOICES - {"auto"}:
        kv_v_pref = ""
    headroom = 0.92 if src == "gguf" else 0.85
    budget_mb = max(vram - vram_reserve_gb, 0.5) * headroom * 1024

    # Vision projector reserve: an mmproj loading alongside the model (explicit
    # path or mmproj_auto sibling) takes real VRAM that the KV/weights math must
    # not spend, or tuned loads with vision on can OOM at boot.
    mmproj_mb = 0.0
    _mp_name = ""
    _mp_cpu = False
    try:
        _mp = _resolve_mmproj_for_tune(model_path, _s_tune)
        if _mp:
            _mp_name = os.path.basename(_mp)
            _mp_cpu = _mmproj_on_cpu(llama_caps(find_llama_bin() or ""))
            if not _mp_cpu:
                mmproj_mb = os.path.getsize(_mp) / (1024 * 1024)
                budget_mb = max(budget_mb - mmproj_mb, 256.0)
    except Exception:
        mmproj_mb = 0.0
    # Speculative intent — decided BEFORE the draft reserve so an auto-tuned
    # pick also pays for its draft:
    #   * a manual choice is honored as-is (the advisor must not nag it away);
    #   * otherwise prefer a metadata-validated DFlash/DSpark pair (measured
    #     ~+50% decode over draft-mtp on the reference box);
    #   * otherwise the existing MTP / n-gram / off policy below decides.
    _spec_manual = str(_s_tune.get("spec_strategy_source") or "").strip().lower() == "manual"
    _spec_saved = str(_s_tune.get("spec_strategy") or "").strip().lower()
    _spec_valid = {"off", "ngram-mod", "draft-mtp", "dflash", "dspark"}
    if _spec_saved not in _spec_valid:
        _spec_manual = False
        _spec_saved = ""
    _auto_pair: dict = {}
    _pairing_off = str(_s_tune.get("spec_draft_mode") or "auto").strip().lower() == "off"
    if not _spec_manual and not _pairing_off and model_path:
        try:
            _candidate = find_spec_draft_for(model_path)
            if _candidate.get("matched") and _candidate.get("path"):
                _auto_pair = _candidate
        except Exception:
            _auto_pair = {}
    draft_mb = 0.0
    _draft_name = ""
    _draft_kind = ""
    try:
        _draft = _resolve_spec_draft_for_tune(model_path, _s_tune)
        if not _draft and _auto_pair.get("path"):
            _draft = str(_auto_pair["path"])
        if _draft:
            _draft_name = os.path.basename(_draft)
            try:
                _draft_kind = str(_spec_fingerprint(_draft).get("architecture") or "").lower()
            except Exception:
                _draft_kind = ""
            draft_mb = os.path.getsize(_draft) / (1024 * 1024)
            # The draft needs its own small KV cache and compute graph too.
            # Five-layer sliding-window drafts (DFlash) are only a few hundred
            # MB there, so reserve a bounded extra rather than a flat guess.
            draft_mb += max(192.0, draft_mb * 0.15)
            budget_mb = max(budget_mb - draft_mb, 256.0)
    except Exception:
        draft_mb = 0.0
    if src == "gguf":
        moe_tag = ""
        if is_moe:
            moe_tag = (f" · MoE {profile['expert_count']} experts, "
                       f"{profile['expert_used_count']} active/token")
        notes.append(
            f"GGUF: {profile['architecture']} · {n_layer} layers · "
            f"GQA head_kv={profile['head_count_kv']} key_len={profile['key_length']}{moe_tag}"
        )
    else:
        notes.append("GGUF header unreadable — using size-bucket fallback (less accurate).")
    notes.append(
        f"VRAM budget: {vram:.1f} GB − {vram_reserve_gb:.2f} GB GPU reserve · "
        f"usable {budget_mb/1024:.1f} GB ({int(headroom*100)}%). "
        f"System-RAM reserve {reserve_gb:.1f} GB applies to CPU offload only."
    )
    if mmproj_mb:
        notes.append(
            f"vision projector: {mmproj_mb:.0f} MB reserved "
            f"({_mp_name} loads alongside the model — disable mmproj to reclaim it)."
        )
    elif _mp_cpu:
        notes.append(f"vision projector: {_mp_name} runs on the CPU, so it takes no VRAM "
                     f"(images take a few seconds longer to read; text speed is unchanged).")
    if draft_mb:
        notes.append(
            f"speculative draft: {draft_mb:.0f} MB reserved "
            f"({_draft_name} loads alongside the model — context sized to fit it)."
        )

    # -- KV cache dtype --
    # q8_0 is the quality/cost sweet spot; f16 only with obvious headroom. An
    # explicit choice in the Auto-tune panel overrides both.
    if kv_pref != "auto":
        out["kv_cache_type"] = kv_pref
        notes.append(f"KV cache: K {kv_pref} (your choice in Auto-tune).")
    elif card >= 24 and size_gb < card * 0.4:
        out["kv_cache_type"] = "f16"
        notes.append("KV cache: f16 (you have headroom).")
    else:
        out["kv_cache_type"] = "q8_0"
        notes.append("KV cache: q8_0 (zero perplexity loss baseline).")
    if kv_v_pref and kv_v_pref != out["kv_cache_type"]:
        out["kv_cache_type_v"] = kv_v_pref
        notes.append(f"KV cache: V {kv_v_pref} (your choice in Auto-tune). Mixed K/V types run on "
                     f"llama.cpp's slower attention path.")

    # -- MTP detection (hoisted above the context solver: the draft-mtp choice
    # feeds the compute-overhead reserve below, and the speculative section
    # further down reuses this result) --
    # Primary signal: GGUF metadata key "<arch>.nextn_predict_layers" set by
    # llama.cpp's own converter for MTP-capable arches. Reliable — no false
    # positives from filenames, no false negatives from non-canonical names.
    has_mtp = bool(profile.get("has_mtp", False))
    if not has_mtp and profile.get("metadata_source") != "gguf":
        # Fallback: filename / arch heuristic, for GGUFs that lost the metadata
        # during conversion or third-party repacks that didn't carry it through.
        name_blob = ((profile.get("name") or "") + " " + (profile.get("architecture") or "")).lower()
        _MTP_HINTS = (
            "qwen3.5", "qwen3.6", "qwen-3.5", "qwen-3.6", "qwen_3.5", "qwen_3.6",
            "qwen3_5", "qwen3_6", "qwen_3_5", "qwen_3_6",
            "deepseek-v3", "deepseek_v3", "deepseekv3",
            "deepseek-r1", "deepseek_r1", "deepseekr1",
        )
        has_mtp = any(h in name_blob for h in _MTP_HINTS)
        if has_mtp:
            _EXCLUDE_MTP = ("distil", "distill", "distilled", "glm")
            if any(e in name_blob for e in _EXCLUDE_MTP):
                has_mtp = False

    # -- Context window + n_cpu_moe (joint optimization) --
    # Bigger ctx eats KV VRAM, which forces more expert offload, which slows
    # decoding. Pick the largest ctx whose required offload stays tolerable
    # (<=70% of layers). KV is priced per tier at the exact context via
    # _kv_total_mb (SWA layers cap at their window), and the notes say which KV
    # path is in use (MLA latent cache / hybrid scaling / sliding window).
    mla = _mla_dims(profile)
    hy = _hybrid_info(profile)
    swa = _swa_info(profile)
    if src == "gguf":
        if mla:
            notes.append(
                f"KV math: MLA latent cache (kv_lora_rank={mla[0]} + qk_rope={mla[1]}, "
                f"not per-head K/V)."
            )
        if hy["hybrid"]:
            if hy["interval"] and n_layer:
                attn_layers = max(1, n_layer // hy["interval"])
                pct = int(round(100.0 * attn_layers / n_layer))
                notes.append(
                    f"hybrid SSM/attention detected ({hy['evidence']}): full attention every "
                    f"{hy['interval']} layers — KV scaled to ~{pct}% of layers."
                )
            else:
                notes.append(
                    f"hybrid SSM/attention detected ({hy['evidence']}) but attention-layer "
                    f"fraction unknown — KV kept conservative (all layers treated as full attention)."
                )
        if swa and not mla and not (hy["hybrid"] and hy["interval"]):
            _w = swa["window"]
            _pat_arr = (profile.get("keys") or {}).get(
                f"{profile.get('architecture') or ''}.attention.sliding_window_pattern")
            if swa["pattern"] and n_layer:
                _full = max(1, n_layer // swa["pattern"])
                notes.append(
                    f"sliding-window attention: window {_w:,} tokens, every "
                    f"{swa['pattern']} layers full ({n_layer - _full} of {n_layer} layers "
                    f"windowed) — their KV caps at the window, so long context is cheap."
                )
            elif isinstance(_pat_arr, list) and n_layer and len(_pat_arr) == n_layer:
                _full = sum(1 for x in _pat_arr if not x)
                notes.append(
                    f"sliding-window attention: window {_w:,} tokens, per-layer pattern from "
                    f"GGUF ({_full} full + {n_layer - _full} windowed, windowed layers use "
                    f"smaller head dims) — exact KV math."
                )
            elif n_layer:
                _full = (n_layer + 1) // 2
                notes.append(
                    f"sliding-window attention: window {_w:,} tokens "
                    f"(layer pattern unknown — conservatively treated as {_full} full + "
                    f"{n_layer - _full} windowed layers)."
                )
        _passes = _layer_passes(profile)
        if _passes and sum(_passes) > n_layer:
            _looped = [i for i, p in enumerate(_passes) if p > 1]
            notes.append(
                f"looped layers: {_looped[0]}-{_looped[-1]} run {max(_passes)}× per token "
                f"({sum(_passes)} layer passes from {n_layer} stored layers) — KV cache and "
                f"decode reads are priced per pass."
            )

    size_mb = size_gb * 1024
    # Tier ladder. The GGUF-reported trained context is deliberately not
    # enforced: many GGUFs report a conservative value the model handles fine past
    # (RoPE/YaRN), so it is shown in notes only.
    trained_max = int(profile.get("context_length", 0) or 0)
    ctx_tiers = (262144, 131072, 98304, 65536, 49152, 32768, 24576, 16384, 12288, 8192, 4096)

    # -- Compute buffer + runtime overhead reserve --
    # Three deterministic parts:
    #   1. compute buffer: 256MB floor, scaled by batch tokens x embedding width
    #      (activations, scratch, attention work area), sized for the largest
    #      batch we might pick (2048). Flat 1536MB when embd is unknown.
    #   2. runtime allowance: fixed 400MB for CUDA context + driver on GPU tiers.
    #   3. MTP overhead, from measured breakdowns (4090, 27B qwen35 hybrid, one
    #      slot): head weights (nextn x per-layer size) + 3 extra copies of the
    #      recurrent state on hybrids (~150 MiB each) + two draft compute buffers
    #      of ~196 MiB per 512 of ubatch each. The draft's own f16 KV cache grows
    #      with context, so it is priced per tier (_kvx).
    embd = profile.get("embedding_length", 0) or 0
    if embd:
        compute_mb = 256.0 + (2048.0 / 512.0) * (embd / 4096.0) * 384.0
    else:
        compute_mb = 1536.0  # no embd from GGUF — keep the old flat reserve
    runtime_mb = 400.0
    mtp_mb = 0.0
    draft_kv_per_tok = 0.0       # bytes/token of the MTP draft's own KV cache
    _tune_caps = llama_caps(find_llama_bin() or "")
    nextn = int(profile.get("nextn_predict_layers", 0) or 0)
    mtp_fixed_mb = 0.0           # MTP head weights + extra recurrent state
    mtp_compute_per_512 = 0.0    # MTP draft compute, per 512 tokens of ubatch
    # The ubatch the batch heuristics below will settle on (full GPU: 1024
    # from 24 GB up, else 512) — the exact fit re-prices with the real one.
    _ub_assumed = 1024 if card >= 24 else 512
    if has_mtp and nextn and n_layer and size_mb > 0:
        mtp_fixed_mb = nextn * (size_mb / n_layer) + (3 * 150.0 if hy["hybrid"] else 0.0)
        mtp_compute_per_512 = 2 * 200.0
        mtp_mb = mtp_fixed_mb + mtp_compute_per_512 * _ub_assumed / 512
        _bpe = 2.0               # f16 draft KV (see the spawn note on -ctkd)
        _hkv = profile.get("head_count_kv") or 0
        _hkv = max(_hkv) if isinstance(_hkv, list) and _hkv else _hkv
        _kl = int(profile.get("key_length") or 0)
        if _hkv and _kl:
            draft_kv_per_tok = nextn * 2 * int(_hkv) * _kl * _bpe
    compute_buf_mb = compute_mb + runtime_mb + mtp_mb
    notes.append(
        f"overhead reserve: {compute_buf_mb:.0f} MB "
        f"(compute buffer {compute_mb:.0f} MB scaled to batch 2048 × embd {embd or 'unknown'}"
        f" + runtime {runtime_mb:.0f} MB"
        + (f" + MTP {mtp_mb:.0f} MB ({nextn} heads)" if mtp_mb else "") + ")."
        + (f" MTP draft KV: {draft_kv_per_tok / 1024:.1f} KB/token, priced per context tier."
           if draft_kv_per_tok else "")
    )

    def _kvx(kv_dtype: str, tier: int, v_dtype: str = "") -> float:
        """KV MB at `tier` tokens, including the MTP draft's own KV cache."""
        return (_kv_total_mb(profile, kv_dtype, tier, size_gb, v_dtype)
                + draft_kv_per_tok * tier / (1024 * 1024))

    # -- MoE dense/expert split (analytic when GGUF carries the fields) --
    keys = profile.get("keys") or {}
    arch_name = profile.get("architecture") or ""
    dense_share = None
    if is_moe and n_layer > 0:
        exp_cnt = profile.get("expert_count", 0) or 0
        exp_ffn = profile.get("expert_feed_forward_length", 0) or 0
        if embd and exp_cnt and exp_ffn:
            # Element counts; the dtype factor cancels in the ratio.
            # attention ≈ 4·embd² (q/k/v/o projections — documented
            # approximation; exact head dims rarely move the ratio much).
            attn_per_layer = 4 * embd * embd
            shared_per_layer = 0
            shared_ffn = keys.get(f"{arch_name}.expert_shared_feed_forward_length")
            shared_cnt = keys.get(f"{arch_name}.expert_shared_count")
            if isinstance(shared_ffn, (int, float)) and shared_ffn:
                shared_per_layer = 3 * int(shared_ffn) * embd
            elif isinstance(shared_cnt, (int, float)) and shared_cnt:
                shared_per_layer = int(shared_cnt) * 3 * exp_ffn * embd
            dense_per_layer = attn_per_layer + shared_per_layer
            expert_per_layer = exp_cnt * 3 * exp_ffn * embd
            dense_share = max(0.03, min(0.50, dense_per_layer / (dense_per_layer + expert_per_layer)))
            notes.append(
                f"MoE dense share: {dense_share*100:.1f}% (analytic: attention 4·embd²"
                f"{' + shared expert' if shared_per_layer else ''} "
                f"vs {exp_cnt} experts × 3·{exp_ffn}·embd)."
            )
        else:
            # Heuristic fallback (~10-15% for big MoEs, more for small ones), used only
            # when the GGUF lacks the fields above.
            dense_share = max(0.08, min(0.20, 1.5 / max(profile.get("expert_count", 8), 1) + 0.08))
            notes.append(
                f"MoE dense share: {dense_share*100:.1f}% (heuristic fallback — "
                f"GGUF lacks embd/expert-FFN fields for the analytic split)."
            )

    # Embedding + LM head are never offloaded by --n-cpu-moe — reserve them as
    # fixed VRAM when we can size them. vocab × embd × 2 tensors × ~2 bytes
    # (f16-ish: a conservative overestimate for quantized embedding tables).
    embed_mb = 0.0
    if is_moe and embd:
        vocab = keys.get(f"{arch_name}.vocab_size")
        if not (isinstance(vocab, (int, float)) and vocab):
            tok = keys.get("tokenizer.ggml.tokens")
            vocab = tok.get("__array_len__", 0) if isinstance(tok, dict) else 0
        if vocab:
            embed_mb = (int(vocab) * embd * 2 * 2) / (1024 * 1024)
            notes.append(
                f"embedding + LM head reserve: {embed_mb:.0f} MB "
                f"(vocab {int(vocab):,} × embd {embd} × 2 tensors, not offloadable)."
            )

    # -- MoE offload speed model --
    # --n-cpu-moe moves whole layers' experts to CPU RAM, but every decoded
    # token still activates expert_used_count experts in those layers — so the
    # CPU share decodes at roughly RAM-bandwidth / bytes-per-token. We estimate
    # that and refuse (ctx, offload) combos that decode below the user's floor:
    # context you can only read at 2 tok/s is not a win, it's a trap.
    # The user's floor (Settings → Minimum speed). Applies to dense and MoE.
    try:
        min_tok_s = max(float(_s_tune.get("min_decode_tok_s") or 45), 1.0)
    except Exception:
        min_tok_s = 45.0
    expert_tok_bytes = 0.0
    if is_moe and n_layer > 0 and embd:
        _eu = profile.get("expert_used_count", 0) or 0
        _ef = profile.get("expert_feed_forward_length", 0) or 0
        if _eu and _ef:
            # Bytes per weight from the file itself (size ÷ parameter count from
            # the same element counts as the dense/expert split), not the quant
            # name: many GGUFs carry no quant label, and the Q4-ish fallback
            # under-counted a Q8 35B-A3B's expert reads ~1.8× (predicted 141
            # tok/s vs ~51 measured).
            _bpw = _quant_bpw(profile.get("quant", ""))
            _ecnt = profile.get("expert_count", 0) or 0
            _vocab = keys.get(f"{arch_name}.vocab_size") or 0
            if not (isinstance(_vocab, (int, float)) and _vocab):
                _tok = keys.get("tokenizer.ggml.tokens")
                _vocab = _tok.get("__array_len__", 0) if isinstance(_tok, dict) else 0
            if _ecnt and size_mb > 0:
                _params = n_layer * (4 * embd * embd + _ecnt * 3 * _ef * embd) + int(_vocab or 0) * embd * 2
                if _params > 0:
                    _bpw = min(max(size_mb * 1048576 / _params, 0.25), 2.2)
            expert_tok_bytes = _eu * 3 * _ef * embd * _bpw
    # RAM bandwidth: auto-probed (memcpy benchmark, cached per machine) unless
    # the user pinned a manual value. Probed lazily — only when the speed
    # model will actually run; the benchmark costs ~0.5s once per machine.
    # expert_tok_bytes is ONE layer's active experts; a token crosses every
    # layer. (The old estimate scaled the single-layer figure by the offloaded
    # SHARE, under-counting CPU bytes ~n_layer× — so the floor never fired.)
    active_expert_bytes = expert_tok_bytes * n_layer if expert_tok_bytes > 0 else 0.0
    ram_bw_gbps = 30.0
    if expert_tok_bytes > 0 or size_mb > budget_mb:
        ram_bw_gbps = _ram_bandwidth_gbps()
        notes.append(f"RAM bandwidth: {ram_bw_gbps:.0f} GB/s (auto-probed unless pinned in settings).")

    # System-RAM safety cap: CPU-offloaded experts (and dense partial-offload
    # layers) live in system RAM. Cap offload at (available − OS reserve) × 0.9
    # so Windows never starts thrashing to fit the model.
    ram_cap_mb = None
    ram_info = _system_ram_info()
    if ram_info:
        total_ram_gb, avail_ram_gb = ram_info
        # Swap-aware: a tune almost always precedes a load that stops the running
        # instance first, so add its RSS back to available RAM (otherwise a re-tune
        # with a big MoE loaded sees almost nothing free).
        inst_ram_mb, _inst_vram_mb = _running_instance_mem_mb()
        if inst_ram_mb > 0:
            avail_ram_gb += inst_ram_mb / 1024.0
            notes.append(
                f"running instance holds {inst_ram_mb/1024:.1f} GB RAM — counted as free "
                f"(a model swap releases it before the new load allocates)."
            )
        ram_cap_mb = max((avail_ram_gb - reserve_gb) * 1024 * 0.9, 0)
        if is_moe or size_mb > budget_mb:
            notes.append(
                f"system RAM: {avail_ram_gb:.1f} GB free of {total_ram_gb:.0f} GB — "
                f"CPU offload capped at {ram_cap_mb/1024:.1f} GB (OS reserve kept)."
            )

    # -- decode speed prediction (see _decode_tok_s) --
    tune_gpus = _tune_gpus(vram)
    gpu_bw_gbps = _effective_gpu_bw(tune_gpus)
    spec_for_speed, _ = _speculative_choice(_s_tune, has_mtp, is_moe, nextn, _auto_pair, _draft_kind)

    def _predict(fill: int, *, n_off: int = 0, num_gpu: int = 99, kv_k: str = "", kv_v: str = "",
                 spec: str = "", draft_n: int = 3, calib_factor: float = 1.0) -> float:
        kv_k = kv_k or out.get("kv_cache_type") or "q8_0"
        gpu_b, cpu_b = _decode_bytes_per_token(
            profile, size_mb, kv_dtype=kv_k, v_dtype=kv_v, fill=fill, n_cpu_moe=n_off,
            num_gpu=num_gpu, dense_share=dense_share, active_expert_bytes=active_expert_bytes)
        gain = _SPEC_GAIN_DEFAULT.get(spec or spec_for_speed, 1.0)
        if (spec or spec_for_speed) == "draft-mtp":
            gain *= _MTP_LEN_FACTOR.get(min(max(int(draft_n or 3), 1), 6), 1.0)
        if is_moe:
            gain = min(gain, _SPEC_GAIN_MOE)
        return _decode_tok_s(gpu_b, cpu_b, gpu_bw_gbps=gpu_bw_gbps, ram_bw_gbps=ram_bw_gbps,
                             spec_gain=gain, calib=calib_factor)

    def _predict_settings(cfg: dict, fill: int) -> float:
        """Uncalibrated prediction for a recorded config (calibration input). Turns
        recorded before the launch draft length was tracked ran MTP at the GGUF
        head count, usually 1."""
        _dn = int(cfg.get("spec_draft_n_eff") or cfg.get("spec_draft_n_max") or (nextn or 1))
        return _predict(fill, n_off=int(cfg.get("n_cpu_moe") or 0),
                        num_gpu=int(cfg.get("num_gpu") or 99),
                        kv_k=str(cfg.get("kv_cache_type") or "q8_0"),
                        kv_v=str(cfg.get("kv_cache_type_v") or ""),
                        spec=str(cfg.get("spec_strategy") or "off"), draft_n=_dn)

    calib, calib_n = _decode_calibration(model_path, _predict_settings)
    typical_fill, fill_src = _typical_fill_tokens(model_path)
    notes.append(
        f"speed model: {gpu_bw_gbps:.0f} GB/s GPU ({', '.join(g['bw_src'] for g in tune_gpus)})"
        + (f", calibrated ×{calib:.2f} from {calib_n} measured turns" if calib_n >= 5
           else f", uncalibrated ({calib_n} measured turns, needs 5)")
        + f"; floor {min_tok_s:g} tok/s judged at your usual chat size, ~{typical_fill:,} tokens "
          f"({fill_src})."
    )

    def _est_tok_s(n_off: int, ctx_tokens: int = 0) -> float:
        """Predicted decode tok/s (calibrated) with n_off expert layers on CPU,
        at the user's usual chat size (never more than the window holds)."""
        fill = min(typical_fill, ctx_tokens) if ctx_tokens > 0 else typical_fill
        return _predict(fill, n_off=n_off, calib_factor=calib)

    def _kv_marginal_mb_per_tok(k_dt: str, v_dt: str = "") -> float:
        """Marginal KV cost (MB) per extra context token, from a finite
        difference taken high above any sliding window — so windowed layers
        contribute their true marginal (zero past the window) instead of an
        average. 0 when GGUF metadata is unreadable."""
        draft_mb_tok = draft_kv_per_tok / (1024 * 1024)
        b_hi = _kv_total_bytes(profile, k_dt, 131072, v_dt)
        b_lo = _kv_total_bytes(profile, k_dt, 65536, v_dt)
        if b_hi > b_lo:
            return (b_hi - b_lo) / (131072 - 65536) / (1024 * 1024) + draft_mb_tok
        b = _kv_total_bytes(profile, k_dt, 65536, v_dt)
        return ((b / 65536) / (1024 * 1024) + draft_mb_tok) if b else 0.0

    # Exact-fit top-up guard: keep this much budget unspent for CUDA
    # fragmentation/allocation jitter when converting leftover VRAM into
    # extra context tokens.
    _TOPUP_GUARD_MB = 96.0

    def _topup_ctx(base_ctx: int, leftover_mb: float, k_dt: str, v_dt: str = "") -> int:
        """Spend leftover budget on context directly — the tier ladder is
        coarse (64k → 96k → 128k) and would otherwise leave real tokens on
        the table. Returns the exact-fit ctx (rounded down to a 4096-token
        multiple), or base_ctx when the gain isn't worth it (<5%)."""
        marg = _kv_marginal_mb_per_tok(k_dt, v_dt)
        if marg <= 0 or leftover_mb <= _TOPUP_GUARD_MB:
            return base_ctx
        extra = int((leftover_mb - _TOPUP_GUARD_MB) / marg)
        exact = min(ctx_tiers[0], (base_ctx + extra) // 4096 * 4096)
        return exact if exact >= int(base_ctx * 1.05) else base_ctx

    def _solve(kv_dtype: str, pinned_tier: int = 0, v_dtype: str = ""):
        """Pick (ctx, n_cpu_moe) for a KV dtype pair (v_dtype="" mirrors kv_dtype).

        KV is priced at the exact context of each tier (_kv_total_mb). With
        pinned_tier=0 the ladder is scanned for the largest ctx that fits and
        decodes at or above the speed floor, then an exact-fit top-up spends the
        leftover budget on tokens beyond the coarse tier; with a pinned tier only
        that tier is evaluated. Returns (ctx, n_cpu_moe, note_lines, fits); fits is
        False only for a pinned tier that busts the budget or needs more offload
        than the caps allow. May set out["quant_downshift"]."""
        tier_notes: list[str] = []
        kv_tag = f"{kv_dtype}/{v_dtype}" if v_dtype and v_dtype != kv_dtype else kv_dtype
        if is_moe and n_layer > 0:
            expert_total_mb = max(size_mb * (1 - dense_share) - embed_mb, size_mb * 0.1)
            expert_per_layer_mb = expert_total_mb / n_layer
            # Hard offload ceiling: 85% of layers when the speed model can
            # price the tradeoff (past that almost no compute stays on the
            # GPU anyway), 70% when it can't (missing GGUF expert fields).
            max_offload_layers = int(n_layer * (0.85 if expert_tok_bytes > 0 else 0.7))
            # System-RAM clamp: offloaded experts live in RAM — never offload
            # more than (available RAM − OS reserve) can actually hold.
            if ram_cap_mb is not None and expert_per_layer_mb > 0:
                max_offload_layers = min(max_offload_layers,
                                         int(ram_cap_mb // expert_per_layer_mb))
            # Fast zone: offload at or below this share is always acceptable
            # without consulting the speed floor — the hit is modest.
            fast_zone_layers = int(n_layer * 0.30)

            chosen_ctx = 0
            chosen_n_cpu_moe = 0
            rejected_slow: list = []   # (tier, need_offload, est_tok_s) — fit but slow
            for tier in ([pinned_tier] if pinned_tier else ctx_tiers):
                kv_mb = _kvx(kv_dtype, tier, v_dtype)
                available_for_model = budget_mb - kv_mb - compute_buf_mb - embed_mb
                if available_for_model <= 0:
                    continue  # KV alone busts the budget
                if available_for_model >= size_mb:
                    # Whole model + KV + compute fits. No offload, max speed.
                    chosen_ctx = tier
                    chosen_n_cpu_moe = 0
                    break
                shortfall = size_mb - available_for_model
                need_offload = int((shortfall + expert_per_layer_mb - 1) // expert_per_layer_mb)
                if need_offload > max_offload_layers:
                    continue
                est = _est_tok_s(need_offload, tier)
                # A pinned tier is the user's saved ctx — speed never vetoes it.
                # The fast zone only stands in when the speed model has no
                # expert sizes to work with.
                if est >= min_tok_s or pinned_tier or (
                        expert_tok_bytes <= 0 and need_offload <= fast_zone_layers):
                    chosen_ctx = tier
                    chosen_n_cpu_moe = need_offload
                    break
                rejected_slow.append((tier, need_offload, est))
            if not chosen_ctx and rejected_slow and not pinned_tier:
                # Every fitting tier decodes under the floor: take the fastest one (smallest
                # ctx, least offload). Usable context beats unusable context.
                tier, need_offload, est = rejected_slow[-1]
                chosen_ctx, chosen_n_cpu_moe = tier, need_offload
                tier_notes.append(
                    f"speed floor relaxed: larger tiers all decoded under "
                    f"{min_tok_s:g} tok/s est. — took {tier:,} tokens at ~{est:.0f} tok/s instead."
                )
            if not chosen_ctx:
                if pinned_tier:
                    # Pinned tier doesn't fit within the offload caps.
                    return pinned_tier, n_layer, [], False
                chosen_ctx = ctx_tiers[-1]  # smallest as fallback
                # Most offload the caps allow — never past the RAM cap, or
                # we'd fix a VRAM problem by creating a system-RAM problem.
                chosen_n_cpu_moe = max_offload_layers
            # Fit reality check: if the model doesn't fit even at the caps, say so with
            # the quant-downshift banner instead of a config that OOMs at boot.
            if chosen_n_cpu_moe > 0 and chosen_n_cpu_moe >= max_offload_layers and expert_per_layer_mb > 0:
                kv_check = _kvx(kv_dtype, chosen_ctx, v_dtype)
                avail_check = budget_mb - kv_check - compute_buf_mb - embed_mb
                shortfall_check = size_mb - avail_check
                need_check = int((shortfall_check + expert_per_layer_mb - 1) // expert_per_layer_mb)
                if need_check > max_offload_layers:
                    cur_q = profile.get("quant", "Q4_K_M")
                    suggest_q = "Q3_K_S" if cur_q.upper().startswith("Q4") else "IQ3_XS"
                    out["quant_downshift"] = (
                        f"This model can't fit on this machine: even offloading the max "
                        f"{max_offload_layers}/{n_layer} expert layers it falls "
                        f"{shortfall_check/1024:.1f} GB short at {chosen_ctx:,} ctx. "
                        f"Grab the {suggest_q} quant, free up VRAM/RAM, or raise the OS reserve tradeoff."
                    )
                    out["does_not_fit"] = True
                    tier_notes.append(
                        f"WARNING: model does not fit — {need_check} expert layers would need "
                        f"offloading but caps allow {max_offload_layers}. Expect boot failure."
                    )
            kv_at_chosen = _kvx(kv_dtype, chosen_ctx, v_dtype)
            # Exact-fit top-up: spend leftover budget on context beyond the
            # coarse tier. Offload stays valid — the fit math above guarantees
            # the total (KV + compute + embed + on-GPU weights) stays within
            # budget, and the top-up guard keeps 96 MB unspent for jitter.
            if not pinned_tier and chosen_ctx < ctx_tiers[0] and expert_per_layer_mb > 0:
                vram_used = (kv_at_chosen + compute_buf_mb + embed_mb
                             + size_mb - chosen_n_cpu_moe * expert_per_layer_mb)
                topped = _topup_ctx(chosen_ctx, budget_mb - vram_used, kv_dtype, v_dtype)
                if topped != chosen_ctx:
                    tier_notes.append(
                        f"exact-fit top-up: ctx {chosen_ctx:,} → {topped:,} "
                        f"(spent leftover budget at "
                        f"{_kv_marginal_mb_per_tok(kv_dtype, v_dtype)*1024:.1f} KB/token marginal KV)."
                    )
                    chosen_ctx = topped
                    kv_at_chosen = _kvx(kv_dtype, chosen_ctx, v_dtype)
            if chosen_n_cpu_moe == 0:
                tier_notes.append(
                    f"context: {chosen_ctx:,} tokens · n_cpu_moe: 0 "
                    f"(model fits fully in VRAM at this context, KV ≈ {kv_at_chosen:.0f} MB)."
                )
            else:
                est = _est_tok_s(chosen_n_cpu_moe, chosen_ctx)
                est_txt = (f"; est. decode ~{est:.0f} tok/s at {ram_bw_gbps:.0f} GB/s RAM"
                           if est != float("inf") else "")
                tier_notes.append(
                    f"context: {chosen_ctx:,} tokens · n_cpu_moe: {chosen_n_cpu_moe} of {n_layer} "
                    f"(KV ≈ {kv_at_chosen:.0f} MB; offloading {chosen_n_cpu_moe} expert layers to fit + leave room{est_txt})."
                )
            # Offloading more than half the layers even at the chosen context leaves real
            # throughput on the table; suggest a smaller quant.
            if chosen_n_cpu_moe > n_layer * 0.5:
                cur_q = profile.get("quant", "Q4_K_M")
                suggest_q = "Q3_K_S" if cur_q.upper().startswith("Q4") else "IQ3_XS"
                out["quant_downshift"] = (
                    f"Offloading {chosen_n_cpu_moe}/{n_layer} layers at {cur_q or 'this quant'}. "
                    f"Grab the {suggest_q} variant for ~3-5x throughput with full context."
                )
            return chosen_ctx, chosen_n_cpu_moe, tier_notes, True
        if is_moe:
            # No layer count from GGUF — fall back to ratio formula + old context calc.
            if pinned_tier:
                est_ctx = pinned_tier
                # Fit check: KV + compute must leave something for the model.
                fits = budget_mb - _kvx(kv_dtype, est_ctx, v_dtype) - compute_buf_mb > 0
            else:
                ctx_budget_mb = budget_mb * 0.45
                est_ctx = ctx_tiers[-1]
                for tier in ctx_tiers:
                    if _kvx(kv_dtype, tier, v_dtype) <= ctx_budget_mb:
                        est_ctx = tier
                        break
                fits = True
            shortfall_gb = (size_gb * 1.1) - vram
            ratio = max(0.0, min(1.0, shortfall_gb / max(size_gb, 1)))
            est_n_cpu_moe = max(0, min(int(round(ratio * 50)), 60))
            tier_notes.append(f"context: {est_ctx:,} tokens (estimated — no layer count from GGUF).")
            tier_notes.append(f"n_cpu_moe: {est_n_cpu_moe} (estimated).")
            return est_ctx, est_n_cpu_moe, tier_notes, fits
        # Dense model: max context that fits alongside model weights. KV is
        # priced exactly per tier, so SWA dense models (Gemma, Mistral) get
        # the big windows their windowed layers make nearly free.
        if pinned_tier:
            kv_mb = _kvx(kv_dtype, pinned_tier, v_dtype)
            if size_mb + kv_mb + compute_buf_mb > budget_mb:
                return pinned_tier, 0, [], False
            dense_ctx = pinned_tier
        else:
            dense_ctx = ctx_tiers[-1]
            fits_any = False
            for tier in ctx_tiers:
                kv_mb = _kvx(kv_dtype, tier, v_dtype)
                if size_mb + kv_mb + compute_buf_mb <= budget_mb:
                    dense_ctx = tier
                    fits_any = True
                    break
            if not fits_any and not pinned_tier:
                # Even the smallest tier busts VRAM. The num_gpu section can still split
                # dense layers GPU/CPU, but if VRAM + offloadable RAM can't hold the weights,
                # say so instead of persisting a config that OOMs at boot.
                _ram_hold = ram_cap_mb if ram_cap_mb is not None else size_mb
                if size_mb + compute_buf_mb > budget_mb + _ram_hold:
                    cur_q = profile.get("quant", "Q4_K_M")
                    suggest_q = "Q3_K_M" if cur_q.upper().startswith("Q4") else "Q4_K_S"
                    out["does_not_fit"] = True
                    out["quant_downshift"] = (
                        f"This model can't fit on this machine: {size_mb/1024:.1f} GB of weights "
                        f"vs {budget_mb/1024:.1f} GB VRAM + {_ram_hold/1024:.1f} GB offloadable RAM. "
                        f"Grab a smaller quant ({suggest_q}) or free up memory."
                    )
        # Exact-fit top-up: the tier ladder is coarse — spend the leftover
        # budget on context directly instead of leaving it on the table.
        if not pinned_tier and dense_ctx < ctx_tiers[0]:
            kv_at_chosen = _kvx(kv_dtype, dense_ctx, v_dtype)
            leftover = budget_mb - (size_mb + kv_at_chosen + compute_buf_mb)
            topped = _topup_ctx(dense_ctx, leftover, kv_dtype, v_dtype)
            if topped != dense_ctx:
                tier_notes.append(
                    f"exact-fit top-up: ctx {dense_ctx:,} → {topped:,} "
                    f"(spent {leftover:.0f} MB leftover budget at "
                    f"{_kv_marginal_mb_per_tok(kv_dtype, v_dtype)*1024:.1f} KB/token marginal KV)."
                )
                dense_ctx = topped
        kv_at_chosen = _kvx(kv_dtype, dense_ctx, v_dtype)
        tier_notes.append(
            f"context: {dense_ctx:,} tokens "
            f"(KV ≈ {kv_at_chosen:.0f} MB at {kv_tag})."
        )
        return dense_ctx, 0, tier_notes, True

    chosen_ctx, chosen_n_cpu_moe, tier_notes, _ = _solve(out["kv_cache_type"],
                                                         v_dtype=out.get("kv_cache_type_v") or "")

    # -- KV dtype policy --
    # K cache is q8_0 by default, f16 only with obvious VRAM headroom (pass 1
    # above). The tuner never emits a split K/V and never q4_0 on its own, except
    # the escape hatch below for GPUs with <=12 GB where context is genuinely
    # scarce. A K/V choice made in the Auto-tune panel is used as given.
    # Same-machine measurements (llama-perplexity, Qwen3.6-27B-Q4_K_M, 5x8192
    # chunks, 150k-char corpus):
    #   q8_0/q8_0  +0.2% PPL vs f16 (noise) and fast (~4 s/pass)
    #   q8_0/q4_0  +1.6% PPL and a slow mixed-dtype path (~184 s/pass); the KIVI
    #               "free V quant" claim (arXiv:2402.02750) does not hold here
    #   q8_0/f16   +1.8% PPL, slow path (~70 s/pass)
    #   q4_0/f16   +2.6% PPL, slow path (~67 s/pass)
    #   q4_0/q4_0  never measured to completion (slow path)
    # So q8_0/q8_0 is both the accuracy pick and the fast pick.
    if (kv_pref == "auto" and not kv_v_pref and out["kv_cache_type"] != "f16"
            and card <= 12.0 and chosen_ctx < 32768):
        # Escape hatch: q4_0 only on small GPUs (≤12 GB VRAM) with starved
        # context — below 16k we accept a 1.5× gain, between 16k and 32k we
        # demand 2×. Above 32k the conversation already has room to work, and
        # on bigger GPUs q8_0 KV fits — the quality cost is never worth it.
        gain_needed = 1.5 if chosen_ctx < 16384 else 2.0
        prev_downshift = out["quant_downshift"]
        out["quant_downshift"] = ""
        ctx_q4, moe_q4, q4_tier_notes, _ = _solve("q4_0", v_dtype="q4_0")
        if ctx_q4 >= int(chosen_ctx * gain_needed):
            out["kv_cache_type"] = "q4_0"
            out["kv_cache_type_v"] = ""
            chosen_ctx, chosen_n_cpu_moe, tier_notes = ctx_q4, moe_q4, q4_tier_notes
            notes.append(
                f"KV cache: switched to q4_0 (≤12 GB VRAM) — context was starved at q8_0 and "
                f"q4_0 buys ≥{gain_needed:g}× context (now {chosen_ctx:,} tokens)."
            )
        else:
            out["quant_downshift"] = prev_downshift
            notes.append(
                f"KV cache: kept {out['kv_cache_type']} — q4_0 retry only buys "
                f"{ctx_q4 / max(chosen_ctx, 1):.1f}× context (<{gain_needed:g}× threshold)."
            )

    # -- Grow-only context (min_ctx) --
    # Callers pass their saved num_ctx so a re-tune never silently downgrades
    # context, and never pairs a big saved ctx with offload computed for a smaller
    # one (a combo the solver rejected, which can OOM at boot). If the saved ctx
    # beats our pick, re-solve pinned to the smallest tier >= min_ctx and keep it
    # only when that ctx's offload stays within the normal caps.
    #
    # Proven configs outrank the estimate: a ctx llama-server actually booted with
    # on this machine (stamped by _stamp_proven_boot) fits by definition. When
    # proven covers min_ctx, the offload keys come straight from the proven combo.
    min_ctx = int(min_ctx or 0)
    proven = proven_cfg if isinstance(proven_cfg, dict) else {}
    if ((kv_pref != "auto" and str(proven.get("kv_cache_type") or "q8_0").lower() != kv_pref)
            or (kv_v_pref and str(proven.get("kv_cache_type_v") or proven.get("kv_cache_type") or "q8_0").lower()
                != kv_v_pref)):
        proven = {}      # it booted with other cache types, so it proves nothing here
    honored_proven = False
    # ">=": when the estimate merely ties the proven ctx, the proven combo still
    # wins. Re-solving it re-derives the offload and ubatch from live free VRAM,
    # which a model still loading makes wrong (seen 2026-11-21: a boot re-tune
    # during Inkling's load moved all 24 expert layers to RAM at ubatch 512).
    if min_ctx > 0 and min_ctx >= chosen_ctx:
        p_ctx_saved = int(proven.get("proven_ctx") or proven.get("num_ctx") or 0)
        pinned = min((t for t in ctx_tiers if t >= min_ctx), default=None)
        if p_ctx_saved >= min_ctx:
            # Honor the proven ctx outright; the offload keys are re-applied after the
            # batch/thread heuristics below.
            chosen_ctx = min_ctx
            chosen_n_cpu_moe = max(0, int(proven.get("n_cpu_moe") or 0))
            honored_proven = True
            notes.append(f"honored proven ctx {min_ctx:,} — this exact combo booted "
                         f"successfully on this machine, so it outranks the VRAM "
                         f"budget estimate.")
        elif min_ctx == chosen_ctx:
            pass                     # the estimate already reaches the saved ctx
        elif pinned is not None:
            p_ctx, p_moe, p_tier_notes, p_fits = _solve(out["kv_cache_type"], pinned_tier=pinned,
                                                        v_dtype=out.get("kv_cache_type_v") or "")
            if p_fits:
                chosen_ctx, chosen_n_cpu_moe, tier_notes = p_ctx, p_moe, p_tier_notes
                notes.append(
                    f"honored existing ctx {min_ctx:,} (pinned tier {pinned:,}; "
                    f"offload recomputed for that context)."
                )
            else:
                kv_pinned = _kv_total_mb(profile, out["kv_cache_type"], pinned, size_gb,
                                         out.get("kv_cache_type_v") or "")
                notes.append(
                    f"saved ctx {min_ctx:,} doesn't fit the current VRAM budget "
                    f"(KV alone ≈ {kv_pinned:.0f} MB at tier {pinned:,}) — "
                    f"not honored; using {chosen_ctx:,} instead."
                )
        else:
            notes.append(
                f"saved ctx {min_ctx:,} exceeds the tuner's {ctx_tiers[0]:,} ceiling — "
                f"not honored; using {chosen_ctx:,} instead."
            )

    out["num_ctx"] = chosen_ctx
    # Expert offload is MoE-only. A proven combo copied from a saved config can
    # carry a value a previous MoE model left in the shared setting; on a dense
    # model llama.cpp ignores it, but it mislabels the plan and skips the
    # spare-VRAM ubatch step.
    out["n_cpu_moe"] = chosen_n_cpu_moe if is_moe else 0
    notes.extend(tier_notes)
    if trained_max:
        notes.append(f"GGUF reports trained max {trained_max:,} tokens (informational, not enforced).")

    # -- num_gpu (dense partial offload) --
    if not is_moe and size_gb * 1024 > budget_mb:
        frac_on_gpu = max(0.2, budget_mb / (size_gb * 1024))
        if ram_cap_mb is not None:
            # Offloaded dense layers live in system RAM — raise the GPU share
            # if the CPU side would bust (available RAM − OS reserve).
            min_frac = 1.0 - ram_cap_mb / (size_gb * 1024)
            if min_frac > frac_on_gpu:
                frac_on_gpu = min(min_frac, 1.0)
                notes.append(
                    f"system RAM cap raised GPU share to {int(frac_on_gpu*100)}% — "
                    f"more CPU offload than this would exceed free RAM + OS reserve."
                )
        if n_layer > 0:
            out["num_gpu"] = max(8, int(n_layer * frac_on_gpu))
            notes.append(
                f"num_gpu: {out['num_gpu']} of {n_layer} layers "
                f"(dense model too big — partial offload, expect slow tok/s)."
            )
            # Quant downshift hint for dense too
            if frac_on_gpu < 0.6:
                cur_q = profile.get("quant", "")
                suggest_q = "Q3_K_M" if cur_q.upper().startswith("Q4") else "Q4_K_S"
                out["quant_downshift"] = (
                    f"Only {int(frac_on_gpu*100)}% of layers fit on GPU. "
                    f"A smaller quant ({suggest_q}) would run much faster."
                )
        else:
            out["num_gpu"] = max(8, int(80 * frac_on_gpu))
            notes.append(f"num_gpu: {out['num_gpu']} layers (dense partial offload).")

    # -- Speculative decoding --
    # One decision table (see _speculative_choice): manual wins, then a
    # validated DFlash/DSpark pair, then MTP, then n-gram/off.
    out["spec_strategy"], _spec_note = _speculative_choice(
        _s_tune, has_mtp, is_moe, nextn, _auto_pair, _draft_kind)
    notes.append(_spec_note)

    # -- batch / ubatch / threads --
    # With CPU offload, bigger batches dramatically improve prompt eval because
    # the PCIe transfer is amortized over more tokens; 512 is too small there.
    cpu_offload = out["n_cpu_moe"] > 0 or (not is_moe and out["num_gpu"] < 99)
    if cpu_offload:
        out["num_batch"] = 2048
        notes.append("batch: 2048 (offloading).")
    elif card >= 24:
        out["num_batch"] = 2048
    elif card >= 16:
        out["num_batch"] = 1024

    # Sanity: batch must never exceed ctx (llama.cpp rejects n_batch > n_ctx).
    if out["num_batch"] > out["num_ctx"]:
        out["num_batch"] = max(512, out["num_ctx"])
        notes.append(f"batch: clamped to {out['num_batch']} (was larger than ctx {out['num_ctx']:,}).")

    # Micro-batch: 512 only with CPU offload (larger values thrash L3 and RAM
    # bandwidth on CPU-offloaded MoEs). Full-GPU runs get 0 = auto, and the spawn
    # path picks min(max(n_batch//2, 512), 1024).
    if cpu_offload:
        out["n_ubatch"] = 512
        notes.append("n_ubatch: 512 (CPU offload active — larger ubatch thrashes L3/RAM bandwidth).")
    else:
        out["n_ubatch"] = 0
        notes.append("n_ubatch: 0 (auto — spawn path picks min(max(n_batch//2, 512), 1024) for full-GPU runs).")

    # Threads matter when part of the model lives on CPU. Physical-core
    # heuristic: half the logical count, floor 4. 0 = llama-server default.
    if cpu_offload:
        out["num_thread"] = max(4, (os.cpu_count() or 8) // 2)
        notes.append(f"threads: {out['num_thread']} (CPU offload active — physical-core heuristic).")
    else:
        out["num_thread"] = 0
        notes.append("threads: 0 (server default — full GPU offload).")

    # An honored proven combo re-applies last, so the heuristics above can't
    # recompute away values that demonstrably booted.
    if honored_proven:
        for _pk in ("num_gpu", "kv_cache_type", "kv_cache_type_v",
                    "num_batch", "n_ubatch", "num_thread"):
            if proven.get(_pk) is not None:
                out[_pk] = proven[_pk]
        # Same sanity llama.cpp enforces on its own flags.
        if out["num_batch"] > out["num_ctx"]:
            out["num_batch"] = max(512, out["num_ctx"])
            notes.append(f"batch: clamped to {out['num_batch']} (was larger than ctx {out['num_ctx']:,}).")

    # -- MTP draft length (from measured acceptance) --
    if out["spec_strategy"] == "draft-mtp":
        try:
            _cur_n = int(_s_tune.get("spec_draft_n_max") or 0)
        except (TypeError, ValueError):
            _cur_n = 0
        out["spec_draft_n_max"], _dl_note = _draft_length_choice(model_path, _cur_n)
        notes.append(f"speculative: {_dl_note}.")
    else:
        out["spec_draft_n_max"] = 0

    # -- MoE expert cache (llama.cpp #29887) --
    # Never switched on by the tuner. Measured on a 4090 (PCIe 4.0 x16, 82 GB/s
    # RAM) with a Q8 35B-A3B, 25 of 41 expert layers on CPU, warm A/B/A runs:
    # a 2.5 GB cache decoded ~40% SLOWER (27 vs 45-50 tok/s). The PR's gains
    # came from much larger caches on bigger cards, which here would cost the
    # context. A value set by hand is kept, and its VRAM is priced below.
    try:
        cache_mb = float(max(int(_s_tune.get("moe_cache_mib") or 0), 0)) if out["n_cpu_moe"] > 0 else 0.0
    except (TypeError, ValueError):
        cache_mb = 0.0
    if cache_mb:
        notes.append(f"MoE expert cache: {cache_mb:.0f} MiB kept from Settings (its VRAM comes out of context).")

    # -- Exact fit (llama-fit-params) --
    # llama.cpp's own estimate of model + KV + compute per device for this exact
    # config (no model load, ~0.6 s). It replaces the ~8% blanket headroom with
    # a measured number, and the slack becomes context. Things the tool can't
    # see (vision projector, speculative drafts, the MoE cache) are added on.
    _ctx_floor_exact = min_ctx if min_ctx > 0 else 4096
    exact_mem = None          # (used MiB, usable MiB) when llama-fit-params ran
    if bool(_s_tune.get("exact_fit", True)) and src == "gguf" and not out.get("does_not_fit"):
        _b = int(out.get("num_batch") or 512)
        _ub = int(out.get("n_ubatch") or 0) or min(max(_b // 2, 512), 4096)
        _kk = out.get("kv_cache_type") or "q8_0"
        _kv2 = out.get("kv_cache_type_v") or _kk
        _flash = bool(_s_tune.get("flash_attn", True))

        def _fit(c: int, ub: int = 0, b: int = 0):
            # ubatch/batch default to the CURRENT plan's (the scan below may lower _ub).
            return _fit_params_measure(model_path, c, ngl=int(out.get("num_gpu") or 99),
                                       n_cpu_moe=int(out.get("n_cpu_moe") or 0), kv_k=_kk,
                                       kv_v=_kv2, ubatch=ub or _ub, batch=b or _b, flash=_flash)

        c0 = int(out["num_ctx"])
        m0 = _fit(c0)
        if m0:
            devs = list(m0["devices"])
            # Memory the new load may use on each device: free now, plus what
            # the running instance holds (a reload frees it), capped by a
            # user-set VRAM tier, minus a small margin (+ the GPU reserve on
            # the display card).
            try:
                _inst_vram = _running_instance_mem_mb()[1]
            except Exception:
                _inst_vram = 0.0
            tier_gb = float(_s_tune.get("vram_tier_gb") or 0)
            avail: dict = {}
            for i, d in enumerate(devs):
                g = tune_gpus[i] if i < len(tune_gpus) else tune_gpus[-1]
                free_mb = float(g.get("free_gb") or 0) * 1024
                if i == 0:
                    free_mb += _inst_vram
                    if tier_gb > 0 and len(devs) == 1:
                        others = float(g.get("total_gb") or tier_gb) * 1024 - free_mb
                        free_mb = min(free_mb, tier_gb * 1024 - max(others, 0.0))
                avail[d] = free_mb - 320.0 - (vram_reserve_gb * 1024 if i == 0 else 0.0)
            _mtp_now = (mtp_fixed_mb + mtp_compute_per_512 * _ub / 512) if mtp_mb else 0.0
            extras_fixed = mmproj_mb + draft_mb + _mtp_now + cache_mb
            draft_mb_tok = draft_kv_per_tok / (1024 * 1024)

            def _need(m: dict, c: int) -> dict:
                return {d: m["devices"][d] + ((extras_fixed + draft_mb_tok * c) if i == 0 else 0.0)
                        for i, d in enumerate(devs)}

            alt = max(4096, c0 // 2) if c0 > 8192 else c0 * 2

            def _slopes(ma: dict, mb: dict) -> dict:
                return {d: (ma["devices"][d] - mb["devices"].get(d, ma["devices"][d])) / (c0 - alt)
                        + (draft_mb_tok if i == 0 else 0.0) for i, d in enumerate(devs)}

            def _reach(ma: dict, sl: dict) -> int:
                """Context llama.cpp's estimate says fits, extrapolated from c0."""
                n = _need(ma, c0)
                g = min(((avail[d] - n[d]) / sl[d] for d in devs if sl.get(d, 0) > 0), default=0.0)
                return min(max(int((c0 + g) // 1024 * 1024), _ctx_floor_exact), _FIT_CTX_CAP)

            m1 = _fit(alt)
            slopes = _slopes(m0, m1) if m1 else {}
            # Some architectures keep a compute buffer that grows with context ×
            # ubatch (Inkling 2's dense relative-position bias: 12.9 GB at 128k
            # with ubatch 512, 3.2 GB with 128). There a smaller ubatch buys far
            # more context than it costs in prompt speed, so halve it while
            # llama.cpp's estimate says that lets context grow by more than a
            # tenth (floor 64). An ordinary model's compute buffer barely moves
            # with context, so this costs it nothing: the two probes above decide.
            if m1 and not honored_proven:
                _d0 = devs[0]
                _comp_slope = ((m0.get("compute", {}).get(_d0, 0.0) - m1.get("compute", {}).get(_d0, 0.0))
                               / (c0 - alt))
                if _comp_slope * max(c0, alt) > 512:
                    _best, _ub_from = _reach(m0, slopes), _ub
                    while _ub > 64:
                        _cand = max(64, _ub // 2)
                        _ma, _mb = _fit(c0, ub=_cand), _fit(alt, ub=_cand)
                        if not (_ma and _mb):
                            break
                        _sl = _slopes(_ma, _mb)
                        _r = _reach(_ma, _sl)
                        if _r < _best * 1.1:
                            break
                        _best, _ub, m0, m1, slopes = _r, _cand, _ma, _mb, _sl
                    if _ub != _ub_from:
                        out["n_ubatch"] = _ub
                        _mtp_now = (mtp_fixed_mb + mtp_compute_per_512 * _ub / 512) if mtp_mb else 0.0
                        extras_fixed = mmproj_mb + draft_mb + _mtp_now + cache_mb
                        notes.append(
                            f"exact fit: this model's compute buffer grows with context × ubatch "
                            f"({_comp_slope * 1024:.0f} MiB per 1k tokens at ubatch {_ub_from}), so "
                            f"ubatch {_ub_from} → {_ub} buys context (prompts read a little slower).")
            n0 = _need(m0, c0)
            room = {d: avail[d] - n0[d] for d in devs}
            grow = min((room[d] / slopes[d] for d in devs if slopes.get(d, 0) > 0), default=0.0)
            target = int((c0 + grow) // 1024 * 1024)
            target = min(max(target, _ctx_floor_exact), _FIT_CTX_CAP)
            if honored_proven:
                target = max(target, c0)          # a booted combo is trusted, never shrunk
            # Verify the extrapolation and back off if it overshoots.
            for _ in range(3):
                if target == c0:
                    over = max(n0[d] - avail[d] for d in devs)
                    break
                mt = _fit(target)
                if not mt:
                    over, target = 0.0, c0
                    break
                nt = _need(mt, target)
                over = max(nt[d] - avail[d] for d in devs)
                if over <= 0:
                    break
                step = max(slopes.values(), default=0.0) or 0.05
                target = max(_ctx_floor_exact, int((target - over / step - 1024) // 1024 * 1024))
            if over > 0 and target != c0 and all(n0[d] <= avail[d] for d in devs):
                target, over = c0, 0.0            # unverified growth: keep the plan that fits
            if over > 0 and not honored_proven and n_layer:
                # Context is already at its floor and the plan still doesn't
                # fit: move weights off the GPU (dense layers or MoE expert
                # layers) until llama.cpp's own estimate says it does.
                shed_from = (out["num_gpu"], out["n_cpu_moe"])
                for _ in range(5):
                    if is_moe and dense_share:
                        per_layer = size_mb * (1 - dense_share) / n_layer
                        if out["n_cpu_moe"] >= n_layer:
                            break
                        out["n_cpu_moe"] = min(n_layer, out["n_cpu_moe"]
                                               + int((over + 256) // max(per_layer, 1)) + 1)
                    else:
                        per_layer = size_mb / n_layer
                        cur = out["num_gpu"] if out["num_gpu"] < 99 else n_layer
                        if cur <= 1:
                            break
                        out["num_gpu"] = max(1, cur - int((over + 256) // max(per_layer, 1)) - 1)
                    mt = _fit(target)
                    if not mt:
                        break
                    over = max(v - avail[d] for d, v in _need(mt, target).items())
                    if over <= 0:
                        break
                if (out["num_gpu"], out["n_cpu_moe"]) != shed_from:
                    what = (f"n_cpu_moe {shed_from[1]} → {out['n_cpu_moe']}" if is_moe
                            else f"GPU layers {min(shed_from[0], n_layer)} → {out['num_gpu']} of {n_layer}")
                    notes.append(f"exact fit: still over at {target:,} tokens — moved weights off the "
                                 f"GPU ({what}) until llama.cpp's estimate fits.")
            if over > 0 and not honored_proven:
                notes.append(
                    f"exact fit: even {target:,} tokens needs {over:.0f} MiB more than this GPU has "
                    f"free — expect a boot failure; free VRAM or lower the context.")
                # llama.cpp's own estimate says this plan can't load: never save
                # it (_save_model_config skips does_not_fit) or apply it unasked.
                out["does_not_fit"] = True
            used = sum(_need(_fit(target) or m0, target).values())
            total_avail = sum(avail.values())
            exact_mem = (used, total_avail)
            if target != c0:
                notes.append(
                    f"exact fit (llama-fit-params): ctx {c0:,} → {target:,} — "
                    f"{used:,.0f} of {total_avail:,.0f} MiB usable across {len(devs)} device(s), "
                    f"replacing the ~8% estimate headroom with measured memory.")
                out["num_ctx"] = target
            else:
                notes.append(f"exact fit (llama-fit-params): {c0:,} tokens confirmed — "
                             f"{used:,.0f} of {total_avail:,.0f} MiB usable.")
            # Context is at the ceiling and memory is left: on MoE, pull expert
            # layers back from system RAM — every reply gets faster.
            if (is_moe and dense_share and n_layer and out["n_cpu_moe"] > 0
                    and out["num_ctx"] >= _FIT_CTX_CAP):
                per_layer = size_mb * (1 - dense_share) / n_layer
                shed_from_moe = out["n_cpu_moe"]

                def _fits_now() -> bool:
                    mt = _fit(out["num_ctx"])
                    return bool(mt) and all(v <= avail[d] for d, v in _need(mt, out["num_ctx"]).items())

                mt = _fit(out["num_ctx"])
                if mt:
                    spare = min(avail[d] - v for d, v in _need(mt, out["num_ctx"]).items())
                    back = int(spare // max(per_layer, 1))
                    if back > 0:
                        out["n_cpu_moe"] = max(0, out["n_cpu_moe"] - back)
                        # llama.cpp has the last word: step back until it fits.
                        while out["n_cpu_moe"] < shed_from_moe and not _fits_now():
                            out["n_cpu_moe"] += 1
                if out["n_cpu_moe"] < shed_from_moe:
                    _mf = _fit(out["num_ctx"])
                    if _mf:
                        exact_mem = (sum(_need(_mf, out["num_ctx"]).values()), total_avail)
                    notes.append(f"context is at the {_FIT_CTX_CAP:,} ceiling, so spare VRAM pulled expert "
                                 f"layers back from RAM: n_cpu_moe {shed_from_moe} → {out['n_cpu_moe']}.")
            # Context is maxed and memory is left: spend it on prompt speed.
            # ubatch 2048 roughly doubles prefill on big cards but its compute
            # buffer costs ~2 GB, so it only wins when context can't grow.
            cpu_off = out["n_cpu_moe"] > 0 or (not is_moe and out["num_gpu"] < 99)
            if out["num_ctx"] >= _FIT_CTX_CAP and not cpu_off and _ub < 2048:
                mu = _fit(out["num_ctx"], ub=2048, b=2048)
                if mu and all(v <= avail[d] for d, v in _need(mu, out["num_ctx"]).items()):
                    out["num_batch"], out["n_ubatch"] = 2048, 2048
                    notes.append("prompt batch: ubatch 2048 — context is at the ceiling and memory "
                                 "is left over, so it buys faster prompt processing instead.")
        else:
            notes.append("exact fit: llama-fit-params unavailable — using the analytic estimate.")

    out["moe_cache_mib"] = int(cache_mb)

    # -- Measured ceiling (dense) --
    # A context the load-time speed check measured slow on this machine (part
    # of the model ran from system RAM) outranks every estimate above.
    if not is_moe:
        _fit_cap = _fit_ceiling_for(model_path, _s_tune)
        if _fit_cap and int(out["num_ctx"]) > _fit_cap:
            notes.append(f"measured ceiling: larger contexts ran slower right after loading on this "
                         f"machine, so context is held at {_fit_cap:,}.")
            out["num_ctx"] = _fit_cap

    # -- Speed verdict --
    # Judged at the user's usual chat size: past the offload the solver already
    # traded against context (MoE), the window size itself costs no decode
    # speed — so falling short here means the model is the limit, and cutting
    # context wouldn't help.
    def _pred_out(ctx_tokens: int) -> float:
        return _predict(min(typical_fill, int(ctx_tokens)), n_off=out["n_cpu_moe"],
                        num_gpu=out["num_gpu"], kv_k=out.get("kv_cache_type") or "q8_0",
                        kv_v=out.get("kv_cache_type_v") or "", spec=out["spec_strategy"],
                        calib_factor=calib)

    pred_now = _pred_out(int(out["num_ctx"]))
    if pred_now < min_tok_s:
        out["speed_floor_unreachable"] = True
        notes.append(
            f"speed floor not met: ≈ {pred_now:.0f} tok/s at your usual chat size (floor {min_tok_s:g}). "
            f"The weights are the limit, not the context window — a smaller context wouldn't be "
            f"faster at the same chat size; only a smaller model or quant would.")
    out["predicted_tok_s"] = round(pred_now, 1)
    notes.append(f"predicted decode ≈ {pred_now:.0f} tok/s at your usual chat size (~{typical_fill:,} tokens).")

    # -- Plain-language summary (the notes are the full reasoning) --
    summary = [f"Context: {out['num_ctx']:,} tokens"
               + (f" (was {min_ctx:,})" if min_ctx and min_ctx != out["num_ctx"] else "")]
    _usual = f"{typical_fill / 1000:.0f}k" if typical_fill >= 1000 else str(typical_fill)
    if out.get("speed_floor_unreachable"):
        summary.append(f"Speed: about {pred_now:.0f} tok/s at your usual chat size (~{_usual} tokens) — "
                       f"below your {min_tok_s:g} tok/s minimum; this model can't go faster on this GPU")
    else:
        summary.append(f"Speed: about {pred_now:.0f} tok/s at your usual chat size (~{_usual} tokens; "
                       f"your minimum: {min_tok_s:g})")
    if out["n_cpu_moe"] > 0 and n_layer:
        summary.append(f"{out['n_cpu_moe']} of {n_layer} expert layers run from system RAM")
    elif not is_moe and n_layer and out["num_gpu"] < 99 and out["num_gpu"] < n_layer:
        summary.append(f"{n_layer - out['num_gpu']} of {n_layer} layers run from system RAM (slow)")
    if out["spec_strategy"] == "draft-mtp":
        summary.append(f"MTP drafting {out.get('spec_draft_n_max') or 3} tokens ahead")
    summary.append(f"Memory checked with llama.cpp's own estimate ({exact_mem[0] / 1024:.1f} of "
                   f"{exact_mem[1] / 1024:.1f} GB)" if exact_mem
                   else "Memory estimated (llama-fit-params not available)")
    out["summary"] = summary
    if out["num_batch"] > out["num_ctx"]:
        out["num_batch"] = max(512, out["num_ctx"])

    out["notes"] = " ".join(notes)
    return out


_MAYA_DRAFT_MIN_CTX = 131072  # below this the drafter's VRAM goes to context instead


def _maya_setup_gib(model_path: str, ctx: int, drafter: str = "", vision_gpu: bool = False) -> float:
    """VRAM (GiB) a maya setup takes, by maya_fit's arithmetic without the
    margin. It matched the measured use within 0.05 GiB (192k and 224k with
    the drafter, 262k without)."""
    meta = _maya_gguf_meta(model_path)
    gib = (_maya_weights_gib(model_path, meta) + _MAYA_FIXED_GIB
           + max(int(ctx or 0), 0) * _maya_kv_bytes_per_token(meta) / _GIB)
    if drafter:
        try:
            gib += os.path.getsize(drafter) / _GIB
        except OSError:
            gib += 0.6
        gib += _MAYA_DRAFT_EXTRA_GIB + 4096 * _MAYA_DRAFT_CTX / _GIB
    if vision_gpu:
        gib += _MAYA_VISION_GPU_GIB
    return gib


def _maya_auto_tune(model_path: str, vram_gb: float, min_ctx: int = 0) -> dict:
    """auto_tune for a model maya runs. llama.cpp's knobs (offload, batch, KV
    type) don't apply; what's left is the context and whether the drafter
    rides along. The drafter speeds code and edits 1.5-2x but costs ~1.3 GiB,
    so on its own the tuner uses it when at least 128k context still fits
    beside it, and a user's larger saved context (min_ctx) wins. The user's own
    pick for this model wins over both: the helper chosen in Settings
    (Speculative decoding: dspark) gets the context that fits beside it, "off"
    or an unloaded decoder gets the whole room."""
    s = get_settings()
    out = {
        "num_gpu": 99, "n_cpu_moe": 0, "kv_cache_type": "q8_0", "kv_cache_type_v": "",
        "num_ctx": 32768, "num_batch": 512, "n_ubatch": 0, "num_thread": 0, "flash_attn": True,
        "spec_strategy": "off", "no_warmup": False, "enable_metrics": False, "quant_downshift": "",
        "engine": "maya",
    }
    saved = _models_config().get(model_path)
    saved = saved if isinstance(saved, dict) else {}
    manual = saved.get("spec_strategy_source") == "manual"
    saved_spec = str(saved.get("spec_strategy") or "").strip().lower()
    helper_off = (manual and saved_spec == "off") \
        or str(s.get("spec_draft_mode") or "auto").strip().lower() == "off"
    helper_asked = manual and saved_spec in ("dspark", "dflash") and not helper_off
    drafter = "" if helper_off else _maya_drafter_for(model_path)
    vision_gpu = _maya_vision_on_gpu(s)
    running = (_llama.engine() == "maya"
               and os.path.normcase(_llama.loaded_model() or "") == os.path.normcase(model_path))
    launch = _llama.maya_launch() if running else {}
    keep_unless_bigger = 0
    if running and launch:
        # Its VRAM is what the running setup needs plus what's free now: no
        # Windows per-process counter to trust.
        try:
            free_now = float(detect_vram_gb().get("free_total_gb") or 0)
        except Exception:
            free_now = 0.0
        vram = _maya_setup_gib(model_path, int(launch.get("ctx") or 0), str(launch.get("drafter") or ""),
                               bool(launch.get("vision_gpu"))) + free_now
        with_helper = bool(launch.get("drafter"))
        switched = (helper_asked and drafter and not with_helper) or (helper_off and with_helper)
        if not switched:
            # Running as the user wants: grow when room appears (other apps
            # closed), never shrink it. A smaller pick would only reload it for
            # nothing at boot; the launch trims a context that no longer fits.
            keep_unless_bigger = int(saved.get("num_ctx") or 0)
    else:
        # The budget is the VRAM free for this model, measured (a running
        # model's share added back). Callers pass the VRAM tier when one is set,
        # and the tier is the card's size, not free memory (24 on a 4090 whose
        # desktop holds 1.5): it only caps, and the argument is the fallback
        # when nothing can be read.
        try:
            tier = max(float(s.get("vram_tier_gb") or 0), 0.0)
        except (TypeError, ValueError):
            tier = 0.0
        try:
            vram = float(_free_vram_gb_for_tune() or 0)
        except Exception:
            vram = 0.0
        try:
            total = float(detect_vram_gb().get("total_gb") or 0)
        except Exception:
            total = 0.0
        if vram <= 0:
            vram = max(float(vram_gb or 0), 0.0) * 0.92
        elif tier > 0 and total > tier:
            vram = max(0.0, vram - (total - tier))
    if vram <= 0:
        out["notes"] = "maya engine: no free VRAM reading, so the safe default context is used."
        return out
    fit = maya_fit(model_path, vram, drafter, vision_gpu)
    plain, draft = fit["plain_ctx"], fit["draft_ctx"]
    notes = [f"maya engine: {vram:.1f} GB free; every expert stays in VRAM up to "
             f"{plain:,} tokens of context ({fit['kv_bytes_per_token'] // 1024} KiB a token)."]
    if plain < 32768:
        # The experts don't all fit: maya streams them from RAM through a VRAM
        # cache. It works, slower; keep the context modest and the drafter off.
        out["num_ctx"] = min(fit["trained_ctx"], 32768)
        notes.append("the experts don't all fit in VRAM, so maya keeps a cache of them there "
                     "and reads the rest from RAM (slower). context 32k, drafter off.")
    elif drafter and ((helper_asked and draft >= 32768)
                      or (not helper_asked and draft >= _MAYA_DRAFT_MIN_CTX and min_ctx <= draft)):
        out["num_ctx"], out["spec_strategy"] = draft, "dspark"
        notes.append(f"drafter on ({os.path.basename(drafter)}): {draft:,} context fits beside it. "
                     f"a larger context switches it off at load.")
    else:
        if min_ctx > plain:
            notes.append(f"your saved context {min_ctx:,} doesn't fit with every expert in VRAM; "
                         f"using {plain:,}.")
        out["num_ctx"] = max(8192, plain)
        if drafter:
            notes.append("drafter off: the context needs its VRAM.")
    if keep_unless_bigger and out["num_ctx"] <= keep_unless_bigger:
        out["num_ctx"] = keep_unless_bigger
        out["spec_strategy"] = str(saved.get("spec_strategy") or out["spec_strategy"])
        notes.append("this model is running: its setup stays (it only grows when more VRAM is free).")
    out["notes"] = "\n".join(notes)
    return out


def _tune_budget_gb(explicit_gb: float = 0.0) -> float:
    """The VRAM budget every tuner entry point uses: a tier the caller passed,
    else the saved tier, else live free VRAM plus what the running instance
    holds (a reload frees it)."""
    try:
        if float(explicit_gb or 0) > 0:
            return float(explicit_gb)
        tier = float(get_settings().get("vram_tier_gb") or 0)
    except (TypeError, ValueError):
        tier = 0.0
    return tier if tier > 0 else _free_vram_gb_for_tune()
