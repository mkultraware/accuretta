"""A model switch that fails must not damage the model you switched away from.

Seen 2026-11-20: switching to a model the installed llama.cpp couldn't load
(Inkling 2 Mini, "tensor 'blk.24.attn_q.weight' not found") left the failed
launch as the watchdog's "last known-good" one. The watchdog crash-looped it,
and its self-heal saved each shrunken setup under the selected model, so Qwen
came back with 12k context instead of 129k, half its layers on the CPU, and
its projector and draft switched off.
"""
import http.client
import json
import re
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _source import app_source
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files

TENSOR_ERROR = ("0.00.410.162 E llama_model_load: error loading model: check_tensor_dims: "
                "tensor 'blk.24.attn_q.weight' not found")


def _sandbox_models_config(testcase):
    store = Path(tempfile.mkdtemp()) / "models_config.json"
    store.write_text("{}", encoding="utf-8")
    store_patch = bridge_patch("MODELS_CONFIG_FILE", store)
    store_patch.start()
    testcase.addCleanup(store_patch.stop)
    return store


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _DeadServer:
    """A llama-server that exits at once, as it does on a load error."""
    pid = 4242
    returncode = 1
    stdout = None

    def poll(self):
        return 1


class FailedLaunchKeepsPreviousTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        _sandbox_models_config(self)
        bridge.save_json(bridge.SETTINGS_FILE, {"spec_strategy": "off", "mmproj_mode": "off",
                                                "num_ctx": 8192})
        folder = Path(tempfile.mkdtemp())
        self.qwen = str(folder / "qwen.gguf")
        self.inkling = str(folder / "inkling.gguf")
        for path in (self.qwen, self.inkling):
            Path(path).write_bytes(b"GGUF")
        self.proc = bridge.LlamaProcess()

    def _launch(self, model_path, log_line=TENSOR_ERROR):
        proc = self.proc
        with bridge_patch("find_llama_bin", return_value="llama-server.exe"), \
             bridge_patch("llama_caps", return_value={}), \
             bridge_patch("inspect_model", return_value={}), \
             bridge_patch("wait_for_llama", return_value=False), \
             patch.object(bridge._child_job, "assign", lambda p: None), \
             patch.object(bridge.subprocess, "Popen", return_value=_DeadServer()), \
             patch.object(proc, "_start_log_reader", lambda p: proc._append_log(log_line)), \
             patch.object(proc, "_ensure_watchdog", lambda: None):
            return proc._start_impl(model_path=model_path, wait=True, wait_seconds=1,
                                    port_override=_free_port())

    def test_failed_switch_hands_the_watchdog_the_previous_model(self):
        self.proc._last_start_args = {"model_path": self.qwen, "wait": False}
        res = self._launch(self.inkling)
        self.assertFalse(res["ok"])
        self.assertEqual(self.proc._last_start_args["model_path"], self.qwen)
        self.assertEqual(res.get("fallback_model"), self.qwen)
        self.assertIn("Going back to qwen", res["error"])

    def test_error_says_what_llama_cpp_refused(self):
        self.proc._last_start_args = {"model_path": self.qwen, "wait": False}
        res = self._launch(self.inkling)
        self.assertIn("tensor 'blk.24.attn_q.weight' not found", res["error"])
        self.assertIn("can't read this model file", res["error"])
        self.assertNotIn("check the model file or VRAM", res["error"])

    def test_unknown_architecture_is_named(self):
        res = self._launch(self.inkling, "llama_model_load: error loading model: "
                                         "unknown model architecture: 'inkling2'")
        self.assertIn("doesn't support the model's architecture (inkling2)", res["error"])

    def test_no_reason_in_the_log_keeps_the_old_hint(self):
        res = self._launch(self.inkling, "srv  load_model: loading model")
        self.assertIn("check the model file or VRAM", res["error"])

    def test_first_start_still_gets_watchdog_retries(self):
        # A boot that hit VRAM held by another app is retried, as before.
        self.proc._last_start_args = None
        res = self._launch(self.qwen)
        self.assertFalse(res["ok"])
        self.assertEqual(self.proc._last_start_args["model_path"], self.qwen)
        self.assertNotIn("fallback_model", res)

    def test_retrying_the_same_model_keeps_its_args(self):
        self.proc._last_start_args = {"model_path": self.qwen, "wait": False}
        res = self._launch(self.qwen)
        self.assertEqual(self.proc._last_start_args["model_path"], self.qwen)
        self.assertNotIn("Going back", res["error"])


