"""The vision projector runs on the CPU when the build supports it.

Measured on a 4090 with Qwen3.8-27B-UD-Q4_K_M at 100,352 ctx and the F16
mmproj: --no-mmproj-offload freed 1,136 MiB of VRAM (~30k more context) for
~7 s more per ~900-token screenshot; text decode stayed at 77-80 tok/s.
The tuner must only reserve projector VRAM when it actually lands on the GPU.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch

_DEFAULTS = bridge.llama_caps("")


def _caps(cpu: bool):
    return lambda *_a, **_k: {**_DEFAULTS, "no_mmproj_offload": cpu}


class CapsProbeTests(unittest.TestCase):
    def caps_for(self, help_text):
        done = subprocess.CompletedProcess(["llama-server", "--help"], 0, stdout=help_text, stderr="")
        bridge._LLAMA_CAPS_CACHE.clear()
        try:
            with patch.object(bridge.subprocess, "run", return_value=done):
                return bridge.llama_caps("fake-llama-server.exe")
        finally:
            bridge._LLAMA_CAPS_CACHE.clear()

    def test_flag_in_help_turns_it_on(self):
        caps = self.caps_for("--mmproj-offload, --no-mmproj-offload   whether to enable GPU offloading\n")
        self.assertTrue(caps["no_mmproj_offload"])
        self.assertTrue(bridge._mmproj_on_cpu(caps))

    def test_old_build_keeps_projector_on_gpu(self):
        caps = self.caps_for("-mm,   --mmproj FILE   path to a multimodal projector file\n")
        self.assertFalse(caps["no_mmproj_offload"])
        self.assertFalse(bridge._mmproj_on_cpu(caps))

    def test_unprobed_defaults_fail_closed(self):
        self.assertFalse(bridge._mmproj_on_cpu(_DEFAULTS))
        self.assertFalse(bridge._mmproj_on_cpu({}))


class TunerReserveTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.mmproj = tmp / "mmproj-F16.gguf"
        with open(self.mmproj, "wb") as fh:
            fh.truncate(900 * 1024 * 1024)       # sparse ~0.9 GB, like the real F16 file

    def _tune(self, cpu: bool):
        prof = {"size_gb": 15.4, "is_moe": False, "block_count": 64, "quant": "Q4_K_M",
                "metadata_source": "headers"}
        with bridge_patch("_resolve_mmproj_for_tune", return_value=str(self.mmproj)), \
                bridge_patch("llama_caps", side_effect=_caps(cpu)):
            return bridge.auto_tune("m.gguf", 24.0, profile=prof)

    def test_cpu_projector_reserves_no_vram(self):
        on_cpu, on_gpu = self._tune(cpu=True), self._tune(cpu=False)
        self.assertIn("runs on the CPU", on_cpu["notes"])
        self.assertNotIn("MB reserved (mmproj", on_cpu["notes"])
        self.assertIn("vision projector: 900 MB reserved", on_gpu["notes"])
        self.assertGreaterEqual(on_cpu["num_ctx"], on_gpu["num_ctx"])


if __name__ == "__main__":
    unittest.main()
