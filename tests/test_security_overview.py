import tempfile
import time
import unittest
from pathlib import Path

from security_overview import SecurityOverviewService


class SecurityOverviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.emitted = []
        self.summary_calls = []

        def summarize(kind, evidence):
            self.summary_calls.append((kind, evidence))
            if kind == "investigation":
                return {
                    "verdict": "unresolved",
                    "confidence": "medium",
                    "summary": "The evidence is limited.",
                    "what_is_known": ["A service was installed."],
                    "what_is_unknown": ["The install origin is unknown."],
                    "next_step": "Review the service path.",
                }
            return {
                "headline": "One change needs review",
                "situation": "A new service was recorded.",
                "confidence": "medium",
                "coverage_note": "Security events are unavailable.",
                "notable": [],
                "recommended_checks": [],
            }

        collectors = {
            "network": lambda: {
                "tcp_count": 4,
                "udp_count": 1,
                "process_details": [],
                "comparison": {"added_processes": [], "added_udp": []},
            },
            "system": lambda: {
                "access": "ok",
                "events": [{
                    "event_id": 7045,
                    "label": "service installed",
                    "time": "2026-09-01 10:00:00",
                    "provider": "Service Control Manager",
                    "details": "Service File Name: C:\\Temp\\LabApp.exe",
                }],
            },
            "application": lambda: {"access": "ok", "events": []},
            "security": lambda: {"access": "denied", "events": []},
            "persistence": lambda: {"flagged": [], "all_entries": []},
            "actions": lambda: {"entries": []},
        }
        self.service = SecurityOverviewService(
            Path(self.tempdir.name) / "security.db",
            collectors,
            summarizer=summarize,
            model_busy=lambda: False,
            emit=self.emitted.append,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def wait_for_scan(self):
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.service.scan_state()["status"] != "scanning":
                return
            time.sleep(0.02)
        self.fail("security scan did not finish")

    def test_scan_builds_alert_and_isolated_summary(self):
        self.assertTrue(self.service.refresh()["started"])
        self.wait_for_scan()
        overview = self.service.get_overview()
        self.assertEqual(overview["metrics"]["tcp_connections"], 4)
        self.assertEqual(overview["risk"]["state"], "elevated")
        self.assertEqual(len(overview["alerts"]), 1)
        self.assertEqual(overview["summary"]["source"], "model")
        self.assertEqual(self.summary_calls[0][0], "overview")
        self.assertNotIn("evidence", self.summary_calls[0][1]["alerts"][0])

    def test_whitelist_suppresses_only_the_recorded_behavior(self):
        self.service.refresh()
        self.wait_for_scan()
        alert = self.service.get_overview()["alerts"][0]
        result = self.service.add_whitelist(alert["id"], "Local test build")
        self.assertTrue(result["ok"])
        overview = result["overview"]
        self.assertEqual(overview["alerts"], [])
        self.assertEqual(len(overview["whitelisted_activity"]), 1)
        self.assertEqual(overview["whitelist"][0]["behaviors"], ["service_install"])

    def test_investigation_is_saved_without_chat_state(self):
        self.service.refresh()
        self.wait_for_scan()
        alert = self.service.get_overview()["alerts"][0]
        result = self.service.investigate(alert["id"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["investigation"]["id"].startswith("case-"))
        self.assertEqual(result["investigation"]["model_assessment"]["verdict"], "unresolved")
        self.assertEqual(len(self.service.get_overview()["investigations"]), 1)

    def test_weak_wmi_filter_stays_inventory_only(self):
        self.service.collectors["system"] = lambda: {"access": "ok", "events": []}
        self.service.collectors["persistence"] = lambda: {
            "flagged": [],
            "all_entries": [{
                "category": "wmi_filter_unbound",
                "location": "SCM Event Log Filter",
                "name": "",
                "value": "select * from MSFT_SCMEventLogEvent",
                "bound": False,
            }],
        }
        self.service.refresh()
        self.wait_for_scan()
        overview = self.service.get_overview()
        self.assertEqual(overview["alerts"], [])
        self.assertEqual(overview["risk"]["state"], "quiet")


if __name__ == "__main__":
    unittest.main()
