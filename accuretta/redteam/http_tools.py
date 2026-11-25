# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team HTTP tools: http_request with sessions, identities and response history,
and the access check that compares what each identity can reach.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from accuretta.redteam.http_summary import _HTTP_SUMMARY_COOKIE_CHARS
from accuretta.redteam.http_summary import _HTTP_SUMMARY_COOKIE_COUNT
from accuretta.redteam.http_summary import _clip_headers
from accuretta.redteam.http_summary import _clip_text
from accuretta.redteam.http_summary import _http_body_preview
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.settings import get_settings
from accuretta.redteam.findings import _evidence_context
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.identities import _RT_IDENTITY_ANON
from accuretta.redteam.identities import _RT_IDENTITY_LABELS
from accuretta.redteam.identities import _rt_active_identities
from accuretta.redteam.identities import _rt_identities_missing_reason
from accuretta.redteam.identities import _rt_identity_label
from accuretta.redteam.identities import _rt_identity_request
from accuretta.redteam.identities import _rt_identity_reserved_headers
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.recon_fetch import _IdentityRedirect
from accuretta.redteam.recon_fetch import _ScopeCheckedRedirect
from accuretta.redteam.recon_fetch import _rt_identity_mark_seen
from accuretta.redteam.recon_fetch import _rt_identity_seen
from accuretta.redteam.recon_fetch import _rt_proxy_handler
from accuretta.redteam.recon_map import _access_segments
from accuretta.redteam.recon_map import _save_http_request_evidence
from accuretta.redteam.request_headers import _browser_headers
from accuretta.redteam.request_headers import _rt_apply_mission_user_agent
from accuretta.redteam.browser_profiles import _pick_profile
from agent_reliability import digest
from agent_reliability import identity_fingerprint
from agent_reliability import response_state


# Path segments that are really IDs — collapse them so /user/1 and /user/2 are
# ONE thing to test, not thousands. Numeric, uuid, long hex, or date-ish.
_MAP_ID_SEG_RE = re.compile(
    r"^(?:\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|"
    r"[0-9a-f]{16,}|\d{4}-\d{2}-\d{2})$", re.I)


def _map_path_id(url: str) -> bool:
    """True when a path segment is an ID (/user/42, /orders/{uuid}). Those are
    IDOR/BOLA targets even with no query string, so they belong on the map."""
    try:
        return any(_MAP_ID_SEG_RE.match(s) for s in urllib.parse.urlsplit(url).path.split("/"))
    except Exception:
        return False


# Per-"session" cookie jars for http_request. Opt-in via the `session` arg so a
# model can carry auth across requests without re-passing cookies each time -
# while the default (no session) stays explicit so the cookie-tamper step is
# never hidden. Keyed by an arbitrary session name the model chooses.
_HTTP_SESSIONS: dict[str, dict] = {}


# Keep only a compact, model-readable view of each response here so a tool
# result can't carry a megabyte of HTML into the conversation; the full body
# stays in the EvidenceStore receipt. The previous-response snapshot lets a
# test-account run show what changed after an anonymous baseline without
# replaying either body.
_HTTP_RESPONSE_HISTORY: dict[tuple[str, str], dict] = {}


_HTTP_RESPONSE_HISTORY_LIMIT = 200


_HTTP_RESPONSE_HISTORY_LOCK = threading.Lock()


def _http_history_key(method: str, url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    normalized = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(),
                                           parts.path or "/", parts.query, ""))
    return (str(method or "GET").upper(), normalized)


def _http_identity_label(session, headers, cookies) -> str:
    if session:
        return f"session:{session}"
    names = {str(key).lower() for key in (headers or {})}
    if names & {"authorization", "cookie"} or cookies:
        return "explicit credentials"
    return "anonymous"


