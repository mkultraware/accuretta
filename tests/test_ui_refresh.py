import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from security_overview import SecurityOverviewService


class UsageRefreshTests(unittest.TestCase):
    def test_calendar_keeps_history_but_model_list_requires_existing_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            present = root / "present.gguf"
            present.write_bytes(b"test fixture")
            missing = root / "removed.gguf"
            dataset = root / "turns.jsonl"
            today = datetime.date.today()
            records = [
                {"day": today.isoformat(), "model": present.name, "prompt_tokens": 20, "output_tokens": 5, "elapsed_ms": 100},
                {"day": (today - datetime.timedelta(days=200)).isoformat(), "model": missing.name, "prompt_tokens": 30, "output_tokens": 8, "elapsed_ms": 500},
            ]
            dataset.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
            profiles = {name: {"turns": 5, "completed_turns": 5} for name in (present.name, missing.name)}
            with (bridge_patch("get_settings", return_value={"models_dir": str(root / "missing-folder"), "model_path": str(present)}),
                  bridge_patch("_models_config", return_value={str(missing): {}}),
                  bridge_patch("_model_runtime_profiles", return_value={"models": profiles}),
                  bridge_patch("get_chats", return_value={"chats": {}}),
                  bridge_patch("_savings_get", return_value={}),
                  bridge_patch("MODEL_DATASET_FILE", dataset),
                  bridge_patch("FOLD_EVENTS_FILE", root / "folds.jsonl")):
                result = bridge._usage_stats()
                self.assertEqual([row["model"] for row in result["models"]], [present.name])
                self.assertEqual(result["longest"]["model"], present.name)
                self.assertEqual(len(result["activity"]["days"]), 365)
                self.assertEqual(sum(row["turns"] for row in result["activity"]["days"]), 2)
                self.assertEqual(result["activity"]["days"][-1]["day"], today.isoformat())
                self.assertEqual(result["totals"]["turns"], 2)
                present.unlink()
                result = bridge._usage_stats()
                self.assertEqual(result["models"], [])
                self.assertIsNone(result["longest"])
                self.assertEqual(result["totals"]["turns"], 2)


class SecurityAlertFilterTests(unittest.TestCase):
    """Agent tool failures (run_powershell errors etc.) are agent-health, not
    system security — since 2026-09 they never become overview alerts, so the
    quieting machinery can only ever apply to legacy recorded occurrences."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "overview.db"
        self.entries = [dict(t=i, tool="read_file", status="error", target="host", args_sha256="args-a", result_sha256="result-a") for i in range(1, 4)]
        self.collectors = {"actions": lambda: {"entries": self.entries}}
        self.service = SecurityOverviewService(self.db, self.collectors)
        self.service._scan_worker()

    def test_tool_failures_never_become_overview_alerts(self):
        # a scan whose only signal is agent tool errors stays empty
        self.assertEqual(self.service.get_overview()["alerts"], [])
        self.assertTrue(self.service.filter_alert("sec-anything", "dismiss")["ok"] is False)

    def test_filters_cannot_quiet_system_security_findings(self):
        # even a hand-crafted system-security alert is out of reach for the
        # quieting endpoint, which only ever touched recorded tool errors
        self.assertFalse(self.service.filter_alert("missing", "ignore")["ok"])
        with self.service._db() as db:
            raw = self.service._latest_raw() or {"alerts_all": []}
            raw["alerts_all"] = [{
                "id": "sec-system", "source": "security", "kind": "service_install",
                "severity": "medium", "title": "A Windows service was installed",
                "detail": "Service File Name: C:\\Temp\\BadApp.exe",
                "entity_key": "path:c:\\temp\\badapp.exe", "entity_label": "BadApp",
                "evidence": [{"t": 1}], "first_seen": "2026-09-01 10:00:00",
            }]
            db.execute("INSERT INTO scans(created_at, payload_json) VALUES (?, ?)", (1, json.dumps(raw)))
        self.assertFalse(self.service.filter_alert("sec-system", "dismiss")["ok"])


if __name__ == "__main__":
    unittest.main()
