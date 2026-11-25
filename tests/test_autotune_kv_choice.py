"""Auto-tune sizes context for the K / V cache types picked in its panel.

Before, the tuner always planned with q8_0 (q4_0 only as an escape hatch on
<=12 GB cards), so a big dense model on a 24 GB card got a tiny context with
no way to ask for a smaller cache.
"""
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch


def _profile(**kw):
    # A big dense model on a 24 GB card, leaving a few GB for context.
    return {"size_gb": 14.0, "is_moe": False, "block_count": 52, "quant": "IQ4_XS",
            "metadata_source": "headers", **kw}


class AutoTuneKvChoiceTests(unittest.TestCase):
    def tune(self, k="auto", v="", vram=24.0, proven=None):
        # A low speed floor so context is bounded by memory, which is what the
        # cache type changes (an unreachable floor pins the analytic pick at 4k).
        settings = {**bridge.DEFAULT_SETTINGS, "min_decode_tok_s": 5,
                    "autotune_kv_cache": k, "autotune_kv_cache_v": v}
        with bridge_patch("get_settings", lambda: settings):
            return bridge.auto_tune("m.gguf", vram, profile=_profile(), proven_cfg=proven)

    def test_auto_keeps_the_existing_pick(self):
        r = self.tune()
        self.assertEqual(r["kv_cache_type"], "q8_0")
        self.assertEqual(r["kv_cache_type_v"], "")

    def test_chosen_k_type_is_used_and_resizes_context(self):
        q8, q4, f16 = self.tune("q8_0"), self.tune("q4_0"), self.tune("f16")
        self.assertEqual((q8["kv_cache_type"], q4["kv_cache_type"], f16["kv_cache_type"]),
                         ("q8_0", "q4_0", "f16"))
        self.assertGreater(q4["num_ctx"], q8["num_ctx"])
        self.assertGreater(q8["num_ctx"], f16["num_ctx"])
        self.assertIn("your choice in Auto-tune", q4["notes"])

    def test_chosen_v_type_is_used(self):
        mixed = self.tune("q8_0", "q4_0")
        self.assertEqual((mixed["kv_cache_type"], mixed["kv_cache_type_v"]), ("q8_0", "q4_0"))
        # Between all-q8_0 and all-q4_0 (the analytic tiers are coarse, so >=).
        self.assertGreaterEqual(mixed["num_ctx"], self.tune("q8_0")["num_ctx"])
        self.assertLessEqual(mixed["num_ctx"], self.tune("q4_0")["num_ctx"])
        self.assertIn("slower attention path", mixed["notes"])

    def test_v_same_as_k_is_not_emitted_as_a_split(self):
        self.assertEqual(self.tune("q4_0", "q4_0")["kv_cache_type_v"], "")

    def test_auto_does_not_switch_on_its_own_on_big_cards(self):
        self.assertEqual(self.tune(vram=24.0)["kv_cache_type"], "q8_0")

    def test_proven_boot_with_other_cache_types_does_not_override_a_choice(self):
        proven = {"proven_ctx": 262144, "num_ctx": 262144, "kv_cache_type": "q8_0"}
        settings = {**bridge.DEFAULT_SETTINGS, "min_decode_tok_s": 5, "autotune_kv_cache": "q4_0"}
        with bridge_patch("get_settings", lambda: settings):
            r = bridge.auto_tune("m.gguf", 24.0, profile=_profile(), min_ctx=262144, proven_cfg=proven)
        self.assertEqual(r["kv_cache_type"], "q4_0")
        self.assertNotIn("honored proven ctx", r["notes"])

    def test_settings_validation(self):
        with bridge_patch("save_json"), bridge_patch("_save_model_config"):
            cur = bridge._update_settings_locked({"autotune_kv_cache": "Q4_0", "autotune_kv_cache_v": "bogus"})
        self.assertEqual(cur["autotune_kv_cache"], "q4_0")
        self.assertEqual(cur["autotune_kv_cache_v"], "")


if __name__ == "__main__":
    unittest.main()
