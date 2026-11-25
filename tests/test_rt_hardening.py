import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from cvss_v31 import base_score, normalize_cwe, severity_for_score


class CvssTests(unittest.TestCase):
    def test_known_vectors(self):
        self.assertEqual(
            base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")["score"], 9.8)
        self.assertEqual(
            base_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H")["score"], 9.9)
        self.assertEqual(
            base_score("CVSS:3.1/AV:L/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:H")["score"], 7.8)

    def test_normalization_and_severity(self):
        scored = base_score("AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
        self.assertEqual(scored["vector"], "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")
        self.assertEqual(scored["severity"], "critical")
        self.assertEqual(severity_for_score(0.0), "none")
        self.assertEqual(severity_for_score(5.3), "medium")

    def test_invalid_vectors_rejected(self):
        self.assertIsNone(base_score("AV:N/AC:L"))
        self.assertIsNone(base_score("CVSS:3.1/AV:X/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"))
        self.assertIsNone(base_score("CVSS:2.0/AV:N/AC:L/Au:N/C:P/I:P/A:P"))
        self.assertIsNone(base_score(""))
        scored = base_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:N/I:N/A:N")
        self.assertEqual(scored["score"], 0.0)
        self.assertEqual(scored["severity"], "none")

    def test_temporal_metrics_ignored_for_base_score(self):
        with_temporal = base_score(
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H/E:F/RL:O/RC:C")
        self.assertEqual(with_temporal["score"], 9.8)
        self.assertNotIn("E:", with_temporal["vector"])

    def test_cwe_normalization(self):
        self.assertEqual(normalize_cwe("79"), {"id": "CWE-79", "name": "Cross-site Scripting (XSS)"})
        self.assertEqual(normalize_cwe("CWE-89")["name"], "SQL Injection")
        unknown = normalize_cwe("CWE-99999")
        self.assertEqual(unknown["id"], "CWE-99999")
        self.assertIsNone(unknown["name"])
        self.assertIsNone(normalize_cwe("not a cwe"))
        self.assertIsNone(normalize_cwe(""))


class ScopeTests(unittest.TestCase):
    def test_structured_scope_wins_over_prose(self):
        chat = {"mission": {
            "authorized": True, "target": "example.com", "facts": [],
            "scope": "in: legacy.example.com out: legacy-nope.example.com",
            "scope_in": ["api.example.com", "10.0.0.0/24"],
            "scope_out": ["blocked.example.com"],
        }}
        self.assertEqual(bridge._rt_scope_in_tokens(chat), ["api.example.com", "10.0.0.0/24"])
        self.assertEqual(bridge._rt_scope_out_tokens(chat), ["blocked.example.com"])

    def test_legacy_prose_scope_still_parses(self):
        chat = {"mission": {
            "authorized": True, "target": "example.com", "facts": [],
            "scope": "in: a.example.com, b.example.com out: nope.example.com",
        }}
        self.assertEqual(bridge._rt_scope_in_tokens(chat), ["a.example.com", "b.example.com"])
        self.assertEqual(bridge._rt_scope_out_tokens(chat), ["nope.example.com"])

    def test_mission_panel_stores_structured_policy(self):
        chat = {}
        panel = {
            "authorized": True, "target": "example.com",
            "scope": "in: example.com", "objective": "test", "engagement": "pentest",
            "scope_in": ["a.example.com", "10.0.0.0/24"],
            "scope_out": "b.example.com, c.example.com",
            "required_headers": {"X-Hunt": "me"},
            "rate_limit_rpm": 30, "request_cap": 500,
            "automated_allowed": False, "allow_destructive": False,
        }
        self.assertTrue(bridge._rt_mission_apply_panel(chat, panel))
        mission = chat["mission"]
        self.assertEqual(mission["scope_in"], ["a.example.com", "10.0.0.0/24"])
        self.assertEqual(mission["scope_out"], ["b.example.com", "c.example.com"])
        self.assertEqual(mission["required_headers"], {"X-Hunt": "me"})
        self.assertEqual(mission["rate_limit_rpm"], 30)
        self.assertEqual(mission["request_cap"], 500)
        self.assertFalse(mission["automated_allowed"])
        self.assertFalse(mission["allow_destructive"])


class RequiredHeaderTests(unittest.TestCase):
    def test_header_cleaning_is_bounded_and_safe(self):
        cleaned = bridge._rt_clean_required_headers({
            "X-Ok": "value",
            "bad name": "x",
            "X-Empty": "  ",
            "X-Unicode": "日本語",
            "X-Long": "a" * 600,
        })
        self.assertEqual(cleaned, {"X-Ok": "value"})

    def test_required_headers_override_model_headers(self):
        mission = {
            "authorized": True, "status": "active", "target": "t.test", "facts": [],
            "user_agent": "ProgramUA/1.0",
            "required_headers": {"X-HackerOne-Research": "tester"},
        }
        chat = {"mission": mission}
        with bridge_patch("_rt_context_chat", lambda: chat):
            headers = bridge._rt_apply_mission_user_agent({
                "User-Agent": "curious-agent", "X-HackerOne-Research": "spoofed",
                "Accept": "*/*",
            })
        self.assertEqual(headers["User-Agent"], "ProgramUA/1.0")
        self.assertEqual(headers["X-HackerOne-Research"], "tester")
        self.assertEqual(headers["Accept"], "*/*")


class GovernorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(bridge._RT_GOV_STATE.clear)
        # Paced requests sleep until their slot; record the slots instead.
        self.slots = []
        sleeper = bridge_patch("_rt_gov_sleep_until",
                               side_effect=lambda when: self.slots.append(when) or True)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_paced_requests_are_spaced_never_refused(self):
        mission = {"authorization_id": "pace-mission", "rate_limit_rpm": 30, "request_cap": 0}
        with bridge_patch("DATA", self.root):
            for i in range(5):
                self.assertIsNone(bridge._rt_gov_admit("h.test", "GET", f"http://h.test/{i}",
                                                       mission=mission))
        gaps = [b - a for a, b in zip(self.slots, self.slots[1:])]
        self.assertEqual(len(gaps), 4)
        for gap in gaps:
            self.assertGreaterEqual(gap, 2.0 - 1e-6)    # never faster than 30/min
            self.assertLessEqual(gap, 3.0 + 1e-6)       # a little slower at most

    def test_bug_bounty_is_paced_without_a_limit(self):
        mission = {"authorization_id": "soft-mission", "engagement": "bug_bounty"}
        with bridge_patch("DATA", self.root):
            for i in range(3):
                bridge._rt_gov_admit("h.test", "GET", f"http://h.test/{i}", mission=mission)
        self.assertEqual(len(self.slots), 3)
        self.assertGreaterEqual(self.slots[2] - self.slots[1], 1.0 - 1e-6)

    def test_browser_page_files_are_ledgered_not_counted(self):
        mission = {"authorization_id": "asset-mission", "rate_limit_rpm": 1, "request_cap": 1}
        with bridge_patch("DATA", self.root):
            for i in range(5):
                self.assertIsNone(bridge._rt_gov_admit(
                    "h.test", "GET", f"http://h.test/f{i}.woff2", mission=mission, kind="asset"))
            self.assertIsNone(bridge._rt_gov_admit("h.test", "GET", "http://h.test/",
                                                   mission=mission, wait=False))
            self.assertIn("cap", bridge._rt_gov_admit("h.test", "GET", "http://h.test/x",
                                                      mission=mission, wait=False))
            ledger = (self.root / "redteam" / "request_ledger.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(ledger.strip().splitlines()), 6)
            self.assertEqual(bridge._rt_gov_ledger_count("asset-mission"), 1)

    def test_cap_blocks_and_ledger_records(self):
        mission = {"authorization_id": "cap-mission", "request_cap": 2, "rate_limit_rpm": 0}
        with bridge_patch("DATA", self.root):
            self.assertIsNone(bridge._rt_gov_admit("h.test", "GET", "http://h.test/1", mission=mission))
            self.assertIsNone(bridge._rt_gov_admit("h.test", "GET", "http://h.test/2", mission=mission))
            blocked = bridge._rt_gov_admit("h.test", "GET", "http://h.test/3", mission=mission)
            ledger = (self.root / "redteam" / "request_ledger.jsonl").read_text(encoding="utf-8")
            count = bridge._rt_gov_ledger_count("cap-mission")
        self.assertIn("cap", blocked)
        self.assertEqual(len(ledger.strip().splitlines()), 2)
        self.assertEqual(count, 2)

    def test_unpaced_rate_window_refuses_then_recovers(self):
        # The browser's request hook cannot wait, so it keeps the window refusal.
        mission = {"authorization_id": "rate-mission", "rate_limit_rpm": 2, "request_cap": 0}
        admit = lambda n: bridge._rt_gov_admit("h.test", "GET", f"http://h.test/{n}",
                                               mission=mission, wait=False)
        with bridge_patch("DATA", self.root):
            self.assertIsNone(admit(1))
            self.assertIsNone(admit(2))
            blocked = admit(3)
            self.assertIn("rate limit", blocked)
            bridge._RT_GOV_STATE["rate-mission"]["window_start"] -= 61
            self.assertIsNone(admit(4))
            usage = bridge._rt_gov_usage(mission)
        self.assertEqual(usage["total_sent"], 3)
        self.assertEqual(usage["blocked"], 1)

    def test_preflight_refuses_over_budget_fanout(self):
        mission = {"authorization_id": "pre-mission", "rate_limit_rpm": 5, "request_cap": 10}
        with bridge_patch("DATA", self.root):
            bridge._rt_gov_admit("h.test", "GET", "http://h.test/1", mission=mission)
            bridge._rt_gov_admit("h.test", "GET", "http://h.test/2", mission=mission)
            bridge._rt_gov_admit("h.test", "GET", "http://h.test/3", mission=mission)
            self.assertIsNone(bridge._rt_gov_preflight(2, mission=mission))
            refusal = bridge._rt_gov_preflight(8, mission=mission)
        self.assertIn("cap", refusal)

    def test_preflight_allows_a_paced_batch_but_not_an_endless_one(self):
        mission = {"authorization_id": "batch-mission", "rate_limit_rpm": 30, "request_cap": 0}
        with bridge_patch("DATA", self.root):
            self.assertIsNone(bridge._rt_gov_preflight(60, mission=mission))   # ~3 minutes
            refusal = bridge._rt_gov_preflight(1000, mission=mission)
        self.assertIn("minutes", refusal)

    def test_no_mission_is_a_noop_and_unlimited_mission_ledgers(self):
        unlimited = {"authorization_id": "unlimited", "rate_limit_rpm": 0, "request_cap": 0}
        with bridge_patch("DATA", self.root), \
             bridge_patch("_rt_context_chat", lambda: None):
            self.assertIsNone(bridge._rt_gov_admit("h.test", "GET", "http://h.test/1", mission=None))
            self.assertFalse((self.root / "redteam").exists())
            self.assertIsNone(bridge._rt_gov_admit(
                "h.test", "GET", "http://h.test/2", mission=unlimited))
            self.assertEqual(bridge._rt_gov_usage(unlimited), {})
            ledger = (self.root / "redteam" / "request_ledger.jsonl").read_text(encoding="utf-8")
            self.assertEqual(len(ledger.strip().splitlines()), 1)
            self.assertEqual(bridge._rt_gov_ledger_count("unlimited"), 1)


class GuardTests(unittest.TestCase):
    def _settings(self):
        return {"red_team_enabled": True}

    def _block(self, name, args, mission):
        chat = {"mission": mission}
        with bridge_patch("get_settings", self._settings), \
             bridge_patch("_rt_context_chat", lambda: chat):
            return bridge._rt_scope_block(name, args)

    def _mission(self, **extra):
        return {
            "authorized": True, "status": "active", "target": "t.test", "facts": [],
            "engagement": "pentest", "automated_allowed": True, "allow_destructive": False,
            **extra,
        }

    def test_automated_tools_refused_when_disabled(self):
        reason = self._block("fuzz", {"url": "http://t.test/x?q=1"},
                             self._mission(automated_allowed=False))
        self.assertIn("automated", reason)

    def test_destructive_delete_refused_by_default(self):
        reason = self._block("http_request", {"url": "http://t.test/account/1", "method": "DELETE"},
                             self._mission())
        self.assertIn("DELETE", reason)

    def test_destructive_path_refused(self):
        reason = self._block("http_request", {"url": "http://t.test/api/delete/user", "method": "POST"},
                             self._mission())
        self.assertIn("destructive", reason)

    def test_safe_request_passes(self):
        self.assertIsNone(self._block("http_request", {"url": "http://t.test/api/user/1", "method": "GET"},
                                      self._mission()))

    def test_destructive_allowed_when_enabled(self):
        self.assertIsNone(self._block("http_request", {"url": "http://t.test/account/1", "method": "DELETE"},
                                      self._mission(allow_destructive=True)))


if __name__ == "__main__":
    unittest.main()
