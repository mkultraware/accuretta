"""The per-round SESSION STATE block must never follow a tool result as its own
message. A trailing user/system message there reads as a new user turn, and
with the previous round's reasoning still in context (Qwen3.5-family templates
preserve it) the model replays that round byte for byte — the OneDoc
run_powershell loop. Replaying that 91K-token prompt on the real model: 2/2
verbatim copies with the separate message, 0/2 with the block folded in."""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import bridge
from _patching import bridge_patch

STATE = ("[task state from the harness: follow it, stay on the task, do not undo it, "
         "and do not restate or comment on it]")


class AttachSessionStateTests(unittest.TestCase):
    def test_mid_turn_folds_into_last_tool_result(self):
        history = [
            {"role": "user", "content": "Map the API"},
            {"role": "assistant", "content": "<think>plan</think>", "tool_calls": [
                {"id": "a", "function": {"name": "run_powershell", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "content": '{"ok": true}'},
        ]
        before = copy.deepcopy(history)
        wire = bridge._attach_session_state(history, STATE, "user")
        self.assertEqual(len(wire), len(history))
        self.assertEqual(wire[-1]["role"], "tool")
        self.assertEqual(wire[-1]["tool_call_id"], "a")
        self.assertTrue(wire[-1]["content"].startswith('{"ok": true}'))
        self.assertTrue(wire[-1]["content"].endswith(STATE))
        self.assertEqual(history, before, "the persisted conversation must not change")

    def test_turn_start_keeps_separate_message(self):
        history = [{"role": "user", "content": "Map the API"}]
        wire = bridge._attach_session_state(history, STATE, "system")
        self.assertEqual(wire[-1], {"role": "system", "content": STATE})
        self.assertEqual(len(history), 1)

    def test_non_text_tool_result_falls_back_to_separate_message(self):
        history = [{"role": "tool", "tool_call_id": "a",
                    "content": [{"type": "text", "text": "image result"}]}]
        wire = bridge._attach_session_state(history, STATE, "user")
        self.assertEqual(wire[-1], {"role": "user", "content": STATE})


class AgentLoopWireTests(unittest.TestCase):
    def test_round_after_tool_call_ends_on_the_tool_result(self):
        requests = []

        class Response:
            def __init__(self, first):
                self.first = first

            def __iter__(self):
                if self.first:
                    delta = {"content": "<think>scan the bundle</think>", "tool_calls": [{
                        "index": 0, "id": "scan-1", "function": {
                            "name": "list_directory", "arguments": json.dumps({"path": "C:/recon"})}}]}
                else:
                    delta = {"content": "The bundle lists three API routes."}
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            self.assertLessEqual(len(requests), 2)
            return Response(len(requests) == 1)

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda *a, **kw: None,
            "_rt_trailing_system_ok": lambda: True,
            "_llama_props_ctx": lambda: 32768, "_conversation_token_scale": lambda *a, **kw: 1,
            "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn",
            "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: {"chats": {}, "order": []},
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            stack.enter_context(bridge_patch("invoke_tool",
                                             return_value={"ok": True, "entries": ["main.js"]}))
            result = bridge.run_chat_turn("session-state-tail", [
                {"role": "user", "content": "Map the API from the recon folder."}], True, lambda e: None)

        self.assertEqual(len(requests), 2)
        self.assertIn("three API routes", result["content"])
        first, second = requests[0]["messages"], requests[1]["messages"]
        # Turn start: the block is its own message after the user's ask.
        self.assertTrue(str(first[-1]["content"]).startswith(STATE))
        # Mid-turn: the wire ends on the tool result, and no user/system message
        # follows it to fake a new turn. The unchanged block stays where it was
        # sent (after the ask) instead of moving, so this request is an exact
        # extension of the first one and the prompt cache is reused whole.
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "scan-1")
        self.assertEqual(sum(STATE in str(m.get("content")) for m in second), 1)
        self.assertEqual(second[:len(first)], first)
        last_tool = max(i for i, m in enumerate(second) if m["role"] == "tool")
        self.assertEqual(second[last_tool + 1:], [])

    def test_changed_block_rides_inside_the_newest_tool_result(self):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "Map the API"}]
        notes.apply(list(conv), STATE, ["plan: one"], "system", 0)
        conv += [{"role": "assistant", "content": "", "tool_calls": [
                     {"id": "a", "function": {"name": "run_powershell", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "a", "content": '{"ok": true}'}]
        wire = notes.apply(list(conv), STATE, ["plan: two"], "system", 1)
        self.assertEqual(wire[-1]["role"], "tool")
        self.assertTrue(wire[-1]["content"].startswith('{"ok": true}\n\n' + STATE))
        self.assertIn("plan: two", wire[-1]["content"])


if __name__ == "__main__":
    unittest.main()
