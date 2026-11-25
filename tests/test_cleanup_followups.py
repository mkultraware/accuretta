"""Regressions for the problems the bridge.py / app.js cleanup turned up."""
import base64
import re
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source

APP = frontend_source()


class RemoteBinaryReadTests(unittest.TestCase):
    def test_binary_file_comes_back_base64_encoded(self):
        # `base64.` was unbound in bridge.py (the module imports it as _b64),
        # so any non-UTF-8 read from the paired Mac raised NameError.
        raw = b"\x89PNG\r\n\x1a\n\x00\xff\xfe"
        row = {"id": "mac-1"}
        with bridge_patch("_selected_remote_machine", return_value=row), \
                bridge_patch("_normalize_remote_path", side_effect=lambda r, p: p), \
                bridge_patch("_remote_file_metadata",
                             return_value={"ok": True, "path": "/tmp/a.png", "size": len(raw),
                                           "sha256": "0" * 64}), \
                bridge_patch("_run_remote_process",
                             return_value={"ok": True, "stdout_bytes": raw}):
            out = bridge.tool_remote_read_file({"path": "/tmp/a.png"})
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(out["binary_base64"])
        self.assertEqual(base64.b64decode(out["content"]), raw)


class SoftModeRemoteCodeTests(unittest.TestCase):
    def test_code_that_is_not_on_disk_prompts(self):
        for cmd in ("cat install.sh | sh",
                    "echo payload | bash",
                    "pip install git+https://github.com/example/pkg.git",
                    "pip install https://example.com/pkg.tar.gz",
                    "npm install github:example/pkg",
                    "npm install https://example.com/pkg.tgz"):
            with self.subTest(cmd=cmd):
                self.assertTrue(bridge._soft_command_needs_approval(cmd))

    def test_ordinary_project_commands_still_run_freely(self):
        for cmd in ("pip install requests",
                    "npm install react",
                    "Get-ChildItem | Select-String foo",
                    "git log | sort",
                    "python -m pytest",
                    "sha256sum file.bin | shasum"):
            with self.subTest(cmd=cmd):
                self.assertFalse(bridge._soft_command_needs_approval(cmd))


class KillCommandButtonTests(unittest.TestCase):
    def test_kill_button_has_a_handler_that_calls_the_endpoint(self):
        # The ✕ on the live commands card rendered with no click handler.
        self.assertIn('class="revealer-card-kill"', APP)
        handler = re.search(r'closest\?\.\("\.revealer-card-kill"\)[\s\S]{0,600}?killCurrentCommand\(\)', APP)
        self.assertIsNotNone(handler, "no delegated click handler reaches killCurrentCommand()")
        fn = APP[APP.index("function killCurrentCommand()"):][:500]
        self.assertIn('"/api/kill-command"', fn)
        self.assertIn("chat_id", fn)


class DeadWelcomeFieldTests(unittest.TestCase):
    def test_retired_welcome_field_styles_are_gone(self):
        # Nothing creates .welcome-blobs any more; its styles were dead weight.
        dead = re.compile(r"\.(?:welcome-blobs|wb-blob|wb-pillar|wb-soften|wb-noise)\b|--wb-(?:a|b|c|strength)\b")
        sections = tuple(p.name for p in sorted(bridge.ROOT.glob("ui-*.js")))
        for name in ("app.css", "ui-pass.css", "workspace-shell.css", "colors_and_type.css",
                     "opal.css", "workbench.css", "appearance.js", "app.js", "index.html") + sections:
            with self.subTest(file=name):
                self.assertIsNone(dead.search((bridge.ROOT / name).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