def _http_compare_with_previous(method, url, identity, status, headers, body,
                                set_cookie) -> dict | None:
    """Describe changes from the last request to this method+URL in-process.

    Only bounded header values and body/cookie digests are retained, never the
    bodies themselves. The comparison is advisory: the previous request may
    have used a different identity, so it is not a substitute for a recorded
    control.
    """
    bounded_headers, _ = _clip_headers(headers)
    header_values = {str(key).lower(): value for key, value in bounded_headers.items()}
    cookie_values = [_clip_text(value, _HTTP_SUMMARY_COOKIE_CHARS)
                     for value in (set_cookie or [])[:_HTTP_SUMMARY_COOKIE_COUNT]]
    body_text = body if isinstance(body, str) else str(body or "")
    snapshot = {
        "identity": identity,
        "status": status,
        "body_sha256": digest(body_text),
        "body_characters": len(body_text),
        "headers": header_values,
        "set_cookie_sha256": digest(cookie_values),
    }
    key = _http_history_key(method, url)
    with _HTTP_RESPONSE_HISTORY_LOCK:
        previous = _HTTP_RESPONSE_HISTORY.get(key)
        _HTTP_RESPONSE_HISTORY[key] = snapshot
        while len(_HTTP_RESPONSE_HISTORY) > _HTTP_RESPONSE_HISTORY_LIMIT:
            _HTTP_RESPONSE_HISTORY.pop(next(iter(_HTTP_RESPONSE_HISTORY)))
    if not previous:
        return None
    old_headers, new_headers = previous["headers"], header_values
    return {
        "previous_identity": previous["identity"],
        "previous_status": previous["status"],
        "status_changed": previous["status"] != status,
        "body_changed": previous["body_sha256"] != snapshot["body_sha256"],
        "body_characters_delta": len(body_text) - previous["body_characters"],
        "headers_added": sorted(set(new_headers) - set(old_headers)),
        "headers_removed": sorted(set(old_headers) - set(new_headers)),
        "headers_changed": sorted(key for key in set(old_headers) & set(new_headers)
                                  if old_headers[key] != new_headers[key]),
        "set_cookie_changed": previous["set_cookie_sha256"] != snapshot["set_cookie_sha256"],
        "note": ("Compared with this process's previous request to the same method+URL; "
                 "identity may differ. A change is a signal to inspect, not proof of a "
                 "vulnerability. Use recorded receipts and compare_evidence for a real control."),
    }


