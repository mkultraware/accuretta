"""Updating llama.cpp from inside the app (Settings > Model > llama.cpp, and the
"needs a newer llama.cpp" warning).

Seen 2026-11-24: the first-run wizard downloaded llama.cpp once and nothing ever
updated it, so a model that needed a newer build (Inkling 2 Mini's loop fix in
b12611+) left a normal user stuck with "Update llama.cpp" and no way to. The
update downloads the newest official build for the same backend, tests it, and
switches; when the model doesn't start on it, the old build comes back.
"""
import http.client
import json
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source
from accuretta.runtime import llama_update as lu
from tests._sandbox import sandbox_user_files


def _build(folder: Path, backend_dll: str = "ggml-cuda.dll") -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / backend_dll).write_bytes(b"dll")
    server = folder / "llama-server.exe"
    server.write_bytes(b"MZ server")
    return str(server)


class FakeLlama:
    """Stands in for the running llama-server manager."""

    def __init__(self, model="", results=()):
        self.model, self.results, self.starts = model, list(results), []
        self._starting = False

    def loaded_model(self):
        return self.model

    def start(self, model, wait_seconds=120):
        self.starts.append((model, lu.get_settings().get("llama_bin")))
        return self.results.pop(0) if self.results else {"ok": True}


class BuildInfoTests(unittest.TestCase):
    def setUp(self):
        lu._BUILD_CACHE.clear()
        self.addCleanup(lu._BUILD_CACHE.clear)
        self.root = Path(tempfile.mkdtemp())

    def _version(self, text):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=text)

    def test_reads_the_build_from_both_version_formats(self):
        server = _build(self.root / "a")
        with patch.object(lu.subprocess, "run", return_value=self._version(
                "version: 1.0.0-dev (build 12669, commit 30ec35ae2)\nbuilt with Clang")):
            self.assertEqual(lu.llama_build_number(server), 12669)
        lu._BUILD_CACHE.clear()
        with patch.object(lu.subprocess, "run", return_value=self._version("version: 12171 (abc1234)")):
            self.assertEqual(lu.llama_build_number(server), 12171)

    def test_a_build_that_doesnt_run_is_zero_and_not_cached(self):
        server = _build(self.root / "b")
        with patch.object(lu.subprocess, "run", side_effect=OSError("bad exe")):
            self.assertEqual(lu.llama_build_number(server), 0)
        self.assertEqual(lu._BUILD_CACHE, {})
        self.assertEqual(lu.llama_build_number(str(self.root / "missing.exe")), 0)

    def test_the_backend_comes_from_the_libraries_beside_it(self):
        self.assertEqual(lu.llama_build_kind(_build(self.root / "c", "ggml-cuda.dll")), "CUDA")
        self.assertEqual(lu.llama_build_kind(_build(self.root / "v", "ggml-vulkan.dll")), "VULKAN")
        self.assertEqual(lu.llama_build_kind(_build(self.root / "p", "ggml-cpu-haswell.dll")), "CPU")

    def test_only_builds_the_app_downloaded_count_as_its_own(self):
        managed = Path(tempfile.mkdtemp())
        with patch.object(lu, "MANAGED_LLAMA_DIR", managed):
            own = _build(managed / "b12669" / "cuda")
            self.assertEqual(lu._install_root(own), managed / "b12669" / "cuda")
            self.assertIsNone(lu._install_root(_build(self.root / "user-build")))


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.root = Path(tempfile.mkdtemp())
        self.managed = self.root / "managed"
        self.old = _build(self.root / "llama.cpp-b100")
        self.model = str(self.root / "model.gguf")
        Path(self.model).write_bytes(b"GGUF")
        bridge.save_json(bridge.SETTINGS_FILE, {"llama_bin": self.old})
        self.builds = {self.old: 100}
        self.fetched = []
        self.saved = []
        for name, value in (
                ("MANAGED_LLAMA_DIR", self.managed),
                ("_latest_llama_release_assets", lambda kind: ("b200", [{"name": "x.zip", "size": 5_000_000}])),
                ("fetch_llama_release", self._fake_fetch),
                ("llama_build_number", lambda path: self.builds.get(str(Path(path).resolve()),
                                                                    self.builds.get(path, 0))),
                ("read_gguf_metadata", lambda *a, **k: {"keys": {"general.architecture": "qwen35"}}),
                ("llama_knows_arch", lambda arch, path="": True),
                ("_maya_engine_for", lambda path: {}),
                ("_model_busy", lambda: False),
                ("broadcast_event", lambda event: None)):
            patcher = patch.object(lu, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        lu._LATEST.clear()
        self.addCleanup(lu._LATEST.clear)
        lu.UPDATE_STATE.clear()
        lu.UPDATE_STATE.update({"status": "checking"})
        self.addCleanup(lu.UPDATE_STATE.clear)

    def _fake_fetch(self, kind, progress, lock, release=None):
        self.fetched.append((kind, release[0]))
        server = _build(self.managed / release[0] / kind.lower())
        self.builds[str(Path(server).resolve())] = 200
        return release[0], Path(server)

    def _run(self, llama):
        with patch.object(lu, "_llama", llama):
            lu._run_update(self.old)
        return dict(lu.UPDATE_STATE)

    def test_downloads_tests_switches_and_restarts_the_model(self):
        llama = FakeLlama(self.model)
        state = self._run(llama)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(self.fetched, [("CUDA", "b200")])
        new_bin = lu.get_settings()["llama_bin"]
        self.assertIn(str(self.managed), new_bin)
        self.assertEqual(llama.starts, [(self.model, new_bin)])     # restarted on the NEW build
        self.assertIn("from b100 to b200", state["message"])
        self.assertTrue(Path(self.old).is_file())                    # the user's own build is never removed

    def test_a_model_that_wont_start_puts_the_old_build_back(self):
        llama = FakeLlama(self.model, results=[{"ok": False, "error": "crashed"}, {"ok": True}])
        state = self._run(llama)
        self.assertEqual(state["status"], "failed")
        self.assertIn("went back to b100", state["message"])
        self.assertEqual(lu.get_settings()["llama_bin"], self.old)
        self.assertEqual([b for _m, b in llama.starts][-1], self.old)  # restarted on the OLD build
        self.assertFalse((self.managed / "b200" / "cuda").exists())   # the failed download is removed

    def test_already_newest_downloads_nothing(self):
        self.builds[self.old] = 200
        state = self._run(FakeLlama(self.model))
        self.assertEqual(state["status"], "up_to_date")
        self.assertEqual(self.fetched, [])
        self.assertEqual(lu.get_settings()["llama_bin"], self.old)

    def test_a_build_that_reports_the_wrong_version_is_not_used(self):
        def fetch(kind, progress, lock, release=None):
            server = _build(self.managed / "b200" / "cuda")
            self.builds[str(Path(server).resolve())] = 150
            return "b200", Path(server)
        with patch.object(lu, "fetch_llama_release", fetch):
            state = self._run(FakeLlama(self.model))
        self.assertEqual(state["status"], "failed")
        self.assertIn("b150, not b200", state["message"])
        self.assertEqual(lu.get_settings()["llama_bin"], self.old)

    def test_a_build_that_doesnt_know_the_model_is_not_used(self):
        llama = FakeLlama(self.model)
        with patch.object(lu, "llama_knows_arch", lambda arch, path="": False):
            state = self._run(llama)
        self.assertEqual(state["status"], "failed")
        self.assertIn("qwen35", state["message"])
        self.assertEqual(llama.starts, [])
        self.assertEqual(lu.get_settings()["llama_bin"], self.old)

    def test_a_model_on_maya_is_left_running(self):
        llama = FakeLlama(self.model)
        with patch.object(lu, "_maya_engine_for", lambda path: {"root": "maya"}):
            state = self._run(llama)
        self.assertEqual(state["status"], "done")
        self.assertEqual(llama.starts, [])
        self.assertIn(str(self.managed), lu.get_settings()["llama_bin"])

    def test_older_app_builds_are_cleaned_up_but_the_previous_one_is_kept(self):
        older = _build(self.managed / "b50" / "cuda")
        previous = _build(self.managed / "b100" / "cuda")
        self.builds[str(Path(previous).resolve())] = 100
        bridge.save_json(bridge.SETTINGS_FILE, {"llama_bin": previous})
        with patch.object(lu, "_llama", FakeLlama(self.model)):
            lu._run_update(previous)
        self.assertEqual(lu.UPDATE_STATE["status"], "done")
        self.assertFalse(Path(older).exists())
        self.assertTrue(Path(previous).exists())
        self.assertTrue((self.managed / "b200" / "cuda" / "llama-server.exe").exists())


class StartGuardTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        lu.UPDATE_STATE.clear()
        lu.UPDATE_STATE.update({"status": "idle"})
        self.addCleanup(lu.UPDATE_STATE.clear)

    def test_refuses_offline_without_a_build_and_while_running(self):
        with patch.object(lu, "_offline_active", True):
            self.assertEqual(lu.start_llama_update()[0], 409)
        with patch.object(lu, "find_llama_bin", lambda: ""), patch.object(lu.os, "name", "nt"):
            code, body = lu.start_llama_update()
            self.assertEqual(code, 409)
            self.assertIn("setup", body["error"])
        lu.UPDATE_STATE["status"] = "downloading"
        with patch.object(lu, "find_llama_bin", lambda: "C:/x/llama-server.exe"), patch.object(lu.os, "name", "nt"):
            code, body = lu.start_llama_update()
        self.assertEqual(code, 409)
        self.assertIn("already running", body["error"])

    def test_status_says_when_a_newer_build_is_out(self):
        root = Path(tempfile.mkdtemp())
        server = _build(root / "b100")
        with patch.object(lu, "find_llama_bin", lambda: server), \
                patch.object(lu, "llama_build_number", lambda path: 100), \
                patch.object(lu, "_latest_llama_release_assets",
                             lambda kind: ("b200", [{"name": "a.zip", "size": 340_000_000},
                                                    {"name": "b.zip", "size": 370_000_000}])), \
                patch.object(lu, "_offline_active", False), patch.object(lu.os, "name", "nt"):
            lu._LATEST.clear()
            self.addCleanup(lu._LATEST.clear)
            before = lu.llama_update_status()
            after = lu.llama_update_status(check=True)
        self.assertFalse(before["available"])
        self.assertEqual((after["kind"], after["build"], after["latest"]), ("CUDA", 100, "b200"))
        self.assertTrue(after["available"])
        self.assertEqual(after["download_mb"], 710)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.started = []
        for name, value in (("llama_update_status", lambda check=False: {"build": 1, "checked": check}),
                            ("start_llama_update", lambda: (self.started.append(1) or (200, {"ok": True})))):
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

    def test_status_and_start(self):
        status, body = self._request("GET", "/api/llama/update?check=1")
        self.assertEqual((status, json.loads(body)), (200, {"build": 1, "checked": True}))
        status, body = self._request("POST", "/api/llama/update", {})
        self.assertEqual((status, json.loads(body)), (200, {"ok": True}))
        self.assertEqual(self.started, [1])


class FrontendTests(unittest.TestCase):
    def test_settings_row_and_warning_offer_the_update(self):
        src = frontend_source()
        index = (bridge.ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-llama-update"', index)
        self.assertIn("offerLlamaUpdate(name, entry.architecture)", src)   # the too-old warning
        self.assertIn('fetch("/api/llama/update"', src.replace('api("/api/llama/update"', 'fetch("/api/llama/update"'))


if __name__ == "__main__":
    unittest.main()
