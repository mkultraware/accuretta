import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from agent_reliability import feedback


class CssSyntaxTests(unittest.TestCase):
    def check_file(self, content, suffix=".css"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ("style" + suffix)
            path.write_text(content, encoding="utf-8")
            with bridge_patch("is_in_workspace", return_value=True):
                result = bridge.tool_check_syntax({"path": str(path)})
            return result, str(path)

    def test_modern_css_passes_with_revision_proof(self):
        result, _ = self.check_file('''@layer base {
  @media (width >= 600px) {
    .card:has(> svg) {
      --gap: 4px;
      color: color-mix(in srgb, red 20%, transparent);
      width: calc(100% - var(--gap));
      & > svg { width: 13px; }
    }
  }
}''')
        self.assertNotIn("error", result)
        self.assertEqual(result["verification"]["kind"], "syntax")

    def test_malformed_css_reports_location_without_proof(self):
        for source in ('a {\n color red;\n}', 'a { color: red;',
                       'a { color: red background: blue; }',
                       'a { content: "unterminated; }', '/* unclosed',
                       'a { width: calc(100% - 1px; }', 'a {} }'):
            with self.subTest(source=source):
                result, _ = self.check_file(source)
                self.assertIn("error", result)
                self.assertGreaterEqual(result["line"], 1)
                self.assertGreaterEqual(result["column"], 1)
                self.assertNotIn("verification", result)

    def test_unsupported_file_is_unchecked_and_has_no_proof(self):
        result, path = self.check_file("plain text", ".txt")
        self.assertEqual(result["check_status"], "unchecked")
        self.assertNotIn("error", result)
        self.assertNotIn("verification", result)
        record = bridge._task_check_record("check_syntax", {"path": path}, result)
        self.assertEqual(record["status"], "unchecked")
        summary = bridge._task_verification_summary([{"path": path, "after_hash": "new"}], [record])
        self.assertEqual(summary["status"], "unchecked")
        debt = [{"path": path, "status": "unchecked", "target": "host", "sha256": "new"}]
        self.assertEqual(bridge._update_verification_debt(debt, "check_syntax", {"path": path}, result),
                         (debt, False))
        self.assertEqual(feedback("check_syntax", result)["failure_guidance"]["category"], "check_unavailable")

    def test_skipped_check_does_not_hide_valid_or_failed_checks(self):
        skipped = bridge._task_check_record("check_syntax", {"path": "notes.txt"},
                                            bridge._syntax_unchecked("Unsupported file type"))
        files = [{"path": "style.css", "after_hash": "final"}]
        passed = bridge._task_check_record("check_syntax", {"path": "style.css"},
                                          {"verification": {"sha256": "final"}})
        failed = bridge._task_check_record("check_syntax", {"path": "style.css"},
                                          {"error": "Unclosed block"})
        self.assertEqual(bridge._task_verification_summary(files, [passed, skipped])["status"], "partial")
        self.assertEqual(bridge._task_verification_summary(files, [failed, skipped])["status"], "failed")

    def test_missing_file_and_outside_workspace_remain_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "missing.css")
            with bridge_patch("is_in_workspace", return_value=True):
                self.assertIn("error", bridge.tool_check_syntax({"path": path}))
            with bridge_patch("is_in_workspace", return_value=False):
                self.assertIn("error", bridge.tool_check_syntax({"path": path}))

    def test_missing_node_does_not_claim_css_passed(self):
        with bridge_patch("_trusted_node_binary", return_value=""):
            result, _ = self.check_file("a { color: red; }")
        self.assertEqual(result["check_status"], "unchecked")
        self.assertNotIn("verification", result)

    def test_imports_and_source_maps_are_not_loaded(self):
        result, _ = self.check_file('''@import "https://example.invalid/absent.css";
a { background: url("missing.png"); }
/*# sourceMappingURL=missing.css.map */''')
        self.assertNotIn("error", result)
        self.assertIn("verification", result)