def tool_http_request(args: dict) -> dict:
    """AUTHORIZED PENTEST. Send an arbitrary HTTP request (method, url, headers,
    cookies, body) and return status + headers + body: the curl / Burp Repeater
    primitive for steps the read-only recon tools can't do (flip and re-encode a
    session cookie, aim an SSRF parameter, POST an SSTI payload, replay with a
    tampered header).

    Uses the engagement's required User-Agent when configured, otherwise a
    rotating browser fingerprint from the shared profile pool. The full response
    (every header, every Set-Cookie, the body) goes to the evidence receipt; the
    model sees a compact summary that still carries Set-Cookie (capped),
    redirect Location and error bodies. Non-2xx responses are returned, not
    retried, because an exploitation tool must see 401/403/404/405; only 429/503
    and connection errors rotate and retry. The identity used is returned too.
    Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    # Range/internal targets are plain HTTP; default to http:// when no scheme
    # is given (recon tools default to https, which is wrong for a local range).
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    headers: dict = {}
    for k, v in (args.get("headers") or {}).items():
        headers[str(k)] = str(v)
    # A named test account (Access Control playbook): the bridge attaches the
    # sign-in the user entered on the engagement form, so the model never handles
    # the credential. It cannot be mixed with credentials the model supplies,
    # otherwise the recorded account would not be the one the server saw.
    account, account_headers = "", {}
    if args.get("identity") not in (None, ""):
        chosen = _rt_identity_request(args.get("identity"))
        if chosen.get("error"):
            return {"error": chosen["error"], "invalid_arguments": True, "not_executed": True}
        account, account_headers = chosen["label"], chosen["headers"]
        reserved = _rt_identity_reserved_headers()
        clash = sorted(k for k in headers if k.lower() in reserved)
        if clash or args.get("cookies") or args.get("session"):
            return {"error": ("identity sends the request as a stored test account, so it cannot be "
                              "combined with session, cookies or a sign-in header"
                              + (f" ({', '.join(clash)})" if clash else "")
                              + ". Remove those, or drop identity to send your own credentials."),
                    "invalid_arguments": True, "not_executed": True}

    def _sent_fingerprint(sent: dict) -> str:
        if account_headers:
            return digest({"account": account,
                           "sign_in": {k.lower(): v for k, v in account_headers.items()}})
        return identity_fingerprint(sent)

    def _transcript_headers(sent: dict) -> dict:
        """Request headers for a saved proof file, without a stored sign-in."""
        hidden = {k.lower() for k in account_headers}
        return {k: (f"[account {account} sign-in, held by Accuretta]" if k.lower() in hidden else v)
                for k, v in sent.items()}
    # Optional cookie jar: when a `session` name is given, we merge previously
    # captured cookies for that session (explicit `cookies` still win) and, on
    # the way back, remember any Set-Cookie the server sends. Opt-in so the
    # default stays explicit and the deliberate cookie-tamper step is never
    # auto-hidden.
    session = args.get("session")
    cookies = dict(args.get("cookies") or {})
    if session:
        merged = dict(_HTTP_SESSIONS.get(str(session), {}))
        merged.update(cookies)  # explicit cookies override the jar
        cookies = merged
    if cookies and not any(k.lower() == "cookie" for k in headers):
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    body = args.get("body")
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
        headers.setdefault("Content-Type", "application/json")
    data = body.encode("utf-8") if isinstance(body, str) and body else None
    # Method defaults to POST when a body is present, else GET.
    method = (args.get("method") or ("POST" if data else "GET")).upper()
    # A raw body with no content type is almost always form-encoded here.
    if data and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    timeout = max(2.0, min(float(args.get("timeout") or 15.0), 30.0))
    identity_label = _http_identity_label(session, headers, cookies)
    if account:
        identity_label = account if account == _RT_IDENTITY_ANON else f"account {account}"

    def _headers_of(msg) -> tuple[dict, list]:
        """Response headers as a dict, plus the FULL list of Set-Cookie values
        kept separately (a dict would collapse multiple Set-Cookie headers into
        one, and the session cookie is exactly what a cookie-tamper finding
        needs)."""
        hdrs = {k: v for k, v in msg.items()}
        return hdrs, (msg.get_all("Set-Cookie") or [])

    def _absorb_cookies(set_cookie_list) -> None:
        """Fold response Set-Cookie values into the named session jar (opt-in)."""
        if not session:
            return
        jar = _HTTP_SESSIONS.setdefault(str(session), {})
        for sc in set_cookie_list or []:
            nv = sc.split(";", 1)[0].strip()
            if "=" in nv:
                cn, cv = nv.split("=", 1)
                jar[cn.strip()] = cv.strip()

    # Browser-identity rotation for evasion, but we capture the RESPONSE HEADERS
    # ourselves (the shared _open_with_rotation drops them). Retry only transient
    # blocks; every other status is returned so the model can read it.
    host = ""
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:
        pass
    tried: list = []
    last_exc: Exception | None = None
    attempts = 3
    for attempt in range(attempts):
        profile = _pick_profile(host, exclude=tried[-1] if tried else None)
        tried.append(profile)
        req_headers = _browser_headers(profile, accept_html=True)
        req_headers.update(headers)  # caller-supplied headers/cookies win
        for _name, _value in account_headers.items():
            for _key in [k for k in req_headers if k.lower() == _name.lower()]:
                req_headers.pop(_key, None)
            req_headers[_name] = _value
        req_headers = _rt_apply_mission_user_agent(req_headers)
        _gov = _rt_gov_admit(_rt_url_host(url), method, url=url)
        if _gov:
            return {"error": _gov, "url": url, "method": method, "rate_limited": True,
                    **({"request_identity": account} if account else {})}
        attempt_started = time.monotonic()
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
            _request_handlers = [_IdentityRedirect(url, account_headers) if account_headers
                                 else _ScopeCheckedRedirect()]
            _ph = _rt_proxy_handler()
            if _ph:
                _request_handlers.append(_ph)
            opener = urllib.request.build_opener(*_request_handlers)
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                resp_headers, set_cookie = _headers_of(resp.headers)
                raw = resp.read(1024 * 1024 + 1)
                final_url = resp.geturl()
            resp_body_text = raw.decode("utf-8", "replace")
            _absorb_cookies(set_cookie)
            result = {
                "ok": True, "status": status, "method": method, "url": url,
                "final_url": final_url,
                "content_type": resp_headers.get("Content-Type", ""),
                "headers": resp_headers,
                "set_cookie": set_cookie,  # every Set-Cookie the server sent
                "body": resp_body_text,
                "elapsed_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                "attempt": attempt + 1,
                "potentially_truncated": len(raw) > 1024 * 1024,
                "request_identity_fingerprint": _sent_fingerprint(req_headers),
                "user_agent": req_headers.get("User-Agent", ""),
            }
            if account:
                result["request_identity"] = account
            previous = _http_compare_with_previous(
                method, url, identity_label, status, resp_headers, resp_body_text, set_cookie)
            if previous:
                result["previous_response"] = previous
            if session:
                result["session_cookies"] = dict(_HTTP_SESSIONS.get(str(session), {}))
            if args.get("save_evidence"):
                ev = _save_http_request_evidence(
                    args.get("evidence_label") or args.get("save_evidence"),
                    method, url, _transcript_headers(req_headers), data, status, resp_headers, resp_body_text)
                if ev:
                    result.update(ev)
            return result
        except urllib.error.HTTPError as e:
            # HTTP errors still have a response status; evidence capture below
            # must use it instead of the success-path local.
            status = e.code
            resp_headers, set_cookie = _headers_of(e.headers)
            try:
                error_body = e.read(1024 * 1024 + 1)
                body_txt = error_body.decode("utf-8", "replace")
            except Exception:
                error_body = b""
                body_txt = ""
            # Only 429/503 are worth rotating past; 401/403/404/405/5xx are the
            # finding itself and are returned immediately.
            if e.code in (429, 503) and attempt < attempts - 1:
                last_exc = e
                time.sleep(0.4 + random.random() * 0.6)
                continue
            _absorb_cookies(set_cookie)  # a 302 login still sets cookies
            result = {
                "ok": False, "status": e.code, "method": method, "url": url,
                "content_type": resp_headers.get("Content-Type", ""),
                "headers": resp_headers,
                "set_cookie": set_cookie,
                "body": body_txt,
                "elapsed_ms": round((time.monotonic() - attempt_started) * 1000, 1),
                "attempt": attempt + 1,
                "potentially_truncated": len(error_body) > 1024 * 1024,
                "request_identity_fingerprint": _sent_fingerprint(req_headers),
                "user_agent": req_headers.get("User-Agent", ""),
                "note": f"HTTP {e.code}",
            }
            if account:
                result["request_identity"] = account
            previous = _http_compare_with_previous(
                method, url, identity_label, status, resp_headers, body_txt, set_cookie)
            if previous:
                result["previous_response"] = previous
            if session:
                result["session_cookies"] = dict(_HTTP_SESSIONS.get(str(session), {}))
            if args.get("save_evidence"):
                ev = _save_http_request_evidence(
                    args.get("evidence_label") or args.get("save_evidence"),
                    method, url, _transcript_headers(req_headers), data, status, resp_headers, body_txt)
                if ev:
                    result.update(ev)
            return result
        except Exception as e:
            # Connection-level failure (DNS, timeout, reset): retry with a fresh
            # identity, then give up.
            last_exc = e
            time.sleep(0.3 + random.random() * 0.4)
            continue
    return {"error": f"request failed: {last_exc}", "url": url, "method": method,
            **({"request_identity": account} if account else {})}


_ACCESS_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# Token overlap at or above this counts as "the same content". Session tokens,
# timestamps and CSRF fields make byte equality too strict for real pages.
_ACCESS_SAME = 0.9


_ACCESS_PREVIEW_CHARS = 500


def _access_similarity(first: str, second: str) -> float:
    """Share of word tokens two response bodies have in common (0.0 to 1.0)."""
    from collections import Counter
    if first == second:
        return 1.0
    a = Counter(re.findall(r"[A-Za-z0-9_@.\-]{2,}", (first or "")[:200000])[:20000])
    b = Counter(re.findall(r"[A-Za-z0-9_@.\-]{2,}", (second or "")[:200000])[:20000])
    size = max(sum(a.values()), sum(b.values()))
    if not size:
        return 0.0
    return round(sum((a & b).values()) / size, 3)


def _access_proof_candidates(owner_body: str, other_body: str, anonymous_body: str,
                             limit: int = 5) -> list[str]:
    """Exact strings from the owner's response that the other account also
    received and a signed-out request did not: ready to quote as proof."""
    shared = [seg for seg in _access_segments(owner_body)
              if seg in other_body and seg not in (anonymous_body or "")]
    # Prefer values that look like personal data (an address, a number) and
    # then the longer ones: a nav label proves less than an email address.
    shared.sort(key=lambda seg: (("@" in seg) + bool(re.search(r"\d{3,}", seg)), len(seg)), reverse=True)
    return shared[:limit]


def _access_final_path(result: dict, fallback: str) -> str:
    try:
        return urllib.parse.urlsplit(str(result.get("final_url") or fallback)).path or "/"
    except ValueError:
        return "/"


def _access_verdict(result: dict, owner: dict | None, url: str) -> tuple[str, float | None]:
    """How one account's response compares with the owner's.

    denied / not_found / rejected / redirected: the account did not get the
    owner's content. same_as_owner: it did. different_content: a successful
    response that is not the owner's. same_status_only: both succeeded with an
    empty body, so only the status could be compared."""
    status = result.get("status")
    if not isinstance(status, int):
        return "no_response", None
    if status == 429 or status >= 500:
        return "inconclusive", None
    if response_state(result) == "authentication_unavailable":
        return "denied", None
    if status in (404, 410):
        return "not_found", None
    if 300 <= status < 400:
        return "redirected", None
    if status >= 400:
        return "rejected", None
    if not owner or not isinstance(owner.get("status"), int):
        return "responded", None
    if _access_final_path(result, url) != _access_final_path(owner, url):
        return "redirected", None
    body = result.get("body") if isinstance(result.get("body"), str) else ""
    owner_body = owner.get("body") if isinstance(owner.get("body"), str) else ""
    similarity = _access_similarity(owner_body, body)
    if status == owner["status"] and not body.strip() and not owner_body.strip():
        return "same_status_only", None
    if status == owner["status"] and similarity >= _ACCESS_SAME:
        return "same_as_owner", similarity
    return "different_content", similarity


def _access_owner_ok(result: dict) -> bool:
    """The owner got a real, successful response: the baseline everything else
    is compared with. A 404 or an error page is a response, but not a baseline."""
    status = result.get("status")
    return (isinstance(status, int) and 200 <= status < 300
            and response_state(result) == "observed")


def _access_row(label: str, result: dict, url: str, receipt_id: str) -> dict:
    row: dict = {"account": label}
    if not isinstance(result.get("status"), int):
        row["error"] = str(result.get("error") or "no response")[:240]
        return row
    body = result.get("body") if isinstance(result.get("body"), str) else ""
    row.update({"status": result["status"], "body_characters": len(body), "body_sha256": digest(body)})
    final_url = str(result.get("final_url") or "")
    if final_url and final_url != str(result.get("url") or url):
        row["final_url"] = final_url[:400]
    preview = _http_body_preview(body, str(result.get("content_type") or ""))
    for key in ("text", "preview", "markup_start"):
        if isinstance(preview.get(key), str):
            preview[key] = preview[key][:_ACCESS_PREVIEW_CHARS]
    preview.pop("omitted_characters", None)
    row["preview"] = preview
    if result.get("potentially_truncated"):
        row["truncated_at_capture"] = True
    if receipt_id:
        row["evidence_id"] = receipt_id
    return row


def tool_rt_access_check(args: dict) -> dict:
    """AUTHORIZED PENTEST. Send one request as each stored test account and once
    signed out, then compare who received the owner's content: the two-account
    replay behind an IDOR / broken-access-control check.

    The accounts come from the Access Control questionnaire; the bridge attaches
    their sign-ins, so the model names accounts and never handles a credential.
    Every response is stored as its own evidence receipt, so a repeated check
    yields the two supporting receipts and the control that record_finding
    needs. The outcome is a comparison, never a confirmed finding."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    identities = _rt_active_identities()
    if not identities:
        return {"error": _rt_identities_missing_reason(), "not_executed": True}
    url = str(args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return {"error": "headers must be a name->value map.", "invalid_arguments": True, "not_executed": True}
    headers = {str(k): str(v) for k, v in headers.items()}
    reserved = _rt_identity_reserved_headers()
    clash = sorted(k for k in headers if k.lower() in reserved)
    if clash or args.get("cookies") or args.get("session") or args.get("identity"):
        return {"error": ("rt_access_check attaches each account's sign-in itself. Remove identity, "
                          "session, cookies and any sign-in header"
                          + (f" ({', '.join(clash)})" if clash else "") + "."),
                "invalid_arguments": True, "not_executed": True}
    body = args.get("body")
    method = str(args.get("method") or ("POST" if body else "GET")).upper()
    safe = method in _ACCESS_SAFE_METHODS
    owner = _rt_identity_label(args.get("owner") or _RT_IDENTITY_LABELS[0])
    if owner not in identities:
        return {"error": f"owner must be one of: {', '.join(identities)}. It names the account the "
                         "page or object belongs to.",
                "invalid_arguments": True, "not_executed": True}
    others = [label for label in _RT_IDENTITY_LABELS if label in identities and label != owner]
    if not safe and args.get("confirm_state_change") is not True:
        return {"error": (f"{method} can change data on the target, and this check sends it once per "
                          f"account ({len(others) + 2} requests). Use it only on test data you created, "
                          "then repeat the call with confirm_state_change=true. GET needs no confirmation."),
                "state_change_unconfirmed": True, "not_executed": True}
    # Reads go owner first: when the owner cannot read the URL the comparison is
    # meaningless, so the remaining requests are not spent. Writes go least
    # privileged first, so the owner's own legitimate write cannot mask the result.
    order = [owner, *others, _RT_IDENTITY_ANON] if safe else [_RT_IDENTITY_ANON, *others, owner]

    call_id = _current_tool_call_id.get() or uuid.uuid4().hex
    try:
        store, cid, mission_id = _evidence_context()
    except ValueError:
        store, cid, mission_id = None, "", ""
    raw: dict[str, dict] = {}
    receipts: dict[str, str] = {}
    stopped = ""
    for label in order:
        request = {"url": url, "method": method, "identity": label}
        if headers:
            request["headers"] = dict(headers)
        if body not in (None, ""):
            request["body"] = body
        result = tool_http_request(request)
        raw[label] = result
        if store is not None and isinstance(result.get("status"), int):
            try:
                receipts[label] = store.record(cid, mission_id, f"{call_id}:{label}",
                                               "rt_access_check", request, dict(result))["id"]
            except (ValueError, OSError, KeyError):
                pass
        if result.get("rate_limited"):
            stopped = "budget"
            break
        if safe and label == owner and not _access_owner_ok(result):
            stopped = "owner"
            break

    owner_result = raw.get(owner) or {}
    owner_ok = _access_owner_ok(owner_result)
    anonymous_result = raw.get(_RT_IDENTITY_ANON) or {}
    anonymous_body = anonymous_result.get("body") if isinstance(anonymous_result.get("body"), str) else ""

    def like_signed_out(result: dict) -> bool:
        """Whether a response is indistinguishable from the signed-out one."""
        if not isinstance(result.get("status"), int) or not isinstance(anonymous_result.get("status"), int):
            return False
        if result["status"] != anonymous_result["status"]:
            return False
        if _access_final_path(result, url) != _access_final_path(anonymous_result, url):
            return False
        text = result.get("body") if isinstance(result.get("body"), str) else ""
        return _access_similarity(text, anonymous_body) >= _ACCESS_SAME

    # An account the target answers differently from a signed-out visitor has a
    # working sign-in. Remembered briefly, so a later refusal that looks exactly
    # like the signed-out one can be read as "refused", not "sign-in expired".
    for label in identities:
        if isinstance((raw.get(label) or {}).get("status"), int) and isinstance(anonymous_result.get("status"), int):
            if not like_signed_out(raw[label]):
                _rt_identity_mark_seen(label)

    rows, verdicts = [], {}
    for label in order:
        if label not in raw:
            rows.append({"account": label, "not_sent": True})
            continue
        row = _access_row(label, raw[label], url, receipts.get(label, ""))
        if label == owner:
            row["role"] = "owner"
            row["verdict"] = "baseline" if owner_ok else "owner_failed"
        else:
            verdict, similarity = _access_verdict(raw[label], owner_result if owner_ok else None, url)
            verdicts[label] = verdict
            row["verdict"] = verdict
            if similarity is not None:
                row["similarity_to_owner"] = similarity
        rows.append(row)

    try:
        names_object = bool(_map_path_id(url) or urllib.parse.urlsplit(url).query or body)
    except ValueError:
        names_object = bool(body)
    leaking = [label for label in others if verdicts.get(label) in ("same_as_owner", "same_status_only")]
    open_to_all = verdicts.get(_RT_IDENTITY_ANON) in ("same_as_owner", "same_status_only")
    refused = ("denied", "not_found", "rejected", "redirected")
    unverified = [label for label in others
                  if verdicts.get(label) in refused and like_signed_out(raw[label])
                  and not _rt_identity_seen(label)]
    proof: list[str] = []
    out: dict = {"ok": True, "url": url, "method": method, "owner": owner}

    if not owner_ok:
        outcome = "owner_could_not_read"
        summary = (f"Account {owner} did not get a normal response from this URL, so there is nothing "
                   "to compare. No conclusion about access control.")
        step = (f"Check the URL, or pass owner='{others[0]}' if it belongs to the other account. If "
                f"account {owner} is refused everywhere, its sign-in has expired: tell the user."
                if others else "Check the URL. If the account is refused everywhere, its sign-in has expired: tell the user.")
    elif stopped == "budget" or any(v in ("no_response", "inconclusive") for v in verdicts.values()) \
            or len(verdicts) < len(others) + 1:
        outcome = "inconclusive"
        summary = ("Not every account's request got a usable response (request budget, server error or "
                   "network failure), so the comparison is incomplete.")
        step = "Repeat the check later. Do not record a finding from this result."
    elif open_to_all:
        outcome = "open_without_sign_in"
        summary = ("A signed-out request received the same content as the owner. This URL is not "
                   "protected by sign-in at all, so it is not an account-to-account leak.")
        step = ("Report it only if this content is meant to be private: record_finding with status "
                "candidate, then continue with the next URL.")
    elif leaking:
        outcome = "candidate" if names_object else "candidate_if_restricted"
        label = leaking[0]
        proof = _access_proof_candidates(
            owner_result.get("body") if isinstance(owner_result.get("body"), str) else "",
            raw[label].get("body") if isinstance(raw[label].get("body"), str) else "",
            anonymous_body)
        if names_object:
            summary = (f"Account {label} received the same content as owner {owner}, and a signed-out "
                       "request did not. That is a candidate, not a confirmed finding.")
        else:
            summary = (f"Account {label} received the same content as owner {owner}, and a signed-out "
                       "request did not. The URL names no specific object, so this may simply be a page "
                       "every signed-in account shares. It is a candidate only if account "
                       f"{label} should not have this page at all (for example an admin-only page).")
        step = ("To confirm: call rt_access_check again with the same arguments, then record_finding "
                f"with status validated, evidence_ids = the account {label} evidence_id from each of "
                "the two runs, control_id = the anonymous evidence_id, and proof = one proof_candidates "
                "string copied exactly."
                + ("" if proof else " No quotable string was found automatically: read the responses "
                   "with security_coverage(evidence_id=...) and pick text that is private to the owner."))
        out["confirm_with"] = {"evidence_id": receipts.get(label, ""),
                               "control_id": receipts.get(_RT_IDENTITY_ANON, "")}
    elif any(verdicts.get(label) == "different_content" for label in others):
        outcome = "inspect"
        label = next(label for label in others if verdicts.get(label) == "different_content")
        summary = (f"Account {label} got a successful response whose content differs from owner "
                   f"{owner}'s. That is normal when each account sees its own data, and a leak only "
                   f"if account {label}'s response contains account {owner}'s private data.")
        step = ("Compare the two previews. If unsure, read both full responses with "
                "security_coverage(evidence_id=...). Otherwise move to the next URL.")
    elif unverified:
        outcome = "sign_in_unverified"
        label = unverified[0]
        summary = (f"Account {label} was refused, but its response is identical to the signed-out one "
                   f"and account {label}'s sign-in has not been seen working yet. This cannot tell "
                   "'correctly refused' from 'sign-in expired or wrong'.")
        step = (f"Run rt_access_check with owner='{label}' on a page account {label} can normally open "
                "(its own profile, dashboard or 'me' endpoint), then repeat this check. If account "
                f"{label} is refused there too, tell the user its sign-in is not working.")
    else:
        outcome = "enforced"
        summary = (f"Only owner {owner} received this content; the other account and the signed-out "
                   "request were refused. The boundary held for this URL.")
        step = "No finding here. Continue with the next URL."

    out.update({"outcome": outcome, "summary": summary, "responses": rows})
    if proof:
        out["proof_candidates"] = proof
    out["signed_in_seen"] = {label: _rt_identity_seen(label) for label in identities}
    out["requests_sent"] = len(raw)
    out["next_step"] = step
    out["note"] = ("Accounts are the sign-ins entered on the engagement form; a label is not proof of "
                   "which user the server saw. Previews are shortened: every full response is stored "
                   "under its evidence_id.")
    return out
