# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team network scope: which hosts and ports a tool call reaches, whether they
resolve inside the mission scope or to this machine, which tools a mission may use, and
MCP calls aimed at a target.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.endpoints import LLAMA
from accuretta.core.endpoints import PORT
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.redteam.command_scope import _rt_scope_in_tokens
from accuretta.redteam.command_scope import _rt_scope_out_tokens
from accuretta.redteam.destructive import _rt_destructive_block
from accuretta.redteam.destructive import _rt_target_values
from accuretta.redteam.doh import _recon_http_doh
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_passive_mission
from accuretta.redteam.scope import _rt_endpoint_from_args
from accuretta.redteam.scope import _rt_is_local_host
from accuretta.redteam.scope import _rt_scope_token_host
from accuretta.redteam.scope import _rt_tok_matches
from typing import Any


def _rt_self_ports() -> set[int]:
    """Ports that belong to accuretta itself — never a red-team target."""
    ports = {int(PORT)}
    for u in (LLAMA, STATE.vision_llama):
        try:
            p = urllib.parse.urlparse(u).port
            if p:
                ports.add(int(p))
        except Exception:
            pass
    return ports


def _rt_resolves_to_local(host: str) -> bool:
    """Catch hostname aliases that resolve onto this machine/private loopback."""
    host = (host or "").strip().strip("[]")
    if _rt_is_local_host(host):
        return True
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, None)}
    except Exception:
        return False
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw)
            if ip.is_loopback or ip.is_unspecified:
                return True
        except ValueError:
            continue
    return False


_RT_NETWORK_SUFFIX_RE = re.compile(r"^([^/\s]+)/(\d{1,3})$")


def _rt_endpoints_from_args(args: dict) -> list[tuple[str, int | None, str]]:
    """(host, port, raw) for every destination in a tool call."""
    out: list[tuple[str, int | None, str]] = []
    seen: set[tuple[str, int | None]] = set()
    for raw in _rt_target_values(args):
        host, port = _rt_endpoint_from_args({"target": raw, "port": args.get("port")})
        if host and (host, port) not in seen:
            seen.add((host, port))
            out.append((host, port, raw))
    return out


def _rt_network_block(raw: str, allowed: list[str], denied: list[str]) -> str | None:
    """Refuse `host/prefix` unless the whole range was authorized.

    Scanners read 203.0.113.5/16 as 65,536 addresses, while a host check sees
    only 203.0.113.5. A range is allowed when an in-scope CIDR contains all of
    it and no out-of-scope entry falls inside it."""
    match = _RT_NETWORK_SUFFIX_RE.match(raw.strip())
    if not match:
        return None
    try:
        network = ipaddress.ip_network(raw.strip(), strict=False)
    except ValueError:
        return (f"refused: {raw} asks for a network range around a hostname. Name the "
                "single in-scope host, or an in-scope range written as an IP network.")
    if network.num_addresses == 1:
        return None

    def as_network(token: str):
        try:
            return ipaddress.ip_network(token.strip().lstrip("*."), strict=False)
        except ValueError:
            return None

    inside = any(n is not None and n.version == network.version and network.subnet_of(n)
                 for n in (as_network(t) for t in allowed if "/" in t))
    if not inside:
        return (f"refused: {raw} is a range of {network.num_addresses} addresses and the "
                "authorized scope does not cover all of it. Scan the single in-scope host instead.")
    for token in denied:
        n = as_network(token)
        if n is not None and n.version == network.version and n.overlaps(network):
            return f"refused: {raw} includes {token}, which is out of scope."
    return None


_RT_HOST_METADATA_TOOLS = {
    "recon_dns", "recon_subdomains", "recon_rdap", "recon_web_archive", "recon_shodan",
}


def _rt_metadata_parent_ok(tok: str, host: str) -> bool:
    """True when host is a registered parent of an in-scope host, e.g. example.com
    for portal.example.com. These lookups go to crt.sh, RDAP and DoH, never the
    target, and RDAP only answers for the registered domain."""
    token_host = _rt_scope_token_host(tok)
    if not token_host.endswith("." + host) or "." not in host:
        return False
    # Stop at the registered domain: co.uk, com.au and the like are not a parent.
    labels = host.split(".")
    return len(labels) > 2 or labels[0] not in {"co", "com", "net", "org", "gov", "ac", "edu"}


_RT_SCOPE_IP_CACHE: dict[str, tuple[float, frozenset]] = {}


_RT_SCOPE_IP_LOCK = threading.Lock()


_RT_SCOPE_IP_TTL = 300


