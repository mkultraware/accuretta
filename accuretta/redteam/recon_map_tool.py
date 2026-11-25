# SPDX-License-Identifier: AGPL-3.0-only
"""The recon_map tool: an attack-surface map of a target from passive sources, its
pages, scripts, endpoints and technologies, rendered with a scope-checked browser.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.optional_imports import _HAVE_PLAYWRIGHT
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.redteam.command_scope import _rt_scope_in_tokens
from accuretta.redteam.command_scope import _rt_scope_out_tokens
from accuretta.redteam.graphql import _map_find_graphql
from accuretta.redteam.graphql import _map_graphql_introspect
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.http_tools import _MAP_ID_SEG_RE
from accuretta.redteam.http_tools import _map_path_id
from accuretta.redteam.identities import _rt_active_identities
from accuretta.redteam.identities import _rt_identity_label
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.network_scope import _rt_self_ports
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.recon_map import _map_openapi
from accuretta.redteam.request_headers import _bounded_int
from accuretta.redteam.request_headers import _rt_clean_user_agent
from accuretta.redteam.scoped_browser import _ScopedBrowserWorker


def _rt_browser_policy(chat: dict | None) -> dict | None:
    """Immutable mission policy handed to the Playwright worker thread."""
    mission = _rt_authorized_mission(chat)
    if not mission:
        return None
    allowed = tuple(_rt_scope_in_tokens(chat))
    denied = tuple(_rt_scope_out_tokens(chat))
    return {
        "authorization_id": str(mission.get("authorization_id") or ""),
        "allowed": allowed,
        "denied": denied,
        "user_agent": _rt_clean_user_agent(mission.get("user_agent")),
        "self_ports": tuple(sorted(_rt_self_ports())),
        "allow_destructive": mission.get("allow_destructive") is not False,
        "rate_limit_rpm": _bounded_int(mission.get("rate_limit_rpm"), 0, 6000),
        "request_cap": _bounded_int(mission.get("request_cap"), 0, 1000000),
        # Pacing reads these two; see _rt_gov_interval.
        "engagement": str(mission.get("engagement") or ""),
        "automated_allowed": mission.get("automated_allowed") is not False,
    }


_RT_BROWSER_WORKER = _ScopedBrowserWorker()


# Links/resources in HTML. Captures href/src/action targets.
_MAP_HREF_RE = re.compile(r"""(?:href|src|action)\s*=\s*["']([^"'<>\s]+)""", re.I)


# <form ...> ... </form> blocks (non-greedy, DOTALL) to read method/action/fields.
_MAP_FORM_RE = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.I | re.S)


_MAP_ATTR_RE = lambda name: re.compile(name + r"""\s*=\s*["']([^"']*)["']""", re.I)


_MAP_ACTION_RE = _MAP_ATTR_RE("action")


_MAP_METHOD_RE = _MAP_ATTR_RE("method")


_MAP_FIELD_RE = re.compile(r"""<(?:input|select|textarea)\b[^>]*?\bname\s*=\s*["']([^"']+)["']""", re.I)


# URL-ish string literals inside JavaScript (the SPA endpoint goldmine): an
# absolute path or http(s) URL in quotes/backticks, optional query string.
_MAP_JS_URL_RE = re.compile(
    r"""["'`](https?://[^"'`\s]{3,300}|/[A-Za-z0-9_][A-Za-z0-9_./\-]{0,200}(?:\?[^"'`\s]{0,200})?)["'`]""")


# Explicit client calls — fetch(...), axios.get(...), .ajax({url:...}), xhr.open('GET', ...).
_MAP_JS_CALL_RE = re.compile(
    r"""(?:fetch|axios(?:\.\w+)?|\.(?:get|post|put|delete|patch)|open|url)\s*(?:\(|:)\s*["'`]([^"'`]{2,300})["'`]""", re.I)


# Links we never crawl: they log you out / destroy state, or are static assets
# with no attack surface. Keeps the map lean and the crawl safe (read-only).
_MAP_SKIP_RE = re.compile(
    r"(?:logout|log-off|logoff|signout|sign-out|/delete|/remove|/destroy|/purge|"
    r"\.(?:png|jpe?g|gif|svg|ico|webp|woff2?|ttf|eot|css|map|pdf|zip|mp4|mp3|avif))(?:$|[?#])", re.I)


