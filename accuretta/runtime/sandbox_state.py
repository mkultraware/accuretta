# SPDX-License-Identifier: AGPL-3.0-only
"""The sandbox's state: its state file and security profile, the WSL distros present,
whether it is ready, and streaming its provisioning.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import io as _io
import subprocess
import threading
import time
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.runtime.sandbox_rootfs import SANDBOX_LOCK
from accuretta.runtime.sandbox_rootfs import SANDBOX_PROGRESS
from accuretta.runtime.sandbox_rootfs import _sbx_log
from accuretta.runtime.wsl_sandbox import SANDBOX_DISTRO
from accuretta.runtime.wsl_sandbox import _note_sandbox_use
from accuretta.runtime.wsl_sandbox import _wsl_env
from accuretta.runtime.wsl_sandbox import _wsl_exe
from accuretta.runtime.wsl_sandbox import _wsl_run


SANDBOX_STATE_FILE = DATA / "sandbox_state.json"


SANDBOX_SECURITY_PROFILE = 3


def _write_wsl_script(stream, script: str) -> None:
    """Write a Bash script to WSL with Unix line endings on every host."""
    script = str(script or "").replace("\r\n", "\n").replace("\r", "\n")
    try:
        stream.reconfigure(newline="\n")
        stream.write(script)
    except (AttributeError, _io.UnsupportedOperation):
        raw = getattr(stream, "buffer", None)
        if raw is None:
            stream.write(script)
        else:
            raw.write(script.encode("utf-8"))
            raw.flush()


def _wsl_distros() -> list:
    """Names of installed distros (empty if WSL absent)."""
    rc, out = _wsl_run(["-l", "-q"], timeout=15)
    if rc != 0:
        return []
    names = []
    for line in out.replace("\x00", "").splitlines():
        n = line.strip()
        if n and not n.lower().startswith("windows subsystem"):
            names.append(n)
    return names


def wsl_probe() -> dict:
    """Cheap snapshot of WSL + sandbox state for the setup wizard and settings.
    Safe to call on a poll."""
    info = {
        "wsl_installed": False, "wsl_version": "", "kernel": "",
        "distros": [], "sandbox_distro": SANDBOX_DISTRO,
        "sandbox_present": False, "sandbox_provisioned": False,
        "security_profile": 0, "interop_disabled": False,
        "ready": False, "state": "no_wsl", "provision": None,
    }
    rc, out = _wsl_run(["--version"], timeout=15)
    if rc == 0 and out.strip():
        info["wsl_installed"] = True
        for line in out.splitlines():
            low = line.lower()
            if "wsl version" in low:
                info["wsl_version"] = line.split(":", 1)[-1].strip()
            elif "kernel version" in low:
                info["kernel"] = line.split(":", 1)[-1].strip()
    else:
        rc2, _ = _wsl_run(["--status"], timeout=15)
        info["wsl_installed"] = (rc2 == 0)
    if info["wsl_installed"]:
        info["distros"] = _wsl_distros()
        info["sandbox_present"] = SANDBOX_DISTRO in info["distros"]
    st = load_json(SANDBOX_STATE_FILE, {})
    info["sandbox_provisioned"] = bool(st.get("provisioned")) and info["sandbox_present"]
    info["security_profile"] = int(st.get("security_profile") or 0)
    info["interop_disabled"] = bool(st.get("interop_disabled"))
    if not info["wsl_installed"]:
        info["state"] = "no_wsl"
    elif not info["sandbox_present"]:
        info["state"] = "no_distro"
    elif not info["sandbox_provisioned"]:
        info["state"] = "present_unprovisioned"
    elif (info["security_profile"] < SANDBOX_SECURITY_PROFILE
          or not info["interop_disabled"]):
        info["state"] = "needs_hardening"
    else:
        info["state"] = "ready"
    info["ready"] = info["state"] == "ready"
    with SANDBOX_LOCK:
        info["provision"] = dict(SANDBOX_PROGRESS, log=list(SANDBOX_PROGRESS["log"][-40:]))
    return info


def _sbx_stream_provision(script, timeout=1800) -> int:
    """Run a provisioning script in the guest, streaming stdout into the log."""
    env = _wsl_env()
    executable = _wsl_exe()
    if not executable:
        _sbx_log("trusted wsl.exe was not found in the Windows system directory")
        return 127
    _note_sandbox_use()
    cmd = [
        executable, "-d", SANDBOX_DISTRO, "-u", "root", "--",
        "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
        "/bin/bash", "--noprofile", "--norc",
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env, creationflags=_NO_WINDOW, bufsize=1,
                         encoding="utf-8", errors="replace")
    _write_wsl_script(p.stdin, script)
    p.stdin.close()

    def _reader():
        try:
            for line in iter(p.stdout.readline, ""):
                _sbx_log(line.rstrip())
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _sbx_log(f"provision timed out after {timeout}s")
            _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=30)
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    p.kill()
                    p.wait(timeout=5)
                except Exception:
                    pass
    finally:
        reader.join(timeout=5)
    if timed_out:
        return 124
    return p.returncode if p.returncode is not None else 1


# --- model-facing sandbox tool ----------------------------------------------
# `sandbox_run` is only advertised to the model once the guest is provisioned
# (see _active_tools). Probing WSL is a subprocess, too slow to run every turn,
# so readiness is cached with a short TTL and short-circuited by the state file
# (no probe at all until the user has actually provisioned).
_SBX_READY_CACHE = {"t": 0.0, "ready": False}


def _sandbox_ready_cached(ttl: float = 30.0) -> bool:
    now = time.time()
    if now - _SBX_READY_CACHE["t"] < ttl:
        return _SBX_READY_CACHE["ready"]
    state = load_json(SANDBOX_STATE_FILE, {})
    if (not state.get("provisioned")
            or int(state.get("security_profile") or 0) < SANDBOX_SECURITY_PROFILE
            or not state.get("interop_disabled")):
        _SBX_READY_CACHE.update(t=now, ready=False)
        return False
    ready = bool(wsl_probe().get("ready"))
    _SBX_READY_CACHE.update(t=now, ready=ready)
    return ready
