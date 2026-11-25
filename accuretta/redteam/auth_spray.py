# SPDX-License-Identifier: AGPL-3.0-only
"""The default-credential check for authorized tests, paced and stopping on rate limits.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.governor import _rt_gov_preflight
from accuretta.redteam.http_audit import _recon_prep_url
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.recon_fetch import _ScopeCheckedRedirect
from accuretta.redteam.recon_fetch import _rt_proxy_handler
from accuretta.redteam.request_headers import _rt_ua


_RECON_DEFAULT_CREDS = [
    ("admin", "admin"), ("admin", "password"), ("admin", "admin123"),
    ("admin", ""), ("admin", "123456"), ("admin", "changeme"),
    ("admin", "letmein"), ("root", "root"), ("root", "toor"),
    ("administrator", "administrator"), ("administrator", "password"),
    ("user", "user"), ("guest", "guest"), ("test", "test"),
    ("tomcat", "tomcat"), ("postgres", "postgres"), ("sa", "sa"),
]


class _RateLimited(Exception):
    """The engagement's request budget refused this request."""


def _recon_try_login(url, mode, user, pw, opts, timeout):
    """One login attempt. Returns (success: bool, status: int|None)."""
    import base64
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    _spray_handlers = [_ScopeCheckedRedirect(), urllib.request.HTTPSHandler(context=ctx)]
    _ph = _rt_proxy_handler()
    if _ph:
        _spray_handlers.append(_ph)
    opener = urllib.request.build_opener(*_spray_handlers)
    if _rt_gov_admit(_rt_url_host(url), "GET" if mode == "basic" else "POST", url=url):
        raise _RateLimited()
    if mode == "basic":
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req = urllib.request.Request(url, headers={"User-Agent": _rt_ua(),
                                                   "Authorization": "Basic " + token})
        try:
            resp = opener.open(req, timeout=timeout)
            return resp.status not in (401, 403, 407), resp.status
        except urllib.error.HTTPError as e:
            return e.code not in (401, 403, 407), e.code
        except Exception:
            return False, None
    # form mode
    user_field = opts.get("user_field") or "username"
    pass_field = opts.get("pass_field") or "password"
    data = {user_field: user, pass_field: pw}
    data.update(opts.get("extra_fields") or {})
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _rt_ua(), "Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = opener.open(req, timeout=timeout)
        text = resp.read(20000).decode("utf-8", "replace")
        status, final_url = resp.status, resp.url
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read(20000).decode("utf-8", "replace")
        except Exception:
            pass
        status, final_url = e.code, url
    except Exception:
        return False, None
    sm = opts.get("success_marker")
    fm = opts.get("fail_marker")
    if sm:
        return (sm.lower() in text.lower()), status
    if fm:
        return (fm.lower() not in text.lower()), status
    return (status in (301, 302, 303) or final_url != url), status


def tool_recon_auth_spray(args: dict) -> dict:
    """AUTHORIZED RECON ONLY. Credential spray / default-cred test against an
    HTTP login (basic or form). Rate-limited, attempt-capped, stops on first
    valid pair per user. For targets you are authorized to test."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    mode = (args.get("mode") or "basic").lower()
    if mode not in ("basic", "form"):
        return {"error": "mode must be 'basic' or 'form'"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    delay = max(0.0, min(float(args.get("delay") or 0.4), 5.0))

    users = args.get("usernames")
    passwords = args.get("passwords")
    if users and passwords:
        pairs = [(u, p) for u in users for p in passwords]
    elif users:
        common = ["admin", "password", "123456", "changeme", "letmein", ""]
        pairs = [(u, p) for u in users for p in common]
    else:
        pairs = list(_RECON_DEFAULT_CREDS)
    # dedupe, cap total attempts
    seen, capped = set(), []
    for pr in pairs:
        if pr not in seen:
            seen.add(pr)
            capped.append(pr)
    max_attempts = max(1, min(int(args.get("max_attempts") or 120), 300))
    capped = capped[:max_attempts]

    _gov = _rt_gov_preflight(len(capped))
    if _gov:
        return {"error": _gov, "rate_limited": True, "attempts_requested": len(capped)}
    valid, attempts, done_users, stopped = [], 0, set(), False
    for (u, p) in capped:
        if u in done_users:  # already cracked this user, skip rest of their pairs
            continue
        if attempts:
            time.sleep(delay + random.uniform(0, delay))  # rate limit + jitter
        try:
            ok, status = _recon_try_login(url, mode, u, p, args, timeout)
        except _RateLimited:
            stopped = True
            break
        attempts += 1
        if ok:
            valid.append({"username": u, "password": p, "status": status})
            done_users.add(u)
    out = {
        "url": url, "mode": mode, "attempts": attempts,
        "valid_count": len(valid), "valid": valid,
        "note": ("VALID CREDENTIALS FOUND — verify and capture evidence." if valid
                 else "no valid credentials in the tested set."),
    }
    if stopped:
        out["rate_limited"] = True
        out["note"] += " Stopped early: the engagement request budget was reached."
    return out
