"""Auto-tune for looped models and context-scaled compute buffers.

Seen 2026-11-21 with Inkling 2 Mini (llama.cpp arch inkling2_loop) on a 4090:
the tuner read its per-layer head_count_kv list as 0, priced KV with the
size-bucket fallback (7.9 GB at 128k vs llama.cpp's 2.3 GB), ignored that
layers 6-17 run three times per token, and kept ubatch 512 although this
model's compute buffer grows with context x ubatch (12.9 GB at 128k). A fresh
install got 83k context with 11 expert layers in system RAM and no headroom;
by hand the card holds 256k with 2-4 layers offloaded.
"""
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source

ARCH = "inkling2_loop"
N = 24
# Inkling 2 Mini's per-layer pattern: every 6th layer is full attention.
PATTERN = [(i % 6) != 5 for i in range(N)]          # True = sliding window


def _looped_profile(loop=True, **kw):
    keys = {
        f"{ARCH}.attention.sliding_window": 1024,
        f"{ARCH}.attention.sliding_window_pattern": PATTERN,
        f"{ARCH}.attention.head_count_kv": [8] * N,
        f"{ARCH}.attention.key_length_swa": 128,
        f"{ARCH}.attention.value_length_swa": 128,
    }
    if loop:
        keys.update({f"{ARCH}.loop.count": 3, f"{ARCH}.loop.start": 6, f"{ARCH}.loop.end": 18})
    return {"architecture": ARCH, "block_count": N, "head_count_kv": 8, "key_length": 128,
            "value_length": 128, "keys": keys, **kw}


class LayerPassesTests(unittest.TestCase):
    def test_loop_keys_repeat_the_core(self):
        self.assertEqual(bridge._layer_passes(_looped_profile()), [1] * 6 + [3] * 12 + [1] * 6)
        self.assertEqual(bridge._loop_factor(_looped_profile()), 2.0)

    def test_ordinary_and_broken_metadata_mean_one_pass(self):
        self.assertEqual(bridge._layer_passes(_looped_profile(loop=False)), [1] * N)
        bad = _looped_profile()
        bad["keys"][f"{ARCH}.loop.end"] = 99
        self.assertEqual(bridge._layer_passes(bad), [1] * N)
        self.assertEqual(bridge._layer_passes({"block_count": 0}), [])


class LoopedKvTests(unittest.TestCase):
    def test_kv_counts_every_pass_of_the_full_attention_layers(self):
        ctx = 262144
        per_tok_full = 8 * (128 + 128) * 1.0625        # q8_0, K and V
        flat = bridge._kv_total_bytes(_looped_profile(loop=False), "q8_0", ctx)
        looped = bridge._kv_total_bytes(_looped_profile(), "q8_0", ctx)
        # Full layers 5, 11, 17, 23; 11 and 17 sit in the loop core: 4 -> 8 caches.
        self.assertAlmostEqual(flat, 4 * per_tok_full * ctx + 20 * per_tok_full * 1024, delta=1)
        self.assertAlmostEqual(looped, 8 * per_tok_full * ctx + 40 * per_tok_full * 1024, delta=1)
        # llama.cpp's own estimate for this model: 4,731 MiB at 256k (q8_0).
        self.assertLess(abs(looped / 2**20 - 4731) / 4731, 0.10)


class LoopedDecodeTests(unittest.TestCase):
    def test_offloaded_core_layers_cost_once_per_pass(self):
        prof = {**_looped_profile(), "is_moe": True}
        per_layer = 1e6
        _gpu, cpu_flat = bridge._decode_bytes_per_token(
            {**prof, "keys": {k: v for k, v in prof["keys"].items() if ".loop." not in k}},
            1000.0, kv_dtype="q8_0", v_dtype="", fill=1, n_cpu_moe=8, dense_share=0.03,
            active_expert_bytes=per_layer * N)
        _gpu, cpu_loop = bridge._decode_bytes_per_token(
            prof, 1000.0, kv_dtype="q8_0", v_dtype="", fill=1, n_cpu_moe=8, dense_share=0.03,
            active_expert_bytes=per_layer * N)
        self.assertAlmostEqual(cpu_flat, 8 * per_layer)
        self.assertAlmostEqual(cpu_loop, (6 + 2 * 3) * per_layer)   # layers 0-5 once, 6-7 three times


def _write_gguf(path: Path, kvs: list) -> None:
    """A header-only GGUF v3: (key, type, value) with type 4 = uint32,
    8 = string, 9 = uint32 array."""
    out = bytearray(b"GGUF") + struct.pack("<IQQ", 3, 0, len(kvs))
    for key, typ, val in kvs:
        k = key.encode()
        out += struct.pack("<Q", len(k)) + k + struct.pack("<I", typ)
        if typ == 4:
            out += struct.pack("<I", val)
        elif typ == 8:
            out += struct.pack("<Q", len(val)) + val.encode()
        else:
            out += struct.pack("<IQ", 4, len(val)) + struct.pack(f"<{len(val)}I", *val)
    path.write_bytes(bytes(out))