# Path/param keywords that mark an endpoint as high-value to test first.
_MAP_JUICY_RE = re.compile(
    r"(api|admin|account|dashboard|user|login|auth|token|search|query|file|"
    r"upload|download|report|export|id|redirect|url|path|cmd|exec|debug|graphql)", re.I)


def _map_same_origin(url: str, origin: tuple) -> bool:
    try:
        u = urllib.parse.urlsplit(url)
        return (u.scheme, u.hostname, u.port or (443 if u.scheme == "https" else 80)) == origin
    except Exception:
        return False


def _map_abs(href: str, base: str) -> str:
    """Absolute, fragment-stripped URL, or '' for non-http (mailto:, js:, data:)."""
    try:
        u = urllib.parse.urljoin(base, href.strip())
        u = urllib.parse.urldefrag(u)[0]
        return u if u.startswith(("http://", "https://")) else ""
    except Exception:
        return ""


def _map_template(url: str) -> str:
    """Collapse ID-like path segments to {id} so near-identical URLs dedupe."""
    try:
        u = urllib.parse.urlsplit(url)
        segs = [("{id}" if _MAP_ID_SEG_RE.match(s) else s) for s in u.path.split("/")]
        keys = "&".join(sorted(k for k, _ in urllib.parse.parse_qsl(u.query)))
        return f"{u.scheme}://{u.netloc}{'/'.join(segs)}" + (f"?{keys}" if keys else "")
    except Exception:
        return url


def _map_params(url: str) -> list:
    try:
        return [k for k, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)]
    except Exception:
        return []


def _map_forms(body: str, page_url: str) -> list:
    """Parse <form> blocks into {method, action(abs), fields:[names]}."""
    out = []
    for attrs, inner in _MAP_FORM_RE.findall(body or ""):
        action = (_MAP_ACTION_RE.search(attrs) or [None, ""])[1] if _MAP_ACTION_RE.search(attrs) else ""
        method = ((_MAP_METHOD_RE.search(attrs) or [None, "get"])[1] if _MAP_METHOD_RE.search(attrs) else "get").upper()
        fields = sorted(set(_MAP_FIELD_RE.findall(inner)))
        target = _map_abs(action, page_url) or page_url
        if fields:
            out.append({"method": "POST" if method == "POST" else "GET",
                        "action": target, "fields": fields})
    return out


def _map_js_endpoints(js_text: str) -> set:
    """Pull path/URL literals and explicit client-call targets out of JS/JSON."""
    found = set()
    for m in _MAP_JS_URL_RE.findall(js_text or ""):
        found.add(m)
    for m in _MAP_JS_CALL_RE.findall(js_text or ""):
        if m.startswith(("http://", "https://", "/")):
            found.add(m)
    return found


def _map_score(ep: dict) -> int:
    """Higher = test first: parameters present, then juicy keywords, then POST."""
    score = 0
    score += 40 * min(len(ep.get("params") or []), 4)
    if ep.get("path_id"):
        score += 30
    if _MAP_JUICY_RE.search(ep.get("url", "")):
        score += 25
    if ep.get("method") == "POST":
        score += 10
    if ep.get("source") == "js":
        score += 5
    return score


# Common locations where apps publish an OpenAPI/Swagger spec or a GraphQL
# endpoint — probed when the model doesn't hand us an explicit URL, so a weak
# model just calls recon_map(url) and the spec-derived surface folds in for free.
_MAP_SPEC_PATHS = ["/openapi.json", "/swagger.json", "/v3/api-docs", "/v2/api-docs",
                   "/api-docs", "/swagger/v1/swagger.json", "/.well-known/openapi.json"]


