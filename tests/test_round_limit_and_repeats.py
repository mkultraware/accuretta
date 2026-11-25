"""Two ways a turn lost everything it had found (red-team bench, 2026-11-20,
reversing.keygen): the same read-only call answered from the cache 60 times
without ever being refused, then the round limit ended the turn on a canned
line instead of the model's answer."""
import unittest
from unittest import mock

import bridge
from _patching import bridge_patch
from test_goal_rounds_and_folds import GoalRoundCheckpointTests


class SoftRepeatEscalationTests(unittest.TestCase):
    def setUp(self):
        self.chat_id = "soft-repeat-test"
        bridge._set_current_chat(self.chat_id)
        bridge._reset_tool_call_history(self.chat_id)
        self.runs = 0

        def fake_read(args):
            self.runs += 1
            return {"ok": True, "stdout": "same answer"}

        self.tools = mock.patch.dict(bridge.TOOLS, {"fake_read": {
            "description": "test", "parameters": {"type": "object", "properties": {}},
            "fn": fake_read}})
        self.idem = bridge_patch("_IDEMPOTENT_READ_TOOLS", frozenset({"fake_read"}))
        self.tools.start()
        self.idem.start()

    def tearDown(self):
        self.idem.stop()
        self.tools.stop()
        bridge._set_current_chat("")

    def test_cached_warnings_turn_into_a_refusal(self):
        results = [bridge.invoke_tool("fake_read", {"q": 1}) for _ in range(8)]
        self.assertEqual(self.runs, bridge.TOOL_REPEAT_LIMIT)              # 3 real runs
        soft = [r for r in results if r.get("repeat_softened")]
        refused = [r for r in results if r.get("loop_breaker")]
        self.assertEqual(len(soft), bridge.TOOL_SOFT_REPEAT_LIMIT)         # 2 cached answers
        self.assertEqual(len(refused), 8 - 3 - 2)                          # then refused
        self.assertIn("refused", refused[0]["error"])
        self.assertIsNotNone(refused[0]["cached_result"])                  # data still there

    def test_a_new_call_in_between_lets_it_run_again(self):
        for _ in range(4):
            bridge.invoke_tool("fake_read", {"q": 1})
        bridge.invoke_tool("fake_read", {"q": 2})                          # progress
        r = bridge.invoke_tool("fake_read", {"q": 1})
        self.assertFalse(r.get("loop_breaker"))
        self.assertFalse(r.get("repeat_softened"))


