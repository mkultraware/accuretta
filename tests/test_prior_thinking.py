"""preserve_prior_thinking=False must actually keep old <think> blocks off the
wire. It used to rely on the chat template to drop them, but Qwen3.5-family
templates and llama.cpp's reasoning-preserve default replay every one — 31% of
a real 91K-token agent prompt. The stored conversation (compaction's input)
must keep its reasoning; only the outgoing copy is trimmed.

Reasoning is stripped only from earlier tasks. Within the current task (since
the user's latest real message) every round keeps it, so the prompt only grows
and hybrid models (which cannot rewind their recurrent state) reuse the whole
prompt cache instead of falling back to a checkpoint 8K+ tokens back."""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import bridge
from _patching import bridge_patch

OFF = {"preserve_prior_thinking": False}


def call(i):
    return [{"id": f"c{i}", "function": {"name": "list_directory", "arguments": "{}"}}]


class WireViewTests(unittest.TestCase):
    def history(self):
        # Two earlier rounds belong to a finished task; the user then asked
        # something new, and the current task has run one round so far.
        return [
            {"role": "user", "content": "Map the API"},
            {"role": "assistant", "content": "<think>old plan</think>", "tool_calls": call(1)},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "assistant", "content": "<think>older note</think>Checking the bundle.", "tool_calls": call(2)},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "user", "content": "Now check the auth routes"},
            {"role": "assistant", "content": "<think>latest plan</think>", "tool_calls": call(3)},
            {"role": "tool", "tool_call_id": "c3", "content": "r3"},
        ]

    def test_off_strips_earlier_tasks_only(self):
        history = self.history()
        before = copy.deepcopy(history)
        view = bridge._thinking_wire_view(history, OFF)
        self.assertEqual(view[1]["content"], "")
        self.assertEqual(view[3]["content"], "Checking the bundle.")
        self.assertEqual(view[6]["content"], "<think>latest plan</think>")
        self.assertEqual([m.get("tool_calls") for m in view], [m.get("tool_calls") for m in history])
        self.assertEqual(history, before, "stored conversation must not change")

    def test_current_task_keeps_every_round(self):
        history = [
            {"role": "user", "content": "Map the API"},
            {"role": "assistant", "content": "<think>plan one</think>", "tool_calls": call(1)},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "assistant", "content": "<think>plan two</think>", "tool_calls": call(2)},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "assistant", "content": "<think>plan three</think>", "tool_calls": call(3)},
            {"role": "tool", "tool_call_id": "c3", "content": "r3"},
        ]
        view = bridge._thinking_wire_view(history, OFF)
        self.assertEqual([m["content"] for m in view if m["role"] == "assistant"],
                         ["<think>plan one</think>", "<think>plan two</think>", "<think>plan three</think>"])

    def test_harness_messages_do_not_start_a_new_task(self):
        injected = [
            {"role": "user", "content": "[automatic note] You announced more steps but stopped."},
            {"role": "user", "content": "[automatic recovery] The oversized tool call did not finish."},
            {"role": "user", "content": "[loop guard] The harness refused your repeated call."},
            {"role": "user", "content": "Stop thinking now.", "_thinking_note": True},
            {"role": "user", "content": "also check the logs", "_steering_id": "s1"},
            {"role": "user", "content": "The research notebook has evidence.", "_synthetic": True},
        ]
        for note in injected:
            with self.subTest(note=note["content"][:30]):
                history = [
                    {"role": "user", "content": "Map the API"},
                    {"role": "assistant", "content": "<think>plan one</think>", "tool_calls": call(1)},
                    {"role": "tool", "tool_call_id": "c1", "content": "r1"},
                    note,
                    {"role": "assistant", "content": "<think>plan two</think>", "tool_calls": call(2)},
                ]
                view = bridge._thinking_wire_view(history, OFF)
                self.assertEqual(view[1]["content"], "<think>plan one</think>")

    def test_rounds_within_a_task_only_append(self):
        # The cache property itself: what round N sends is an exact prefix of
        # what round N+1 sends, so llama.cpp never has to rewind.
        rounds = [{"role": "user", "content": "Map the API"}]
        sent = []
        for i in range(1, 6):
            rounds += [{"role": "assistant", "content": f"<think>plan {i}</think>Step {i}.", "tool_calls": call(i)},
                       {"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i}"}]
            with bridge_patch("get_settings", lambda: OFF):
                sent.append(bridge._sanitize_messages_for_openai(list(rounds), stub_old_tools=False))
        for a, b in zip(sent, sent[1:]):
            self.assertEqual(b[:len(a)], a)

    def test_on_leaves_history_alone(self):
        history = self.history()
        self.assertIs(bridge._thinking_wire_view(history, {"preserve_prior_thinking": True}), history)

    def test_sanitizer_applies_the_same_view(self):
        with bridge_patch("get_settings", lambda: OFF):
            wire = bridge._sanitize_messages_for_openai(self.history(), stub_old_tools=False)
        self.assertNotIn("old plan", json.dumps(wire))
        self.assertNotIn("older note", json.dumps(wire))
        self.assertIn("latest plan", json.dumps(wire))
        self.assertEqual(wire[3]["content"], "Checking the bundle.")

    def test_marker_form_rounds_are_stripped(self):
        # What the stream loop actually stores: reasoning closed by the bare
        # "\n response" boundary, no </think>. The closer-only regex missed it,
        # so the reasoning and the marker went back to the model every round.
        history = [
            {"role": "user", "content": "Build it"},
            {"role": "assistant", "content": "<think>old plan\n responseChecking first.", "tool_calls": call(1)},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "assistant", "content": "<think>plan B\n responseFixing three bugs.</think>", "tool_calls": call(2)},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
            {"role": "user", "content": "Now write it"},
            {"role": "assistant", "content": "<think>latest plan\n responseWriting it.", "tool_calls": call(3)},
            {"role": "tool", "tool_call_id": "c3", "content": "r3"},
        ]
        before = copy.deepcopy(history)
        view = bridge._thinking_wire_view(history, OFF)
        self.assertEqual(view[1]["content"], "Checking first.")
        self.assertEqual(view[3]["content"], "Fixing three bugs.")
        # The kept round goes out with real tags the chat template understands.
        self.assertEqual(view[6]["content"], "<think>latest plan</think>Writing it.")
        self.assertNotIn("\n response", json.dumps(view))
        self.assertEqual([m.get("tool_calls") for m in view], [m.get("tool_calls") for m in history])
        self.assertEqual(history, before, "stored conversation must not change")

    def test_close_think_marker_edges(self):
        close = bridge._close_think_marker
        self.assertEqual(close("<think>plan only\n response"), "<think>plan only</think>")
        self.assertEqual(close("<think>plan</think>Answer"), "<think>plan</think>Answer")
        self.assertEqual(close("<think>still thinking"), "<think>still thinking")
        self.assertEqual(close("no tags\n responseTime ok"), "no tags\n responseTime ok")
        self.assertEqual(close("<think>the\n responses differ\n responseOK"),
                         "<think>the\n responses differ</think>OK")
        once = close("<think>a\n responseb\n responseAnswer")
        self.assertEqual(close(once), once, "must be idempotent")

    def test_preserve_mode_rewrites_marker_rounds_as_scratchpad(self):
        history = [
            {"role": "user", "content": "Build it"},
            {"role": "assistant", "content": "<think>old plan\n responseChecking first.", "tool_calls": call(1)},
            {"role": "tool", "tool_call_id": "c1", "content": "r1"},
            {"role": "assistant", "content": "<think>latest plan\n responseWriting it.", "tool_calls": call(2)},
            {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        ]
        with bridge_patch("get_settings", lambda: {"preserve_prior_thinking": True}):
            wire = bridge._sanitize_messages_for_openai(
                bridge._thinking_wire_view(history, {"preserve_prior_thinking": True}),
                stub_old_tools=False)
        self.assertEqual(wire[1]["content"],
                         "[scratchpad-from-earlier-turn]\nold plan\n[/scratchpad-from-earlier-turn]Checking first.")
        self.assertEqual(wire[3]["content"], "<think>latest plan</think>Writing it.")

    def test_sanitizer_preserve_mode_unchanged(self):
        with bridge_patch("get_settings", lambda: {"preserve_prior_thinking": True}):
            wire = bridge._sanitize_messages_for_openai(self.history(), stub_old_tools=False)
        self.assertIn("[scratchpad-from-earlier-turn]\nold plan", wire[1]["content"])
        self.assertEqual(wire[6]["content"], "<think>latest plan</think>")


class ReasoningFieldTests(unittest.TestCase):
    """Templates that read reasoning_content print their own think block from
    the field and never parse <think> inside content, so inline reasoning came
    out doubled: an empty <think></think> followed by the real one."""

    HISTORY = [
        {"role": "user", "content": "Map the API"},
        {"role": "assistant", "content": "<think>\nold plan\n</think>\n\nChecked.", "tool_calls": call(1)},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "user", "content": "Now the auth routes"},
        {"role": "assistant", "content": "<think>\nnew plan\n</think>\n\nChecking auth.", "tool_calls": call(2)},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
    ]

    def sent(self, reads_field, settings=None):
        s = settings or {**OFF, "model_path": "m.gguf"}
        with bridge_patch("get_settings", lambda: s), \
             bridge_patch("_template_reads_reasoning_content", lambda p: reads_field):
            return [m for m in bridge._sanitize_messages_for_openai(self.HISTORY, stub_old_tools=False)
                    if m["role"] == "assistant"]

    def test_reasoning_moves_to_its_own_field(self):
        earlier, current = self.sent(True)
        self.assertEqual(current["reasoning_content"], "new plan")
        self.assertEqual(current["content"], "Checking auth.")
        # An earlier task's reasoning is stripped, so there is nothing to move.
        self.assertNotIn("reasoning_content", earlier)
        self.assertEqual(earlier["content"], "Checked.")
        self.assertEqual(current["tool_calls"][0]["function"]["name"], "list_directory")

    def test_templates_without_the_field_keep_inline_reasoning(self):
        _, current = self.sent(False)
        self.assertNotIn("reasoning_content", current)
        self.assertIn("<think>\nnew plan\n</think>", current["content"])

    def test_native_mode_is_untouched(self):
        with bridge_patch("_prior_thinking_mode", lambda settings=None: "native"):
            earlier, current = self.sent(True)
        self.assertIn("<think>\nold plan\n</think>", earlier["content"])
        self.assertNotIn("reasoning_content", current)

    def test_split_edges(self):
        split = bridge._split_reasoning_field
        self.assertEqual(split("<think>\nR\n</think>\n\nA"), ("R", "A"))
        self.assertEqual(split("<think>R</think>"), ("R", ""))
        self.assertIsNone(split("no reasoning"))
        self.assertIsNone(split("<think>unclosed"))
        self.assertIsNone(split("Answer <think>later</think>"))

    def test_context_estimate_counts_the_field(self):
        inline = {"role": "assistant", "content": "<think>" + "word " * 200 + "</think>Done."}
        field = {"role": "assistant", "reasoning_content": "word " * 200, "content": "Done."}
        self.assertGreater(bridge._count_msg_tokens(field), bridge._count_msg_tokens(inline) * 0.9)

    def test_template_detection(self):
        bridge._REASONING_FIELD_CACHE.clear()
        qwen = "{%- if message.reasoning_content is string %}{{ message.reasoning_content }}{% endif %}"
        with bridge_patch("read_gguf_metadata",
                          lambda p: {"keys": {"tokenizer.chat_template": qwen}}):
            self.assertTrue(bridge._template_reads_reasoning_content("a.gguf"))
        with bridge_patch("read_gguf_metadata",
                          lambda p: {"keys": {"tokenizer.chat_template": "{{ message.content }}"}}):
            self.assertFalse(bridge._template_reads_reasoning_content("b.gguf"))
        self.assertFalse(bridge._template_reads_reasoning_content(""))
        bridge._REASONING_FIELD_CACHE.clear()


