# SPDX-License-Identifier: AGPL-3.0-only
"""Endpoints found in frontend code: URLs, API paths, WebSocket addresses and fetch
calls, minus static files and noise.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request


# Static assets are not endpoints of interest (unless they carry a query).
_ENDPOINT_STATIC_EXT = (
    ".js", ".mjs", ".cjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp", ".ico", ".bmp", ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav", ".avi", ".mov", ".pdf", ".zip",
)


# wss://host/path  — websocket endpoints
_ENDPOINT_WS_RE = re.compile(
    r"""['"`](wss?://[A-Za-z0-9.\-]+(?::\d+)?(?:/[^'"`\s<>()]*)?)['"`]""", re.IGNORECASE)


# https://host/path  or  //host/path  — fully-qualified / protocol-relative
_ENDPOINT_ABS_RE = re.compile(
    r"""['"`]((?:https?:)?//[A-Za-z0-9.\-]+(?::\d+)?(?:/[^'"`\s<>()]*)?)['"`]""", re.IGNORECASE)


# /rooted/path  (not //, which the absolute rule owns)
_ENDPOINT_PATH_RE = re.compile(
    r"""['"`](/(?!/)[A-Za-z0-9._~\-/]{1,}(?:\?[^'"`\s<>]{0,200})?)['"`]""")


# Explicit client calls — captures the URL even when generic, plus the verb
# (from axios.VERB(...) or from an XHR .open("VERB", url)).
_ENDPOINT_CALL_RE = re.compile(
    r"""(?:fetch|axios(?:\.(get|post|put|patch|delete|head|options))?|\.open)\s*\(\s*"""
    r"""(?:['"]([A-Za-z]+)['"]\s*,\s*)?['"`]([^'"`\s<>]{1,400})['"`]""",
    re.IGNORECASE,
)


def _endpoint_is_noise(value: str) -> bool:
    v = value.strip()
    if len(v) < 2 or len(v) > 400:
        return True
    if any(ch in v for ch in (" ", "\t", "\n", "<", ">", "{", "}", "$", "\\", "^", "|")):
        return True
    if v.count("/") == 0:            # single token, no path — too generic
        return True
    # A static asset is not an endpoint even with a cache-busting query.
    base = v.split("?", 1)[0].split("#", 1)[0].lower()
    if base.endswith(_ENDPOINT_STATIC_EXT):
        return True
    return False


def _classify_endpoint(value: str, target_host: str) -> tuple[str, str]:
    low = value.lower()
    if low.startswith(("ws://", "wss://")):
        return "websocket", "external"
    if low.startswith(("http://", "https://", "//")):
        try:
            netloc = urllib.parse.urlparse(
                value if "://" in value else "https:" + value).netloc
        except Exception:
            netloc = ""
        host = netloc.split("@")[-1].split(":")[0].lower()
        same = bool(host and target_host and host == target_host)
        return "absolute_url", ("same_origin" if same else "external")
    return "path", "same_origin"


def _extract_frontend_endpoints(source_url: str, text: str, target_host: str,
                                seen: set, out: list, cap: int = 2000) -> None:
    """Append de-duplicated endpoints found in `text` to `out` (bounded)."""
    def push(value: str, start: int, verb: str = "") -> None:
        if len(out) >= cap:
            return
        v = value.strip()
        if _endpoint_is_noise(v) or v in seen:
            return
        seen.add(v)
        etype, scope = _classify_endpoint(v, target_host)
        rec = {"value": v[:300], "type": etype, "scope": scope,
               "source": source_url, "line": text.count("\n", 0, start) + 1}
        if verb:
            rec["method"] = verb.upper()
        out.append(rec)

    # Call sites first so the HTTP verb is recorded before a plain match dedups.
    for m in _ENDPOINT_CALL_RE.finditer(text):
        push(m.group(3), m.start(3), m.group(1) or m.group(2) or "")
    for m in _ENDPOINT_WS_RE.finditer(text):
        push(m.group(1), m.start(1))
    for m in _ENDPOINT_ABS_RE.finditer(text):
        push(m.group(1), m.start(1))
    for m in _ENDPOINT_PATH_RE.finditer(text):
        push(m.group(1), m.start(1))
