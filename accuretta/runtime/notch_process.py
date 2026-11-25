# SPDX-License-Identifier: AGPL-3.0-only
"""Starting and stopping the notch overlay's host process.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import sys
import threading
from accuretta.core.endpoints import PORT
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.security.child_env import _child_env
from pathlib import Path
from accuretta.core.settings import get_settings


_notch_proc = None


_notch_lock = threading.Lock()


def _notch_host_script() -> Path | None:
    """The overlay host for this platform, or None where there is none."""
    if sys.platform == "win32":
        return ROOT / "notch_host.py"
    if sys.platform == "darwin":
        return ROOT / "notch_host_mac.py"
    return None


def _notch_host_available() -> bool:
    script = _notch_host_script()
    if script is None or not script.is_file() or not (ROOT / "accuretta-notch.html").is_file():
        return False
    try:
        import importlib.util
        if sys.platform == "darwin":
            return all(importlib.util.find_spec(m) is not None for m in ("AppKit", "WebKit", "objc"))
        spec = importlib.util.find_spec("webview")
        if not (spec and spec.submodule_search_locations):
            return False
        lib = Path(list(spec.submodule_search_locations)[0]) / "lib" / "Microsoft.Web.WebView2.Core.dll"
        return lib.is_file()
    except Exception:
        return False


def notch_start() -> bool:
    """Spawn the overlay host process (idempotent)."""
    global _notch_proc
    import subprocess
    with _notch_lock:
        if _notch_proc is not None and _notch_proc.poll() is None:
            return True
        if not _notch_host_available():
            return False
        env = _child_env({"ACCURETTA_PORT": str(PORT), "ACCURETTA_DATA_DIR": str(DATA)}, keep=("dev",))
        try:
            _notch_proc = subprocess.Popen(
                [sys.executable, str(_notch_host_script())],
                cwd=str(ROOT), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception:
            _notch_proc = None
            return False


def notch_stop() -> None:
    global _notch_proc
    with _notch_lock:
        proc, _notch_proc = _notch_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def notch_sync() -> None:
    """Start/stop the overlay so it matches the notch_enabled setting."""
    if get_settings().get("notch_enabled"):
        notch_start()
    else:
        notch_stop()
