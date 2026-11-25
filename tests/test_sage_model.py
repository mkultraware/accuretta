import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
import subagents
from _sandbox import sandbox_user_files


SAGE = r"D:\MODELS\Sage-A1.5-Q4_K_M.gguf"
QWEN = r"D:\MODELS\Qwen3.6-27B-UD-Q3_K_XL.gguf"
QWEN_SAMPLING = {"temperature": 0.5, "top_p": 0.9, "top_k": 40, "min_p": 0.05,
                 "repeat_penalty": 1.1, "presence_penalty": 0, "frequency_penalty": 0}


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


class PresetTests(unittest.TestCase):
    def test_sage_filenames_match(self):
        for name in ("Sage-A1.5-Q4_K_M.gguf", "sage-a1.5-UD-Q5_K_XL.gguf", "Sage_A1_5-Q8_0.gguf"):
            self.assertIn("subagents", bridge._model_features(rf"D:\MODELS\{name}"), name)

    def test_other_models_have_no_profile(self):
        for name in ("Qwen3.6-27B-UD-Q3_K_XL.gguf", "Agents-A1-Q4_K_M.gguf", "flint-preview-UD-Q6_K_XL.gguf"):
            self.assertEqual(bridge._model_preset(rf"D:\MODELS\{name}"), ({}, frozenset()), name)


class SamplingSwapTests(_Store):
    def test_load_sage_then_qwen_restores_baseline(self):
        self.assertEqual(bridge._apply_model_sampling(SAGE), "profile")
        self.assertEqual(self.sampling()["temperature"], 1.0)
        self.assertEqual(self.sampling()["presence_penalty"], 1.5)
        self.assertEqual(self.sampling()["top_k"], 20)
        self.assertEqual(bridge.get_settings()["sampling_baseline"], QWEN_SAMPLING)
        self.assertEqual(bridge._apply_model_sampling(QWEN), "baseline")
        self.assertEqual(self.sampling(), QWEN_SAMPLING)
        self.assertNotIn("sampling_baseline", bridge.get_settings())

    def test_switching_between_profiled_models_keeps_first_baseline(self):
        bridge._apply_model_sampling(SAGE)
        bridge._apply_model_sampling(SAGE.replace("Q4_K_M", "Q8_0"))
        self.assertEqual(bridge.get_settings()["sampling_baseline"], QWEN_SAMPLING)

    def test_no_profile_and_no_baseline_changes_nothing(self):
        self.assertEqual(bridge._apply_model_sampling(QWEN), "")
        self.assertEqual(self.sampling(), QWEN_SAMPLING)

    def test_edit_while_sage_loaded_is_remembered_for_sage_only(self):
        bridge._apply_model_sampling(SAGE)
        s = bridge.get_settings()
        s["model_path"] = SAGE
        bridge.save_json(bridge.SETTINGS_FILE, s)
        # The settings form posts every slider; only temperature changed.
        bridge.update_settings({**self.sampling(), "temperature": 0.8})
        self.assertEqual(bridge._models_config()[SAGE]["sampling"], {"temperature": 0.8})
        self.assertEqual(bridge.get_settings()["sampling_baseline"], QWEN_SAMPLING)
        bridge._apply_model_sampling(QWEN)
        self.assertEqual(self.sampling(), QWEN_SAMPLING)
        bridge._apply_model_sampling(SAGE)
        self.assertEqual(self.sampling()["temperature"], 0.8)
        self.assertEqual(self.sampling()["presence_penalty"], 1.5)

    def test_unchanged_form_save_writes_no_model_sampling(self):
        bridge._apply_model_sampling(SAGE)
        s = bridge.get_settings()
        s["model_path"] = SAGE
        bridge.save_json(bridge.SETTINGS_FILE, s)
        bridge.update_settings(self.sampling())
        self.assertNotIn("sampling", bridge._models_config().get(SAGE, {}))

    def test_edits_without_profile_stay_global(self):
        bridge.update_settings({"temperature": 0.65})
        self.assertEqual(bridge.get_settings()["temperature"], 0.65)
        self.assertEqual(bridge._models_config(), {})

    def test_llama_options_carries_profile(self):
        bridge._apply_model_sampling(SAGE)
        opt = bridge.llama_options(bridge.get_settings())
        self.assertEqual((opt["temperature"], opt["top_p"], opt["top_k"], opt["min_p"],
                          opt["repeat_penalty"], opt["presence_penalty"]),
                         (1.0, 0.95, 20.0, 0.0, 1.0, 1.5))


class SettingsValidationTests(_Store):
    def test_verification_mode_and_limits_are_clamped(self):
        bridge.update_settings({"verification_mode": "sometimes", "subagent_max_tasks": 99,
                                "subagent_max_turns": 0})
        s = bridge.get_settings()
        self.assertEqual((s["verification_mode"], s["subagent_max_tasks"], s["subagent_max_turns"]),
                         ("auto", 8, 2))

    def test_baseline_is_not_settable_from_the_api(self):
        bridge.update_settings({"sampling_baseline": {"temperature": 2}})
        self.assertNotIn("sampling_baseline", bridge.get_settings())