def _map_fetch_openapi(base: str, timeout: float, extra_headers: dict | None = None) -> tuple:
    """Probe common spec paths; return (spec_dict, url) for the first that parses."""
    for p in _MAP_SPEC_PATHS:
        u = urllib.parse.urljoin(base, p)
        r = _recon_fetch(u, timeout=timeout, max_bytes=2_000_000, extra_headers=extra_headers)
        if r.get("error") or r.get("status") != 200:
            continue
        try:
            spec = json.loads(r.get("body") or "")
        except Exception:
            continue
        if isinstance(spec, dict) and (spec.get("openapi") or spec.get("swagger") or spec.get("paths")):
            return spec, u
    return None, None


def _map_render(url: str, session: str, policy: dict, timeout: float) -> dict:
    """Render a page with the scoped headless browser and harvest rendered links,
    form-field names, and the endpoints the SPA actually CALLED (captured network
    requests) — the surface a static crawler can't see. Reuses the given browser
    session, so a login performed earlier in that rt_browser session carries over,
    giving an authenticated map."""
    nav = _RT_BROWSER_WORKER.call({"action": "navigate", "url": url, "session": session,
                                   "policy": policy, "timeout": timeout})
    if nav.get("error"):
        return {"error": nav["error"]}
    links, fields = set(), set()
    for el in (nav.get("elements") or []):
        if el.get("href"):
            links.add(el["href"])
        if el.get("name"):
            fields.add(el["name"])
    reqs = _RT_BROWSER_WORKER.call({"action": "requests", "session": session,
                                    "policy": policy, "limit": 300})
    api = set()
    for ev in (reqs.get("events") or []):
        if ev.get("blocked"):
            continue
        rtype = (ev.get("resource_type") or "").lower()
        u = ev.get("url") or ""
        if u and (rtype in ("xhr", "fetch") or "/api" in u or "?" in u):
            api.add(u)
    return {"links": links, "fields": fields, "api": api}


