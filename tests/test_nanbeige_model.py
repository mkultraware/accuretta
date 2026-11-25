"""Nanbeige4.3 loads with its vendor sampling and gives the user's settings
back when another model loads, the same way Sage-A1.5 does.

Model card: agentic / tool use temp 1.0, top_p 0.95; reasoning and chat
temp 0.6, top_p 0.95; nothing else specified.
"""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _sandbox import sandbox_user_files

NANBEIGE = r"D:\MODELS\Nanbeige4.3-33B-AD-IQ4_XS.gguf"
SAGE = r"D:\MODELS\Sage-A1.5-Q4_K_M.gguf"
QWEN = r"D:\MODELS\Qwen3.6-27B-UD-Q3_K_XL.gguf"
QWEN_SAMPLING = {"temperature": 0.5, "top_p": 0.9, "top_k": 40, "min_p": 0.05,
                 "repeat_penalty": 1.1, "presence_penalty": 0, "frequency_penalty": 0}
NANBEIGE_SAMPLING = {"temperature": 1.0, "top_p": 0.95, "top_k": 0, "min_p": 0.0,
                     "repeat_penalty": 1.0, "presence_penalty": 0.0, "frequency_penalty": 0.0}


class _Store(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        tmp = Path(tempfile.mkdtemp())
        store_patch = bridge_patch("MODELS_CONFIG_FILE", tmp / "models_config.json")
        store_patch.start()
        self.addCleanup(store_patch.stop)
        bridge.SETTINGS_FILE.write_text(json.dumps({**QWEN_SAMPLING, "model_path": QWEN}), encoding="utf-8")

    def sampling(self):
        s = bridge.get_settings()
        return {k: s[k] for k in bridge._SAMPLING_KEYS}

    def select(self, path):
        bridge._apply_model_sampling(path)
        s = bridge.get_settings()
        s["model_path"] = path
        bridge.save_json(bridge.SETTINGS_FILE, s)


class NanbeigePresetTests(unittest.TestCase):
    def test_filenames_match(self):
        for name in ("Nanbeige4.3-33B-AD-IQ4_XS.gguf", "Nanbeige4.3-33B-Q8_0.gguf",
                     "nanbeige-4.3-33b-Q4_K_M.gguf"):
            sampling, features = bridge._model_preset(rf"D:\MODELS\{name}")
            self.assertEqual(sampling, NANBEIGE_SAMPLING, name)
            self.assertIn("vendor_agentic_sampling", features)
            self.assertNotIn("subagents", features, "spawn_subagent is Sage's trained schema only")

    def test_other_nanbeige_versions_are_not_matched(self):
        self.assertEqual(bridge._model_preset(r"D:\MODELS\Nanbeige4-3B-Thinking-Q8_0.gguf"),
                         ({}, frozenset()))


class NanbeigeSamplingSwapTests(_Store):
    def test_load_nanbeige_then_qwen_restores_your_settings(self):
        self.assertEqual(bridge._apply_model_sampling(NANBEIGE), "profile")
        self.assertEqual(self.sampling(), NANBEIGE_SAMPLING)
        self.assertEqual(bridge._apply_model_sampling(QWEN), "baseline")
        self.assertEqual(self.sampling(), QWEN_SAMPLING)

    def test_sage_then_nanbeige_then_qwen_keeps_the_original_baseline(self):
        bridge._apply_model_sampling(SAGE)
        bridge._apply_model_sampling(NANBEIGE)
        self.assertEqual(self.sampling(), NANBEIGE_SAMPLING)
        self.assertEqual(bridge.get_settings()["sampling_baseline"], QWEN_SAMPLING)
        bridge._apply_model_sampling(QWEN)
        self.assertEqual(self.sampling(), QWEN_SAMPLING)

    def test_edit_while_loaded_is_remembered_for_nanbeige_only(self):
        self.select(NANBEIGE)
        bridge.update_settings({**self.sampling(), "temperature": 0.8})
        self.assertEqual(bridge._models_config()[NANBEIGE]["sampling"], {"temperature": 0.8})
        self.select(QWEN)
        self.assertEqual(self.sampling(), QWEN_SAMPLING)
        self.select(NANBEIGE)
        self.assertEqual(self.sampling()["temperature"], 0.8)

    def test_llama_options_turns_top_k_off(self):
        bridge._apply_model_sampling(NANBEIGE)
        opt = bridge.llama_options(bridge.get_settings())
        self.assertEqual((opt["temperature"], opt["top_p"], opt["top_k"], opt["min_p"],
                          opt["repeat_penalty"], opt["presence_penalty"]),
                         (1.0, 0.95, 0.0, 0.0, 1.0, 0.0))

    def test_chat_temperature_applies_without_tools_unless_you_set_your_own(self):
        self.select(NANBEIGE)
        self.assertEqual(bridge._model_no_tools_sampling(), {"temperature": 0.6})
        bridge.update_settings({**self.sampling(), "temperature": 0.9})
        self.assertEqual(bridge._model_no_tools_sampling(), {})
        self.select(QWEN)
        self.assertEqual(bridge._model_no_tools_sampling(), {})


class NanbeigePriorThinkingTests(unittest.TestCase):
    """Nanbeige's template renders earlier <think> blocks back as reasoning and
    its card recommends keeping them for multi-turn tool use, so they go out
    verbatim for Nanbeige only; other models follow the global setting."""

    HISTORY = [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": "<think>\nI should call ls first.\n</think>\n\nChecking."},
        {"role": "user", "content": "now count them"},
        {"role": "assistant", "content": "<think>\nThere were three.\n</think>\n\nThree files."},
        {"role": "user", "content": "thanks"},
    ]

    def settings(self, model_path, preserve=False):
        return {**bridge.DEFAULT_SETTINGS, "model_path": model_path, "preserve_prior_thinking": preserve}

    def sent(self, model_path, preserve=False):
        s = self.settings(model_path, preserve)
        with bridge_patch("get_settings", lambda: s):
            msgs = bridge._sanitize_messages_for_openai(bridge._thinking_wire_view(self.HISTORY, s))
        return [m["content"] for m in msgs if m["role"] == "assistant"]

    def test_modes(self):
        with bridge_patch("get_settings", lambda: self.settings(QWEN)):
            self.assertEqual(bridge._prior_thinking_mode(self.settings(NANBEIGE)), "native")
            self.assertEqual(bridge._prior_thinking_mode(self.settings(NANBEIGE, True)), "native")
            self.assertEqual(bridge._prior_thinking_mode(self.settings(QWEN)), "strip")
            self.assertEqual(bridge._prior_thinking_mode(self.settings(QWEN, True)), "scratchpad")

    def test_nanbeige_gets_earlier_reasoning_verbatim(self):
        first, latest = self.sent(NANBEIGE)
        self.assertIn("<think>\nI should call ls first.\n</think>", first)
        self.assertNotIn("scratchpad-from-earlier-turn", first)
        self.assertIn("<think>", latest)

    def test_other_models_still_strip_by_default(self):
        # Both replies belong to tasks the user has since moved on from
        # ("thanks" is the latest real message), so both lose their reasoning.
        first, latest = self.sent(QWEN)
        self.assertNotIn("I should call ls first", first)
        self.assertNotIn("There were three", latest)
        self.assertEqual(latest, "Three files.")

    def test_global_setting_still_rewrites_for_other_models(self):
        first, _ = self.sent(QWEN, preserve=True)
        self.assertIn("[scratchpad-from-earlier-turn]", first)
        self.assertNotIn("<think>", first)


class _Response:
    def __iter__(self):
        yield ("data: " + json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]})).encode()
        yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
        yield b"data: [DONE]"

    def close(self):
        pass


