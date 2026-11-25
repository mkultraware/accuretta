import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


class SkillAssetBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.install_root = base / "install"
        self.user_root = base / "user"
        src = self.install_root / "skill_assets" / "my-template"
        (src / "engine" / "node_modules").mkdir(parents=True)
        (src / "engine" / "package.json").write_text("{}", encoding="utf-8")
        (src / "engine" / "node_modules" / "puppeteer-core").write_text("heavy", encoding="utf-8")
        (src / "out").mkdir()
        (src / "out" / "sheet.jpg").write_text("artifact", encoding="utf-8")
        self._patches = [
            bridge_patch("ROOT", self.install_root),
            bridge_patch("get_workspace", lambda: {"folders": [str(self.user_root / "ws")]}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tempdir.cleanup()

    def test_provisions_template_into_workspace(self):
        bridge._bootstrap_skill_assets()
        dst = self.user_root / "ws" / "my-template"
        self.assertTrue((dst / "engine" / "package.json").is_file())
        self.assertFalse((dst / "engine" / "node_modules").exists())
        self.assertFalse((dst / "out").exists())

    def test_copy_if_missing_never_clobbers(self):
        bridge._bootstrap_skill_assets()
        marker = self.user_root / "ws" / "my-template" / "user-edit.txt"
        marker.write_text("mine", encoding="utf-8")
        bridge._bootstrap_skill_assets()  # second run
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "mine")

    def test_no_skill_assets_dir_is_noop(self):
        before = sorted(p.name for p in self.user_root.glob("*"))
        shutil.rmtree(self.install_root / "skill_assets")
        bridge._bootstrap_skill_assets()
        self.assertEqual(sorted(p.name for p in self.user_root.glob("*")), before)


if __name__ == "__main__":
    unittest.main()
