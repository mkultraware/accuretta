# SPDX-License-Identifier: AGPL-3.0-only
"""The model health report for the UI, and the advisor's suggested settings for the loaded model.

Moved out of bridge.py unchanged (briefly appended to accuretta/runtime/model_advisor.py);
bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.settings import get_settings
from accuretta.runtime.auto_tune import _tune_budget_gb
from accuretta.runtime.auto_tune import auto_tune
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _MODEL_ADVISOR_BASELINE_SESSIONS
from accuretta.runtime.model_advisor import _MODEL_ADVISOR_BASELINE_TURNS
from accuretta.runtime.model_advisor import _MODEL_ADVISOR_OPTIONAL_KEYS
from accuretta.runtime.model_advisor import _MODEL_RUNTIME_LOCK
from accuretta.runtime.model_advisor import _advisor_changes
from accuretta.runtime.model_advisor import _advisor_fit_advice
from accuretta.runtime.model_advisor import _advisor_trial_public
from accuretta.runtime.model_advisor import _fit_settle
from accuretta.runtime.model_advisor import _model_config_signature
from accuretta.runtime.model_advisor import _model_observed
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_advisor import _model_runtime_config
from accuretta.runtime.model_config import _proven_cfg
from accuretta.runtime.model_health import _MODEL_ADVISOR_APPLY_KEYS
from accuretta.runtime.model_inspect import inspect_model


def _advisor_suggest_settings(settings: dict, observed: dict) -> dict:
    """Return the hardware planner's exact operational settings, if available."""
    model_path = str(settings.get("model_path") or "").strip()
    if not model_path or not safe_exists(model_path):
        return {}
    try:
        vram = _tune_budget_gb()
        if vram <= 0:
            return {}
        current_ctx = max(8192, int(settings.get("num_ctx") or 8192))
        suggested = auto_tune(
            model_path, vram, profile=inspect_model(model_path), min_ctx=current_ctx,
            proven_cfg=_proven_cfg(model_path))
        return {k: suggested[k] for k in _MODEL_ADVISOR_APPLY_KEYS if k in suggested}
    except Exception:
        return {}


