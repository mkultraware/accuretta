# SPDX-License-Identifier: AGPL-3.0-only
"""Running PowerShell for the agent: quoting, the wrapper script, BOM cleanup, reading
the outcome, and the streaming runner.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.tool_stream import _append_cmd_history
from accuretta.core.tool_stream import _emit_execution_event
from accuretta.core.tool_stream import _emit_tool_stream
from accuretta.core.tool_stream import _execution_not_run
from accuretta.core.tool_stream import _execution_tool_name
from accuretta.core.windows_bin import _trusted_windows_binary
from accuretta.runtime.command_kill import _killed_procs
from accuretta.runtime.command_kill import _running_cmd_targets
from accuretta.runtime.command_kill import _running_cmds
from accuretta.runtime.command_kill import _running_cmds_lock
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.security.agent_process_note import _note_agent_process
from accuretta.security.agent_process_note import _note_agent_process_end
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.tool_env import _tool_child_env
from accuretta.tools.ps_bom import _UTF8_BOM
from accuretta.tools.ps_bom import _ps_bom_candidates
from pathlib import Path


# Model-facing commands run inside a small wrapper. Windows PowerShell 5.1
# reads BOM-less UTF-8 files as ANSI and writes the pipe in the OEM code page,
# so non-ASCII text came back garbled; the prelude makes both UTF-8. The status
# line reports $? and $LASTEXITCODE: with -Command the process exit code follows
# $? of the last statement, so a program that succeeded but wrote to a
# redirected stderr (`node x 2>&1`) came back as exit 1.
_PS_STATUS = "__ACCURETTA_STATUS__"


_PS_ERROR_ID_RE = re.compile(r"FullyQualifiedErrorId\s*:\s*([\w.,]+)")


def _strip_new_boms(paths, since: float) -> None:
    """Remove the BOM Windows PowerShell 5.1 wrote at the start of these files."""
    for path in paths or ():
        try:
            if os.path.getmtime(path) < since - 2:
                continue
            data = Path(path).read_bytes()
            if data.startswith(_UTF8_BOM):
                Path(path).write_bytes(data[3:])
        except OSError:
            pass


def _clip_head_tail(text: str, limit: int) -> str:
    """Keep the start and (mostly) the end of a huge output, saying what was dropped."""
    if len(text) <= limit:
        return text
    head = limit // 4
    return (text[:head] + f"\n… [{len(text) - limit:,} chars of output dropped here] …\n"
            + text[-(limit - head):])


def _ps_quote(text: str) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def _ps_script(cmd: str, bom_files=()) -> str:
    bom_hook = ""
    if bom_files:
        bom_hook = (
            "$global:__AccBomFiles = @(" + ", ".join(_ps_quote(p) for p in bom_files) + ")\n"
            "$global:__AccBomFlush = { foreach ($f in $global:__AccBomFiles) {"
            " try { $s = [IO.File]::Open($f, 'Open', 'ReadWrite', 'None') } catch { continue };"
            " try { $h = [byte[]]::new(3);"
            " if ($s.Read($h, 0, 3) -eq 3 -and $h[0] -eq 0xEF -and $h[1] -eq 0xBB -and $h[2] -eq 0xBF) {"
            " $r = [IO.MemoryStream]::new(); $s.CopyTo($r); $s.SetLength(0); $r.Position = 0; $r.CopyTo($s) }"
            " } catch {} finally { $s.Dispose() } } }\n"
            "$ExecutionContext.InvokeCommand.PreCommandLookupAction = { & $global:__AccBomFlush }\n"
        )
    return (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
        "$PSDefaultParameterValues['Get-Content:Encoding'] = 'UTF8'\n"
        "$PSDefaultParameterValues['Select-String:Encoding'] = 'UTF8'\n"
        "$PSDefaultParameterValues['Out-File:Encoding'] = 'UTF8'\n"
        + bom_hook +
        "$global:LASTEXITCODE = 0\n"
        f"{cmd}\n"
        f"$__accOk = $?; Write-Output \"{_PS_STATUS}$__accOk;$LASTEXITCODE\""
    )


def _ps_outcome(stdout: str, stderr: str, returncode: int) -> tuple[str, bool, int]:
    """(stdout without the status line, success, exit code) of a wrapped command.

    The last program's own exit code wins. Error records that are only a
    program's stderr lines (NativeCommandError) do not make a run fail."""
    head, sep, tail = stdout.rpartition(_PS_STATUS)
    if not sep:
        return stdout, returncode == 0, returncode
    ps_ok, _, code_txt = tail.strip().partition(";")
    code_txt = code_txt.strip()
    program_code = int(code_txt) if code_txt.lstrip("-").isdigit() else 0
    if program_code:
        return head, False, program_code
    if ps_ok == "True":
        return head, True, 0
    ids = _PS_ERROR_ID_RE.findall(head + stderr)
    if ids and all(i.startswith("NativeCommandError") for i in ids):
        return head, True, 0
    return head, False, returncode or 1


