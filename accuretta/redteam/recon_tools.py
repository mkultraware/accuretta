# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team recon tools: HTTP security-header audit, TLS audit, RDAP lookup, web archive
history, CVE matching and capturing evidence of a page.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.paths import DATA
from accuretta.core.public_tls import _public_source_tls
from accuretta.redteam.doh import _RECON_UA
from accuretta.redteam.doh import _recon_clean_host
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.http_audit import _recon_cert_from_der
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.recon_fetch import _ScopeCheckedRedirect
from accuretta.redteam.recon_fetch import _rt_proxy_handler
from accuretta.redteam.request_headers import _rt_apply_mission_user_agent
from accuretta.redteam.request_headers import _rt_ua
from accuretta.redteam.waf import _recon_classify_body
from accuretta.redteam.whois import _recon_whois


# --- Tool 4: audit_http_headers ---
def tool_audit_http_headers(args: dict) -> dict:
    """Fetch a URL and grade its HTTP security headers."""
    url = args.get("url")
    if not url:
        return {"error": "url required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
        
    follow_redirects = args.get("follow_redirects", True)
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers=_rt_apply_mission_user_agent({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }))
    
    try:
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                if follow_redirects:
                    return super().http_error_302(req, fp, code, msg, headers)
                return urllib.response.addinfourl(fp, headers, req.get_full_url(), code)
            http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

        opener = urllib.request.build_opener(NoRedirectHandler(), urllib.request.HTTPSHandler(context=ctx))
        resp = opener.open(req, timeout=15)
        
        headers = dict(resp.headers)
        status_code = resp.getcode()
        final_url = resp.geturl()
        
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
        status_code = e.code
        final_url = e.url
    except Exception as e:
        return {"error": f"Failed to fetch {url}: {e}"}

    graded_headers = {}
    score = 100
    findings = []
    
    h_hsts = headers.get("Strict-Transport-Security", "").lower()
    if h_hsts:
        m = re.search(r"max-age=(\d+)", h_hsts)
        if m and int(m.group(1)) >= 31536000:
            graded_headers["Strict-Transport-Security"] = {"value": h_hsts, "status": "pass", "note": "HSTS enabled with sufficient max-age"}
        else:
            graded_headers["Strict-Transport-Security"] = {"value": h_hsts, "status": "warn", "note": "HSTS max-age is too short"}
            score -= 5
    else:
        graded_headers["Strict-Transport-Security"] = {"value": None, "status": "fail", "note": "Missing HSTS"}
        score -= 15
        findings.append("No Strict-Transport-Security header (vulnerable to downgrade attacks)")

    h_csp = headers.get("Content-Security-Policy", "").lower()
    if h_csp:
        if "unsafe-inline" in h_csp or "unsafe-eval" in h_csp:
            graded_headers["Content-Security-Policy"] = {"value": h_csp, "status": "warn", "note": "CSP present but contains unsafe directives"}
            score -= 10
            findings.append("CSP contains unsafe-inline or unsafe-eval")
        else:
            graded_headers["Content-Security-Policy"] = {"value": h_csp, "status": "pass", "note": "Strong CSP"}
    else:
        graded_headers["Content-Security-Policy"] = {"value": None, "status": "fail", "note": "Missing CSP"}
        score -= 15
        findings.append("No Content-Security-Policy header (vulnerable to XSS)")

    h_xfo = headers.get("X-Frame-Options", "").upper()
    if h_xfo in {"DENY", "SAMEORIGIN"}:
        graded_headers["X-Frame-Options"] = {"value": h_xfo, "status": "pass", "note": "Clickjacking protection active"}
    elif not h_xfo:
        graded_headers["X-Frame-Options"] = {"value": None, "status": "fail", "note": "Missing X-Frame-Options"}
        score -= 10
        findings.append("No X-Frame-Options header (vulnerable to clickjacking)")
    else:
        graded_headers["X-Frame-Options"] = {"value": h_xfo, "status": "warn", "note": f"Unrecognized value: {h_xfo}"}
        score -= 5

    h_cto = headers.get("X-Content-Type-Options", "").lower()
    if h_cto == "nosniff":
        graded_headers["X-Content-Type-Options"] = {"value": h_cto, "status": "pass", "note": "MIME-sniffing protection active"}
    else:
        graded_headers["X-Content-Type-Options"] = {"value": h_cto, "status": "fail", "note": "Missing or invalid nosniff"}
        score -= 5
        findings.append("Missing X-Content-Type-Options: nosniff")

    h_ref = headers.get("Referrer-Policy", "").lower()
    if h_ref in {"no-referrer", "strict-origin-when-cross-origin"}:
        graded_headers["Referrer-Policy"] = {"value": h_ref, "status": "pass", "note": "Strong referrer policy"}
    elif h_ref:
        graded_headers["Referrer-Policy"] = {"value": h_ref, "status": "warn", "note": "Weak referrer policy"}
        score -= 5
    else:
        graded_headers["Referrer-Policy"] = {"value": None, "status": "fail", "note": "Missing Referrer-Policy"}
        score -= 5

    h_server = headers.get("Server", "")
    if h_server:
        if re.search(r"[0-9]", h_server):
            graded_headers["Server"] = {"value": h_server, "status": "fail", "note": "Server version info leaked"}
            score -= 5
            findings.append("Server header leaks version information")
        else:
            graded_headers["Server"] = {"value": h_server, "status": "warn", "note": "Generic server header"}

    h_powered = headers.get("X-Powered-By", "")
    if h_powered:
        graded_headers["X-Powered-By"] = {"value": h_powered, "status": "fail", "note": "Technology info leaked"}
        score -= 5
        findings.append("X-Powered-By header leaks technology stack")

    if score >= 90: grade = "A"
    elif score >= 80: grade = "B"
    elif score >= 70: grade = "C"
    elif score >= 60: grade = "D"
    else: grade = "F"

    tls_info = {}
    if final_url.startswith("https://"):
        parsed = urllib.parse.urlparse(final_url)
        try:
            with socket.create_connection((parsed.netloc.split(":")[0], parsed.port or 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=parsed.netloc.split(":")[0]) as ssock:
                    tls_info = {
                        "version": ssock.version(),
                        "cipher": ssock.cipher()
                    }
        except Exception:
            pass

    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "grade": grade,
        "score": score,
        "headers": graded_headers,
        "findings": findings,
        "tls": tls_info
    }