SAGE_TEMPLATE = ("{%- set verify_on = verification is defined and verification %}"
                 "{{- '\\n\\nVerification protocol: enabled' if verify_on }}")


class VerificationTests(unittest.TestCase):
    def setUp(self):
        bridge._VERIFY_CAP_CACHE.clear()
        self.addCleanup(bridge._VERIFY_CAP_CACHE.clear)

    def _meta(self, template):
        return bridge_patch("read_gguf_metadata",
                            return_value={"keys": {"tokenizer.chat_template": template}})

    def test_detects_sage_template(self):
        with self._meta(SAGE_TEMPLATE):
            self.assertTrue(bridge._template_supports_verification(SAGE, props={}))

    def test_plain_qwen_template_is_not_detected(self):
        with self._meta("{%- if enable_thinking is defined and enable_thinking is false %}"):
            self.assertFalse(bridge._template_supports_verification(QWEN, props={}))

    def test_wanted_follows_mode_and_effort(self):
        with self._meta(SAGE_TEMPLATE), bridge_patch("_llama_props_data", return_value={}):
            self.assertTrue(bridge._verification_wanted({"verification_mode": "auto"}, "high", SAGE))
            self.assertFalse(bridge._verification_wanted({"verification_mode": "auto"}, "medium", SAGE))
            self.assertTrue(bridge._verification_wanted({"verification_mode": "on"}, "low", SAGE))
            self.assertFalse(bridge._verification_wanted({"verification_mode": "off"}, "high", SAGE))
        bridge._VERIFY_CAP_CACHE.clear()
        with self._meta("plain"), bridge_patch("_llama_props_data", return_value={}):
            self.assertFalse(bridge._verification_wanted({"verification_mode": "on"}, "high", QWEN))


class ToolGatingTests(_Store):
    def test_spawn_subagent_only_for_sage(self):
        self.assertIn("spawn_subagent", bridge._excluded_tools(True, ""))
        s = bridge.get_settings()
        s["model_path"] = SAGE
        bridge.save_json(bridge.SETTINGS_FILE, s)
        self.assertNotIn("spawn_subagent", bridge._excluded_tools(True, ""))
        self.assertIn("spawn_subagent", bridge._visible_tool_names(True, ""))

    def test_tool_refuses_on_other_models(self):
        result = bridge._spawn_subagent_tool({"tasks": [{"task": "x", "context": "", "expected_output": "y"}]})
        self.assertTrue(result.get("not_executed"))

    def test_subagent_tools_are_read_only(self):
        names = {t["function"]["name"] for t in bridge._subagent_tools("")}
        self.assertTrue(names)
        self.assertTrue(names <= set(bridge._SUBAGENT_READ_TOOLS))
        self.assertNotIn("spawn_subagent", names)

    def test_spec_matches_published_schema(self):
        params = bridge.TOOLS["spawn_subagent"]["parameters"]
        item = params["properties"]["tasks"]["items"]
        self.assertEqual(params["required"], ["tasks"])
        self.assertEqual(item["required"], ["task", "context", "expected_output"])


class ParseCallTests(unittest.TestCase):
    def test_native_tool_calls(self):
        calls = bridge._subagent_parse_calls({"tool_calls": [
            {"id": "c1", "function": {"name": "web_search", "arguments": '{"query": "x"}'}}]})
        self.assertEqual(calls, [{"id": "c1", "name": "web_search", "arguments": {"query": "x"}}])

    def test_final_json_is_not_a_tool_call(self):
        msg = {"content": '{"answer": "1998", "status": "answered", "evidence": [], "unresolved_issues": []}'}
        self.assertEqual(bridge._subagent_parse_calls(msg), [])


TASK = {"task": "When was X founded?", "context": "X is a company.", "expected_output": "a year YYYY"}
FINAL = json.dumps({"answer": "1998", "status": "answered",
                    "evidence": [{"source": "https://x.example", "detail": "about page"}],
                    "unresolved_issues": []})
TOOLS = [{"type": "function", "function": {"name": "web_search", "parameters": {}}}]


def _parse(message):
    return [{"id": c["id"], "name": c["function"]["name"],
             "arguments": json.loads(c["function"]["arguments"])}
            for c in message.get("tool_calls") or []]


def _scripted(*replies):
    replies = list(replies)
    seen = []

    def complete(messages, tools):
        seen.append((list(messages), tools))
        return replies.pop(0), {"prompt_tokens": 100}
    return complete, seen


def _call(name, **args):
    return {"content": "", "tool_calls": [{"id": "t1", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}]}