class PerLayerHeaderTests(unittest.TestCase):
    def test_per_layer_head_counts_are_read_not_zeroed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.gguf"
            _write_gguf(p, [("general.architecture", 8, "toy"), ("toy.block_count", 4, 4),
                            ("toy.attention.head_count", 9, [16, 16, 16, 16]),
                            ("toy.attention.head_count_kv", 9, [2, 4, 2, 4]),
                            ("toy.attention.key_length", 4, 128)])
            g = bridge.read_gguf_metadata(str(p))
        self.assertEqual((g["head_count"], g["head_count_kv"]), (16, 4))

    def test_scalar_head_counts_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "m.gguf"
            _write_gguf(p, [("general.architecture", 8, "toy"), ("toy.block_count", 4, 4),
                            ("toy.attention.head_count", 4, 32),
                            ("toy.attention.head_count_kv", 4, 8)])
            g = bridge.read_gguf_metadata(str(p))
        self.assertEqual((g["head_count"], g["head_count_kv"]), (32, 8))


class ContextScaledComputeTests(unittest.TestCase):
    """The exact fit lowers ubatch only when llama.cpp says compute grows with context."""

    def tune(self, compute_scales_with_ctx: bool):
        calls = []

        def fake_fit(model_path, ctx, *, ngl, n_cpu_moe, kv_k, kv_v, ubatch, batch, flash):
            calls.append(ubatch)
            kv = ctx * 0.018                                   # MiB
            compute = (300 + ctx * ubatch * 0.0002) if compute_scales_with_ctx else (300 + ubatch * 0.8)
            return {"devices": {"CUDA0": 15000 + kv + compute}, "ctx": {"CUDA0": kv},
                    "compute": {"CUDA0": compute}, "host": 0.0}

        profile = {"size_gb": 15.0, "is_moe": False, "block_count": 24, "metadata_source": "gguf",
                   "head_count_kv": 8, "key_length": 128, "value_length": 128,
                   "embedding_length": 2304, "architecture": "toy", "keys": {}}
        settings = {**bridge.DEFAULT_SETTINGS, "min_decode_tok_s": 5}
        gpu = [{"name": "RTX 4090", "total_gb": 24.0, "free_gb": 22.9, "bw_gbps": 1008.0,
                "bw_src": "4090"}]
        with bridge_patch("get_settings", lambda: settings), \
             bridge_patch("_fit_params_measure", fake_fit), \
             bridge_patch("_tune_gpus", lambda vram: gpu), \
             bridge_patch("_running_instance_mem_mb", lambda: (0.0, 0.0)), \
             bridge_patch("find_spec_draft_for", lambda p: {}):
            return bridge.auto_tune("m.gguf", 22.9, profile=profile, proven_cfg={}), calls

    def test_context_scaled_compute_gets_a_smaller_ubatch_and_more_context(self):
        plain, _ = self.tune(False)
        scaled, calls = self.tune(True)
        self.assertLess(int(scaled["n_ubatch"]), 512)
        self.assertGreaterEqual(int(scaled["n_ubatch"]), 64)
        self.assertIn("compute buffer grows with context", scaled["notes"])
        self.assertLess(min(calls), 512)
        # With the smaller ubatch the context reaches what the KV alone allows.
        self.assertGreater(scaled["num_ctx"], 131072)

    def test_ordinary_models_keep_their_ubatch(self):
        plain, calls = self.tune(False)
        self.assertNotIn("compute buffer grows with context", plain["notes"])
        # Never lowered (the existing at-the-ceiling step may still raise it to 2048).
        self.assertFalse(0 < int(plain["n_ubatch"] or 0) < 512)
        self.assertNotIn(64, calls)
        self.assertNotIn(256, calls)


