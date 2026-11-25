# SPDX-License-Identifier: AGPL-3.0-only
"""Running PowerShell and Python on this machine for the agent: the checks in front, the
Python launcher, and the undo snapshot of files a command may delete.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
import subprocess
from accuretta.core.command_cwd import _default_command_cwd
from accuretta.core.paths import ROOT
from accuretta.core.tool_stream import _emit_execution_event
from accuretta.core.tool_stream import _execution_not_run
from accuretta.core.undo import _undo_snapshot
from accuretta.core.undo import _undo_store
from accuretta.core.updates import _installer_info
from accuretta.core.workspace import normalize_path
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.security.approvals import _log_gate_decision
from accuretta.security.approvals import request_approval
from accuretta.security.danger_gate import _danger_gate
from accuretta.security.hard_blocks import _catastrophic_cmd
from accuretta.security.hard_blocks import _clickfix_cmd
from accuretta.security.path_guards import is_in_workspace
from accuretta.security.registry_writes import registry_assess
from accuretta.security.self_protect import bridge_self_threat
from accuretta.security.soft_commands import _REBUILDABLE_DIRS
from accuretta.security.soft_commands import _soft_command_concerns
from accuretta.tools.powershell import _run_powershell
from accuretta.tools.sandbox_tools import _host_command_executes_code
from pathlib import Path
from accuretta.security.agent_process_note import _note_agent_process
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.path_guards import is_blocked_path


def _snapshot_shell_deletes(targets: list[str], limit_files: int = 300) -> list[str]:
    """Put the text files a shell command is about to delete into this turn's
    undo journal. Returns the snapshotted paths (checkpoint them afterwards)."""
    done: list[str] = []
    for target in targets:
        paths = []
        if "*" in target or "?" in target:
            try:
                import glob as _glob
                paths = [p for p in _glob.glob(target) if os.path.isfile(p)]
            except Exception:
                paths = []
        elif os.path.isfile(target):
            paths = [target]
        elif os.path.isdir(target):
            for dirpath, dirnames, filenames in os.walk(target):
                dirnames[:] = [d for d in dirnames if d.lower() not in _REBUILDABLE_DIRS]
                paths += [os.path.join(dirpath, f) for f in filenames]
                if len(paths) > limit_files:
                    break
        for p in paths[:max(0, limit_files - len(done))]:
            try:
                if os.path.getsize(p) <= 2 * 1024 * 1024:
                    _undo_snapshot(p)
                    done.append(p)
            except OSError:
                continue
    return done


def _checkpoint_shell_deletes(paths: list[str]) -> None:
    for p in paths:
        try:
            _undo_store.checkpoint(p)
        except Exception:
            pass


def tool_run_powershell(args: dict) -> dict:
    return _run_host_command(str(args.get("command") or ""), str(args.get("cwd") or ""),
                             args.get("timeout", 120))


def _run_host_command(cmd: str, requested_cwd: str, timeout, code_preview: str = "") -> dict:
    """Guard, approve and run one model-requested PowerShell command.

    code_preview is the script a launcher command runs (run_python). It comes
    from the harness, never from model arguments, so the approval card cannot
    show text the command does not run."""
    cmd = (cmd or "").strip()
    if not cmd:
        return {"error": "empty command"}
    requested_cwd = (requested_cwd or "").strip()
    process_cwd = normalize_path(requested_cwd) if requested_cwd else _default_command_cwd()
    if requested_cwd:
        if not os.path.isdir(process_cwd):
            return {"error": f"cwd is not a directory: {process_cwd}", "not_executed": True}
        if not is_in_workspace(process_cwd):
            return {"error": "cwd is outside the workspace. Add the folder in the Workspace panel.",
                    "not_executed": True}
    _emit_execution_event("requested", "host", cmd, cwd=process_cwd)

    def not_run(message: str, reason: str, **details) -> dict:
        return _execution_not_run(message, "host", cmd, reason=reason,
                                  cwd=process_cwd, **details)

    threat = bridge_self_threat(cmd)
    catastrophic_hit = None if threat else _catastrophic_cmd(cmd)
    clickfix_hit = None if (threat or catastrophic_hit) else _clickfix_cmd(cmd)
    if threat or catastrophic_hit or clickfix_hit:
        _log_gate_decision({"kind": "powershell"}, {"status": "blocked"}, 0.0,
                           reason=threat or catastrophic_hit or clickfix_hit)
    if threat:
        return not_run(
            (
                f"refused: {threat}. Accuretta won't terminate or overwrite its own "
                "server through a tool call — restart the bridge yourself if you "
                "really mean to. If you're trying to kill a different python "
                "process (flask dev server, etc.), target it by exact PID rather "
                "than 'all python.exe' or 'whatever listens on 8787'."
            ),
            "self_protection", refused_command=cmd)
    catastrophic = catastrophic_hit
    if catastrophic:
        return not_run(
            (
                f"refused: this command {catastrophic}. Accuretta hard-blocks "
                "whole-machine / whole-drive destruction — it never even reaches "
                "the approval queue. If you truly intend this, run it yourself."
            ),
            "catastrophic", refused_command=cmd, catastrophic=True)
    clickfix = clickfix_hit
    if clickfix:
        return not_run(
            (
                f"refused: this command {clickfix} — the ClickFix signature "
                "(remote content piped into an interpreter). Accuretta hard-blocks "
                "download-and-execute pipelines; they never reach the approval "
                "queue. If you truly intend this, run it yourself."
            ),
            "clickfix", refused_command=cmd, clickfix=True)
    # Weakening security, destroying backups, secrets leaving the PC: refused,
    # or the hold-to-approve card, in every mode and under /goal.
    danger = _danger_gate(cmd, "SECURITY CHANGE (hold to approve)", code=code_preview)
    if danger and not danger.get("approved"):
        return not_run(danger["error"], "blocked" if danger.get("blocked") else "denied",
                       refused_command=cmd)
    # The registry check runs before the generic powershell approval. Both tiers
    # use the hold-to-approve CRITICAL card (never auto-approved in any mode);
    # system hives get the loudest wording.
    reg = registry_assess(cmd)
    if danger:
        pass                                 # the hold-to-approve card above covered it
    elif reg["level"] == "system":
        approval = request_approval(
            title="REGISTRY EDIT (SYSTEM hive - can brick Windows)",
            command=cmd,
            details={"kind": "registry", "level": "system", "targets": reg["targets"], "critical": True},
        )
        if approval.get("decision") != "approve":
            return not_run(f"user denied SYSTEM registry edit ({approval.get('status')})",
                           "denied")
    elif reg["level"] == "user":
        approval = request_approval(
            title="REGISTRY EDIT (user hive)",
            command=cmd,
            details={"kind": "registry", "level": "user", "targets": reg["targets"], "critical": True},
        )
        if approval.get("decision") != "approve":
            return not_run(f"user denied registry edit ({approval.get('status')})",
                           "denied")
    else:
        executes_code = bool(code_preview) or _host_command_executes_code(cmd)
        red_team_context = bool(_rt_authorized_mission(_rt_context_chat()))
        details = {
            "kind": "powershell",
            "target": "host",
            "executes_code": executes_code,
            "red_team_context": red_team_context,
            "run_cwd": process_cwd,          # Soft mode's data-folder check
        }
        if code_preview:
            details["code_preview"] = code_preview[:6000]
        approval = request_approval(
            title=("Host code execution" if executes_code else "Command on Windows host"),
            command=cmd,
            details=details,
        )
        if approval.get("decision") != "approve":
            return not_run(f"user denied command ({approval.get('status')})", "denied")
    # Files the command deletes inside the workspace go into this turn's undo
    # journal first, so letting those deletes run without a question stays safe.
    snapped: list[str] = []
    try:
        targets: list[str] = []
        _soft_command_concerns(cmd, "powershell", None, run_cwd=process_cwd, targets_out=targets)
        if targets:
            snapped = _snapshot_shell_deletes(targets)
    except Exception:
        snapped = []
    try:
        return _run_powershell(cmd, timeout=int(timeout or 120), cwd=process_cwd, wrap=True)
    finally:
        if snapped:
            _checkpoint_shell_deletes(snapped)


def _python_launcher(cwd: str) -> list[str]:
    """argv prefix for Python: the project's virtual environment when it has one,
    else the py launcher, else python on PATH."""
    for venv in (".venv", "venv", "env"):
        candidate = Path(cwd) / venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if candidate.is_file():
            return [str(candidate)]
    launcher = shutil.which("py") if os.name == "nt" else None
    if launcher:
        return [launcher, "-3"]
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return [found]
    # An installer build carries its own Python next to the app folder; use it
    # when the PC has none, so run_python works out of the box.
    if _installer_info():
        bundled = ROOT.parent / "python" / ("python.exe" if os.name == "nt" else "python")
        if bundled.is_file():
            return [str(bundled)]
    return []


def tool_run_python(args: dict) -> dict:
    """Run model-written Python from a temporary script file.

    Python pasted into a PowerShell here-string breaks on `$`, quotes and
    backticks. The script runs through the run_powershell path, so the same
    approval rules apply and Soft mode reads the script file it runs."""
    code = str(args.get("code") or "")
    if not code.strip():
        return {"error": "empty code", "not_executed": True}
    for check in (bridge_self_threat, _catastrophic_cmd):
        reason = check(code)
        if reason:
            return {"error": f"refused: this script {reason}.", "not_executed": True}
    requested_cwd = str(args.get("cwd") or "").strip()
    cwd = normalize_path(requested_cwd) if requested_cwd else _default_command_cwd()
    launcher = _python_launcher(cwd)
    if not launcher:
        return {"error": "No Python interpreter was found. Install Python or create a .venv in the project.",
                "not_executed": True}
    script_dir = Path(tempfile.gettempdir()) / "accuretta-run-python"
    script_dir.mkdir(parents=True, exist_ok=True)
    script = script_dir / f"run_{uuid.uuid4().hex[:12]}.py"
    script.write_text(code, encoding="utf-8")
    command = ('& "' + launcher[0] + '"' + "".join(f" {a}" for a in launcher[1:])
               + f' -X utf8 "{script}"')
    try:
        return _run_host_command(command, requested_cwd, args.get("timeout", 120), code_preview=code)
    finally:
        try:
            script.unlink()
        except OSError:
            pass


def tool_open_program(args: dict) -> dict:
    """Launch a program. Allowed from Program Files and user areas; blocked for Windows/System32 only."""
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": f"not found: {path}"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    approval = request_approval(
        title="Launch program",
        command=f'Start-Process "{path}"',
        details={"kind": "launch", "path": path},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied launch ({approval.get('status')})"}
    try:
        launched = subprocess.Popen([path] + list(args.get("args", []) or []), shell=False,
                                    env=_child_env(keep=_ENV_TOOLS))
        _note_agent_process(launched)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}
