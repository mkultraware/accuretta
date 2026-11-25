# SPDX-License-Identifier: AGPL-3.0-only
"""Subdomains from certificate transparency logs, and the subdomain-takeover check.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.public_tls import _public_source_tls
from accuretta.redteam.doh import _recon_clean_host
from accuretta.redteam.doh import _recon_http_doh
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.request_headers import _rt_stealth_headers
from accuretta.redteam.request_headers import _rt_ua


def _ct_names_crtsh(domain: str, timeout: float) -> tuple[list[str] | None, str]:
    """Certificate names for *.domain from crt.sh. crt.sh runs on donated
    hardware and answers 502/503 (or an HTML error page) whenever its database
    is slow, so transient failures are retried with backoff inside a budget."""
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(domain)}&output=json"
    deadline = time.monotonic() + max(timeout * 2, 25.0)
    last = ""
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining < 3:
            break
        try:
            req = urllib.request.Request(url, headers=_rt_stealth_headers())
            with urllib.request.urlopen(req, timeout=min(timeout, remaining),
                                        context=_public_source_tls()) as r:
                rows = json.loads(r.read().decode("utf-8", "replace"))
            return [n for row in rows for n in str(row.get("name_value", "")).splitlines()], ""
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.reason}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, socket.timeout, ValueError) as e:
            # ValueError covers the HTML error page crt.sh sends with a 200
            last = str(getattr(e, "reason", None) or e)
        except Exception as e:
            last = str(e)
            break
        if attempt < 2:
            time.sleep(min(1.5 * (attempt + 1) + random.random(), max(0.0, deadline - time.monotonic())))
    return None, last or "no response"


def _ct_names_certspotter(domain: str, timeout: float) -> tuple[list[str] | None, str]:
    """Fallback certificate-transparency source: Cert Spotter's free API (no
    key, about 10 requests per window). Follows a few pages at most."""
    names: list[str] = []
    after = ""
    for _page in range(4):
        query = {"domain": domain, "include_subdomains": "true", "expand": "dns_names"}
        if after:
            query["after"] = after
        url = "https://api.certspotter.com/v1/issuances?" + urllib.parse.urlencode(query)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _rt_ua(), "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout, context=_public_source_tls()) as r:
                rows = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if names:
                break
            return None, f"HTTP {e.code} {e.reason}"
        except Exception as e:
            if names:
                break
            return None, str(getattr(e, "reason", None) or e)
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            names.extend(str(n) for n in (row.get("dns_names") or []))
        if len(rows) < 50:
            break
        after = str(rows[-1].get("id") or "")
        if not after:
            break
    return names, ""


def tool_recon_subdomains(args: dict) -> dict:
    """Passive subdomain enumeration via certificate transparency logs:
    crt.sh first, Cert Spotter when crt.sh is down. No active brute-forcing.
    Read-only, and neither source contacts the target."""
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    timeout = max(4.0, min(float(args.get("timeout") or 15.0), 30.0))
    names, crtsh_error = _ct_names_crtsh(domain, timeout)
    source = "certificate transparency (crt.sh) — passive"
    note = ""
    if names is None:
        names, spotter_error = _ct_names_certspotter(domain, timeout)
        if names is None:
            return {"error": f"certificate transparency lookup failed: crt.sh ({crtsh_error}), "
                             f"Cert Spotter ({spotter_error})", "domain": domain}
        source = "certificate transparency (Cert Spotter) — passive"
        note = f"crt.sh was unavailable ({crtsh_error}), so Cert Spotter answered instead."
    subs = set()
    for name in names:
        name = name.strip().lstrip("*.").lower()
        if name.endswith("." + domain):
            subs.add(name)
    result = {
        "domain": domain, "count": len(subs),
        "subdomains": sorted(subs)[:300],
        "source": source,
    }
    if note:
        result["note"] = note
    return result


_RECON_TAKEOVER_FINGERPRINTS = [
    ("AWS S3", "NoSuchBucket"),
    ("AWS S3", "The specified bucket does not exist"),
    ("GitHub Pages", "There isn't a GitHub Pages site here"),
    ("Heroku", "No such app"),
    ("Heroku", "herokucdn.com/error-pages/no-such-app"),
    ("Fastly", "Fastly error: unknown domain"),
    ("Shopify", "Sorry, this shop is currently unavailable"),
    ("Surge.sh", "project not found"),
    ("Bitbucket", "Repository not found"),
    ("Ghost", "The thing you were looking for is no longer here"),
    ("Pantheon", "The gods are wise"),
    ("Tumblr", "Whatever you were looking for doesn't currently exist"),
    ("Readthedocs", "unknown to Read the Docs"),
    ("Wordpress", "Do you want to register"),
    ("Help Scout", "No settings were found for this company"),
]


def tool_recon_subdomain_takeover(args: dict) -> dict:
    """AUTHORIZED RECON. Check subdomains for dangling CNAME takeover: resolves
    the CNAME, fetches the host, matches known orphaned-service fingerprints.
    Read-only (no claiming performed)."""
    subs = args.get("subdomains")
    if isinstance(subs, str):
        subs = [subs]
    if not subs:
        one = args.get("domain") or args.get("target") or ""
        subs = [one] if one else []
    subs = [_recon_clean_host(s) for s in subs if s]
    if not subs:
        return {"error": "provide `subdomains` (list) or `domain`"}
    subs = subs[:80]
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    vulnerable = []
    checked = []

    for sub in subs:
        cnames = []
        try:
            cnames = _recon_http_doh(sub, "CNAME", timeout=6.0)
        except Exception:
            pass
        cname = cnames[0].rstrip(".") if cnames else ""
        body = ""
        for scheme in ("https://", "http://"):
            r = _recon_fetch(scheme + sub, timeout=timeout, max_bytes=4000)
            if "body" in r:
                body = r["body"]
                break
        hit = None
        for service, fp in _RECON_TAKEOVER_FINGERPRINTS:
            if fp.lower() in body.lower():
                hit = service
                break
        checked.append({"subdomain": sub, "cname": cname, "takeoverable": bool(hit)})
        if hit:
            vulnerable.append({"subdomain": sub, "cname": cname, "service": hit,
                               "evidence": f"orphaned-service fingerprint for {hit}"})
    return {"checked_count": len(checked), "vulnerable_count": len(vulnerable),
            "vulnerable": vulnerable, "checked": checked,
            "note": "fingerprint match = likely claimable. Confirm before reporting."}
