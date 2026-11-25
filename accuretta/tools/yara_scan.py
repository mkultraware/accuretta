# SPDX-License-Identifier: AGPL-3.0-only
"""The yara_scan tool and its built-in rule set for common malware indicators.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.optional_imports import _HAVE_YARA
from accuretta.core.optional_imports import _yara
from accuretta.security.path_guards import _data_file_in_walk
from accuretta.security.path_guards import _fw_check_path
from accuretta.security.path_guards import _is_data_dir
from accuretta.tools.binary_triage import _yara_string_to_dict
from typing import Any


# --- YARA -----------------------------------------------------------------
# A small bundled rule set. Keep these intentionally narrow — false positives
# train models to ignore the tool. Each rule uses `condition: <N> of them`
# with N small enough to fire on real samples but big enough that a benign
# binary with one match doesn't trigger.
_DEFAULT_YARA_RULES = r"""
rule SuspiciousURL
{
    meta:
        description = "URL referencing pastebin/discord/tempfile hosts often used by stagers"
        author = "accuretta"
    strings:
        $u1 = "pastebin.com/raw/" nocase ascii wide
        $u2 = "hastebin.com/raw/" nocase ascii wide
        $u3 = "ghostbin.com/paste/" nocase ascii wide
        $u4 = "discordapp.com/api/webhooks/" nocase ascii wide
        $u5 = "transfer.sh/" nocase ascii wide
        $u6 = "anonfile.com/" nocase ascii wide
        $u7 = "ngrok.io/" nocase ascii wide
        $u8 = "duckdns.org" nocase ascii wide
        $u9 = "bit.ly/" nocase ascii wide
    condition:
        any of them
}

rule HardcodedIPv4
{
    meta:
        description = "Possible hardcoded IPv4 (loose; many false positives in version strings)"
    strings:
        $ip = /[^0-9.]([1-9][0-9]{0,2}\.){3}[1-9][0-9]{0,2}[^0-9.]/ ascii wide
    condition:
        #ip > 3
}

rule WindowsRegistryAutorun
{
    meta:
        description = "Strings referencing Windows Run/RunOnce autorun keys"
    strings:
        $r1 = "Software\\Microsoft\\Windows\\CurrentVersion\\Run" nocase ascii wide
        $r2 = "Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce" nocase ascii wide
        $r3 = "Software\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" nocase ascii wide
        $r4 = "Software\\Microsoft\\Active Setup\\Installed Components" nocase ascii wide
    condition:
        any of them
}

rule SuspiciousAPIs
{
    meta:
        description = "Combination of process-injection / persistence APIs"
    strings:
        $a1 = "VirtualAllocEx" ascii wide
        $a2 = "WriteProcessMemory" ascii wide
        $a3 = "CreateRemoteThread" ascii wide
        $a4 = "NtUnmapViewOfSection" ascii wide
        $a5 = "SetWindowsHookExA" ascii wide
        $a6 = "SetWindowsHookExW" ascii wide
        $a7 = "QueueUserAPC" ascii wide
        $a8 = "RtlCreateUserThread" ascii wide
    condition:
        3 of them
}

rule PowerShellEncoded
{
    meta:
        description = "powershell -enc / -encodedcommand invocation"
    strings:
        $p1 = "powershell" nocase ascii wide
        $p2 = " -enc " nocase ascii wide
        $p3 = " -encodedcommand" nocase ascii wide
        $p4 = "-NoProfile -ExecutionPolicy Bypass" nocase ascii wide
    condition:
        $p1 and any of ($p2, $p3, $p4)
}

rule Base64ExecutableHeader
{
    meta:
        description = "Base64-encoded PE (MZ) header — common dropper pattern"
    strings:
        // 'MZ' followed by the standard PE preamble (\x90\x00\x03), b64-encoded
        $b1 = "TVqQAAMAAAAEAAAA" ascii wide
        $b2 = "TVoAAAAAAAAAAAAA" ascii wide
        // 'MZ\x00\x00' / common DOS stub variants
        $b3 = "TVoAAAAA" ascii wide
    condition:
        any of them
}

