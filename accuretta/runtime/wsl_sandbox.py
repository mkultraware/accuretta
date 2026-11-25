# SPDX-License-Identifier: AGPL-3.0-only
"""The WSL sandbox: its distro name, running commands inside it, and stopping only our
own distro when Accuretta exits.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess
import threading
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.core.windows_bin import _trusted_windows_binary
from accuretta.security.child_env import _child_env


from accuretta.core.sandbox_distro import (  # noqa: F401  re-export: moved 2026-11-23
    SANDBOX_DISTRO,
)


def _wsl_exe() -> str:
    return _trusted_windows_binary("wsl.exe")


_sandbox_exit_stop_lock = threading.Lock()


_sandbox_exit_stop_registered = False


def _stop_sandbox_on_exit() -> None:
    """Stop the sandbox guest as this process exits. Only our own distro is
    terminated; `wsl --shutdown` would also stop Docker and the user's other
    distros. WSL then lets its idle VM go on its own."""
    try:
        _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=15)
    except Exception:
        pass


def _note_sandbox_use() -> None:
    """Remember that this process started the sandbox guest, so it is stopped
    when the process exits. Registered on first use rather than in main(), so
    a script that imports the bridge and runs sandbox commands (a benchmark,
    a dev check) also stops the guest instead of leaving it running."""
    global _sandbox_exit_stop_registered
    with _sandbox_exit_stop_lock:
        if _sandbox_exit_stop_registered:
            return
        _sandbox_exit_stop_registered = True
    import atexit
    atexit.register(_stop_sandbox_on_exit)


def _wsl_args_enter_sandbox(args) -> bool:
    """True when wsl.exe args run something inside the sandbox guest."""
    args = list(args or [])
    for flag in ("-d", "--distribution"):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args) and args[i + 1] == SANDBOX_DISTRO:
                return True
    return False


def _wsl_env() -> dict:
    """WSL_UTF8 makes wsl.exe emit UTF-8; WSLENV (when the user set one) says
    which Windows variables cross into the distro."""
    extra = {"WSL_UTF8": "1"}
    if os.environ.get("WSLENV"):
        extra["WSLENV"] = os.environ["WSLENV"]
    return _child_env(extra)


def _wsl_run(args, timeout=60):
    """Run a wsl.exe command, return (returncode, combined_output).
    WSL_UTF8=1 makes wsl.exe emit UTF-8 (its native output is UTF-16LE, which
    otherwise arrives full of NUL bytes)."""
    env = _wsl_env()
    executable = _wsl_exe()
    if not executable:
        return 127, "trusted wsl.exe was not found in the Windows system directory"
    if _wsl_args_enter_sandbox(args):
        _note_sandbox_use()
    try:
        r = subprocess.run([executable, *args], capture_output=True, text=True,
                           timeout=timeout, env=env, creationflags=_NO_WINDOW,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "wsl not found"
    except Exception as e:
        return 1, str(e)
