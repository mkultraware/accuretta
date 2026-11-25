# SPDX-License-Identifier: AGPL-3.0-only
"""The run_tests tool: running a project's tests with live output, telling a full run
from a partial one, and noticing when no tests ran at all.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.command_cwd import _default_command_cwd
from accuretta.core.tool_stream import _append_cmd_history
from accuretta.core.tool_stream import _emit_execution_event
from accuretta.core.tool_stream import _emit_tool_stream
from accuretta.core.tool_stream import _execution_not_run
from accuretta.core.tool_stream import _execution_tool_name
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.core.workspace import normalize_path
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.runtime.command_kill import _killed_procs
from accuretta.runtime.command_kill import _running_cmd_targets
from accuretta.runtime.command_kill import _running_cmds
from accuretta.runtime.command_kill import _running_cmds_lock
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.security.agent_process_note import _note_agent_process
from accuretta.security.agent_process_note import _note_agent_process_end
from accuretta.security.approvals import request_approval
from accuretta.security.danger_gate import _danger_gate
from accuretta.security.hard_blocks import _catastrophic_cmd
from accuretta.security.hard_blocks import _clickfix_cmd
from accuretta.security.path_guards import is_in_workspace
from accuretta.security.self_protect import bridge_self_threat
from accuretta.security.tool_env import _tool_child_env
from pathlib import Path
from task_state import file_revision
from typing import Any


def _full_test_run(command: str, output: str) -> bool:
    try:
        argv = [part.strip('"') for part in shlex.split(command, posix=os.name != "nt")]
    except ValueError:
        return False
    if not argv:
        return False
    executable = Path(argv[0]).stem.lower()
    args = argv[1:]
    runner = executable
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?|py", executable):
        if len(args) < 2 or args[0] != "-m":
            return False
        runner, args = args[1], args[2:]
    if runner == "pytest" and all(arg in {"-q", "-v", "-vv", "--quiet", "--verbose"} for arg in args):
        return bool(re.search(r"\b[1-9]\d* passed\b", output))
    if runner == "unittest" and args[:1] == ["discover"] and all(arg in {"-q", "-v"} for arg in args[1:]):
        return bool(re.search(r"Ran [1-9]\d* tests?\b", output) and re.search(r"(?m)^OK(?: \(.*\))?$", output))
    if executable == "node" and args == ["--test"]:
        return bool(re.search(r"(?m)^# pass [1-9]\d*\s*$", output))
    return False


_NO_TESTS_RE = re.compile(
    r"no tests ran|collected 0 items|^ran 0 tests|no tests found|no test files found|"
    r"0 tests? (?:found|collected)|no tests to run|testsuites? empty",
    re.IGNORECASE | re.MULTILINE)


def _no_tests_ran(returncode, output: str) -> bool:
    """True when a test runner reports it found nothing to run (pytest and
    unittest exit 5; jest/vitest/go/cargo print it)."""
    tail = str(output or "")[-6000:]
    if returncode == 5 and re.search(r"pytest|unittest|no tests|collected 0", tail, re.IGNORECASE):
        return True
    return bool(_NO_TESTS_RE.search(tail))


def tool_run_tests(args: dict) -> dict:
    command = str(args.get("command") or "").strip()
    cwd = normalize_path(str(args.get("cwd") or "") or _default_command_cwd())
    if not command:
        return {"error": "test command required"}
    _emit_execution_event("requested", "host", command, cwd=cwd)

    def not_run(message: str, reason: str, **details) -> dict:
        return _execution_not_run(message, "host", command, reason=reason,
                                  cwd=cwd, **details)

    if not is_in_workspace(cwd):
        return not_run("CWD is outside workspace bounds.", "scope_blocked")
    threat = bridge_self_threat(command)
    if threat:
        return not_run(f"refused: {threat}", "self_protection")
    catastrophic = _catastrophic_cmd(command)
    if catastrophic:
        return not_run(f"refused: this command {catastrophic}", "catastrophic",
                       catastrophic=True)
    clickfix = _clickfix_cmd(command)
    if clickfix:
        return not_run(
            f"refused: this command {clickfix} (ClickFix signature)",
            "clickfix", clickfix=True)
    danger = _danger_gate(command, "SECURITY CHANGE (hold to approve)")
    if danger and not danger.get("approved"):
        return not_run(danger["error"], "blocked" if danger.get("blocked") else "denied")
    approval = request_approval(
        title="Run project tests on Windows host",
        command=command,
        details={
            "kind": "test",
            "cwd": cwd,
            "target": "host",
            "executes_code": True,
            "red_team_context": bool(_rt_authorized_mission(_rt_context_chat())),
        },
    )
    if approval.get("decision") != "approve":
        return not_run(f"user denied test command ({approval.get('status')})", "denied")

    revisions = {}
    chat = (get_chats().get("chats", {}) or {}).get(_get_current_chat(), {})
    for entry in chat.get("verification_debt", []):
        path = Path(entry.get("path") or "")
        if entry.get("target", "host") == "host" and path.resolve().is_relative_to(Path(cwd).resolve()):
            revision = file_revision(path)
            if revision:
                revisions[_verification_key(str(path))] = revision
    t_start = time.time()
    chat_id = _current_chat_id.get() or _get_current_chat()
    tool_name = _execution_tool_name("run_tests")
    call_id = _current_tool_call_id.get()
    try:
        argv = shlex.split(command, posix=os.name != "nt")
        # Windows looks a bare program name up next to python.exe, in the bridge's
        # own folder and on PATH, never in cwd, so a project wrapper such as
        # mvnw.cmd or gradlew.bat failed with WinError 2. cmd.exe searches cwd.
        # UTF-8 both ways: Python runners print in UTF-8 mode (see _tool_child_env)
        # and Node always does, so a check mark no longer turns into mojibake or
        # stops the reader. No stdin: a test that waits for input fails fast.
        proc = subprocess.Popen(
            command if os.name == "nt" else argv, cwd=cwd, shell=os.name == "nt",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            env=_tool_child_env(), text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        _note_agent_process(proc)
    except Exception as e:
        _append_cmd_history({
            "ts": int(t_start * 1000), "chat_id": chat_id, "command": command,
            "cwd": cwd, "exit": -1, "ok": False, "duration_ms": 0,
            "stdout": "", "stderr": str(e), "spawn_error": True,
            "target": "host", "tool": tool_name, "call_id": call_id,
        })
        return not_run(str(e), "spawn_error", spawn_error=True)

    out_lines: list[str] = []
    err_lines: list[str] = []

    def reader(pipe, sink):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                _emit_tool_stream(tool_name, line.rstrip("\r\n"), chat_id,
                                  target="host", call_id=call_id)
            pipe.close()
        except Exception:
            pass

    out_thread = threading.Thread(target=reader, args=(proc.stdout, out_lines), daemon=True)
    err_thread = threading.Thread(target=reader, args=(proc.stderr, err_lines), daemon=True)
    if chat_id:
        with _running_cmds_lock:
            _running_cmds[chat_id] = proc
            _running_cmd_targets[chat_id] = "host"
    _emit_execution_event("spawned", "host", command, cwd=cwd,
                          chat_id=chat_id, pid=proc.pid)
    out_thread.start()
    err_thread.start()

    timeout = max(1, min(int(args.get("timeout") or 600), 3600))
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(proc, "host")
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        out_thread.join(timeout=5)
        err_thread.join(timeout=5)
        _note_agent_process_end(proc.pid)
        stdout = "".join(out_lines)
        stderr = "".join(err_lines)
        output = stdout + "\n" + stderr
        with _running_cmds_lock:
            killed = proc in _killed_procs
        if proc.returncode is not None and proc.returncode < 0:
            killed = True
        failures = [
            line.strip() for line in output.splitlines()
            if any(kw in line for kw in ("FAIL", "Error:", "Exception", "Traceback", "FAILED"))
        ]
        summary = {
            "passed": proc.returncode == 0 and not timed_out and not killed,
            "returncode": proc.returncode,
            "failure_hints": failures[:15],
            "output_tail": output[-2000:]
        }
        duration_ms = int((time.time() - t_start) * 1000)
        _append_cmd_history({
            "ts": int(t_start * 1000), "chat_id": chat_id, "command": command,
            "cwd": cwd, "exit": proc.returncode, "ok": summary["passed"],
            "duration_ms": duration_ms, "stdout": stdout[-16000:],
            "stderr": stderr[-4000:], "killed": killed, "timed_out": timed_out,
            "target": "host", "tool": tool_name, "call_id": call_id,
        })
        _emit_execution_event("finished", "host", command, cwd=cwd,
                              chat_id=chat_id, exit_code=proc.returncode,
                              killed=killed, timed_out=timed_out,
                              duration_ms=duration_ms)
        base = {
            "details": summary,
            "exit_code": proc.returncode,
            "executed": True,
            "execution_target": "host",
        }
        if timed_out:
            return {**base, "error": f"tests timed out after {timeout}s", "timed_out": True}
        if killed:
            return {**base, "error": "test command was killed", "killed": True}
        if _no_tests_ran(proc.returncode, output):
            # pytest exits 5 and unittest 5 (3.12+) when they find nothing to run;
            # "Tests failed" sent the model hunting for a bug that wasn't there.
            return {**base, "error": ("the command found no tests to run, so nothing was tested. "
                                      "Check the cwd and the test path or pattern."),
                    "no_tests": True}
        if proc.returncode == 0:
            return {**base, "ok": True, "result": summary,
                    "verification": {"target": "host", "cwd": cwd,
                                     "tests_ran": _full_test_run(command, output),
                                     "revisions": revisions}}
        return {**base, "error": "Tests failed: see details.failure_hints and details.output_tail"}
    finally:
        with _running_cmds_lock:
            if chat_id and _running_cmds.get(chat_id) is proc:
                _running_cmds.pop(chat_id, None)
                _running_cmd_targets.pop(chat_id, None)
            _killed_procs.discard(proc)


def _verification_key(path: Any) -> str:
    try:
        return os.path.normcase(os.path.normpath(str(path or "").strip()))
    except Exception:
        return str(path or "").strip().lower()
