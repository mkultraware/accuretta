"""Bug Bounty playbook: reading a pasted program into the questionnaire.

What the playbook promises the user:
  * the model's reading of the program can leave a field empty or raise a
    question, but it can never widen what the run may touch: a host is only
    kept when the program itself names it;
  * app store links and patterns like www.example.* never become scope;
  * the program's required User-Agent text is added to a normal User-Agent;
  * a bug bounty run does not carry the data-extraction, password-guessing
    and network-scanning tools, and refuses them if called by name;
  * the program's rules ride the mission block every turn.
"""

import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch

PROGRAM = """
About the Scope of this Program
Our Bug Bounty program is limited to our web application at https://www.shop-example.de,
https://www.shop-example.at and https://www.shop-example.com.tr. Please be aware that
*.shop-example.pt is out-of-scope. Subdomains are out-of-scope.
Don'ts: Run automated scanning. Access other customers' accounts.
Scopes
https://www.shop-example.de Web application
https://www.shop-example.at Web application
https://www.shop-example.com.tr Web application
https://play.google.com/store/apps/details?id=com.shop.example Mobile application Android
User agent: Please append to your user-agent header the following value: ' -SHOP-BugBounty '.
""" * 2

MODEL_READING = {
    "scope_in": [
        "https://www.shop-example.de", "https://www.shop-example.at",
        "https://www.shop-example.com.tr",
        "shop-example.de",                  # bare domain: would allow every subdomain
        "www.shop-example.com",             # only a prefix of www.shop-example.com.tr
        "api.shop-example.de",              # never written in the program
        "https://www.shop-example.*",       # a pattern
        "https://play.google.com/store/apps/details?id=com.shop.example",
    ],
    "scope_out": ["*.shop-example.pt"],
    "subdomains_in_scope": False,
    "user_agent_text": "-SHOP-BugBounty",
    "user_agent_mode": "append",
    "required_headers": [{"name": "X-Research", "value": "your_username"}],
    "rate_limit_rpm": 30,
    "automated_scanning_allowed": False,
    "destructive_allowed": False,
    "account_rule": "Use your @example.ninja alias.",
    "mobile_apps_in_scope": True,
    "rules": ["Do not access other customers' data.", "XSS proof: alert() only."],
    "questions": ["Is the API host in scope?"],
}


class ProgramFieldsTest(unittest.TestCase):
    def setUp(self):
        self.fields = bridge._rt_program_fields(MODEL_READING, PROGRAM)

    def test_only_hosts_the_program_names_survive(self):
        self.assertEqual(self.fields["scope_in"],
                         ["www.shop-example.de", "www.shop-example.at", "www.shop-example.com.tr"])
        self.assertEqual(self.fields["target"], "www.shop-example.de")
        flags = " ".join(self.fields["flags"])
        self.assertIn("api.shop-example.de isn't written", flags)
        self.assertIn("shop-example.de isn't written", flags)
        self.assertIn("www.shop-example.com isn't written", flags)
        self.assertIn("pattern", flags)
        self.assertIn("mobile apps", flags)

    def test_out_of_scope_and_limits(self):
        self.assertEqual(self.fields["scope_out"], ["*.shop-example.pt"])
        self.assertEqual(self.fields["rate_limit_rpm"], 30)
        self.assertIs(self.fields["automated_allowed"], False)
        self.assertIs(self.fields["allow_destructive"], False)

    def test_user_agent_is_appended_to_a_browser_one(self):
        ua = self.fields["user_agent"]
        self.assertTrue(ua.startswith("Mozilla/5.0"))
        self.assertTrue(ua.endswith(" -SHOP-BugBounty"))

    def test_placeholder_header_is_flagged(self):
        self.assertEqual(self.fields["required_headers"], {"X-Research": "your_username"})
        self.assertTrue(any("placeholder" in f for f in self.fields["flags"]))

    def test_rules_become_notes_and_questions_become_flags(self):
        self.assertIn("- Test accounts: Use your @example.ninja alias.", self.fields["notes"])
        self.assertIn("- XSS proof: alert() only.", self.fields["notes"])
        self.assertIn("Is the API host in scope?", self.fields["flags"])

    def test_unknown_automation_is_flagged(self):
        fields = bridge._rt_program_fields({**MODEL_READING, "automated_scanning_allowed": None}, PROGRAM)
        self.assertIsNone(fields["automated_allowed"])
        self.assertTrue(any("automated scanning" in f for f in fields["flags"]))

    def test_nothing_confirmed_says_so(self):
        fields = bridge._rt_program_fields({"scope_in": ["evil.example.net"]}, PROGRAM)
        self.assertEqual(fields["scope_in"], [])
        self.assertEqual(fields["target"], "")
        self.assertIn("No in-scope web host", fields["flags"][0])


