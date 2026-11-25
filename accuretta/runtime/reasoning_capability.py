# SPDX-License-Identifier: AGPL-3.0-only
"""Whether the loaded model reasons, and how: its template's thinking switches and
native effort levels.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.runtime.reasoning_request import _nearest_native_level
from accuretta.core.effort import _EFFORT_RANK
from accuretta.core.effort import _REASONING_LEVELS
from accuretta.core.persistence import load_json
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.llama_process import _REASONING_CAP_CACHE
from accuretta.runtime.llama_process import _llama_props_data
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _model_profile_key
from pathlib import Path


def _model_reasoning_was_observed(model: str) -> bool:
    """True when a prior real response exposed a reasoning stream.

    This stores no generated text. It is just a durable capability bit, useful
    for brand-new model families whose template names Accuretta has never seen.
    """
    if not model:
        return False
    try:
        rows = (load_json(MODEL_RUNTIME_FILE, {"models": {}}).get("models") or {})
        p = Path(str(model))
        keys = {_model_profile_key(model), p.stem[:180], p.name[:180]}
        return any(bool((rows.get(k) or {}).get("reasoning_observed")) for k in keys)
    except Exception:
        return False


_EFFORT_VAR = r"[\w.]*(?:effort|thinking_level|reasoning_level|reasoning_strength)[\w.]*"


def _effort_names(raw: str) -> list[str]:
    """Known effort names in a quoted list, ordered low to high. "none" is
    thinking-off, which the thinking toggle already covers."""
    found = {m.lower() for m in re.findall(r"['\"]([A-Za-z]+)['\"]", raw or "")}
    return sorted((n for n in found if n in _EFFORT_RANK and n != "none"),
                  key=lambda n: _EFFORT_RANK[n])


def _parse_native_effort_levels(template: str) -> tuple[list[str], str]:
    """Read a chat template's own effort level names and its default.

    Tried in order, most to least explicit:
      1. a whitelist the template enforces, e.g. Qwen3.8's
         `resolved_reasoning_effort not in ('xhigh', 'medium', 'low')`;
      2. the values it assigns to its effort variable, for templates that
         fold aliases (high/max/extreme -> xhigh) into a few canonical levels;
      3. the values it compares its effort variable against.
    Returns ([], "") when the template names no levels."""
    text = str(template or "").replace('\\"', '"').replace("\\n", "\n")
    levels: list[str] = []
    for m in re.finditer(_EFFORT_VAR + r"\s+not\s+in\s*[\(\[]([^\)\]]*)[\)\]]", text, re.I):
        levels = _effort_names(m.group(1))
        if levels:
            break
    if not levels:
        assigned = re.findall(r"set\s+" + _EFFORT_VAR + r"\s*=\s*(['\"][A-Za-z]+['\"])", text, re.I)
        levels = _effort_names(" ".join(assigned))
        if len(levels) < 2:
            levels = []
    if not levels:
        compared = re.findall(_EFFORT_VAR + r"\s*==\s*(['\"][A-Za-z]+['\"])", text, re.I)
        levels = _effort_names(" ".join(compared))
        if len(levels) < 2:
            levels = []
    default = ""
    for pat in (_EFFORT_VAR + r"\s*\|\s*default\(\s*['\"]([A-Za-z]+)['\"]",
                r"set\s+[\w.]*default[\w.]*(?:effort|level)[\w.]*\s*=\s*['\"]([A-Za-z]+)['\"]",
                r"set\s+(?:reasoning_effort|thinking_level|reasoning_level|reasoning_strength)"
                r"\s*=\s*['\"]([A-Za-z]+)['\"]"):
        m = re.search(pat, text, re.I)
        if m and m.group(1).lower() in _EFFORT_RANK:
            default = m.group(1).lower()
            break
    if levels and default and default not in levels:
        default = _nearest_native_level(default, levels) or ""
    return levels, default


def _reasoning_capability(model_path: str = "", settings: dict | None = None,
                          props: dict | None = None) -> dict:
    """Describe adjustable reasoning support without trusting a single name.

    Detection prefers actual template kwargs from live llama-server /props and
    the GGUF chat template. Known-family names are only a fallback. A Settings
    override exists because new local models routinely arrive before harnesses
    know their template vocabulary.
    """
    settings = settings or get_settings()
    model_path = str(model_path or settings.get("model_path") or "").strip()
    override = str(settings.get("reasoning_capability_override") or "auto").strip().lower()
    if override not in ("auto", "off", "budget", "native_effort"):
        override = "auto"
    if not model_path:
        return {
            "supported": False, "mode": "none", "source": "no_model",
            "levels": list(_REASONING_LEVELS),
            "description": "Load a model to detect adjustable reasoning.",
        }
    if override == "off":
        return {
            "supported": False, "mode": "none", "source": "manual",
            "levels": list(_REASONING_LEVELS),
            "description": "Reasoning control is disabled in Settings.",
        }

    observed = _model_reasoning_was_observed(model_path)
    try:
        st = Path(model_path).stat()
        file_sig = (int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        file_sig = (0, 0)
    props = props if isinstance(props, dict) else _llama_props_data()
    cache_key = (model_path, file_sig, override, observed, id(props))
    cached = _REASONING_CAP_CACHE.get(cache_key)
    if cached:
        return dict(cached)

    if override in ("budget", "native_effort"):
        cap = {
            "supported": True, "mode": override, "source": "manual",
            "levels": list(_REASONING_LEVELS),
            "description": ("Uses the model's native effort levels."
                            if override == "native_effort"
                            else "Uses a token budget with Accuretta's runaway guard."),
        }
        if override == "native_effort":
            cap["native_parameter"] = "reasoning_effort"
            cap["native_levels"] = ["low", "medium", "high"]
            cap["native_default"] = ""
        _REASONING_CAP_CACHE[cache_key] = cap
        return dict(cap)

    # Only inspect chat-template values from GGUF metadata. Other huge metadata
    # strings cannot establish reasoning support and would just waste memory.
    gguf_template = ""
    try:
        keys = (read_gguf_metadata(model_path).get("keys") or {})
        gguf_template = "\n".join(
            str(v) for k, v in keys.items()
            if "chat_template" in str(k).lower() and isinstance(v, str)
        )
    except Exception:
        pass
    # /props also exposes generic server request defaults. Looking for an
    # arbitrary `reasoning_effort` key across that whole object would make the
    # slider appear for every model even when its template drops the value.
    # Inspect only fields explicitly belonging to a chat template/capability.
    prop_templates: list[str] = []
    def _collect_prop_templates(value, key_hint=""):
        if len(prop_templates) >= 24:
            return
        if "chat_template" in key_hint.lower():
            try:
                prop_templates.append(json.dumps(value, ensure_ascii=False)[:262144])
            except Exception:
                pass
            return
        if isinstance(value, dict):
            for k, v in value.items():
                _collect_prop_templates(v, str(k))
        elif isinstance(value, list):
            for v in value[:32]:
                _collect_prop_templates(v, key_hint)
    _collect_prop_templates(props)
    props_text = "\n".join(prop_templates)
    signal = (gguf_template + "\n" + props_text).lower()
    name = Path(model_path).name.lower()

    native_parameter = ""
    for candidate in ("reasoning_effort", "reasoning_strength",
                      "thinking_level", "reasoning_level"):
        if candidate in signal:
            native_parameter = candidate
            break
    has_native = bool(native_parameter)
    has_budget = ("thinking_budget_tokens" in signal or "thinking_budget" in signal)
    has_toggle = "enable_thinking" in signal
    advertised_native_levels = set(re.findall(
        r"['\"](minimal|low|medium|high|xhigh|max|none)['\"]", signal
    ))
    native_family = any(x in name for x in ("gpt-oss", "stepfun", "step-3", "step3"))
    budget_family = any(x in name for x in (
        "qwq", "deepseek-r1", "deepseek_r1", "r1-distill",
        "reasoner", "reasoning", "thinker",
    ))

    if has_native or native_family:
        mode = "native_effort"
        source = "chat_template" if has_native else "model_family"
        description = "The model exposes native low, medium, and high effort levels."
    elif has_budget or has_toggle or observed or budget_family:
        mode = "budget"
        if has_budget or has_toggle:
            source = "chat_template"
        elif observed:
            source = "observed_output"
        else:
            source = "model_family"
        description = "Accuretta adjusts the thinking budget and keeps its runaway guard active."
    else:
        mode = "none"
        source = "not_detected"
        description = "This model did not advertise adjustable reasoning."

    cap = {
        "supported": mode != "none", "mode": mode, "source": source,
        "levels": list(_REASONING_LEVELS), "description": description,
    }
    if mode == "native_effort":
        cap["native_parameter"] = native_parameter or "reasoning_effort"
        native_map = {}
        # The composer uses a compact, model-agnostic Auto/Low/Medium/High
        # ladder. Some templates name their deepest tier xhigh or max. Map the
        # UI tier to an actually advertised value instead of sending a string
        # the template rejects. Qwen3.8, for example, accepts low/medium/xhigh.
        if "high" not in advertised_native_levels:
            if "xhigh" in advertised_native_levels:
                native_map["high"] = "xhigh"
            elif "max" in advertised_native_levels:
                native_map["high"] = "max"
        if native_map:
            cap["native_effort_map"] = native_map
        if advertised_native_levels:
            cap["native_effort_values"] = sorted(advertised_native_levels)
        # The picker shows the template's own names (Qwen3.8: low, medium,
        # xhigh), read from the GGUF template first, then the live server's.
        native_levels, native_default = _parse_native_effort_levels(gguf_template)
        if not native_levels:
            native_levels, native_default = _parse_native_effort_levels(props_text)
        if not native_levels and any(x in name for x in ("gpt-oss", "gpt_oss")):
            native_levels, native_default = ["low", "medium", "high"], "medium"
        if not native_levels:
            native_levels = [l for l in ("low", "medium", "high", "xhigh", "max")
                             if l in advertised_native_levels]
            if len(native_levels) < 2:
                native_levels = ["low", "medium", "high"]
        cap["native_levels"] = native_levels
        cap["native_default"] = native_default
        cap["levels"] = ["auto"] + native_levels
        cap["description"] = "Uses the model's own effort levels: " + ", ".join(native_levels) + "."
    _REASONING_CAP_CACHE[cache_key] = cap
    return dict(cap)
