from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch


class CodeCheckResultTests(unittest.TestCase):
    def test_saved_edit_with_findings_retains_metadata_and_verification_debt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "example.py"
            path.write_text("value = missing_name\n", encoding="utf-8")
            args = {"path": str(path), "edits": [{"old_text": "value =", "new_text": "result ="}]}
            with bridge_patch("is_in_workspace", return_value=True), \
                    bridge_patch("request_approval", return_value={"decision": "approve"}), \
                    bridge_patch("_run_linter", return_value="F821 undefined name 'missing_name'"), \
                    bridge_patch("_undo_snapshot"), \
                    bridge_patch("_selected_execution_target", return_value="host"):
                result = bridge.tool_edit_file(args)
                debt, changed = bridge._update_verification_debt([], "edit_file", args, result)
            self.assertEqual(path.read_text(), "result = missing_name\n")
            self.assertTrue(result["ok"])
            self.assertNotIn("error", result)
            self.assertEqual(result["edits_applied"], 1)
            self.assertEqual(result["path"], str(path))
            self.assertIn("F821", result["code_check"]["details"])
            self.assertEqual(result["code_check"]["introduced_by_edit"], "unknown")
            self.assertTrue(changed)
            self.assertEqual(debt[0]["status"], "unchecked")

    def test_missing_file_remains_a_failure(self):
        with tempfile.TemporaryDirectory() as directory, \
                bridge_patch("is_in_workspace", return_value=True):
            result = bridge.tool_edit_file({"path": str(Path(directory) / "missing.py"),
                                           "edits": [{"old_text": "x", "new_text": "y"}]})
        self.assertIn("error", result)
        self.assertNotIn("code_check", result)

    def test_clean_check_does_not_add_a_warning(self):
        result = bridge._with_code_check({"ok": True, "path": "file.py"}, "")
        self.assertNotIn("code_check", result)


if __name__ == "__main__":
    unittest.main()
