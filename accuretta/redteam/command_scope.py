# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team scope for shell commands: which hosts a command reaches, and blocking
network commands aimed outside the mission's scope.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission import _rt_passive_mission
from accuretta.redteam.scope import _rt_tok_matches
from accuretta.security.data_guard import _DATA_READABLE_SUBDIRS


def _rt_scope_out_tokens(chat: dict | None) -> list[str]:
    """Out-of-scope tokens from the structured mission scope_out list, falling
    back to the legacy 'out:' segment of mission.scope. Only fires when an
    explicit out list was declared."""
    m = _rt_mission_get(chat) if isinstance(chat, dict) else None
    if m:
        structured = [str(t).strip().lower() for t in (m.get("scope_out") or [])
                      if str(t).strip()]
        if structured:
            return structured
    scope = str((m or {}).get("scope", "")) if m else ""
    low = scope.lower()
    if "out:" not in low:
        return []
    seg = scope[low.index("out:") + 4:]
    toks = []
    for t in re.split(r"[,\s|]+", seg):
        t = re.sub(r"^https?://", "", t.strip().strip("/")).lower()
        if t and t not in ("out", "in", "scope"):
            toks.append(t)
    return toks


def _rt_scope_in_tokens(chat: dict | None) -> list[str]:
    """Authoritative in-scope allowlist.

    An explicit structured scope_in list wins; then the legacy ``in:``
    segment. If neither is present, the confirmed mission target is the only
    allowed host. This is intentionally allowlist-based: an ``out:`` denylist
    alone cannot stop model drift to a host the questionnaire never mentioned.
    """
    m = _rt_mission_get(chat) if isinstance(chat, dict) else None
    if not m:
        return []
    structured = [str(t).strip().lower() for t in (m.get("scope_in") or [])
                  if str(t).strip()]
    if structured:
        return structured
    scope = str(m.get("scope") or "")
    low = scope.lower()
    seg = ""
    if "in:" in low:
        start = low.index("in:") + 3
        end = low.find("out:", start)
        seg = scope[start:end if end >= 0 else None]
    if not seg.strip():
        seg = str(m.get("target") or "")
    toks = []
    for t in re.split(r"[,\s|]+", seg):
        t = t.strip().strip("/()[]{}'\"").lower()
        t = re.sub(r"^https?://", "", t)
        if "/" in t and not re.fullmatch(r"[0-9a-f:.]+/\d{1,3}", t):
            t = t.split("/", 1)[0]
        if t and t not in ("out", "in", "scope"):
            toks.append(t)
    return toks


_RT_NETWORK_CMD_RE = re.compile(
    r"\b(?:curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm|nmap|sqlmap|"
    r"nikto|ffuf|gobuster|dirsearch|feroxbuster|netcat|ncat|nc|telnet|ssh|scp|"
    r"sftp|ftp|dig|nslookup|host|openssl\s+s_client|test-netconnection|tnc)\b",
    re.IGNORECASE,
)


# Code that can open a connection: Python/Node/PowerShell network libraries.
_RT_CODE_NETWORK_RE = re.compile(
    r"\b(?:requests|urllib|urllib3|http\.client|httpx|aiohttp|socket|websockets?|selenium|playwright|"
    r"scapy|paramiko|ftplib|smtplib|telnetlib|pycurl|treq|grequests|dns\.resolver|"
    r"fetch(?=\s*\()|axios|node-fetch|https?\.(?:get|request)|net\.(?:connect|createconnection)|"
    r"system\.net\.|net\.webclient|net\.sockets|webclient|tcpclient|"
    r"invoke-webrequest|invoke-restmethod|iwr|irm|start-bitstransfer|resolve-dnsname)\b",
    re.IGNORECASE)


_RT_CMD_FILE_EXTS = (".py", ".js", ".txt", ".gz", ".xz", ".img", ".bin", ".zip", ".tar",
                     ".json", ".md", ".html", ".css", ".apk", ".so", ".elf", ".yar",
                     ".pcap", ".evtx", ".ps1", ".csv", ".log", ".xml", ".yaml", ".yml")


# Endings a host outside a URL is taken to have: every two-letter country
# ending plus the common generic ones. Code like re.findall, json.loads or
# $c.Length ends in a word that is none of these, so it no longer reads as a
# website. Anything after "://" or "@" counts whatever its ending.
_RT_CMD_GENERIC_TLDS = frozenset((
    "com net org io dev app ai co info biz gov edu mil int xyz online site tech cloud "
    "shop store page me tv cc ninja live pro work world web blog news one top club "
    "agency digital network systems solutions services email link click host space "
    "website fun icu vip lol bet casino finance bank money wtf").split())


