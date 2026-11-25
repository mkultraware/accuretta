# SPDX-License-Identifier: AGPL-3.0-only
"""Running commands in the WSL sandbox for the agent: the scope and danger checks, path
translation, and the sandbox_run, nmap and sqlmap tools.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import shlex
import time
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.core.tool_stream import _emit_execution_event
from accuretta.core.tool_stream import _execution_not_run
from accuretta.redteam.command_scope import _sandbox_scope_block
from accuretta.redteam.governor import _rt_gov_limits
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.network_scope import _rt_scope_block
from accuretta.runtime.sandbox_setup import _SANDBOX_INTEROP_VERIFY
from accuretta.runtime.sandbox_setup import _sandbox_hardening_lock
from accuretta.runtime.sandbox_setup import _wsl_bash
from accuretta.runtime.sandbox_state import SANDBOX_STATE_FILE
from accuretta.runtime.sandbox_state import _SBX_READY_CACHE
from accuretta.runtime.sandbox_state import wsl_probe
from accuretta.runtime.wsl_sandbox import SANDBOX_DISTRO
from accuretta.runtime.wsl_sandbox import _wsl_run
from accuretta.security.approvals import request_approval
from accuretta.security.danger import _danger_assess
from accuretta.security.hard_blocks import _catastrophic_cmd
from accuretta.security.hard_blocks import _clickfix_cmd


_HOST_CODE_EXEC_RE = re.compile(
    r"(?:^|[;&|]\s*)"
    r"(?:&\s*)?"
    r"(?:node(?:\.exe)?|python(?:3|w)?(?:\.exe)?|py(?:\.exe)?|deno|bun|"
    r"ruby|perl|php|java|dotnet|npx|pnpm\s+(?:run|exec|test|start|install|add)|"
    r"npm\s+(?:run|exec|test|start|install|ci|update)|"
    r"yarn\s+(?:run|test|start|install|add)|uv\s+run|poetry\s+run|pipenv\s+run|"
    r"pytest|tox|nox|jest|vitest|mocha|tsx|ts-node|"
    r"cargo\s+(?:run|test|build)|go\s+(?:run|test)|dotnet\s+(?:run|test|build)|"
    r"(?:make|msbuild|gradle|mvn)(?:\.exe)?|"
    r"git\s+(?:checkout|switch|merge|rebase|commit|pull|push|clone|submodule)|"
    r"cmd(?:\.exe)?\s+/c|powershell(?:\.exe)?|"
    r"pwsh(?:\.exe)?|wsl(?:\.exe)?|bash|sh|cscript|wscript|mshta|rundll32|"
    r"invoke-expression|iex|add-type|"
    r"[^\s;&|]+\.(?:exe|com|ps1|bat|cmd|js|vbs|wsf|hta|jar))\b",
    re.IGNORECASE,
)


_WSL_HOST_INTEROP_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:&\s*)?"
    r"(?:cmd|powershell|pwsh|wscript|cscript|mshta|rundll32|reg|regedit|"
    r"explorer|start|schtasks|sc)(?:\.exe)?\b|"
    r"(?:^|[\s;&|])(?:/mnt/[a-z]/[^\s;&|]*\.exe)\b",
    re.IGNORECASE,
)


def _host_command_executes_code(command: str) -> bool:
    """Conservatively identify host commands that execute code or scripts."""
    return bool(_HOST_CODE_EXEC_RE.search(str(command or "").strip()))


def _sandbox_security_profile_active() -> tuple[bool, str]:
    """Enforce and verify the profile after any external WSL guest restart."""
    rc, out, _, _ = _wsl_bash(SANDBOX_DISTRO, _SANDBOX_INTEROP_VERIFY, timeout=60)
    if rc == 0 and "ACCURETTA_INTEROP_DISABLED" in out:
        return True, ""
    state = load_json(SANDBOX_STATE_FILE, {})
    state.update({"interop_disabled": False, "updated": time.time()})
    save_json(SANDBOX_STATE_FILE, state)
    _SBX_READY_CACHE.update(t=0.0, ready=False)
    return False, (out or f"security verification exited with {rc}").strip()[-500:]


def _win_to_wsl_path(p: str) -> str:
    """Translate a Windows path to its /mnt/... path inside the guest."""
    if not p:
        return "~"
    rc, out = _wsl_run(["-d", SANDBOX_DISTRO, "-u", "root", "--", "wslpath", "-u", p], timeout=20)
    if rc == 0 and out.strip():
        return out.strip().splitlines()[0]
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p


def sandbox_run(command: str, cwd: str = "", timeout: int = 300,
                request_emitted: bool = False) -> dict:
    """Run a shell command inside the separate accuretta-sbx WSL guest. Windows
    workspace paths are visible at /mnt/... ; pass cwd as a Windows path and it
    is translated. This is the execution primitive the model-facing sandbox
    tool / auto-routing will build on."""
    if not request_emitted:
        _emit_execution_event("requested", "sandbox", command, cwd=cwd)
    probe = wsl_probe()
    if not probe["ready"]:
        return _execution_not_run(
            f"WSL guest not ready (state={probe['state']}). Set it up in Settings > Sandbox.",
            "sandbox", command, reason="guest_not_ready", cwd=cwd,
            state=probe["state"],
        )
    with _sandbox_hardening_lock:
        profile_ok, profile_error = _sandbox_security_profile_active()
        if not profile_ok:
            return _execution_not_run(
                "WSL guest security verification failed. Windows executable interop may be active, so the command was not run.",
                "sandbox", command, reason="unsafe_guest", cwd=cwd,
                security_error=profile_error,
            )
        wsl_cwd = _win_to_wsl_path(cwd) if cwd else ""
        prefix = f"cd {shlex.quote(wsl_cwd)} 2>/dev/null; " if wsl_cwd else ""
        rc, out, killed, spawned = _wsl_bash(
            SANDBOX_DISTRO, prefix + command, timeout=timeout, announce=True,
            command=command, cwd=wsl_cwd or "~",
        )
    if not spawned:
        return {
            "error": (out or "WSL guest process could not start")[-2000:],
            "not_executed": True,
            "executed": False,
            "execution_target": "sandbox",
            "execution_state": "spawn_error",
            "exit_code": rc,
        }
    result = {
        "ok": rc == 0 and not killed,
        "exit_code": rc,
        "output": (out or "")[-20000:],
        "distro": SANDBOX_DISTRO,
        "cwd": wsl_cwd or "~",
        "executed": True,
        "execution_target": "sandbox",
    }
    if killed:
        result.update(killed=True, error="sandbox command was killed")
    elif rc == 124:
        result.update(timed_out=True, error=f"sandbox command timed out after {timeout}s")
    elif rc != 0:
        result.update(error=f"sandbox command exited with code {rc}")
    return result


def tool_sandbox_run(args: dict) -> dict:
    """Run a shell command inside the separate accuretta-sbx Linux guest.
    Approval follows the selected access mode and catastrophic commands are
    hard-refused. This is a convenience WSL guest, not a malware containment
    boundary: mounted Windows paths remain real host files. A delete targeting
    /mnt is a delete of the user's real files, hence the same guards."""
    command = (args.get("command") or "").strip()
    if not command:
        return {"error": "empty command"}
    requested_cwd = str(args.get("cwd") or "")
    _emit_execution_event("requested", "sandbox", command, cwd=requested_cwd)

    def not_run(message: str, reason: str, **details) -> dict:
        return _execution_not_run(message, "sandbox", command, reason=reason,
                                  cwd=requested_cwd, **details)

    if _WSL_HOST_INTEROP_RE.search(command):
        return not_run(
            (
                "refused: this sandbox command invokes a Windows executable. "
                "WSL interop crosses the guest boundary, so use the explicit "
                "host PowerShell tool and its approval prompt instead."
            ),
            "host_interop", refused_command=command, host_interop=True)
    catastrophic = _catastrophic_cmd(command)
    if catastrophic:
        return not_run(
            (
                f"refused: this command {catastrophic}. Even inside the sandbox, "
                "/mnt/<drive> is a window into the user's real filesystem, so this "
                "is hard-blocked. Run it yourself if you truly intend it."
            ),
            "catastrophic", refused_command=command, catastrophic=True)
    clickfix = _clickfix_cmd(command)
    if clickfix:
        return not_run(
            (
                f"refused: this command {clickfix} (ClickFix signature). Even "
                "inside the sandbox, download-and-execute pipelines are hard-"
                "blocked. Run it yourself if you truly intend it."
            ),
            "clickfix", refused_command=command, clickfix=True)
    danger = _danger_assess(command)
    if danger and danger[0] == "block":
        return not_run(f"refused: this {danger[1]}. Run it yourself if you truly mean to.",
                       "blocked", refused_command=command)
    sblk = _sandbox_scope_block(command)
    if sblk:
        return not_run(sblk, "scope_blocked", scope_blocked=True)
    executes_code = _host_command_executes_code(command)
    approval = request_approval(
        title=("Execute code in WSL guest" if executes_code else "Command in WSL guest"),
        command=command,
        details={
            "kind": "sandbox",
            "distro": SANDBOX_DISTRO,
            "target": "sandbox (WSL guest)",
            "cwd": requested_cwd,
            "executes_code": executes_code,
            "host_mounts_exposed": True,
            "windows_interop_disabled": True,
        },
    )
    if approval.get("decision") != "approve":
        return not_run(f"user denied WSL guest command ({approval.get('status')})", "denied")
    return sandbox_run(
        command, cwd=requested_cwd, timeout=int(args.get("timeout", 300)),
        request_emitted=True,
    )


