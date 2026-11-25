"""One machine, one answer: card-size decisions don't depend on the caller.

Model loads and the Auto-tune button pass the VRAM tier (24 GB on a 4090),
other paths pass live free VRAM (22.8 GB on the same card). The tuner used
that number for "is this a 24 GB card?", so the same 4090 got batch 2048 /
ubatch 1024 (138,240 ctx) one way and batch 1024 / ubatch 512 (158,720 ctx)
the other. Card size now comes from the tier, else the detected total.
"""
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch

_4090 = {"gb": 22.8, "free_total_gb": 22.8, "total_gb": 24.0, "source": "nvidia-smi",
         "gpus": [{"name": "NVIDIA GeForce RTX 4090", "total_gb": 24.0, "free_gb": 22.8}]}


def _settings(tier):
    real = bridge.get_settings
    return lambda: {**real(), "vram_tier_gb": tier}


class CardSizeTests(unittest.TestCase):
    def test_tier_wins_then_detected_total_then_budget(self):
        with bridge_patch("detect_vram_gb", return_value=_4090):
            with bridge_patch("get_settings", side_effect=_settings(16)):
                self.assertEqual(bridge._tune_card_gb(22.8), 16.0)
            with bridge_patch("get_settings", side_effect=_settings(0)):
                self.assertEqual(bridge._tune_card_gb(22.8), 24.0)
        with bridge_patch("detect_vram_gb", return_value={}), \
                bridge_patch("get_settings", side_effect=_settings(0)):
            self.assertEqual(bridge._tune_card_gb(11.5), 11.5)

    def test_multi_gpu_sums_the_cards(self):
        two = {"gpus": [{"total_gb": 24.0, "free_gb": 23.0}, {"total_gb": 12.0, "free_gb": 11.5}]}
        with bridge_patch("detect_vram_gb", return_value=two), \
                bridge_patch("get_settings", side_effect=_settings(0)):
            self.assertEqual(bridge._tune_card_gb(34.5), 36.0)

    def test_free_vram_and_tier_give_the_same_card_choices(self):
        prof = {"size_gb": 15.4, "is_moe": False, "block_count": 64, "quant": "Q4_K_M",
                "metadata_source": "headers"}
        keys = ("num_batch", "n_ubatch", "kv_cache_type", "kv_cache_type_v")
        with bridge_patch("detect_vram_gb", return_value=_4090):
            with bridge_patch("get_settings", side_effect=_settings(24)):
                tier = bridge.auto_tune("m.gguf", 24.0, profile=prof)
            with bridge_patch("get_settings", side_effect=_settings(0)):
                free = bridge.auto_tune("m.gguf", 22.8, profile=prof)
        self.assertEqual({k: tier.get(k) for k in keys}, {k: free.get(k) for k in keys})
        self.assertEqual(free["num_batch"], 2048)


if __name__ == "__main__":
    unittest.main()
