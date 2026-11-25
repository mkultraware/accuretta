# SPDX-License-Identifier: AGPL-3.0-only
"""The HTTP fingerprint tool: server, framework and security-header grade from one
response.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.network_scope import _rt_scope_block
from accuretta.redteam.request_headers import _browser_headers
from accuretta.redteam.waf import _hget
from accuretta.redteam.waf import _recon_waf_classify
from accuretta.redteam.browser_profiles import _pick_profile


_SEC_HEADERS = [
    ("Strict-Transport-Security", "HSTS"), ("Content-Security-Policy", "CSP"),
    ("X-Frame-Options", "X-Frame-Options"), ("X-Content-Type-Options", "X-Content-Type-Options"),
    ("Referrer-Policy", "Referrer-Policy"), ("Permissions-Policy", "Permissions-Policy"),
]


def _recon_grade_security_headers(url, final_url, status, headers) -> dict:
    """Grade the standard security response headers already fetched. A..F by count
    present, with weak-value notes. {severity, evidence}."""
    present, missing, weak = {}, [], []
    for hname, label in _SEC_HEADERS:
        v = _hget(headers, hname)
        if v:
            present[label] = v[:120]
        else:
            missing.append(label)
    csp = _hget(headers, "content-security-policy").lower()
    if csp and ("unsafe-inline" in csp or "unsafe-eval" in csp):
        weak.append("CSP allows unsafe-inline/unsafe-eval")
    xcto = _hget(headers, "x-content-type-options").lower()
    if xcto and "nosniff" not in xcto:
        weak.append("X-Content-Type-Options is not nosniff")
    n = len(present)
    grade = ["F", "E", "D", "C", "B", "A"][min(n, 5)]
    sev = "info" if n >= 5 else ("low" if n >= 3 else "medium")
    ev = f"{n}/6 security headers present" + (f"; missing: {', '.join(missing)}" if missing else "")
    if weak:
        ev += f"; weak: {', '.join(weak)}"
    return {"url": url, "final_url": final_url, "status": status, "mode": "security_headers",
            "grade": grade, "present": present, "missing": missing, "weak": weak,
            "severity": sev, "evidence": ev}


def tool_recon_http_fingerprint(args: dict) -> dict:
    """Fetch a URL and fingerprint the server: status, redirect chain, Server /
    X-Powered-By, page title, notable headers, and cookies. Read-only. mode=waf
    classifies block signatures; mode=security_headers grades CSP/HSTS/etc."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 20.0))

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Rotating real-browser identity (same stack as http_request) so the
    # fingerprint probe blends in instead of announcing a static old UA.
    _fp_headers = _browser_headers(_pick_profile(urllib.parse.urlparse(url).netloc))
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    chain = []
    cur = url
    final_headers = {}
    status = None
    body = ""
    for _ in range(6):
        _gov = _rt_gov_admit(_rt_url_host(cur), "GET", url=cur)
        if _gov:
            return {"error": _gov, "rate_limited": True, "url": cur}
        req = urllib.request.Request(cur, headers=_fp_headers)
        try:
            resp = opener.open(req, timeout=timeout)
            status = resp.status
            final_headers = {k: v for k, v in resp.headers.items()}
            body = resp.read(20000).decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            status = e.code
            loc = e.headers.get("Location")
            final_headers = {k: v for k, v in e.headers.items()}
            if loc and 300 <= e.code < 400 and len(chain) < 5:
                next_url = urllib.parse.urljoin(cur, loc)
                block = _rt_scope_block("redirect", {"url": next_url})
                if block:
                    return {"error": block, "scope_blocked": True,
                            "url": url, "redirect_from": cur, "redirect_to": next_url}
                chain.append({"from": cur, "status": e.code, "to": loc})
                cur = next_url
                continue
            break
        except Exception as e:
            return {"error": f"request failed: {e}", "url": cur}

    mode = (args.get("mode") or "").lower().strip()
    if mode == "waf":
        return _recon_waf_classify(url, cur, status, final_headers, body)
    if mode in ("security_headers", "sec_headers", "headers"):
        return _recon_grade_security_headers(url, cur, status, final_headers)

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    notable = {}
    for h in ("Server", "X-Powered-By", "X-AspNet-Version", "Via", "X-Generator",
              "Content-Type", "Strict-Transport-Security", "Content-Security-Policy"):
        for k, v in final_headers.items():
            if k.lower() == h.lower():
                notable[h] = v
                break
    cookies = [c.split(";")[0] for k, v in final_headers.items()
               if k.lower() == "set-cookie" for c in [v]]
    return {
        "url": url, "final_url": cur, "status": status,
        "title": title, "redirect_chain": chain,
        "server": notable.get("Server", ""),
        "powered_by": notable.get("X-Powered-By", ""),
        "notable_headers": notable, "cookies": cookies[:10],
    }
