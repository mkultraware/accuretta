# SPDX-License-Identifier: AGPL-3.0-only
"""Finding a GraphQL endpoint and reading its schema by introspection.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.request_headers import _rt_apply_mission_user_agent
from accuretta.redteam.request_headers import _rt_jitter
from accuretta.redteam.request_headers import _rt_stealth_headers


_MAP_GQL_PATHS = ["/graphql", "/api/graphql", "/v1/graphql", "/query"]


_MAP_GQL_INTROSPECT = ("{__schema{queryType{fields{name args{name}}}"
                       "mutationType{fields{name args{name}}}}}")


def _map_post_json(url: str, payload: dict, timeout: float = 8.0, extra_headers: dict | None = None) -> dict:
    """POST JSON with the same stealth headers, UA, rate-governance and jitter as
    _recon_fetch (which is GET-only). Used for GraphQL introspection."""
    gov = _rt_gov_admit(_rt_url_host(url), "POST", url=url)
    if gov:
        return {"error": gov}
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    hdrs = _rt_apply_mission_user_agent(_rt_stealth_headers())
    hdrs["Content-Type"] = "application/json"
    if extra_headers:
        hdrs.update(extra_headers)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    _rt_jitter()
    try:
        resp = opener.open(req, timeout=timeout)
        return {"status": resp.status, "body": resp.read(500_000).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:
            body = ""
        return {"status": e.code, "body": body}
    except Exception as e:
        return {"error": str(e)}


def _map_graphql_introspect(url: str, timeout: float = 8.0, extra_headers: dict | None = None) -> list | None:
    """Introspect a GraphQL endpoint → [{kind, name, args}] for queries+mutations."""
    r = _map_post_json(url, {"query": _MAP_GQL_INTROSPECT}, timeout, extra_headers=extra_headers)
    if r.get("error"):
        return None
    try:
        schema = (json.loads(r.get("body") or "{}").get("data") or {}).get("__schema") or {}
    except Exception:
        return None
    ops = []
    for kind, key in (("query", "queryType"), ("mutation", "mutationType")):
        for f in ((schema.get(key) or {}).get("fields") or []):
            if isinstance(f, dict) and f.get("name"):
                ops.append({"kind": kind, "name": f["name"],
                            "args": [a.get("name") for a in (f.get("args") or []) if isinstance(a, dict) and a.get("name")]})
    return ops or None


def _map_find_graphql(base: str, timeout: float, extra_headers: dict | None = None) -> tuple:
    for p in _MAP_GQL_PATHS:
        u = urllib.parse.urljoin(base, p)
        ops = _map_graphql_introspect(u, timeout, extra_headers=extra_headers)
        if ops:
            return ops, u
    return None, None
