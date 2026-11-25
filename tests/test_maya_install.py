"""Installing the Maya engine from inside the app (Settings > Model > Maya engine, and
the "made for the Maya engine" warning).

Maya runs Inkling 2 Mini about 2.5 times faster than llama.cpp but ships as source
only, so until 2026-11-24 only someone who built it could use it. Accuretta's releases
now carry a ready-made package (tools/maya-package/build.ps1); the app downloads it,
checks every file, starts the engine once, and switches, putting the old version back
when the model doesn't start on the new one.
"""
import hashlib
import http.client
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source
from accuretta.runtime import maya_find as mf
from accuretta.runtime import maya_install as mi
from tests._sandbox import sandbox_user_files


def _package_tree(root: Path, version: str) -> Path:
    """A minimal installed package (what the zip's maya/ folder holds)."""
    files = {"serve/server.py": b"# server", "build/maya-ik2.exe": b"MZ engine",
             "tools/strata_tokenizer.py": b"# tok", "pylib/common/jinja2/__init__.py": b"",
             f"pylib/cp{sys.version_info[0]}{sys.version_info[1]}/regex/__init__.py": b""}
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    manifest = {"version": version, "engine": "build/maya-ik2.exe", "vision": "build-vision/bin/maya-vision.exe",
                "python_libs": "pylib/common", "python_libs_per_version": "pylib/cp{0}{1}",
                "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}}
    (root / "accuretta-maya.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _package_zip(folder: Path, version: str) -> Path:
    tree = _package_tree(folder / "src" / "maya", version)
    zpath = folder / f"Accuretta-Maya-{version}-win-x64.zip"
    with zipfile.ZipFile(zpath, "w") as z:
        for p in tree.rglob("*"):
            if p.is_file():
                z.write(p, "maya/" + p.relative_to(tree).as_posix())
    return zpath


class PackageDirMixin:
    def use_package_dir(self):
        self.pkg = Path(tempfile.mkdtemp()) / "bin" / "maya"
        for mod in (mf, mi):
            patcher = patch.object(mod, "MAYA_PACKAGE_DIR", self.pkg)
            patcher.start()
            self.addCleanup(patcher.stop)
        # no checkout anywhere: an empty home and no env override
        home = patch.object(mf.Path, "home", lambda: Path(tempfile.mkdtemp()))
        home.start()
        self.addCleanup(home.stop)
        env = patch.dict(mf.os.environ, {"ACCURETTA_MAYA_DIR": ""})
        env.start()
        self.addCleanup(env.stop)
        mf._MAYA_MANIFEST_CACHE.clear()

    def activate(self, version):
        self.pkg.mkdir(parents=True, exist_ok=True)
        (self.pkg / "active.json").write_text(json.dumps({"version": version}), encoding="utf-8")


class FindPackageTests(PackageDirMixin, unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.use_package_dir()

    def test_an_installed_package_runs_on_accurettas_own_python(self):
        self.assertEqual(mf.find_maya_engine(), {})
        _package_tree(self.pkg / "2026.11.24.1643", "2026.11.24.1643")
        self.activate("2026.11.24.1643")
        eng = mf.find_maya_engine()
        self.assertEqual((eng["kind"], eng["version"]), ("package", "2026.11.24.1643"))
        self.assertTrue(eng["engine"].endswith("maya-ik2.exe"))
        self.assertEqual(Path(eng["python"]).parent, Path(sys.executable).parent)
        libs = eng["pythonpath"].split(mf.os.pathsep)
        self.assertEqual([Path(p).name for p in libs], ["common", f"cp{sys.version_info[0]}{sys.version_info[1]}"])

    def test_a_checkout_someone_built_wins(self):
        _package_tree(self.pkg / "2026.11.24.1643", "2026.11.24.1643")
        self.activate("2026.11.24.1643")
        checkout = Path(tempfile.mkdtemp()) / "maya-inkling-mini"
        for rel in ("serve/server.py", "build/maya-ik2.exe", ".venv/Scripts/python.exe"):
            (checkout / rel).parent.mkdir(parents=True, exist_ok=True)
            (checkout / rel).write_bytes(b"x")
        with patch.dict(mf.os.environ, {"ACCURETTA_MAYA_DIR": str(checkout)}), patch.object(mf.os, "name", "nt"):
            self.assertEqual(mf.find_maya_engine()["kind"], "checkout")

    def test_a_bad_active_entry_is_ignored(self):
        for bad in ("../x", "..", "", "a/b"):
            self.activate(bad)
            self.assertIsNone(mf.active_maya_package(), bad)

    def test_package_runner_loads_pylib_when_python_ignores_pythonpath(self):
        # Embeddable Windows Python (shipped by the installer) uses a ._pth file
        # that ignores PYTHONPATH and omits the script directory from sys.path.
        # Python's -I flag enforces the same isolation, so testing under -I
        # proves packaged Maya scripts still find their bundled pylib and siblings.
        root = _package_tree(self.pkg / "2026.11.24.1643", "2026.11.24.1643")
        (root / "serve" / "helper.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
        (root / "serve" / "server.py").write_text(
            "import sys, jinja2, regex, helper\n"
            "print(helper.VALUE, sys.argv[1:])\n",
            encoding="utf-8",
        )
        self.activate("2026.11.24.1643")
        eng = mf.find_maya_engine()
        cmd = mf._maya_python_cmd(eng, eng["server"], "--flag", "42")
        # Insert -I right after the Python executable to simulate ._pth isolation.
        iso_cmd = [cmd[0], "-I", *cmd[1:]]
        out = subprocess.run(
            iso_cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": mf.os.environ.get("PATH", ""), "SYSTEMROOT": mf.os.environ.get("SYSTEMROOT", ""),
                 "PYTHONPATH": eng["pythonpath"]},
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("ok ['--flag', '42']", out.stdout)
        # Developer checkouts (no pythonpath) keep running the script directly.
        self.assertEqual(
            mf._maya_python_cmd({"python": "py.exe"}, "serve/server.py", "--port", "8080"),
            ["py.exe", "serve/server.py", "--port", "8080"],
        )


class PackageCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_unpacks_under_one_folder_and_refuses_escapes(self):
        z = _package_zip(self.tmp, "2026.11.24.1643")
        out = self.tmp / "out"
        mi._extract(z, out)
        self.assertTrue((out / "serve" / "server.py").is_file())
        self.assertFalse((out / "maya").exists())
        evil = self.tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("maya/../../outside.txt", "x")
        with self.assertRaises(RuntimeError):
            mi._extract(evil, self.tmp / "out2")
        self.assertFalse((self.tmp / "outside.txt").exists())

    def _ok_run(self, cmd, **kw):
        said = "maya-ik2: usage: ..." if str(cmd[0]).endswith(".exe") else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=said, stderr="")

    def test_every_file_is_checked_and_the_engine_must_start(self):
        root = _package_tree(self.tmp / "pkg", "2026.11.24.1643")
        with patch.object(mi.subprocess, "run", self._ok_run):
            self.assertEqual(mi._check_package(root)["version"], "2026.11.24.1643")
            (root / "serve" / "server.py").write_bytes(b"# changed")
            with self.assertRaisesRegex(RuntimeError, "damaged"):
                mi._check_package(root)
        root = _package_tree(self.tmp / "pkg2", "2026.11.24.1643")
        with patch.object(mi.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
                cmd, 3221225781, stdout="", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "didn't start"):
                mi._check_package(root)

    def test_library_probe_works_when_python_ignores_pythonpath(self):
        # _check_package's Python library check must succeed even under isolated
        # Python (-I / Windows embeddable ._pth) where PYTHONPATH is ignored.
        root = _package_tree(self.tmp / "pkg_iso", "2026.11.24.1643")
        real_run = subprocess.run

        def run_isolated(cmd, **kw):
            if "-c" not in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="maya-ik2: usage: ...", stderr="")
            return real_run([cmd[0], "-I", *cmd[1:]], **kw)

        with patch.object(mi.subprocess, "run", run_isolated):
            self.assertEqual(mi._check_package(root)["version"], "2026.11.24.1643")


class SourceAndEligibilityTests(unittest.TestCase):
    def setUp(self):
        mi._SOURCE.clear()
        self.addCleanup(mi._SOURCE.clear)

    def test_picks_the_newest_package_from_accurettas_releases_only(self):
        def asset(name, url=None):
            return {"name": name, "size": 421_000_000, "digest": "sha256:ab",
                    "browser_download_url": url or f"{mi._DOWNLOAD_PREFIX}v2/{name}"}
        releases = [{"assets": [asset("Accuretta-Maya-2026.11.24.1643-win-x64.zip"),
                                asset("Accuretta-Setup-2.0.0.exe")]},
                    {"assets": [asset("Accuretta-Maya-2026.12.01.0900-win-x64.zip",
                                      "https://evil.example/Accuretta-Maya-2026.12.01.0900-win-x64.zip")]},
                    {"draft": True, "assets": [asset("Accuretta-Maya-2027.01.01.0000-win-x64.zip")]}]

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(releases).encode()

        with patch.object(mi.urllib.request, "urlopen", lambda *a, **k: Resp()), \
                patch.dict(mi.os.environ, {"ACCURETTA_MAYA_PACKAGE": ""}):
            src = mi._newest_package(force=True)
        self.assertEqual(src["version"], "2026.11.24.1643")
        self.assertTrue(src["url"].startswith(mi._DOWNLOAD_PREFIX))

    def test_only_pcs_that_can_run_it_are_offered_it(self):
        cases = [({}, "NVIDIA graphics card"),
                 ({"name": "RTX 2080 Ti", "vram_gb": 11, "compute": (7, 5), "driver": 600}, "RTX 30-series"),
                 ({"name": "RTX 3080", "vram_gb": 10, "compute": (8, 6), "driver": 600}, "20 GB"),
                 ({"name": "RTX 3090", "vram_gb": 24, "compute": (8, 6), "driver": 560}, "driver 580"),
                 ({"name": "NVIDIA", "vram_gb": 0, "compute": (0, 0), "driver": 0, "old_driver": True}, "driver 580"),
                 ({"name": "RTX 4090", "vram_gb": 24, "compute": (8, 9), "driver": 621}, "")]
        for gpu, why in cases:
            with self.subTest(gpu=gpu.get("name")), patch.object(mi, "_gpu_info", lambda g=gpu: g), \
                    patch.object(mi.os, "name", "nt"):
                ok, reason = mi.maya_eligibility()
                self.assertEqual(ok, not why)
                self.assertIn(why, reason)


class FakeLlama:
    def __init__(self, model="", results=()):
        self.model, self.results, self.starts = model, list(results), []
        self._starting = False

    def loaded_model(self):
        return self.model

    def engine(self):
        return "maya" if self.model else ""

    def start(self, model, wait_seconds=120):
        self.starts.append((model, (mf.active_maya_package() or Path("-")).name))
        return self.results.pop(0) if self.results else {"ok": True}


class InstallFlowTests(PackageDirMixin, unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.use_package_dir()
        self.tmp = Path(tempfile.mkdtemp())
        self.zip = _package_zip(self.tmp, "2026.11.24.1643")
        self.model = str(self.tmp / "Inkling-2-Mini-UD-Q4_K_XL.gguf")
        Path(self.model).write_bytes(b"GGUF")
        for name, value in (
                ("USER_DIR", self.tmp / "home"),
                ("_newest_package", lambda force=False: {"version": "2026.11.24.1643", "url": str(self.zip),
                                                        "size": self.zip.stat().st_size, "local": True}),
                ("_check_package", lambda root: mf.maya_package_manifest(root)),
                ("_maya_engine_for", lambda path: {"kind": "package"}),
                ("_model_busy", lambda: False),
                ("broadcast_event", lambda event: None)):
            patcher = patch.object(mi, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        mi.INSTALL_STATE.clear()
        mi.INSTALL_STATE.update({"status": "checking"})
        self.addCleanup(mi.INSTALL_STATE.clear)

    def _run(self, llama):
        with patch.object(mi, "_llama", llama):
            mi._run_install()
        return dict(mi.INSTALL_STATE)

    def test_a_fresh_install_sets_it_up(self):
        state = self._run(FakeLlama())
        self.assertEqual(state["status"], "done", state)
        self.assertIn("installed", state["message"])
        self.assertEqual(mf.find_maya_engine()["version"], "2026.11.24.1643")
        self.assertFalse(list((self.tmp / "home" / "bin").glob(".maya-download-*")))   # staging cleaned up

    def test_an_update_restarts_the_running_model_on_the_new_version(self):
        _package_tree(self.pkg / "2026.11.20.0800", "2026.11.20.0800")
        self.activate("2026.11.20.0800")
        llama = FakeLlama(self.model)
        state = self._run(llama)
        self.assertEqual(state["status"], "done", state)
        self.assertEqual(llama.starts, [(self.model, "2026.11.24.1643")])
        self.assertTrue((self.pkg / "2026.11.20.0800").is_dir())        # the previous one is kept

    def test_a_model_that_wont_start_puts_the_old_version_back(self):
        _package_tree(self.pkg / "2026.11.20.0800", "2026.11.20.0800")
        self.activate("2026.11.20.0800")
        llama = FakeLlama(self.model, results=[{"ok": False, "error": "cuda error"}, {"ok": True}])
        state = self._run(llama)
        self.assertEqual(state["status"], "failed")
        self.assertIn("went back to the previous version", state["message"])
        self.assertEqual(mf.active_maya_package().name, "2026.11.20.0800")
        self.assertEqual([v for _m, v in llama.starts], ["2026.11.24.1643", "2026.11.20.0800"])
        self.assertFalse((self.pkg / "2026.11.24.1643").exists())

    def test_already_newest_downloads_nothing(self):
        _package_tree(self.pkg / "2026.11.24.1643", "2026.11.24.1643")
        self.activate("2026.11.24.1643")
        with patch.object(mi, "_download", side_effect=AssertionError("downloaded")):
            state = self._run(FakeLlama())
        self.assertEqual(state["status"], "up_to_date")

    def test_a_broken_package_is_not_used(self):
        with patch.object(mi, "_check_package", side_effect=RuntimeError("build/maya-ik2.exe is damaged")):
            state = self._run(FakeLlama())
        self.assertEqual(state["status"], "failed")
        self.assertIn("damaged", state["message"])
        self.assertIsNone(mf.active_maya_package())
        self.assertFalse((self.pkg / "2026.11.24.1643").exists())

    def test_never_replaces_a_maya_someone_built(self):
        with patch.object(mi, "find_maya_engine", lambda: {"kind": "checkout", "root": "C:/maya"}):
            state = self._run(FakeLlama())
        self.assertEqual(state["status"], "failed")
        self.assertIn("its own Maya build", state["message"])


class StartAndStatusTests(PackageDirMixin, unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.use_package_dir()
        mi.INSTALL_STATE.clear()
        mi.INSTALL_STATE.update({"status": "idle"})
        self.addCleanup(mi.INSTALL_STATE.clear)

    def test_refuses_where_it_cant_run_and_while_running(self):
        with patch.object(mi, "maya_eligibility", lambda: (False, "Maya needs an NVIDIA graphics card.")):
            self.assertEqual(mi.start_maya_install(), (409, {"ok": False, "error": "Maya needs an NVIDIA graphics card."}))
        mi.INSTALL_STATE["status"] = "downloading"
        with patch.object(mi, "maya_eligibility", lambda: (True, "")), patch.object(mi, "_offline_active", False):
            code, body = mi.start_maya_install()
        self.assertEqual(code, 409)
        self.assertIn("already", body["error"])

    def test_status_offers_install_only_when_it_helps(self):
        with patch.object(mi, "maya_eligibility", lambda: (True, "")), \
                patch.object(mi, "_gpu_info", lambda: {"name": "RTX 4090", "vram_gb": 24}), \
                patch.object(mi, "_local_package", lambda: {"version": "2026.11.24.1643", "size": 421_000_000}):
            st = mi.maya_install_status()
            self.assertTrue(st["available"])
            self.assertEqual(st["download_mb"], 421)
            with patch.object(mi, "find_maya_engine", lambda: {"kind": "checkout", "root": "C:/maya"}):
                self.assertFalse(mi.maya_install_status()["available"])


class EndpointTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        self.started = []
        for name, value in (("maya_install_status", lambda check=False: {"eligible": True, "checked": check}),
                            ("start_maya_install", lambda: (self.started.append(1) or (200, {"ok": True})))):
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
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request("GET", "/")
        page = conn.getresponse().read()
        conn.close()
        self.token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()

    def test_status_and_start(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request("GET", "/api/maya/install?check=1")
        resp = conn.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())), (200, {"eligible": True, "checked": True}))
        conn.request("POST", "/api/maya/install", "{}",
                     {"Content-Type": "application/json", "X-Accuretta-Token": self.token})
        resp = conn.getresponse()
        self.assertEqual((resp.status, json.loads(resp.read())), (200, {"ok": True}))
        conn.close()
        self.assertEqual(self.started, [1])


class FrontendTests(unittest.TestCase):
    def test_settings_row_and_warning_offer_the_install(self):
        index = (bridge.ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="btn-maya-install"', index)
        self.assertIn('id="maya-install-row" hidden', index)            # hidden unless this PC can run it
        self.assertIn("offerMayaInstall(name, modelPath)", frontend_source())


if __name__ == "__main__":
    unittest.main()
