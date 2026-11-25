# SPDX-License-Identifier: AGPL-3.0-only
"""A model's sampling settings: applying its preset, the no-tools sampling, keeping a
manual spec on switch, noting it reasons, the auto-tune at load, the draft-model match
and the too-old llama.cpp message.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import sys
import time
import traceback
from accuretta.core.events import broadcast_event
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.auto_tune import _tune_budget_gb
from accuretta.runtime.auto_tune import auto_tune
from accuretta.runtime.llama_process import _REASONING_CAP_CACHE
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _MODEL_RUNTIME_LOCK
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_config import _MODEL_CONFIG_KEYS
from accuretta.runtime.model_config import _MODEL_TUNE_KEYS
from accuretta.runtime.model_config import _apply_model_config
from accuretta.runtime.model_config import _models_config
from accuretta.runtime.model_config import _proven_cfg
from accuretta.runtime.model_config import _save_model_config
from accuretta.runtime.model_inspect import inspect_model
from accuretta.runtime.model_presets import _model_preset_entry
from accuretta.runtime.settings_update import _SAMPLING_KEYS
from accuretta.runtime.settings_update import _SETTINGS_LOCK
from accuretta.runtime.settings_update import _model_sampling_profile


def _mark_model_reasoning_observed(model: str) -> None:
    """Persist a path-free, content-free fact learned from a real response."""
    if not model:
        return
    key = _model_profile_key(model)
    with _MODEL_RUNTIME_LOCK:
        try:
            data = load_json(MODEL_RUNTIME_FILE, {"version": 1, "models": {}})
            if not isinstance(data, dict):
                data = {"version": 1, "models": {}}
            row = data.setdefault("models", {}).setdefault(key, {})
            if row.get("reasoning_observed"):
                return
            row["reasoning_observed"] = True
            row["reasoning_observed_at"] = int(time.time())
            save_json(MODEL_RUNTIME_FILE, data)
            _REASONING_CAP_CACHE.clear()
        except Exception:
            pass


def llama_too_old_message(arch: str) -> str:
    return (f"This model needs a newer llama.cpp: your build doesn't support its "
            f"architecture ({arch}). Update llama.cpp, then load it again.")


def draft_match_payload(match: dict) -> dict:
    """JSON-safe summary of a draft match (the full match carries a fingerprint
    with a set of identities; API responses only need these four fields)."""
    return {
        "path": str((match or {}).get("path") or ""),
        "matched": bool((match or {}).get("matched")),
        "score": int((match or {}).get("score") or 0),
        "reasons": list((match or {}).get("reasons") or []),
    }


# What a model switch writes into the shared settings before the spawn (the
# target's tune, projector and preferences). A switch that fails puts these
# back, so the previous model returns with its own setup.
_SWITCH_RESTORE_KEYS = _MODEL_CONFIG_KEYS + ("mmproj_auto", "enable_speculative")


def _model_no_tools_sampling(model_path: str = "") -> dict:
    """Vendor chat sampling for turns without tools, minus any key the user
    has set for this model themselves (their edit wins)."""
    if not model_path:
        try:
            model_path = str(get_settings().get("model_path") or "")
        except Exception:
            return {}
    no_tools = _model_preset_entry(model_path)[2]
    saved = (_models_config().get(model_path) or {}).get("sampling")
    if isinstance(saved, dict):
        no_tools = {k: v for k, v in no_tools.items() if k not in saved}
    return no_tools


def _apply_model_sampling(model_path: str) -> str:
    """Swap the global sampling sliders for this model. Returns "profile",
    "baseline" or "" (nothing changed)."""
    try:
        with _SETTINGS_LOCK:
            s = get_settings()
            profile = _model_sampling_profile(model_path)
            if profile:
                if not isinstance(s.get("sampling_baseline"), dict):
                    s["sampling_baseline"] = {k: s.get(k) for k in _SAMPLING_KEYS}
                s.update(profile)
                save_json(SETTINGS_FILE, s)
                return "profile"
            baseline = s.pop("sampling_baseline", None)
            if isinstance(baseline, dict):
                s.update({k: baseline[k] for k in _SAMPLING_KEYS if k in baseline})
                save_json(SETTINGS_FILE, s)
                return "baseline"
    except Exception:
        traceback.print_exc()
    return ""


def _preserve_manual_model_spec(model_path: str, suggested: dict) -> dict:
    """Keep background tuning from replacing a user's decoder choice."""
    out = dict(suggested or {})
    saved = _models_config().get(model_path)
    if isinstance(saved, dict) and saved.get("spec_strategy_source") == "manual":
        out.pop("spec_strategy", None)
        out["spec_strategy_source"] = "manual"
    return out


def _auto_tune_for_load(model_path: str) -> bool:
    """Auto-tune on model load: restore the model's remembered config if we
    have one; otherwise run the tuner against live free VRAM and remember the
    result (opt-out via auto_tune_on_load). Returns True if settings were
    updated by either path. Never raises — a tuner hiccup must not block a
    model the user explicitly asked to load."""
    try:
        if _apply_model_config(model_path):
            return True
        if not get_settings().get("auto_tune_on_load", True):
            return False
        vram = _tune_budget_gb()
        if vram <= 0:
            return False
        profile = inspect_model(model_path)
        # A fresh tune (no remembered config): still grow-only vs the saved
        # num_ctx so a user's raised context survives a re-tune here too — but
        # only when that ctx is this model's. On a switch, settings.num_ctx
        # still holds the previous model's window, which is no floor for this one.
        _min_ctx_saved = (int(get_settings().get("num_ctx") or 0)
                          if _llama.loaded_model() == model_path else 0)
        suggested = auto_tune(model_path, vram, profile=profile, min_ctx=_min_ctx_saved,
                              proven_cfg=_proven_cfg(model_path))
        if not suggested.get("num_ctx"):
            return False
        suggested["spec_strategy_source"] = "auto"
        s = get_settings()
        for k in _MODEL_TUNE_KEYS:
            if k in suggested:
                s[k] = suggested[k]
        save_json(SETTINGS_FILE, s)
        _save_model_config(model_path, suggested)
        _kv_disp = suggested.get("kv_cache_type") or ""
        if suggested.get("kv_cache_type_v") and suggested["kv_cache_type_v"] != _kv_disp:
            _kv_disp += "/" + suggested["kv_cache_type_v"]
        print(f"[llama] auto-tuned on load: ctx {suggested['num_ctx']:,} · "
              f"n_cpu_moe {suggested.get('n_cpu_moe', 0)} · {_kv_disp} KV",
              file=sys.stderr)
        try:
            broadcast_event({"type": "llama:auto_tuned", "model": model_path,
                             "notes": suggested.get("notes", ""),
                             "ctx": suggested.get("num_ctx"),
                             "n_cpu_moe": suggested.get("n_cpu_moe", 0),
                             "kv_cache_type": suggested.get("kv_cache_type"),
                             "kv_cache_type_v": suggested.get("kv_cache_type_v") or "",
                             "quant_downshift": suggested.get("quant_downshift") or ""})
        except Exception:
            pass
        return True
    except Exception:
        traceback.print_exc()
        return False
