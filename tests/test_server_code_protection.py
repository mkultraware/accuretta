"""The file and git tools refuse to change Accuretta's own running code.

bridge.py used to hold the whole server, so refusing bridge.py was enough. Most
of the server now lives in the accuretta/ folder next to it; those files get the
same refusal. The tool tests point the guard at a throwaway folder, so a broken
guard can only ever touch a scratch file, never the real source.
"""
import os
import tempfile
import unittest
from pathlib import Path

import bridge
from _patching import bridge_patch


def _deny(**_kw):
    return {"decision": "deny", "status": "denied"}


class WhatCountsAsServerCode(unittest.TestCase):
    def test_bridge_file_is_the_running_bridge(self):
        self.assertEqual(bridge._BRIDGE_FILE_ABS, str(Path(bridge.__file__).resolve()).lower())

    def test_bridge_and_package_files_count(self):
        root = Path(bridge.ROOT)
        for p in (root / "bridge.py", root / "accuretta", root / "accuretta" / "__init__.py",
                  root / "accuretta" / "core" / "paths.py", root / "accuretta" / "security" / "self_protect.py"):
            self.assertTrue(bridge.is_bridge_self_path(str(p)), p)

    def test_other_files_do_not(self):
        root = Path(bridge.ROOT)
        for p in (root / "web" / "js" / "app.js", root / "README.md", root / "tests" / "check.py",
                  root / "accuretta_extra.py", Path(tempfile.gettempdir()) / "accuretta" / "x.py", ""):
            self.assertFalse(bridge.is_bridge_self_path(str(p)), p)


class FileToolsRefuse(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pkg = Path(tmp.name) / "accuretta"
        (pkg / "core").mkdir(parents=True)
        self.module = pkg / "core" / "thing.py"
        self.module.write_text("VALUE = 'a'\n", encoding="utf-8")
        for name, value in (("_SERVER_PACKAGE_ABS", str(pkg.resolve()).lower()),
                            ("is_in_workspace", lambda *a, **k: True)):
            p = bridge_patch(name, value)
            p.start()
            self.addCleanup(p.stop)
        p = bridge_patch("request_approval", side_effect=_deny)
        p.start()
        self.addCleanup(p.stop)

    def _unchanged(self):
        self.assertEqual(self.module.read_text(encoding="utf-8"), "VALUE = 'a'\n")

    def test_write(self):
        out = bridge.tool_write_file({"path": str(self.module), "content": "VALUE = 'b'\n"})
        self.assertIn("accuretta/", out.get("error", ""))
        self._unchanged()

    def test_edit(self):
        out = bridge.tool_edit_file({"path": str(self.module), "edits": [{"old_text": "'a'", "new_text": "'b'"}]})
        self.assertIn("accuretta/", out.get("error", ""))
        self._unchanged()

    def test_delete(self):
        out = bridge.tool_delete_file({"path": str(self.module)})
        self.assertIn("accuretta/", out.get("error", ""))
        self._unchanged()


class GitRefusesDiscardingPackageEdits(unittest.TestCase):
    def setUp(self):
        self.root = str(bridge.ROOT)
        self.asked = []
        p = bridge_patch("request_approval", side_effect=lambda **kw: self.asked.append(kw) or _deny())
        p.start()
        self.addCleanup(p.stop)

    def test_restore_and_checkout_of_package_paths(self):
        for argv in (["restore", "accuretta/core/paths.py"], ["checkout", "--", "accuretta"],
                     ["restore", "--staged", "--worktree", os.path.join("accuretta", "agent")]):
            out = bridge._git_approve("t", self.root, argv)
            self.assertIn("accuretta/", (out or {}).get("error", ""), argv)
        self.assertEqual(self.asked, [], "a refused command must not reach the approval prompt")

    def test_other_paths_still_ask(self):
        out = bridge._git_approve("t", self.root, ["restore", "README.md"])
        self.assertIn("denied", out["error"])
        self.assertEqual(len(self.asked), 1)

    def test_other_repos_are_not_affected(self):
        with tempfile.TemporaryDirectory() as other:
            out = bridge._git_approve("t", other, ["restore", "accuretta/core/paths.py"])
        self.assertIn("denied", out["error"])


if __name__ == "__main__":
    unittest.main()
