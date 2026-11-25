# SPDX-License-Identifier: AGPL-3.0-only
"""Per-model presets matched by file name: vendor sampling, model features, and
the overrides for turns without tools.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.core.settings import get_settings
from pathlib import Path


# (filename pattern, vendor sampling, model features, no-tools overrides).
# Features: "subagents" exposes spawn_subagent (the model was trained on that
# exact orchestration schema); "vendor_agentic_sampling" keeps the profile on
# tool turns instead of the generic agentic clamp in run_chat_turn;
# "native_prior_thinking" sends earlier reasoning verbatim, for templates that
# render it back as reasoning (Nanbeige4.3: its card recommends keeping it for
# multi-turn tool use, and its template keeps it by default). The
# no-tools overrides apply to turns without tools (IDE mode) for vendors that
# publish separate chat and agentic sampling.
_MODEL_SAMPLING_PRESETS: tuple[tuple[re.Pattern, dict, frozenset, dict], ...] = (
    (re.compile(r"sage[-_ ]?a1[._-]?5", re.I),
     {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
      "repeat_penalty": 1.0, "presence_penalty": 1.5, "frequency_penalty": 0.0},
     frozenset({"subagents", "vendor_agentic_sampling"}),
     {}),
    # Nanbeige4.3 model card: agentic / tool use temp 1.0, top_p 0.95; reasoning
    # and chat temp 0.6, top_p 0.95. Nothing else is set, so the other samplers
    # are off, as on the vendor's vLLM / SGLang setups (top_k 0 = disabled).
    # "4.3" as a version only: Nanbeige4-3B is a different (3B) model.
    (re.compile(r"nanbeige[-_ ]?4[._]3(?![\db])", re.I),
     {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "min_p": 0.0,
      "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0},
     frozenset({"vendor_agentic_sampling", "native_prior_thinking"}),
     {"temperature": 0.6}),
    # Inkling 2 (Thinking Machines): generation_config.json is do_sample with
    # temperature 1.0 and nothing else, so the other samplers are off. Under the
    # generic agentic clamp (0.4, top_k 40, min_p 0.05) Mini's reasoning went in
    # circles on tool turns until maya's loop guard ended it (2026-11-21).
    (re.compile(r"inkling", re.I),
     {"temperature": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0,
      "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0},
     frozenset({"vendor_agentic_sampling"}),
     {}),
)


def _model_preset_entry(model_path: str) -> tuple[dict, frozenset, dict]:
    name = Path(str(model_path or "")).name
    for pattern, sampling, features, no_tools in _MODEL_SAMPLING_PRESETS:
        if name and pattern.search(name):
            return dict(sampling), features, dict(no_tools)
    return {}, frozenset(), {}


def _model_preset(model_path: str) -> tuple[dict, frozenset]:
    sampling, features, _no_tools = _model_preset_entry(model_path)
    return sampling, features


def _model_features(model_path: str = "") -> frozenset:
    """Feature flags for the given (default: currently selected) model."""
    if not model_path:
        try:
            model_path = str(get_settings().get("model_path") or "")
        except Exception:
            model_path = ""
    return _model_preset(model_path)[1]
