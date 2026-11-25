# SPDX-License-Identifier: AGPL-3.0-only
"""The model advisor: per-model runtime profiles, load-time speed samples, the
fit check for how much fits on the GPU, and trial suggestions.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import threading
import time
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from pathlib import Path


MODEL_RUNTIME_FILE = DATA / "model_runtime_profiles.json"


_MODEL_RUNTIME_LOCK = threading.Lock()


def _model_profile_key(model: str) -> str:
    name = Path(str(model or "unknown")).name or "unknown"
    return name[:180]


_MODEL_ADVISOR_BASELINE_TURNS = 25


_MODEL_ADVISOR_BASELINE_SESSIONS = 3


_MODEL_ADVISOR_TRIAL_TURNS = 10


_MODEL_ADVISOR_TRIAL_MAX_AGE_S = 7 * 24 * 3600   # a measuring trial left idle for a week closes itself silently


_MODEL_ADVISOR_CONFIG_KEYS = (
    "num_ctx", "num_gpu", "num_batch", "n_ubatch", "n_cpu_moe",
    "num_thread", "n_parallel", "kv_cache_type", "kv_cache_type_v",
    "flash_attn", "spec_strategy",
)


# Newer tuner outputs. They join the config snapshot only when set (non-zero),
# so configs recorded before they existed keep their signature and history.
_MODEL_ADVISOR_OPTIONAL_KEYS = ("spec_draft_n_max", "moe_cache_mib")


# What the running llama-server was actually launched with, where settings
# alone can't say (spec_draft_n_max 0 = "auto"). Set at spawn, recorded with
# every turn so speed calibration knows which draft length produced it.
_RUNTIME_LAUNCH: dict = {"spec_draft_n": 0}


_MODEL_ADVISOR_LABELS = {
    "num_ctx": "Context window", "num_gpu": "GPU layers",
    "num_batch": "Batch size", "n_ubatch": "Micro-batch",
    "n_cpu_moe": "CPU MoE experts", "num_thread": "CPU threads",
    "n_parallel": "Parallel sequences", "kv_cache_type": "KV cache",
    "kv_cache_type_v": "V cache", "flash_attn": "Flash attention",
    "spec_strategy": "Speculative decoding",
    "spec_draft_n_max": "Draft length", "moe_cache_mib": "MoE expert cache",
}


def _model_runtime_config(settings: dict | None = None) -> dict:
    """Small content-free snapshot used to keep unlike configurations apart."""
    s = settings if isinstance(settings, dict) else get_settings()
    out = {}
    for key in _MODEL_ADVISOR_CONFIG_KEYS:
        value = s.get(key)
        if isinstance(value, bool):
            out[key] = value
        elif key in {"kv_cache_type", "kv_cache_type_v", "spec_strategy"}:
            out[key] = str(value or "")[:40]
        else:
            try:
                out[key] = int(value or 0)
            except (TypeError, ValueError):
                out[key] = 0
    for key in _MODEL_ADVISOR_OPTIONAL_KEYS:
        try:
            value = int(s.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value:
            out[key] = value
    # "auto" draft length: record the length the server was launched with.
    # An explicit spec_draft_n_max is already in the snapshot above.
    if (out.get("spec_strategy") == "draft-mtp" and not out.get("spec_draft_n_max")
            and _RUNTIME_LAUNCH.get("spec_draft_n")):
        out["spec_draft_n_eff"] = int(_RUNTIME_LAUNCH["spec_draft_n"])
    return out


def _model_config_signature(snapshot: dict) -> str:
    raw = json.dumps(snapshot or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


_HARNESS_INTERVENTION_KEYS = (
    "context_retry", "server_retry", "reasoning_guard", "tool_call_repair",
    "empty_reply_retry", "auto_continue", "loop_guard",
)


def _sanitize_interventions(value) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    return {
        key: max(0, int(source.get(key, 0) or 0))
        for key in _HARNESS_INTERVENTION_KEYS
    }


def _model_observed(row: dict, observations: list[dict] | None = None) -> dict:
    source = observations if observations is not None else None
    if source is not None:
        turns = len(source)
        completed = sum(int(bool(x.get("completed"))) for x in source)
        prompt_tokens = sum(max(0, int(x.get("prompt_tokens", 0) or 0)) for x in source)
        output_tokens = sum(max(0, int(x.get("output_tokens", 0) or 0)) for x in source)
        rounds = sum(max(0, int(x.get("rounds", 0) or 0)) for x in source)
        calls = sum(max(0, int(x.get("tool_calls", 0) or 0)) for x in source)
        errors = sum(max(0, int(x.get("tool_errors", 0) or 0)) for x in source)
        fallbacks = sum(int(bool(x.get("native_fallback"))) for x in source)
        elapsed_ms = sum(max(0.0, float(x.get("elapsed_ms", 0) or 0)) for x in source)
        prefill_ms = sum(max(0.0, float(x.get("prefill_ms", 0) or 0)) for x in source)
        eval_ms = sum(max(0.0, float(x.get("eval_ms", 0) or 0)) for x in source)
        peaks = sorted(max(0, int(x.get("peak_context_tokens", 0) or 0)) for x in source)
        compactions = sum(max(0, int(x.get("compactions", 0) or 0)) for x in source)
        compaction_failures = sum(max(0, int(x.get("compaction_failures", 0) or 0)) for x in source)
        interventions = {
            key: sum(_sanitize_interventions(x.get("interventions")).get(key, 0)
                     for x in source)
            for key in _HARNESS_INTERVENTION_KEYS
        }
    else:
        turns = max(0, int(row.get("turns", 0) or 0))
        completed = max(0, int(row.get("completed_turns", 0) or 0))
        prompt_tokens = max(0, int(row.get("prompt_tokens", 0) or 0))
        output_tokens = max(0, int(row.get("output_tokens", 0) or 0))
        rounds = max(0, int(row.get("rounds", 0) or 0))
        calls = max(0, int(row.get("tool_calls", 0) or 0))
        errors = max(0, int(row.get("tool_errors", 0) or 0))
        fallbacks = max(0, int(row.get("native_fallback_turns", 0) or 0))
        elapsed_ms = max(0.0, float(row.get("elapsed_s", 0) or 0)) * 1000.0
        prefill_ms = max(0.0, float(row.get("prefill_ms", 0) or 0))
        eval_ms = max(0.0, float(row.get("eval_ms", 0) or 0))
        peaks = sorted(max(0, int(x.get("peak_context_tokens", 0) or 0))
                       for x in (row.get("observations") or []) if isinstance(x, dict))
        compactions = max(0, int(row.get("compactions", 0) or 0))
        compaction_failures = max(0, int(row.get("compaction_failures", 0) or 0))
        interventions = _sanitize_interventions(row.get("interventions"))
    denom_turns = max(1, turns)
    p90_peak = 0
    if peaks:
        p90_peak = peaks[min(len(peaks) - 1, max(0, math.ceil(len(peaks) * 0.90) - 1))]
    avg_peak = (sum(peaks) / len(peaks)) if peaks else (
        float(row.get("peak_context_sum", 0) or 0) / denom_turns)
    decode_seconds = eval_ms / 1000.0
    prompt_seconds = prefill_ms / 1000.0
    elapsed_seconds = elapsed_ms / 1000.0
    return {
        "completion_rate": round(completed / denom_turns, 3),
        "tool_error_rate": round(errors / max(1, calls), 3),
        "avg_rounds": round(rounds / denom_turns, 2),
        "output_tok_s": round(output_tokens / (decode_seconds or elapsed_seconds or 0.001), 2),
        "prompt_tok_s": round(prompt_tokens / (prompt_seconds or 0.001), 2) if prefill_ms else 0.0,
        "avg_peak_context_tokens": round(avg_peak, 1),
        "p90_peak_context_tokens": int(p90_peak or row.get("peak_context_tokens", 0) or 0),
        "native_fallback_rate": round(fallbacks / denom_turns, 3),
        "compactions": compactions,
        "compaction_failures": compaction_failures,
        "interventions": interventions,
    }


def _model_runtime_profiles() -> dict:
    data = load_json(MODEL_RUNTIME_FILE, {"version": 1, "models": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "models": {}}
    out = copy.deepcopy(data)
    for row in (out.get("models") or {}).values():
        turns = max(1, int(row.get("turns", 0) or 0))
        row["observed"] = _model_observed(row)
        for cfg in (row.get("configs") or {}).values():
            if not isinstance(cfg, dict):
                continue
            cfg["observed"] = _model_observed(cfg, cfg.get("observations") or None)
            cfg_turns = int(cfg.get("turns", 0) or 0)
            cfg_sessions = len(cfg.get("sessions") or [])
            cfg["confidence"] = ("high" if cfg_turns >= _MODEL_ADVISOR_BASELINE_TURNS
                                 and cfg_sessions >= _MODEL_ADVISOR_BASELINE_SESSIONS
                                 else "warming_up")
        row["confidence"] = "high" if turns >= 25 else "medium" if turns >= 8 else "warming_up"
        advice = []
        observed = row["observed"]
        ctx = int(row.get("last_context", 0) or 0)
        if turns >= 8:
            if observed["native_fallback_rate"] >= 0.25:
                advice.append("Keep text-tool repair enabled; native tool parsing has required frequent fallback.")
            if observed["completion_rate"] < 0.8:
                advice.append("Keep conservative defaults; incomplete turns are too frequent for automatic aggressiveness increases.")
            if ctx and observed["p90_peak_context_tokens"] >= ctx * 0.82:
                advice.append("Normal work approaches the context ceiling; prefer earlier compaction or smaller active tool bundles.")
            if int(row.get("tool_calls", 0) or 0) >= 8 and observed["tool_error_rate"] >= 0.25:
                advice.append("Tool errors are elevated; inspect capability health and action outcomes before changing model settings.")
        row["advice"] = advice
    return out


def _advisor_changes(before: dict | None, updates: dict | None) -> list:
    """Plain before/after rows for every setting a recommendation touches."""
    before = before if isinstance(before, dict) else {}
    updates = updates if isinstance(updates, dict) else {}
    return [{"key": key, "label": _MODEL_ADVISOR_LABELS.get(key, key),
             "from": before.get(key), "to": updates[key]}
            for key in _MODEL_ADVISOR_CONFIG_KEYS + _MODEL_ADVISOR_OPTIONAL_KEYS
            if key in updates]


def _advisor_trial_public(row: dict, active_config: str) -> dict | None:
    trial = row.get("trial") if isinstance(row.get("trial"), dict) else None
    if not trial or trial.get("status") in {"kept", "rolled_back", "aborted"}:
        return None
    cfg = (row.get("configs") or {}).get(active_config) or {}
    progress = max(0, int(cfg.get("turns", 0) or 0) - int(trial.get("start_turns", 0) or 0))
    status = str(trial.get("status") or "measuring")
    if status == "measuring":
        now = int(time.time())
        if active_config != trial.get("candidate_config"):
            # The user changed settings mid-trial. Never fight them: close the
            # comparison silently and leave their settings exactly as they are.
            trial["status"] = "aborted"
            trial["result"] = ("The model configuration changed while the trial was measuring, "
                               "so the comparison was closed without changing anything.")
            trial["closed_at"] = now
            return None
        if (int(trial.get("applied_at", 0) or 0)
                and now - int(trial.get("applied_at", 0) or 0) > _MODEL_ADVISOR_TRIAL_MAX_AGE_S):
            # Idle trial: the recommended settings stay applied, but no verdict
            # is forced from stale data. Close silently.
            trial["status"] = "aborted"
            trial["result"] = ("The trial expired without enough measured use, "
                               "so the comparison was closed without changing anything.")
            trial["closed_at"] = now
            return None
        elif (active_config == trial.get("candidate_config")
                and progress >= _MODEL_ADVISOR_TRIAL_TURNS):
            recent = [x for x in (cfg.get("observations") or [])
                      if int(x.get("ts", 0) or 0) >= int(trial.get("applied_at", 0) or 0)]
            candidate = _model_observed(cfg, recent[-_MODEL_ADVISOR_TRIAL_TURNS:])
            baseline = trial.get("baseline") or {}
            worse_reasons = []
            if candidate["completion_rate"] + 0.08 < float(baseline.get("completion_rate", 0) or 0):
                worse_reasons.append("more turns failed to finish")
            if candidate["tool_error_rate"] > float(baseline.get("tool_error_rate", 0) or 0) + 0.12:
                worse_reasons.append("tool errors increased")
            old_speed = float(baseline.get("output_tok_s", 0) or 0)
            new_speed = float(candidate.get("output_tok_s", 0) or 0)
            if old_speed > 0 and new_speed > 0 and new_speed < old_speed * 0.82:
                worse_reasons.append("response speed fell by more than 18%")
            status = "worse" if worse_reasons else "stable"
            trial["status"] = status
            trial["candidate"] = candidate
            trial["result"] = ("; ".join(worse_reasons) if worse_reasons
                               else "The ten-turn check found no meaningful loss in speed or reliability.")
            status = str(trial.get("status") or status)
    return {
        "id": str(trial.get("id") or ""), "status": status,
        "progress": min(progress, _MODEL_ADVISOR_TRIAL_TURNS),
        "target": _MODEL_ADVISOR_TRIAL_TURNS,
        "result": str(trial.get("result") or ""),
        # Rebuilt from what was actually applied, so trials started before
        # every setting was listed still show the full before/after.
        "changes": (_advisor_changes(trial.get("previous"), trial.get("updates"))
                    or list(trial.get("changes") or [])),
    }


_LOAD_SAMPLE_CAP = 40


_LOAD_SAMPLE_MAX_AGE_S = 60 * 24 * 3600


_FIT_SLOW = 0.90        # below 90% of a smaller context's speed: it didn't fit


_FIT_SAME = 0.95        # within 5%: same speed


_FIT_CHECK_STEP = 8     # the one-time check loads with 1/8 less context


# Prompt batch sizes change prompt reading, not generation speed.
_FIT_IGNORED_KEYS = ("num_ctx", "num_batch", "n_ubatch")


def _fit_group(snapshot: dict) -> str:
    """Setups that differ only in context and batch size decode alike."""
    return _model_config_signature({k: v for k, v in (snapshot or {}).items()
                                    if k not in _FIT_IGNORED_KEYS})


def _fit_key(model_path: str) -> str:
    return _model_profile_key(Path(str(model_path or "")).stem)


def _record_load_sample(model_path: str, ctx: int, tok_s: float,
                        settings: dict | None = None) -> None:
    if tok_s <= 0 or ctx <= 0 or not model_path:
        return
    s = settings if isinstance(settings, dict) else get_settings()
    sample = {"ts": int(time.time()), "group": _fit_group(_model_runtime_config(s)),
              "ctx": int(ctx), "tok_s": float(tok_s)}
    with _MODEL_RUNTIME_LOCK:
        data = load_json(MODEL_RUNTIME_FILE, {"version": 2, "models": {}})
        if not isinstance(data, dict):
            data = {"version": 2, "models": {}}
        rows = data.setdefault("load_samples", {}).setdefault(_fit_key(model_path), [])
        rows.append(sample)
        data["load_samples"][_fit_key(model_path)] = rows[-_LOAD_SAMPLE_CAP:]
        save_json(MODEL_RUNTIME_FILE, data)


def _fit_speeds(samples, group: str) -> dict:
    """Latest load-time speed per context for one setup (newest evidence wins)."""
    cutoff = int(time.time()) - _LOAD_SAMPLE_MAX_AGE_S
    out: dict = {}
    for x in sorted((x for x in (samples or []) if isinstance(x, dict)),
                    key=lambda x: int(x.get("ts", 0) or 0)):
        if (x.get("group") == group and int(x.get("ts", 0) or 0) >= cutoff
                and int(x.get("ctx", 0) or 0) > 0 and float(x.get("tok_s", 0) or 0) > 0):
            out[int(x["ctx"])] = float(x["tok_s"])
    return out


def _fit_ceiling(speeds: dict) -> int:
    """Largest context that ran at full speed below the smallest one measured
    slow; 0 when no context has been measured slow."""
    slow = [c for c, v in speeds.items()
            if any(c2 < c and v < v2 * _FIT_SLOW for c2, v2 in speeds.items())]
    if not slow:
        return 0
    first_slow = min(slow)
    below = {c: v for c, v in speeds.items() if c < first_slow}
    best = max(below.values())
    fast = [c for c, v in below.items() if v >= best * _FIT_SAME]
    return max(fast) if fast else 0


def _fit_ceiling_for(model_path: str, settings: dict | None = None) -> int:
    try:
        s = settings if isinstance(settings, dict) else get_settings()
        data = load_json(MODEL_RUNTIME_FILE, {})
        samples = ((data.get("load_samples") or {}) if isinstance(data, dict) else {}).get(
            _fit_key(model_path)) or []
        return _fit_ceiling(_fit_speeds(samples, _fit_group(_model_runtime_config(s))))
    except Exception:
        return 0


def _silent_spill_possible() -> bool:
    """Windows' GPU driver spills an oversized load into system RAM instead of
    failing it; elsewhere such a load fails at boot."""
    return os.name == "nt"


def _fit_full_gpu_dense(model_path: str, snapshot: dict) -> bool:
    return (_model_is_dense(model_path) and not int(snapshot.get("n_cpu_moe") or 0)
            and int(snapshot.get("num_gpu") or 99) >= 99)


def _advisor_fit_advice(data: dict, settings: dict) -> dict:
    """The speed-check card: {"recommendation": ...} and/or {"result": ...}.

    Three cases, all from load-time speed of the same setup at different
    contexts: the current context ran slow next to a smaller one (suggest the
    largest fast one); a one-time check at 1/8 less context found no difference
    (suggest going back); or nothing smaller was ever measured on Windows, where
    a silent spill is possible (suggest the one-time check)."""
    model_path = str(settings.get("model_path") or "")
    snap = _model_runtime_config(settings)
    ctx = int(snap.get("num_ctx") or 0)
    if not model_path or not ctx or not _fit_full_gpu_dense(model_path, snap):
        return {}
    key = _fit_key(model_path)
    state = ((data.get("fit_state") or {}).get(key) or {}) if isinstance(data, dict) else {}
    samples = ((data.get("load_samples") or {}).get(key) or []) if isinstance(data, dict) else []
    speeds = _fit_speeds(samples, _fit_group(snap))
    here = speeds.get(ctx)
    if not here:
        return {"result": state.get("result")} if state.get("result") else {}
    dismissed = set(state.get("dismissed") or [])

    def rec(kind: str, target: int, reason: str, tradeoff: str) -> dict:
        rid = hashlib.sha256(f"{key}|{kind}|{ctx}|{target}".encode("utf-8")).hexdigest()[:18]
        if rid in dismissed:
            return {}
        return {"recommendation": {
            "id": rid, "kind": kind, "reason": reason, "tradeoff": tradeoff,
            "updates": {"num_ctx": int(target)},
            "changes": _advisor_changes(snap, {"num_ctx": int(target)}),
        }}

    ceiling = _fit_ceiling(speeds)
    if ceiling and ctx > ceiling:
        slow_pct = round((1 - here / speeds[ceiling]) * 100)
        return rec("fit_down", ceiling,
                   f"Right after loading, this model generated {here:.0f} tok/s at {ctx:,} context but "
                   f"{speeds[ceiling]:.0f} tok/s at {ceiling:,}. {slow_pct}% slower means the larger "
                   f"context doesn't fit in VRAM and part of the model is running from system RAM.",
                   "Less context, full speed. Nothing else about the model changes.")
    check_from = int(state.get("check_from") or 0)
    if check_from > ctx and check_from in speeds and speeds[check_from] >= here * _FIT_SAME:
        return rec("fit_up", check_from,
                   f"The speed check found no difference: {speeds[check_from]:.0f} tok/s at "
                   f"{check_from:,} context and {here:.0f} tok/s at {ctx:,}. The larger context fits "
                   f"in VRAM, so it's safe to go back to it.",
                   "More context at the same speed.")
    if (_silent_spill_possible() and ctx < _FIT_CTX_CAP and not check_from
            and not any(c < ctx for c in speeds)):
        target = max(8192, (ctx - ctx // _FIT_CHECK_STEP) // 1024 * 1024)
        if target < ctx:
            out = rec("fit_check", target,
                      f"On Windows, a model that doesn't quite fit in VRAM still loads, but part of it "
                      f"runs from system RAM and generation slows down with no error. Loading once at "
                      f"{target:,} context and timing it against the {here:.0f} tok/s measured at "
                      f"{ctx:,} shows whether that's happening here.",
                      "One reload at slightly less context. If the speed is the same, the advisor "
                      "suggests going back.")
            if state.get("result"):
                out["result"] = state["result"]
            return out
    return {"result": state.get("result")} if state.get("result") else {}


def _fit_settle(data: dict, settings: dict) -> bool:
    """Close a finished one-time check with a plain result line. True if the
    state changed."""
    model_path = str(settings.get("model_path") or "")
    key = _fit_key(model_path)
    state = (data.get("fit_state") or {}).get(key) if isinstance(data, dict) else None
    if not isinstance(state, dict) or not state.get("check_from"):
        return False
    snap = _model_runtime_config(settings)
    speeds = _fit_speeds((data.get("load_samples") or {}).get(key) or [], _fit_group(snap))
    big, small = int(state["check_from"]), int(state.get("check_to") or 0)
    if big not in speeds or small not in speeds:
        return False
    if speeds[big] < speeds[small] * _FIT_SLOW:
        state["result"] = (f"Speed check: {big:,} context ran at {speeds[big]:.0f} tok/s and "
                           f"{small:,} at {speeds[small]:.0f}. The larger context didn't fit in VRAM, "
                           f"so {small:,} stays and the tuner won't go back above it.")
        state.pop("check_from", None)
        state.pop("check_to", None)
        return True
    return False


_FIT_CTX_CAP = 262144


def _model_is_dense(model_path: str) -> bool:
    """True only when the GGUF header was read and lists no experts. An
    unreadable header says nothing, so callers keep their MoE offload then."""
    if not model_path:
        return False
    try:
        gg = read_gguf_metadata(model_path)
    except Exception:
        return False
    return bool(gg.get("ok")) and int(gg.get("expert_count") or 0) <= 1
