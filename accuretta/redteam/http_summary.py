# SPDX-License-Identifier: AGPL-3.0-only
"""Short, bounded views of HTTP responses: clipped text and headers, a body
preview, and the summary a tool returns.

Moved out of accuretta/agent/tool_output.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.redteam.recon_map import _http_body_kind
from agent_reliability import digest


_HTTP_SUMMARY_MIN_BODY = 1200


_HTTP_SUMMARY_TEXT_CHARS = 900


_HTTP_SUMMARY_HEADER_CHARS = 200


_HTTP_SUMMARY_HEADER_COUNT = 30


_HTTP_SUMMARY_COOKIE_CHARS = 400


_HTTP_SUMMARY_COOKIE_COUNT = 10


_HTTP_NOTABLE_HEADERS = {
    "content-type", "content-length", "location", "www-authenticate",
    "access-control-allow-origin", "access-control-allow-credentials",
    "content-security-policy", "x-frame-options", "strict-transport-security",
    "server", "x-powered-by", "cache-control", "age", "vary", "x-cache",
}


def _clip_text(value, limit: int) -> str:
    text = str(value if value is not None else "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} chars omitted]"


def _clip_headers(headers) -> tuple[dict, int]:
    if not isinstance(headers, dict):
        return {}, 0
    ordered = sorted(headers.items(),
                     key=lambda item: 0 if str(item[0]).lower() in _HTTP_NOTABLE_HEADERS else 1)
    shown = {}
    for key, value in ordered:
        if len(shown) >= _HTTP_SUMMARY_HEADER_COUNT:
            break
        shown[str(key)] = _clip_text(value, _HTTP_SUMMARY_HEADER_CHARS)
    return shown, max(0, len(headers) - len(shown))


def _html_visible_text(body: str) -> str:
    sample = (body or "")[:200000]
    sample = re.sub(r"(?is)<(script|style|noscript|template)\b.*?</\1\s*>", " ", sample)
    sample = re.sub(r"(?is)<!--.*?-->", " ", sample)
    sample = re.sub(r"(?s)<[^>]*>", " ", sample)
    return re.sub(r"\s+", " ", sample).strip()


def _http_body_preview(body: str, content_type: str) -> dict:
    kind = _http_body_kind(content_type, body)
    if kind == "binary":
        return {"kind": "binary",
                "note": "Binary body not shown; exact bytes are in the evidence receipt.",
                "characters": len(body or "")}
    if kind == "html":
        match = re.search(r"(?is)<title[^>]*>(.*?)</title>", (body or "")[:100000])
        title = re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]*>", " ", match.group(1))).strip()[:200] if match else ""
        visible = _html_visible_text(body)
        preview = {"kind": "html", "title": title,
                   "text": visible[:_HTTP_SUMMARY_TEXT_CHARS],
                   "markup_start": re.sub(r"\s+", " ", (body or "")[:300]).strip()}
        if len(visible) > _HTTP_SUMMARY_TEXT_CHARS:
            preview["omitted_characters"] = len(visible) - _HTTP_SUMMARY_TEXT_CHARS
        if not visible:
            preview["note"] = "No visible text found; showing the raw markup start."
        return preview
    if kind == "json":
        try:
            parsed = json.loads(body or "")
        except (ValueError, TypeError):
            parsed = None
        preview = {"kind": "json", "preview": (body or "")[:_HTTP_SUMMARY_TEXT_CHARS]}
        if isinstance(parsed, dict):
            preview["top_level"] = "object"
            preview["keys"] = [str(key) for key in list(parsed)[:30]]
        elif isinstance(parsed, list):
            preview["top_level"] = "array"
            preview["items"] = len(parsed)
            if parsed and isinstance(parsed[0], dict):
                preview["item_keys"] = [str(key) for key in list(parsed[0])[:30]]
        return preview
    preview = {"kind": kind, "text": (body or "")[:_HTTP_SUMMARY_TEXT_CHARS]}
    if len(body or "") > _HTTP_SUMMARY_TEXT_CHARS:
        preview["omitted_characters"] = len(body) - _HTTP_SUMMARY_TEXT_CHARS
    return preview


def _http_summary(result: dict) -> dict:
    """Model-facing HTTP result: status, metadata, headers and a body preview.

    The full response stays in the evidence receipt; this view exists so a large
    HTML/JSON response cannot crowd out the rest of the turn. Callers fall back
    to the original result when no receipt was recorded.
    """
    body = result.get("body")
    if not isinstance(body, str):
        body = "" if body is None else json.dumps(body, ensure_ascii=False, default=str)
    raw_headers = result.get("headers") if isinstance(result.get("headers"), dict) else {}
    headers, omitted_headers = _clip_headers(raw_headers)
    content_type = str(result.get("content_type") or raw_headers.get("Content-Type")
                       or raw_headers.get("content-type") or "")
    cookies = result.get("set_cookie") if isinstance(result.get("set_cookie"), list) else []
    shown_cookies = [_clip_text(cookie, _HTTP_SUMMARY_COOKIE_CHARS)
                     for cookie in cookies[:_HTTP_SUMMARY_COOKIE_COUNT]]
    summary = {key: value for key, value in result.items()
               if key not in {"body", "headers", "set_cookie"}}
    summary["content_type"] = content_type
    summary["headers"] = headers
    summary["set_cookie"] = shown_cookies
    if omitted_headers:
        summary["headers_omitted"] = omitted_headers
    if len(cookies) > len(shown_cookies):
        summary["set_cookie_omitted"] = len(cookies) - len(shown_cookies)
    summary["body"] = {
        "characters": len(body),
        "bytes": len(body.encode("utf-8", "replace")),
        "sha256": digest(body),
        "truncated_at_capture": bool(result.get("potentially_truncated")),
        "preview": _http_body_preview(body, content_type),
    }
    flags = list(dict.fromkeys(re.findall(r"FLAG\{[^}]+\}", body)))[:5]
    if flags:
        summary["body"]["flags"] = flags
    receipt = result.get("evidence_receipt")
    if isinstance(receipt, dict) and receipt.get("id"):
        summary["full_response"] = {
            "evidence_id": receipt["id"],
            "read_with": f"security_coverage(evidence_id='{receipt['id']}', offset=0)",
            "note": ("The complete body and headers are in the receipt; the preview is not the "
                     "whole response. Follow next_offset to page."),
        }
    return summary


def _http_model_view(result: dict) -> dict:
    receipt = result.get("evidence_receipt")
    body = result.get("body")
    if (isinstance(body, str) and len(body) > _HTTP_SUMMARY_MIN_BODY
            and isinstance(receipt, dict) and receipt.get("id")):
        return _http_summary(result)
    return result