class NanbeigeTurnPayloadTests(unittest.TestCase):
    def payload_for(self, model_path, use_tools):
        requests = []
        settings = {**bridge.DEFAULT_SETTINGS, **NANBEIGE_SAMPLING, "model": "test-model",
                    "model_path": model_path, "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False}
        mocks = {"get_settings": lambda: settings,
                 "llama_post_stream": lambda path, payload: requests.append(copy.deepcopy(payload)) or _Response(),
                 "_llama_props_ctx": lambda: 32768,
                 "_conversation_token_scale": lambda *a, **kw: 1,
                 "_record_model_observation": lambda *a, **kw: None,
                 "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
                 "_models_config": lambda: {},
                 "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
                 "_turn_journal_begin": lambda *a: "test-turn",
                 "_turn_journal_checkpoint": lambda *a, **kw: None,
                 "get_chats": lambda: {"chats": {}, "order": []}}
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            bridge.run_chat_turn("nanbeige-test", [{"role": "user", "content": "hi"}], use_tools, lambda e: None)
        return requests[0]

    def test_agent_turns_keep_the_vendor_agentic_temperature(self):
        # Not clamped to 0.4 like generic models.
        self.assertEqual(self.payload_for(NANBEIGE, True)["temperature"], 1.0)

    def test_turns_without_tools_use_the_vendor_chat_temperature(self):
        payload = self.payload_for(NANBEIGE, False)
        self.assertEqual(payload["temperature"], 0.6)
        self.assertEqual(payload["top_p"], 0.95)

    def test_other_models_are_unchanged(self):
        self.assertEqual(self.payload_for(QWEN, True)["temperature"], 0.4)    # generic agentic clamp
        self.assertEqual(self.payload_for(QWEN, False)["temperature"], 1.0)   # sliders as set


if __name__ == "__main__":
    unittest.main()
