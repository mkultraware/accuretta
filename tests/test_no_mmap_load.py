"""A model that runs fully on the GPU is loaded without a memory-mapped copy.

llama.cpp maps the .gguf by default and the mapped pages stay in RAM after the
weights are copied to the GPU: 12.6 GB held for a 16.35 GB Qwen3.8 quant whose
every layer ran on the GPU, with the system at 0.7 GB free.
"""
import unittest

import bridge

NEW = {"load_mode": True, "no_mmap": False}
OLD = {"load_mode": False, "no_mmap": True}


class NoMmapFlagTests(unittest.TestCase):
    def test_full_gpu_model_loads_without_mapping(self):
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 0), ["--load-mode", "none"])

    def test_older_builds_use_no_mmap(self):
        self.assertEqual(bridge._no_mmap_flags(OLD, -1, 0), ["--no-mmap"])

    def test_builds_with_neither_flag_get_nothing(self):
        self.assertEqual(bridge._no_mmap_flags({}, -1, 0), [])

    def test_cpu_offload_keeps_the_mapping(self):
        # Offloaded layers or MoE experts run from the mapped file.
        self.assertEqual(bridge._no_mmap_flags(NEW, 40, 0), [])
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 12), [])

    def test_small_expert_offload_unmaps(self):
        # Seen 2026-11-21: Inkling 2 Mini, 4 of 24 expert layers on the CPU
        # (~2.7 GB), held 15.5 GB of RAM through the map; 22 GB were free.
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 4, cpu_share_gb=2.7, ram_free_gb=22.0),
                         ["--load-mode", "none"])
        self.assertEqual(bridge._no_mmap_flags(OLD, -1, 4, cpu_share_gb=2.7, ram_free_gb=22.0),
                         ["--no-mmap"])

    def test_large_expert_offload_keeps_the_mapping(self):
        # Most of a big MoE on the CPU: the map lets Windows page instead of running out.
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 30, cpu_share_gb=15.5, ram_free_gb=20.0), [])
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 4, cpu_share_gb=2.7, ram_free_gb=0.0), [])
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 4, cpu_share_gb=2.7, ram_free_gb=22.0,
                                               extra_args="--mmap"), [])
        # Dense partial offload runs from the map regardless.
        self.assertEqual(bridge._no_mmap_flags(NEW, 40, 0, cpu_share_gb=2.0, ram_free_gb=30.0), [])

    def test_user_extra_args_win(self):
        for extra in ("--load-mode mmap", "-lm dio", "--no-mmap", "--mlock", "--mmap"):
            with self.subTest(extra=extra):
                self.assertEqual(bridge._no_mmap_flags(NEW, -1, 0, extra), [])
        self.assertEqual(bridge._no_mmap_flags(NEW, -1, 0, "--alias qwen"), ["--load-mode", "none"])


class CapsProbeTests(unittest.TestCase):
    """The suite may not launch llama-server, so feed the probe help text."""

    def caps_for(self, help_text):
        from unittest.mock import patch
        import subprocess
        done = subprocess.CompletedProcess(["llama-server", "--help"], 0, stdout=help_text, stderr="")
        bridge._LLAMA_CAPS_CACHE.clear()
        try:
            with patch.object(bridge.subprocess, "run", return_value=done):
                return bridge.llama_caps("fake-llama-server.exe")
        finally:
            bridge._LLAMA_CAPS_CACHE.clear()

    def test_new_build_help_reports_load_mode(self):
        # Excerpt from build 12121's --help.
        caps = self.caps_for("-lm,   --load-mode MODE                 model loading mode (default: auto)\n"
                             "-ngl, --gpu-layers N\n")
        self.assertTrue(caps["load_mode"])
        self.assertFalse(caps["no_mmap"])

    def test_old_build_help_reports_no_mmap(self):
        caps = self.caps_for("--no-mmap                               do not memory-map model\n")
        self.assertFalse(caps["load_mode"])
        self.assertTrue(caps["no_mmap"])


if __name__ == "__main__":
    unittest.main()
