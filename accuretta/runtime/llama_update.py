# SPDX-License-Identifier: AGPL-3.0-only
"""Updating llama.cpp from inside the app.

The first-run wizard downloads llama.cpp once; this keeps it current. An update
downloads the newest official release for the same backend (CUDA, Vulkan or CPU)
into the app's own folder, checks that the new build runs and knows the loaded
model's architecture, then switches to it and restarts the model on it. When the
model doesn't come up on the new build, the old one is put back and the model
restarted on it. The previous build is kept as a fallback; older ones the app
downloaded itself are removed. Builds the user installed elsewhere are never
touched.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from accuretta.core.endpoints import LLAMA
from accuretta.core.events import broadcast_event
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import _offline_active
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.llama_bin import find_llama_bin
from accuretta.runtime.llama_bin import llama_knows_arch
from accuretta.runtime.llama_download import MANAGED_LLAMA_DIR
from accuretta.runtime.llama_download import fetch_llama_release
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_releases import _latest_llama_release_assets
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.settings_update import _SETTINGS_LOCK
from accuretta.security.child_env import _child_env


UPDATE_LOCK = threading.Lock()


# The running or last update. status: idle, checking, downloading, extracting,
# testing, waiting (for a reply to finish), switching, done, up_to_date, failed.
UPDATE_STATE: dict = {"status": "idle"}


_RUNNING = {"checking", "downloading", "extracting", "testing", "waiting", "switching"}


# The newest release per backend, looked up on request and kept for a while so
# opening Settings doesn't call GitHub every time.
_LATEST: dict = {}


_LATEST_TTL_S = 10 * 60


_BUILD_CACHE: dict = {}


def _tag_build(tag: str) -> int:
    m = re.fullmatch(r"b(\d+)", str(tag or "").strip())
    return int(m.group(1)) if m else 0


def llama_build_kind(server: str) -> str:
    """CUDA, VULKAN or CPU, from the backend libraries next to llama-server."""
    folder = Path(server or "").parent
    try:
        names = {p.name.lower() for p in folder.iterdir()}
    except OSError:
        return ""
    if any(n.startswith(("ggml-cuda", "libggml-cuda")) for n in names):
        return "CUDA"
    if any(n.startswith(("ggml-vulkan", "libggml-vulkan")) for n in names):
        return "VULKAN"
    return "CPU"


def llama_build_number(server: str) -> int:
    """The build number llama-server reports (`--version`), 0 when it can't be
    told. Cached per file version: it only changes when the file does."""
    if not server:
        return 0
    try:
        st = os.stat(server)
    except OSError:
        return 0
    key = (str(server), st.st_mtime, st.st_size)
    if key in _BUILD_CACHE:
        return _BUILD_CACHE[key]
    text = ""
    try:
        out = subprocess.run([str(server), "--version"], capture_output=True, text=True, timeout=30,
                             env=_child_env(keep=("gpu",)), cwd=str(Path(server).parent),
                             creationflags=0x08000000 if os.name == "nt" else 0)
        text = (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        text = ""
    # New builds: "version: 1.0.0-dev (build 12669, commit 30ec35ae2)".
    # Older ones: "version: 12171 (abc1234)".
    m = re.search(r"\(build\s+(\d+)", text) or re.search(r"version:\s*(\d+)\b", text)
    build = int(m.group(1)) if m else 0
    if not build:
        m = re.search(r"(?:^|[\\/._-])b(\d{4,6})(?=$|[\\/._-])", str(server))
        build = int(m.group(1)) if m and text else 0
    if text:
        _BUILD_CACHE[key] = build
    return build


def _install_root(server: str) -> Path | None:
    """The app's own folder for one downloaded build (releases/<tag>/<kind>),
    or None for a build the user put somewhere else."""
    try:
        rel = Path(server).resolve().relative_to(MANAGED_LLAMA_DIR.resolve())
    except (OSError, ValueError):
        return None
    return MANAGED_LLAMA_DIR / rel.parts[0] / rel.parts[1] if len(rel.parts) >= 3 else None


def _latest(kind: str, force: bool = False) -> dict:
    now = time.time()
    cached = _LATEST.get(kind)
    if cached and not force and now - cached["checked_at"] < _LATEST_TTL_S:
        return cached
    try:
        tag, assets = _latest_llama_release_assets(kind)
        entry = {"tag": tag, "assets": assets, "checked_at": now, "error": ""}
    except Exception as exc:
        entry = {"tag": (cached or {}).get("tag", ""), "assets": (cached or {}).get("assets", []),
                 "checked_at": now, "error": str(exc)[:200]}
    _LATEST[kind] = entry
    return entry


def llama_update_status(check: bool = False) -> dict:
    """What Settings shows: the build in use and, once looked up, the newest one."""
    server = find_llama_bin() or ""
    kind = llama_build_kind(server) if server else ""
    build = llama_build_number(server) if server else 0
    supported = os.name == "nt"
    if check and supported and server and not _offline_active:
        _latest(kind, force=True)
    latest = _LATEST.get(kind) or {}
    latest_build = _tag_build(latest.get("tag", ""))
    with UPDATE_LOCK:
        job = dict(UPDATE_STATE)
    return {
        "supported": supported,
        "offline": bool(_offline_active),
        "path": server,
        "kind": kind,
        "build": build,
        "managed": _install_root(server) is not None if server else False,
        "latest": latest.get("tag", ""),
        "latest_build": latest_build,
        "available": bool(latest_build and latest_build > build),
        "download_mb": round(sum(int(a.get("size") or 0) for a in latest.get("assets") or []) / 1e6),
        "checked_at": int(latest.get("checked_at") or 0),
        "check_error": latest.get("error", ""),
        "job": job,
    }


def _model_busy() -> bool:
    """Whether the model is writing a reply right now."""
    try:
        with urllib.request.urlopen(f"{LLAMA}/slots", timeout=3) as r:
            slots = json.loads(r.read().decode("utf-8", "replace"))
        return any(isinstance(s, dict) and s.get("is_processing") for s in slots or [])
    except Exception:
        return False


def _set(**fields) -> None:
    with UPDATE_LOCK:
        UPDATE_STATE.update(fields)


def _save_llama_bin(path: str) -> None:
    with _SETTINGS_LOCK:
        s = get_settings()
        s["llama_bin"] = path
        save_json(SETTINGS_FILE, s)
    broadcast_event({"type": "settings:update"})


def _wait_seconds(model: str) -> int:
    try:
        size_gb = os.path.getsize(model) / 1e9
    except OSError:
        size_gb = 0.0
    return max(120, min(900, int(size_gb * 20)))


def _wait_until_idle(limit_s: float = 600.0) -> bool:
    end = time.time() + limit_s
    while time.time() < end:
        if not _model_busy() and not getattr(_llama, "_starting", False):
            return True
        time.sleep(2)
    return False


def _prune(keep: list[str]) -> None:
    """Remove builds the app downloaded itself, except the ones in `keep`."""
    keep_roots = {str(r) for r in (_install_root(p) for p in keep) if r is not None}
    try:
        tags = [t for t in MANAGED_LLAMA_DIR.iterdir() if t.is_dir()]
    except OSError:
        return
    for tag_dir in tags:
        for kind_dir in [k for k in tag_dir.iterdir() if k.is_dir()]:
            if str(kind_dir) not in keep_roots:
                shutil.rmtree(kind_dir, ignore_errors=True)
        try:
            tag_dir.rmdir()          # only when it's empty now
        except OSError:
            pass


def _run_update(old_server: str) -> None:
    new_server = ""
    try:
        kind = llama_build_kind(old_server) or "CPU"
        old_build = llama_build_number(old_server)
        latest = _latest(kind, force=True)
        if latest.get("error") and not latest.get("tag"):
            raise RuntimeError(f"couldn't look up the newest llama.cpp: {latest['error']}")
        tag, new_build = latest["tag"], _tag_build(latest["tag"])
        _set(from_build=old_build, to_build=new_build, release=tag)
        if old_build and new_build <= old_build:
            _set(status="up_to_date", message=f"Already on the newest llama.cpp (b{old_build}).",
                 finished_at=int(time.time()))
            return

        tag, server = fetch_llama_release(kind, UPDATE_STATE, UPDATE_LOCK, release=(tag, latest["assets"]))
        new_server = str(server.resolve())

        _set(status="testing")
        got = llama_build_number(new_server)
        if not got:
            raise RuntimeError("the new build didn't start (llama-server --version failed)")
        if new_build and got != new_build:
            raise RuntimeError(f"the new build says it is b{got}, not b{new_build}")
        model = _llama.loaded_model()
        on_llama = bool(model) and not _maya_engine_for(model)
        if on_llama:
            arch = str((read_gguf_metadata(model, max_bytes=1024 * 1024, retry_bytes=0)
                        .get("keys") or {}).get("general.architecture") or "")
            if arch and llama_knows_arch(arch, new_server) is False:
                raise RuntimeError(f"the new build doesn't know your model's architecture ({arch})")

        _set(status="waiting")
        if on_llama and not _wait_until_idle():
            raise RuntimeError("the model stayed busy for 10 minutes; try again when it's idle")

        _set(status="switching")
        _save_llama_bin(new_server)
        if on_llama:
            res = _llama.start(model, wait_seconds=_wait_seconds(model))
            if not res.get("ok"):
                reason = str(res.get("error") or "it didn't start")[:200]
                _save_llama_bin(old_server)
                back = _llama.start(model, wait_seconds=_wait_seconds(model))
                broadcast_event({"type": "models:update", "loaded_model": model if back.get("ok") else ""})
                raise RuntimeError(f"your model didn't start on b{got} ({reason}); went back to "
                                   f"b{old_build or '?'}" + ("" if back.get("ok") else ", which didn't start either"))
            broadcast_event({"type": "models:update", "loaded_model": model})
        _prune([new_server, old_server])
        _set(status="done", message=f"llama.cpp updated from b{old_build or '?'} to b{got}.",
             finished_at=int(time.time()))
    except Exception as exc:
        root = _install_root(new_server) if new_server else None
        if root is not None and root != _install_root(old_server):
            shutil.rmtree(root, ignore_errors=True)
        _set(status="failed", message=f"Update failed: {exc}", finished_at=int(time.time()))
    finally:
        broadcast_event({"type": "llama:update"})


def start_llama_update() -> tuple[int, dict]:
    """Start an update in the background. Returns (HTTP status, body)."""
    if os.name != "nt":
        return 409, {"ok": False, "error": "Updating llama.cpp from the app works on Windows only."}
    if _offline_active:
        return 409, {"ok": False, "error": "Offline mode is on."}
    server = find_llama_bin() or ""
    if not server:
        return 409, {"ok": False, "error": "No llama.cpp found. Run the setup to download one."}
    with UPDATE_LOCK:
        if UPDATE_STATE.get("status") in _RUNNING:
            return 409, {"ok": False, "error": "An update is already running."}
        UPDATE_STATE.clear()
        UPDATE_STATE.update({"status": "checking", "bytes_written": 0, "bytes_total": 0,
                             "speed": "", "message": "", "started_at": int(time.time())})
    threading.Thread(target=_run_update, args=(server,), daemon=True, name="llama-update").start()
    return 200, {"ok": True}
