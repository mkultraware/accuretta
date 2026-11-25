# SPDX-License-Identifier: AGPL-3.0-only
"""Talking to a paired remote machine over SSH: finding ssh, the pinned known-hosts
file, building the command line, running a remote process with live output, and checking
a new pairing.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import posixpath
import re
import shlex
import subprocess
import time
from accuretta.core.chats import _get_current_chat
from accuretta.core.settings import get_settings
from accuretta.core.tool_stream import _append_cmd_history
from accuretta.core.tool_stream import _emit_tool_stream
from accuretta.runtime.command_kill import _killed_procs
from accuretta.runtime.command_kill import _running_cmds
from accuretta.runtime.command_kill import _running_cmds_lock
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.tools.remote_helpers import _public_remote_machine
from accuretta.tools.remote_helpers import _resolved_remote_roots
from accuretta.tools.remote_machines import REMOTE_DIR
from accuretta.tools.remote_machines import _load_remote_machines
from accuretta.tools.remote_machines import _remote_machine
from accuretta.tools.remote_pairing import _REMOTE_USER_RE
from accuretta.tools.remote_pairing import _find_executable
from accuretta.tools.remote_pairing import _refresh_remote_machine
from accuretta.tools.remote_pairing import _save_remote_machines


REMOTE_KNOWN_HOSTS_FILE = REMOTE_DIR / "known_hosts"


def _ssh_exe() -> str:
    candidates = ("ssh.exe", "ssh") if os.name == "nt" else ("ssh",)
    return _find_executable(candidates)


def _remote_ssh_argv(row: dict, *, accept_new: bool = False) -> list[str]:
    ssh = _ssh_exe()
    if not ssh:
        return []
    host = str(row.get("ip") or row.get("dns_name") or "").strip()
    username = str(row.get("username") or "").strip()
    key_path = str(row.get("key_path") or "").strip()
    if not host or not _REMOTE_USER_RE.fullmatch(username) or not os.path.isfile(key_path):
        return []
    REMOTE_KNOWN_HOSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    return [
        ssh,
        "-T",
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", "KbdInteractiveAuthentication=no",
        "-o", "IdentitiesOnly=yes",
        "-o", "ConnectTimeout=12",
        "-o", f"UserKnownHostsFile={REMOTE_KNOWN_HOSTS_FILE}",
        "-o", f"StrictHostKeyChecking={'accept-new' if accept_new else 'yes'}",
        "-i", key_path,
        f"{username}@{host}",
    ]


def _run_remote_process(row: dict, command: str, *, timeout: int | None = None,
                        stdin: bytes | None = None, max_stdout: int = 200_000,
                        accept_new: bool = False, stream_name: str = "remote_shell") -> dict:
    row = _refresh_remote_machine(row)
    argv = _remote_ssh_argv(row, accept_new=accept_new)
    if not argv:
        return {"ok": False, "error": "SSH is unavailable or this pairing is incomplete."}
    timeout = max(1, min(int(timeout or get_settings().get("remote_command_timeout") or 120), 3600))
    started = time.time()
    try:
        proc = subprocess.Popen(
            [*argv, command], stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_child_env(keep=_ENV_TOOLS),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    cid = _get_current_chat() or ""
    if cid:
        with _running_cmds_lock:
            _running_cmds[cid] = proc
    try:
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return {"ok": False, "error": f"Remote command timed out after {timeout}s.", "timed_out": True}
        with _running_cmds_lock:
            killed = proc in _killed_procs or (proc.returncode is not None and proc.returncode < 0)
        stdout = stdout or b""
        stderr = stderr or b""
        result = {
            "ok": proc.returncode == 0 and not killed,
            "exit": proc.returncode,
            "stdout_bytes": stdout[-max_stdout:],
            "stderr": stderr[-20_000:].decode("utf-8", "replace"),
            "duration_ms": int((time.time() - started) * 1000),
            "machine": _public_remote_machine(row),
        }
        if killed:
            result.update({"ok": False, "killed": True, "error": "Remote command was stopped."})
        if stream_name and stdout:
            text_out = stdout[-min(len(stdout), 16_000):].decode("utf-8", "replace")
            for line in text_out.splitlines()[-120:]:
                _emit_tool_stream(stream_name, line)
        _append_cmd_history({
            "ts": int(started * 1000), "chat_id": cid, "target": row.get("label"),
            "command": command, "exit": proc.returncode, "ok": result["ok"],
            "duration_ms": result["duration_ms"],
            "stdout": stdout[-16_000:].decode("utf-8", "replace"),
            "stderr": result["stderr"], "remote": True, "killed": killed,
        })
        return result
    finally:
        with _running_cmds_lock:
            if cid and _running_cmds.get(cid) is proc:
                _running_cmds.pop(cid, None)
            _killed_procs.discard(proc)


def verify_remote_pairing(machine_id: str) -> dict:
    row = _remote_machine(machine_id)
    if not row:
        return {"ok": False, "error": "Pairing not found."}
    row = _refresh_remote_machine(row)
    marker = "__ACCURETTA_OK__"
    command = (
        f"printf '{marker}\\n'; sw_vers -productVersion 2>/dev/null; "
        "printf '__HOME__%s\\n' \"$HOME\""
    )
    result = _run_remote_process(row, command, timeout=25, accept_new=True, max_stdout=40_000, stream_name="")
    text_out = (result.get("stdout_bytes") or b"").decode("utf-8", "replace")
    if not result.get("ok") or marker not in text_out:
        error = result.get("stderr") or result.get("error") or "The Mac did not accept the dedicated key."
        return {"ok": False, "error": error.strip()[:2000], "machine": _public_remote_machine(row)}
    home_match = re.search(r"^__HOME__(/[^\r\n]+)$", text_out, re.MULTILINE)
    if not home_match:
        return {"ok": False, "error": "SSH worked, but the Mac home folder could not be verified."}
    row["home"] = posixpath.normpath(home_match.group(1).strip())
    requested_roots = _resolved_remote_roots(row)
    resolved_roots = []
    missing_roots = []
    for root in requested_roots:
        root_result = _run_remote_process(
            row, f"test -d {shlex.quote(root)} && cd -P -- {shlex.quote(root)} && pwd -P",
            timeout=20, max_stdout=4096, stream_name="",
        )
        resolved = (root_result.get("stdout_bytes") or b"").decode("utf-8", "replace").strip().splitlines()
        if root_result.get("ok") and resolved and resolved[-1].startswith("/"):
            real_root = posixpath.normpath(resolved[-1])
            if real_root not in resolved_roots:
                resolved_roots.append(real_root)
        else:
            missing_roots.append(root)
    if missing_roots:
        return {
            "ok": False,
            "error": "These allowed Mac folders do not exist: " + ", ".join(missing_roots),
            "machine": _public_remote_machine(row),
        }
    row["resolved_roots"] = resolved_roots
    row["status"] = "ready"
    row["last_verified"] = int(time.time())
    data = _load_remote_machines()
    data["machines"] = [row if m.get("id") == machine_id else m for m in data["machines"]]
    _save_remote_machines(data)
    return {"ok": True, "machine": _public_remote_machine(row), "detail": "Dedicated-key SSH verified."}