class ProgramJsonTest(unittest.TestCase):
    def test_reads_fenced_and_thinking_replies(self):
        self.assertEqual(bridge._rt_program_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(bridge._rt_program_json('<think>hmm</think>Here: {"a": 2} done'), {"a": 2})
        self.assertEqual(bridge._rt_program_json("no json here"), {})


class ReadProgramTest(unittest.TestCase):
    def test_short_text_is_refused_before_any_model_call(self):
        with bridge_patch("llama_post") as post:
            out = bridge._rt_read_program("too short")
        self.assertFalse(out["ok"])
        post.assert_not_called()

    def test_no_model_loaded(self):
        with bridge_patch("llama_ready", return_value=False):
            out = bridge._rt_read_program(PROGRAM)
        self.assertFalse(out["ok"])
        self.assertIn("Load a model", out["error"])

    def test_full_call(self):
        import json
        reply = {"choices": [{"message": {"content": json.dumps(MODEL_READING)}}]}
        with bridge_patch("llama_ready", return_value=True), \
             bridge_patch("_llama_props_ctx", return_value=32768), \
             bridge_patch("llama_post", return_value=reply) as post:
            out = bridge._rt_read_program(PROGRAM)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["scope_in"]), 3)
        sent = post.call_args[0][1]
        self.assertIn(PROGRAM.strip()[:80], sent["messages"][1]["content"])

    def test_context_too_small(self):
        with bridge_patch("llama_ready", return_value=True), \
             bridge_patch("_llama_props_ctx", return_value=1024), \
             bridge_patch("llama_post") as post:
            out = bridge._rt_read_program(PROGRAM)
        self.assertFalse(out["ok"])
        self.assertIn("too long", out["error"])
        post.assert_not_called()


class BountyEngagementTest(unittest.TestCase):
    def _chat(self):
        chat = {}
        ok = bridge._rt_mission_apply_panel(chat, {
            "authorized": True, "target": "www.shop-example.de", "engagement": "bug_bounty",
            "scope_in": ["www.shop-example.de"], "notes": "- XSS proof: alert() only.",
            "automated_allowed": False,
        })
        self.assertTrue(ok)
        return chat

    def test_engagement_and_notes_are_stored(self):
        m = self._chat()["mission"]
        self.assertEqual(m["engagement"], "bug_bounty")
        self.assertEqual(m["notes"], "- XSS proof: alert() only.")

    def test_rules_ride_the_mission_block(self):
        chat = self._chat()
        with bridge_patch("_rt_mission_get", return_value=chat["mission"]):
            text = bridge._rt_mission_render(chat)
        self.assertIn("program rules:\n- XSS proof: alert() only.", text)
        self.assertIn("outrank every other instruction", text)
        self.assertIn("Prove each one with the smallest proof", text)

    def test_extraction_tools_are_refused(self):
        chat = self._chat()
        with bridge_patch("get_settings", return_value={"red_team_enabled": True}), \
             bridge_patch("_rt_context_chat", return_value=chat), \
             bridge_patch("_rt_authorized_mission", return_value=chat["mission"]):
            for name in ("sql_injection", "recon_auth_spray", "recon_port_scan"):
                block = bridge._rt_scope_block(name, {"url": "https://www.shop-example.de/"})
                self.assertIsNotNone(block, name)
                self.assertIn("bug bounty", block)

    def test_next_step_stays_on_minimal_proof(self):
        hint = bridge._rt_next_step("recon_injection_probe", {}, True, "bug_bounty")
        self.assertIn("smallest", hint)
        self.assertNotIn("dump", hint)


class ShellCommandTest(unittest.TestCase):
    def test_code_is_not_read_as_hosts(self):
        hosts = bridge._rt_command_hosts(
            "import re, json; s = json.loads(t); re.findall(r'x', s); os.path.join(a, b); "
            "$c = get-content 'x.txt' -raw; $c.substring(0, 5); $p.length; data.items")
        self.assertEqual(hosts, set())

    def test_real_hosts_are_still_caught(self):
        hosts = bridge._rt_command_hosts(
            "curl https://evil.photography/x; iwr www.mediamarkt.de; ping 203.0.113.9; "
            "urlopen('http://a.b.example.museum/') ; nc evil.io 80; ssh root@box.internal")
        self.assertTrue({"evil.photography", "www.mediamarkt.de", "203.0.113.9",
                         "a.b.example.museum", "evil.io", "box.internal"} <= hosts)

    def test_data_folder_is_off_limits_but_evidence_is_readable(self):
        root = str(bridge.DATA)
        self.assertIsNotNone(bridge._rt_command_data_block(
            f"Set-Content -Path '{root}\\preloaded_state.json' -Value x"))
        self.assertIsNotNone(bridge._rt_command_data_block(f"type {root}\\settings.json"))
        self.assertIsNone(bridge._rt_command_data_block(
            f"Copy-Item '{root}\\recon_evidence\\a.txt' 'C:\\work\\a.html'"))
        self.assertIsNone(bridge._rt_command_data_block("Get-ChildItem C:\\work"))


class ReaderQuestionsTest(unittest.TestCase):
    def test_mobile_app_questions_are_not_repeated(self):
        reading = {**MODEL_READING, "questions": [
            "Are all official iOS and Android apps in scope?", "Is the API host in scope?"]}
        flags = bridge._rt_program_fields(reading, PROGRAM)["flags"]
        self.assertEqual(sum("mobile apps" in f or "iOS" in f for f in flags), 1)
        self.assertIn("Is the API host in scope?", flags)


if __name__ == "__main__":
    unittest.main()