class SelfHealOnlyTouchesTheCrashingModelTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        _sandbox_models_config(self)
        self.saved = []
        save_patch = bridge_patch("_save_model_config",
                                  lambda mp, flags: self.saved.append((mp, dict(flags))))
        save_patch.start()
        self.addCleanup(save_patch.stop)
        bridge.save_json(bridge.SETTINGS_FILE, {
            "model_path": "D:/MODELS/qwen.gguf", "num_ctx": 129024, "num_gpu": 99,
            "spec_strategy": "draft-mtp", "spec_draft_model": "D:/MODELS/draft.gguf"})

    def test_another_models_crash_leaves_the_selected_setup_alone(self):
        before = bridge.load_json(bridge.SETTINGS_FILE, {})
        with bridge_patch("broadcast_event"), \
             bridge_patch("inspect_model", return_value={}):
            bridge._watchdog_self_heal(2, "D:/MODELS/inkling.gguf")
            bridge._watchdog_self_heal(3, "D:/MODELS/inkling.gguf")
        self.assertEqual(bridge.load_json(bridge.SETTINGS_FILE, {}), before)
        self.assertEqual(self.saved, [])

    def test_the_selected_models_own_crash_still_heals(self):
        with bridge_patch("broadcast_event"), \
             bridge_patch("inspect_model", return_value={}):
            bridge._watchdog_self_heal(2, "d:/models/QWEN.gguf")   # same file, other case
        self.assertEqual(bridge.get_settings()["num_ctx"], 64512)
        self.assertTrue(self.saved and self.saved[0][0] == "D:/MODELS/qwen.gguf")

    def test_the_watchdog_passes_the_model_it_restarts(self):
        source = app_source()
        self.assertIn('_watchdog_self_heal(attempt, str(args.get("model_path") or ""))', source)


class FailedSwitchRestoresSettingsTests(unittest.TestCase):
    """The model-switch endpoint gives the previous model its setup back."""

    QWEN_SETUP = {"num_ctx": 129024, "n_cpu_moe": 0, "num_gpu": 99, "n_ubatch": 0,
                  "spec_strategy": "draft-mtp", "spec_strategy_source": "manual",
                  "mmproj_mode": "manual", "mmproj_auto": False,
                  "mmproj_path": "D:/MODELS/mmproj-F16.gguf",
                  "spec_draft_model": "D:/MODELS/Qwen-DFlash2.gguf"}

    def setUp(self):
        sandbox_user_files(self)
        _sandbox_models_config(self)
        folder = Path(tempfile.mkdtemp())
        self.qwen = str(folder / "qwen.gguf")
        self.inkling = str(folder / "inkling.gguf")
        for path in (self.qwen, self.inkling):
            Path(path).write_bytes(b"GGUF")
        bridge.save_json(bridge.SETTINGS_FILE, {"model_path": self.qwen, "model": "qwen",
                                                **self.QWEN_SETUP})

        def fake_tune(model_path):
            # What the tuner wrote for Inkling on the owner's 4090.
            s = bridge.get_settings()
            s.update({"num_ctx": 49152, "n_cpu_moe": 31, "num_gpu": 99, "n_ubatch": 512,
                      "spec_strategy": "draft-mtp", "spec_strategy_source": "auto"})
            bridge.save_json(bridge.SETTINGS_FILE, s)
            return True

        class FakeLlama:
            running = False

            def loaded_model(self):
                return ""

            def is_running(self):
                return self.running

            def start(self, target, wait_seconds=120):
                return {"ok": False, "error": "llama-server exited (code 1)"}

        self.llama = FakeLlama()
        for name, value in (("_auto_tune_for_load", fake_tune), ("_llama", self.llama)):
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
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/")
            page = connection.getresponse().read()
        finally:
            connection.close()
        self.token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()

    def _switch(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("POST", "/api/models/load", json.dumps({"path": self.inkling}),
                               {"X-Accuretta-Token": self.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_previous_model_gets_its_setup_back(self):
        status, _ = self._switch()
        self.assertEqual(status, 500)
        after = bridge.get_settings()
        self.assertEqual(after["model_path"], self.qwen)
        for key, value in self.QWEN_SETUP.items():
            self.assertEqual(after[key], value, key)

    def test_a_model_still_loading_is_left_alone(self):
        # Timed out but alive: the target may still come up with its own tune.
        self.llama.running = True
        self._switch()
        after = bridge.get_settings()
        self.assertEqual(after["num_ctx"], 49152)
        self.assertEqual(after["n_cpu_moe"], 31)


if __name__ == "__main__":
    unittest.main()