def tool_recon_map(args: dict) -> dict:
    """AUTHORIZED RECON. Map a web target's attack surface and return a ready-to-
    act inventory of endpoints + parameters. Crawls same-origin HTML links,
    parses <form> fields, AND mines linked JavaScript for API endpoints (where
    modern single-page apps hide them — a link-only crawler misses these). Output
    is built to collapse the model's next decision: `targets_for_batch_probe` is a
    paste-ready list for batch_probe, and `next` names the obvious follow-up.
    Read-only (GET only; never submits forms or follows logout/delete links).
    Bounded and scope-enforced. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    base = (args.get("url") or args.get("target") or "").strip()
    if not base:
        return {"error": "url (or target) required"}
    base = _recon_prep_url(base)
    try:
        b = urllib.parse.urlsplit(base)
        origin = (b.scheme, b.hostname, b.port or (443 if b.scheme == "https" else 80))
    except Exception:
        return {"error": f"could not parse url: {base}"}
    same_origin = args.get("same_origin", True) is not False
    max_pages = max(1, min(int(args.get("max_pages") or 40), 150))
    max_depth = max(0, min(int(args.get("max_depth") or 3), 6))
    max_js = max(0, min(int(args.get("max_js") or 15), 40))
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 15.0))
    # Authenticated reuse: send caller-supplied cookies/headers on every static
    # fetch (and the GraphQL POST), so the map covers the behind-login surface.
    extra = {}
    if isinstance(args.get("headers"), dict):
        extra.update({str(k): str(v) for k, v in args["headers"].items() if v is not None})
    if args.get("cookies"):
        extra["Cookie"] = str(args["cookies"])
    # Access Control engagements: identity A or B crawls signed in as that stored
    # test account. Any other identity value stays a plain coverage label.
    access_accounts = _rt_active_identities()
    if access_accounts and _rt_identity_label(args.get("identity")) in access_accounts:
        extra.update(access_accounts[_rt_identity_label(args.get("identity"))])

    seen_pages, seen_tmpl = set(), set()
    endpoints, forms, js_files = {}, [], []
    unreachable, needs_auth = [], []

    def add_endpoint(url, method, source, extra_params=None, force_path_id=False, body=None):
        tmpl = _map_template(url)
        key = (method, tmpl)
        params = sorted(set(_map_params(url)) | set(extra_params or []))
        pid = _map_path_id(url) or force_path_id or ("{" in tmpl)
        ep = endpoints.get(key)
        if ep:
            ep["params"] = sorted(set(ep["params"]) | set(params))
            ep["path_id"] = ep.get("path_id") or pid
            if body:
                ep["body"] = sorted(set(ep.get("body") or []) | set(body))
            ep["seen"] += 1
        else:
            endpoints[key] = {"url": url, "template": tmpl, "method": method,
                              "params": params, "path_id": pid, "body": sorted(set(body or [])),
                              "source": source, "seen": 1}

    frontier = [(base, 0)]
    js_queue = []
    while frontier and len(seen_pages) < max_pages:
        url, depth = frontier.pop(0)
        tmpl = _map_template(url)
        if tmpl in seen_tmpl:
            continue
        seen_tmpl.add(tmpl)
        seen_pages.add(url)
        r = _recon_fetch(url, timeout=timeout, max_bytes=300_000, extra_headers=extra)
        if r.get("error"):
            unreachable.append({"url": url, "error": str(r["error"])[:120]})
            continue
        status = r.get("status")
        if status in (401, 403):
            needs_auth.append({"url": url, "status": status})
        body = r.get("body", "") or ""
        # This page is itself an endpoint if it carries query params or a path ID.
        if _map_params(url) or _map_path_id(url):
            add_endpoint(url, "GET", "crawl")
        # Mine linked JS even on a mostly-empty SPA shell.
        for raw in _MAP_HREF_RE.findall(body):
            absu = _map_abs(raw, url)
            if not absu:
                continue
            if _MAP_SKIP_RE.search(absu):
                if raw.lower().endswith(".js") and len(js_files) < max_js:
                    pass  # .js matched the asset skip but we DO want scripts
                else:
                    continue
            if absu.lower().split("?")[0].endswith(".js"):
                if len(js_files) + len(js_queue) < max_js and absu not in js_queue:
                    js_queue.append(absu)
                continue
            if same_origin and not _map_same_origin(absu, origin):
                continue
            if _MAP_SKIP_RE.search(absu):
                continue
            if _map_params(absu) or _map_path_id(absu):
                add_endpoint(absu, "GET", "crawl")
            nt = _map_template(absu)
            if nt not in seen_tmpl and depth < max_depth and len(seen_pages) + len(frontier) < max_pages:
                frontier.append((absu, depth + 1))
        # Forms → endpoints (parse fields; NEVER submit them).
        for f in _map_forms(body, url):
            if same_origin and not _map_same_origin(f["action"], origin):
                continue
            forms.append(f)
            qurl = f["action"] + ("?" if "?" not in f["action"] else "&") + "&".join(
                f"{n}=FUZZ" for n in f["fields"])
            add_endpoint(qurl if f["method"] == "GET" else f["action"], f["method"], "form")

    # Mine the collected JavaScript bundles for hidden endpoints.
    for js_url in js_queue[:max_js]:
        r = _recon_fetch(js_url, timeout=timeout, max_bytes=800_000, extra_headers=extra)
        if r.get("error"):
            continue
        js_files.append(js_url)
        for ep_path in _map_js_endpoints(r.get("body", "")):
            absu = _map_abs(ep_path, base)
            if not absu or (same_origin and not _map_same_origin(absu, origin)):
                continue
            if _MAP_SKIP_RE.search(absu):
                continue
            add_endpoint(absu, "GET", "js")

    # OpenAPI/Swagger: an explicit spec URL, else probe common locations. A spec
    # hands over the WHOLE API surface at once — the biggest breadth win on APIs.
    auto = args.get("auto_spec", True) is not False
    spec, openapi_url, spec_count = None, (args.get("openapi_url") or "").strip(), 0
    if openapi_url:
        rr = _recon_fetch(_map_abs(openapi_url, base) or openapi_url, timeout=timeout, max_bytes=2_000_000, extra_headers=extra)
        if not rr.get("error"):
            try:
                spec = json.loads(rr.get("body") or "")
            except Exception:
                spec = None
    elif auto:
        spec, openapi_url = _map_fetch_openapi(base, timeout, extra_headers=extra)
    if isinstance(spec, dict):
        for e in _map_openapi(spec, base):
            if same_origin and not _map_same_origin(e["url"], origin):
                continue
            add_endpoint(e["url"], e["method"], "openapi",
                         extra_params=e["query"], force_path_id=e["path_id"], body=e["body"])
            spec_count += 1

    # GraphQL: explicit endpoint, else probe. Each query/mutation + its args is
    # testable surface; they share one POST endpoint, listed under graphql_operations.
    graphql_ops, graphql_url = None, (args.get("graphql_url") or "").strip()
    if graphql_url:
        graphql_url = _map_abs(graphql_url, base) or graphql_url
        graphql_ops = _map_graphql_introspect(graphql_url, timeout, extra_headers=extra)
    elif auto:
        graphql_ops, graphql_url = _map_find_graphql(base, timeout, extra_headers=extra)
    if graphql_ops:
        add_endpoint(graphql_url, "POST", "graphql",
                     extra_params=sorted({a for op in graphql_ops for a in op["args"]}))

    # Headless rendering (opt-in, render=true): a static crawl can't see links a
    # JS framework injects or the API calls an SPA makes at runtime. Drive the
    # scoped headless browser to render the base + top crawled pages and harvest
    # both. Reuses `session` so a login done earlier in that rt_browser session
    # carries over. Strictly defensive — any failure just falls back to static.
    rendered, render_note = 0, None
    if args.get("render"):
        if not _HAVE_PLAYWRIGHT:
            render_note = "render=true but Playwright isn't installed; mapped statically only."
        else:
            policy = _rt_browser_policy(_rt_context_chat())
            if not policy:
                render_note = "render=true but no active browser mission policy; mapped statically only."
            else:
                rsession = str(args.get("session") or "recon_map")
                budget = max(1, min(int(args.get("render_pages") or 4), 12))
                render_urls = [base] + [u for u in seen_pages if u != base]
                for ru in render_urls[:budget]:
                    rr = _map_render(ru, rsession, policy, min(timeout * 3, 30.0))
                    if rr.get("error"):
                        render_note = "rendering hit an error on at least one page; partial render."
                        continue
                    rendered += 1
                    for cand in (rr["links"] | rr["api"]):
                        absu = _map_abs(cand, base)
                        if not absu or (same_origin and not _map_same_origin(absu, origin)):
                            continue
                        if _MAP_SKIP_RE.search(absu):
                            continue
                        if _map_params(absu) or _map_path_id(absu) or "/api" in absu:
                            add_endpoint(absu, "GET", "render")

    eps = sorted(endpoints.values(), key=_map_score, reverse=True)
    for ep in eps:
        ep.pop("seen", None)
        if not ep.get("body"):
            ep.pop("body", None)
    param_names = sorted({p for ep in eps for p in ep["params"]})
    batch_targets = [ep["url"] for ep in eps if ep["method"] == "GET" and ep["params"]][:40]
    post_eps = [ep["url"] for ep in eps if ep["method"] == "POST"][:20]
    idor_candidates = [ep["template"] for ep in eps if ep.get("path_id")][:20]

    # Optional coverage seeding (seed_coverage=true): record each discovered
    # endpoint as an 'untested' row in security_coverage, so the engagement (and
    # the final report) shows the WHOLE surface vs. what's actually been tested —
    # the model can't silently forget an endpoint it never got to.
    seeded = 0
    if args.get("seed_coverage"):
        try:
            cid = _current_chat_id.get() or _get_current_chat()
            chats = get_chats()
            chat = chats.get("chats", {}).get(cid, {}) if cid else {}
            mid = str((_rt_mission_get(_rt_context_chat() or {}) or {}).get("authorization_id") or "")
            plans = list(chat.get("security_coverage") or [])
            have = {(p.get("mission_id"), p.get("feature"), p.get("target")) for p in plans}
            identity = str(args.get("identity") or ("authenticated" if (extra or args.get("render")) else "unauthenticated"))
            for ep in eps[:40]:
                feature = f"{ep['method']} {ep['template']}"[:240]
                key = (mid, feature, ep["url"])
                if key in have:
                    continue
                plans.append({"mission_id": mid, "feature": feature, "identity": identity[:120],
                              "target": ep["url"][:2000], "state": "untested", "evidence_ids": [],
                              "limitations": "surface-mapped by recon_map; not yet tested"})
                have.add(key)
                seeded += 1
            if seeded and cid:
                chat["security_coverage"] = plans
                chats.setdefault("chats", {})[cid] = chat
                save_json(CHATS_FILE, chats)
        except Exception as exc:
            render_note = ((render_note + " ") if render_note else "") + f"coverage seeding skipped: {exc}"

    # The `next` line exists to collapse a weak model's planning into one call.
    if batch_targets:
        nxt = ("Run batch_probe({targets: <targets_for_batch_probe>, probe: 'sqli'}) "
               "then repeat with 'xss','ssti','lfi'. For each hit, confirm with sql_injection "
               "or http_request and record_finding.")
    elif post_eps:
        nxt = ("No GET-parameter endpoints found; test the POST endpoints with http_request "
               "(send each form's fields as the body) and watch for injection/auth flaws.")
    else:
        nxt = ("No parameterized endpoints surfaced. Try recon_content_discovery for hidden "
               "paths, or re-run with an authenticated session / higher max_pages.")
    if idor_candidates:
        nxt += (" Also test IDOR/BOLA on idor_candidates: with http_request, swap the id for "
                "another account's and compare the response under each identity.")
    if graphql_ops:
        nxt += (f" GraphQL is live at {graphql_url} ({len(graphql_ops)} operations in "
                "graphql_operations): test each with http_request (POST a query naming its "
                "args), and try alias/batching abuse and field-level authz.")
    if access_accounts:
        # This engagement compares accounts; it does not run injection sweeps.
        nxt = ("Run rt_access_check on each endpoint whose path_id is true or that takes an id "
               "parameter, using its real url (not the {id} template) and owner = the account the "
               "object belongs to."
               + ("" if extra else " This crawl was signed out: repeat it with identity 'A' to "
                  "reach the pages behind sign-in."))

    notes = []
    if spec_count:
        notes.append(f"Ingested {spec_count} endpoints from an OpenAPI/Swagger spec at {openapi_url}.")
    if rendered:
        notes.append(f"Rendered {rendered} page(s) with the headless browser (JS-injected links + runtime API calls).")
    if render_note:
        notes.append(render_note)
    if seeded:
        notes.append(f"Seeded {seeded} endpoint(s) into security_coverage as 'untested'.")
    if needs_auth:
        notes.append(f"{len(needs_auth)} page(s) returned 401/403 — map behind login by supplying a session.")
    if unreachable:
        notes.append(f"{len(unreachable)} page(s) were unreachable.")
    if len(seen_pages) >= max_pages:
        notes.append(f"Hit the {max_pages}-page crawl budget; raise max_pages to go deeper.")
    if not js_files and max_js:
        notes.append("No JavaScript bundles were found/linked; a rendered SPA may hide its endpoints behind JS the crawler couldn't reach.")

    return {
        "target": base,
        "endpoints": eps[:120],
        "param_names": param_names,
        "targets_for_batch_probe": batch_targets,
        "post_endpoints": post_eps,
        "idor_candidates": idor_candidates,
        "forms": forms[:40],
        "graphql_endpoint": graphql_url if graphql_ops else None,
        "graphql_operations": (graphql_ops or [])[:80],
        "openapi_spec_url": openapi_url if spec_count else None,
        "stats": {"pages_crawled": len(seen_pages), "js_mined": len(js_files),
                  "endpoints": len(eps), "params": len(param_names),
                  "from_openapi": spec_count, "graphql_ops": len(graphql_ops or []),
                  "rendered_pages": rendered, "coverage_seeded": seeded,
                  "authenticated": bool(extra or rendered),
                  "needs_auth": len(needs_auth), "unreachable": len(unreachable)},
        "coverage_gaps": notes,
        "next": nxt,
    }