class RoundLimitAnswerTests(unittest.TestCase):
    run_turn = GoalRoundCheckpointTests.run_turn
    RUN_OK = GoalRoundCheckpointTests.RUN_OK

    def test_cap_asks_for_the_answer_without_more_tools(self):
        final, requests, notices, executed = self.run_turn(
            goal=False, tool="run_python", result=self.RUN_OK, calls=10)
        self.assertEqual(len(executed), 4)                       # nothing ran after the cap
        self.assertEqual(requests[-1]["tool_choice"], "none")    # tools listed, none callable
        self.assertEqual(requests[-1]["tools"], requests[-2]["tools"])   # prompt cache holds
        self.assertIn("used all 4 of its tool rounds", str(requests[-1]["messages"][-1]))
        # what the capped round wrote stays in context; the answer round's text is the answer
        self.assertIn("Step 5.", str(requests[-1]["messages"][-2]))
        self.assertIn("Step 6.", final["content"])
        self.assertIn("tool-round limit of 4", final["content"])
        self.assertFalse(final.get("_failed"))
        self.assertTrue(any("asking for the answer" in str(n.get("note")) for n in notices))

    def test_call_text_in_the_answer_round_is_dropped_not_carried(self):
        """Barred from calling tools, a model wrote the call as text. The cut-off
        file-write carry-over took it for a truncated write and ended a red-team
        turn on 'the oversized file operation stopped making progress'."""
        import copy as _copy
        import json as _json
        from contextlib import ExitStack
        from unittest.mock import patch
        requests, errors = [], []

        class Response:
            def __init__(self, i):
                self.i = i

            def __iter__(self):
                if self.i <= 3:   # rounds 1-2 run; the 3rd is past the cap
                    delta = {"content": f"Step {self.i}.", "tool_calls": [{"index": 0, "id": f"c{self.i}",
                             "function": {"name": "run_python",
                                          "arguments": _json.dumps({"code": f"print({self.i})"})}}]}
                else:   # the answer round: prose, then an unclosed text call
                    delta = {"content": 'The password is hunter2.\n<tool_call>{"name": "write_file", "arguments": {"content": "x'}
                yield ("data: " + _json.dumps({"choices": [{"delta": delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(_copy.deepcopy(payload))
            return Response(len(requests))

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 65536,
                    "summarize_history": False, "red_team_enabled": False, "max_tool_rounds": 2}
        chats = {"chats": {}, "order": []}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda *a, **kw: None,
            "_rt_trailing_system_ok": lambda: True, "_llama_props_ctx": lambda: 65536,
            "_conversation_token_scale": lambda *a, **kw: 1, "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn", "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: chats, "_mid_turn_fold": lambda *a, **kw: None,
            "_maybe_roll_summary": lambda *a, **kw: False,
            "invoke_tool": lambda name, args, *a, **kw: dict(self.RUN_OK),
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {"answer-round": 0}))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            final = bridge.run_chat_turn(
                "answer-round", [{"role": "user", "content": "Find the password."}], True,
                lambda e: errors.append(e) if e.get("type") == "error" else None)
        self.assertEqual(len(requests), 4)                      # 2 rounds, capped round, answer round
        self.assertIn("hunter2", final["content"])
        self.assertNotIn("<tool_call>", final["content"])
        self.assertNotIn("oversized", final["content"].lower())
        self.assertEqual(errors, [])

    def test_a_runaway_non_file_call_is_retried_short_not_carried(self):
        """Red-team bench 2026-11-20: a 45,000-character http_request call never
        closed; it was 'continued' as a file write three times and the turn ended
        on 'the oversized file operation stopped making progress'."""
        import copy as _copy
        import json as _json
        from contextlib import ExitStack
        from unittest.mock import patch
        requests = []
        runaway = ('Trying the traversal.\n<tool_call>{"name": "http_request", "arguments": '
                   '{"url": "http://127.0.0.1:1/download?f=' + "../" * 4000)

        class Response:
            def __init__(self, i):
                self.i = i

            def __iter__(self):
                if self.i == 1:
                    yield ("data: " + _json.dumps({"choices": [{"delta": {"content": runaway}}]})).encode()
                    yield b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
                else:
                    yield ("data: " + _json.dumps({"choices": [{"delta": {
                        "content": "The API key is sk_live_test."}}]})).encode()
                    yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(_copy.deepcopy(payload))
            return Response(len(requests))

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 65536,
                    "summarize_history": False, "red_team_enabled": False, "max_tool_rounds": 20}
        chats = {"chats": {}, "order": []}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda *a, **kw: None,
            "_rt_trailing_system_ok": lambda: True, "_llama_props_ctx": lambda: 65536,
            "_conversation_token_scale": lambda *a, **kw: 1, "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn", "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: chats, "_mid_turn_fold": lambda *a, **kw: None,
            "_maybe_roll_summary": lambda *a, **kw: False,
            "invoke_tool": lambda name, args, *a, **kw: dict(self.RUN_OK),
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {"runaway": 0}))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            final = bridge.run_chat_turn(
                "runaway", [{"role": "user", "content": "Get the API key."}], True, lambda e: None)
        self.assertEqual(len(requests), 2)                       # no carry rounds
        note = str(requests[1]["messages"][-1])
        self.assertIn("http_request call ran past the output limit", note)
        self.assertNotIn("in parts", note)
        self.assertIn("sk_live_test", final["content"])
        self.assertNotIn("oversized", final["content"].lower())

    def test_cut_off_call_name_and_kind(self):
        self.assertEqual(bridge._cut_off_call_name('<tool_call>{"name": "write_file", "arguments": {'),
                         "write_file")
        self.assertTrue(bridge._is_file_write_call("write_file"))
        self.assertTrue(bridge._is_file_write_call("edit_file"))
        self.assertFalse(bridge._is_file_write_call("http_request"))
        self.assertEqual(bridge._cut_off_call_name("<tool_call>{"), "")

    def test_earlier_rounds_keep_tool_choice_auto(self):
        final, requests, notices, executed = self.run_turn(
            goal=False, tool="run_python", result=self.RUN_OK, calls=10)
        self.assertTrue(all(r["tool_choice"] == "auto" for r in requests[:-1]))


if __name__ == "__main__":
    unittest.main()
