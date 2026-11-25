# SPDX-License-Identifier: AGPL-3.0-only
"""Why stopping a process might matter: the model server, Accuretta itself, processes
the agent didn't start, and what a stop command would really hit.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import fnmatch
import os
import re
import sys
from accuretta.core.endpoints import PORT
from accuretta.core.processes import _local_listeners
from accuretta.core.processes import _proc_create_time
from accuretta.core.processes import _proc_image_path
from accuretta.core.processes import _process_parent_map
from accuretta.core.processes import _process_table
from accuretta.security.agent_processes import _AGENT_PROCS
from accuretta.security.agent_processes import _AGENT_PROCS_LOCK
from accuretta.security.agent_processes import _parse_llama_port
from accuretta.security.agent_processes import _started_by_agent
from accuretta.security.process_stops import _stop_pid_file_values
from accuretta.security.process_stops import _stop_var_ok
from accuretta.security.soft_paths import _soft_target_ok


def _model_server_pids() -> set[int]:
    """Processes serving Accuretta itself or its model (llama-server / Maya)."""
    ports = {int(PORT), _parse_llama_port()}
    try:
        return {pid for port, pid in _local_listeners() if port in ports}
    except Exception:
        return set()


def _pid_stop_reason(pid: int, *, table: dict | None = None, model_pids: set | None = None) -> str | None:
    """Why stopping `pid` needs a question, or None when it's fine: not running,
    started by Accuretta's own tools, or a program that lives in the workspace
    or temp folder (the thing it just built)."""
    pid = int(pid)
    if pid == os.getpid():
        return "stops Accuretta itself"
    if _proc_create_time(pid) is None:
        return None                                   # not running: nothing to stop
    if model_pids is None:
        model_pids = _model_server_pids()
    table = table if table is not None else _process_table()
    name = (table.get(pid) or (0, ""))[1] or f"process {pid}"
    if pid in model_pids:
        return f"stops Accuretta's model server ({name})"
    try:
        with _AGENT_PROCS_LOCK:
            agents = {p: list(v) for p, v in _AGENT_PROCS.items()}
        if _started_by_agent(pid, {p: v[0] for p, v in table.items()} if table else _process_parent_map(),
                             agents):
            return None
    except Exception:
        pass
    image = _proc_image_path(pid)
    if image and _soft_target_ok(("host", os.path.normcase(os.path.normpath(image))), delete=False):
        return None
    return f"stops a program Accuretta didn't start ({name})"


def _stop_name_reasons(pattern: str, table: dict, model_pids: set) -> list[str]:
    pat = pattern.lower().strip("\"'")
    pat = pat[:-4] if pat.endswith(".exe") else pat
    if not pat:
        return []
    pids = [pid for pid, (_, name) in table.items() if fnmatch.fnmatchcase(name, pat)]
    reasons = [r for r in (_pid_stop_reason(p, table=table, model_pids=model_pids) for p in pids) if r]
    return list(dict.fromkeys(reasons))


def _process_stop_concerns(name: str, args: list[str], lits: list[str], code: str,
                           posix: bool, cwd: str | None = None) -> list[str]:
    """Concerns for a Stop-Process / taskkill / kill command: none when every
    process it targets is one Accuretta's tools started, or a program from the
    workspace, or not running at all."""
    if posix:
        return []                                     # the WSL sandbox is disposable
    lit = lambda tok: lits[int(tok[1:-1])] if re.fullmatch(r'"\d+"', tok) else tok  # noqa: E731
    vals = [lit(a).strip(",") for a in args]
    pids: list[int] = []
    names: list[str] = []
    ports: list[int] = []
    unknown = False
    low = [v.lower() for v in vals]
    if name in ("taskkill", "tskill"):
        i = 0
        while i < len(low):
            a = low[i]
            if a in ("/pid", "-pid") and i + 1 < len(vals):
                (pids.append(int(vals[i + 1])) if vals[i + 1].isdigit() else None)
                unknown |= not vals[i + 1].isdigit()
                i += 2
                continue
            if a in ("/im", "-im") and i + 1 < len(vals):
                names.append(vals[i + 1])
                i += 2
                continue
            if a in ("/fi", "-fi", "/s", "/u", "/p"):
                unknown = True
                i += 2
                continue
            if name == "tskill" and not a.startswith(("/", "-")):
                (pids.append(int(a)) if a.isdigit() else names.append(vals[i]))
            i += 1
    else:
        piped = False                          # a target that comes down the pipeline ($_, $p from Get-Process)
        vetted = False                         # a variable already shown to be fine (started by this command)
        i = 0

        def var_target(tok: str) -> None:
            nonlocal unknown, piped, vetted
            file_pids = _stop_pid_file_values(tok, code, lits, cwd)
            if file_pids is not None:
                pids.extend(file_pids)
                return
            if re.match(r"\$(?:_|psitem)\b", tok, re.IGNORECASE) and "owningprocess" not in tok.lower():
                piped = True
            elif re.match(r"\$(\w+)", tok) and re.search(
                    rf"\${re.escape(re.match(r'[$](\w+)', tok).group(1))}\s*=\s*(?:get-process|gps)\b", code,
                    re.IGNORECASE):
                piped = True
            elif _stop_var_ok(tok, code, ports):
                vetted = True
            else:
                unknown = True

        while i < len(vals):
            a, la = vals[i], low[i]
            if la in ("-id", "-processid", "-pid") and i + 1 < len(vals):
                for part in vals[i + 1].split(","):
                    part = part.strip()
                    if part.isdigit():
                        pids.append(int(part))
                    elif part:
                        var_target(part)
                i += 2
                continue
            if la in ("-name", "-processname") and i + 1 < len(vals):
                names += [p.strip() for p in vals[i + 1].split(",") if p.strip()]
                i += 2
                continue
            if la in ("-inputobject",) and i + 1 < len(vals):
                var_target(vals[i + 1])
                i += 2
                continue
            if la.startswith("-") or re.match(r"[\d*]?>", la):
                i += 1
                continue
            if a.isdigit():
                pids.append(int(a))
            elif a.startswith("$"):
                var_target(a)
            elif a:
                names.append(a)
            i += 1
        if piped or not (pids or names or ports or unknown or vetted):
            # Fed from a pipeline: Get-Process <name|id> | Stop-Process, a CIM
            # process lookup, or the owner of a port.
            cim = _cim_stop_targets(code, lits)
            producer = re.search(r"\b(?:get-process|gps)\b([^|;\n]*)\|\s*(?:stop-process|spps|kill)\b",
                                 code, re.IGNORECASE)
            piped_var = re.search(r"\$(\w+)\s*\|\s*(?:stop-process|spps|kill)\b", code, re.IGNORECASE) \
                or re.search(r"(?:-id|-inputobject)\s+\$(\w+)", code, re.IGNORECASE)
            if not producer and piped_var:
                # $p = Get-Process -Name X ...; $p | Stop-Process
                producer = re.search(rf"\${re.escape(piped_var.group(1))}\s*=\s*(?:get-process|gps)\b([^|;\n]*)",
                                     code, re.IGNORECASE)
            port = re.search(r"get-nettcpconnection\b[^|;\n]*-localport\s+(\d+)", code, re.IGNORECASE)
            if cim is not None:
                if cim[0] is None:
                    unknown = True
                else:
                    pids += cim[1]
            elif producer:
                found = 0
                for tok in producer.group(1).split():
                    t = lit(tok).strip(",")
                    if t.startswith("-") or t.lower() in ("silentlycontinue", "stop", "ignore", "continue"):
                        continue
                    (pids.append(int(t)) if t.isdigit() else names.append(t))
                    found += 1
                unknown = unknown or not found
            elif port:
                ports.append(int(port.group(1)))
            else:
                unknown = True
    if unknown:
        return ["stops a program that can't be checked first"]
    table = _process_table()
    model_pids = _model_server_pids()
    reasons: list[str] = []
    for pid in pids:
        r = _pid_stop_reason(pid, table=table, model_pids=model_pids)
        if r:
            reasons.append(r)
    for n in names:
        reasons += _stop_name_reasons(n, table, model_pids)
    if ports:
        try:
            owners = [pid for port, pid in _local_listeners() if port in ports]
        except Exception:
            owners = []
        for pid in owners:
            r = _pid_stop_reason(pid, table=table, model_pids=model_pids)
            if r:
                reasons.append(r)
    return list(dict.fromkeys(reasons))


def _own_command_line() -> str:
    return " ".join([sys.executable] + list(sys.argv))


def _cim_stop_targets(code: str, lits: list[str]):
    """Get-CimInstance / Get-WmiObject Win32_Process -Filter "Name='x.exe'"
    [| Where-Object { $_.CommandLine -like '*y*' }] | ... Stop-Process.
    Returns None when the command has no such lookup, (None, []) when it can't
    be followed, else (names, pids) of the processes it would stop. A
    CommandLine filter is checked against each process when psutil can read
    command lines, and always against Accuretta's own."""
    m = re.search(r"\b(?:get-ciminstance|gcim|get-wmiobject|gwmi)\b[^|;\n]*win32_process\b([^|;\n]*)", code,
                  re.IGNORECASE)
    if not m or not re.search(r"\b(?:stop-process|spps|kill|remove-ciminstance|terminate)\b", code, re.IGNORECASE):
        return None
    filt = re.search(r'-filter\s+"(\d+)"', m.group(1), re.IGNORECASE)
    text = lits[int(filt.group(1))] if filt else ""
    name = re.search(r"name\s*=\s*'([^']+)'", text, re.IGNORECASE)
    if not name:
        return None, []
    image = name.group(1).lower()
    image = image[:-4] if image.endswith(".exe") else image
    like = re.search(r'commandline\s+-(like|match)\s+"(\d+)"', code, re.IGNORECASE)
    pattern = lits[int(like.group(2))] if like else ""
    def matches(cmdline: str) -> bool:
        if not like:
            return True
        if like.group(1).lower() == "like":
            return fnmatch.fnmatch(cmdline.lower(), pattern.lower())
        try:
            return bool(re.search(pattern, cmdline, re.IGNORECASE))
        except re.error:
            return True
    if like and matches(_own_command_line()):
        return None, []                    # the filter would match Accuretta itself
    pids = []
    for pid, (_, pname) in _process_table().items():
        if pname != image or pid == os.getpid():
            continue
        if like:
            try:
                import psutil                                    # optional
                cmdline = " ".join(psutil.Process(pid).cmdline())
            except Exception:
                cmdline = ""
            if cmdline and not matches(cmdline):
                continue
        pids.append(pid)
    return [image], pids