def _run_powershell(cmd: str, timeout: int = 120, max_stdout: int = 16000,
                    cwd: str | None = None, wrap: bool = False) -> dict:
    t_start = time.time()
    stream_chat_id = _current_chat_id.get() or _get_current_chat()
    stream_tool_name = _execution_tool_name("run_powershell")
    stream_call_id = _current_tool_call_id.get()
    process_cwd = cwd or os.getcwd()
    powershell_exe = _trusted_windows_binary("WindowsPowerShell", "v1.0", "powershell.exe")
    if not powershell_exe:
        return _execution_not_run(
            "Trusted Windows PowerShell executable was not found in the Windows system directory.",
            "host", cmd, reason="spawn_error", cwd=process_cwd, spawn_error=True,
        )
    bom_files = _ps_bom_candidates(cmd, process_cwd) if wrap else []
    try:
        proc = subprocess.Popen(
            [powershell_exe, "-NoProfile", "-NonInteractive", "-Command",
             _ps_script(cmd, bom_files) if wrap else cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8" if wrap else None, errors="replace" if wrap else None,
            # Model commands get no stdin: a script that waits for input() or a
            # prompt fails at once instead of hanging until the timeout.
            stdin=subprocess.DEVNULL if wrap else None,
            cwd=process_cwd, env=_tool_child_env() if wrap else _child_env(keep=_ENV_TOOLS),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        if wrap:                       # the model's own command, not an app-internal one
            _note_agent_process(proc)
    except Exception as e:
        _append_cmd_history({
            "ts": int(t_start * 1000),
            "chat_id": _get_current_chat(),
            "command": cmd,
            "exit": -1,
            "ok": False,
            "duration_ms": int((time.time() - t_start) * 1000),
            "stdout": "",
            "stderr": str(e),
            "spawn_error": True,
            "target": "host",
            "tool": stream_tool_name,
            "call_id": stream_call_id,
        })
        return _execution_not_run(str(e), "host", cmd, reason="spawn_error",
                                  cwd=process_cwd, spawn_error=True)

    out_lines: list[str] = []
    err_lines: list[str] = []

    def reader(pipe, sink, name):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                if wrap and _PS_STATUS in line:
                    continue
                _emit_tool_stream(stream_tool_name, line.rstrip("\n\r"), stream_chat_id,
                                  target="host", call_id=stream_call_id)
            pipe.close()
        except Exception:
            pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, out_lines, "run_powershell"), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_lines, "run_powershell"), daemon=True)

    # Register for the per-command kill button before announcing the spawn.
    _cid = stream_chat_id or ""
    if _cid:
        with _running_cmds_lock:
            _running_cmds[_cid] = proc
            _running_cmd_targets[_cid] = "host"
    _emit_execution_event("spawned", "host", cmd, cwd=process_cwd,
                          chat_id=stream_chat_id, pid=proc.pid)
    t_out.start()
    t_err.start()
    try:
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc, "host")
            timed_out = True
            try:
                proc.wait(timeout=5)
            except Exception:
                pass

        t_out.join(timeout=5)
        t_err.join(timeout=5)
        _note_agent_process_end(proc.pid)
        _strip_new_boms(bom_files, t_start)

        if wrap:
            # The whole output goes to the result. compress_tool_result fits it to
            # the model's budget and saves the full text where read_file can page
            # it; the old silent [-16000:] slice lost the start with no marker.
            stdout = _clip_head_tail("".join(out_lines), 400_000)
            stderr = _clip_head_tail("".join(err_lines), 100_000)
        else:
            stdout = "".join(out_lines)[-max_stdout:]
            stderr = "".join(err_lines)[-4000:]
        # Was this terminated by the kill button? Check the explicit marker
        # (returncode alone can't tell on Windows) plus the POSIX negative code.
        with _running_cmds_lock:
            killed = proc in _killed_procs
        if proc.returncode is not None and proc.returncode < 0:
            killed = True
        ok, exit_code = proc.returncode == 0, proc.returncode
        if wrap and not (timed_out or killed):
            stdout, ok, exit_code = _ps_outcome(stdout, stderr, proc.returncode)
        result = {
            "ok": ok,
            "exit": exit_code,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "executed": True,
            "execution_target": "host",
        }
        if wrap and exit_code != proc.returncode:
            result["powershell_exit_code"] = proc.returncode
        if wrap:
            result["cwd"] = process_cwd
        if timed_out:
            result["timed_out"] = True
            result["error"] = f"timeout after {timeout}s"
            if wrap:
                result["error"] += (
                    " (the command was stopped). For a long build or test run, call again "
                    "with a larger timeout; for a server or watcher that never exits, start "
                    "it with session_start instead (list_more_tools bundle 'sessions').")
        elif killed:
            result["killed"] = True
            result["error"] = "command was killed"
        duration_ms = int((time.time() - t_start) * 1000)
        _append_cmd_history({
            "ts": int(t_start * 1000),
            "chat_id": stream_chat_id,
            "command": cmd,
            "exit": exit_code,
            "ok": result["ok"],
            "duration_ms": duration_ms,
            "stdout": stdout[-max_stdout:],
            "stderr": stderr[-4000:],
            "killed": killed,
            "timed_out": timed_out,
            "target": "host",
            "tool": stream_tool_name,
            "call_id": stream_call_id,
        })
        _emit_execution_event("finished", "host", cmd, cwd=process_cwd,
                              chat_id=stream_chat_id, exit_code=exit_code,
                              killed=killed, timed_out=timed_out,
                              duration_ms=duration_ms)
        return result
    finally:
        with _running_cmds_lock:
            if _cid and _running_cmds.get(_cid) is proc:
                _running_cmds.pop(_cid, None)
                _running_cmd_targets.pop(_cid, None)
            _killed_procs.discard(proc)
