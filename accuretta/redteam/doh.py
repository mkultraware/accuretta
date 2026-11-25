# SPDX-License-Identifier: AGPL-3.0-only
"""Name lookups for recon over DNS-over-HTTPS, the cleaned host a recon tool targets,
and the recon user agent.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.public_tls import _public_source_tls
from accuretta.redteam.request_headers import _RT_UA_POOL


def _recon_clean_host(raw: str) -> str:
    """Strip scheme/path/query/port and whitespace; return the bare host.
    (For DNS/TLS/crt.sh callers that genuinely need a bare hostname.)"""
    h = (raw or "").strip()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0]
    if h.count(":") == 1:  # drop a trailing :port
        h = h.split(":", 1)[0]
    return h.strip().strip(".")


def _recon_http_doh(name: str, rtype: str, timeout: float = 6.0) -> list:
    """DNS-over-HTTPS query via dns.google. Returns a list of answer strings."""
    url = f"https://dns.google/resolve?name={urllib.parse.quote(name)}&type={rtype}"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_public_source_tls()) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [a.get("data", "").strip('"') for a in (data.get("Answer") or [])]


_RECON_UA = _RT_UA_POOL[0]  # deprecated alias; new code should call _rt_ua()
