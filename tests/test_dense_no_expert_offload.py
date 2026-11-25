"""Dense models never carry --n-cpu-moe.

n_cpu_moe is one shared setting, so a value a MoE model left behind used to
ride into a dense model's saved config (a drawer save right after a model
switch writes only touched keys), get stamped as a proven boot, and then be
copied back by every re-tune. llama.cpp ignores it on a dense model, but the
tuner reported "N of M expert layers run from system RAM" and skipped the
spare-VRAM ubatch step.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


def _profile(**kw):
    return {"size_gb": 8.0, "is_moe": False, "block_count": 36, "quant": "Q4_K_M",
            "metadata_source": "headers", **kw}


class TunerTests(unittest.TestCase):
    def test_proven_combo_cannot_give_a_dense_model_expert_offload(self):
        proven = {"proven_ctx": 524288, "n_cpu_moe": 10, "num_gpu": 99,
                  "kv_cache_type": "q8_0", "kv_cache_type_v": "",
                  "num_batch": 2048, "n_ubatch": 512, "num_thread": 0}
        r = bridge.auto_tune("m.gguf", 12.0, profile=_profile(), min_ctx=524288,
                             proven_cfg=proven)
        self.assertIn("honored proven ctx", r["notes"])
        self.assertEqual(r["n_cpu_moe"], 0)
        self.assertFalse(any("expert layers" in line for line in r["summary"]), r["summary"])


class ApplyModelConfigTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = tmp / "models_config.json"
        self.settings = tmp / "settings.json"
        self.settings.write_text(json.dumps({"n_cpu_moe": 11}), encoding="utf-8")
        for name, path in (("MODELS_CONFIG_FILE", self.store), ("SETTINGS_FILE", self.settings)):
            p = bridge_patch(name, path)
            p.start()
            self.addCleanup(p.stop)

    def _apply(self, cfg, dense):
        self.store.write_text(json.dumps({"m.gguf": cfg}), encoding="utf-8")
        with bridge_patch("_model_is_dense", return_value=dense):
            self.assertTrue(bridge._apply_model_config("m.gguf"))
        return (bridge.load_json(self.settings, {}).get("n_cpu_moe"),
                bridge.load_json(self.store, {})["m.gguf"].get("n_cpu_moe"))

    def test_partial_config_does_not_inherit_previous_offload(self):
        live, _ = self._apply({"num_ctx": 65536, "spec_strategy": "draft-mtp"}, dense=False)
        self.assertEqual(live, 0)

    def test_stale_dense_value_is_healed_in_both_stores(self):
        live, saved = self._apply({"num_ctx": 65536, "n_cpu_moe": 10}, dense=True)
        self.assertEqual((live, saved), (0, 0))

    def test_moe_offload_is_kept(self):
        live, saved = self._apply({"num_ctx": 65536, "n_cpu_moe": 10}, dense=False)
        self.assertEqual((live, saved), (10, 10))


class DenseDetectionTests(unittest.TestCase):
    def test_unreadable_header_is_not_called_dense(self):
        with bridge_patch("read_gguf_metadata", return_value={"ok": False}):
            self.assertFalse(bridge._model_is_dense("x.gguf"))
        with bridge_patch("read_gguf_metadata", return_value={"ok": True, "expert_count": 0}):
            self.assertTrue(bridge._model_is_dense("x.gguf"))
        with bridge_patch("read_gguf_metadata", return_value={"ok": True, "expert_count": 128}):
            self.assertFalse(bridge._model_is_dense("x.gguf"))

    def test_real_dense_gguf_if_present(self):
        path = r"D:\MODELS\Qwen3.8-27B-UD-Q4_K_M.gguf"
        if not Path(path).exists():
            self.skipTest("model file not on this machine")
        self.assertTrue(bridge._model_is_dense(path))


if __name__ == "__main__":
    unittest.main()