class SubagentLoopTests(unittest.TestCase):
    def test_validate(self):
        self.assertEqual(subagents.validate_tasks({"tasks": [TASK]}, 5), ([TASK], ""))
        self.assertIn("at most 2", subagents.validate_tasks({"tasks": [TASK] * 3}, 2)[1])
        self.assertIn("missing", subagents.validate_tasks({"tasks": [{"task": "x"}]}, 5)[1])
        self.assertTrue(subagents.validate_tasks({"tasks": json.dumps([TASK])}, 5)[0])
        self.assertTrue(subagents.validate_tasks({"tasks": []}, 5)[1])

    def test_parse_result(self):
        self.assertEqual(subagents.parse_result("```json\n" + FINAL + "\n```")["answer"], "1998")
        loose = subagents.parse_result("I could not find it.")
        self.assertEqual(loose["status"], "partial")
        self.assertEqual(subagents.parse_result("")["answer"], "UNKNOWN")
        odd = subagents.parse_result('{"answer": "a", "status": "great"}')
        self.assertEqual(odd["status"], "partial")

    def test_tool_round_then_answer(self):
        complete, seen = _scripted(_call("web_search", query="X founded"), {"content": FINAL})
        tool_args = []
        result = subagents.run_one(TASK, complete=complete, parse_calls=_parse,
                                   call_tool=lambda n, a: tool_args.append(a) or {"results": ["1998"]},
                                   tools=TOOLS, max_turns=5)
        self.assertEqual((result["answer"], result["status"], result["tool_calls"]), ("1998", "answered", 1))
        self.assertEqual(tool_args, [{"query": "X founded"}])
        first_messages = seen[0][0]
        self.assertEqual(first_messages[0]["role"], "system")
        self.assertIn("a year YYYY", first_messages[1]["content"])
        self.assertEqual(seen[1][0][-1]["role"], "tool")

    def test_unknown_tool_is_refused_not_run(self):
        complete, seen = _scripted(_call("write_file", path="a"), {"content": FINAL})
        ran = []
        subagents.run_one(TASK, complete=complete, parse_calls=_parse,
                          call_tool=lambda n, a: ran.append(n), tools=TOOLS, max_turns=5)
        self.assertEqual(ran, [])
        self.assertIn("not available", seen[1][0][-1]["content"])

    def test_turn_budget_forces_final_answer_without_tools(self):
        complete, seen = _scripted(_call("web_search", query="a"), _call("web_search", query="b"),
                                   {"content": FINAL})
        result = subagents.run_one(TASK, complete=complete, parse_calls=_parse,
                                   call_tool=lambda n, a: {}, tools=TOOLS, max_turns=2)
        self.assertEqual(result["answer"], "1998")
        self.assertEqual(seen[-1][1], [])
        self.assertEqual(seen[-1][0][-1]["content"], subagents.FINAL_NUDGE)

    def test_context_budget_forces_final_answer(self):
        replies = [_call("web_search", query="a"), {"content": FINAL}]
        seen = []

        def complete(messages, tools):
            seen.append(tools)
            return replies.pop(0), {"prompt_tokens": 9000}
        result = subagents.run_one(TASK, complete=complete, parse_calls=_parse,
                                   call_tool=lambda n, a: {}, tools=TOOLS, max_turns=10, ctx_budget=8000)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(seen, [TOOLS, []])

    def test_model_failure_and_cancel(self):
        def boom(messages, tools):
            raise OSError("connection refused")
        failed = subagents.run_one(TASK, complete=boom, parse_calls=_parse,
                                   call_tool=lambda n, a: {}, tools=TOOLS, max_turns=3)
        self.assertEqual(failed["status"], "error")
        stopped = subagents.run_one(TASK, complete=boom, parse_calls=_parse, call_tool=lambda n, a: {},
                                    tools=TOOLS, max_turns=3, cancelled=lambda: True)
        self.assertEqual(stopped["unresolved_issues"], ["Stopped by the user."])

    def test_batch_keeps_input_order_and_runs_concurrently(self):
        active, peak, lock = [0], [0], threading.Lock()

        def complete(messages, tools):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            year = messages[1]["content"].split("#")[1].split()[0]
            return {"content": json.dumps({"answer": year, "status": "answered"})}, {}
        tasks = [{**TASK, "task": f"Task #{i} please"} for i in range(4)]
        results = subagents.run_batch(tasks, concurrency=4, complete=complete, parse_calls=_parse,
                                      call_tool=lambda n, a: {}, tools=TOOLS, max_turns=2)
        self.assertEqual([r["answer"] for r in results], ["0", "1", "2", "3"])
        self.assertGreater(peak[0], 1)
        peak[0] = 0
        subagents.run_batch(tasks, concurrency=1, complete=complete, parse_calls=_parse,
                            call_tool=lambda n, a: {}, tools=TOOLS, max_turns=2)
        self.assertEqual(peak[0], 1)


if __name__ == "__main__":
    unittest.main()
