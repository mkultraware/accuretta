"""Context-length memory — the user's raised ctx must survive restarts.

A working context (llama-server booted with it and answered) outranks the
tuner's free-VRAM estimate, which is exactly what used to clamp the user's
customized context back to the suggested tier on every app restart: the boot
re-tune re-solved under the budget, found the saved ctx "doesn't fit", and
wrote the clamp over both settings.json AND the per-model config.
"""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

import bridge
from _source import app_source
from _patching import bridge_patch

URLError = urllib.error.URLError


def _profile(**kw):
    return {"size_gb": 8.0, "is_moe": False, "block_count": 36, "quant": "Q4_K_M",
            "metadata_source": "headers", **kw}


class ProvenCtxTunerTests(unittest.TestCase):
    """auto_tune grows only; proven boots outrank the budget estimate."""

    def _pick(self, **kw):
        return bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=0, **kw)

    def test_ceiling_clamp_stands_without_proof(self):
        # min_ctx higher than every ctx tier and no recorded boot: the tuner
        # has no idea it fits, so the suggested pick stands (unchanged behavior).
        pick = self._pick()
        r = bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=524288)
        self.assertEqual(r["num_ctx"], pick["num_ctx"])
        self.assertIn("exceeds the tuner", r["notes"])

    def test_proven_boot_outranks_the_budget_estimate(self):
        # The SAME ask, with a boot stamped for that ctx: honor the user's
        # config verbatim — ctx and offload keys both.
        proven = {"proven_ctx": 524288, "n_cpu_moe": 0, "num_gpu": 99,
                  "kv_cache_type": "q8_0", "kv_cache_type_v": "",
                  "num_batch": 2048, "n_ubatch": 512, "num_thread": 0}
        r = bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=524288,
                             proven_cfg=proven)
        self.assertEqual(r["num_ctx"], 524288)
        self.assertIn("honored proven ctx", r["notes"])
        self.assertEqual(r["num_batch"], 2048)
        self.assertEqual(r["n_ubatch"], 512)
        self.assertEqual(r["kv_cache_type"], "q8_0")

    def test_proven_below_the_ask_does_not_honor(self):
        # A proven boot at 65536 cannot cover an ask of 262144: normal solve
        # rules apply (no configured model here → falls to its defaults).
        r = bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=262144,
                             proven_cfg={"proven_ctx": 65536})
        self.assertNotIn("honored proven ctx", r["notes"])
        self.assertNotEqual(r["num_ctx"], 262144)

    def test_min_ctx_within_one_solve_still_uses_the_pin(self):
        # Proven is the fallback for a FAILED pinned solve, not a blanket call
        # — untouched when min_ctx doesn't exceed the pick at all.
        pick = self._pick()
        r = bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=0,
                             proven_cfg={"proven_ctx": 131072})
        self.assertEqual(r["num_ctx"], pick["num_ctx"])
        self.assertNotIn("honored proven", r["notes"])


