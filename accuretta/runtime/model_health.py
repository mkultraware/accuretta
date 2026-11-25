# SPDX-License-Identifier: AGPL-3.0-only
"""Acting on the model advisor's suggestions: the keys it may change and applying a fit
change.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import time
import uuid
from accuretta.core.events import broadcast_event
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _MODEL_ADVISOR_CONFIG_KEYS
from accuretta.runtime.model_advisor import _MODEL_ADVISOR_OPTIONAL_KEYS
from accuretta.runtime.model_advisor import _MODEL_RUNTIME_LOCK
from accuretta.runtime.model_advisor import _advisor_fit_advice
from accuretta.runtime.model_advisor import _fit_key
from accuretta.runtime.model_advisor import _model_config_signature
from accuretta.runtime.model_advisor import _model_observed
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_advisor import _model_runtime_config
from accuretta.runtime.model_config import _save_model_config


_MODEL_ADVISOR_APPLY_KEYS = set(_MODEL_ADVISOR_CONFIG_KEYS) | set(_MODEL_ADVISOR_OPTIONAL_KEYS)


def _fit_action(action: str, rec_id: str) -> dict:
    settings = get_settings()
    model_path = str(settings.get("model_path") or "")
    with _MODEL_RUNTIME_LOCK:
        data = load_json(MODEL_RUNTIME_FILE, {"version": 2, "models": {}})
        if not isinstance(data, dict):
            data = {"version": 2, "models": {}}
        advice = _advisor_fit_advice(data, settings).get("recommendation") or {}
        if not advice or advice.get("id") != rec_id:
            return {"ok": False, "error": "recommendation is no longer current"}
        state = data.setdefault("fit_state", {}).setdefault(_fit_key(model_path), {})
        if action == "fit_dismiss":
            state["dismissed"] = (list(state.get("dismissed") or []) + [rec_id])[-20:]
            save_json(MODEL_RUNTIME_FILE, data)
            return {"ok": True}
        target = int(advice["updates"]["num_ctx"])
        current = int(settings.get("num_ctx") or 0)
        state.pop("result", None)
        if advice.get("kind") == "fit_check":
            state["check_from"], state["check_to"] = current, target
        else:
            state.pop("check_from", None)
            state.pop("check_to", None)
        settings["num_ctx"] = target
        save_json(SETTINGS_FILE, settings)
        if model_path:
            _save_model_config(model_path, {"num_ctx": target})
        save_json(MODEL_RUNTIME_FILE, data)
    broadcast_event({"type": "settings:update"})
    return {"ok": True, "settings": settings, "reload": True, "model_path": model_path}


def _model_health_action(action: str, recommendation_id: str = "") -> dict:
    action = str(action or "").strip().lower()
    if action in {"fit_apply", "fit_dismiss"}:
        return _fit_action(action, recommendation_id)
    settings = get_settings()
    model = _model_profile_key(settings.get("model_path") or settings.get("model") or "")
    runtime_key = _model_profile_key(settings.get("model") or model)
    active_config = _model_config_signature(_model_runtime_config(settings))
    with _MODEL_RUNTIME_LOCK:
        data = load_json(MODEL_RUNTIME_FILE, {"version": 2, "models": {}})
        models = data.get("models") if isinstance(data, dict) else {}
        row = ((models or {}).get(runtime_key) or (models or {}).get(model) or {})
        cfg = (row.get("configs") or {}).get(active_config) or {}
        rec = cfg.get("recommendation") if isinstance(cfg.get("recommendation"), dict) else None
        if action in {"seen", "dismiss", "apply"}:
            if not rec or (recommendation_id and rec.get("id") != recommendation_id):
                return {"ok": False, "error": "recommendation is no longer current"}
            if action == "seen":
                rec["notified_at"] = int(time.time())
                save_json(MODEL_RUNTIME_FILE, data)
                return {"ok": True}
            if action == "dismiss":
                rec["dismissed_at"] = int(time.time())
                save_json(MODEL_RUNTIME_FILE, data)
                return {"ok": True}
            updates = {k: v for k, v in (rec.get("updates") or {}).items()
                       if k in _MODEL_ADVISOR_APPLY_KEYS}
            if not updates:
                return {"ok": False, "error": "recommendation has no applicable settings"}
            previous = {k: settings.get(k) for k in updates}
            settings.update(updates)
            if "spec_strategy" in updates:
                settings["enable_speculative"] = updates["spec_strategy"] != "off"
            candidate_config = _model_config_signature(_model_runtime_config(settings))
            start_turns = int(((row.get("configs") or {}).get(candidate_config) or {}).get("turns", 0) or 0)
            row["trial"] = {
                "id": uuid.uuid4().hex[:16], "recommendation_id": rec.get("id"),
                "status": "measuring", "applied_at": int(time.time()),
                "from_config": active_config, "candidate_config": candidate_config,
                "start_turns": start_turns, "updates": updates, "previous": previous,
                "changes": list(rec.get("changes") or []),
                "baseline": _model_observed(cfg, cfg.get("observations") or None),
            }
            save_json(SETTINGS_FILE, settings)
            if settings.get("model_path"):
                _save_model_config(str(settings["model_path"]), updates)
            save_json(MODEL_RUNTIME_FILE, data)
            broadcast_event({"type": "settings:update"})
            return {"ok": True, "settings": settings, "reload": True,
                    "model_path": settings.get("model_path") or "", "trial": row["trial"]}
        trial = row.get("trial") if isinstance(row.get("trial"), dict) else None
        if action in {"undo", "keep"} and trial:
            if action == "keep":
                trial["status"] = "kept"
                trial["closed_at"] = int(time.time())
                save_json(MODEL_RUNTIME_FILE, data)
                return {"ok": True, "settings": settings, "reload": False}
            previous = {k: v for k, v in (trial.get("previous") or {}).items()
                        if k in _MODEL_ADVISOR_APPLY_KEYS}
            settings.update(previous)
            if "spec_strategy" in previous:
                settings["enable_speculative"] = previous["spec_strategy"] != "off"
            trial["status"] = "rolled_back"
            trial["closed_at"] = int(time.time())
            save_json(SETTINGS_FILE, settings)
            if settings.get("model_path"):
                _save_model_config(str(settings["model_path"]), previous)
            save_json(MODEL_RUNTIME_FILE, data)
            broadcast_event({"type": "settings:update"})
            return {"ok": True, "settings": settings, "reload": True,
                    "model_path": settings.get("model_path") or ""}
    return {"ok": False, "error": "unsupported model health action"}
