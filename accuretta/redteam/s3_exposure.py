# SPDX-License-Identifier: AGPL-3.0-only
"""The exposure check: storage buckets named after a target that answer without
credentials.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.redteam.content_discovery import _recon_baseline
from accuretta.redteam.content_discovery import _recon_same_as_baseline
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.waf import _recon_classify_body
from accuretta.security.approvals import request_approval


def _recon_s3_candidates(host: str) -> list:
    """Derive candidate public-bucket names from a target host."""
    parts = (host or "").split(".")
    name = parts[-2] if len(parts) >= 2 else (host or "")
    name = name.replace("_", "-")
    cands = [name, f"{name}-assets", f"{name}-backup", f"{name}-backups", f"{name}-media",
             f"{name}-static", f"{name}-dev", f"{name}-prod", f"assets-{name}",
             f"{name}-uploads", f"{name}-data", (host or "").replace(".", "-")]
    return list(dict.fromkeys(c for c in cands if c))


def _recon_check_s3(host: str, explicit: list | None) -> dict:
    """Check candidate AWS S3 buckets for public listing. TOUCHES A THIRD PARTY
    (Amazon S3, outside the target), so it asks for approval first via an
    approval card. Read-only listing probes. {findings:[{severity,evidence}]}."""
    # A bucket name becomes part of a hostname, so it may only be a bucket name.
    buckets = [b for b in (str(x).strip().lower() for x in (explicit or _recon_s3_candidates(host)))
               if re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", b)][:16]
    if not buckets:
        return {"third_party": "aws-s3", "checked": 0, "findings": []}
    dec = request_approval(
        "Public S3 bucket check (third party: AWS)",
        "GET https://<bucket>.s3.amazonaws.com/ for: " + ", ".join(buckets[:8])
        + ("…" if len(buckets) > 8 else ""),
        {"kind": "recon_s3", "third_party": "aws-s3", "buckets": buckets, "host": host},
        timeout_s=300,
    )
    if dec.get("decision") != "approve":
        return {"third_party": "aws-s3", "skipped": True, "candidates": buckets,
                "reason": "user declined the third-party AWS S3 check"}
    findings = []
    for b in buckets:
        r = _recon_fetch(f"https://{b}.s3.amazonaws.com/?list-type=2", timeout=6.0, max_bytes=4000,
                         scope_exempt=True)
        body, st = r.get("body", "") or "", r.get("status")
        if st == 200 and "<ListBucketResult" in body:
            keys = re.findall(r"<Key>([^<]+)</Key>", body)
            findings.append({"severity": "high", "issue": f"public S3 bucket: {b}",
                             "evidence": f"listable ({len(keys)} keys shown)",
                             "bucket": b, "third_party": "aws-s3"})
        elif st == 403 and "AccessDenied" in body:
            findings.append({"severity": "info", "issue": f"private S3 bucket exists: {b}",
                             "evidence": "AccessDenied (bucket exists, listing denied)",
                             "bucket": b, "third_party": "aws-s3"})
    return {"third_party": "aws-s3", "checked": len(buckets), "findings": findings}


def tool_recon_check_exposure(args: dict) -> dict:
    """AUTHORIZED RECON. Targeted check for high-signal exposures and interprets
    them: readable .git/.svn, .env secrets, web.config, WEB-INF/web.xml, phpinfo,
    directory listing, Spring actuator, Apache server-status, security.txt, and
    (opt-in, third-party) public S3 buckets. Findings carry severity + evidence."""
    base = (args.get("url") or args.get("target") or "").strip()
    if not base:
        return {"error": "url required"}
    base = _recon_prep_url(base)
    base = base.rstrip("/") + "/"
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    findings = []
    baseline = _recon_baseline(base, timeout)

    def real(r):
        """A genuinely distinct 200 — not the catch-all page, not a WAF block."""
        if r.get("status") != 200 or "body" not in r:
            return False
        if _recon_same_as_baseline(r, baseline):
            return False
        if _recon_classify_body(r.get("status"), r.get("headers"), r.get("body")) == "waf_challenge":
            return False
        return True

    # exposed git repo — body must actually be a git ref, not HTML
    r = _recon_fetch(urllib.parse.urljoin(base, ".git/HEAD"), timeout)
    if real(r) and r["body"].strip().startswith("ref:"):
        findings.append({"severity": "high", "issue": "exposed .git repository",
                         "evidence": r["body"].strip()[:80],
                         "impact": "full source + history is reconstructable (git-dumper)."})
    # exposed env file — body must contain KEY=VALUE lines, not an HTML/SPA page
    r = _recon_fetch(urllib.parse.urljoin(base, ".env"), timeout)
    if real(r) and _recon_classify_body(r.get("status"), r.get("headers"), r["body"]) != "html_page" \
            and re.search(r"^[A-Z][A-Z0-9_]{2,}\s*=", r["body"], re.MULTILINE):
        keys = re.findall(r"^([A-Z][A-Z0-9_]{2,})\s*=", r["body"], re.MULTILINE)
        findings.append({"severity": "critical", "issue": "exposed .env file",
                         "evidence": "keys: " + ", ".join(keys[:8]),
                         "impact": "application secrets / credentials disclosed."})
    # spring actuator — body must actually be actuator JSON, not just a 200
    r = _recon_fetch(urllib.parse.urljoin(base, "actuator/env"), timeout)
    if real(r) and ('"propertySources"' in r["body"] or '"activeProfiles"' in r["body"]):
        findings.append({"severity": "high", "issue": "Spring Boot actuator/env exposed",
                         "evidence": "propertySources/activeProfiles present in JSON",
                         "impact": "configuration and secrets retrievable."})
    r = _recon_fetch(urllib.parse.urljoin(base, "actuator/heapdump"), timeout)
    cls = _recon_classify_body(r.get("status"), r.get("headers"), r.get("body"))
    if real(r) and cls not in ("html_page", "waf_challenge") and len(r.get("body", "")) > 2000:
        findings.append({"severity": "high", "issue": "Spring Boot actuator/heapdump exposed",
                         "evidence": "non-HTML binary heap dump served",
                         "impact": "full heap (secrets, sessions) retrievable."})
    # apache server-status — needs the actual status page signature
    r = _recon_fetch(urllib.parse.urljoin(base, "server-status"), timeout)
    if real(r) and "Apache Server Status" in r["body"]:
        findings.append({"severity": "medium", "issue": "Apache server-status exposed",
                         "evidence": "Apache Server Status page reachable",
                         "impact": "active requests, client IPs, internal vhosts disclosed."})
    # directory listing on the root
    r = _recon_fetch(base, timeout)
    if r.get("status") == 200 and re.search(r"<title>Index of /", r.get("body", ""), re.IGNORECASE):
        findings.append({"severity": "medium", "issue": "directory listing enabled",
                         "evidence": "Index of / autoindex page",
                         "impact": "filesystem contents browsable."})
    # .DS_Store — magic-byte validated
    r = _recon_fetch(urllib.parse.urljoin(base, ".DS_Store"), timeout, max_bytes=64)
    if r.get("status") == 200 and r.get("body", "").startswith("\x00\x00\x00\x01Bud1"):
        findings.append({"severity": "low", "issue": "exposed .DS_Store",
                         "evidence": "Bud1 magic present",
                         "impact": "directory/file names enumerable."})
    # .svn metadata — source/history disclosure (new SVN uses wc.db SQLite)
    r = _recon_fetch(urllib.parse.urljoin(base, ".svn/wc.db"), timeout, max_bytes=64)
    if r.get("status") == 200 and r.get("body", "").startswith("SQLite format 3"):
        findings.append({"severity": "high", "issue": "exposed .svn/wc.db",
                         "evidence": "SQLite working-copy DB served",
                         "impact": "source tree and history reconstructable."})
    # web.config — IIS config / connection strings
    r = _recon_fetch(urllib.parse.urljoin(base, "web.config"), timeout, max_bytes=4000)
    if real(r) and re.search(r"<configuration[>\s]", r.get("body", ""), re.IGNORECASE):
        findings.append({"severity": "high", "issue": "exposed web.config",
                         "evidence": "<configuration> XML served",
                         "impact": "IIS config, connection strings, app settings disclosed."})
    # WEB-INF/web.xml — Java servlet descriptor
    r = _recon_fetch(urllib.parse.urljoin(base, "WEB-INF/web.xml"), timeout, max_bytes=4000)
    if real(r) and re.search(r"<web-app[>\s]", r.get("body", ""), re.IGNORECASE):
        findings.append({"severity": "high", "issue": "exposed WEB-INF/web.xml",
                         "evidence": "<web-app> descriptor served",
                         "impact": "servlet mappings and framework internals disclosed."})
    # phpinfo — full PHP environment disclosure
    for _p in ("phpinfo.php", "info.php"):
        r = _recon_fetch(urllib.parse.urljoin(base, _p), timeout, max_bytes=6000)
        if r.get("status") == 200 and "phpinfo()" in r.get("body", "") and "PHP Version" in r.get("body", ""):
            findings.append({"severity": "medium", "issue": f"exposed {_p} (phpinfo)",
                             "evidence": "phpinfo() output served",
                             "impact": "PHP env, absolute paths, loaded modules disclosed."})
            break
    # security.txt — informational (published disclosure policy)
    r = _recon_fetch(urllib.parse.urljoin(base, ".well-known/security.txt"), timeout, max_bytes=2000)
    if real(r) and re.search(r"(?im)^\s*contact\s*:", r.get("body", "")):
        _c = re.search(r"(?im)^\s*contact\s*:\s*(.+)$", r.get("body", ""))
        findings.append({"severity": "info", "issue": "security.txt present",
                         "evidence": (_c.group(1).strip()[:80] if _c else "contact listed"),
                         "impact": "disclosure policy published (informational)."})
    # public S3 buckets — opt-in, third-party (asks for approval inside).
    if args.get("check_s3") or args.get("buckets"):
        _s3 = _recon_check_s3(urllib.parse.urlparse(base).hostname or base, args.get("buckets"))
        if _s3.get("findings"):
            findings.extend(_s3["findings"])
        elif _s3.get("skipped"):
            findings.append({"severity": "info", "issue": "S3 check skipped",
                             "evidence": _s3.get("reason", "declined")})

    note = "validated exposures only (content-checked, baseline-diffed)."
    if baseline["is_catchall"]:
        note += " Site returns 200 for unknown paths (SPA/catch-all) — bare 200s were rejected."
    if baseline["classification"] == "waf_challenge":
        note += " A WAF/Cloudflare challenge is active."
    return {"base": base, "catchall_detected": baseline["is_catchall"],
            "waf_detected": baseline["classification"] == "waf_challenge",
            "finding_count": len(findings),
            "findings": findings or [{"severity": "info", "issue": "no validated high-signal exposures found"}],
            "note": note}
