# SPDX-License-Identifier: AGPL-3.0-only
"""The Shodan recon tool: host lookups, and spotting CDN and shared-hosting addresses
that belong to someone else.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.public_tls import _public_source_tls
from accuretta.core.settings import get_settings
from accuretta.redteam.doh import _RECON_UA
from accuretta.redteam.doh import _recon_clean_host
from accuretta.redteam.doh import _recon_http_doh
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.osint import _shodan_host_summary
from accuretta.redteam.osint import _shodan_refusal


_SHODAN_MAX_IPS = 8


def _shodan_get_json(url: str, timeout: float = 15.0):
    """GET a Shodan endpoint. Returns (data, None), (None, None) on 404 (no
    record), or (None, error). Errors never carry the URL, which holds the key;
    a refusal says what it means, with Shodan's own reason when it gave one."""
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": _RECON_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_public_source_tls()) as r:
            return json.loads(r.read(4_000_000).decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        reason = ""
        try:
            body = json.loads(e.read(4000).decode("utf-8", "replace"))
            reason = str(body.get("error") or "").strip()[:200] if isinstance(body, dict) else ""
        except Exception:
            pass
        return None, _shodan_refusal(e.code, reason)
    except Exception as e:
        return None, str(getattr(e, "reason", None) or type(e).__name__)[:200]


_SHODAN_CDN_WORDS = (
    "cloudflare", "akamai", "fastly", "cloudfront", "incapsula", "imperva", "sucuri",
    "stackpath", "edgecast", "bunny", "cdn77", "vercel", "netlify", "azure front door",
)


_SHODAN_SHARED_WORDS = (
    "one.com", "webcluster", "webpod", "shared", "cpanel", "plesk", "godaddy",
    "secureserver", "hostgator", "bluehost", "siteground", "hostinger", "ionos",
    "1and1", "namecheap", "dreamhost", "loopia", "binero", "wix", "squarespace",
    "shopify", "github.io", "wpengine", "kinsta", "pantheon",
)


def _recon_registered_domain(host: str) -> str:
    """Best-effort registered domain: portal.example.com -> example.com,
    app.example.co.uk -> example.co.uk."""
    labels = (host or "").lower().strip(".").split(".")
    if (len(labels) >= 3 and len(labels[-1]) == 2
            and labels[-2] in {"co", "com", "net", "org", "gov", "ac", "edu"}):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _shodan_shared_hosting(row: dict, base: str) -> dict | None:
    """Spot an IP that serves more than the target: a CDN edge or a shared web
    host. Shodan's ports and CVEs for such an IP describe the provider or other
    customers, so the model must not pin them on the target."""
    idb = row.get("internetdb") if isinstance(row.get("internetdb"), dict) else {}
    full = row.get("shodan") if isinstance(row.get("shodan"), dict) else {}
    names = {str(n).lower() for n in (idb.get("hostnames") or []) + (full.get("hostnames") or [])
             + (full.get("domains") or []) if n}
    mine = {n for n in names if base and (n == base or n.endswith("." + base))}
    other = sorted(names - mine)
    org = " ".join(str(full.get(k) or "") for k in ("org", "isp")).lower()
    blob = " ".join([org, " ".join(other), " ".join(str(c) for c in idb.get("cpes") or [])]).lower()
    tags = {str(t).lower() for t in (idb.get("tags") or []) + (full.get("tags") or [])}
    reasons, kind = [], ""
    cdn = sorted({w for w in _SHODAN_CDN_WORDS if w in blob})
    if cdn or "cdn" in tags:
        kind = "CDN or reverse proxy"
        reasons.append("CDN signals: " + ", ".join(cdn or ["tagged cdn by Shodan"]))
    shared = sorted({w for w in _SHODAN_SHARED_WORDS if w in blob})
    if shared:
        kind = kind or "shared web hosting"
        reasons.append("hosting provider signals: " + ", ".join(shared))
    if len(other) >= 2:
        kind = kind or "shared web hosting"
        reasons.append(f"{len(other)} hostname(s) on this IP are not under {base or 'the target'}")
    if not kind:
        return None
    if "cloud" in tags:
        reasons.append("tagged cloud by Shodan")
    who = base or "the target"
    if kind == "CDN or reverse proxy":
        caution = (f"{row['ip']} is a CDN or reverse-proxy edge. Shodan scanned the edge, not "
                   f"{who}'s own server, so these ports, software and CVEs belong to the CDN and "
                   f"are shared by every site behind it. Do not report them as {who}'s findings.")
    else:
        caution = (f"{row['ip']} looks like shared web hosting: several unrelated domains can sit "
                   f"on this one IP. The ports, software and CVEs may belong to the hosting "
                   f"provider or other customers, not {who}. Only attribute one to {who} if "
                   f"another source ties it to {who}; otherwise report it as the host's.")
    out = {"likely": True, "kind": kind, "reasons": reasons, "caution": caution}
    if other:
        out["other_names_sample"] = other[:8]
    return out


def tool_recon_shodan(args: dict) -> dict:
    """Look up what Shodan already knows about a host from its past scans.

    Resolves the domain through DoH, then queries Shodan's keyless InternetDB
    for each public IP, plus the full host API when a key is set in Settings.
    Every request goes to dns.google or Shodan, never the target."""
    target = _recon_clean_host(args.get("target") or args.get("domain") or args.get("host") or "")
    if not target:
        return {"error": "target required (a domain or IPv4 address)"}
    try:
        ipaddress.ip_address(target)
        ips = [target]
        # An IP target says nothing about whose it is; compare against the mission.
        mission = _rt_mission_get(_rt_context_chat() or {}) or {}
        base = _recon_registered_domain(_rt_url_host(str(mission.get("target") or "")))
        try:
            ipaddress.ip_address(base)
            base = ""
        except ValueError:
            pass
    except ValueError:
        base = _recon_registered_domain(target)
        try:
            answers = _recon_http_doh(target, "A")
        except Exception as e:
            return {"error": f"DNS lookup failed: {e}", "target": target}
        ips = sorted({a for a in answers if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", a)})
        if not ips:
            return {"error": f"{target} has no A records", "target": target}
    public, skipped = [], []
    for ip in ips:
        addr = ipaddress.ip_address(ip)
        (skipped if (addr.is_private or addr.is_loopback or addr.is_link_local
                     or addr.is_reserved) else public).append(ip)
    key = str(get_settings().get("shodan_api_key") or "").strip()
    hosts, errors = [], []
    for ip in public[:_SHODAN_MAX_IPS]:
        row = {"ip": ip}
        idb, err = _shodan_get_json("https://internetdb.shodan.io/" + ip)
        if err:
            errors.append(f"InternetDB {ip}: {err}")
        elif idb:
            row["internetdb"] = {k: (idb.get(k) or [])[:50] for k in
                                 ("ports", "hostnames", "cpes", "vulns", "tags")
                                 if idb.get(k)}
            if len(idb.get("vulns") or []) > 50:
                row["internetdb"]["vulns_total"] = len(idb["vulns"])
        else:
            row["internetdb"] = "no record"
        if key:
            url = (f"https://api.shodan.io/shodan/host/{ip}?"
                   + urllib.parse.urlencode({"key": key}))
            full, err = _shodan_get_json(url)
            if err:
                errors.append(f"Shodan host {ip}: {err}")
            elif full:
                row["shodan"] = _shodan_host_summary(full)
            else:
                row["shodan"] = "no record"
        shared = _shodan_shared_hosting(row, base)
        if shared:
            # Put the warning first so it is read before the ports and CVEs.
            row = {"ip": ip, "shared_hosting": shared, **{k: v for k, v in row.items() if k != "ip"}}
        hosts.append(row)
    shared_ips = [h["ip"] for h in hosts if h.get("shared_hosting")]
    result = {"target": target}
    if shared_ips:
        result["caution"] = (
            f"{', '.join(shared_ips)} serve(s) other sites besides {base or target} (CDN or "
            f"shared hosting). Data for these IPs describes the provider and its other "
            f"customers too. Do not list their ports, software or CVEs as {base or target}'s "
            f"findings unless another source ties them to it; describe them as the hosting "
            f"provider's.")
    result.update({
        "ips": ips, "hosts": hosts,
        "source": "Shodan InternetDB" + (" + Shodan host API" if key else "")
                  + " (past scans, passive)",
        "contacted": "dns.google and Shodan, not the target",
    })
    if not key:
        result["note"] = ("No Shodan API key in Settings, so only InternetDB ran "
                          "(ports, CPEs, CVEs, tags). Add a key for banners, org and services.")
    if len(public) > _SHODAN_MAX_IPS:
        result["truncated"] = f"looked up the first {_SHODAN_MAX_IPS} of {len(public)} IPs"
    if skipped:
        result["skipped_private"] = skipped
    if errors:
        result["errors"] = errors
    return result
