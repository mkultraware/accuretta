# SPDX-License-Identifier: AGPL-3.0-only
"""The per-model config store (models_config.json): the tuned flags and choices
remembered for each model, and applying them on load.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import sys
import time
from accuretta.core.paths import DATA
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import DEFAULT_SETTINGS
from accuretta.core.settings import _normalized_mmproj_mode
from accuretta.core.settings import get_settings
from accuretta.runtime.model_advisor import _model_is_dense


MODELS_CONFIG_FILE = DATA / "models_config.json"   # per-model tuned flag memory (auto-tune on load)


# ---- per-model config memory -------------------------------------------------
# llama flags that are model-specific (what fits THIS model on THIS machine).
# Remembered per GGUF path so switching models restores each model's own best
# config instead of inheriting the previous model's leftovers.
_MODEL_TUNE_KEYS = ("num_ctx", "n_cpu_moe", "kv_cache_type", "kv_cache_type_v", "num_gpu",
                    "num_batch", "n_ubatch", "num_thread", "spec_strategy", "spec_strategy_source",
                    "spec_draft_n_max", "moe_cache_mib")


# Tune keys newer than many saved per-model configs: when a model's saved
# config lacks one, it falls back to the default instead of inheriting the
# previously loaded model's value (a draft length tuned for one model is
# meaningless for the next).
_MODEL_TUNE_RESET_KEYS = ("spec_draft_n_max", "moe_cache_mib")


_MODEL_PROJECTOR_KEYS = ("mmproj_mode", "mmproj_path")


_MODEL_PREFERENCE_KEYS = ("reasoning_effort", "spec_draft_model")


_MODEL_CONFIG_KEYS = _MODEL_TUNE_KEYS + _MODEL_PROJECTOR_KEYS + _MODEL_PREFERENCE_KEYS


def _models_config() -> dict:
    cfg = load_json(MODELS_CONFIG_FILE, {})
    return cfg if isinstance(cfg, dict) else {}


def _apply_model_config(model_path: str) -> bool:
    """Apply this model's saved tuned flags to settings; returns True if a saved
    config existed. Saved configs below the usable floor (8192 ctx; the system
    prompt + tools don't fit in 4k) are leftovers from crash loops or tunes
    taken while another instance held the RAM, so they are deleted and retuned.
    Same for stale KV dtypes (split K/V or q4_0) that the tuner no longer emits."""
    cfg = _models_config().get(model_path)
    if isinstance(cfg, dict):
        saved_ctx = int(cfg.get("num_ctx") or 0)
        saved_kv = str(cfg.get("kv_cache_type") or "").strip().lower()
        saved_kvv = str(cfg.get("kv_cache_type_v") or "").strip().lower()
        # Split K/V and q4_0 K only came from older tuner versions; if q4_0 is
        # warranted (<=12 GB VRAM + starved ctx) a fresh tune reproduces it. Either
        # way: delete and re-tune.
        stale_kv = (saved_kvv not in ("", saved_kv)) or saved_kv == "q4_0"
        if 0 < saved_ctx < 8192 or stale_kv:
            try:
                allc = _models_config()
                allc.pop(model_path, None)
                save_json(MODELS_CONFIG_FILE, allc)
            except Exception:
                pass
            reason = (f"ctx {saved_ctx:,} is below the usable floor (scar from a bad tune)"
                      if 0 < saved_ctx < 8192 else
                      f"KV dtype {saved_kv}/{saved_kvv or 'same'} is no longer auto-emitted (stale tuner config)")
            print(f"[llama] ignoring saved config for {os.path.basename(model_path)}: "
                  f"{reason} — re-tuning", file=sys.stderr)
            return False
    if not isinstance(cfg, dict):
        return False
    s = get_settings()
    for k in _MODEL_CONFIG_KEYS:
        if k in cfg:
            s[k] = cfg[k]
    for k in _MODEL_TUNE_RESET_KEYS:
        if k not in cfg:
            s[k] = DEFAULT_SETTINGS[k]
    # n_cpu_moe is one shared setting. A partial saved config (a drawer save
    # right after switching models writes only the touched keys) must not let
    # the previous model's offload ride along, and a dense model never offloads
    # experts at all. Heal a stale saved value while we're here.
    if "n_cpu_moe" not in cfg or (int(cfg.get("n_cpu_moe") or 0) > 0 and _model_is_dense(model_path)):
        s["n_cpu_moe"] = 0
        if int(cfg.get("n_cpu_moe") or 0) > 0:
            try:
                allc = _models_config()
                ent = allc.get(model_path)
                if isinstance(ent, dict):
                    ent["n_cpu_moe"] = 0
                    save_json(MODELS_CONFIG_FILE, allc)
            except Exception:
                pass
    s["mmproj_mode"] = _normalized_mmproj_mode(s)
    s["mmproj_auto"] = s["mmproj_mode"] == "auto"
    if "spec_strategy" in cfg:
        s["enable_speculative"] = str(s.get("spec_strategy") or "off") != "off"
    save_json(SETTINGS_FILE, s)
    return True


def _save_model_config(model_path: str, flags: dict) -> None:
    """Merge tuned flags into this model's remembered config. Give-up results
    (does_not_fit) are never persisted: they were computed under transient
    conditions (low free RAM/VRAM, a running instance) and would poison every
    future load with a config that can't work."""
    if not model_path:
        return
    if flags.get("does_not_fit"):
        return
    try:
        allc = _models_config()
        ent = allc.get(model_path)
        if not isinstance(ent, dict):
            ent = {}
        for k in _MODEL_CONFIG_KEYS:
            if k in flags:
                ent[k] = flags[k]
        ent["tuned_at"] = int(time.time())
        allc[model_path] = ent
        save_json(MODELS_CONFIG_FILE, allc)
    except Exception:
        pass


def _proven_cfg(model_path: str) -> dict:
    """The proven-boot metadata for a model, or {} — tiny passthrough so
    callers pass it straight into auto_tune()."""
    if not model_path:
        return {}
    cfg = _models_config().get(model_path)
    return cfg if isinstance(cfg, dict) else {}
