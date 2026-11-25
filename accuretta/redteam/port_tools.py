# SPDX-License-Identifier: AGPL-3.0-only
"""Port scans and open-service checks: the top ports, their usual names, and banner
grabs.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import socket
import time
from accuretta.redteam.doh import _recon_clean_host
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.governor import _rt_gov_preflight
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.network_scope import _rt_connect_block
from accuretta.redteam.network_scope import _rt_self_ports
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.scope import _rt_is_local_host
from concurrent.futures import ThreadPoolExecutor


_RECON_TOP_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 161, 389, 443, 445,
    465, 587, 636, 993, 995, 1433, 1521, 1723, 2049, 2375, 2376, 3000,
    3306, 3389, 5432, 5601, 5900, 5985, 5986, 6379, 7001, 8000, 8008,
    8080, 8081, 8443, 8888, 9000, 9092, 9200, 9300, 11211, 27017,
]


_RECON_PORT_NAMES = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http",
    110: "pop3", 111: "rpcbind", 135: "msrpc", 139: "netbios", 143: "imap",
    161: "snmp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps",
    587: "submission", 636: "ldaps", 993: "imaps", 995: "pop3s",
    1433: "mssql", 1521: "oracle", 1723: "pptp", 2049: "nfs", 2375: "docker",
    2376: "docker-tls", 3000: "dev-http", 3306: "mysql", 3389: "rdp",
    5432: "postgres", 5601: "kibana", 5900: "vnc", 5985: "winrm",
    5986: "winrm-tls", 6379: "redis", 7001: "weblogic", 8080: "http-alt",
    8443: "https-alt", 9200: "elasticsearch", 11211: "memcached",
    27017: "mongodb",
}


def _recon_resolve(host: str) -> dict:
    """Resolve a host to its IPs. {"ips":[...]} or {"error":...}."""
    try:
        infos = socket.getaddrinfo(host, None)
        return {"ips": sorted({i[4][0] for i in infos})}
    except Exception as e:
        return {"error": f"could not resolve {host}: {e}"}


def tool_recon_port_scan(args: dict) -> dict:
    """Stealthy TCP-connect port scan: capped concurrency, randomized order,
    jitter between probes. Deliberately quieter than nmap. Read-only."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    res = _recon_resolve(host)
    if "error" in res:
        return res
    target_ip = res["ips"][0]
    ports = args.get("ports")
    if ports:
        try:
            port_list = [int(p) for p in ports if 0 < int(p) < 65536]
        except Exception:
            return {"error": "ports must be a list of integers"}
    else:
        port_list = list(_RECON_TOP_PORTS)
    # Never probe accuretta's own bridge/llama ports on a local scan — that's the
    # "sniffing around my own ports" problem. Only strips them for local targets;
    # a remote host's 8080 is fair game.
    if _rt_is_local_host(host) or _rt_is_local_host(target_ip):
        _self = _rt_self_ports()
        port_list = [p for p in port_list if p not in _self]
    _gov = _rt_gov_preflight(len(port_list))
    if _gov:
        return {"error": _gov, "rate_limited": True, "ports_requested": len(port_list)}
    random.shuffle(port_list)  # randomized order = less obvious sweep pattern
    timeout = max(0.3, min(float(args.get("timeout") or 1.5), 5.0))
    concurrency = max(1, min(int(args.get("concurrency") or 8), 32))
    jitter_ms = max(0, min(int(args.get("jitter_ms") or 40), 1000))

    _scan_mission = _rt_authorized_mission(_rt_context_chat()) or {}

    def probe(port):
        if jitter_ms:
            time.sleep(random.uniform(0, jitter_ms / 1000.0))
        if _rt_gov_admit(host, "TCP", url=f"tcp://{host}:{port}", mission=_scan_mission):
            return None
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((target_ip, port)) == 0:
                banner = ""
                try:
                    s.settimeout(0.8)
                    banner = s.recv(96).decode("latin-1", "replace").strip()[:80]
                except Exception:
                    pass
                return {"port": port, "service": _RECON_PORT_NAMES.get(port, "unknown"), "banner": banner}
        except Exception:
            pass
        finally:
            try: s.close()
            except Exception: pass
        return None

    open_ports = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recon-scan") as ex:
        for r in ex.map(probe, port_list):
            if r:
                open_ports.append(r)
    open_ports.sort(key=lambda x: x["port"])
    return {
        "host": host, "resolved_ip": target_ip, "all_ips": res["ips"],
        "scanned": len(port_list), "open_count": len(open_ports),
        "open_ports": open_ports,
        "note": "TCP connect scan (randomized order + jitter). Authorized targets only.",
    }


def _recon_tcp_cmd(host: str, port: int, payload: bytes, timeout: float = 4.0) -> bytes:
    """Open a TCP socket, send payload, return up to 4KB of response."""
    _refusal = _rt_connect_block(host, port) or _rt_gov_admit(host, "TCP", url=f"tcp://{host}:{port}")
    if _refusal:
        raise ConnectionError(_refusal)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        if payload:
            s.sendall(payload)
        return s.recv(4096)
    finally:
        try: s.close()
        except Exception: pass


def tool_recon_open_services(args: dict) -> dict:
    """AUTHORIZED RECON. Check for UNAUTHENTICATED data services exposed to the
    network: Redis, Elasticsearch, Docker API, Memcached. A live unauth hit is
    often a direct way in. Read-only probes."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    timeout = max(2.0, min(float(args.get("timeout") or 4.0), 10.0))
    findings = []

    # Redis (6379): PING should return +PONG if no auth
    try:
        resp = _recon_tcp_cmd(host, 6379, b"PING\r\n", timeout)
        if b"+PONG" in resp:
            findings.append({"service": "Redis", "port": 6379, "severity": "critical",
                             "state": "OPEN / no auth", "evidence": "+PONG to unauthenticated PING"})
        elif b"NOAUTH" in resp:
            findings.append({"service": "Redis", "port": 6379, "severity": "info",
                             "state": "present, auth required", "evidence": "NOAUTH response"})
    except Exception:
        pass
    # Elasticsearch (9200): root returns cluster JSON
    r = _recon_fetch(f"http://{host}:9200/", timeout)
    if r.get("status") == 200 and '"cluster_name"' in (r.get("body") or ""):
        findings.append({"service": "Elasticsearch", "port": 9200, "severity": "high",
                         "state": "OPEN / no auth", "evidence": "cluster info returned unauthenticated"})
    # Docker API (2375): /version
    r = _recon_fetch(f"http://{host}:2375/version", timeout)
    if r.get("status") == 200 and "ApiVersion" in (r.get("body") or ""):
        findings.append({"service": "Docker API", "port": 2375, "severity": "critical",
                         "state": "OPEN / no auth", "evidence": "/version returned — full host control"})
    # Memcached (11211): stats
    try:
        resp = _recon_tcp_cmd(host, 11211, b"stats\r\n", timeout)
        if b"STAT " in resp:
            findings.append({"service": "Memcached", "port": 11211, "severity": "high",
                             "state": "OPEN / no auth", "evidence": "stats returned unauthenticated"})
    except Exception:
        pass

    return {"host": host, "finding_count": len(findings),
            "findings": findings or [{"service": "none", "severity": "info",
                                       "state": "no exposed unauthenticated services found"}]}
