"""The composer's folder chip follows the agent: a chat with no chosen project
takes the folder the agent changes things in (writes, edits, git, a clone).
Reading never moves it, and a folder the user picked is never replaced."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


class ProjectFollowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Workspace"
        self.repo = self.root / "Clipboard-Defanger"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src").mkdir()
        (self.root / "plain-site" / "css").mkdir(parents=True)
        self.other = self.root / "other-repo"
        (self.other / ".git").mkdir(parents=True)
        self.chats_file = Path(self.tmp.name) / "chats.json"
        self.chats_file.write_text(json.dumps({"chats": {"c1": {"id": "c1", "messages": []}}, "order": ["c1"]}))
        self.events = []
        root = str(self.root)
        self.patches = [
            bridge_patch("CHATS_FILE", self.chats_file),
            bridge_patch("get_workspace", return_value={"folders": [root]}),
            bridge_patch("_default_command_cwd", return_value=root),
            bridge_patch("_emit_chat_event", side_effect=self.events.append),
        ]
        for p in self.patches:
            p.start()
        self.token = bridge._current_chat_id.set("c1")

    def tearDown(self):
        bridge._current_chat_id.reset(self.token)
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def project(self):
        return bridge.get_chats()["chats"]["c1"].get("project_workspace")

    def same(self, a, b):
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))

    def test_a_clone_command_names_the_new_folder(self):
        clone = f'git clone https://github.com/me/Clipboard-Defanger "{self.repo}"'
        bridge._project_follow("run_powershell", {"command": clone}, {"ok": True})
        p = self.project()
        self.assertTrue(self.same(p["path"], self.repo))
        self.assertEqual((p["name"], p["auto"]), ("Clipboard-Defanger", True))
        self.assertEqual(self.events[-1]["type"], "project_follow")

    def test_clone_forms_and_git_dash_c(self):
        cases = {
            "git clone --depth 1 -b main https://x.test/me/Clipboard-Defanger.git": self.repo,
            "git clone git@github.com:me/Clipboard-Defanger.git": self.repo,
            f'git -C "{self.other}" push origin master': self.other,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertTrue(self.same(bridge._project_follow_target("run_powershell", {"command": command}), expected))
        # reading through git or the shell is not working there
        for command in (f'git -C "{self.other}" status', "Get-ChildItem", f'git -C "{self.other}" log -5'):
            with self.subTest(command=command):
                self.assertIsNone(bridge._project_follow_target("run_powershell", {"command": command}))

    def test_files_map_to_their_repo_or_top_folder(self):
        target = bridge._project_follow_target
        self.assertTrue(self.same(target("edit_file", {"path": str(self.repo / "src" / "x.py")}), self.repo))
        self.assertTrue(self.same(target("write_file", {"path": str(self.root / "plain-site" / "css" / "a.css")}), self.root / "plain-site"))
        self.assertIsNone(target("write_file", {"path": str(self.root / "notes.txt")}))
        self.assertIsNone(target("write_file", {"path": str(Path(self.tmp.name) / "elsewhere.txt")}))
        self.assertIsNone(target("read_file", {"path": str(self.repo / "src" / "x.py")}))

    def test_a_users_pick_stays_and_an_auto_one_moves(self):
        bridge._project_follow("edit_file", {"path": str(self.repo / "src" / "x.py")}, {"ok": True})
        bridge._project_follow("git_commit", {"path": str(self.other)}, {"ok": True})
        self.assertTrue(self.same(self.project()["path"], self.other))
        chats = bridge.get_chats()
        chats["chats"]["c1"]["project_workspace"] = {"name": "Clipboard-Defanger", "path": str(self.repo)}
        bridge.save_json(bridge.CHATS_FILE, chats)
        bridge._project_follow("git_commit", {"path": str(self.other)}, {"ok": True})
        self.assertTrue(self.same(self.project()["path"], self.repo))

    def test_failed_or_skipped_calls_do_not_move_it(self):
        for result in ({"error": "denied"}, {"not_executed": True}, "text"):
            bridge._project_follow("write_file", {"path": str(self.repo / "a.txt")}, result)
        self.assertIsNone(self.project())

    def test_the_turns_own_save_keeps_a_project_picked_up_mid_turn(self):
        turn = bridge.get_chats()               # the turn loads the chat first
        bridge._project_follow("write_file", {"path": str(self.repo / "a.txt")}, {"ok": True})
        turn["chats"]["c1"]["messages"].append({"role": "assistant", "content": "done"})
        bridge.save_json(bridge.CHATS_FILE, turn)
        chat = bridge.get_chats()["chats"]["c1"]
        self.assertEqual(len(chat["messages"]), 1)
        self.assertTrue(self.same(chat["project_workspace"]["path"], self.repo))


if __name__ == "__main__":
    unittest.main()
