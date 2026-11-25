"""Switching models must not rewrite a known model's tuned config.

The model advisor learns per exact runtime config, so a switch that nudges
num_ctx (or carries another model's n_cpu_moe) files the next turns under a
new fingerprint and the advisor starts learning that model from zero. The UI
used to pre-tune the target with the previous model's ctx as the floor and
save it while model_path still named the previous model.
"""
import re
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source


class FrontendSwitchTests(unittest.TestCase):
    def test_load_model_by_path_leaves_tuning_to_the_load(self):
        src = frontend_source()
        m = re.search(r"async function loadModelByPath\(.*?\n  \}\n", src, re.S)
        self.assertIsNotNone(m, "loadModelByPath not found")
        body = m.group(0)
        self.assertNotIn("/api/llama/auto-tune", body)
        self.assertNotIn("applyAutoTune(", body)
        self.assertIn("requestModelLoad(modelPath)", body)


class FreshTuneFloorTests(unittest.TestCase):
    def _fresh_tune(self, loaded):
        seen = {}

        def fake_tune(mp, vram, profile=None, min_ctx=0, **kw):
            seen["min_ctx"] = min_ctx
            return {}                      # no num_ctx: nothing gets saved

        with bridge_patch("_apply_model_config", return_value=False), \
             bridge_patch("get_settings",
                          return_value={"auto_tune_on_load": True, "vram_tier_gb": 24,
                                        "num_ctx": 151552}), \
             bridge_patch("inspect_model", return_value={}), \
             bridge_patch("auto_tune", side_effect=fake_tune), \
             patch.object(bridge._llama, "loaded_model", return_value=loaded):
            bridge._auto_tune_for_load("new.gguf")
        return seen["min_ctx"]

    def test_previous_models_ctx_is_not_the_floor(self):
        self.assertEqual(self._fresh_tune("other.gguf"), 0)

    def test_reloading_the_same_model_keeps_grow_only(self):
        self.assertEqual(self._fresh_tune("new.gguf"), 151552)


if __name__ == "__main__":
    unittest.main()
