import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bridge


class TextToolProtocolTests(unittest.TestCase):
    def test_tool_code_is_protocol_not_visible_code(self):
        call = (
            "```tool_code\n"
            "write_file(path=\"C:/workspace/page.html\", content=\"hello\")\n"
            "```"
        )
        visible, state = bridge._classify_tool_stream(call, False)
        self.assertEqual(visible, "")
        self.assertFalse(state)
        self.assertEqual(
            bridge.extract_tool_calls(call),
            [{
                "name": "write_file",
                "arguments": {"path": "C:/workspace/page.html", "content": "hello"},
            }],
        )

    def test_split_tool_code_opener_is_suppressed_across_chunks(self):
        first, state = bridge._classify_tool_stream("```tool_", False)
        second, state = bridge._classify_tool_stream(
            "code\nwrite_file(path=\"C:/x\", content=\"ok\")", state)
        third, state = bridge._classify_tool_stream("\n```done", state)
        self.assertEqual(first + second + third, "done")
        self.assertFalse(state)

    def test_truncated_tool_code_is_carried_and_not_accepted_as_answer(self):
        partial = (
            "<think>planning\n response\n"
            "```tool_code\n"
            "write_file(path=\"C:/workspace/page.html\", content=\"unfinished"
        )
        self.assertTrue(bridge._has_unclosed_tool_call(partial))
        self.assertTrue(bridge._unclosed_tool_tail(partial).startswith("```tool_code"))
        self.assertEqual(bridge.extract_tool_calls(partial), [])
        self.assertEqual(bridge._visible_final_answer(partial), "")

    def test_tool_code_continuation_preserves_the_original_prefix(self):
        carried = "```tool_code\nwrite_file(path=\"C:/x\", content=\"first"
        merged = bridge._merge_carried_tool_call(carried, " second\")\n```")
        self.assertEqual(merged, carried + " second\")\n```")
        self.assertFalse(bridge._has_unclosed_tool_call(merged))

    def test_ordinary_python_fence_remains_a_visible_answer(self):
        answer = "```python\nprint('hello')\n```"
        self.assertEqual(bridge._visible_final_answer(answer), answer)


class ArtifactSaveTests(unittest.TestCase):
    def setUp(self):
        chats = {
            "chats": {
                "chat-test": {
                    "messages": [{
                        "role": "assistant",
                        "content": "```html\n<!doctype html>\n<title>Exact</title>\n```",
                    }],
                },
            },
        }
        self.chats_patch = mock.patch.object(bridge, "get_chats", return_value=chats)
        self.chats_patch.start()
        bridge._set_current_chat("chat-test")

    def tearDown(self):
        bridge._set_current_chat("")
        self.chats_patch.stop()

    def test_write_file_copies_prior_content_without_regeneration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "exact.html"
            with mock.patch.object(bridge, "is_in_workspace", return_value=True), \
                    mock.patch.object(bridge, "is_ignored", return_value=False), \
                    mock.patch.object(bridge, "request_approval", return_value={"decision": "approve"}), \
                    mock.patch.object(bridge, "_undo_snapshot"), \
                    mock.patch.object(bridge, "_record_file_read"), \
                    mock.patch.object(bridge, "_record_write"), \
                    mock.patch.object(bridge, "_write_count", return_value=0), \
                    mock.patch.object(bridge, "_run_linter", return_value=""):
                result = bridge.tool_write_file({
                    "path": str(destination),
                    "source": "visible_code_block",
                    "language": "html",
                })

            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "<!doctype html>\n<title>Exact</title>\n",
            )
        self.assertEqual(result["source"], "visible_chat_code_block")

    def test_remote_write_file_uses_the_same_source_contract(self):
        with mock.patch.object(bridge, "_selected_remote_machine", return_value={"label": "Mac"}), \
                mock.patch.object(bridge, "_normalize_remote_path", return_value="/tmp/exact.html"), \
                mock.patch.object(bridge, "_confirm_remote_path", return_value=("/tmp/exact.html", "")), \
                mock.patch.object(bridge, "_remote_expected_hash", return_value=""), \
                mock.patch.object(bridge, "_remote_write_approved_payload", return_value={"ok": True}) as write:
            result = bridge.tool_remote_write_file({
                "path": "/tmp/exact.html",
                "source": "visible_code_block",
                "language": "html",
            })

        self.assertEqual(
            write.call_args.args[2],
            b"<!doctype html>\n<title>Exact</title>\n",
        )
        self.assertEqual(result["source"], "visible_chat_code_block")

    def test_source_capability_does_not_add_a_tool(self):
        self.assertNotIn("save_code_block", bridge.TOOLS)
        self.assertNotIn("remote_save_code_block", bridge.TOOLS)
        self.assertIn("source", bridge.TOOLS["write_file"]["parameters"]["properties"])


class ExecutionTargetTests(unittest.TestCase):
    def test_remote_schema_excludes_pc_tools(self):
        chat_id = "remote-schema-test"
        bridge._client_context_by_chat[chat_id] = {"execution_target": "mac-test"}
        try:
            with mock.patch.object(bridge, "_excluded_tools", return_value=set()), \
                    mock.patch.object(bridge, "_base_core_tool_names", return_value={
                        "read_file", "write_file", "run_powershell", "web_search",
                    }):
                visible = bridge._visible_tool_names(chat_id=chat_id)
        finally:
            bridge._client_context_by_chat.pop(chat_id, None)

        self.assertNotIn("read_file", visible)
        self.assertNotIn("write_file", visible)
        self.assertNotIn("run_powershell", visible)
        self.assertIn("remote_read_file", visible)
        self.assertIn("remote_write_file", visible)
        self.assertIn("remote_shell", visible)
        self.assertIn("web_search", visible)

    def test_remote_selection_hard_blocks_pc_file_tools(self):
        chat_id = "remote-target-test"
        bridge._client_context_by_chat[chat_id] = {"execution_target": "mac-test"}
        bridge._set_current_chat(chat_id)
        try:
            with mock.patch.object(bridge, "_record_action_audit"):
                result = bridge.invoke_tool("write_file", {
                    "path": "C:/workspace/wrong-target.txt", "content": "nope",
                })
        finally:
            bridge._client_context_by_chat.pop(chat_id, None)
            bridge._set_current_chat("")

        self.assertTrue(result["wrong_execution_target"])
        self.assertTrue(result["not_executed"])
        self.assertEqual(result["suggested_tool"], "remote_write_file")


if __name__ == "__main__":
    unittest.main()