def _rt_resolved_ips(name: str) -> frozenset:
    """IPv4 addresses for name via dns.google, cached for a few minutes."""
    now = time.time()
    with _RT_SCOPE_IP_LOCK:
        hit = _RT_SCOPE_IP_CACHE.get(name)
    if hit and now - hit[0] < _RT_SCOPE_IP_TTL:
        return hit[1]
    try:
        ips = frozenset(a for a in _recon_http_doh(name, "A", timeout=4.0)
                        if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", a))
    except Exception:
        return frozenset()
    with _RT_SCOPE_IP_LOCK:
        _RT_SCOPE_IP_CACHE[name] = (now, ips)
    return ips


def _rt_metadata_ip_ok(tok: str, host: str) -> bool:
    """True when host is an IPv4 address that an in-scope hostname, or its
    registered parent domain, currently resolves to, so Shodan and friends can
    look up the target's own IPs. Resolved via dns.google like recon_dns, so the
    check itself never contacts the target."""
    token_host = _rt_scope_token_host(tok)
    if not token_host:
        return False
    try:
        if ipaddress.ip_address(host).version != 4:
            return False
    except ValueError:
        return False
    labels = token_host.split(".")
    names = [token_host] + [p for p in (".".join(labels[i:]) for i in range(1, len(labels) - 1))
                            if _rt_metadata_parent_ok(tok, p)]
    return any(host in _rt_resolved_ips(n) for n in names)


def _rt_allow_matches(name: str, tok: str, host: str, port: int | None,
                      args: dict | None = None) -> bool:
    """Let host metadata checks omit an authorized port, and look up the parent
    domain or the resolved IPs of an in-scope host, without widening traffic scope."""
    if _rt_tok_matches(tok, host, port):
        return True
    if name not in _RT_HOST_METADATA_TOOLS or port is not None:
        return False
    args = args or {}
    # AXFR talks to the nameservers and would dump the whole parent zone.
    axfr = name == "recon_dns" and (str(args.get("mode") or "").lower() == "axfr"
                                    or bool(args.get("loud")))
    if not axfr and (_rt_metadata_parent_ok(tok, host) or _rt_metadata_ip_ok(tok, host)):
        return True
    if ":" not in tok:
        return False
    token_host, token_port = tok.rsplit(":", 1)
    return token_port.isdigit() and _rt_tok_matches(token_host, host, None)


_RT_AUTOMATED_TOOL_NAMES = {
    # Scanners, sprayers, and fan-out fuzzers. A program or client that bans
    # automated testing disables exactly this set; the manual primitives
    # (http_request, rt_browser) remain so enumeration is still possible.
    "sandbox_nmap", "sandbox_sqlmap", "recon_auth_spray",
    "recon_port_scan", "recon_content_discovery", "fuzz", "batch_probe",
    "recon_map",
}


_MCP_URL_RE = re.compile(r"^(?i:https?|wss?)://")


def _rt_mcp_args_urls(args: Any) -> list[str]:
    """Every URL-looking string anywhere in an MCP tool call's arguments.
    MCP connectors are third-party, so the bridge must not guess where a
    connector hid a target: keys, nested dicts, and lists are all walked."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            s = node.strip()
            if _MCP_URL_RE.match(s):
                found.append(s)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(args)
    return found


def _rt_scope_mcp_calls(name: str, args: dict) -> str | None:
    """Mission gate for dynamic MCP tool calls. With an active mission, URL-bearing
    arguments get the same checks as rt_browser (allowlist, self-service block,
    out-of-scope tokens, phase locks); without one, MCP calls run under approval
    alone. Returns a refusal reason or None."""
    if not name.startswith("mcp_"):
        return None
    if not _rt_authorized_mission(_rt_context_chat()):
        return None
    for value in _rt_mcp_args_urls(args):
        reason = _rt_scope_block("rt_browser", {"action": "navigate", "url": value})
        if reason:
            return f"blocked for MCP {name}: {value} — {reason}"
    return None


def _rt_scope_block(name: str, args: dict) -> str | None:
    """Server-side authorization, phase, and scope guard for red-team tools."""
    settings = get_settings()
    if not settings.get("red_team_enabled"):
        return "refused: red-team tools are disabled in Settings."
    # Worker calls inherit contextvars. Prefer the mutable per-turn mission so a
    # validation in one round unlocks the next round before chats.json is saved.
    chat = _rt_context_chat()
    mission = _rt_authorized_mission(chat)
    if not mission:
        return ("refused: this chat has no active user-authorized red-team mission. "
                "Open the Red team gate, confirm authorization, and submit a target first.")
    if _rt_passive_mission(chat):
        if name not in _RT_PASSIVE_TOOL_NAMES:
            return ("refused: passive OSINT allows public-source queries only and will not "
                    "connect to the target. Start a Target Recon or Red Team mission to use "
                    f"{name}.")
        if name == "recon_dns" and (
                str((args or {}).get("mode") or "").lower() == "axfr"
                or bool((args or {}).get("loud"))):
            return ("refused: AXFR directly contacts the target's nameserver and is not "
                    "allowed in passive OSINT.")
    if name in _RT_EXPLOIT_TOOL_NAMES and not _rt_incl_exploit_for(chat, settings):
        return ("refused: this action is outside Target Recon mode. Start an authorized Red Team "
                "assessment for active testing. Finding validation affects reporting, not tool access.")
    if mission.get("engagement") == "bug_bounty" and name in _RT_BOUNTY_HIDDEN_TOOLS:
        return ("refused: bug bounty runs do not extract data, guess passwords or scan the "
                "hosting network. Prove the bug with the smallest request the program allows "
                "(http_request or rt_browser) and record it.")
    if mission.get("automated_allowed") is False and name in _RT_AUTOMATED_TOOL_NAMES:
        return ("refused: the engagement questionnaire disables automated testing tools "
                "(scanners, sprayers, fuzzers). Test manually with http_request, rt_browser, "
                "or the individual recon checks instead of retrying this tool.")
    if mission.get("allow_destructive") is False:
        _destructive = _rt_destructive_block(name, args or {})
        if _destructive:
            return _destructive

    endpoints = _rt_endpoints_from_args(args)
    if not endpoints:
        return None
    denied = _rt_scope_out_tokens(chat)
    allowed = _rt_scope_in_tokens(chat)
    for host, port, raw in endpoints:
        if port in _rt_self_ports() and _rt_resolves_to_local(host):
            svc = "bridge" if int(port) == int(PORT) else "llama.cpp server"
            return (f"refused: {host}:{port} is accuretta's OWN {svc}. Red-team tools "
                    f"never touch the app's own infrastructure. Aim at the target "
                    f"(e.g. the challenge on its own port), not localhost:{port}.")
        for tok in denied:
            if _rt_tok_matches(tok, host, port):
                where = f"{host}:{port}" if port else host
                return (f"refused: {where} is OUT OF SCOPE (you marked '{tok}' out of "
                        f"scope). Stay on the in-scope target.")
        if not any(_rt_allow_matches(name, tok, host, port, args) for tok in allowed):
            where = f"{host}:{port}" if port else host
            return (f"refused: {where} is not in the user-authorized scope "
                    f"({', '.join(allowed) or 'no targets'}). Return to the confirmed target.")
        if "://" not in raw:
            _network = _rt_network_block(raw, allowed, denied)
            if _network:
                return _network
    return None


def _rt_connect_block(url_or_host: str, port: int | None = None) -> str | None:
    """Scope check at the moment a connection is opened.

    The argument guard above runs before a tool starts; this runs where the
    tool actually dials, so a destination the tool derived itself (a crawled
    link, a list entry, a second URL field) is held to the same allowlist.
    No-op outside an active engagement."""
    if not _rt_authorized_mission(_rt_context_chat()):
        return None
    args: dict = {"url": url_or_host} if "://" in str(url_or_host) else {"host": url_or_host}
    if port is not None:
        args["port"] = port
    return _rt_scope_block("redirect", args)


def _rt_incl_exploit_for(chat: dict | None, settings: dict | None = None) -> bool:
    """Whether tools_for_llama should expose the exploit subset for `chat`.

    The selected engagement mode controls availability; evidence status only
    affects reporting, so a finding never has to validate itself to unlock the
    tool needed to reproduce it."""
    s = settings or get_settings()
    if not (s.get("red_team_enabled") and s.get("rt_mission_state", True)):
        return True
    if s.get("rt_force_exploit"):
        return True
    m = chat.get("mission") if isinstance(chat, dict) else None
    return bool(isinstance(m, dict)
                and (m.get("engagement") in ("pentest", "access_control", "bug_bounty")
                     or m.get("phase") == "exploit"))


# Bug bounty programs pay for a proven web bug and forbid pulling data out,
# guessing passwords and touching the hosting network. These tools exist to
# do exactly that, so a bug bounty run does not carry them.
_RT_BOUNTY_HIDDEN_TOOLS = {
    "sql_injection", "sandbox_sqlmap", "recon_auth_spray", "tcp_send",
    "recon_port_scan", "sandbox_nmap", "recon_open_services",
}


# Red-team tools are gated behind `red_team_enabled` so a normal coding turn
# doesn't pay their schema tokens. This is the heavier active-testing set: held
# out of Target Recon and exposed from the start of an explicitly authorized
# Red Team assessment (see _rt_incl_exploit_for / _active_tools). Every name
# here must stay a real breach tool; the recon set below is always on when
# red_team_enabled.
_RT_EXPLOIT_TOOL_NAMES = {
    # Active exploitation primitive — lets the flow actually breach, not just
    # flag suspicion.
    "http_request",
    "rt_browser",
    # Two-account replay (IDOR / broken access control). Shown only when the
    # engagement was started with test accounts (see _excluded_tools).
    "rt_access_check",
    # JWT decode/forge/crack — modern auth attacks (alg:none, weak-secret).
    "jwt_tool",
    # sqlmap-lite and param fuzzer/enumerator.
    "sql_injection",
    "fuzz",
    # one payload set -> every parameterized endpoint at once.
    "batch_probe",
    # credential spray / default-cred test against a login.
    "recon_auth_spray",
    # raw TCP for non-HTTP services and report-to-admin/CTF bots.
    "tcp_send",
}


# Tools permitted during a zero-direct-contact OSINT mission. Every network
# request made by these functions goes to a public third-party data source.
# DNS uses Google's DoH resolver; AXFR is separately refused by the guard.
_RT_PASSIVE_TOOL_NAMES = {
    "recon_dns", "recon_subdomains", "recon_rdap", "recon_web_archive", "recon_shodan",
    "recon_cve_match", "encode_decode",
}
