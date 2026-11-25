# SPDX-License-Identifier: AGPL-3.0-only
"""Hard stops: weakening security, destroying backups, secrets leaving the PC.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.security.data_guard import _soft_data_text_hit
from accuretta.security.shell_text import _command_words


# -- hard stops -------------------------------------------------------------------
_DANGER_BLOCK_RES = tuple((re.compile(p, re.IGNORECASE), why) for p, why in (
    (r"\bvssadmin(?:\.exe)?\s+(?:delete\s+shadows|resize\s+shadowstorage)\b",
     "deletes Windows' backup snapshots (shadow copies)"),
    (r"\bwmic(?:\.exe)?\b[^\n;|&]*\bshadowcopy\b[^\n;|&]*\bdelete\b",
     "deletes Windows' backup snapshots (shadow copies)"),
    (r"win32_shadowcopy\b[^\n]*(?:\.delete\s*\(|\bremove-(?:ciminstance|wmiobject)\b)",
     "deletes Windows' backup snapshots (shadow copies)"),
    (r"\bwbadmin(?:\.exe)?\s+delete\b", "deletes Windows backups"),
    (r"\bbcdedit(?:\.exe)?\b[^\n;|&]*\b(?:recoveryenabled\s+(?:no|off|0|false)|bootstatuspolicy\s+ignoreallfailures)\b",
     "turns off Windows recovery"),
    (r"\breagentc(?:\.exe)?\s+/disable\b", "turns off Windows recovery"),
    (r"\bdisable-computerrestore\b", "turns off System Restore"),
    (r"\breg(?:\.exe)?\s+save\s+[\"']?(?:hklm|hkey_local_machine)\\(?:sam|security|system)\b",
     "copies the Windows password database"),
    (r"\bfsutil(?:\.exe)?\s+usn\s+deletejournal\b", "erases the file-change journal"),
))


_DANGER_CRITICAL_RES = tuple((re.compile(p, re.IGNORECASE), why) for p, why in (
    (r"\bset-mppreference\b[^\n;|]*-disable\w+(?:\s*:)?\s+\$?(?:true|1)\b", "turns off part of Windows Defender"),
    (r"\b(?:set|add)-mppreference\b[^\n;|]*-exclusion(?:path|process|extension|ipaddress)\b",
     "adds a Windows Defender exclusion"),
    (r"\b(?:sc(?:\.exe)?\s+(?:stop|config|delete|failure)|(?:stop|set|suspend)-service\b[^\n;|]*?)\s+"
     r"(?:-name\s+)?[\"']?windefend\b", "stops or changes the Windows Defender service"),
    (r"\bmpcmdrun(?:\.exe)?\b[^\n]*-removedefinitions\b", "removes Windows Defender's virus definitions"),
    (r"\bnetsh(?:\.exe)?\b[^\n;|&]*\badvfirewall\s+set\s+\S+\s+state\s+off\b", "turns off the Windows firewall"),
    (r"\bnetsh(?:\.exe)?\b[^\n;|&]*\bfirewall\s+set\s+opmode\s+(?:mode\s*=\s*)?disable\b",
     "turns off the Windows firewall"),
    (r"\bnetsh(?:\.exe)?\b[^\n;|&]*\badvfirewall\s+reset\b", "resets the Windows firewall rules"),
    (r"\bset-netfirewallprofile\b[^\n;|]*-enabled(?:\s*:)?\s+\$?(?:false|0)\b", "turns off the Windows firewall"),
    (r"\bwevtutil(?:\.exe)?\s+(?:cl|clear-log)\b", "clears a Windows event log"),
    (r"\b(?:clear|remove|limit)-eventlog\b", "clears or removes a Windows event log"),
    (r"\bauditpol(?:\.exe)?\s+/(?:set|clear|remove|restore)\b", "changes Windows security auditing"),
    (r"\bbcdedit(?:\.exe)?\b[^\n;|&]*\b(?:testsigning|nointegritychecks)\s+on\b",
     "turns off driver signature checks"),
    (r"\bschtasks(?:\.exe)?\b[^\n;|&]*\s/create\b", "creates a scheduled task, which runs by itself later"),
    (r"\bregister-scheduledtask\b", "creates a scheduled task, which runs by itself later"),
    (r"\bsc(?:\.exe)?\s+create\b|\bnew-service\b", "installs a Windows service"),
    (r"(?:\b(?:copy-item|cpi|copy|cp|move-item|mv|move|new-item|ni|set-content|add-content|out-file)\b|>)"
     r"[^\n;|]*(?:\\start menu\\programs\\startup\b|shell:startup)", "puts something in the Startup folder"),
    (r"\bnet(?:\.exe)?\s+user\s+\S+(?:\s+\S+)?\s+/add\b|\bnew-localuser\b", "creates a Windows user account"),
    (r"\bnet(?:\.exe)?\s+localgroup\s+[\"']?administrators[\"']?\b[^\n;|&]*/add\b"
     r"|\badd-localgroupmember\b[^\n;|]*administrators", "makes an account an administrator"),
))


# Private keys, cloud and Git credentials, browser password stores, and
# Accuretta's own key files. `.pub` keys are public.
_SECRET_PATH_RE = re.compile(
    r"(?:\.ssh[\\/](?:id_[\w-]+\b(?!\.pub)|[\w.-]+\.(?:pem|key|ppk)\b)"
    r"|\.aws[\\/]credentials\b|\.azure[\\/]|\.config[\\/]gh[\\/]hosts\.yml"
    r"|\.git-credentials\b|[\\/_.]netrc\b|\.docker[\\/]config\.json|\.kube[\\/]config\b"
    r"|\\user data\\[^\n\"']*\\(?:login data|cookies|web data|local state)\b"
    r"|\blogins\.json\b|\bkey4\.db\b|\bgithub_cred\.json\b|\bbridge_mcp_config\.json\b)",
    re.IGNORECASE)


_SECRET_SEND_RE = re.compile(
    r"\b(?:invoke-webrequest|iwr|invoke-restmethod|irm|curl(?:\.exe)?|wget|scp|sftp|ftp|ncat|netcat|nc|"
    r"send-mailmessage|start-bitstransfer|bitsadmin|rclone|uploadfile|uploadstring|uploaddata|"
    r"tcpclient|net\.sockets)\b"
    r"|(?:^|[\s\"'=,(])\\\\(?!wsl(?:\$|\.localhost)\\|\?\\|\.\\)[\w.-]+\\",
    re.IGNORECASE)


_SECRET_PY_NET_RE = re.compile(r"\b(?:requests|urllib|http\.client|httpx|aiohttp|smtplib|ftplib|socket)\b")


_SECRET_IDENTITY_ARG_RE = re.compile(
    r"(?:\s-i\s+(?:\"[^\"]*\"|'[^']*'|\S+)|identityfile\s*=\s*\S+|key_filename\s*=\s*\S+)", re.IGNORECASE)


# Where a real credential file starts: a drive, home (~, $HOME, $env:USERPROFILE,
# %APPDATA%), a share, or a Linux/macOS home. A web route such as "/.ssh/id_rsa"
# in a honeypot or a scanner's wordlist is not a file on this PC.
_SECRET_ANCHOR_RE = re.compile(
    r"^[\"'(@]*(?:[a-z]:[\\/]|~[\\/]|\$env:\w+|\$\{?home\}?|\$\{?userprofile\}?|%\w+%|\\\\|/home/|/root/|/users/|"
    r"/mnt/[a-z]/|\$\w+[\\/])", re.IGNORECASE)


_SECRET_UNANCHORED_RE = re.compile(r"github_cred\.json|bridge_mcp_config\.json", re.IGNORECASE)


def _secret_named(text: str) -> bool:
    """A credential store on this PC is named (keys passed to ssh/scp with -i
    don't count: that's using a key, not reading it)."""
    stripped = _SECRET_IDENTITY_ARG_RE.sub(" ", text or "")
    if _soft_data_text_hit(stripped):
        return True
    for m in _SECRET_PATH_RE.finditer(stripped):
        if _SECRET_UNANCHORED_RE.match(m.group(0)):
            return True
        start = max(stripped.rfind(c, 0, m.start()) for c in " \t\n\"'=,;|(") + 1
        if _SECRET_ANCHOR_RE.match(stripped[start:m.end()]):
            return True
    return False


def _danger_assess(cmd: str) -> tuple[str, str] | None:
    """("block", why) for actions no one should hand an agent, ("critical", why)
    for ones that get the hold-to-approve card in every mode, else None."""
    words = _command_words(cmd)
    if not words:
        return None
    low = re.sub(r"[ \t]+", " ", words.lower())
    for pattern, why in _DANGER_BLOCK_RES:
        if pattern.search(low):
            return "block", why
    if _secret_named(low) and _SECRET_SEND_RE.search(_SECRET_IDENTITY_ARG_RE.sub(" ", low)):
        return "block", "sends a private key or saved password off this PC"
    for pattern, why in _DANGER_CRITICAL_RES:
        if pattern.search(low):
            return "critical", why
    return None