class BootRetuneTests(unittest.TestCase):
    """Seen 2026-11-21 07:55: the boot self-heal re-tuned Inkling while it was
    still loading. The card read as full, the estimate tied the saved 256k, so
    the proven combo was skipped, every expert layer went to RAM at ubatch 512,
    and that was saved and applied. RAM filled; the prompt crawled at 26 tok/s."""

    # Small KV, so the analytic estimate reaches 256k by itself: the tie case.
    PROFILE = {"size_gb": 15.0, "is_moe": False, "block_count": 24, "metadata_source": "gguf",
               "head_count_kv": 1, "key_length": 128, "value_length": 128,
               "embedding_length": 2304, "architecture": "toy", "keys": {}}

    def tune(self, free_gb, proven=None, min_ctx=262144):
        def fake_fit(model_path, ctx, *, ngl, n_cpu_moe, kv_k, kv_v, ubatch, batch, flash):
            kv, compute = ctx * 0.018, 300 + ctx * ubatch * 0.0002
            return {"devices": {"CUDA0": 15000 + kv + compute}, "ctx": {"CUDA0": kv},
                    "compute": {"CUDA0": compute}, "host": 0.0}

        settings = {**bridge.DEFAULT_SETTINGS, "min_decode_tok_s": 5}
        gpu = [{"name": "RTX 4090", "total_gb": 24.0, "free_gb": free_gb, "bw_gbps": 1008.0,
                "bw_src": "4090"}]
        with bridge_patch("get_settings", lambda: settings), \
             bridge_patch("_fit_params_measure", fake_fit), \
             bridge_patch("_tune_gpus", lambda vram: gpu), \
             bridge_patch("_running_instance_mem_mb", lambda: (0.0, 0.0)), \
             bridge_patch("find_spec_draft_for", lambda p: {}):
            return bridge.auto_tune("m.gguf", 24.0, profile=dict(self.PROFILE), min_ctx=min_ctx,
                                    proven_cfg=proven or {})

    def test_a_tied_proven_combo_survives_a_card_that_reads_full(self):
        proven = {"num_ctx": 262144, "proven_ctx": 262144, "num_gpu": 99, "n_ubatch": 64,
                  "num_batch": 2048, "kv_cache_type": "q8_0"}
        r = self.tune(free_gb=0.9, proven=proven)
        self.assertEqual(r["num_ctx"], 262144)
        self.assertEqual(r["num_gpu"], 99)
        self.assertEqual(r["n_ubatch"], 64)
        self.assertFalse(r.get("does_not_fit"))
        self.assertIn("honored proven ctx", r["notes"])

    def test_a_plan_that_cannot_load_is_flagged_not_saved(self):
        r = self.tune(free_gb=0.9, proven={}, min_ctx=262144)
        self.assertTrue(r.get("does_not_fit"))
        saved = []
        with bridge_patch("_models_config", lambda: {}), \
             bridge_patch("save_json", lambda *a: saved.append(a)):
            bridge._save_model_config("m.gguf", r)
        self.assertEqual(saved, [])

    def test_a_free_card_keeps_tuning_normally(self):
        r = self.tune(free_gb=22.9, proven={}, min_ctx=0)
        self.assertFalse(r.get("does_not_fit"))
        self.assertGreaterEqual(r["num_ctx"], 131072)


class BootRetuneEndpointTests(unittest.TestCase):
    def setUp(self):
        from tests._sandbox import sandbox_user_files
        import http.client, json as _json, re, threading
        self.http, self.json = http.client, _json
        sandbox_user_files(self)
        self.saved = []
        for name, value in (("auto_tune", lambda *a, **k: {"num_ctx": 65536, "n_cpu_moe": 9}),
                            ("_save_model_config", lambda mp, flags: self.saved.append(mp)),
                            ("inspect_model", lambda p: {}), ("_tune_budget_gb", lambda v=0: 24.0)):
            p = bridge_patch(name, value)
            p.start()
            self.addCleanup(p.stop)

        class QuietHandler(bridge.Handler):
            def log_message(self, *args):
                pass

        self.server = bridge.AccurettaHTTPServer(("127.0.0.1", 0), QuietHandler)
        t = threading.Thread(target=self.server.serve_forever, daemon=True)
        t.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(t.join, 1)
        self.addCleanup(self.server.shutdown)
        page = self._req("GET", "/")
        self.token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()

    def _req(self, method, path, body=None):
        c = self.http.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if method == "POST":
                headers["X-Accuretta-Token"] = self.token
            c.request(method, path, self.json.dumps(body) if body is not None else None, headers)
            return c.getresponse().read()
        finally:
            c.close()

    def test_background_retune_only_suggests(self):
        self._req("POST", "/api/llama/auto-tune", {"model_path": "m.gguf", "vram_gb": 24,
                                                     "min_ctx": 262144, "background": True})
        self.assertEqual(self.saved, [])
        self._req("POST", "/api/llama/auto-tune", {"model_path": "m.gguf", "vram_gb": 24})
        self.assertEqual(self.saved, ["m.gguf"])                 # the Suggest button still saves


class BootRetuneClientSourceTests(unittest.TestCase):
    def test_boot_retune_waits_and_never_degrades(self):
        src = frontend_source()
        start = src.index("async function autoRetuneOnBoot")
        body = src[start:src.index("function reflectIdeToggles", start)]
        self.assertIn("llama_up", body)                             # waits for the model to load
        self.assertIn("background: true", body)                     # server doesn't save
        self.assertIn("sug.does_not_fit", body)                     # skips plans that can't load
        self.assertIn("Number(sug.n_cpu_moe || 0) > curMoe", body)  # never more experts to RAM
        self.assertLess(body.index("llama_up"), body.index("/api/llama/auto-tune"))


if __name__ == "__main__":
    unittest.main()
