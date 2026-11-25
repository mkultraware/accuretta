# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team scope basics: local hosts, the target a tool call names, and matching it against scope entries.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request


def _rt_is_local_host(host: str) -> bool:
    """Loopback / private / this machine — where our own services live."""
    host = (host or "").lower().strip().strip("[]")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "", "host.docker.internal"):
        return True
    if host.endswith(".local"):
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip.is_link_local
    except ValueError:
        pass
    try:
        if host == socket.gethostname().lower():
            return True
    except Exception:
        pass
    return False


def _rt_endpoint_from_args(args: dict) -> tuple[str, int | None]:
    """(host, port) a red-team tool is aimed at. port None when unspecified."""
    if not isinstance(args, dict):
        return "", None
    raw = ""
    for k in ("url", "base_url", "target", "host", "domain", "endpoint", "origin"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            raw = v.strip()
            break
    if not raw:
        return "", None
    s = raw if "://" in raw else "//" + raw
    host, port = "", None
    try:
        sp = urllib.parse.urlsplit(s)
        host = (sp.hostname or "").lower()
        port = sp.port
    except Exception:
        host = raw.split("/", 1)[0].split(":", 1)[0].lower()
    if port is None and args.get("port") not in (None, ""):
        try:
            port = int(args.get("port"))
        except Exception:
            pass
    if port is None and "://" in raw:
        port = 443 if raw.lower().startswith("https") else 80
    return host, port


def _rt_tok_matches(tok: str, host: str, port: int | None) -> bool:
    tok = tok.strip()
    if not tok:
        return False
    if tok.isdigit():                      # bare port
        return port is not None and int(tok) == port
    if "/" in tok:                        # IPv4/IPv6 CIDR
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(tok, strict=False)
        except ValueError:
            return False
    if ":" in tok:                         # host:port
        th, tp = tok.rsplit(":", 1)
        if tp.isdigit():
            th = th.lstrip("*.")
            return (host == th or host.endswith("." + th)) and port == int(tp)
    tok = tok.lstrip("*.")
    return host == tok or host.endswith("." + tok)   # host / parent domain


def _rt_scope_token_host(tok: str) -> str:
    """The hostname inside an in-scope token, or "" for IPs, CIDRs and bare ports."""
    token_host = tok.strip().lstrip("*.")
    if ":" in token_host and token_host.rsplit(":", 1)[1].isdigit():
        token_host = token_host.rsplit(":", 1)[0]
    if not token_host or token_host.isdigit() or "/" in token_host or "." not in token_host:
        return ""
    try:
        ipaddress.ip_address(token_host)
        return ""
    except ValueError:
        return token_host
