# SPDX-License-Identifier: AGPL-3.0-only
"""Whether a PowerShell or cmd command writes to the Windows registry, and whether that
touches the user's or the whole system's settings.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import sys
from accuretta.security.shell_text import _command_words
from accuretta.security.child_env import _child_env


# ---- registry guardrails ---------------------------------------------------
# Registry writes have the highest blast radius of anything the agent can do
# on Windows, so protection is split by hive:
#
#   - SYSTEM hives (HKLM, HKCR, HKU): hard-refused at the bridge; no approval
#     card is shown.
#   - USER hives (HKCU, HKCC): routed to the REGISTRY EDIT approval card with
#     hold-to-approve, so a single mis-click can't push a write through.
#
# Opaque imports (`reg import file.reg`, `regedit /s file.reg`) count as SYSTEM
# because the target hive isn't visible in the command, and .reg files in the
# wild routinely write HKLM.
_REG_SYSTEM_HIVES = (
    "HKLM:", "HKCR:", "HKU:",
    "HKEY_LOCAL_MACHINE", "HKEY_CLASSES_ROOT", "HKEY_USERS",
)


_REG_USER_HIVES = (
    "HKCU:", "HKCC:",
    "HKEY_CURRENT_USER", "HKEY_CURRENT_CONFIG",
)


_REG_WRITE_VERBS = re.compile(
    r"\b("
    r"Set-ItemProperty|New-ItemProperty|Remove-ItemProperty|Clear-ItemProperty"
    r"|New-Item|Remove-Item|Rename-Item|Copy-Item|Move-Item"
    r"|reg\s+(?:add|delete|copy|restore)"
    r")\b",
    re.IGNORECASE,
)


_REG_OPAQUE_IMPORT = re.compile(
    r"\b(reg\s+import\b|regedit(?:\.exe)?\s+(?:/s\b|-s\b))",
    re.IGNORECASE,
)


_REG_LAUNCH_REGEDIT = re.compile(
    r"\b(Start-Process\s+regedit|^\s*regedit(?:\.exe)?\b|;\s*regedit(?:\.exe)?\b)",
    re.IGNORECASE,
)


def registry_assess(cmd: str) -> dict:
    """Classify whether a PowerShell/cmd string writes to the registry, and
    at what blast-radius. Returns {"level": "none"|"user"|"system",
    "targets": [matched hive references]}.

    Conservative on ambiguity — `reg import` / `regedit /s` and bare
    `regedit` launches escalate to "system" because their actual targets
    aren't visible in the cmd string.
    """
    if not cmd:
        return {"level": "none", "targets": []}
    # Command words only: a commit message that mentions HKLM next to a
    # Remove-Item of a temp file is not a registry edit.
    cmd = _command_words(cmd) or cmd
    # Opaque imports / regedit launches: can't see the target, assume worst.
    if _REG_OPAQUE_IMPORT.search(cmd) or _REG_LAUNCH_REGEDIT.search(cmd):
        return {"level": "system", "targets": ["(opaque: .reg file or regedit launch)"]}
    # A write verb alone isn't enough (Set-ItemProperty / New-Item also work on
    # filesystem PSDrives); a hive reference must be present too.
    if not _REG_WRITE_VERBS.search(cmd):
        return {"level": "none", "targets": []}
    system_hits: list[str] = []
    user_hits: list[str] = []
    # Match hive prefix + the path that follows it. Stop at whitespace,
    # quote, comma, semicolon, or backtick (PowerShell line continuation).
    for hive in _REG_SYSTEM_HIVES:
        for m in re.finditer(re.escape(hive) + r"[\\:\w.()$ -]*", cmd, re.IGNORECASE):
            system_hits.append(m.group(0).strip())
    for hive in _REG_USER_HIVES:
        for m in re.finditer(re.escape(hive) + r"[\\:\w.()$ -]*", cmd, re.IGNORECASE):
            user_hits.append(m.group(0).strip())
    if system_hits:
        # Dedupe preserving order. User hits ride along so the approval card shows the
        # full picture, though the level comes from the more dangerous one.
        seen = set()
        targets = []
        for t in system_hits + user_hits:
            if t.lower() not in seen:
                seen.add(t.lower())
                targets.append(t)
        return {"level": "system", "targets": targets}
    if user_hits:
        seen = set()
        targets = []
        for t in user_hits:
            if t.lower() not in seen:
                seen.add(t.lower())
                targets.append(t)
        return {"level": "user", "targets": targets}
    # Write verb found but no hive reference → not a registry write
    # (e.g. Set-ItemProperty against a filesystem path).
    return {"level": "none", "targets": []}


def _notch_registry_diff(cmd: str) -> dict:
    """Read-only old/new rows for a registry gate's diff view (notch overlay).

    Parses the PENDING command (never executes it), then reads the CURRENT
    value with `reg query` — a strictly read-only binary, invoked with a
    fixed argv and no shell. Only `reg add` / `reg delete` and simple
    PowerShell *-ItemProperty forms are parsed; anything else returns an
    empty result and the gate card shows the command without a diff.
    Fails toward the hold: parsing never affects the gate kind.
    """
    empty = {"path": "", "diff": []}
    if not cmd or sys.platform != "win32":
        return empty

    hives = ("HKLM", "HKCU", "HKCR", "HKU", "HKCC",
             "HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER", "HKEY_CLASSES_ROOT",
             "HKEY_USERS", "HKEY_CURRENT_CONFIG")

    def _clean_key(k: str) -> str:
        k = k.strip().strip('"').replace("/", "\\")
        # PowerShell drive form -> reg.exe form (HKCU:\Software -> HKCU\Software)
        k = re.sub(r"^(HK[A-Z]{2}|HKEY_[A-Z_]+):\\?", lambda m: m.group(1) + "\\", k, flags=re.I)
        return k

    def _valid_key(k: str) -> bool:
        if not k or len(k) > 512:
            return False
        if not any(k.upper().startswith(h.upper() + "\\") for h in hives):
            return False
        # Conservative charset: letters, digits, spaces and common path chars
        # only. Nothing here is ever shelled out (fixed argv), but tight
        # validation keeps weird input from reaching even reg.exe query.
        return bool(re.fullmatch(r"[A-Za-z0-9 _\-.(){}\\]+", k))

    def _query(key: str, value: str) -> tuple[str, str] | None:
        """(type, data) of the current value, or None when unreadable."""
        import subprocess
        argv = ["reg", "query", key]
        if value:
            argv += ["/v", value]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=8, env=_child_env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            return None
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            m = re.match(r"\s*(\S.*?)\s{2,}(REG_\w+)\s+(.*)$", line)
            if m and (not value or m.group(1).strip().lower() == value.lower()):
                return m.group(2), m.group(3).strip()
        return None

    # --- reg add "KEY" /v NAME /t TYPE /d DATA [/f] ---
    m = re.search(r"\breg(?:\.exe)?\s+add\s+(\"[^\"]+\"|\S+)", cmd, re.I)
    if m:
        key = _clean_key(m.group(1))
        if not _valid_key(key):
            return empty
        vm = re.search(r"\s/v\s+(\"[^\"]+\"|\S+)", cmd, re.I)
        tm = re.search(r"\s/t\s+(REG_\w+)", cmd, re.I)
        dm = re.search(r"\s/d\s+(\"[^\"]*\"|\S+)", cmd, re.I)
        value = (vm.group(1).strip('"') if vm else "(Default)")
        vtype = (tm.group(1) if tm else "REG_SZ")
        data = (dm.group(1).strip('"') if dm else "")
        old = _query(key, "" if value == "(Default)" else value)
        new_row = f"{value}  {vtype}  {data}"
        if old:
            return {"path": key, "diff": [["-", f"{value}  {old[0]}  {old[1]}"], ["+", new_row]]}
        return {"path": key, "diff": [["+", new_row]]}

    # --- reg delete "KEY" [/v NAME] [/f] ---
    m = re.search(r"\breg(?:\.exe)?\s+delete\s+(\"[^\"]+\"|\S+)", cmd, re.I)
    if m:
        key = _clean_key(m.group(1))
        if not _valid_key(key):
            return empty
        vm = re.search(r"\s/v\s+(\"[^\"]+\"|\S+)", cmd, re.I)
        if vm:
            value = vm.group(1).strip('"')
            old = _query(key, value)
            if old:
                return {"path": key, "diff": [["-", f"{value}  {old[0]}  {old[1]}"]]}
            return {"path": key, "diff": [["-", value]]}
        return {"path": key, "diff": [["-", "(entire key)"]]}

    # --- PowerShell Set/New-ItemProperty -Path HKCU:\... -Name X -Value Y ---
    m = re.search(r"\b(?:Set|New)-ItemProperty\b", cmd, re.I)
    if m:
        pm = re.search(r"-Path\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd, re.I)
        nm = re.search(r"-Name\s+(\"[^\"]+\"|'[^']+'|\S+)", cmd, re.I)
        if not pm or not nm:
            return empty
        key = _clean_key(pm.group(1).strip("\"'"))
        if not _valid_key(key):
            return empty
        value = nm.group(1).strip("\"'")
        vm = re.search(r"-Value\s+(\"[^\"]*\"|'[^']*'|\S+)", cmd, re.I)
        data = vm.group(1).strip("\"'") if vm else ""
        old = _query(key, value)
        new_row = f"{value}  {data}"
        if old:
            return {"path": key, "diff": [["-", f"{value}  {old[0]}  {old[1]}"], ["+", new_row]]}
        return {"path": key, "diff": [["+", new_row]]}

    return empty
