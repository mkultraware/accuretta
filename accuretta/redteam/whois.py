# SPDX-License-Identifier: AGPL-3.0-only
"""Domain registration data over classic WHOIS, shaped like the RDAP result.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.redteam.osint import _whois_query


# ---- WHOIS over port 43, for TLDs without RDAP ----
# IANA's own WHOIS names each TLD's registry server ("whois:" in its answer to
# a bare TLD). Servers are cached per TLD for the life of the bridge.
_WHOIS_SERVERS: dict[str, str] = {}


_WHOIS_QUERY_FORMAT = {
    "whois.denic.de": "-T dn,ace {}",  # plain queries only get a status line
    "whois.jprs.jp": "{}/e",           # English output
}


# registry field names vary; these cover .se/.nu, .dk, .de, gTLD thick records
_WHOIS_FIELDS = {
    "registrar": ("registrar", "registrar name", "sponsoring registrar"),
    "registration": ("created", "creation date", "registered", "registration date",
                     "created on", "domain registration date", "registered on"),
    "expiration": ("expires", "registry expiry date", "registrar registration expiration date",
                   "expiry date", "expiration date", "paid-till", "expire date", "expires on"),
    "last changed": ("modified", "updated date", "last updated", "last modified", "changed",
                     "last-update", "updated on"),
}


_WHOIS_NS_KEYS = ("nserver", "name server", "nameserver", "nameservers", "hostname", "ns")


_WHOIS_STATUS_KEYS = ("state", "status", "domain status")


_WHOIS_FREE = re.compile(
    r"\b(no match|not found|no data found|no entries found|status:\s*free|domain is available"
    r"|available for registration|no object found)\b", re.I)


def _whois_server_for(domain: str) -> str:
    tld = domain.rsplit(".", 1)[-1].lower()
    if tld not in _WHOIS_SERVERS:
        answer = _whois_query("whois.iana.org", tld)
        m = re.search(r"^\s*whois:\s*(\S+)", answer, re.M | re.I)
        _WHOIS_SERVERS[tld] = m.group(1).strip().lower() if m else ""
    return _WHOIS_SERVERS[tld]


def _recon_whois(domain: str, rdap_error: str = "") -> dict:
    """Registration data over classic WHOIS, shaped like the RDAP result so the
    model and the passive intel card read it the same way. Contacts IANA and
    the registry's WHOIS server, never the target."""
    server = _whois_server_for(domain)
    if not server:
        raise RuntimeError(f"IANA lists no WHOIS server for .{domain.rsplit('.', 1)[-1]}")
    text = _whois_query(server, _WHOIS_QUERY_FORMAT.get(server, "{}").format(domain))
    events, status, nameservers, registrar, dnssec = {}, [], set(), "", ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "%#>" or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not value:
            continue
        if key in _WHOIS_NS_KEYS:
            nameservers.add(value.split()[0].lower().rstrip("."))
        elif key in _WHOIS_STATUS_KEYS:
            status.append(value.split()[0])
        elif key == "dnssec":
            dnssec = value.lower()
        else:
            for field, names in _WHOIS_FIELDS.items():
                if key in names:
                    if field == "registrar":
                        registrar = registrar or value
                    elif field not in events:
                        events[field] = value
                    break
    body = "\n".join(l for l in text.splitlines() if l.strip() and not l.lstrip().startswith(("%", "#")))
    result = {
        "domain": domain,
        "registered": not (_WHOIS_FREE.search(text) and not events),
        "registrar": registrar[:160],
        "status": status[:12],
        "events": events,
        "nameservers": sorted(nameservers)[:30],
        "dnssec_signed": dnssec.startswith(("signed", "yes")) or "signeddelegation" in dnssec,
        "entities": [],
        "source": f"WHOIS ({server}), passive",
        "contacted": f"whois.iana.org and {server}, not the target",
        "raw": body[:2500],
    }
    if rdap_error:
        result["note"] = f"RDAP had no answer ({rdap_error}); this TLD's registry is read over WHOIS instead."
    return result
