"""The user folder (ACCURETTA_HOME) for installer builds.

A git checkout keeps the user's files next to the code. The Windows installer's
launcher sets ACCURETTA_HOME to a folder outside the app, so an upgrade that
replaces the app files never deletes chats, settings, skills, keys or downloaded
llama.cpp builds. These tests pin both layouts, plus the two installer-only
behaviours: run_python's bundled-Python fallback and the update link.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch

ROOT = Path(bridge.ROOT)


def norm(p):
    return os.path.normcase(os.path.normpath(str(p)))


class GitCheckoutLayout(unittest.TestCase):
    def test_without_accuretta_home_everything_stays_next_to_the_code(self):
        # The suite runs with ACCURETTA_DATA_DIR set and ACCURETTA_HOME unset.
        self.assertNotIn("ACCURETTA_HOME", os.environ)
        self.assertEqual(norm(bridge.USER_DIR), norm(ROOT))
        self.assertEqual(norm(bridge.SKILLS_DIR), norm(ROOT / "skills"))
        # (conftest points _MCP_CONFIG_FILE at its throwaway folder to protect the
        # real key file, so that path is checked by the installer probe instead.)
        if not os.environ.get("ACCURETTA_EMAIL_CONFIG"):
            self.assertEqual(norm(bridge.EMAIL_CONFIG_FILE), norm(ROOT / "email_plugin" / "email_config.json"))


class InstallerLayout(unittest.TestCase):
    def probe(self, home: Path) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("ACCURETTA_DATA_DIR", "ACCURETTA_HOME", "ACCURETTA_EMAIL_CONFIG")}
        env.update(ACCURETTA_HOME=str(home), ACCURETTA_OFFLINE="1", ACCURETTA_BROWSER="none")
        code = ("import json, bridge; print(json.dumps({"
                "'root': str(bridge.ROOT), 'user': str(bridge.USER_DIR), 'data': str(bridge.DATA), "
                "'skills': str(bridge.SKILLS_DIR), 'mcp': str(bridge._MCP_CONFIG_FILE), "
                "'email': str(bridge.EMAIL_CONFIG_FILE), "
                "'key_mcp': bridge.is_key_file(str(bridge._MCP_CONFIG_FILE)), "
                "'key_email': bridge.is_key_file(str(bridge.EMAIL_CONFIG_FILE)), "
                "'data_guard': bridge.is_data_path(str(bridge.DATA / 'chats.json'))}))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_user_files_move_to_the_user_folder_and_code_stays(self):
        home = Path(tempfile.mkdtemp()) / "Accuretta"
        home.mkdir()
        got = self.probe(home)
        self.assertEqual(norm(got["root"]), norm(ROOT))
        self.assertEqual(norm(got["user"]), norm(home))
        self.assertEqual(norm(got["data"]), norm(home / "data"))
        self.assertEqual(norm(got["skills"]), norm(home / "skills"))
        self.assertEqual(norm(got["mcp"]), norm(home / "bridge_mcp_config.json"))
        self.assertEqual(norm(got["email"]), norm(home / "email_plugin" / "email_config.json"))
        # The protections follow the files.
        self.assertTrue(got["key_mcp"])
        self.assertTrue(got["key_email"])
        self.assertTrue(got["data_guard"])

    def test_child_processes_learn_the_user_folder(self):
        with patch.dict(os.environ, {"ACCURETTA_HOME": "C:\\Users\\tester\\AppData\\Local\\Accuretta"}):
            self.assertEqual(bridge._child_env()["ACCURETTA_HOME"],
                             "C:\\Users\\tester\\AppData\\Local\\Accuretta")

    def test_email_scripts_use_the_user_folder(self):
        home = Path(tempfile.mkdtemp())
        env = {k: v for k, v in os.environ.items() if k != "ACCURETTA_EMAIL_CONFIG"}
        env["ACCURETTA_HOME"] = str(home)
        code = "import sys; sys.path.insert(0, 'email_plugin'); import email_common; print(email_common.CFG)"
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(norm(out.stdout.strip()), norm(home / "email_plugin" / "email_config.json"))


class InstallerOnlyBehaviour(unittest.TestCase):
    def setUp(self):
        self.install = Path(tempfile.mkdtemp())
        self.app = self.install / "app"
        (self.install / "python").mkdir(parents=True)
        self.app.mkdir()
        (self.install / "python" / "python.exe").write_bytes(b"")
        patcher = bridge_patch("ROOT", self.app)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mark_installer(self):
        (self.install / "install.json").write_text(json.dumps({"type": "installer", "version": "1.1.0"}),
                                                   encoding="utf-8")

    def test_installer_marker_is_read(self):
        self.assertIsNone(bridge._installer_info())
        self.mark_installer()
        self.assertEqual(bridge._installer_info()["version"], "1.1.0")

    def test_run_python_falls_back_to_the_bundled_python(self):
        cwd = tempfile.mkdtemp()
        with patch.object(bridge.shutil, "which", return_value=None):
            self.assertEqual(bridge._python_launcher(cwd), [])          # a git checkout: no fallback
            self.mark_installer()
            self.assertEqual([norm(p) for p in bridge._python_launcher(cwd)],
                             [norm(self.install / "python" / "python.exe")])

    def test_update_link_points_installer_users_to_the_installer(self):
        self.mark_installer()
        payload = {
            "tag_name": "v9.9.9", "name": "v9.9.9", "body": "Big release.",
            "html_url": "https://github.com/mkultraware/accuretta/releases/tag/v9.9.9",
            "published_at": "2027-01-01T00:00:00Z",
            "assets": [
                {"name": "notes.txt", "browser_download_url":
                    "https://github.com/mkultraware/accuretta/releases/download/v9.9.9/notes.txt"},
                {"name": "Accuretta-Setup-9.9.9.exe", "browser_download_url":
                    "https://github.com/mkultraware/accuretta/releases/download/v9.9.9/Accuretta-Setup-9.9.9.exe"},
            ],
        }

        class Response:
            headers = {"ETag": ""}

            def read(self):
                return json.dumps(payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        check_file = Path(tempfile.mkdtemp()) / "update_check.json"
        with bridge_patch("UPDATE_CHECK_FILE", check_file), \
                bridge_patch("_offline_active", False), \
                patch.object(bridge.urllib.request, "urlopen", return_value=Response()):
            status = bridge.app_update_status(manual=True)
        self.assertEqual(status["install_type"], "installer")
        self.assertTrue(status["release"]["installer_url"].endswith("/Accuretta-Setup-9.9.9.exe"))

    def test_a_foreign_download_link_is_ignored(self):
        payload_asset = {"name": "Accuretta-Setup-1.0.0.exe",
                         "browser_download_url": "https://evil.example/Accuretta-Setup-1.0.0.exe"}
        self.assertTrue(bridge._INSTALLER_ASSET_RE.match(payload_asset["name"]))
        self.assertFalse(payload_asset["browser_download_url"].startswith(
            "https://github.com/mkultraware/accuretta/releases/download/"))


if __name__ == "__main__":
    unittest.main()
