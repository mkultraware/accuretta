"""Inkling 2 through the maya engine when it is installed.

2026-11-21: Project Maya's maya-ik2 (github.com/alesha-pro/maya-inkling-mini)
ran Inkling 2 Mini on the owner's RTX 4090 at 150 tok/s writing and 4,000 tok/s
reading, where llama.cpp managed 56 and 400; with Thinking Machines' drafter it
edited at 290 tok/s. Its server speaks llama-server's HTTP API, so the bridge
only has to launch it: found automatically (no setting), used for Unsloth's
looped Inkling 2 layout that only it runs right, tuned by its own memory math
(every expert resident, the drafter only when 128k still fits beside it), and
stopped as a process tree. Calibration from that night, 22.5 GiB free: 262k
plain and 192k with the drafter ran; 262k with the drafter overflowed and
Windows paged it to RAM.
"""
import http.client
import json
import os
import re
import struct
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import bridge
from _source import app_source, frontend_source
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files

GIB = 1024 ** 3
REAL_SIZES = {"model": 17_629_759_456, "drafter": 635_801_416}   # Unsloth UD-Q4_K_XL, mtp.safetensors


def _kv(key, typ, payload: bytes) -> bytes:
    return struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", typ) + payload


def _str(v: str) -> bytes:
    return struct.pack("<Q", len(v)) + v.encode()


def _gguf(path: Path, arch="inkling2", loops=3) -> str:
    """A header with the keys maya's routing and memory math read."""
    pattern = ([True] * 5 + [False]) * 8                         # 8 of 48 visits see everything
    kvs = [_kv("general.architecture", 8, _str(arch)),
           _kv(f"{arch}.block_count", 4, struct.pack("<I", 48)),
           _kv(f"{arch}.context_length", 4, struct.pack("<I", 1_048_576)),
           _kv(f"{arch}.embedding_length", 4, struct.pack("<I", 2304)),
           _kv(f"{arch}.attention.head_count_kv", 9, struct.pack("<IQ", 4, 48) + struct.pack("<48I", *[8] * 48)),
           _kv(f"{arch}.attention.key_length", 4, struct.pack("<I", 128)),
           _kv(f"{arch}.attention.value_length", 4, struct.pack("<I", 128)),
           _kv(f"{arch}.vocab_size", 4, struct.pack("<I", 201024)),
           _kv(f"{arch}.attention.sliding_window_pattern", 9,
               struct.pack("<IQ", 7, 48) + struct.pack("<48?", *pattern))]
    if loops:
        kvs.append(_kv(f"{arch}.num_loops", 4, struct.pack("<I", loops)))
    path.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, len(kvs)) + b"".join(kvs))
    return str(path)


def _checkout(root: Path, vision=True) -> Path:
    exe = "maya-ik2.exe" if os.name == "nt" else "maya-ik2"
    py = (".venv", "Scripts", "python.exe") if os.name == "nt" else (".venv", "bin", "python")
    for rel in [("serve", "server.py"), ("build", exe), py] + (
            [("build-vision", "bin", "maya-vision.exe" if os.name == "nt" else "maya-vision")] if vision else []):
        f = root.joinpath(*rel)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return root