def tool_sandbox_nmap(args: dict) -> dict:
    """nmap from inside the sandbox guest, XML-parsed to compact JSON."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    target = (args.get("target") or "").strip()
    if not target or not re.fullmatch(r"[A-Za-z0-9._:/-]+", target):
        return {"error": "target required (hostname, IP, or CIDR - no spaces/flags)"}
    blk = _rt_scope_block("sandbox_nmap", {"target": target})
    if blk:
        return {"error": blk, "scope_blocked": True}
    ports = (args.get("ports") or "").strip()
    if ports and not re.fullmatch(r"[0-9,\-]+", ports):
        return {"error": "ports must look like '22,80,443' or '1-1024'"}
    timing = max(0, min(int(args.get("timing", 3)), 4))
    cmd = f"nmap -Pn -T{timing} --open -sV --version-light -oX - "
    _mission = _rt_authorized_mission(_rt_context_chat()) or {}
    _rpm = _rt_gov_limits(_mission)[0]
    if _rpm:
        # nmap takes packets per second and accepts fractions for slow limits.
        cmd += f"--max-rate {_rpm / 60:.2f} --max-retries 1 "
    if ports:
        cmd += f"-p {ports} "
    cmd += shlex.quote(target)
    _emit_execution_event("requested", "sandbox", cmd)
    approval = request_approval(
        title="WSL guest nmap scan",
        command=cmd,
        details={"kind": "sandbox", "distro": SANDBOX_DISTRO},
    )
    if approval.get("decision") != "approve":
        return _execution_not_run(
            f"user denied nmap scan ({approval.get('status')})",
            "sandbox", cmd, reason="denied")
    res = sandbox_run(
        cmd, timeout=int(args.get("timeout", 600)), request_emitted=True)
    if (res.get("error") or res.get("executed") is False
            or res.get("exit_code") not in (None, 0)):
        return res
    out = res.get("output") or ""
    hosts = []
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(out[out.index("<"):])
        for h in root.iter("host"):
            addr = h.find("address")
            state = h.find("status")
            entry = {"ip": addr.get("addr", "") if addr is not None else "",
                     "state": state.get("state", "") if state is not None else "",
                     "ports": []}
            for p in h.iter("port"):
                st = p.find("state")
                if st is None or st.get("state") != "open":
                    continue
                sv = p.find("service")
                entry["ports"].append({
                    "port": int(p.get("portid", "0")),
                    "proto": p.get("protocol", "tcp"),
                    "service": sv.get("name", "") if sv is not None else "",
                    "product": (sv.get("product", "") or "") if sv is not None else "",
                    "version": (sv.get("version", "") or "") if sv is not None else "",
                })
            hosts.append(entry)
    except Exception as e:
        return {"error": f"nmap output parse failed: {e}",
                "exit_code": res.get("exit_code"), "raw": out[-4000:],
                "executed": True, "execution_target": "sandbox"}
    return {"ok": True, "target": target, "command": cmd, "hosts": hosts,
            "host_count": len(hosts),
            "open_ports": sum(len(h["ports"]) for h in hosts),
            "exit_code": 0, "executed": True, "execution_target": "sandbox"}


def tool_sandbox_sqlmap(args: dict) -> dict:
    """sqlmap from inside the sandbox guest: batch mode, rotating UA, optional
    proxy via rt_proxy. Parsed verdict (injectable / params / DBMS) + raw tail."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    blk = _rt_scope_block("sandbox_sqlmap", {"url": url})
    if blk:
        return {"error": blk, "scope_blocked": True}
    level = max(1, min(int(args.get("level", 1)), 5))
    risk = max(1, min(int(args.get("risk", 1)), 3))
    _mission = _rt_authorized_mission(_rt_context_chat()) or {}
    if _mission.get("allow_destructive") is False:
        # sqlmap risk 3 adds OR-based payloads that can rewrite rows through an
        # UPDATE or DELETE statement.
        risk = min(risk, 2)
    techniques = str(args.get("techniques") or "BEU").upper()
    if techniques not in {"BEU", "BEUT"}:
        return {"error": "techniques must be BEU or BEUT", "invalid_arguments": True,
                "not_executed": True}
    parts = ["sqlmap", "-u", shlex.quote(url), "--batch", "--random-agent",
             f"--level={level}", f"--risk={risk}", "--threads=1", f"--technique={techniques}"]
    if "T" in techniques:
        time_sec = max(1, min(int(args.get("time_sec") or 2), 5))
        parts.append(f"--time-sec={time_sec}")
    if args.get("forms"):
        parts.append("--forms")
    if args.get("tamper"):
        parts.append("--tamper=between,space2comment")
    if args.get("flush_session"):
        parts.append("--flush-session")
    _rpm = _rt_gov_limits(_mission)[0]
    if _rpm:
        parts.append(f"--delay={60.0 / _rpm:.2f}")
    proxy = (get_settings().get("rt_proxy") or "").strip()
    if proxy:
        parts.append(f"--proxy={shlex.quote(proxy)}")
    cmd = " ".join(parts)
    _emit_execution_event("requested", "sandbox", cmd)
    approval = request_approval(
        title="WSL guest sqlmap",
        command=cmd,
        details={"kind": "sandbox", "distro": SANDBOX_DISTRO},
    )
    if approval.get("decision") != "approve":
        return _execution_not_run(
            f"user denied sqlmap ({approval.get('status')})",
            "sandbox", cmd, reason="denied")
    res = sandbox_run(
        cmd, timeout=int(args.get("timeout", 900)), request_emitted=True)
    if (res.get("error") or res.get("executed") is False
            or res.get("exit_code") not in (None, 0)):
        return res
    out = res.get("output") or ""
    injectable = ("is vulnerable" in out
                  or "sqlmap identified the following injection point" in out)
    params = sorted(set(re.findall(r"Parameter:\s*(\S+)", out)))
    m = re.search(r"back-end DBMS:\s*(.+)", out)
    warnings = [line.strip() for line in out.splitlines()
                if re.search(r"\[(WARNING|CRITICAL)\].*(timeout|timed out|recover|HTTP error|unable to connect)", line, re.I)]
    return {"ok": True, "url": url, "injectable": injectable,
            "parameters": params, "dbms": m.group(1).strip() if m else "",
            "techniques": techniques,
            "scan_warnings": warnings, "verification_status": "candidate" if injectable else "not_detected",
            "limitations": "Scanner detection only. No independent reproduction, database dump, or write access was established. Negative results apply only to the checks performed.",
            "exit_code": res.get("exit_code"), "raw_tail": out[-3000:],
            "executed": True, "execution_target": "sandbox"}