class AgentLoopTests(unittest.TestCase):
    def test_current_task_reasoning_stays_on_wire_and_in_saved_turn(self):
        requests = []
        rounds = [
            {"content": "<think>round one reasoning</think>", "tool_calls": [{
                "index": 0, "id": "r1", "function": {"name": "list_directory", "arguments": json.dumps({"path": "C:/a"})}}]},
            {"content": "<think>round two reasoning</think>", "tool_calls": [{
                "index": 0, "id": "r2", "function": {"name": "list_directory", "arguments": json.dumps({"path": "C:/b"})}}]},
            {"content": "Both folders hold the recon files."},
        ]

        class Response:
            def __init__(self, delta):
                self.delta = delta

            def __iter__(self):
                yield ("data: " + json.dumps({"choices": [{"delta": self.delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            self.assertLessEqual(len(requests), 3)
            return Response(rounds[len(requests) - 1])

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False,
                    "preserve_prior_thinking": False}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda: None,
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
            stack.enter_context(bridge_patch("invoke_tool", return_value={"ok": True, "entries": []}))
            result = bridge.run_chat_turn("prior-thinking", [
                {"role": "user", "content": "List the recon folders."}], True, lambda e: None)

        self.assertEqual(len(requests), 3)
        third = json.dumps(requests[2]["messages"])
        # One task: every round keeps its reasoning, the task-state note stays
        # where it was sent, and each request is an exact extension of the
        # previous one, so the prompt cache is reused whole.
        self.assertIn("round one reasoning", third)
        self.assertIn("round two reasoning", third)
        for earlier, later in zip(requests, requests[1:]):
            self.assertEqual(later["messages"][:len(earlier["messages"])], earlier["messages"])
        self.assertEqual(third.count("[task state from the harness"), 1)
        # Tool calls themselves stay on the wire.
        self.assertEqual(sum(1 for m in requests[2]["messages"] if m.get("tool_calls")), 2)
        # What gets saved (and later summarized by compaction) keeps all reasoning.
        saved = json.dumps(result["_appended_intermediate"])
        self.assertIn("round one reasoning", saved)
        self.assertIn("round two reasoning", saved)

    def test_reasoning_channel_rounds_leave_wire_without_marker(self):
        # llama-server's reasoning_content channel: the bridge stores each
        # round as "<think>R response<answer>". Those must strip like closed
        # blocks, and no " response" marker may reach the model.
        requests = []
        rounds = [
            [{"reasoning_content": "round one reasoning\n"},
             {"content": "Checking a.", "tool_calls": [{
                 "index": 0, "id": "r1", "function": {"name": "list_directory", "arguments": json.dumps({"path": "C:/a"})}}]}],
            [{"reasoning_content": "round two reasoning\n"},
             {"content": "Checking b.", "tool_calls": [{
                 "index": 0, "id": "r2", "function": {"name": "list_directory", "arguments": json.dumps({"path": "C:/b"})}}]}],
            [{"content": "Both folders hold the recon files."}],
        ]

        class Response:
            def __init__(self, deltas):
                self.deltas = deltas

            def __iter__(self):
                for delta in self.deltas:
                    yield ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            self.assertLessEqual(len(requests), 3)
            return Response(rounds[len(requests) - 1])

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False,
                    "preserve_prior_thinking": False}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda: None,
            "_rt_trailing_system_ok": lambda: True,
            "_llama_props_ctx": lambda: 32768, "_conversation_token_scale": lambda *a, **kw: 1,
            "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn",
            "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: {"chats": {}, "order": []},
            "_mark_model_reasoning_observed": lambda *a, **kw: None,
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            stack.enter_context(bridge_patch("invoke_tool", return_value={"ok": True, "entries": []}))
            result = bridge.run_chat_turn("prior-thinking-channel", [
                {"role": "user", "content": "List the recon folders."}], True, lambda e: None)

        self.assertEqual(len(requests), 3)
        third = json.dumps(requests[2]["messages"])
        self.assertIn("round one reasoning", third)
        self.assertIn("Checking a.", third)
        self.assertIn("round two reasoning", third)
        self.assertNotIn(" response", third)
        self.assertEqual(sum(1 for m in requests[2]["messages"] if m.get("tool_calls")), 2)
        saved = json.dumps(result["_appended_intermediate"])
        self.assertIn("round one reasoning", saved)
        self.assertIn("round two reasoning", saved)


if __name__ == "__main__":
    unittest.main()