class _Base(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        bridge._MAYA_META_CACHE.clear()
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.models = self.tmp / "models"
        self.models.mkdir()
        self.engine_root = _checkout(self.tmp / "maya-inkling-mini")
        self.model = _gguf(self.models / "Inkling-2-Mini-UD-Q4_K_XL.gguf")
        self.drafter = str(self.models / "mtp.safetensors")
        Path(self.drafter).write_bytes(b"x")
        for patcher in (patch.object(bridge.Path, "home", lambda: self.home),
                        bridge_patch("DATA", self.data),
                        patch.dict(os.environ, {"ACCURETTA_MAYA_DIR": str(self.engine_root)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def real_sizes(self):
        """The real files' sizes, without writing 18 GB."""
        real = os.path.getsize

        def size(p):
            p = str(p)
            if p == self.model:
                return REAL_SIZES["model"]
            if p == self.drafter:
                return REAL_SIZES["drafter"]
            return real(p)
        return patch.object(bridge.os.path, "getsize", side_effect=size)


class FindAndRouteTests(_Base):
    def test_finds_a_built_checkout(self):
        eng = bridge.find_maya_engine()
        self.assertEqual(Path(eng["root"]), self.engine_root)
        self.assertTrue(eng["vision"].endswith(("maya-vision.exe", "maya-vision")))

    def test_found_in_the_home_folder_without_the_variable(self):
        with patch.dict(os.environ, {"ACCURETTA_MAYA_DIR": ""}):
            self.assertEqual(bridge.find_maya_engine(), {})
            _checkout(self.home / "maya-inkling-mini", vision=False)
            eng = bridge.find_maya_engine()
            self.assertEqual(Path(eng["root"]), self.home / "maya-inkling-mini")
            self.assertEqual(eng["vision"], "")

    def test_an_unbuilt_checkout_is_not_an_engine(self):
        exe = "maya-ik2.exe" if os.name == "nt" else "maya-ik2"
        (self.engine_root / "build" / exe).unlink()
        self.assertEqual(bridge.find_maya_engine(), {})

    def test_only_unslothes_looped_layout_goes_to_maya(self):
        self.assertTrue(bridge._maya_engine_for(self.model))
        unlooped = _gguf(self.models / "Inkling-2-Small.gguf", loops=0)
        mainline = _gguf(self.models / "Inkling-2-Mini-Q8_0.gguf", arch="inkling2_loop")
        qwen = _gguf(self.models / "Qwen.gguf", arch="qwen35", loops=0)
        for other in (unlooped, mainline, qwen):
            self.assertEqual(bridge._maya_engine_for(other), {}, other)

    def test_without_an_engine_everything_stays_on_llama_cpp(self):
        with patch.dict(os.environ, {"ACCURETTA_MAYA_DIR": str(self.tmp / "nowhere")}):
            self.assertEqual(bridge._maya_engine_for(self.model), {})


class MemoryMathTests(_Base):
    def test_kv_and_weights_match_what_the_engine_reports(self):
        meta = bridge._maya_gguf_meta(self.model)
        self.assertEqual(bridge._maya_kv_bytes_per_token(meta), 16 * 1024)
        with self.real_sizes():
            self.assertAlmostEqual(bridge._maya_weights_gib(self.model, meta), 15.96, places=2)

    def test_fit_matches_the_4090_runs(self):
        with self.real_sizes():
            fit = bridge.maya_fit(self.model, 22.5, self.drafter)
        self.assertEqual(fit["plain_ctx"], 286_720)        # 262,144 ran with 0.3 GiB to spare
        self.assertEqual(fit["draft_ctx"], 204_800)        # 196,608 ran; 262,144 spilled
        self.assertLess(fit["draft_ctx"], 262_144)
        with self.real_sizes():
            small = bridge.maya_fit(self.model, 15.0, self.drafter)
        self.assertEqual((small["plain_ctx"], small["draft_ctx"]), (0, 0))

    def test_gpu_image_encoder_costs_context(self):
        with self.real_sizes():
            cpu = bridge.maya_fit(self.model, 22.5)["plain_ctx"]
            gpu = bridge.maya_fit(self.model, 22.5, vision_gpu=True)["plain_ctx"]
        self.assertLess(gpu, cpu)


class TuneTests(_Base):
    def tune(self, vram=22.5, min_ctx=0, tier=None, total=24.0):
        """auto_tune as the app calls it: the measured free VRAM is `vram`; with
        a tier set, callers pass the tier (the boot retune always does)."""
        if tier is not None:
            s = bridge.load_json(bridge.SETTINGS_FILE, {})
            s["vram_tier_gb"] = tier
            bridge.save_json(bridge.SETTINGS_FILE, s)
        with self.real_sizes(), \
                bridge_patch("_free_vram_gb_for_tune", lambda: vram), \
                bridge_patch("detect_vram_gb", lambda: {"total_gb": total, "free_total_gb": vram}):
            return bridge.auto_tune(self.model, vram if tier is None else tier, min_ctx=min_ctx)

    def test_the_vram_tier_is_not_free_memory(self):
        # The boot retune passes the 24 GB tier. Taken as free memory it gave
        # 368,640 context, the experts didn't fit and maya crashed (2026-11-21).
        out = self.tune(vram=21.9, tier=24)
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (163_840, "dspark"))
        os.remove(self.drafter)
        self.assertEqual(self.tune(vram=21.9, tier=24)["num_ctx"], 245_760)

    def test_a_smaller_tier_caps_the_card(self):
        os.remove(self.drafter)
        capped = self.tune(vram=21.9, tier=20, total=24.0)["num_ctx"]   # 4 GB of the card held back
        self.assertLess(capped, self.tune(vram=21.9, tier=24)["num_ctx"])

    def test_a_running_maya_model_keeps_its_setup(self):
        bridge.save_json(bridge.MODELS_CONFIG_FILE, {self.model: {"num_ctx": 188_416, "spec_strategy": "dspark"}})
        running = bridge.LlamaProcess()

        class Alive:
            def poll(self):
                return None
        running._proc, running._engine, running._loaded_model = Alive(), "maya", self.model
        running._maya_launch = {"ctx": 188_416, "drafter": self.drafter, "vision_gpu": False}
        with bridge_patch("_llama", running):
            out = self.tune(vram=0.1)          # nothing free beside it: no shrink, no reload
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (188_416, "dspark"))

    def test_drafter_on_when_128k_fits_beside_it(self):
        out = self.tune()
        self.assertEqual(out["engine"], "maya")
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (204_800, "dspark"))
        self.assertEqual(out["n_cpu_moe"], 0)

    def test_a_larger_saved_context_wins_over_the_drafter(self):
        out = self.tune(min_ctx=262_144)
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (286_720, "off"))

    def test_without_a_drafter_the_whole_room_is_context(self):
        os.remove(self.drafter)
        out = self.tune()
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (286_720, "off"))

    def test_a_manual_off_for_this_model_is_kept(self):
        bridge.save_json(bridge.MODELS_CONFIG_FILE, {self.model: {"spec_strategy": "off",
                                                                  "spec_strategy_source": "manual"}})
        out = self.tune()
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (286_720, "off"))

    def test_a_card_too_small_for_every_expert_gets_a_modest_context(self):
        out = self.tune(vram=12.0)
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (32768, "off"))
        self.assertIn("RAM", out["notes"])


