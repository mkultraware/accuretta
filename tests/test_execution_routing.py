import unittest
from unittest import mock

import bridge
from _patching import bridge_patch


class ExecutionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.chat_id = "execution-routing-test"
        self.previous_user_text = bridge.STATE.last_user_text
        bridge.STATE.last_user_text = ""
        bridge._set_current_chat(self.chat_id)
        bridge._reset_tool_call_history(self.chat_id)
        bridge._unlocked_bundles_by_chat[self.chat_id] = {"remote"}

    def tearDown(self):
        bridge._client_context_by_chat.pop(self.chat_id, None)
        bridge._unlocked_bundles_by_chat.pop(self.chat_id, None)
        bridge.STATE.last_user_text = self.previous_user_text
        bridge._set_current_chat("")

    def test_switch_to_pc_refreshes_to_pc_only_surface(self):
        bridge._client_context_by_chat[self.chat_id] = {"execution_target": "mac-test"}
        bridge._client_context_by_chat[self.chat_id]["_user_text"] = "I meant on the PC, not the MacBook."

        result = bridge.tool_switch_execution_target({"target": "host"})

        self.assertTrue(result["ok"])
        self.assertTrue(result["execution_target_changed"])
        self.assertEqual(result["execution_target"], "host")
        self.assertEqual(
            bridge._client_context_by_chat[self.chat_id]["execution_target"],
            "host",
        )
        with bridge_patch("_excluded_tools", return_value=set()), \
                bridge_patch("_base_core_tool_names", return_value={
                    "read_file", "find_files", "switch_execution_target",
                }):
            visible = bridge._visible_tool_names(chat_id=self.chat_id)

        self.assertIn("read_file", visible)
        self.assertIn("find_files", visible)
        self.assertIn("switch_execution_target", visible)
        self.assertNotIn("remote_read_file", visible)
        self.assertNotIn("remote_list_directory", visible)

    def test_switch_to_mac_refreshes_to_remote_only_surface(self):
        bridge._client_context_by_chat[self.chat_id] = {"execution_target": "host"}
        bridge._client_context_by_chat[self.chat_id]["_user_text"] = "Check it on the MacBook."
        machine = {"id": "mac-test", "label": "Test Mac", "status": "ready", "enabled": True}

        with bridge_patch("_ready_remote_machines", return_value=[machine]):
            result = bridge.tool_switch_execution_target({"target": "remote"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["execution_target"], "mac-test")
        with bridge_patch("_excluded_tools", return_value=set()), \
                bridge_patch("_base_core_tool_names", return_value={
                    "read_file", "find_files", "switch_execution_target",
                }):
            visible = bridge._visible_tool_names(chat_id=self.chat_id)

        self.assertNotIn("read_file", visible)
        self.assertNotIn("find_files", visible)
        self.assertIn("remote_read_file", visible)
        self.assertIn("remote_list_directory", visible)
        self.assertIn("switch_execution_target", visible)

    def test_unrequested_switch_requires_user_approval(self):
        bridge._client_context_by_chat[self.chat_id] = {"execution_target": "mac-test"}
        bridge.STATE.last_user_text = "Keep looking for the folder."

        with bridge_patch("request_approval",
            return_value={"decision": "deny", "status": "denied"},
        ) as approval:
            result = bridge.tool_switch_execution_target({"target": "host"})

        approval.assert_called_once()
        self.assertTrue(result["not_executed"])
        self.assertEqual(result["execution_target"], "mac-test")
        self.assertEqual(
            bridge._client_context_by_chat[self.chat_id]["execution_target"],
            "mac-test",
        )

    def test_capability_discovery_does_not_advertise_hidden_pc_tools(self):
        bridge._client_context_by_chat[self.chat_id] = {"execution_target": "mac-test"}

        with bridge_patch("_excluded_tools", return_value=set()), \
                bridge_patch("_base_core_tool_names", return_value={
                    "read_file", "find_files", "switch_execution_target",
                }):
            result = bridge.tool_list_more_tools({})

        self.assertNotIn("read_file", result["core"])
        self.assertNotIn("find_files", result["core"])
        self.assertIn("switch_execution_target", result["core"])
        self.assertNotIn("workspace-extra", result["bundles"])
        self.assertIn("remote", result["bundles"])

    def test_hard_boundary_blocks_both_wrong_device_directions(self):
        with bridge_patch("_record_action_audit"):
            bridge._client_context_by_chat[self.chat_id] = {"execution_target": "mac-test"}
            pc_result = bridge.invoke_tool("write_file", {
                "path": "C:/workspace/wrong-target.txt",
                "content": "nope",
            })
            bridge._client_context_by_chat[self.chat_id] = {"execution_target": "host"}
            mac_result = bridge.invoke_tool("remote_list_directory", {"path": "~/Desktop"})

        self.assertTrue(pc_result["wrong_execution_target"])
        self.assertTrue(pc_result["not_executed"])
        self.assertTrue(pc_result["loop_breaker"])
        self.assertTrue(mac_result["wrong_execution_target"])
        self.assertTrue(mac_result["not_executed"])
        self.assertTrue(mac_result["loop_breaker"])

    def test_varied_directory_walk_is_bounded_and_resets_on_new_strategy(self):
        bridge._client_context_by_chat[self.chat_id] = {"execution_target": "host"}
        fake_spec = {
            **bridge.TOOLS["list_directory"],
            "fn": lambda args: {"ok": True, "path": args["path"], "entries": []},
        }

        with mock.patch.dict(bridge.TOOLS, {"list_directory": fake_spec}), \
                bridge_patch("_record_action_audit"):
            results = [
                bridge.invoke_tool("list_directory", {"path": f"C:/workspace/{index}"})
                for index in range(bridge.DIRECTORY_WALK_LIMIT + 1)
            ]
            bridge.invoke_tool("switch_execution_target", {"target": "host"})
            after_reset = bridge.invoke_tool("list_directory", {"path": "C:/workspace/reset"})

        self.assertTrue(all(result.get("ok") for result in results[:-1]))
        self.assertTrue(results[-1]["loop_breaker"])
        self.assertEqual(results[-1]["tool_family"], "directory_walk")
        self.assertTrue(after_reset["ok"])


if __name__ == "__main__":
    unittest.main()