def tool_recon_tls_audit(args: dict) -> dict:
    """Inspect a host's TLS: negotiated protocol/cipher, certificate subject,
    issuer, SANs, expiry, and validation status. Flags weak protocols and
    expired/self-signed certs. Read-only."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    port = int(args.get("port") or 443)
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 15.0))
    findings = []
    _gov = _rt_gov_admit(host, "TLS", url=f"tls://{host}:{port}")
    if _gov:
        return {"error": _gov, "rate_limited": True}

    # First pass: verify against system CAs to learn validation status.
    validated = False
    verify_error = ""
    cert_dict = {}
    try:
        vctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with vctx.wrap_socket(raw, server_hostname=host) as ss:
                cert_dict = ss.getpeercert() or {}
        validated = True
    except ssl.SSLCertVerificationError as e:
        verify_error = str(getattr(e, "verify_message", "") or e)
    except Exception as e:
        return {"error": f"TLS connect failed to {host}:{port}: {e}"}

    # Second pass: grab protocol/cipher and the raw cert regardless of trust.
    proto = cipher = None
    der = b""
    try:
        nctx = ssl.create_default_context()
        nctx.check_hostname = False
        nctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with nctx.wrap_socket(raw, server_hostname=host) as ss:
                proto = ss.version()
                cipher = ss.cipher()  # (name, tls_version, bits)
                der = ss.getpeercert(binary_form=True) or b""
    except Exception:
        pass

    parsed = _recon_cert_from_der(der) if der else None
    # Prefer the validated dict's fields when present.
    subject = issuer = not_after = ""
    sans = []
    if cert_dict:
        subject = dict(x[0] for x in cert_dict.get("subject", []) if x).get("commonName", "")
        issuer = dict(x[0] for x in cert_dict.get("issuer", []) if x).get("commonName", "")
        not_after = cert_dict.get("notAfter", "")
        sans = [v for (k, v) in cert_dict.get("subjectAltName", []) if k == "DNS"][:50]
    elif parsed:
        subject, issuer, not_after = parsed["subject"], parsed["issuer"], parsed["not_after"]
        sans = parsed.get("san", [])
        if parsed.get("self_signed"):
            findings.append("certificate appears self-signed")

    if proto and proto in ("TLSv1", "TLSv1.1", "SSLv3", "SSLv2"):
        findings.append(f"weak protocol negotiated: {proto}")
    if not validated and verify_error:
        findings.append(f"did not validate against system CAs: {verify_error}")
    # Expiry check
    days_left = None
    try:
        import datetime as _dt
        if cert_dict.get("notAfter"):
            exp = _dt.datetime.strptime(cert_dict["notAfter"], "%b %d %H:%M:%S %Y %Z")
            days_left = (exp - _dt.datetime.utcnow()).days
        elif parsed and parsed.get("not_after"):
            exp = _dt.datetime.fromisoformat(parsed["not_after"].replace("Z", "+00:00"))
            days_left = (exp - _dt.datetime.now(_dt.timezone.utc)).days
    except Exception:
        pass
    if days_left is not None:
        if days_left < 0:
            findings.append(f"certificate EXPIRED {abs(days_left)} days ago")
        elif days_left < 21:
            findings.append(f"certificate expires soon ({days_left} days)")

    return {
        "host": host, "port": port, "validated": validated,
        "protocol": proto, "cipher": cipher[0] if cipher else None,
        "cipher_bits": cipher[2] if cipher else None,
        "subject": subject, "issuer": issuer, "not_after": not_after,
        "days_until_expiry": days_left, "san": sans,
        "findings": findings or ["no obvious TLS issues"],
    }


def tool_recon_rdap(args: dict) -> dict:
    """Read public registration data through the RDAP bootstrap service.

    The request goes to RDAP infrastructure, never to the subject domain.
    """
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    url = "https://rdap.org/domain/" + urllib.parse.quote(domain, safe="")
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/rdap+json, application/json",
            "User-Agent": _RECON_UA,
        })
        with urllib.request.urlopen(req, timeout=15, context=_public_source_tls()) as r:
            data = json.loads(r.read(2_000_000).decode("utf-8", "replace"))
    except Exception as e:
        # .se, .nu, .dk, .de and others publish no RDAP service, so rdap.org
        # answers 404 for every domain under them. Classic WHOIS still works.
        rdap_error = str(getattr(e, "reason", None) or e)
        try:
            return _recon_whois(domain, rdap_error)
        except Exception as whois_error:
            return {"error": f"RDAP query failed ({rdap_error}) and WHOIS failed ({whois_error})",
                    "domain": domain}

    events = {}
    for event in data.get("events") or []:
        action = str(event.get("eventAction") or "").strip()
        date = str(event.get("eventDate") or "").strip()
        if action and date:
            events[action] = date
    entities = []
    for entity in (data.get("entities") or [])[:30]:
        names = []
        card = entity.get("vcardArray") or []
        rows = card[1] if isinstance(card, list) and len(card) > 1 else []
        for item in rows if isinstance(rows, list) else []:
            if isinstance(item, list) and len(item) >= 4 and item[0] in ("fn", "org"):
                value = item[3]
                if isinstance(value, list):
                    value = " ".join(str(v) for v in value if v)
                value = str(value or "").strip()
                if value and value not in names:
                    names.append(value[:160])
        entities.append({
            "handle": str(entity.get("handle") or "")[:120],
            "roles": [str(x) for x in (entity.get("roles") or [])[:8]],
            "name": names[0] if names else "",
        })
    nameservers = sorted({
        str(row.get("ldhName") or "").lower()
        for row in (data.get("nameservers") or [])
        if row.get("ldhName")
    })
    return {
        "domain": domain,
        "handle": data.get("handle") or "",
        "status": data.get("status") or [],
        "events": events,
        "nameservers": nameservers[:30],
        "entities": entities,
        "dnssec_signed": bool((data.get("secureDNS") or {}).get("delegationSigned")),
        "source": "RDAP bootstrap and registry data (passive)",
        "contacted": "rdap.org / authoritative RDAP registry, not the target",
    }


def tool_recon_web_archive(args: dict) -> dict:
    """Query the Internet Archive CDX index without visiting the target."""
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    limit = max(10, min(int(args.get("limit") or 200), 500))
    query = urllib.parse.urlencode({
        "url": f"{domain}/*",
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "limit": str(limit),
    })
    url = "https://web.archive.org/cdx/search/cdx?" + query
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _RECON_UA})
        with urllib.request.urlopen(req, timeout=20, context=_public_source_tls()) as r:
            rows = json.loads(r.read(4_000_000).decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"Internet Archive query failed: {e}", "domain": domain}
    if not isinstance(rows, list) or len(rows) < 2:
        return {
            "domain": domain, "count": 0, "captures": [],
            "source": "Internet Archive CDX (passive)",
        }
    headers = [str(x) for x in rows[0]]
    captures = []
    years = set()
    for raw in rows[1:]:
        if not isinstance(raw, list):
            continue
        item = dict(zip(headers, raw))
        ts = str(item.get("timestamp") or "")
        if len(ts) >= 4:
            years.add(ts[:4])
        captures.append({
            "timestamp": ts,
            "url": str(item.get("original") or "")[:500],
            "type": str(item.get("mimetype") or "")[:80],
        })
    return {
        "domain": domain,
        "count": len(captures),
        "years": sorted(years),
        "captures": captures[:limit],
        "source": "Internet Archive CDX (passive)",
        "contacted": "web.archive.org, not the target",
    }


def tool_recon_cve_match(args: dict) -> dict:
    """AUTHORIZED RECON. Map a component + version to known CVEs via OSV.dev.
    Best coverage for OSS packages (npm, PyPI, Go, Maven, etc.) — feed it the
    components you find in exposed package.json / requirements / JS libs."""
    name = (args.get("package") or args.get("product") or "").strip()
    if not name:
        return {"error": "package/product required"}
    version = (args.get("version") or "").strip()
    ecosystem = (args.get("ecosystem") or "").strip()
    query = {}
    if version:
        query["version"] = version
    pkg = {"name": name}
    if ecosystem:
        pkg["ecosystem"] = ecosystem
    query["package"] = pkg
    try:
        data = json.dumps(query).encode("utf-8")
        req = urllib.request.Request("https://api.osv.dev/v1/query", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15, context=_public_source_tls()) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"OSV query failed: {e}"}
    vulns = []
    for v in (res.get("vulns") or [])[:40]:
        sev = ""
        for s in (v.get("severity") or []):
            sev = s.get("score", "") or sev
        vulns.append({"id": v.get("id"), "summary": (v.get("summary") or "")[:160],
                      "severity": sev, "aliases": (v.get("aliases") or [])[:5]})
    return {"package": name, "version": version, "ecosystem": ecosystem,
            "vuln_count": len(vulns), "vulns": vulns,
            "note": "OSV.dev — strongest for OSS packages; pass ecosystem for best precision."}


def tool_recon_capture_evidence(args: dict) -> dict:
    """AUTHORIZED RECON. Fetch a URL and store the response as a proof artifact
    under data/recon_evidence/ with a sha256 + snippet, so the report cites a
    real file instead of a claim."""
    import hashlib
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url required"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + _recon_clean_host(url)
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", (args.get("label") or "evidence"))[:60]
    timeout = max(2.0, min(float(args.get("timeout") or 10.0), 20.0))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _capture_handlers = [_ScopeCheckedRedirect(), urllib.request.HTTPSHandler(context=ctx)]
    _ph = _rt_proxy_handler()
    if _ph:
        _capture_handlers.append(_ph)
    opener = urllib.request.build_opener(*_capture_handlers)
    _gov = _rt_gov_admit(_rt_url_host(url), "GET", url=url)
    if _gov:
        return {"error": _gov, "rate_limited": True, "url": url}
    req = urllib.request.Request(url, headers={"User-Agent": _rt_ua()})
    ctype = ""
    try:
        resp = opener.open(req, timeout=timeout)
        raw, status = resp.read(5_000_000), resp.status
        ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(5_000_000)
        except Exception:
            raw = b""
        status = e.code
        try:
            ctype = e.headers.get("Content-Type", "")
        except Exception:
            ctype = ""
    except Exception as e:
        return {"error": f"capture failed: {e}", "url": url}

    evidence_dir = DATA / "recon_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    host = _recon_clean_host(url)
    path = evidence_dir / f"{ts}_{label}_{host}.dat"
    try:
        path.write_bytes(raw)
    except Exception as e:
        return {"error": f"could not save evidence: {e}"}
    snippet_text = raw[:2000].decode("utf-8", "replace")
    classification = _recon_classify_body(status, {"Content-Type": ctype}, snippet_text)
    warning, likely = "", True
    if classification == "waf_challenge":
        warning = "captured content is a WAF/Cloudflare challenge page — NOT a real artifact. Do NOT report this as a finding."
        likely = False
    elif classification == "html_page":
        warning = "captured content is a generic HTML page (possibly the site's own index/SPA) — confirm it is actually sensitive before treating as evidence."
        likely = False
    return {
        "saved": str(path), "url": url, "status": status, "bytes": len(raw),
        "content_type": ctype, "classification": classification,
        "likely_evidence": likely, "warning": warning,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "snippet": raw[:400].decode("utf-8", "replace"),
        "note": ("evidence stored — cite this artifact path + sha256 in the report." if likely
                 else "stored, but this does NOT look like a real artifact — see `warning`."),
    }
