# SPDX-License-Identifier: AGPL-3.0-only
"""Downloading and unpacking a llama.cpp release safely, with progress.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from accuretta.core.events import broadcast_event
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.paths import USER_DIR
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.llama_releases import _latest_llama_release_assets
from pathlib import Path


# Thread-safe global state for the installer downloader
DOWNLOAD_LOCK = threading.Lock()


DOWNLOAD_STATE = {
    "status": "idle",       # idle, resolving, downloading, extracting, done, failed
    "bytes_written": 0,
    "bytes_total": 0,
    "speed": "0 KB/s",
    "error_msg": ""
}


def _extract_zip_safely(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with zipfile.ZipFile(archive, "r") as bundle:
        total_unpacked = sum(max(0, int(info.file_size)) for info in bundle.infolist())
        if total_unpacked > 5_000_000_000:
            raise RuntimeError(f"refusing oversized llama.cpp archive: {archive.name}")
        for info in bundle.infolist():
            target = (destination / info.filename).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"unsafe path in llama.cpp archive: {info.filename}") from exc
        bundle.extractall(destination)


MANAGED_LLAMA_DIR = USER_DIR / "bin" / "llama" / "releases"


def fetch_llama_release(kind: str, progress: dict, lock: threading.Lock,
                        release: tuple[str, list[dict]] | None = None) -> tuple[str, Path]:
    """Download, check and unpack one official llama.cpp release into the app's own
    folder (bin/llama/releases/<tag>/<kind>), without switching to it. `release`
    is (tag, assets) when the caller already looked it up. Progress goes into
    `progress` under `lock`. Returns (tag, path of its llama-server.exe)."""
    kind = str(kind or "CPU").strip().upper()
    if kind not in {"CUDA", "VULKAN", "CPU"}:
        kind = "CPU"
    stage = USER_DIR / "bin" / f".llama-download-{uuid.uuid4().hex}"
    try:
        tag, assets = release or _latest_llama_release_assets(kind)
        total_size = sum(int(asset["size"]) for asset in assets)
        extract_dir = stage / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=False)
        with lock:
            progress.update({
                "status": "downloading", "bytes_total": total_size,
                "release": tag,
            })

        downloaded = 0
        started = time.monotonic()
        for asset in assets:
            archive = stage / asset["name"]
            digest = hashlib.sha256()
            request = urllib.request.Request(
                asset["url"], headers={"User-Agent": "AccurettaBootstrapper/2.0"})
            with urllib.request.urlopen(request, timeout=30) as response, archive.open("wb") as output:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    elapsed = max(time.monotonic() - started, 0.001)
                    speed = downloaded / elapsed
                    speed_text = (f"{speed / (1024 * 1024):.1f} MB/s" if speed >= 1024 * 1024
                                  else f"{speed / 1024:.1f} KB/s")
                    with lock:
                        progress.update({
                            "bytes_written": downloaded, "speed": speed_text,
                        })
            if archive.stat().st_size != int(asset["size"]):
                raise RuntimeError(f"incomplete llama.cpp download: {asset['name']}")
            expected = str(asset.get("digest") or "")
            if expected.startswith("sha256:") and digest.hexdigest() != expected[7:]:
                raise RuntimeError(f"checksum mismatch for llama.cpp asset: {asset['name']}")

        with lock:
            progress["status"] = "extracting"
        for asset in assets:
            _extract_zip_safely(stage / asset["name"], extract_dir)

        server = next(extract_dir.rglob("llama-server.exe"), None)
        if server is None or not server.is_file():
            raise RuntimeError("downloaded llama.cpp package contains no llama-server.exe")
        relative_server = server.relative_to(extract_dir)
        install_root = MANAGED_LLAMA_DIR / tag / kind.lower()
        if install_root.exists():
            existing = install_root / relative_server
            if existing.is_file():
                server = existing
            else:
                shutil.rmtree(install_root)
                install_root.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(extract_dir), str(install_root))
                server = install_root / relative_server
        else:
            install_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extract_dir), str(install_root))
            server = install_root / relative_server
        return tag, server
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def download_and_extract_llama(build_type: str):
    """Download the newest complete official llama.cpp package set and use it
    (the first-run wizard)."""
    try:
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE.update({
                "status": "resolving", "bytes_written": 0, "bytes_total": 0,
                "speed": "0 KB/s", "error_msg": "", "release": "",
            })
        _tag, server = fetch_llama_release(build_type, DOWNLOAD_STATE, DOWNLOAD_LOCK)
        settings = get_settings()
        settings["llama_bin"] = str(server.resolve())
        save_json(SETTINGS_FILE, settings)
        broadcast_event({"type": "settings:update"})
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE.update({
                "status": "done", "bytes_written": DOWNLOAD_STATE.get("bytes_total") or 0,
                "speed": DOWNLOAD_STATE.get("speed") or "0 KB/s",
            })
    except Exception as exc:
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE.update({"status": "failed", "error_msg": str(exc)})
