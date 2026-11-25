# SPDX-License-Identifier: AGPL-3.0-only
"""Interactive sessions: a shell or REPL the agent keeps open, writes to and reads from
across tool calls, and stops.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import codecs
import os
import re
import subprocess
import sys
import threading
import time
from accuretta.core.paths import ROOT
from accuretta.core.windows_bin import _trusted_windows_binary
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.security.agent_process_note import _note_agent_process
from accuretta.security.approvals import request_approval
from accuretta.security.danger_gate import _danger_gate
from accuretta.security.hard_blocks import _catastrophic_cmd
from accuretta.security.hard_blocks import _clickfix_cmd
from accuretta.security.path_guards import is_blocked_path
from accuretta.security.self_protect import bridge_self_threat
from accuretta.security.tool_env import _tool_child_env


# ---- interactive sessions --------------------------------------------------
# A long-lived child process that the agent and the user (via the shared Shell
# tab) drive statefully across many tool calls: reverse shells, ssh, REPLs,
# debuggers, DB clients. A reader thread drains output into a capped buffer
# with absolute offsets, so the model (its cursor) and the UI (its since=) can
# each tail it. Pipes, not a PTY: line-based interaction works everywhere, but
# full-screen TUIs and TTY-only password prompts may misbehave.
_SESSION_BUF_CAP = 200_000   # chars kept per session


class Session:
    def __init__(self, sid: str, command: str):
        self.id = sid
        self.command = command
        self.created = time.time()
        self._proc: subprocess.Popen | None = None
        self._out = ""      # recent output (front-trimmed at the cap)
        self._base = 0      # absolute offset of _out[0] (chars already dropped)
        self._cursor = 0    # the model's read cursor (absolute)
        self._lock = threading.Lock()

    def start(self, cwd: str | None = None) -> None:
        shell_executable = None
        if sys.platform == "win32":
            shell_executable = _trusted_windows_binary("cmd.exe")
            if not shell_executable:
                raise RuntimeError(
                    "Trusted Windows command processor was not found in the system directory."
                )
        # Binary pipes, read in chunks: a prompt that ends without a newline
        # (">>> ", "(gdb) ", "Password:") shows up at once instead of waiting
        # behind readline for a line that never comes. Python output is
        # unbuffered so a script's prints arrive as they happen.
        self._proc = subprocess.Popen(
            self.command,
            shell=True,
            executable=shell_executable,
            cwd=cwd or None,
            env=_tool_child_env(PYTHONUNBUFFERED="1"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        _note_agent_process(self._proc)
        threading.Thread(target=self._pump, name=f"session-{self.id}", daemon=True).start()

    def _append(self, text: str) -> None:
        with self._lock:
            self._out += text
            if len(self._out) > _SESSION_BUF_CAP:
                drop = len(self._out) - _SESSION_BUF_CAP
                self._out = self._out[drop:]
                self._base += drop

    def _pump(self) -> None:
        try:
            if not self._proc or not self._proc.stdout:
                return
            fd = self._proc.stdout.fileno()
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            held = ""
            while True:
                chunk = os.read(fd, 65536)
                text = held + decoder.decode(chunk, final=not chunk)
                # Hold a trailing \r back: its \n may be in the next chunk.
                held = text[-1:] if chunk and text.endswith("\r") else ""
                if held:
                    text = text[:-1]
                if text:
                    self._append(text.replace("\r\n", "\n").replace("\r", "\n"))
                if not chunk:
                    break
        except Exception:
            pass

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_code(self):
        return None if self._proc is None else self._proc.poll()

    def send(self, text: str, enter: bool = True) -> bool:
        if not self.alive() or not self._proc or not self._proc.stdin:
            return False
        try:
            data = text + ("\n" if enter and not text.endswith("\n") else "")
            self._proc.stdin.write(data.encode("utf-8"))
            self._proc.stdin.flush()
            self._append(data)
            return True
        except Exception:
            return False

    def read_since(self, offset: int):
        with self._lock:
            start = max(0, offset - self._base)
            return self._out[start:], self._base + len(self._out)

    def read_model(self, cap: int = 12000) -> str:
        text, newcur = self.read_since(self._cursor)
        self._cursor = newcur
        if len(text) > cap:
            text = "… [earlier output truncated — read again for more] …\n" + text[-cap:]
        return text

    def stop(self) -> bool:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return True
        try:
            _terminate_process_tree(proc, "host")
        except Exception:
            return proc.poll() is not None
        return proc.poll() is not None

    def info(self) -> dict:
        return {"id": self.id, "command": self.command, "alive": self.alive(),
                "exit_code": self.exit_code(), "created": self.created}


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._n = 0

    def create(self, command: str, cwd: str | None = None) -> Session:
        with self._lock:
            self._n += 1
            sid = f"s{self._n}"
        s = Session(sid, command)
        s.start(cwd=cwd)
        with self._lock:
            self._sessions[sid] = s
        return s

    def get(self, sid: str | None) -> Session | None:
        with self._lock:
            return self._sessions.get(sid or "")

    def list(self) -> list:
        with self._lock:
            return [s.info() for s in self._sessions.values()]

    def stop(self, sid: str) -> bool:
        s = self.get(sid)
        if s:
            return s.stop()
        return False

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.stop()


_session_mgr = SessionManager()


def _session_wait(ms) -> None:
    try:
        ms = int(ms)
    except Exception:
        ms = 600
    ms = max(0, min(ms, 6000))
    if ms:
        time.sleep(ms / 1000.0)


def _session_not_executed(message: str, **details) -> dict:
    result = {
        "error": message,
        "not_executed": True,
        "executed": False,
        "execution_target": "host",
    }
    result.update(details)
    return result


def _session_workspace_cwd(requested: str = "") -> tuple[str, str]:
    configured = [
        normalize_path(folder)
        for folder in (get_workspace().get("folders", []) or [])
        if isinstance(folder, str) and folder.strip()
    ]
    roots = configured or [normalize_path(str(ROOT))]
    cwd = normalize_path(requested or roots[0])
    normalized_cwd = os.path.normcase(os.path.abspath(cwd))
    inside = False
    for root in roots:
        normalized_root = os.path.normcase(os.path.abspath(root))
        try:
            if os.path.commonpath([normalized_cwd, normalized_root]) == normalized_root:
                inside = True
                break
        except ValueError:
            continue
    if not inside or is_blocked_path(cwd):
        return "", "CWD is outside configured workspace bounds."
    if not os.path.isdir(cwd):
        return "", f"CWD is not a directory: {cwd}"
    return cwd, ""


# A bare `python` or `node` reading a pipe is not a REPL: Python waits for the
# end of input and runs it all as one script, so nothing ever came back. These
# flags make them interactive (Python then prints its >>> prompts too).
_SESSION_PY_REPL_RE = re.compile(
    r'^\s*"?(?:[^"\s]*[\\/])?(?:python[0-9.]*|py)(?:\.exe)?"?(?:\s+-3(?:\.\d+)?)?\s*$', re.IGNORECASE)


_SESSION_NODE_REPL_RE = re.compile(r'^\s*"?(?:[^"\s]*[\\/])?node(?:\.exe)?"?\s*$', re.IGNORECASE)


def _session_repl_command(command: str) -> str:
    if _SESSION_PY_REPL_RE.match(command):
        return command.rstrip() + " -i -u"
    if _SESSION_NODE_REPL_RE.match(command):
        return command.rstrip() + " -i"
    return command


def tool_session_start(args: dict) -> dict:
    command = (args.get("command") or ("powershell" if sys.platform == "win32" else "bash")).strip()
    if not command:
        return _session_not_executed("empty command")
    command = _session_repl_command(command)
    cwd, cwd_error = _session_workspace_cwd(str(args.get("cwd") or ""))
    if cwd_error:
        return _session_not_executed(cwd_error, scope_blocked=True)
    if sys.platform == "win32" and command.lower() in {"powershell", "powershell.exe"}:
        powershell_exe = _trusted_windows_binary("WindowsPowerShell", "v1.0", "powershell.exe")
        if not powershell_exe:
            return _session_not_executed(
                "Trusted Windows PowerShell executable was not found.", spawn_error=True)
        command = f'"{powershell_exe}" -NoLogo -NoProfile'
    threat = bridge_self_threat(command)
    if threat:
        return _session_not_executed(
            f"refused: {threat}", refused_command=command, self_protection=True)
    catastrophic = _catastrophic_cmd(command)
    if catastrophic:
        return _session_not_executed(
            f"refused: this command {catastrophic}. Run it yourself if you truly mean to.",
            refused_command=command, catastrophic=True)
    clickfix = _clickfix_cmd(command)
    if clickfix:
        return _session_not_executed(
            f"refused: this command {clickfix} (ClickFix signature). Run it yourself if you truly mean to.",
            refused_command=command, clickfix=True)
    danger = _danger_gate(command, "SECURITY CHANGE (hold to approve)")
    if danger and not danger.get("approved"):
        return _session_not_executed(danger["error"], refused_command=command)
    approval = request_approval(
        title="Start persistent process on Windows host",
        command=command,
        details={
            "kind": "session", "target": "host", "cwd": cwd,
            "executes_code": True,
            "red_team_context": bool(_rt_authorized_mission(_rt_context_chat())),
        },
    )
    if approval.get("decision") != "approve":
        return _session_not_executed(
            f"user denied session ({approval.get('status')})")
    try:
        s = _session_mgr.create(command, cwd=cwd)
    except Exception as e:
        return _session_not_executed(str(e), spawn_error=True)
    _session_wait(args.get("wait_ms", 600))
    return {
        "session_id": s.id, "alive": s.alive(), "output": s.read_model(),
        "executed": True, "execution_target": "host", "cwd": cwd,
        "note": "Interactive session started. Drive it with session_send (input=command), "
                "poll new output with session_read, and end it with session_stop.",
    }


def tool_session_send(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    s = _session_mgr.get(sid)
    if not s:
        return _session_not_executed(
            f"no such session: {sid}", sessions=_session_mgr.list())
    if not s.alive():
        return _session_not_executed(
            f"session {sid} has exited", exit_code=s.exit_code())
    text = args.get("input")
    if text is None:
        text = args.get("text", "")
    text = str(text)
    threat = bridge_self_threat(text)
    if threat:
        return _session_not_executed(
            f"refused: {threat}", refused_input=text, self_protection=True)
    catastrophic = _catastrophic_cmd(text)
    if catastrophic:
        return _session_not_executed(
            f"refused: this input {catastrophic}. Run it yourself if you truly mean to.",
            refused_input=text, catastrophic=True)
    clickfix = _clickfix_cmd(text)
    if clickfix:
        return _session_not_executed(
            f"refused: this input {clickfix} (ClickFix signature). Run it yourself if you truly mean to.",
            refused_input=text, clickfix=True)
    danger = _danger_gate(text, "SECURITY CHANGE (hold to approve)")
    if danger and not danger.get("approved"):
        return _session_not_executed(danger["error"], refused_input=text)
    approval = request_approval(
        title="Send input to persistent Windows host process",
        command=text,
        details={
            "kind": "session", "session_id": sid, "target": "host",
            "executes_code": True,
            "red_team_context": bool(_rt_authorized_mission(_rt_context_chat())),
        },
    )
    if approval.get("decision") != "approve":
        return _session_not_executed(
            f"user denied input ({approval.get('status')})")
    if not s.send(text, enter=bool(args.get("enter", True))):
        return _session_not_executed(
            "failed to write to session (it may have exited)", alive=s.alive())
    _session_wait(args.get("wait_ms", 700))
    return {"session_id": sid, "alive": s.alive(), "output": s.read_model(),
            "executed": True, "execution_target": "host"}


def tool_session_read(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    s = _session_mgr.get(sid)
    if not s:
        return _session_not_executed(
            f"no such session: {sid}", sessions=_session_mgr.list())
    _session_wait(args.get("wait_ms", 0))
    return {"session_id": sid, "alive": s.alive(), "exit_code": s.exit_code(), "output": s.read_model()}


def tool_session_stop(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    if not sid:
        return _session_not_executed(
            "session_id required", sessions=_session_mgr.list())
    session = _session_mgr.get(sid)
    if not session:
        return _session_not_executed(
            f"no such session: {sid}", session_id=sid,
            sessions=_session_mgr.list())
    stopped = _session_mgr.stop(sid)
    alive = session.alive()
    result = {
        "session_id": sid,
        "stopped": bool(stopped and not alive),
        "alive": alive,
        "executed": True,
        "execution_target": "host",
    }
    if not result["stopped"]:
        result["error"] = "failed to stop the persistent process tree"
    return result


def tool_session_list(args: dict) -> dict:
    return {"sessions": _session_mgr.list()}