def _advisor_recommendation(model: str, config_key: str, cfg: dict,
                            settings: dict) -> dict | None:
    turns = int(cfg.get("turns", 0) or 0)
    sessions = len(cfg.get("sessions") or [])
    if turns < _MODEL_ADVISOR_BASELINE_TURNS or sessions < _MODEL_ADVISOR_BASELINE_SESSIONS:
        return None
    observed = _model_observed(cfg, cfg.get("observations") or None)
    current = _model_runtime_config(settings)
    suggested = _advisor_suggest_settings(settings, observed)
    updates = {k: v for k, v in suggested.items()
               if k in _MODEL_ADVISOR_APPLY_KEYS and current.get(k) != v}
    # An optional key left at 0 means "auto" and is absent from the snapshot,
    # so absent -> 0 is not a change.
    for key in _MODEL_ADVISOR_OPTIONAL_KEYS:
        if key in updates and not updates[key] and not current.get(key):
            updates.pop(key)
    if not updates:
        return None
    changes = _advisor_changes(current, updates)
    ctx = max(1, int(current.get("num_ctx", 0) or cfg.get("last_context", 0) or 1))
    peak = int(observed.get("p90_peak_context_tokens", 0) or 0)
    pressure = peak / ctx if ctx else 0
    if "num_ctx" in updates and int(updates["num_ctx"] or 0) > ctx:
        reason = (
            f"Across {turns} turns, busy prompts reached {round(pressure * 100)}% of the current context window. "
            "The model and available memory can support more working room with the settings below."
        )
        tradeoff = "More context uses additional memory and very long prompts take longer to process."
    else:
        reason = (
            f"After {turns} turns across {sessions} sessions, this model has a stable local baseline. "
            "The hardware planner found a better fit than the settings currently loaded."
        )
        tradeoff = "The ten-turn trial checks speed and reliability before Accuretta calls the change an improvement."
    rec_id = hashlib.sha256(json.dumps({
        "model": model, "config": config_key, "updates": updates,
    }, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:18]
    return {
        "id": rec_id, "created_at": int(time.time()), "confidence": "high",
        "reason": reason, "tradeoff": tradeoff, "updates": updates,
        "changes": changes, "turns": turns, "sessions": sessions,
        "notified_at": 0,
    }


def _public_model_health() -> dict:
    """Small, path-free snapshot safe for the Settings UI.

    The full runtime profile stays private to the bridge/tool layer.  This view
    exposes only aggregate counters and conservative advice; it never contains
    prompts, generated text, tool arguments/results, or executable paths.
    """
    settings = get_settings()
    enabled = bool(settings.get("passive_model_telemetry", True))
    model = _model_profile_key(settings.get("model_path") or settings.get("model") or "")
    runtime_key = _model_profile_key(settings.get("model") or model)
    active_config = _model_config_signature(_model_runtime_config(settings))
    try:
        with _MODEL_RUNTIME_LOCK:
            data = load_json(MODEL_RUNTIME_FILE, {"version": 2, "models": {}})
            profiles = data.get("models") if isinstance(data, dict) else {}
            row = ((profiles or {}).get(runtime_key) or (profiles or {}).get(model) or {}) if model else {}
            cfg = (row.get("configs") or {}).get(active_config) or {}
            changed = False
            if cfg:
                observed = _model_observed(cfg, cfg.get("observations") or None)
                rec = cfg.get("recommendation") if isinstance(cfg.get("recommendation"), dict) else None
                open_trial = row.get("trial") if isinstance(row.get("trial"), dict) else None
                if open_trial and open_trial.get("status") in {"kept", "rolled_back", "aborted"}:
                    open_trial = None
                if not rec and not open_trial:
                    rec = _advisor_recommendation(model, active_config, cfg, settings)
                    if rec:
                        cfg["recommendation"] = rec
                        changed = True
                if rec and rec.get("dismissed_at"):
                    rec = None
                if rec:
                    # A stored recommendation can go stale once the live settings
                    # catch up with it (e.g. a manual decoder choice landed or
                    # auto-tune adopted the same flags). Drop it instead of
                    # nagging with a change that is no longer a change.
                    _live_cfg = _model_runtime_config(settings)
                    outstanding = {k: v for k, v in (rec.get("updates") or {}).items()
                                   if _live_cfg.get(k) != v}
                    if not outstanding:
                        cfg.pop("recommendation", None)
                        rec = None
                        changed = True
            else:
                observed = _model_observed(row)
                rec = None
            trial_before = copy.deepcopy(row.get("trial"))
            trial = _advisor_trial_public(row, active_config)
            if row.get("trial") != trial_before:
                changed = True
            if _fit_settle(data, settings):
                changed = True
            fit = _advisor_fit_advice(data, settings) if enabled else {}
            if changed:
                save_json(MODEL_RUNTIME_FILE, data)
    except Exception:
        row, cfg, observed, rec, trial, fit = {}, {}, {}, None, None, {}
    current = cfg if cfg else row
    turns = max(0, int(current.get("turns", 0) or 0))
    sessions = len(current.get("sessions") or []) if cfg else 0
    comparison = []
    if isinstance(profiles, dict):
        # Content-free cross-model comparison: aggregate counters only, no
        # paths, config details, or any content. The current model is left to
        # the UI (it already renders its own metrics), so this lists others.
        for name, prow in profiles.items():
            if not isinstance(prow, dict) or name in (runtime_key, model):
                continue
            pturns = max(0, int(prow.get("turns", 0) or 0))
            if pturns < 1:
                continue
            pobs = _model_observed(prow)
            comparison.append({
                "model": str(name)[:180],
                "turns": pturns,
                "completion_rate": float(pobs.get("completion_rate", 0) or 0),
                "tool_success": round(1 - float(pobs.get("tool_error_rate", 0) or 0), 3),
                "output_tok_s": float(pobs.get("output_tok_s", 0) or 0),
                "last_seen": max(0, int(prow.get("last_seen", 0) or 0)),
            })
        comparison.sort(key=lambda m: m["turns"], reverse=True)
        comparison = comparison[:5]
    rec_public = copy.deepcopy(rec) if rec and not trial else None
    if rec_public:
        rec_public["changes"] = (
            _advisor_changes(_model_runtime_config(settings), rec_public.get("updates"))
            or list(rec_public.get("changes") or []))
    advice = []
    context = max(0, int(current.get("last_context", 0) or settings.get("num_ctx") or 0))
    if turns >= 8 and context and float(observed.get("p90_peak_context_tokens", 0) or 0) >= context * 0.82:
        advice.append("Normal work approaches the context ceiling. The setup advisor will check whether this model can safely use more room.")
    if turns >= 8 and float(observed.get("completion_rate", 0) or 0) < 0.8:
        advice.append("Incomplete turns are elevated. Accuretta will keep conservative settings until reliability improves.")
    return {
        "enabled": enabled,
        "model": model,
        "turns": turns,
        "completed_turns": max(0, int(current.get("completed_turns", 0) or 0)),
        "tool_calls": max(0, int(current.get("tool_calls", 0) or 0)),
        "tool_errors": max(0, int(current.get("tool_errors", 0) or 0)),
        "last_context": context,
        "peak_context_tokens": max(0, int(current.get("peak_context_tokens", 0) or 0)),
        "confidence": ("high" if turns >= _MODEL_ADVISOR_BASELINE_TURNS
                       and sessions >= _MODEL_ADVISOR_BASELINE_SESSIONS else "warming_up"),
        "observed": observed,
        "comparison": comparison,
        "advice": advice,
        "learning": {
            "turns_required": _MODEL_ADVISOR_BASELINE_TURNS,
            "sessions_required": _MODEL_ADVISOR_BASELINE_SESSIONS,
            "turns_remaining": max(0, _MODEL_ADVISOR_BASELINE_TURNS - turns),
            "sessions": sessions,
            "sessions_remaining": max(0, _MODEL_ADVISOR_BASELINE_SESSIONS - sessions),
        },
        "recommendation": rec_public,
        "notification_needed": bool(rec and not trial and not rec.get("notified_at")),
        "trial": trial,
        "fit": fit or None,
        "privacy": ("Local performance numbers only, plus tool names and exit codes in a private "
                    "comparison dataset (model_usage_dataset.jsonl). Prompts, replies, tool "
                    "arguments, tool output, commands, files, screenshots, peer addresses, and "
                    "account names are never stored."),
    }
