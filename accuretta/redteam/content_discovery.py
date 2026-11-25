# SPDX-License-Identifier: AGPL-3.0-only
"""Content discovery and finding validation: common, API and backup paths and virtual
hosts, compared against a baseline response so soft 404s don't count.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.settings import get_settings
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.waf import _recon_classify_body
from concurrent.futures import ThreadPoolExecutor


_RECON_COMMON_PATHS = [
    "admin/", "administrator/", "login", "wp-admin/", "wp-login.php",
    "phpmyadmin/", "manager/html", "console/", "dashboard/", "portal/",
    ".env", ".git/HEAD", ".git/config", ".svn/entries", ".DS_Store",
    "config.php", "config.json", "config.yml", "config.yaml", "settings.py",
    "web.config", "wp-config.php.bak", "wp-config.php~", "backup.zip",
    "backup.sql", "backup.tar.gz", "db.sql", "dump.sql", "database.sql",
    ".htaccess", ".htpasswd", "id_rsa", ".aws/credentials", "credentials.json",
    "robots.txt", "sitemap.xml", "server-status", "server-info", "status",
    "actuator", "actuator/health", "actuator/env", "actuator/heapdump",
    "metrics", "phpinfo.php", "info.php", "test.php", "debug", "trace",
    "api/", "api/v1/", "swagger.json", "swagger-ui/", "openapi.json",
    "graphql", ".well-known/security.txt", "graphiql",
    ".gitlab-ci.yml", "Dockerfile", "docker-compose.yml", ".github/workflows/",
]


# Highest-signal subset for quiet mode — a handful of the most likely wins so a
# stealthy sweep sends ~10 requests instead of ~50.
_RECON_QUIET_PATHS = [
    ".env", ".git/config", ".git/HEAD", "admin/", "login", "api/",
    "config.json", "backup.zip", "actuator/env", "robots.txt",
]


def _recon_baseline(base, timeout):
    """Probe a guaranteed-nonexistent path to learn catch-all behavior. If the
    site returns 200 for garbage, bare 200s mean nothing and must be diffed."""
    rnd = "zz" + "".join(random.choice("abcdefghijklmnop0123456789") for _ in range(22)) + "/"
    r = _recon_fetch(urllib.parse.urljoin(base, rnd), timeout=timeout, max_bytes=6000)
    body = r.get("body", "") or ""
    return {"status": r.get("status"), "size": len(body), "body": body,
            "is_catchall": r.get("status") == 200,
            "classification": _recon_classify_body(r.get("status"), r.get("headers"), body)}


def _recon_same_as_baseline(r, baseline):
    """True if a response is effectively the catch-all page (same status, near
    identical size) — i.e. NOT a real distinct resource."""
    if not baseline.get("is_catchall"):
        return False
    body = r.get("body", "") or ""
    return r.get("status") == baseline.get("status") and abs(len(body) - baseline.get("size", 0)) < 64


# API-surface discovery: swagger/openapi/graphql/.well-known.
_RECON_API_PATHS = [
    "swagger.json", "swagger.yaml", "swagger-ui/", "swagger-ui.html", "api-docs",
    "v2/api-docs", "v3/api-docs", "openapi.json", "openapi.yaml", "api/swagger.json",
    "graphql", "graphiql", "api/graphql", "playground", "redoc", "docs", "api/docs",
    "wp-json/", "rest/", "api/", "api/v1/", "api/v2/",
    ".well-known/openid-configuration", ".well-known/oauth-authorization-server",
    ".well-known/security.txt",
]


# Backup/temp suffix variants of common filenames (editor swap files, archives).
_RECON_BACKUP_SUFFIXES = ["~", ".bak", ".old", ".orig", ".save", ".swp", ".copy",
                          ".1", ".backup", ".tmp", ".zip", ".tar.gz", ".sql"]


_RECON_BACKUP_SEEDS = ["index.php", "index.html", "config.php", "app.js", "web.config",
                       "wp-config.php", "database", "backup", "db", "site", "www",
                       "main.js", "style.css"]


# Virtual-host names to fuzz via the Host header.
_RECON_VHOST_WORDS = ["admin", "internal", "intranet", "staging", "stage", "dev",
    "test", "qa", "uat", "api", "portal", "dashboard", "beta", "preprod", "corp",
    "vpn", "git", "jenkins", "jira", "grafana", "kibana", "backup", "old", "legacy",
    "private", "db", "monitor", "status"]


def _recon_backup_wordlist() -> list:
    return list(dict.fromkeys(s + suf for s in _RECON_BACKUP_SEEDS for suf in _RECON_BACKUP_SUFFIXES))


def _recon_vhost_fuzz(base: str, args: dict, timeout: float) -> dict:
    """Fuzz the Host header against the base IP/host and report vhosts whose
    response DIFFERS from the default-Host baseline (a distinct virtual host)."""
    parsed = urllib.parse.urlparse(base)
    host = parsed.hostname or ""
    parts = host.split(".")
    domain = ".".join(parts[-2:]) if len(parts) >= 2 else host
    words = args.get("wordlist") or list(_RECON_VHOST_WORDS)
    candidates = [w if "." in w else f"{w}.{domain}" for w in words]
    baseline = _recon_fetch(base, timeout, max_bytes=6000)
    b_status, b_size = baseline.get("status"), len(baseline.get("body", "") or "")
    concurrency = max(1, min(int(args.get("concurrency") or 6), 20))
    jitter_ms = max(0, min(int(args.get("jitter_ms") or 60), 1000))

    def probe(vhost):
        if jitter_ms:
            time.sleep(random.uniform(0, jitter_ms / 1000.0))
        r = _recon_fetch(base, timeout, max_bytes=6000, extra_headers={"Host": vhost})
        st = r.get("status")
        if not st or "body" not in r:
            return None
        if _recon_classify_body(st, r.get("headers"), r.get("body")) == "waf_challenge":
            return None
        size = len(r.get("body", "") or "")
        if st != b_status or abs(size - b_size) > 64:
            title = ""
            m = re.search(r"<title[^>]*>(.*?)</title>", r.get("body", ""), re.IGNORECASE | re.DOTALL)
            if m:
                title = re.sub(r"\s+", " ", m.group(1)).strip()[:80]
            return {"vhost": vhost, "status": st, "size": size, "title": title}
        return None

    found = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recon-vhost") as ex:
        for r in ex.map(probe, candidates):
            if r:
                found.append(r)
    found.sort(key=lambda x: (x["status"], x["vhost"]))
    return {"base": base, "mode": "vhost", "baseline_status": b_status, "baseline_size": b_size,
            "tested": len(candidates), "found_count": len(found), "found": found,
            "note": "Host-header vhost fuzz: responses that DIFFER from the default-Host baseline. "
                    "A hit is a distinct virtual host served off this IP."}


def tool_recon_content_discovery(args: dict) -> dict:
    """AUTHORIZED RECON. Probe a base URL for a high-value path list. Establishes
    a catch-all baseline first (random path) so SPA/WAF sites that return 200 for
    everything don't produce false positives — only responses that DIFFER from
    the baseline (and aren't WAF challenges) are reported. Read-only. mode=api,
    backup, or vhost swap the wordlist/technique; default is the general list."""
    base = (args.get("url") or args.get("target") or "").strip()
    if not base:
        return {"error": "url required"}
    base = _recon_prep_url(base)
    base = base.rstrip("/") + "/"
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 15.0))
    mode = (args.get("mode") or "").lower().strip()
    if mode == "vhost":
        return _recon_vhost_fuzz(base, args, timeout)
    # Quiet mode: a small, highest-signal path set + low concurrency. A content
    # IDS scores REQUESTS (not timing), so a smaller footprint = fewer chances to
    # trip the "active scanning" signature. This is the stealth lever that
    # actually works against a content sensor (jitter does not).
    if args.get("quiet"):
        paths = args.get("wordlist") or list(_RECON_QUIET_PATHS)
        concurrency = 2
        jitter_ms = max(200, min(int(args.get("jitter_ms") or 200), 1000))
    else:
        if mode == "api":
            default_paths = list(_RECON_API_PATHS)
        elif mode == "backup":
            default_paths = _recon_backup_wordlist()
        else:
            default_paths = list(_RECON_COMMON_PATHS)
        paths = args.get("wordlist") or default_paths
        concurrency = max(1, min(int(args.get("concurrency") or 6), 20))
        jitter_ms = max(0, min(int(args.get("jitter_ms") or 60), 1000))
    random.shuffle(paths)
    baseline = _recon_baseline(base, timeout)

    def probe(path):
        if jitter_ms:
            time.sleep(random.uniform(0, jitter_ms / 1000.0))
        r = _recon_fetch(urllib.parse.urljoin(base, path), timeout=timeout, max_bytes=6000)
        st = r.get("status")
        if not st or st == 404 or st >= 500:
            return None
        cls = _recon_classify_body(st, r.get("headers"), r.get("body"))
        if cls == "waf_challenge":
            return None  # blocked, not a real find
        if _recon_same_as_baseline(r, baseline):
            return None  # catch-all 200, not a distinct resource
        clen = r.get("headers", {}).get("Content-Length", "") or str(len(r.get("body", "") or ""))
        return {"path": path, "status": st, "size": clen, "type": cls,
                "url": urllib.parse.urljoin(base, path)}

    found = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recon-disc") as ex:
        for r in ex.map(probe, paths):
            if r:
                found.append(r)
    found.sort(key=lambda x: (x["status"], x["path"]))
    note = "non-404 responses that DIFFER from the catch-all baseline."
    if baseline["is_catchall"]:
        note += " NOTE: site returns 200 for unknown paths (SPA/catch-all) — a bare 200 is NOT proof a path exists."
    if baseline["classification"] == "waf_challenge":
        note += " A WAF/Cloudflare challenge is active; results may be filtered."
    return {"base": base, "mode": mode or "paths", "tested": len(paths), "found_count": len(found),
            "catchall_detected": baseline["is_catchall"],
            "waf_detected": baseline["classification"] == "waf_challenge",
            "found": found, "note": note}


def tool_validate_finding(args: dict) -> dict:
    """AUTHORIZED PENTEST. Sanity-check a suspected finding BEFORE reporting it, to
    avoid false positives on decoys / catch-all pages / WAF blocks. Fetches the
    url, diffs it against a random-path baseline, classifies the response, and
    flags placeholder/decoy markers (example.invalid, 'placeholder', 'nothing
    sensitive', lorem ipsum, …). Returns likely_real + reasons so you don't
    report a honeypot. Read-only. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    parsed = urllib.parse.urlparse(url)
    baseline = _recon_baseline(f"{parsed.scheme}://{parsed.netloc}/", timeout)
    r = _recon_fetch(url, timeout=timeout, max_bytes=8000)
    if "error" in r:
        return {"url": url, "likely_real": False, "reasons": [f"request failed: {r['error']}"]}
    body = r.get("body", "") or ""
    cls = _recon_classify_body(r.get("status"), r.get("headers"), body)
    reasons, likely_real = [], True
    if r.get("status") != 200:
        reasons.append(f"status {r.get('status')} (not 200)"); likely_real = False
    if _recon_same_as_baseline(r, baseline):
        reasons.append("identical to the catch-all baseline (not a distinct resource)"); likely_real = False
    if cls == "waf_challenge":
        reasons.append("response is a WAF/block page"); likely_real = False
    markers = ["example.invalid", "example.com", "placeholder", "changeme", "lorem ipsum",
               "nothing sensitive", "do not use", "dummy", "sample data", "not a real", "test data"]
    hit = [m for m in markers if m in body.lower()]
    if hit:
        reasons.append(f"contains placeholder/decoy markers: {hit}"); likely_real = False
    if likely_real:
        reasons.append("distinct 200 resource, not a catch-all/WAF, no placeholder markers")
    return {"url": url, "status": r.get("status"), "classification": cls,
            "likely_real": likely_real, "resource_signal_only": True, "reasons": reasons, "snippet": body[:200],
            "note": "This is a resource heuristic, not vulnerability validation. Both positive and negative signals require "
                    "claim-specific evidence. Use observe_response for full captures, then record_finding with status validated."}