class LaunchTests(_Base):
    def launch(self, settings, free=22.5):
        bridge.save_json(bridge.SETTINGS_FILE, settings)
        captured = {}
        proc = bridge.LlamaProcess()

        def spawn(cmd, **kw):
            captured.update(kw, cmd=cmd)
            return {"ok": True}

        tok = self.tmp / "tok"
        with self.real_sizes(), \
                patch.object(proc, "_spawn_and_track", side_effect=spawn), \
                bridge_patch("_maya_tokenizer_dir", lambda eng, m, log=None: str(tok)), \
                patch.object(proc, "_maya_free_after_stop", lambda stopped: free), \
                bridge_patch("_maya_cuda_dirs", lambda eng: ["X:/cuda/bin/x64"]), \
                bridge_patch("find_llama_bin", lambda: ""):
            res = proc._start_impl(self.model, wait=False, port_override=59871)
        self.assertTrue(res["ok"], res)
        cfg_path = captured["cmd"][captured["cmd"].index("--config") + 1]
        captured["cfg"] = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        return captured

    def test_needs_no_llama_cpp_and_builds_maya_config(self):
        mmproj = self.models / "Inkling-2-Mini-mmproj-F16.gguf"
        mmproj.write_bytes(b"x")
        got = self.launch({"num_ctx": 188_416, "spec_strategy": "dspark", "mmproj_mode": "manual",
                           "mmproj_path": str(mmproj)})          # the tuner's pick for 22.5 GiB
        cfg = got["cfg"]
        self.assertEqual(got["engine"], "maya")
        self.assertEqual(Path(got["cwd"]), self.engine_root)
        self.assertEqual(got["cmd"][2:4], ["--engine", "maya"])
        self.assertIn("--port", got["cmd"])
        self.assertEqual(cfg["args"][:4], ["--model", self.model, "--max-context", "188416"])
        self.assertEqual(cfg["args"][4:], ["--draft", self.drafter])
        self.assertEqual(cfg["env"]["IK2_KV"], "fp8")
        self.assertEqual(cfg["env"]["IK2_DRAFT_CTX"], "65536")
        self.assertEqual(cfg["vision"]["mmproj"], str(mmproj))
        self.assertFalse(cfg["vision"]["gpu"])                   # projector on the CPU by default
        self.assertTrue(cfg["vision"]["resident"])
        self.assertEqual(cfg["vision"]["threads"], os.cpu_count() or 8)
        self.assertTrue(got["env"]["PATH"].startswith("X:/cuda/bin/x64" + os.pathsep))
        self.assertEqual(got["env"]["PYTHONUTF8"], "1")

    def test_drafter_left_out_when_the_context_needs_its_room(self):
        got = self.launch({"num_ctx": 262_144, "spec_strategy": "dspark"})
        self.assertNotIn("--draft", got["cfg"]["args"])
        self.assertNotIn("IK2_DRAFT_CTX", got["cfg"]["env"])
        self.assertTrue(any("drafter left out" in n for n in got["notes"]))

    def test_speculative_off_means_no_drafter(self):
        got = self.launch({"num_ctx": 131_072, "spec_strategy": "off"})
        self.assertNotIn("--draft", got["cfg"]["args"])

    def test_projector_off_means_no_images(self):
        got = self.launch({"num_ctx": 131_072, "spec_strategy": "off", "mmproj_mode": "off"})
        self.assertNotIn("vision", got["cfg"])

    def test_context_is_capped_at_the_trained_size(self):
        got = self.launch({"num_ctx": 4_000_000, "spec_strategy": "off"}, free=0)
        self.assertEqual(got["cfg"]["args"][3], "1048576")

    def test_a_context_too_big_for_the_card_starts_at_what_fits(self):
        # 368,640 crashed maya's engine (it didn't fit); start at the fit instead.
        got = self.launch({"num_ctx": 368_640, "spec_strategy": "dspark"})
        self.assertEqual(got["cfg"]["args"][3], "286720")
        self.assertNotIn("--draft", got["cfg"]["args"])
        self.assertEqual(bridge.load_json(bridge.SETTINGS_FILE, {})["num_ctx"], 368_640)   # settings untouched
        self.assertTrue(any("starting at 286,720" in n for n in got["notes"]))

    def test_without_a_reading_the_settings_are_used_as_they_are(self):
        got = self.launch({"num_ctx": 368_640, "spec_strategy": "off"}, free=0)
        self.assertEqual(got["cfg"]["args"][3], "368640")

    def test_a_sliver_over_trims_the_context_to_keep_the_helper(self):
        # The fit beside the helper is 204,800 at 22.5 GB; 212,992 is one step over.
        got = self.launch({"num_ctx": 212_992, "spec_strategy": "dspark"})
        self.assertEqual(got["cfg"]["args"][3], "204800")
        self.assertIn("--draft", got["cfg"]["args"])
        self.assertTrue(any("trimmed" in n for n in got["notes"]))


