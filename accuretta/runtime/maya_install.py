# SPDX-License-Identifier: AGPL-3.0-only
"""Installing and updating the Maya engine from inside the app.

Maya runs Inkling 2 Mini about 2.5 times faster than llama.cpp, but upstream ships
source only. Accuretta's releases carry a ready-made package of it
(tools/maya-package/build.ps1); this downloads the newest one, checks every file
against its manifest, runs the engine once, and switches to it. A Maya model that
was running is restarted on the new version, and the old one comes back when it
doesn't start. The previous version is kept; older ones are removed. A Maya
someone built themselves (a checkout with its own .venv) always wins and is never
touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path

from accuretta.core.events import broadcast_event
from accuretta.core.paths import USER_DIR
from accuretta.core.settings import _offline_active
from accuretta.core.updates import _UPDATE_REPO
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_update import _model_busy
from accuretta.runtime.maya_find import MAYA_PACKAGE_DIR
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.maya_find import _own_python
from accuretta.runtime.maya_find import _package_python_path
from accuretta.runtime.maya_find import active_maya_package
from accuretta.runtime.maya_find import find_maya_engine
from accuretta.runtime.maya_find import maya_package_manifest
from accuretta.security.child_env import _child_env


INSTALL_LOCK = threading.Lock()


# The running or last install. status: idle, checking, downloading, extracting,
# testing, waiting (for a reply to finish), switching, done, up_to_date, failed.
INSTALL_STATE: dict = {"status": "idle"}


_RUNNING = {"checking", "downloading", "extracting", "testing", "waiting", "switching"}


_ASSET_RE = re.compile(r"^Accuretta-Maya-(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)-win-x64\.zip$")


_RELEASES_API = f"https://api.github.com/repos/{_UPDATE_REPO}/releases?per_page=15"


_DOWNLOAD_PREFIX = f"https://github.com/{_UPDATE_REPO}/releases/download/"


# What Inkling 2 Mini needs with every expert on the card (Maya's way of being fast).
_MIN_VRAM_GB = 20.0
_MIN_COMPUTE = (8, 6)        # RTX 30 series and newer (the package carries 8.6, 8.9 and 12.0)
_MIN_DRIVER = 580            # CUDA 13


_SOURCE: dict = {}
_SOURCE_TTL_S = 10 * 60
_GPU: dict = {}


def _version_key(version: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", str(version or "")))


def _gpu_info() -> dict:
    """The NVIDIA card with the most memory: name, vram_gb, compute (major, minor),
    driver (major). {} without an NVIDIA driver. Looked up once."""
    if "probed" in _GPU:
        return _GPU.get("info", {})
    info: dict = {}
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,compute_cap,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, env=_child_env(keep=("gpu",)),
            creationflags=0x08000000 if os.name == "nt" else 0)
        best = None
        if out.returncode != 0:
            # nvidia-smi is there but too old for these questions: an old driver
            best = {"name": "NVIDIA", "vram_gb": 0.0, "compute": (0, 0), "driver": 0, "old_driver": True}
        for line in (out.stdout or "").splitlines() if out.returncode == 0 else []:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 4:
                continue
            try:
                mem = float(parts[1]) / 1024
                cc = tuple(int(x) for x in parts[2].split(".")[:2])
                driver = int(parts[3].split(".")[0])
            except ValueError:
                continue
            if best is None or mem > best["vram_gb"]:
                best = {"name": parts[0], "vram_gb": round(mem, 1), "compute": cc, "driver": driver}
        info = best or {}
    except (OSError, subprocess.SubprocessError):
        info = {}
    _GPU.update({"probed": True, "info": info})
    return info


def maya_eligibility() -> tuple[bool, str]:
    """Whether this PC can run Maya, and in plain words why not."""
    if os.name != "nt":
        return False, "Installing Maya from the app works on Windows."
    gpu = _gpu_info()
    if not gpu:
        return False, "Maya needs an NVIDIA graphics card."
    if gpu.get("old_driver"):
        return False, f"Maya needs NVIDIA driver {_MIN_DRIVER} or newer. Update the graphics driver first."
    if tuple(gpu["compute"]) < _MIN_COMPUTE:
        return False, f"Maya needs an RTX 30-series card or newer ({gpu['name']} is older)."
    if gpu["vram_gb"] < _MIN_VRAM_GB:
        return False, (f"Maya keeps the whole model on the graphics card and needs about "
                       f"{_MIN_VRAM_GB:.0f} GB; {gpu['name']} has {gpu['vram_gb']:.0f} GB.")
    if gpu["driver"] < _MIN_DRIVER:
        return False, (f"Maya needs NVIDIA driver {_MIN_DRIVER} or newer (this PC has {gpu['driver']}). "
                       "Update the graphics driver first.")
    return True, ""


def _local_package() -> dict:
    """A package file named by ACCURETTA_MAYA_PACKAGE (for testing a build before
    it's published), else {}."""
    path = (os.environ.get("ACCURETTA_MAYA_PACKAGE") or "").strip()
    if not path or not Path(path).is_file():
        return {}
    m = _ASSET_RE.match(Path(path).name)
    if not m:
        return {}
    return {"version": m.group(1), "url": str(Path(path).resolve()), "size": Path(path).stat().st_size,
            "digest": "", "local": True}


def _newest_package(force: bool = False) -> dict:
    """The newest Maya package on Accuretta's releases: {version, url, size, digest}."""
    local = _local_package()
    if local:
        return {**local, "checked_at": time.time(), "error": ""}
    now = time.time()
    if _SOURCE and not force and now - _SOURCE.get("checked_at", 0) < _SOURCE_TTL_S:
        return dict(_SOURCE)
    found: dict = {}
    error = ""
    try:
        req = urllib.request.Request(_RELEASES_API, headers={
            "Accept": "application/vnd.github+json", "User-Agent": "Accuretta",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=15) as r:
            releases = json.loads(r.read().decode("utf-8"))
        for rel in releases if isinstance(releases, list) else []:
            if rel.get("draft"):
                continue
            for asset in rel.get("assets") or []:
                m = _ASSET_RE.match(str(asset.get("name") or ""))
                url = str(asset.get("browser_download_url") or "")
                if not m or not url.startswith(_DOWNLOAD_PREFIX):
                    continue
                if not found or _version_key(m.group(1)) > _version_key(found["version"]):
                    found = {"version": m.group(1), "url": url, "size": int(asset.get("size") or 0),
                             "digest": str(asset.get("digest") or "").lower()}
        if not found:
            error = "no Maya package has been published yet"
    except Exception as exc:
        error = str(exc)[:200]
    _SOURCE.clear()
    _SOURCE.update({**(found or {}), "checked_at": now, "error": error})
    return dict(_SOURCE)


def maya_install_status(check: bool = False) -> dict:
    """What Settings shows: the Maya in use, whether this PC can run it, and the
    newest package once looked up."""
    eligible, reason = maya_eligibility()
    eng = find_maya_engine()
    if check and eligible and not _offline_active:
        _newest_package(force=True)
    src = _local_package() or dict(_SOURCE)
    installed = {"kind": eng.get("kind", ""), "version": eng.get("version", ""), "root": eng.get("root", "")} if eng else {}
    newer = bool(src.get("version")) and (
        not eng or (eng.get("kind") == "package"
                    and _version_key(src["version"]) > _version_key(eng.get("version", ""))))
    with INSTALL_LOCK:
        job = dict(INSTALL_STATE)
    gpu = _gpu_info()
    return {
        "supported": os.name == "nt",
        "offline": bool(_offline_active),
        "eligible": eligible,
        "reason": reason,
        "gpu": {"name": gpu.get("name", ""), "vram_gb": gpu.get("vram_gb", 0)} if gpu else {},
        "installed": installed,
        "latest": src.get("version", ""),
        "download_mb": round(int(src.get("size") or 0) / 1e6),
        "available": bool(eligible and newer and eng.get("kind") != "checkout"),
        "check_error": src.get("error", ""),
        "job": job,
    }


def _set(**fields) -> None:
    with INSTALL_LOCK:
        INSTALL_STATE.update(fields)


def _download(src: dict, dest: Path) -> None:
    if src.get("local"):
        shutil.copyfile(src["url"], dest)
        return
    if not str(src.get("url") or "").startswith(_DOWNLOAD_PREFIX):
        raise RuntimeError("refusing a Maya package from outside Accuretta's releases")
    req = urllib.request.Request(src["url"], headers={"User-Agent": "Accuretta"})
    digest = hashlib.sha256()
    done, started = 0, time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as r, dest.open("wb") as out:
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            speed = done / max(time.monotonic() - started, 0.001)
            _set(bytes_written=done, speed=(f"{speed / 1048576:.1f} MB/s" if speed >= 1048576
                                            else f"{speed / 1024:.0f} KB/s"))
    if src.get("size") and dest.stat().st_size != int(src["size"]):
        raise RuntimeError("the download was cut short")
    expected = str(src.get("digest") or "")
    if expected.startswith("sha256:") and digest.hexdigest() != expected[7:]:
        raise RuntimeError("the download doesn't match its published checksum")


def _extract(archive: Path, dest: Path) -> None:
    """Unpack the package (one top folder, maya/) into dest, refusing anything
    that would land outside it."""
    dest = dest.resolve()
    with zipfile.ZipFile(archive) as z:
        if sum(max(0, i.file_size) for i in z.infolist()) > 4_000_000_000:
            raise RuntimeError("the package is unexpectedly large")
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            rel = name.split("/", 1)[1] if "/" in name else ""
            if not rel or name.endswith("/"):
                continue
            target = (dest / rel).resolve()
            try:
                target.relative_to(dest)
            except ValueError:
                raise RuntimeError(f"unsafe path in the package: {name}") from None
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)


def _check_package(root: Path) -> dict:
    """Every file present and unchanged, the engine starts, the Python libraries import."""
    manifest = maya_package_manifest(root)
    files = manifest.get("files") if isinstance(manifest.get("files"), dict) else {}
    if not files:
        raise RuntimeError("the package has no file list")
    for rel, sha in files.items():
        path = root / rel
        if not path.is_file():
            raise RuntimeError(f"the package is missing {rel}")
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        if h.hexdigest() != str(sha).lower():
            raise RuntimeError(f"{rel} is damaged")
    engine = root / str(manifest.get("engine") or "build/maya-ik2.exe")
    try:
        out = subprocess.run([str(engine)], capture_output=True, text=True, timeout=60, cwd=str(engine.parent),
                             env=_child_env(keep=("gpu",)), creationflags=0x08000000 if os.name == "nt" else 0)
        said = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        said = str(exc)
    if "usage" not in said:
        raise RuntimeError(f"the Maya engine didn't start on this PC ({said.strip()[:160] or 'no output'})")
    libs = os.pathsep.join(_package_python_path(root, manifest))
    # Prepend PYTHONPATH explicitly to sys.path: Windows embeddable Python
    # (shipped by the Accuretta installer) uses a ._pth file that ignores
    # PYTHONPATH during interpreter startup.
    probe = (
        "import os, sys; "
        "sys.path[:0] = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]; "
        "import jinja2, regex"
    )
    out = subprocess.run([_own_python(), "-c", probe], capture_output=True, text=True,
                         timeout=60, env=_child_env({"PYTHONPATH": libs or None}),
                         creationflags=0x08000000 if os.name == "nt" else 0)
    if out.returncode != 0:
        raise RuntimeError("Maya's Python libraries don't load on this Python "
                           f"({(out.stderr or '').strip().splitlines()[-1:] or ['?']})")
    return manifest


def _set_active(version: str) -> None:
    MAYA_PACKAGE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = MAYA_PACKAGE_DIR / "active.json.tmp"
    tmp.write_text(json.dumps({"version": version}), encoding="utf-8")
    os.replace(tmp, MAYA_PACKAGE_DIR / "active.json")


def _clear_active() -> None:
    try:
        (MAYA_PACKAGE_DIR / "active.json").unlink()
    except OSError:
        pass


def _wait_until_idle(limit_s: float = 600.0) -> bool:
    end = time.time() + limit_s
    while time.time() < end:
        if not _model_busy() and not getattr(_llama, "_starting", False):
            return True
        time.sleep(2)
    return False


def _restart_seconds(model: str) -> int:
    try:
        return max(120, min(900, int(os.path.getsize(model) / 1e9 * 20)))
    except OSError:
        return 300


def _prune(keep: set[str]) -> None:
    try:
        entries = [p for p in MAYA_PACKAGE_DIR.iterdir() if p.is_dir()]
    except OSError:
        return
    for p in entries:
        if p.name not in keep:
            shutil.rmtree(p, ignore_errors=True)


def _run_install() -> None:
    stage = USER_DIR / "bin" / f".maya-download-{uuid.uuid4().hex}"
    new_root = None
    previous = active_maya_package()
    try:
        src = _newest_package(force=True)
        if not src.get("version"):
            raise RuntimeError(src.get("error") or "no Maya package was found")
        _set(release=src["version"], bytes_total=int(src.get("size") or 0))
        before = find_maya_engine()
        if before.get("kind") == "checkout":
            raise RuntimeError(f"this PC uses its own Maya build ({before.get('root')}); "
                               "the ready-made one would not be used")
        if previous is not None and _version_key(maya_package_manifest(previous).get("version", "")) >= _version_key(src["version"]):
            _set(status="up_to_date", message=f"Maya is up to date ({src['version']}).",
                 finished_at=int(time.time()))
            return

        _set(status="downloading")
        stage.mkdir(parents=True, exist_ok=True)
        archive = stage / "maya.zip"
        _download(src, archive)

        _set(status="extracting")
        new_root = MAYA_PACKAGE_DIR / src["version"]
        if new_root.exists() and new_root != previous:
            shutil.rmtree(new_root, ignore_errors=True)
        _extract(archive, new_root)

        _set(status="testing")
        manifest = _check_package(new_root)
        if str(manifest.get("version") or "") != src["version"]:
            raise RuntimeError(f"the package says it is {manifest.get('version')}, not {src['version']}")

        model = _llama.loaded_model()
        restart = bool(model) and bool(_maya_engine_for(model)) and _llama.engine() == "maya"
        if restart:
            _set(status="waiting")
            if not _wait_until_idle():
                raise RuntimeError("the model stayed busy for 10 minutes; try again when it's idle")
        _set(status="switching")
        _set_active(src["version"])
        if restart:
            res = _llama.start(model, wait_seconds=_restart_seconds(model))
            if not res.get("ok"):
                reason = str(res.get("error") or "it didn't start")[:200]
                if previous is not None:
                    _set_active(previous.name)
                else:
                    _clear_active()
                back = _llama.start(model, wait_seconds=_restart_seconds(model))
                broadcast_event({"type": "models:update", "loaded_model": model if back.get("ok") else ""})
                raise RuntimeError(f"your model didn't start on Maya {src['version']} ({reason}); "
                                   "went back to the previous version")
        _prune({src["version"]} | ({previous.name} if previous is not None else set()))
        _set(status="done", finished_at=int(time.time()),
             message=(f"Maya updated to {src['version']}." if previous is not None
                      else f"Maya {src['version']} installed."))
        broadcast_event({"type": "models:update", "loaded_model": _llama.loaded_model()})
    except Exception as exc:
        if new_root is not None and new_root != previous and active_maya_package() != new_root:
            shutil.rmtree(new_root, ignore_errors=True)
        _set(status="failed", message=f"Maya install failed: {exc}", finished_at=int(time.time()))
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        broadcast_event({"type": "maya:install"})


def start_maya_install() -> tuple[int, dict]:
    """Start an install or update in the background. Returns (HTTP status, body)."""
    eligible, reason = maya_eligibility()
    if not eligible:
        return 409, {"ok": False, "error": reason}
    if _offline_active and not _local_package():
        return 409, {"ok": False, "error": "Offline mode is on."}
    with INSTALL_LOCK:
        if INSTALL_STATE.get("status") in _RUNNING:
            return 409, {"ok": False, "error": "Maya is already being installed."}
        INSTALL_STATE.clear()
        INSTALL_STATE.update({"status": "checking", "bytes_written": 0, "bytes_total": 0, "speed": "",
                              "message": "", "started_at": int(time.time())})
    threading.Thread(target=_run_install, daemon=True, name="maya-install").start()
    return 200, {"ok": True}
