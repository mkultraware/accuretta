"""Warn when the installed llama.cpp is too old for a model's architecture.

Seen 2026-11-20/21: a model whose architecture the installed llama.cpp didn't
know (Inkling 2 Mini, "inkling2_loop") could only fail to load, and the
attempt first stopped the running model. llama.cpp keeps every architecture
name it supports as a C string in its library, so the bridge can tell before
trying: the model menu marks the file and the load call refuses with a reason.
"""
import http.client
import json
import re
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files
from _source import frontend_source


def _fake_build(folder: Path, archs, lib_name="llama.dll") -> str:
    """A llama.cpp folder whose library carries these architecture names."""
    table = b"\x00".join(a.encode() for a in archs)
    (folder / lib_name).write_bytes(b"MZ\x90\x00junk\x00" + table + b"\x00more junk")
    server = folder / "llama-server.exe"
    server.write_bytes(b"MZ\x90\x00server")
    return str(server)


def _gguf(path: Path, arch: str) -> str:
    kvs = [("general.architecture", arch), ("general.name", "toy")]
    out = bytearray(b"GGUF") + struct.pack("<IQQ", 3, 0, len(kvs))
    for key, val in kvs:
        out += struct.pack("<Q", len(key)) + key.encode() + struct.pack("<I", 8)
        out += struct.pack("<Q", len(val)) + val.encode()
    path.write_bytes(bytes(out))
    return str(path)


class ArchTableTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        bridge._LLAMA_ARCH_TABLE.clear()
        self.addCleanup(bridge._LLAMA_ARCH_TABLE.clear)

    def test_known_and_unknown_architectures(self):
        server = _fake_build(self.dir, ["llama", "qwen35", "inkling2"])
        self.assertTrue(bridge.llama_knows_arch("qwen35", server))
        self.assertTrue(bridge.llama_knows_arch("inkling2", server))
        # A longer name sharing a prefix is not a match.
        self.assertFalse(bridge.llama_knows_arch("inkling2_loop", server))

    def test_linux_and_mac_libraries(self):
        for lib in ("libllama.so", "libllama.dylib"):
            folder = self.dir / lib
            folder.mkdir()
            server = _fake_build(folder, ["llama", "gemma4"], lib_name=lib)
            self.assertTrue(bridge.llama_knows_arch("gemma4", server))
            self.assertFalse(bridge.llama_knows_arch("inkling2_loop", server))

    def test_unknown_when_it_cannot_be_told(self):
        server = _fake_build(self.dir, ["qwen35"])            # no "llama": not the name table
        self.assertIsNone(bridge.llama_knows_arch("inkling2_loop", server))
        self.assertIsNone(bridge.llama_knows_arch("qwen35", str(self.dir / "nope" / "llama-server.exe")))
        self.assertIsNone(bridge.llama_knows_arch("", server))
        self.assertIsNone(bridge.llama_knows_arch("bad name!", server))

    def test_a_new_build_is_reread(self):
        server = _fake_build(self.dir, ["llama"])
        self.assertFalse(bridge.llama_knows_arch("inkling2_loop", server))
        _fake_build(self.dir, ["llama", "inkling2_loop", "x"])   # different size → new key
        self.assertTrue(bridge.llama_knows_arch("inkling2_loop", server))


class TooOldEndpointTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        bridge._LLAMA_ARCH_TABLE.clear()
        self.addCleanup(bridge._LLAMA_ARCH_TABLE.clear)
        root = Path(tempfile.mkdtemp())
        (root / "build").mkdir()
        (root / "models").mkdir()
        self.server_bin = _fake_build(root / "build", ["llama", "qwen35"])
        self.old_model = _gguf(root / "models" / "Qwen-Toy-Q4_K_M.gguf", "qwen35")
        self.new_model = _gguf(root / "models" / "Brand-New-Q8_0.gguf", "inkling2_loop")
        bridge.save_json(bridge.SETTINGS_FILE, {"models_dir": str(root / "models"),
                                                "model_path": self.old_model, "num_ctx": 32768})
        self.calls = []

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
                return {"ok": False, "error": "should not be reached"}

        for name, value in (("_llama", FakeLlama()), ("find_llama_bin", lambda: self.server_bin),
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

    def test_model_list_marks_models_the_build_cannot_run(self):
        status, raw = self._request("GET", "/api/models")
        self.assertEqual(status, 200)
        by_name = {m["name"]: m for m in json.loads(raw)["models"]}
        self.assertTrue(by_name["Qwen-Toy-Q4_K_M.gguf"]["llama_supported"])
        self.assertFalse(by_name["Brand-New-Q8_0.gguf"]["llama_supported"])
        self.assertEqual(by_name["Brand-New-Q8_0.gguf"]["architecture"], "inkling2_loop")

    def test_load_refuses_before_touching_anything(self):
        before = bridge.load_json(bridge.SETTINGS_FILE, {})
        status, raw = self._request("POST", "/api/models/load", {"path": self.new_model})
        res = json.loads(raw)
        self.assertEqual(status, 409)
        self.assertTrue(res["llama_too_old"])
        self.assertIn("needs a newer llama.cpp", res["error"])
        self.assertIn("inkling2_loop", res["error"])
        self.assertEqual(self.calls, [])                       # no tune, no stop, no spawn
        self.assertEqual(bridge.load_json(bridge.SETTINGS_FILE, {}), before)

    def test_supported_models_still_go_through(self):
        self._request("POST", "/api/models/load", {"path": self.old_model})
        self.assertIn(("start", self.old_model), self.calls)


class MenuWarningSourceTests(unittest.TestCase):
    def test_menu_and_settings_warn_instead_of_loading(self):
        src = frontend_source()
        self.assertIn("entry.llama_supported === false", src)       # loadModelByPath guard
        self.assertIn("wbTooOldBadge(single)", src)                  # single-file rows
        self.assertIn("wbTooOldBadge(m)", src)                       # version rows
        self.assertIn("needs a newer llama.cpp", src)                # settings dropdown


if __name__ == "__main__":
    unittest.main()
