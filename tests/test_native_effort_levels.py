"""The effort picker shows each model's own level names, read from its chat
template, and the chosen name reaches the template unchanged.

Template snippets are copied from real GGUFs: Qwen3.8 (whitelist, xhigh
default, with and without the high->xhigh alias), a Qwen fine-tune that folds
aliases into canonical levels, and gpt-oss (no list in the template).
"""
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch

QWEN38 = """
{%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
{{- raise_exception('Unexpected reasoning effort ' ~ reasoning_effort ~ '. Supported types are xhigh (default), medium, and low.') }}
{%- endif %}
{%- if resolved_reasoning_effort == 'xhigh' %}x{%- elif resolved_reasoning_effort == 'low' %}l{%- endif %}
"""

QWEN38_ALIAS = """
{%- set resolved_reasoning_effort = reasoning_effort|default('xhigh') %}
{%- if resolved_reasoning_effort == 'high' %}
{%- set resolved_reasoning_effort = 'xhigh' %}
{%- endif %}
{%- if resolved_reasoning_effort not in ('xhigh', 'medium', 'low') %}
{{- raise_exception('bad') }}
{%- endif %}
"""

FOLDED = """
{%- set _default_reasoning_effort = 'medium' %}
{%- set _effort_raw = (reasoning_effort | string | lower) if reasoning_effort is defined else _default_reasoning_effort %}
{%- if _effort_raw in ('none', 'off') %}
{%- set _initial_effort = 'medium' %}
{%- elif _effort_raw in ('minimal', 'low') %}
{%- set _initial_effort = 'low' %}
{%- elif _effort_raw in ('high', 'xhigh', 'max', 'ultracode', 'extreme') %}
{%- set _initial_effort = 'xhigh' %}
{%- else %}
{%- set _initial_effort = 'medium' %}
{%- endif %}
"""

GPT_OSS = """
{%- if not reasoning_effort is defined %}
    {%- set reasoning_effort = "medium" %}
{%- endif %}
{{- "Reasoning: " + reasoning_effort + "\\n\\n" }}
"""


def capability_for(template: str, name: str = "Some-Model-Q4_K_M.gguf") -> dict:
    bridge._REASONING_CAP_CACHE.clear()
    meta = {"keys": {"tokenizer.chat_template": template}}
    with bridge_patch("read_gguf_metadata", return_value=meta), \
            bridge_patch("_model_reasoning_was_observed", return_value=False), \
            patch.object(Path, "stat", side_effect=OSError):
        return bridge._reasoning_capability(
            "D:/MODELS/" + name, {"reasoning_capability_override": "auto"}, props={})


class TemplateParsing(unittest.TestCase):
    def test_qwen38_whitelist_and_default(self):
        self.assertEqual(bridge._parse_native_effort_levels(QWEN38), (["low", "medium", "xhigh"], "xhigh"))

    def test_alias_line_does_not_add_high(self):
        self.assertEqual(bridge._parse_native_effort_levels(QWEN38_ALIAS), (["low", "medium", "xhigh"], "xhigh"))

    def test_folded_aliases_use_canonical_levels(self):
        self.assertEqual(bridge._parse_native_effort_levels(FOLDED), (["low", "medium", "xhigh"], "medium"))

    def test_json_escaped_template_from_props(self):
        import json
        self.assertEqual(bridge._parse_native_effort_levels(json.dumps(QWEN38))[0], ["low", "medium", "xhigh"])

    def test_no_levels(self):
        self.assertEqual(bridge._parse_native_effort_levels("{{ messages }}"), ([], ""))


class Capability(unittest.TestCase):
    def test_qwen38_capability(self):
        cap = capability_for(QWEN38)
        self.assertEqual(cap["mode"], "native_effort")
        self.assertEqual(cap["native_levels"], ["low", "medium", "xhigh"])
        self.assertEqual(cap["native_default"], "xhigh")
        self.assertEqual(cap["levels"], ["auto", "low", "medium", "xhigh"])

    def test_gpt_oss_gets_its_three_levels(self):
        cap = capability_for(GPT_OSS, "gpt-oss-20b-Q4_K_M.gguf")
        self.assertEqual(cap["mode"], "native_effort")
        self.assertEqual(cap["native_levels"], ["low", "medium", "high"])
        self.assertEqual(cap["native_default"], "medium")


class Resolve(unittest.TestCase):
    CAP = {"supported": True, "mode": "native_effort", "native_parameter": "reasoning_effort",
           "native_levels": ["low", "medium", "xhigh"], "native_effort_map": {"high": "xhigh"}}

    def resolve(self, choice, cap=None):
        return bridge._resolve_reasoning_request(choice, cap or self.CAP, {"enable_thinking": True}, 131072)

    def test_native_name_passes_through(self):
        r = self.resolve("xhigh")
        self.assertEqual(r["native_effort"], "xhigh")
        self.assertEqual(r["effort"], "high")      # tier: verification etc. still see "high"
        self.assertEqual(r["choice"], "xhigh")

    def test_old_saved_high_maps_to_xhigh(self):
        self.assertEqual(self.resolve("high")["native_effort"], "xhigh")
        self.assertEqual(self.resolve("deep")["native_effort"], "xhigh")

    def test_low_and_medium(self):
        self.assertEqual(self.resolve("low")["native_effort"], "low")
        self.assertEqual(self.resolve("medium")["native_effort"], "medium")

    def test_auto_sends_nothing(self):
        self.assertIsNone(self.resolve("auto")["native_effort"])

    def test_budget_model_treats_xhigh_as_high(self):
        cap = {"supported": True, "mode": "budget"}
        r = self.resolve("xhigh", cap)
        self.assertEqual(r["effort"], "high")
        self.assertIsNone(r["native_effort"])
        self.assertEqual(r["budget"], bridge._reasoning_budget_for_effort("high", 131072))

    def test_settings_keep_native_names(self):
        self.assertEqual(bridge._normalize_effort_choice("xhigh"), "xhigh")
        self.assertEqual(bridge._normalize_effort_choice("bogus"), "auto")
        self.assertEqual(bridge._effort_tier("max"), "high")
        self.assertEqual(bridge._effort_tier("minimal"), "low")


if __name__ == "__main__":
    unittest.main()
