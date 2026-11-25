import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


class ActiveSkillInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.skills_dir = Path(self.tempdir.name) / "skills"
        self.skills_dir.mkdir()
        (self.skills_dir / "demo-skill.md").write_text(
            "---\nname: demo-skill\ndescription: demo\nbudget: 100\n---\n"
            "Step 1: do the thing.\nStep 2: verify the thing.\n",
            encoding="utf-8",
        )
        self.chat = {"active_skill": "demo-skill", "active_skill_budget": 100}
        self._patcher = bridge_patch("SKILLS_DIR", self.skills_dir)
        self._patcher.start()
        bridge._SKILLS_CACHE.clear()

    def tearDown(self):
        self._patcher.stop()
        bridge._SKILLS_CACHE.clear()
        self.tempdir.cleanup()

    def test_section_includes_name_and_body(self):
        section = bridge._active_skill_section(self.chat)
        self.assertIn("ACTIVE SKILL: demo-skill", section)
        self.assertIn("Step 1: do the thing.", section)
        self.assertTrue(section.startswith("=== "))

    def test_no_active_skill_returns_empty(self):
        self.assertEqual(bridge._active_skill_section({}), "")
        self.assertEqual(bridge._active_skill_section(None), "")
        self.assertEqual(bridge._active_skill_section({"active_skill": ""}), "")

    def test_unknown_skill_returns_empty(self):
        self.assertEqual(bridge._active_skill_section({"active_skill": "nope"}), "")

    def test_deleted_skill_file_returns_empty(self):
        (self.skills_dir / "demo-skill.md").unlink()
        bridge._SKILLS_CACHE.clear()
        self.assertEqual(bridge._active_skill_section(self.chat), "")

    def test_body_cap_enforced(self):
        (self.skills_dir / "big-skill.md").write_text(
            "---\nname: big-skill\ndescription: big\n---\n" + ("x" * 50000),
            encoding="utf-8",
        )
        bridge._SKILLS_CACHE.clear()
        with bridge_patch("SKILL_BODY_CAP_CHARS", 100):
            section = bridge._active_skill_section({"active_skill": "big-skill"})
        self.assertIn("ACTIVE SKILL: big-skill", section)
        self.assertNotIn("x" * 50000, section)          # full body did NOT flow through
        self.assertIn("x" * 100, section)                # truncated to the cap


if __name__ == "__main__":
    unittest.main()