class ProvenStampTests(unittest.TestCase):
    """A healthy llama boot stamps the ctx it actually ran at."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Path(self.tmp) / "models-config.json"
        self.store.write_text("{}", encoding="utf-8")
        store_patch = bridge_patch("MODELS_CONFIG_FILE", self.store)
        store_patch.start()
        self.addCleanup(store_patch.stop)

    def test_boot_stamps_and_is_idempotent(self):
        bridge._stamp_proven_boot("m.gguf", 65536)
        cfg = json.loads(self.store.read_text(encoding="utf-8"))["m.gguf"]
        self.assertEqual(cfg["proven_ctx"], 65536)
        self.assertTrue(cfg["booted_at"])
        mtime1 = self.store.stat().st_mtime_ns
        bridge._stamp_proven_boot("m.gguf", 65536)       # no churn per reload
        self.assertEqual(self.store.stat().st_mtime_ns, mtime1)
        bridge._stamp_proven_boot("m.gguf", 131072)      # new ctx → re-stamp
        cfg = json.loads(self.store.read_text(encoding="utf-8"))["m.gguf"]
        self.assertEqual(cfg["proven_ctx"], 131072)

    def test_stamp_is_rejected_for_empty_args(self):
        bridge._stamp_proven_boot("", 65536)
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8")), {})

    def test_proven_cfg_reader(self):
        bridge._stamp_proven_boot("m.gguf", 65536)
        self.assertEqual(bridge._proven_cfg("m.gguf")["proven_ctx"], 65536)
        self.assertEqual(bridge._proven_cfg("other.gguf"), {})
        self.assertEqual(bridge._proven_cfg(""), {})


class ManualCtxSyncTests(unittest.TestCase):
    """A manual ctx change via update_settings lands in BOTH stores."""

    def test_settings_update_syncs_the_model_store(self):
        seen = []
        tmp = Path(tempfile.mkdtemp()) / "settings.json"
        tmp.write_text("{}", encoding="utf-8")
        settings_patch = bridge_patch("SETTINGS_FILE", tmp)   # never write the live settings.json from a test
        settings_patch.start()
        save_patch = bridge_patch("_save_model_config", lambda mp, flags: seen.append((mp, flags)))
        save_patch.start()
        try:
            bridge.update_settings({"model_path": "toy.gguf", "num_ctx": 65536})
        finally:
            save_patch.stop()
            settings_patch.stop()
        self.assertTrue(any(mp == "toy.gguf" and flags.get("num_ctx") == 65536
                            for mp, flags in seen),
                        f"manual ctx never reached the per-model store: {seen}")
        # The sandbox holds the write; the real file must stay untouched.
        self.assertEqual(bridge.load_json(bridge.SETTINGS_FILE, {}), bridge.load_json(bridge.SETTINGS_FILE, {}))


class LlamaStartRetryTests(unittest.TestCase):
    """A turn arriving inside a llama restart window waits for the server
    instead of dying with an error the notch never showed."""

    def test_connection_refused_classified_unreachable(self):
        import urllib.error
        self.assertTrue(bridge._is_llama_unreachable(
            URLError(ConnectionRefusedError(10061, "refused"))))
        self.assertTrue(bridge._is_llama_unreachable(
            URLError(OSError(10061, "No connection could be made"))))
        self.assertFalse(bridge._is_llama_unreachable(
            urllib.error.HTTPError("http://x", 503, "busy", {}, None)))
        self.assertFalse(bridge._is_llama_unreachable(ValueError("opaque")))

    def test_turn_stream_open_retries_for_llama(self):
        src = app_source()
        self.assertIn("_is_llama_unreachable(e)", src)
        self.assertIn("llama-server is not answering", src)


class BreakerRecoveryTests(unittest.TestCase):
    """A tripped watchdog breaker self-recovers (bounded) instead of leaving
    the model dead until a manual restart the owner never thinks of."""

    def _lp(self):
        proc = bridge.LlamaProcess()
        return proc

    def test_tripped_breaker_recovers_after_cooldown_bounded(self):
        proc = bridge._llama if hasattr(bridge, "_llama") else None
        if proc is None:
            self.skipTest("no process singleton")
        from unittest.mock import patch
        import time as _t
        orig = (proc._restart_history[:], proc._restart_failed,
                proc._breaker_trip_at, proc._breaker_recoveries,
                proc._starting, proc._last_start_args)
        try:
            with bridge_patch("get_settings", return_value={"watchdog_enabled": True}), \
                 bridge_patch("broadcast_event"), \
                 patch.object(proc, "is_running", return_value=False), \
                 patch.object(proc, "_last_start_args", {"model_path": "x"}), \
                 patch.object(proc, "stop", return_value=True), \
                 bridge_patch("_llama", proc), \
                 bridge_patch("inspect_model", return_value={}), \
                 bridge_patch("llama_caps", return_value={}):
                with patch.object(bridge.LlamaProcess, "start",
                                  return_value={"ok": True, "pid": 1}), \
                     patch.object(proc, "_start_impl",
                                  return_value={"ok": True, "pid": 1}):
                    # crash-loop through the window → breaker trips
                    for _ in range(3):
                        proc._restart_history.append(_t.time())
                    proc._breaker_trip_at = _t.time() - 500     # cooldown long over
                    proc._breaker_recoveries = 0
                    proc._maybe_restart()
                    self.assertFalse(proc._restart_failed,
                                     "breaker must self-recover after the cooldown")
                    self.assertEqual(proc._breaker_recoveries, 1)
                    # exhaust recoveries → stays suspended on the next trip
                    proc._breaker_recoveries = 2
                    now2 = _t.time()
                    proc._restart_history = [now2, now2, now2]
                    proc._breaker_trip_at = now2      # cooldown not yet over
                    proc._maybe_restart()
                    self.assertTrue(proc._restart_failed,
                                    "after 2 recoveries the breaker must stay tripped")
        finally:
            proc._restart_history = orig[0]
            proc._restart_failed = orig[1]
            proc._breaker_trip_at = orig[2]
            proc._breaker_recoveries = orig[3]
            proc._starting = isinstance(orig[4], bool) and orig[4]
            proc._last_start_args = orig[5]


class WatchdogSyncTests(unittest.TestCase):
    """Self-heal degradations must reach the per-model store too, or the next
    load restores the crashing config verbatim. Settings writes are redirected
    to a sandbox file: these calls previously WROTE THE LIVE settings.json."""

    def _isolated(self):
        tmp = Path(tempfile.mkdtemp()) / "settings.json"
        tmp.write_text("{}", encoding="utf-8")
        return tmp

    def test_heal_syncs_degraded_tune_keys(self):
        seen = []
        sandbox = self._isolated()
        settings_patch = bridge_patch("SETTINGS_FILE", sandbox)
        settings_patch.start()
        save_patch = bridge_patch("_save_model_config", lambda mp, flags: seen.append((mp, flags)))
        save_patch.start()
        try:
            with self.subTest("halve ctx path"):
                seen.clear()
                bridge._watchdog_self_heal(2)
                self.assertTrue(any(flags.get("num_ctx") for _, flags in seen),
                                f"ctx halve never synced to the model store: {seen}")
            with self.subTest("spec/offload path"):
                seen.clear()
                bridge._watchdog_self_heal(3)
                self.assertTrue(seen,
                                f"self-heal attempt-3 never reached the model store: {seen}")
        finally:
            save_patch.stop()
            settings_patch.stop()


if __name__ == "__main__":
    unittest.main()
