"""Ghidra runs in its own process with a capped heap.

It used to run inside the bridge: Java took up to a quarter of RAM, kept it
until the app quit (a JVM can't restart in-process), and a timed-out analysis
kept running in a thread nobody could stop. A fake worker that speaks the same
protocol stands in for Ghidra here.
"""
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
import ghidra_worker

FAKE = textwrap.dedent(r'''
    import json, os, sys, time
    MARK = "@@ACCURETTA-GHIDRA@@ "
    mode = os.environ.get("FAKE_MODE", "ok")
    print("ghidra noise on stdout that is not an answer", flush=True)
    if mode == "fatal":
        print(MARK + json.dumps({"fatal": "no JDK found"}), flush=True); sys.exit(1)
    print(MARK + json.dumps({"ready": True, "heap_mb": int(os.environ["ACCURETTA_GHIDRA_HEAP_MB"])}), flush=True)
    idle = float(os.environ.get("ACCURETTA_GHIDRA_IDLE_S", "300"))
    import threading, queue
    q = queue.Queue()
    threading.Thread(target=lambda: [q.put(l) for l in sys.stdin] + [q.put(None)], daemon=True).start()
    while True:
        try:
            line = q.get(timeout=idle)
        except queue.Empty:
            sys.exit(0)
        if line is None:
            sys.exit(0)
        req = json.loads(line)
        if mode == "slow":
            time.sleep(60)
        if mode == "die":
            sys.exit(3)
        print(MARK + json.dumps({"id": req["id"], "result": {"ok": True, "pid": os.getpid(),
              "path": req["path"], "heap_mb": int(os.environ["ACCURETTA_GHIDRA_HEAP_MB"])}}), flush=True)
''')


class WorkerProcessTests(unittest.TestCase):
    def setUp(self):
        self.script = Path(tempfile.mkdtemp()) / "fake_ghidra_worker.py"
        self.script.write_text(FAKE, encoding="utf-8")
        self.worker = bridge._GhidraWorker([sys.executable, "-u", str(self.script)])
        self.addCleanup(self.worker.stop)

    def env(self, mode="ok", idle=300):
        return {"FAKE_MODE": mode, "ACCURETTA_GHIDRA_HEAP_MB": "2048", "ACCURETTA_GHIDRA_IDLE_S": str(idle)}

    def run_one(self, mode="ok", timeout=20, heap=2048, idle=300):
        env = self.env(mode, idle)
        env["ACCURETTA_GHIDRA_HEAP_MB"] = str(heap)
        return self.worker.analyze({"path": "x.so"}, timeout, env, heap)

    def test_answers_and_reuses_the_process(self):
        first = self.run_one()
        self.assertTrue(first.get("ok"), first)
        second = self.run_one()
        self.assertEqual(first["pid"], second["pid"], "the second analysis should reuse the worker")

    def test_timeout_kills_the_worker(self):
        out = self.run_one(mode="slow", timeout=2)
        self.assertIn("timed out", out["error"])
        self.assertIn("stopped", out["error"])
        self.assertFalse(self.worker.alive(), "a timed-out analysis must not keep running")

    def test_start_failure_is_reported(self):
        out = self.run_one(mode="fatal")
        self.assertEqual(out["error"], "no JDK found")
        self.assertFalse(self.worker.alive())

    def test_crash_mid_analysis_is_reported(self):
        out = self.run_one(mode="die")
        self.assertIn("stopped mid-analysis", out["error"])
        self.assertFalse(self.worker.alive())

    def test_memory_limit_change_restarts_the_worker(self):
        first = self.run_one(heap=2048)
        second = self.run_one(heap=3072)
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(second["heap_mb"], 3072)

    def test_idle_exit_then_next_request_starts_a_new_one(self):
        first = self.run_one(idle=1)
        deadline = time.time() + 15
        while self.worker.alive() and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(self.worker.alive(), "the worker should exit by itself when idle")
        second = self.run_one(idle=1)
        self.assertTrue(second.get("ok"), second)
        self.assertNotEqual(first["pid"], second["pid"])


class HeapAndIdleSettingTests(unittest.TestCase):
    def test_setting_wins(self):
        self.assertEqual(bridge._ghidra_heap_mb({"ghidra_max_heap_gb": 3}), 3072)

    def test_auto_is_an_eighth_of_ram_between_2_and_8_gb(self):
        for total_gb, want in ((8, 2048), (32, 4096), (128, 8192)):
            with self.subTest(total_gb=total_gb), bridge_patch("_system_ram_info", lambda t=total_gb: (t, 1)):
                self.assertEqual(bridge._ghidra_heap_mb({"ghidra_max_heap_gb": 0}), want)

    def test_idle_minutes(self):
        self.assertEqual(bridge._ghidra_idle_s({"ghidra_idle_minutes": 5}), 300)
        self.assertEqual(bridge._ghidra_idle_s({}), 300)

    def test_defaults_exist(self):
        self.assertEqual(bridge.DEFAULT_SETTINGS["ghidra_max_heap_gb"], 0)
        self.assertEqual(bridge.DEFAULT_SETTINGS["ghidra_idle_minutes"], 5)


class ProjectFolderTests(unittest.TestCase):
    def test_project_name_is_stable_and_changes_with_the_file(self):
        f = Path(tempfile.mkdtemp()) / "lib native.so"
        f.write_bytes(b"\x7fELF" + b"\0" * 64)
        a = ghidra_worker.project_name_for(str(f))
        self.assertEqual(a, ghidra_worker.project_name_for(str(f)))
        self.assertTrue(a.startswith("lib_native.so_"))
        time.sleep(0.02)
        f.write_bytes(b"\x7fELF" + b"\1" * 80)
        os.utime(f, None)
        self.assertNotEqual(a, ghidra_worker.project_name_for(str(f)))

    def test_stale_lock_cleanup_only_touches_lock_files(self):
        d = Path(tempfile.mkdtemp())
        for name in ("p.lock", "p.lock~", "p.gpr", "keep.txt"):
            (d / name).write_text("x", encoding="utf-8")
        self.assertEqual(ghidra_worker.clear_stale_locks(d), 2)
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["keep.txt", "p.gpr"])

    def test_bridge_shares_the_danger_list(self):
        self.assertIs(bridge._GHIDRA_DANGER_IMPORTS, ghidra_worker.DANGER_IMPORTS)
        self.assertIn("strcpy", bridge._GHIDRA_DANGER_IMPORTS)


if __name__ == "__main__":
    unittest.main()
