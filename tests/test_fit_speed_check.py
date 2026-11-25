"""Load-time speed check: the advisor finds a dense model's real context limit.

On Windows a dense model whose context doesn't fit in VRAM still loads, with
part of it in system RAM, and only generation speed shows it. Numbers below
are the validation runs on a 4090 with Qwen3.8-27B-UD-Q4_K_M: first probe
after loading 83.2 / 83.0 / 82.4 tok/s at 100k / 135k / 147k context, and
70.1 / 30.7 tok/s on two loads at 160,768.
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch

MODEL = r"D:\MODELS\Qwen3.8-27B-UD-Q4_K_M.gguf"
SETTINGS = {"model_path": MODEL, "model": "Qwen3.8-27B-UD-Q4_K_M", "num_ctx": 160768,
            "num_gpu": 99, "n_cpu_moe": 0, "num_batch": 1024, "n_ubatch": 0,
            "kv_cache_type": "q8_0", "spec_strategy": "draft-mtp", "flash_attn": True,
            "passive_model_telemetry": True}


def _samples(settings, speeds):
    group = bridge._fit_group(bridge._model_runtime_config(settings))
    now = int(time.time())
    return [{"ts": now - 100 + i, "group": group, "ctx": c, "tok_s": v}
            for i, (c, v) in enumerate(speeds)]


class CeilingTests(unittest.TestCase):
    def test_validation_runs(self):
        healthy = {100352: 83.2, 135168: 83.0, 147456: 82.4}
        self.assertEqual(bridge._fit_ceiling(healthy), 0)
        self.assertEqual(bridge._fit_ceiling({**healthy, 160768: 70.1}), 147456)
        self.assertEqual(bridge._fit_ceiling({**healthy, 160768: 30.7}), 147456)

    def test_noise_is_not_a_spill(self):
        self.assertEqual(bridge._fit_ceiling({100352: 83.2, 160768: 80.0}), 0)

    def test_newest_sample_per_context_wins_and_groups_stay_apart(self):
        s = dict(SETTINGS)
        rows = _samples(s, [(160768, 70.1), (160768, 82.9)])
        rows.append({**rows[0], "group": "other", "ctx": 100352, "tok_s": 99.0})
        self.assertEqual(bridge._fit_speeds(rows, rows[0]["group"]), {160768: 82.9})

    def test_old_samples_expire(self):
        rows = _samples(SETTINGS, [(160768, 70.1)])
        rows[0]["ts"] = int(time.time()) - bridge._LOAD_SAMPLE_MAX_AGE_S - 10
        self.assertEqual(bridge._fit_speeds(rows, rows[0]["group"]), {})


class AdviceTests(unittest.TestCase):
    def advise(self, settings, speeds, state=None, dense=True, windows=True):
        data = {"load_samples": {"Qwen3.8-27B-UD-Q4_K_M": _samples(settings, speeds)},
                "fit_state": {"Qwen3.8-27B-UD-Q4_K_M": state or {}}}
        with bridge_patch("_model_is_dense", return_value=dense), \
                bridge_patch("_silent_spill_possible", return_value=windows):
            return bridge._advisor_fit_advice(data, settings), data

    def test_first_load_offers_one_check_on_windows_only(self):
        adv, _ = self.advise(SETTINGS, [(160768, 82.0)])
        rec = adv["recommendation"]
        self.assertEqual(rec["kind"], "fit_check")
        self.assertEqual(rec["updates"]["num_ctx"], 140288)    # 1/8 less, whole KiB
        self.assertEqual(self.advise(SETTINGS, [(160768, 82.0)], windows=False)[0], {})

    def test_no_sample_at_current_context_means_no_advice(self):
        self.assertEqual(self.advise(SETTINGS, [(147456, 82.4)])[0], {})

    def test_slow_current_context_suggests_the_largest_fast_one(self):
        adv, _ = self.advise(SETTINGS, [(100352, 83.2), (147456, 82.4), (160768, 70.1)])
        rec = adv["recommendation"]
        self.assertEqual((rec["kind"], rec["updates"]["num_ctx"]), ("fit_down", 147456))
        self.assertIn("15% slower", rec["reason"])

    def test_check_with_same_speed_suggests_going_back(self):
        small = dict(SETTINGS, num_ctx=140288)
        adv, _ = self.advise(small, [(160768, 82.4), (140288, 83.0)],
                             state={"check_from": 160768, "check_to": 140288})
        self.assertEqual(adv["recommendation"]["kind"], "fit_up")
        self.assertEqual(adv["recommendation"]["updates"]["num_ctx"], 160768)

    def test_check_that_finds_a_spill_settles_with_a_result(self):
        small = dict(SETTINGS, num_ctx=140288)
        adv, data = self.advise(small, [(160768, 70.1), (140288, 83.0)],
                                state={"check_from": 160768, "check_to": 140288})
        self.assertNotIn("recommendation", adv)
        with bridge_patch("_model_is_dense", return_value=True):
            self.assertTrue(bridge._fit_settle(data, small))
        state = data["fit_state"]["Qwen3.8-27B-UD-Q4_K_M"]
        self.assertIn("didn't fit in VRAM", state["result"])
        self.assertNotIn("check_from", state)

    def test_moe_and_partial_offload_are_left_alone(self):
        self.assertEqual(self.advise(SETTINGS, [(160768, 82.0)], dense=False)[0], {})
        self.assertEqual(self.advise(dict(SETTINGS, num_gpu=40), [(160768, 82.0)])[0], {})

    def test_dismissed_advice_stays_dismissed(self):
        adv, _ = self.advise(SETTINGS, [(160768, 82.0)])
        rid = adv["recommendation"]["id"]
        self.assertNotIn("recommendation", self.advise(SETTINGS, [(160768, 82.0)],
                                                       state={"dismissed": [rid]})[0])


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.runtime, self.settings, self.models = (tmp / "rt.json", tmp / "s.json", tmp / "m.json")
        self.settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
        self.models.write_text(json.dumps({MODEL: {"num_ctx": 160768}}), encoding="utf-8")
        for name, path in (("MODEL_RUNTIME_FILE", self.runtime), ("SETTINGS_FILE", self.settings),
                           ("MODELS_CONFIG_FILE", self.models)):
            p = bridge_patch(name, path)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("_model_is_dense", True), ("_silent_spill_possible", True),
                            ("broadcast_event", None)):
            p = bridge_patch(name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_check_round_trip_and_tuner_cap(self):
        bridge._record_load_sample(MODEL, 160768, 70.1)
        rec = bridge._advisor_fit_advice(bridge.load_json(self.runtime, {}),
                                         bridge.get_settings())["recommendation"]
        out = bridge._model_health_action("fit_apply", rec["id"])
        self.assertTrue(out["ok"] and out["reload"])
        self.assertEqual(bridge.get_settings()["num_ctx"], 140288)
        self.assertEqual(bridge.load_json(self.models, {})[MODEL]["num_ctx"], 140288)
        bridge._record_load_sample(MODEL, 140288, 83.0)     # the reload's own sample
        data = bridge.load_json(self.runtime, {})
        self.assertTrue(bridge._fit_settle(data, bridge.get_settings()))
        self.assertEqual(bridge._fit_ceiling_for(MODEL), 140288)

    def test_stale_id_is_refused(self):
        bridge._record_load_sample(MODEL, 160768, 82.0)
        self.assertFalse(bridge._model_health_action("fit_apply", "nope")["ok"])

    def test_zero_speed_is_not_recorded(self):
        bridge._record_load_sample(MODEL, 160768, 0.0)
        self.assertEqual(bridge.load_json(self.runtime, {}), {})


class TunerCapTests(unittest.TestCase):
    def test_dense_tune_is_held_at_the_measured_ceiling(self):
        prof = {"size_gb": 8.0, "is_moe": False, "block_count": 36, "quant": "Q4_K_M",
                "metadata_source": "headers"}
        with bridge_patch("_fit_ceiling_for", return_value=12288):
            r = bridge.auto_tune("m.gguf", 24.0, profile=prof)
        self.assertEqual(r["num_ctx"], 12288)
        self.assertIn("measured ceiling", r["notes"])

    def test_moe_tune_ignores_it(self):
        prof = {"size_gb": 8.0, "is_moe": True, "block_count": 36, "quant": "Q4_K_M",
                "metadata_source": "headers"}
        with bridge_patch("_fit_ceiling_for", return_value=12288):
            self.assertNotIn("measured ceiling", bridge.auto_tune("m.gguf", 24.0, profile=prof)["notes"])


if __name__ == "__main__":
    unittest.main()
