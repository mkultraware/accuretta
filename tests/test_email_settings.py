import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source

ROOT = Path(bridge.__file__).resolve().parent
APP = frontend_source()


class EmailSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tempdir.name) / "email_config.json"
        self._patcher = bridge_patch("EMAIL_CONFIG_FILE", self.cfg)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self.tempdir.cleanup()

    def saved(self):
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def test_empty_account_reads_as_not_set_up(self):
        out = bridge.email_settings()
        self.assertFalse(out["configured"])
        self.assertFalse(out["password_set"])
        self.assertEqual((out["smtp_port"], out["imap_port"]), (465, 993))

    def test_password_goes_in_and_never_comes_out(self):
        out = bridge.update_email_settings({
            "from": "alex@northwind.example", "smtp_host": "smtp.northwind.example", "password": "s3cret-value"})
        self.assertTrue(out["configured"])
        self.assertTrue(out["password_set"])
        self.assertNotIn("password", out)
        self.assertNotIn("s3cret-value", json.dumps(out))
        self.assertEqual(self.saved()["password"], "s3cret-value")

    def test_empty_password_keeps_the_saved_one(self):
        bridge.update_email_settings({"password": "first"})
        bridge.update_email_settings({"password": "", "from_name": "Alex"})
        self.assertEqual(self.saved()["password"], "first")
        self.assertEqual(self.saved()["from_name"], "Alex")

    def test_keys_settings_does_not_know_are_kept(self):
        self.cfg.write_text(json.dumps({"username": "login-name", "save_sent": False}), encoding="utf-8")
        bridge.update_email_settings({"from_name": "Alex"})
        self.assertEqual(self.saved()["username"], "login-name")
        self.assertIs(self.saved()["save_sent"], False)

    def test_port_decides_the_encryption_mode(self):
        bridge.update_email_settings({"smtp_port": 587})
        self.assertIs(self.saved()["use_ssl"], False)
        bridge.update_email_settings({"smtp_port": 465})
        self.assertIs(self.saved()["use_ssl"], True)

    def test_bad_values_are_refused(self):
        for bad in ({"from": "not-an-address"},
                    {"from_name": "A\nBcc: x@y.z"},          # no smuggling a header in
                    {"smtp_host": "bad host!"},
                    {"imap_port": 99999},
                    {"identities": ["ok@example.com", "nope"]},
                    {"contacts": {"": "a@b.co"}},
                    {"contacts": {"Sam": "not-an-address"}},
                    {"password": "line\nbreak"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                bridge.update_email_settings(bad)
        self.assertFalse(self.cfg.exists())

    def test_contacts_and_identities_round_trip(self):
        out = bridge.update_email_settings({
            "from": "alex@northwind.example",
            "identities": ["info@northwind.example"],
            "contacts": {"Sam Rivera": "sam@contoso.example"}})
        self.assertEqual(out["identities"], ["info@northwind.example"])
        self.assertEqual(out["contacts"], {"Sam Rivera": "sam@contoso.example"})

    def test_test_connection_asks_for_the_missing_pieces_first(self):
        out = bridge.email_test_connection()
        self.assertFalse(out["ok"])
        self.assertIn("first", out["message"])

    def test_file_tools_refuse_the_account_file(self):
        self.assertTrue(bridge.is_data_path(str(self.cfg)))
        self.assertTrue(bridge.is_data_path(str(self.cfg), write=True))
        self.assertFalse(bridge.is_data_path(str(bridge.EMAIL_PLUGIN_DIR / "email_send.py")))


class EmailSkillTests(unittest.TestCase):
    def setUp(self):
        bridge._SKILLS_CACHE.clear()

    def tearDown(self):
        bridge._SKILLS_CACHE.clear()

    def test_skill_is_listed_and_gets_its_folder(self):
        entry = bridge._scan_skills().get("send-email")
        if entry is None:
            self.skipTest("email_plugin is not installed")
        body = bridge._read_skill_body(entry)
        self.assertNotIn("{{skill_dir}}", body)
        self.assertIn(bridge.EMAIL_PLUGIN_DIR.resolve().as_posix() + "/email_send.py", body)

    def test_shipped_files_hold_no_account(self):
        shipped = [p for p in bridge.EMAIL_PLUGIN_DIR.glob("*") if p.is_file() and p.name != "email_config.json"]
        if not shipped:
            self.skipTest("email_plugin is not installed")
        example = json.loads((bridge.EMAIL_PLUGIN_DIR / "email_config.example.json").read_text(encoding="utf-8"))
        self.assertEqual(example["password"], "")
        for path in shipped:
            with self.subTest(file=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"[A-Za-z]:[\\/]Users[\\/]")


class EmailUiTests(unittest.TestCase):
    def test_settings_has_an_email_page(self):
        self.assertIn('{ id: "email", label: "Email", icon: "ph-envelope-simple", sections: ["email"] }', APP)
        self.assertIn("email: () => loadEmailSettings(),", APP)

    def test_email_fence_renders_as_a_card_not_code(self):
        self.assertIn('if (displayLang === "email") {', APP)
        self.assertIn('if (lang === "email") return true;', APP)


if __name__ == "__main__":
    unittest.main()