rule MimikatzStrings
{
    meta:
        description = "Strings characteristic of mimikatz"
    strings:
        $m1 = "sekurlsa::logonpasswords" nocase ascii wide
        $m2 = "kerberos::list" nocase ascii wide
        $m3 = "lsadump::sam" nocase ascii wide
        $m4 = "mimikatz" nocase ascii wide
        $m5 = "gentilkiwi" nocase ascii wide
    condition:
        2 of them
}
"""


def _yara_compile(rules_arg: str) -> tuple[Any, dict | None]:
    """Compile YARA rules from one of: (a) absolute path to a .yar/.yara file,
    (b) inline source string, or (c) empty/missing -> bundled defaults.
    Returns (rules_object, error_or_None)."""
    if not _HAVE_YARA:
        return None, {"error": "yara-python not installed. Run: pip install yara-python"}
    src = (rules_arg or "").strip()
    try:
        if src and os.path.isfile(src):
            return _yara.compile(filepath=src), None
        if src:
            return _yara.compile(source=src), None
        return _yara.compile(source=_DEFAULT_YARA_RULES), None
    except Exception as e:
        return None, {"error": f"yara compile failed: {type(e).__name__}: {e}"}


def tool_yara_scan(args: dict) -> dict:
    """Scan a file or directory with YARA. By default uses a bundled rule set;
    pass rules='C:\\path\\to\\my.yar' to point at a custom file or rules='rule X
    {...}' to compile inline source. Returns matches grouped per file."""
    if not _HAVE_YARA:
        return {"error": "yara-python not installed. Run: pip install yara-python"}

    target_arg = args.get("path") or ""
    if not target_arg:
        return {"error": "missing path"}

    path, err = _fw_check_path(target_arg)
    if err:
        return err

    rules_arg = (args.get("rules") or "").strip()
    # If `rules` looks like a filesystem path inside the workspace, sandbox it.
    if rules_arg and ("\\" in rules_arg or "/" in rules_arg or rules_arg.lower().endswith((".yar", ".yara"))):
        rp, rerr = _fw_check_path(rules_arg, must_exist=True)
        if rerr:
            return rerr
        rules_arg = rp

    rules, cerr = _yara_compile(rules_arg)
    if cerr:
        return cerr

    recursive = bool(args.get("recursive", False))
    max_files = int(args.get("max_files") or 200)
    if max_files < 1:
        max_files = 1
    if max_files > 5000:
        max_files = 5000
    timeout = int(args.get("timeout") or 60)
    if timeout < 1:
        timeout = 1
    if timeout > 600:
        timeout = 600
    max_size = int(args.get("max_size") or 200 * 1024 * 1024)  # 200 MiB
    if max_size < 1024:
        max_size = 1024

    targets: list[str] = []
    if os.path.isfile(path):
        targets.append(path)
    elif os.path.isdir(path):
        if recursive:
            for root, _dirs, files in os.walk(path):
                _dirs[:] = [d for d in _dirs if not _is_data_dir(os.path.join(root, d))]
                for n in files:
                    if _data_file_in_walk(os.path.join(root, n)):
                        continue
                    targets.append(os.path.join(root, n))
                    if len(targets) >= max_files:
                        break
                if len(targets) >= max_files:
                    break
        else:
            try:
                for n in os.listdir(path):
                    fp = os.path.join(path, n)
                    if os.path.isfile(fp) and not _data_file_in_walk(fp):
                        targets.append(fp)
                    if len(targets) >= max_files:
                        break
            except OSError as e:
                return {"error": f"listdir failed: {e}"}
    else:
        return {"error": f"not a file or directory: {path}"}

    results: list[dict] = []
    rules_fired: set[str] = set()
    files_with_hits = 0
    skipped = 0
    for fp in targets:
        try:
            sz = os.path.getsize(fp)
        except OSError:
            skipped += 1
            continue
        if sz > max_size:
            skipped += 1
            continue
        try:
            matches = rules.match(fp, timeout=timeout)
        except Exception as e:
            results.append({"path": fp, "error": f"{type(e).__name__}: {e}"})
            continue
        if not matches:
            continue
        files_with_hits += 1
        ms_out = []
        for m in matches:
            try:
                tags = list(getattr(m, "tags", []) or [])
                meta = dict(getattr(m, "meta", {}) or {})
                ns = getattr(m, "namespace", "")
                strings = []
                for s in getattr(m, "strings", []) or []:
                    strings.append(_yara_string_to_dict(s))
                rules_fired.add(m.rule)
                ms_out.append({
                    "rule": m.rule,
                    "namespace": ns,
                    "tags": tags,
                    "meta": meta,
                    "strings": strings[:20],
                })
            except Exception:
                continue
        results.append({"path": fp, "matches": ms_out})

    return {
        "target": path,
        "rules_source": (
            "custom_file" if (rules_arg and os.path.isfile(rules_arg))
            else ("inline" if rules_arg else "bundled_defaults")
        ),
        "files_scanned": len(targets) - skipped,
        "files_skipped": skipped,
        "files_with_matches": files_with_hits,
        "rules_fired": sorted(rules_fired),
        "matches": results,
    }
