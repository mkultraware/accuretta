# SPDX-License-Identifier: AGPL-3.0-only
"""Spotting destructive red-team requests: delete-like paths, verbs, query
actions and payloads aimed at a target.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request


# Every argument a tool might read a destination from. The guard checks all of
# them, never just the first: a tool that prefers `host` over `target` (or takes
# a list) must not be aimed somewhere the guard did not look.
_RT_TARGET_ARG_KEYS = ("url", "base_url", "target", "host", "domain", "endpoint", "origin",
                       "openapi_url", "graphql_url")


_RT_TARGET_LIST_KEYS = ("targets", "urls", "hosts", "domains", "subdomains")


def _rt_target_values(args: dict) -> list[str]:
    """Every destination string in a tool call: scalar keys and list keys."""
    if not isinstance(args, dict):
        return []
    values: list[str] = []
    for key in _RT_TARGET_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    for key in _RT_TARGET_LIST_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            value = [value]
        if isinstance(value, (list, tuple)):
            values.extend(str(item).strip() for item in value
                          if isinstance(item, (str, int, float)) and str(item).strip())
    # A site-relative path ("/openapi.json") is joined onto the checked base URL.
    return [v for v in values if not (v.startswith("/") and not v.startswith("//"))]


_RT_DESTRUCTIVE_PATH_RE = re.compile(
    r"(?i)(?:^|[/_\-.])(delete|destroy|purge|shutdown|drop|wipe|uninstall|remove|truncate|erase)"
    r"(?:[/_\-.]|$)")


_RT_DESTRUCTIVE_WORDS = {"delete", "destroy", "purge", "shutdown", "drop", "wipe",
                         "uninstall", "remove", "truncate", "erase"}


# Query keys that name an operation: ?action=delete is a delete, ?q=delete is a search.
_RT_ACTION_QUERY_KEYS = {"action", "act", "op", "operation", "cmd", "command", "do", "task",
                         "method", "_method"}


# Raw-socket payloads that wipe or stop a datastore or remove files (Redis,
# memcached, SQL, FTP) and a hand-written HTTP DELETE request line.
_RT_DESTRUCTIVE_PAYLOAD_RE = re.compile(
    r"(?im)^\s*(?:flushall|flushdb|flush_all|shutdown|del|unlink|config\s+set|debug\s+segfault|"
    r"drop|truncate|delete\s+from|dele|rmd|delete\s+\S+\s+http/)\b")


# Tools whose URL arguments are sent to the target as given by the model. Crawlers
# and scanners that discover their own URLs are not listed: a stylesheet called
# drop-down.css must not stop a crawl.
_RT_DESTRUCTIVE_URL_TOOLS = {
    "http_request", "rt_browser", "fuzz", "batch_probe", "sql_injection", "sandbox_sqlmap",
    "recon_injection_probe", "recon_auth_spray", "validate_finding", "observe_response",
    "recon_capture_evidence", "cors_probe", "rt_access_check",
}


_RT_DESTRUCTIVE_HINT = ("Tick 'Allow destructive methods' when starting the engagement if this "
                        "is intended, or show the finding with a request that changes nothing.")


def _rt_destructive_url_reason(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else "//" + url)
    except Exception:
        return None
    if _RT_DESTRUCTIVE_PATH_RE.search(parts.path or ""):
        return f"the path '{parts.path[:120]}' looks destructive"
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _RT_ACTION_QUERY_KEYS and value.strip().lower() in _RT_DESTRUCTIVE_WORDS:
            return f"the query '{key}={value[:40]}' asks for a destructive action"
    return None


def _rt_destructive_block(name: str, args: dict) -> str | None:
    """Refuse data-destroying requests when the engagement panel left 'Allow
    destructive methods' unticked (allow_destructive is False).

    Covers every tool that sends a model-chosen request: the DELETE method and
    its override headers/fields, destructive-looking paths and action
    parameters, and raw-socket commands that wipe or stop a datastore. PUT,
    PATCH and POST stay available because ordinary testing needs them."""
    args = args or {}
    if name in ("http_request", "rt_access_check"):
        method = str(args.get("method") or "").upper()
        if not method:
            method = "POST" if args.get("body") else "GET"
        overrides = [str(v) for k, v in (args.get("headers") or {}).items()
                     if str(k).lower() in ("x-http-method-override", "x-http-method",
                                           "x-method-override")] \
            if isinstance(args.get("headers"), dict) else []
        body = args.get("body")
        if isinstance(body, dict):
            overrides.append(str(body.get("_method") or ""))
        elif isinstance(body, str):
            overrides.extend(re.findall(r"(?i)(?:^|[&\"{,\s])_method\"?\s*[=:]\s*\"?([a-z]+)", body))
        if method == "DELETE" or any(v.strip().upper() == "DELETE" for v in overrides):
            return ("refused: DELETE is disabled for this engagement (destructive methods "
                    "were not allowed on the authorization panel). " + _RT_DESTRUCTIVE_HINT)
    if name == "tcp_send":
        data = args.get("data")
        if data is None:
            data = args.get("payload") or ""
        hit = _RT_DESTRUCTIVE_PAYLOAD_RE.search(str(data))
        if hit:
            return (f"refused: the payload line '{hit.group(0).strip()[:40]}' wipes, stops or "
                    "deletes data and this engagement is limited to non-destructive testing. "
                    + _RT_DESTRUCTIVE_HINT)
    if name in _RT_DESTRUCTIVE_URL_TOOLS:
        for value in _rt_target_values(args):
            reason = _rt_destructive_url_reason(value)
            if reason:
                return (f"refused: {reason} and this engagement is limited to "
                        "non-destructive testing. " + _RT_DESTRUCTIVE_HINT)
    return None
