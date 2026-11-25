# SPDX-License-Identifier: AGPL-3.0-only
"""The DNS recon tool: records, email authentication (SPF, DMARC, DKIM) and a zone-
transfer check.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import re
import socket
import struct
from accuretta.redteam.doh import _recon_clean_host
from accuretta.redteam.doh import _recon_http_doh


def _recon_dns_email_auth(domain: str) -> dict:
    """Parse SPF + DMARC (+ a couple of common DKIM selectors) into a spoofing
    posture verdict. All read-only DoH lookups. {severity, evidence, ...}."""
    txt = []
    try:
        txt = _recon_http_doh(domain, "TXT")
    except Exception:
        pass
    spf = next((t for t in txt if t.lower().startswith("v=spf1")), "")
    spf_all = ""
    for tok in spf.split():
        if tok.lower().endswith("all") and tok[:1] in "-~?+":
            spf_all = tok
    dmarc = ""
    try:
        dmarc = next((t for t in _recon_http_doh("_dmarc." + domain, "TXT")
                      if t.lower().startswith("v=dmarc1")), "")
    except Exception:
        pass
    m = re.search(r"\bp\s*=\s*(none|quarantine|reject)", dmarc, re.I)
    dmarc_p = (m.group(1).lower() if m else "")
    dkim = []
    for sel in ("default", "google", "selector1", "selector2", "k1", "mail"):
        try:
            rec = _recon_http_doh(f"{sel}._domainkey.{domain}", "TXT")
            if any("v=dkim1" in r.lower() or "k=rsa" in r.lower() or "p=" in r.lower() for r in rec):
                dkim.append(sel)
        except Exception:
            continue
    # Posture: no SPF/DMARC = trivially spoofable; enforcing both = strong.
    hard_spf = spf_all in ("-all", "~all")
    if not spf and not dmarc:
        sev, verdict = "high", "no SPF and no DMARC: domain is trivially spoofable"
    elif dmarc_p == "reject" and hard_spf:
        sev, verdict = "info", "DMARC p=reject with SPF enforcement: strong anti-spoofing"
    elif dmarc_p in ("reject", "quarantine"):
        sev, verdict = "low", f"DMARC p={dmarc_p} present; check SPF strictness ({spf_all or 'no all'})"
    else:
        sev, verdict = "medium", "SPF and/or DMARC present but not enforcing (p=none / soft ~all / no all)"
    return {"domain": domain, "mode": "email_auth", "severity": sev, "evidence": verdict,
            "spf": spf or "(none)", "spf_all": spf_all or "(none)",
            "dmarc": dmarc or "(none)", "dmarc_policy": dmarc_p or "(none)",
            "dkim_selectors_found": dkim, "source": "DNS-over-HTTPS (dns.google)"}


def _dns_build_query(qname: str, qtype: int) -> bytes:
    """Minimal DNS query message (no EDNS, RD off). For the TCP AXFR probe."""
    tid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", tid, 0x0000, 1, 0, 0, 0)
    labels = b"".join(bytes([len(l)]) + l.encode("ascii", "ignore")
                      for l in qname.rstrip(".").split(".") if l) + b"\x00"
    return header + labels + struct.pack(">HH", qtype, 1)


def _recon_dns_axfr(domain: str, timeout: float) -> dict:
    """Attempt a zone transfer (AXFR) against each authoritative NS over TCP/53.
    LOUD and active — a direct connection to the target's nameservers — so it is
    gated behind loud=true by the caller. {severity, evidence, ...}."""
    try:
        nslist = [n.rstrip(".") for n in _recon_http_doh(domain, "NS")][:6]
    except Exception:
        nslist = []
    if not nslist:
        return {"domain": domain, "mode": "axfr", "severity": "info",
                "evidence": "no NS records found to try", "nameservers": []}
    results, allowed = [], []
    for ns in nslist:
        ip = ""
        try:
            a = _recon_http_doh(ns, "A")
            ip = a[0] if a else ""
        except Exception:
            pass
        if not ip:
            results.append({"ns": ns, "result": "unresolved"}); continue
        msg = _dns_build_query(domain, 252)  # 252 = AXFR
        buf, rcode, answers = b"", None, 0
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((ip, 53))
            s.sendall(struct.pack(">H", len(msg)) + msg)
            while len(buf) < 262144:
                try:
                    chunk = s.recv(8192)
                except socket.timeout:
                    break
                if not chunk:
                    break
                buf += chunk
        except Exception as e:
            results.append({"ns": ns, "ip": ip, "result": f"connect/send failed: {e}"}); continue
        finally:
            try: s.close()
            except Exception: pass
        i = 0
        while i + 2 <= len(buf):
            mlen = struct.unpack(">H", buf[i:i+2])[0]; i += 2
            m = buf[i:i+mlen]; i += mlen
            if len(m) < 12:
                break
            h = struct.unpack(">HHHHHH", m[:12])
            if rcode is None:
                rcode = h[1] & 0x0F
            answers += h[3]
        if rcode == 0 and answers > 1:
            results.append({"ns": ns, "ip": ip, "result": "TRANSFER ALLOWED", "records": answers})
            allowed.append(ns)
        elif rcode == 5:
            results.append({"ns": ns, "ip": ip, "result": "refused"})
        else:
            results.append({"ns": ns, "ip": ip, "result": f"no transfer (rcode={rcode}, answers={answers})"})
    if allowed:
        return {"domain": domain, "mode": "axfr", "severity": "high",
                "evidence": f"zone transfer allowed by {', '.join(allowed)}: full DNS zone disclosed",
                "nameservers": nslist, "results": results}
    return {"domain": domain, "mode": "axfr", "severity": "info",
            "evidence": "no nameserver allowed a zone transfer",
            "nameservers": nslist, "results": results}


def tool_recon_dns(args: dict) -> dict:
    """Enumerate DNS records (A, AAAA, MX, NS, TXT, SOA, CNAME) via DoH, or run a
    focused mode: email_auth (SPF/DMARC/DKIM posture), caa, axfr (zone transfer;
    loud). Read-only except axfr. See tool_recon_dns schema for modes."""
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    mode = (args.get("mode") or "").lower().strip()
    if mode == "email_auth":
        return _recon_dns_email_auth(domain)
    if mode == "caa":
        try:
            caa = _recon_http_doh(domain, "CAA")
        except Exception as e:
            return {"error": f"CAA lookup failed: {e}", "domain": domain}
        return {"domain": domain, "mode": "caa", "count": len(caa),
                "caa": caa or [], "severity": "info",
                "evidence": ("issuance restricted to: " + ", ".join(caa)) if caa
                            else "no CAA record: any CA may issue certs for this domain",
                "source": "DNS-over-HTTPS (dns.google)"}
    if mode == "axfr":
        if not args.get("loud"):
            return {"domain": domain, "mode": "axfr", "severity": "info",
                    "note": "AXFR is a LOUD active probe (direct TCP to the target's nameservers). "
                            "Re-run with loud=true to proceed."}
        timeout = max(3.0, min(float(args.get("timeout") or 6.0), 15.0))
        return _recon_dns_axfr(domain, timeout)
    records = {}
    for rtype in ("A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME"):
        try:
            ans = _recon_http_doh(domain, rtype)
            if ans:
                records[rtype] = ans[:50]
        except Exception:
            continue
    if not records:
        return {"error": f"no DNS records resolved for {domain}", "domain": domain}
    return {"domain": domain, "records": records, "source": "DNS-over-HTTPS (dns.google)"}
