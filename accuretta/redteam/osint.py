# SPDX-License-Identifier: AGPL-3.0-only
"""Public-source lookups: WHOIS over port 43 and Shodan host summaries.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import socket


def _whois_query(server: str, query: str, timeout: float = 12.0) -> str:
    with socket.create_connection((server, 43), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((query + "\r\n").encode("ascii", "ignore"))
        chunks, size = [], 0
        while size < 200_000:
            chunk = sock.recv(8192)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    raw = b"".join(chunks)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", "replace")


def _shodan_refusal(status: int, reason: str = "") -> str:
    """What a Shodan refusal means, in words, so nobody chases a bad key or a
    bad query when the account simply isn't allowed the call. `reason` is
    Shodan's own message when it sent one."""
    said = f' (Shodan says: "{reason}")' if reason else ""
    if status == 401:
        return (f"Shodan rejected the API key (401){said}: it is wrong or was revoked. "
                "Get a fresh key at account.shodan.io.")
    if status == 403:
        return (f"Shodan refused this request (403){said}. Free Shodan keys can look up a "
                "single IP, but searches with filters (port:, http.title:, ...), bulk DNS and "
                "other paid calls need a paid Shodan membership. Not a key or query mistake: "
                "retrying will not help.")
    if status == 429:
        return (f"Shodan rate-limited this key or it is out of query credits (429){said}. "
                "Wait a moment, or check the credits at account.shodan.io.")
    return f"Shodan answered HTTP {status}{said}."


def _shodan_host_summary(data: dict) -> dict:
    """Trim a /shodan/host response to the fields a recon brief needs."""
    services = []
    for b in (data.get("data") or [])[:40]:
        if not isinstance(b, dict):
            continue
        svc = {"port": b.get("port"), "transport": b.get("transport") or "tcp"}
        for k in ("product", "version", "os"):
            if b.get(k):
                svc[k] = str(b[k])[:80]
        if b.get("cpe23") or b.get("cpe"):
            svc["cpe"] = [str(c)[:120] for c in (b.get("cpe23") or b.get("cpe"))[:5]]
        module = (b.get("_shodan") or {}).get("module")
        if module:
            svc["module"] = module
        http = b.get("http") or {}
        if http.get("title"):
            svc["http_title"] = str(http["title"])[:120]
        if http.get("server"):
            svc["http_server"] = str(http["server"])[:80]
        cert = (b.get("ssl") or {}).get("cert") or {}
        if cert:
            svc["tls_subject_cn"] = str((cert.get("subject") or {}).get("CN") or "")[:120]
            svc["tls_issuer"] = str((cert.get("issuer") or {}).get("O") or
                                    (cert.get("issuer") or {}).get("CN") or "")[:80]
            if cert.get("expires"):
                svc["tls_expires"] = cert["expires"]
        if b.get("timestamp"):
            svc["seen"] = str(b["timestamp"])[:19]
        banner = str(b.get("data") or "").strip()
        if banner:
            svc["banner"] = banner[:200]
        services.append(svc)
    vulns = data.get("vulns") or []
    if isinstance(vulns, dict):
        vulns = list(vulns)
    out = {
        "org": data.get("org"), "isp": data.get("isp"), "asn": data.get("asn"),
        "country": data.get("country_name"), "city": data.get("city"),
        "os": data.get("os"), "hostnames": (data.get("hostnames") or [])[:20],
        "domains": (data.get("domains") or [])[:20], "ports": data.get("ports") or [],
        "tags": data.get("tags") or [], "vulns": sorted(vulns)[:50],
        "last_update": data.get("last_update"), "services": services,
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}
