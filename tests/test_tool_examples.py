import json
import copy
from contextlib import ExitStack
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch


PARTIAL = "<tool_call><function=write_file><parameter=path>..."
EXAMPLE = "- Emit a partial `" + PARTIAL + "` and just stop\n\nThe rest of the audit."
PIN = "<tool_call><function=pin_note><parameter=note>Audit done</parameter></function></tool_call>"


class ToolExampleTests(unittest.TestCase):
    def test_quoted_partial_call_is_neither_action_nor_unfinished(self):
        self.assertEqual(bridge.extract_tool_calls(EXAMPLE), [])
        self.assertFalse(bridge._has_unclosed_tool_call(EXAMPLE))
        self.assertEqual(bridge._unclosed_tool_tail(EXAMPLE), "")

    def test_examples_in_ordinary_fences_are_not_actions(self):
        samples = [
            "```xml\n" + PARTIAL + "\n```",
            "~~~xml\n" + PARTIAL + "\n~~~",
            '```json\n{"name":"write_file","arguments":{"path":"x","content":"y"}}\n```',
            "```python\nwrite_file(path='x', content='y')\n```",
            "``literal ` and " + PARTIAL + "``",
        ]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(bridge.extract_tool_calls(sample), [])
                self.assertFalse(bridge._has_unclosed_tool_call(sample))

    def test_real_call_after_example_is_preserved(self):
        calls = bridge.extract_tool_calls(EXAMPLE + "\n" + PIN)
        self.assertEqual(calls, [{"name": "pin_note", "arguments": {"note": "Audit done"}}])
        self.assertFalse(bridge._has_unclosed_tool_call(EXAMPLE + "\n" + PIN))

    def test_real_write_keeps_markdown_content(self):
        body = "# Notes\nUse `value` here.\n```python\nx = 1\n```"
        call = ("<tool_call><function=write_file><parameter=path>notes.md</parameter>"
                "<parameter=content>" + body + "</parameter></function></tool_call>")
        self.assertEqual(bridge.extract_tool_calls(call)[0]["arguments"]["content"], body)

    def test_bare_json_write_keeps_markdown_content(self):
        body = "Use `value` here."
        call = json.dumps({"name": "write_file", "arguments": {"path": "notes.md", "content": body}})
        self.assertEqual(bridge.extract_tool_calls(call)[0]["arguments"]["content"], body)

    def test_llama_json_write_keeps_markdown_content(self):
        body = "Use `value` here."
        call = '<|python_tag|>' + json.dumps({"name": "write_file", "parameters": {
            "path": "notes.md", "content": body}}) + '<|eom_id|>'
        self.assertEqual(bridge.extract_tool_calls(call)[0]["arguments"]["content"], body)

    def test_gemma_write_keeps_markdown_content(self):
        call = '<|tool_call>call:write_file{path:<|"|>notes.md<|"|>,content:<|"|>Use `value` here.<|"|>}<tool_call|>'
        self.assertEqual(bridge.extract_tool_calls(call)[0]["arguments"]["content"], "Use `value` here.")

    def test_real_unfinished_call_is_still_carried(self):
        text = EXAMPLE + "\n" + PARTIAL
        self.assertTrue(bridge._has_unclosed_tool_call(text))
        self.assertEqual(bridge._unclosed_tool_tail(text), PARTIAL)

    def test_dedicated_tool_fence_still_executes(self):
        text = '```tool_call\n{"name":"pin_note","arguments":{"note":"done"}}\n```'
        self.assertEqual(bridge.extract_tool_calls(text)[0]["name"], "pin_note")

    def test_stripping_actual_calls_keeps_examples(self):
        text = EXAMPLE + "\n" + PIN
        result = bridge._strip_tool_markup(text, r"<tool_call>[\s\S]*?(?:</tool_call>|$)")
        self.assertEqual(result, EXAMPLE + "\n")

    def test_stream_preserves_examples_at_every_chunk_boundary(self):
        examples = [EXAMPLE, "```xml\n" + PARTIAL + "\n```", "~~~xml\n" + PARTIAL + "\n~~~"]
        for example in examples:
            text = example + "\n" + PIN + "\nFinished."
            for split in range(len(text) + 1):
                with self.subTest(example=example, split=split):
                    first, state = bridge._classify_tool_stream(text[:split], False)
                    second, state = bridge._classify_tool_stream(text[split:], state)
                    self.assertEqual(first + second + bridge._flush_tool_stream(state),
                                     example + "\n\nFinished.")

    def test_stream_one_character_at_a_time(self):
        text = EXAMPLE + "\n" + PIN + "\nFinished."
        out, state = [], False
        for char in text:
            visible, state = bridge._classify_tool_stream(char, state)
            out.append(visible)
        self.assertEqual("".join(out) + bridge._flush_tool_stream(state), EXAMPLE + "\n\nFinished.")

    def test_write_requires_content_or_visible_source(self):
        for name in ("write_file", "remote_write_file"):
            for args in ({"path": "x"}, {"path": "x", "content": "  "}):
                self.assertIn("content", bridge._missing_required_tool_args(name, args))
            for args in ({"path": "x", "content": "hello"},
                         {"path": "x", "source": "visible_code_block"}):
                self.assertEqual(bridge._missing_required_tool_args(name, args), [])

    def test_native_prose_path_cannot_reach_file_handler(self):
        args = json.loads(json.dumps({"path": "...` and just stop\n\nThe rest of the audit."}))
        with bridge_patch("_selected_execution_target", return_value="host"), \
                bridge_patch("_record_action_audit"), \
                patch.dict(bridge.TOOLS["write_file"], {"fn": lambda args: self.fail("File handler ran")}):
            result = bridge.invoke_tool("write_file", args)
        self.assertTrue(result["not_executed"])
        self.assertTrue(result["invalid_path"])

    def test_native_failure_regenerates_answer_without_writing(self):
        requests, events = [], []

        class Response:
            def __init__(self, first):
                self.first = first

            def __iter__(self):
                delta = {"content": "Here is the audit. Emit a partial `", "tool_calls": [{
                    "index": 0, "id": "native-example", "function": {
                        "name": "write_file", "arguments": json.dumps({
                            "path": "...` and just stop\n\nThe rest of the audit."})}}]}
                if not self.first:
                    delta = {"content": "The completed audit explains the issue."}
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
            invoke = stack.enter_context(bridge_patch("invoke_tool"))
            result = bridge.run_chat_turn("tool-example-recovery", [
                {"role": "user", "content": "Audit this code. Explain the problems."}], True, events.append)
        invoke.assert_not_called()
        self.assertEqual(len(requests), 2)
        self.assertIn("completed audit", result["content"])
        recovery = "\n".join(str(message.get("content", "")) for message in requests[1]["messages"])
        self.assertIn("may have been mistaken", recovery)
        self.assertNotIn("Re-emit the SAME call", recovery)


if __name__ == "__main__":
    unittest.main()
