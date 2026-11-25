# SPDX-License-Identifier: AGPL-3.0-only
"""Reading WAF and CDN block signatures from a response already fetched: headers,
cookies and body.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _hget(headers: dict, name: str) -> str:
    """Case-insensitive header lookup. '' if absent."""
    name = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == name:
            return v
    return ""


def _recon_waf_classify(url, final_url, status, headers, body) -> dict:
    """Classify WAF/CDN block signatures from headers, cookies, and body.
    Read-only interpretation of a response already fetched. {severity, evidence}."""
    bl = (body or "").lower()
    cookies = _hget(headers, "set-cookie").lower()
    server = _hget(headers, "server").lower()
    def hh(n):
        return bool(_hget(headers, n))
    checks = [
        ("Cloudflare", hh("cf-ray") or "cloudflare" in server or "cloudflare" in bl or "__cf" in cookies),
        ("Akamai", "akamaighost" in server or hh("x-akamai-transformed")),
        ("Sucuri", hh("x-sucuri-id") or "sucuri" in bl),
        ("Imperva Incapsula", hh("x-iinfo") or "incap_ses" in cookies or "incapsula incident id" in bl),
        ("F5 BIG-IP", "bigipserver" in cookies or "the requested url was rejected" in bl),
        ("AWS WAF", "aws-waf-token" in cookies or "awswaf" in cookies),
        ("ModSecurity", "mod_security" in server or "mod_security" in bl or (status == 406 and "not acceptable" in bl)),
        ("Wordfence", "wordfence" in bl),
        ("Fastly", "fastly" in server or "fastly" in _hget(headers, "x-served-by").lower()),
    ]
    vendors = [name for name, ok in checks if ok]
    blocked = status in (403, 406, 429, 503) or _recon_classify_body(status, headers, body) == "waf_challenge"
    if vendors:
        ev = f"WAF/CDN detected: {', '.join(vendors)}" + (f" (blocking response {status})" if blocked else "")
    elif blocked:
        vendors = ["unknown"]
        ev = f"generic block/challenge response ({status}): a WAF may be present but unfingerprinted"
    else:
        ev = "no WAF/CDN fingerprint on this response"
    return {"url": url, "final_url": final_url, "status": status, "mode": "waf",
            "waf_detected": bool(vendors), "vendors": vendors, "blocked": blocked,
            "severity": "info", "evidence": ev}


_RECON_WAF_MARKERS = [
    "attention required! | cloudflare", "/cdn-cgi/", "cf-error-details",
    "you have been blocked", "request blocked", "access denied",
    "ddos protection by cloudflare", "incapsula incident id", "akamai reference",
    "web application firewall", "forbidden - blocked",
]


def _recon_classify_body(status, headers, body):
    """Label a response so callers never mistake a block/SPA page for a finding:
    'waf_challenge' | 'html_page' | 'json' | 'other'."""
    ct = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ct = (v or "").lower()
            break
    bl = (body or "").lower()
    if any(m in bl for m in _RECON_WAF_MARKERS):
        return "waf_challenge"
    head = bl[:300]
    if "<!doctype html" in head or "<html" in head:
        return "html_page"
    stripped = (body or "").lstrip()
    if ct.startswith("application/json") or stripped[:1] in ("{", "["):
        return "json"
    return "other"