class WaitTests(unittest.TestCase):
    def test_a_server_that_died_ends_the_wait_at_once(self):
        class Dead:
            def poll(self):
                return 1
        import time
        t = time.time()
        with bridge_patch("llama_ready", return_value=False):
            self.assertFalse(bridge.wait_for_llama(60, base_url="http://127.0.0.1:1", proc=Dead()))
        self.assertLess(time.time() - t, 5)


class ProcessTests(unittest.TestCase):
    def test_stop_ends_the_whole_tree(self):
        class FakeProc:
            pid = 4242
            _accuretta_tree = True

            def poll(self):
                return None

        proc = bridge.LlamaProcess()
        proc._proc, proc._engine = FakeProc(), "maya"
        self.assertEqual(proc.engine(), "maya")
        self.assertFalse(proc.continues_replies())
        with bridge_patch("_terminate_process_tree", return_value=True) as kill:
            proc.stop()
        kill.assert_called_once()
        self.assertEqual(proc.engine(), "")
        self.assertTrue(proc.continues_replies())

    @unittest.skipUnless(os.name == "nt", "Windows' GPU counter")
    def test_retune_counts_the_engine_childs_vram(self):
        class FakeProc:
            pid = 4242
            _accuretta_tree = True

            def poll(self):
                return None

        fake = bridge.LlamaProcess()
        fake._proc = FakeProc()
        with bridge_patch("_llama", fake), \
                bridge_patch("_process_children", return_value=[5151, 6161]), \
                patch.object(bridge.shutil, "which", return_value=None), \
                bridge_patch("_win_gpu_tree_mem_mb", return_value=21_900.0) as tree:
            _ram, vram = bridge._running_instance_mem_mb()
        self.assertEqual(vram, 21_900.0)
        self.assertEqual(sorted(tree.call_args[0][0]), [4242, 5151, 6161])

    @unittest.skipUnless(os.name == "nt", "working sets are Windows'")
    def test_only_the_engine_is_trimmed(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "x = bytearray(64 << 20); import time; time.sleep(20)"])
        try:
            import time
            time.sleep(1.0)
            self.assertEqual(bridge._trim_maya_engine_memory(os.getpid()), 0.0)   # a python child is left alone
        finally:
            child.kill()
            child.wait()

    def test_children_of_a_real_process(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        try:
            self.assertIn(child.pid, bridge._process_children(os.getpid()))
        finally:
            child.kill()
            child.wait()


class SamplingTypeTests(unittest.TestCase):
    def test_top_k_goes_out_as_a_whole_number(self):
        # maya's server refused top_k 40.0 with HTTP 400 on every turn (2026-11-21).
        opt = bridge.llama_options({"top_k": 40, "min_p": 0.05, "repeat_penalty": 1.1})
        self.assertIs(type(opt["top_k"]), int)
        self.assertEqual(opt["top_k"], 40)
        self.assertEqual(bridge.llama_options({"top_k": "20"})["top_k"], 20)
        self.assertIs(type(opt["min_p"]), float)


class CarrySourceTests(unittest.TestCase):
    def test_cut_off_calls_are_not_continued_on_maya(self):
        src = app_source()
        self.assertIn("_can_continue = _llama.continues_replies()", src)
        self.assertIn("and _can_continue", src)


class EndpointTests(_Base):
    def setUp(self):
        super().setUp()
        self.calls = []
        bridge.save_json(bridge.SETTINGS_FILE, {"models_dir": str(self.models), "num_ctx": 32768})

        class FakeLlama:
            def loaded_model(self):
                return ""

            def is_running(self):
                return False

            def is_vision_capable(self):
                return False

            def loaded_mmproj(self):
                return ""

            def start(inner, target, wait_seconds=120):
                self.calls.append(("start", target))
                return {"ok": False, "error": "stub"}

        for name, value in (("_llama", FakeLlama()),
                            ("llama_knows_arch", lambda arch, bin_path="": False),   # an old llama.cpp
                            ("_auto_tune_for_load", lambda p: self.calls.append(("tune", p))),
                            ("llama_ping", lambda timeout=0.5: False)):
            patcher = bridge_patch(name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        class QuietHandler(bridge.Handler):
            def log_message(self, *args):
                pass

        self.server = bridge.AccurettaHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(self.server.shutdown)
        page = self._request("GET", "/")[1]
        self.token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()

    def _request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            headers = {"Content-Type": "application/json"}
            if method == "POST":
                headers["X-Accuretta-Token"] = self.token
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_model_list_marks_maya_models_runnable(self):
        status, raw = self._request("GET", "/api/models")
        self.assertEqual(status, 200)
        entry = {m["name"]: m for m in json.loads(raw)["models"]}["Inkling-2-Mini-UD-Q4_K_XL.gguf"]
        self.assertTrue(entry["llama_supported"])
        self.assertEqual(entry["engine"], "maya")

    def test_load_is_not_refused_for_an_old_llama_cpp(self):
        self._request("POST", "/api/models/load", {"path": self.model})
        self.assertIn(("start", self.model), self.calls)

    def test_without_maya_the_menu_and_load_say_it_is_needed(self):
        with patch.dict(os.environ, {"ACCURETTA_MAYA_DIR": str(self.tmp / "nowhere")}):
            status, raw = self._request("GET", "/api/models")
            entry = {m["name"]: m for m in json.loads(raw)["models"]}["Inkling-2-Mini-UD-Q4_K_XL.gguf"]
            self.assertFalse(entry["llama_supported"])
            self.assertTrue(entry["needs_maya"])
            status, raw = self._request("POST", "/api/models/load", {"path": self.model})
        res = json.loads(raw)
        self.assertEqual(status, 409)
        self.assertTrue(res["needs_maya"])
        self.assertIn("Maya engine", res["error"])
        self.assertEqual(self.calls, [])                       # no tune, no stop, no spawn

    def test_draft_picker_shows_mayas_helper(self):
        status, raw = self._request("GET", "/api/models/spec-drafts?model_path="
                                    + urllib.parse.quote(self.model))
        data = json.loads(raw)
        self.assertEqual(data["engine"], "maya")
        self.assertEqual(data["paired"], self.drafter)
        self.assertTrue(data["paired_match"]["matched"])
        self.assertEqual([d["path"] for d in data["drafts"]], [self.drafter])


class NeedsMayaTests(_Base):
    def test_start_refuses_instead_of_feeding_llama_cpp(self):
        with patch.dict(os.environ, {"ACCURETTA_MAYA_DIR": str(self.tmp / "nowhere")}), \
                bridge_patch("find_llama_bin", lambda: "C:/llama/llama-server.exe"):
            res = bridge.LlamaProcess()._start_impl(self.model, wait=False, port_override=59871)
        self.assertFalse(res["ok"])
        self.assertTrue(res["needs_maya"])

    def test_menu_text_names_the_maya_engine(self):
        src = frontend_source()
        self.assertIn("needs the Maya engine", src)                  # settings dropdown
        self.assertIn("entry.needs_maya", src)                       # loadModelByPath guard
        self.assertIn("Needs the Maya engine", src)                  # model picker badge


class HelperSwitchTests(_Base):
    """Switching the helper while maya runs resizes the context (no Windows counters)."""

    def running(self, ctx, drafter=""):
        proc = bridge.LlamaProcess()

        class Alive:
            pid = 1

            def poll(self):
                return None
        proc._proc, proc._engine, proc._loaded_model = Alive(), "maya", self.model
        proc._maya_launch = {"ctx": ctx, "drafter": drafter, "vision_gpu": False}
        return proc

    def tune_running(self, proc, free_now, settings=None, saved=None):
        bridge.save_json(bridge.SETTINGS_FILE, {"model_path": self.model, **(settings or {})})
        bridge.save_json(bridge.MODELS_CONFIG_FILE, {self.model: saved or {}})
        with self.real_sizes(), bridge_patch("_llama", proc), \
                bridge_patch("detect_vram_gb", lambda: {"total_gb": 24.0, "free_total_gb": free_now}), \
                bridge_patch("_free_vram_gb_for_tune", lambda: 0.4):     # a bad add-back: unused
            return bridge._maya_auto_tune(self.model, 24)

    def test_a_running_setup_grows_when_room_appears(self):
        # Other apps closed: 1.5 GB free beside a 245,760 plain setup.
        out = self.tune_running(self.running(245_760), 1.5,
                                settings={"spec_draft_mode": "off"}, saved={"num_ctx": 245_760})
        self.assertGreater(out["num_ctx"], 245_760)

    def test_a_running_setup_never_shrinks(self):
        out = self.tune_running(self.running(262_144), 0.0,
                                settings={"spec_draft_mode": "off"}, saved={"num_ctx": 262_144})
        self.assertEqual(out["num_ctx"], 262_144)

    def test_helper_chosen_while_running_gets_its_context(self):
        # 262,144 without the helper, 0.3 GB free: the helper fits beside 180,224.
        out = self.tune_running(self.running(262_144), 0.3,
                                saved={"num_ctx": 262_144, "spec_strategy": "dspark",
                                       "spec_strategy_source": "manual"})
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (180_224, "dspark"))

    def test_unloading_the_helper_while_running_regrows_the_context(self):
        out = self.tune_running(self.running(180_224, self.drafter), 0.3,
                                settings={"spec_draft_mode": "off"},
                                saved={"num_ctx": 180_224, "spec_strategy": "dspark"})
        self.assertEqual((out["num_ctx"], out["spec_strategy"]), (262_144, "off"))

    def test_settings_accept_dspark_for_a_maya_model(self):
        # Saving dspark was refused: the check wanted a draft GGUF beside the model.
        bridge.save_json(bridge.SETTINGS_FILE, {"model_path": self.model})
        cur = bridge.update_settings({"spec_strategy": "dspark", "spec_strategy_source": "manual",
                                      "spec_draft_mode": "auto"})
        self.assertEqual(cur["spec_strategy"], "dspark")
        cur = bridge.update_settings({"spec_strategy": "dspark", "spec_draft_model": self.drafter})
        self.assertEqual(cur["spec_draft_model"], self.drafter)
        os.remove(self.drafter)
        with self.assertRaises(ValueError) as e:
            bridge.update_settings({"spec_strategy": "dspark", "spec_draft_model": ""})
        self.assertIn("Maya helper", str(e.exception))

    def test_settings_save_sizes_the_context_for_the_helper(self):
        proc = self.running(262_144)
        bridge.save_json(bridge.SETTINGS_FILE, {"model_path": self.model, "num_ctx": 262_144,
                                                "spec_strategy": "dspark", "spec_draft_mode": "auto"})
        bridge.save_json(bridge.MODELS_CONFIG_FILE, {self.model: {
            "num_ctx": 262_144, "spec_strategy": "dspark", "spec_strategy_source": "manual"}})
        with self.real_sizes(), bridge_patch("_llama", proc), \
                bridge_patch("detect_vram_gb", lambda: {"total_gb": 24.0, "free_total_gb": 0.3}):
            self.assertTrue(bridge._maya_helper_followup({"spec_strategy": "dspark"}))
            self.assertFalse(bridge._maya_helper_followup({"spec_strategy": "dspark", "num_ctx": 262_144}))
        self.assertEqual(bridge.load_json(bridge.SETTINGS_FILE, {})["num_ctx"], 180_224)
        self.assertEqual(bridge._models_config()[self.model]["num_ctx"], 180_224)


class SessionReviewFixesTests(unittest.TestCase):
    """Three bugs from the owner's first long Inkling session (2026-11-21)."""

    def test_inkling_gets_its_own_sampling(self):
        # Under the generic agentic clamp (0.4, top_k 40, min_p 0.05) its
        # reasoning went in circles on tool turns.
        for name in ("Inkling-2-Mini-UD-Q4_K_XL.gguf", "inkling-2-mini-Q8_0.gguf"):
            sampling, features = bridge._model_preset(rf"D:\MODELS\{name}")
            self.assertEqual(sampling["temperature"], 1.0, name)
            self.assertEqual(sampling["top_k"], 0, name)          # off: maya reads 0 as the widest list
            self.assertEqual(sampling["min_p"], 0.0, name)
            self.assertIn("vendor_agentic_sampling", features, name)

    def test_mayas_loop_stop_is_asked_again_without_thinking(self):
        """maya's loop guard ended a reply with only its note, and that note
        was the whole answer the user got."""
        from test_goal_rounds_and_folds import CTX
        import copy
        from contextlib import ExitStack
        chat_id = "maya-loop-stop"
        requests = []
        replies = [
            {"reasoning_content": "Let me check. " * 40, "content": bridge._MAYA_LOOP_STOP_NOTE},
            {"content": "Hello! How can I help?"},
        ]

        class Response:
            def __init__(self, delta):
                self.delta = delta

            def __iter__(self):
                yield ("data: " + json.dumps({"choices": [{"delta": self.delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            return Response(replies[min(len(requests), len(replies)) - 1])

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": CTX,
                    "summarize_history": False, "red_team_enabled": False}
        chats = {"chats": {}, "order": []}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda *a, **kw: None,
            "_rt_trailing_system_ok": lambda: True,
            "_llama_props_ctx": lambda: CTX, "_conversation_token_scale": lambda *a, **kw: 1,
            "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn",
            "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: chats,
            "_mid_turn_fold": lambda *a, **kw: None,
            "_maybe_roll_summary": lambda *a, **kw: False,
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {chat_id: 0}))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            final = bridge.run_chat_turn(chat_id, [{"role": "user", "content": "hello"}], True,
                                         lambda e: None)
        self.assertEqual(len(requests), 2)                    # asked again
        self.assertIn("Hello! How can I help?", final["content"])
        self.assertNotIn(bridge._MAYA_LOOP_STOP_NOTE, final["content"])

    def test_screenshots_are_described_not_rebuilt(self):
        # "for someone recreating its design" got the page's HTML back
        prompt = bridge._page_screenshot_prompt("the login form")
        self.assertIn("Describe only", prompt)
        self.assertIn("Do not write code", prompt)
        self.assertIn("Pay special attention to: the login form.", prompt)
        self.assertNotIn("recreating", prompt)
        self.assertNotIn("Pay special attention", bridge._page_screenshot_prompt(""))


if __name__ == "__main__":
    unittest.main()