def _rt_command_hosts(scrub: str) -> set[str]:
    """Literal hosts in a lower-cased shell command, without code identifiers."""
    hosts = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", scrub))
    for match in re.finditer(r"(?<![\w$.])((?:[a-z0-9-]+\.)+[a-z]{2,})\b(\s*\()?", scrub):
        host = match.group(1)
        if host.endswith(_RT_CMD_FILE_EXTS):
            continue
        start = match.start(1)
        in_url = scrub[max(0, start - 3):start] == "://" or scrub[start - 1:start] == "@"
        if not in_url:
            if match.group(2):                   # re.findall( / os.path.join(
                continue
            tld = host.rsplit(".", 1)[1]
            if len(tld) != 2 and tld not in _RT_CMD_GENERIC_TLDS:
                continue
        hosts.add(host)
    return hosts


def _rt_command_data_block(command: str) -> str | None:
    """Refuse an engagement shell command that names Accuretta's data folder.

    File tools already refuse that folder; a shell command could otherwise
    read or write it anyway (a run once wrote a 650 KB scratch file there).
    The reports and evidence folders stay reachable, as they are for the file
    tools, so a run can still copy its own evidence out to work on it."""
    low = command.lower().replace("/", "\\")
    root = os.path.normcase(str(DATA)).lower().replace("/", "\\").rstrip("\\")
    start = low.find(root)
    while start >= 0:
        rest = low[start + len(root):]
        sub = re.match(r"\\+([^\\\s'\"`;|&]*)", rest)
        if not (sub and sub.group(1) in _DATA_READABLE_SUBDIRS):
            return ("refused: Accuretta's own data folder (settings, keys and chats) is off "
                    "limits to commands. Keep working files in the workspace; evidence "
                    "under data\\recon_evidence and reports may be read.")
        start = low.find(root, start + 1)
    return None


def _rt_command_scope_block(command: str, chat: dict | None) -> str | None:
    """Scope network-capable generic shell commands during an engagement."""
    if not _rt_authorized_mission(chat):
        return None
    scrub = re.sub(r"/mnt/[a-z]/\S+", " ", str(command or "").lower())
    if _rt_passive_mission(chat) and _RT_NETWORK_CMD_RE.search(scrub):
        return ("refused: passive OSINT forbids shell-driven network access. "
                "Use the public-source OSINT tools supplied for this mission.")
    data_block = _rt_command_data_block(str(command or ""))
    if data_block:
        return data_block
    # Only a command that can reach the network is scope-checked: grepping saved
    # evidence that names a CDN or analytics host is local work, not contact.
    networked = bool(_RT_NETWORK_CMD_RE.search(scrub) or _RT_CODE_NETWORK_RE.search(scrub))
    if not networked:
        return None
    hosts = _rt_command_hosts(scrub)
    if _RT_NETWORK_CMD_RE.search(scrub) and not hosts:
        return ("refused: this active red-team mission uses a network-capable generic command, "
                "but its destination is not a literal host the harness can scope-check. Use the "
                "typed red-team tool for this action or include the in-scope host explicitly.")
    allowed = _rt_scope_in_tokens(chat)
    denied = _rt_scope_out_tokens(chat)
    for host in hosts:
        pm = re.search(re.escape(host) + r"[^\d]{1,12}(?:-p\s*|port[=\s:])(\d{2,5})", scrub)
        port = int(pm.group(1)) if pm else None
        if any(_rt_tok_matches(tok, host, port) for tok in denied):
            return f"refused: generic command references out-of-scope host {host}."
        if not any(_rt_tok_matches(tok, host, port) for tok in allowed):
            return (f"refused: generic command references {host}, which is not in the "
                    f"user-authorized scope ({', '.join(allowed) or 'no targets'}).")
    return None


def _sandbox_scope_block(command: str) -> str | None:
    """Allowlist-check generic guest commands during an active RT mission."""
    try:
        chat_id = _current_chat_id.get() or ""
        chat = (get_chats().get("chats", {}) or {}).get(chat_id) if chat_id else None
    except Exception:
        chat = None
    return _rt_command_scope_block(command, chat)
