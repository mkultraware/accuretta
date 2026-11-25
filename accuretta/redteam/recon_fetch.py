# SPDX-License-Identifier: AGPL-3.0-only
"""Fetching for red-team tools: redirects re-checked against the scope, the mission
proxy, identities applied per request, and the shared recon fetch.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.settings import get_settings
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.identities import _RT_IDENTITY_LABELS
from accuretta.redteam.identities import _RT_IDENTITY_SEEN
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.network_scope import _rt_connect_block
from accuretta.redteam.network_scope import _rt_scope_block
from accuretta.redteam.request_headers import _rt_apply_mission_user_agent
from accuretta.redteam.request_headers import _rt_jitter
from accuretta.redteam.request_headers import _rt_stealth_headers


_RT_IDENTITY_SEEN_TTL = 1200.0


def _rt_identity_mark_seen(label: str) -> None:
    key = str((_rt_authorized_mission(_rt_context_chat()) or {}).get("authorization_id") or "")
    if key and label in _RT_IDENTITY_LABELS:
        _RT_IDENTITY_SEEN[(key, label)] = time.time()


def _rt_identity_seen(label: str) -> bool:
    key = str((_rt_authorized_mission(_rt_context_chat()) or {}).get("authorization_id") or "")
    return bool(key) and time.time() - _RT_IDENTITY_SEEN.get((key, label), 0.0) < _RT_IDENTITY_SEEN_TTL


class _ScopeCheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects normally, except across an active RT scope boundary."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        chat = _rt_context_chat()
        if _rt_authorized_mission(chat):
            block = _rt_scope_block("redirect", {"url": newurl})
            if block:
                raise urllib.error.URLError(block)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _IdentityRedirect(_ScopeCheckedRedirect):
    """Scope-checked redirects that keep a test account's sign-in on the host it
    was entered for: a redirect to another host is followed without it, the way
    a browser would not send that site's cookie elsewhere."""
    def __init__(self, origin_url: str, header_names):
        self._host = (urllib.parse.urlsplit(origin_url).hostname or "").lower()
        self._names = {str(name).lower() for name in header_names}

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and (urllib.parse.urlsplit(newurl).hostname or "").lower() != self._host:
            for held in (new.headers, new.unredirected_hdrs):
                for key in [k for k in held if str(k).lower() in self._names]:
                    held.pop(key, None)
        return new


def _rt_proxy_handler():
    """Route red-team HTTP through settings.rt_proxy (Burp / Tor / VPN gateway)
    when set - the real origin never touches the target."""
    p = (get_settings().get("rt_proxy") or "").strip()
    if not p:
        return None
    return urllib.request.ProxyHandler({"http": p, "https": p})


class _ReconNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _recon_fetch(url: str, timeout: float = 8.0, max_bytes: int = 8000,
                 extra_headers: dict | None = None, follow: bool = True,
                 scope_exempt: bool = False) -> dict:
    """Single GET (TLS verification off so self-signed targets still respond).
    During an engagement the URL is scope-checked here, where the request is
    made; scope_exempt is only for a user-approved third-party lookup.
    extra_headers override/add request headers (e.g. a Host override for vhost
    fuzzing). follow=False stops at the first 3xx so the caller can read the
    Location header (open-redirect / CRLF checks). {status, headers, body, url}."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    hdrs = _rt_stealth_headers()
    if extra_headers:
        hdrs.update(extra_headers)
    hdrs = _rt_apply_mission_user_agent(hdrs)
    if not scope_exempt:
        _scope = _rt_connect_block(url)
        if _scope:
            return {"error": _scope, "url": url, "scope_blocked": True}
    _gov = _rt_gov_admit(_rt_url_host(url), "GET", url=url)
    if _gov:
        return {"error": _gov, "url": url, "rate_limited": True}
    req = urllib.request.Request(url, headers=hdrs)
    _handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if not follow:
        _handlers.insert(0, _ReconNoRedirect())
    else:
        _handlers.insert(0, _ScopeCheckedRedirect())
    _ph = _rt_proxy_handler()
    if _ph:
        _handlers.append(_ph)
    opener = urllib.request.build_opener(*_handlers)
    _rt_jitter()
    try:
        resp = opener.open(req, timeout=timeout)
        body = resp.read(max_bytes).decode("utf-8", "replace")
        return {"status": resp.status, "headers": {k: v for k, v in resp.headers.items()},
                "body": body, "url": resp.url}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(max_bytes).decode("utf-8", "replace")
        except Exception:
            pass
        return {"status": e.code, "headers": {k: v for k, v in e.headers.items()},
                "body": body, "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}
