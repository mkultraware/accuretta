# SPDX-License-Identifier: AGPL-3.0-only
"""Saving settings: validating each change, and keeping the model's remembered setup and
the projector, draft and sampling choices in step.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import threading
from accuretta.core.effort import _normalize_effort_choice
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import _normalize_composer_mode
from accuretta.core.persistence import save_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.settings import DEFAULT_SETTINGS
from accuretta.core.settings import _normalized_keyboard_shortcuts
from accuretta.core.settings import _normalized_mmproj_mode
from accuretta.core.settings import get_settings
from accuretta.runtime.auto_tune import _AUTOTUNE_KV_CHOICES
from accuretta.runtime.maya import _maya_drafter_for
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.model_config import MODELS_CONFIG_FILE
from accuretta.runtime.model_config import _MODEL_CONFIG_KEYS
from accuretta.runtime.model_config import _models_config
from accuretta.runtime.model_config import _save_model_config
from accuretta.runtime.model_files import _spec_fingerprint
from accuretta.runtime.model_files import find_spec_draft_for
from accuretta.runtime.model_files import spec_draft_match
from accuretta.runtime.model_presets import _model_preset
from pathlib import Path


_SETTINGS_LOCK = threading.Lock()


def update_settings(updates: dict) -> dict:
    """Persist validated settings and any model-specific choices in one place.

    The read-modify-write is serialized: UI auto-save sends quick toggles as
    concurrent POSTs, and an unlocked interleave dropped the earlier key."""
    with _SETTINGS_LOCK:
        return _update_settings_locked(updates)


def _update_settings_locked(updates: dict) -> dict:
    if "offline_mode" in (updates or {}) and not isinstance(updates["offline_mode"], bool):
        raise ValueError("offline_mode must be true or false")
    allowed = {k: v for k, v in (updates or {}).items() if k in DEFAULT_SETTINGS}
    if "profile_avatar" in allowed:
        avatar = allowed["profile_avatar"]
        # The client downsizes to 192px JPEG (~10-30 KB); refuse anything else
        # so settings.json can't be bloated or made to carry other content.
        if not isinstance(avatar, str) or (avatar and (
                len(avatar) > 300_000
                or not re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+", avatar))):
            raise ValueError("profile_avatar must be a small PNG, JPEG or WebP data URL")
    if "profile_name" in allowed:
        name = allowed["profile_name"]
        if not isinstance(name, str) or len(name) > 40 or any(ord(ch) < 32 for ch in name):
            raise ValueError("profile_name must be plain text of at most 40 characters")
        allowed["profile_name"] = name.strip()
    if "custom_palettes" in allowed:
        palettes = allowed["custom_palettes"]
        if not isinstance(palettes, dict) or len(palettes) > 32:
            raise ValueError("Invalid custom palettes")
        for theme, colors in palettes.items():
            if not isinstance(theme, str) or not re.fullmatch(r"[a-z-]{1,32}", theme):
                raise ValueError("Invalid palette theme")
            if not isinstance(colors, dict) or set(colors) != {"background", "surface", "accent"}:
                raise ValueError("A palette requires background, surface and accent")
            if any(not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value) for value in colors.values()):
                raise ValueError("Palette colors must be six-digit hex colors")
    cur = get_settings()
    cur.update(allowed)
    if "keyboard_shortcuts" in allowed:
        cur["keyboard_shortcuts"] = _normalized_keyboard_shortcuts(
            allowed.get("keyboard_shortcuts"))

    if any(k in allowed for k in ("mmproj_mode", "mmproj_auto", "mmproj_path")):
        if "mmproj_mode" in allowed:
            mode = str(allowed.get("mmproj_mode") or "").strip().lower()
            if mode not in {"off", "auto", "manual"}:
                mode = _normalized_mmproj_mode(cur)
        elif "mmproj_auto" in allowed:
            mode = "auto" if bool(allowed.get("mmproj_auto")) else (
                "manual" if str(cur.get("mmproj_path") or "").strip() else "off")
        else:
            mode = "manual" if str(allowed.get("mmproj_path") or "").strip() else (
                "auto" if bool(cur.get("mmproj_auto", True)) else "off")
        cur["mmproj_mode"] = mode
        cur["mmproj_auto"] = mode == "auto"
        if mode == "manual":
            projector = str(cur.get("mmproj_path") or "").strip()
            if not projector:
                raise ValueError("Manual projector mode requires a .gguf file path.")
            if Path(projector).suffix.lower() != ".gguf":
                raise ValueError("Vision projector must be a .gguf file.")
            if not safe_exists(projector):
                raise ValueError(f"Vision projector file does not exist: {projector}")

    if "spec_strategy" in allowed:
        strategy = str(cur.get("spec_strategy") or "").strip().lower()
        if strategy not in {"off", "ngram-mod", "draft-mtp", "dflash", "dspark"}:
            strategy = "ngram-mod"
        cur["spec_strategy"] = strategy
        cur["enable_speculative"] = strategy != "off"

    if "spec_strategy_source" in allowed:
        source = str(cur.get("spec_strategy_source") or "auto").strip().lower()
        cur["spec_strategy_source"] = source if source in {"auto", "manual"} else "auto"

    if "spec_draft_mode" in allowed:
        mode = str(cur.get("spec_draft_mode") or "auto").strip().lower()
        cur["spec_draft_mode"] = mode if mode in {"auto", "off"} else "auto"
    # An explicit draft pick re-arms pairing — but only when the caller did
    # not explicitly set the mode in the same call (explicit mode wins, so
    # the unload endpoint's own write is never undone here).
    if "spec_draft_mode" not in allowed:
        _want_draft = (str(allowed.get("spec_draft_model") or "").strip()
                       or str(allowed.get("spec_strategy") or "").strip().lower() in {"dflash", "dspark"})
        if _want_draft:
            cur["spec_draft_mode"] = "auto"

    if (cur.get("spec_strategy") in {"dflash", "dspark"}
            and cur.get("spec_draft_mode") != "off"
            and any(k in allowed for k in ("spec_strategy", "spec_draft_model"))):
        draft_model = str(cur.get("spec_draft_model") or "").strip()
        _model = str(cur.get("model_path") or "").strip()
        _maya_model = bool(_model) and bool(_maya_engine_for(_model))
        _maya_draft = _maya_drafter_for(_model) if _maya_model else ""
        if _maya_model and (not draft_model
                            or os.path.normcase(draft_model) == os.path.normcase(_maya_draft)):
            # A model maya runs: its helper is Thinking Machines' drafter beside
            # it (mtp.safetensors), not a draft GGUF.
            if not _maya_draft:
                raise ValueError("The Maya helper (Thinking Machines' mtp.safetensors) isn't beside "
                                 "the model. Put it in the model's folder, then pick dspark again.")
        elif not draft_model:
            # An empty path is fine when the selected model has a metadata-
            # validated pair on disk: the spawn path auto-pairs it.
            pair = find_spec_draft_for(str(cur.get("model_path") or "").strip())
            if not (pair.get("matched") and pair.get("path")):
                raise ValueError(
                    f"{cur['spec_strategy'].upper()} needs a matching draft GGUF beside the "
                    "selected model (or pick one explicitly).")
        elif Path(draft_model).suffix.lower() != ".gguf":
            raise ValueError("Speculative draft model must be a .gguf file.")
        elif not safe_exists(draft_model):
            raise ValueError(f"Speculative draft model does not exist: {draft_model}")
        elif cur.get("model_path") and safe_exists(str(cur["model_path"])):
            match = spec_draft_match(str(cur["model_path"]), draft_model)
            if not match.get("matched"):
                # Only reject when both headers were readable enough to prove a
                # mismatch; unverifiable files stay the user's explicit choice.
                draft_ok = bool((match.get("draft") or {}).get("ok"))
                target_ok = bool(_spec_fingerprint(str(cur["model_path"])).get("ok"))
                if draft_ok and target_ok:
                    raise ValueError(
                        "That draft does not match the selected model: "
                        + "; ".join(match.get("reasons") or []))

    if "composer_mode" in allowed:
        cur["composer_mode"] = _normalize_composer_mode(cur.get("composer_mode"))

    if "agent_prompt_variant" in allowed:
        variant = str(cur.get("agent_prompt_variant") or "classic").strip().lower()
        cur["agent_prompt_variant"] = variant if variant in {"classic", "compact"} else "classic"

    if "tool_verbosity" in allowed:
        tv = str(cur.get("tool_verbosity") or "lean").strip().lower()
        cur["tool_verbosity"] = tv if tv in {"lean", "full"} else "lean"

    if "reasoning_effort" in allowed:
        effort = str(cur.get("reasoning_effort") or "auto").strip().lower()
        cur["reasoning_effort"] = _normalize_effort_choice(effort)

    if "autotune_kv_cache" in allowed:
        kv_pref = str(cur.get("autotune_kv_cache") or "auto").strip().lower()
        cur["autotune_kv_cache"] = kv_pref if kv_pref in _AUTOTUNE_KV_CHOICES else "auto"
    if "autotune_kv_cache_v" in allowed:
        kv_v_pref = str(cur.get("autotune_kv_cache_v") or "").strip().lower()
        cur["autotune_kv_cache_v"] = kv_v_pref if kv_v_pref in _AUTOTUNE_KV_CHOICES - {"auto"} else ""

    if "verification_mode" in allowed:
        vm = str(cur.get("verification_mode") or "auto").strip().lower()
        cur["verification_mode"] = vm if vm in {"auto", "on", "off"} else "auto"

    for _key, _lo, _hi in (("subagent_max_tasks", 1, 8), ("subagent_max_turns", 2, 60)):
        if _key in allowed:
            try:
                cur[_key] = max(_lo, min(_hi, int(cur.get(_key))))
            except (TypeError, ValueError):
                cur[_key] = DEFAULT_SETTINGS[_key]

    save_json(SETTINGS_FILE, cur)
    # Sampling edited while a model with its own sampling profile is loaded
    # belongs to that model, not to the baseline every other model uses.
    _model_sampling_remember_edit(cur, allowed)
    model_path = str(cur.get("model_path") or "").strip()
    model_keys = _MODEL_CONFIG_KEYS
    profile_update = {k: cur.get(k) for k in model_keys if k in allowed}
    if any(k in allowed for k in ("mmproj_mode", "mmproj_auto", "mmproj_path")):
        profile_update.update({
            "mmproj_mode": cur["mmproj_mode"],
            "mmproj_path": str(cur.get("mmproj_path") or "").strip(),
        })
    if model_path and profile_update:
        _save_model_config(model_path, profile_update)
    return cur


# ---- per-model sampling profiles ----------------------------------------------
# Sampling is global (the Settings sliders), which is right for the models it
# was tuned on. A few models ship vendor sampling that is part of how they were
# trained to run (Sage-A1.5: temp 1.0 + presence 1.5). Loading one of those
# stashes the global sliders in settings["sampling_baseline"] and applies the
# profile; loading any model without a profile puts the baseline back. Edits
# made while a profiled model is loaded are remembered for that model only
# (models_config.json → "sampling"), so the baseline is never overwritten.
_SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p",
                  "repeat_penalty", "presence_penalty", "frequency_penalty")


def _model_sampling_profile(model_path: str) -> dict:
    """Remembered per-model sampling, else the built-in preset, else {}."""
    preset, _features = _model_preset(model_path)
    saved = (_models_config().get(model_path) or {}).get("sampling")
    if isinstance(saved, dict) and saved:
        return {**preset, **{k: saved[k] for k in _SAMPLING_KEYS if k in saved}}
    return preset


def _model_sampling_remember_edit(cur: dict, allowed: dict) -> None:
    """Called from update_settings: while a profiled model is loaded, store a
    changed sampling value as that model's own (the baseline stays as is)."""
    if not isinstance(cur.get("sampling_baseline"), dict):
        return
    model_path = str(cur.get("model_path") or "").strip()
    if not model_path or not any(k in allowed for k in _SAMPLING_KEYS):
        return
    profile = _model_sampling_profile(model_path)
    edited = {k: cur.get(k) for k in _SAMPLING_KEYS
              if k in allowed and str(cur.get(k)) != str(profile.get(k))}
    if not edited:
        return
    try:
        allc = _models_config()
        ent = allc.get(model_path)
        ent = dict(ent) if isinstance(ent, dict) else {}
        sampling = ent.get("sampling") if isinstance(ent.get("sampling"), dict) else {}
        sampling.update(edited)
        ent["sampling"] = sampling
        allc[model_path] = ent
        save_json(MODELS_CONFIG_FILE, allc)
    except Exception:
        pass
