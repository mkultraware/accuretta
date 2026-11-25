# SPDX-License-Identifier: AGPL-3.0-only
"""Ending a process together with every child it started.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess
from accuretta.core.windows_bin import _trusted_windows_binary
from accuretta.runtime.wsl_sandbox import SANDBOX_DISTRO
from accuretta.runtime.wsl_sandbox import _wsl_run
from accuretta.security.child_env import _child_env


def _terminate_process_tree(proc: subprocess.Popen, target: str) -> bool:
    if proc.poll() is not None:
        return True
    if target == "sandbox":
        rc, _ = _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=30)
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return rc == 0 or proc.poll() is not None
    if os.name == "nt":
        taskkill = _trusted_windows_binary("taskkill.exe")
        if taskkill:
            try:
                result = subprocess.run(
                    [taskkill, "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, text=True, timeout=30, env=_child_env(),
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                if result.returncode == 0 or proc.poll() is not None:
                    return True
            except Exception:
                pass
    try:
        proc.kill()
        proc.wait(timeout=5)
        return True
    except Exception:
        return proc.poll() is not None
