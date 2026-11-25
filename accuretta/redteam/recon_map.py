# SPDX-License-Identifier: AGPL-3.0-only
"""Attack-surface mapping helpers: OpenAPI specs, request evidence, JWT claims, SQL columns, frontend URLs.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.paths import DATA
from typing import Any


def _map_openapi(spec: dict, base: str) -> list:
    """Turn an OpenAPI 3 / Swagger 2 spec into endpoint descriptors: concrete URL,
    method, query params, whether it has a path param, and body field names."""
    out = []
    if not isinstance(spec, dict):
        return out
    paths = spec.get("paths") or {}
    server = ""
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict):
        server = servers[0].get("url") or ""
    elif spec.get("basePath"):
        server = str(spec.get("basePath"))
    root = urllib.parse.urljoin(base, server) if server else base
    comp = ((spec.get("components") or {}).get("parameters")) or {}

    def deref(p):
        if isinstance(p, dict) and "$ref" in p:
            return comp.get(str(p["$ref"]).split("/")[-1], {})
        return p if isinstance(p, dict) else {}

    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = [deref(p) for p in (item.get("parameters") or [])]
        for method, op in item.items():
            if str(method).lower() not in ("get", "post", "put", "delete", "patch"):
                continue
            op = op if isinstance(op, dict) else {}
            params = shared + [deref(p) for p in (op.get("parameters") or [])]
            qp = [p.get("name") for p in params if p.get("in") == "query" and p.get("name")]
            has_path = any(p.get("in") == "path" for p in params) or ("{" in str(path))
            body = []
            rb = op.get("requestBody")
            if isinstance(rb, dict):
                for media in (rb.get("content") or {}).values():
                    props = ((media or {}).get("schema") or {}).get("properties") or {}
                    body += list(props.keys())
            for p in params:
                if p.get("in") in ("body", "formData"):
                    props = (p.get("schema") or {}).get("properties") or {}
                    body += list(props.keys()) or ([p["name"]] if p.get("name") else [])
            url = root.rstrip("/") + "/" + str(path).lstrip("/")
            if qp:
                url += ("&" if "?" in url else "?") + "&".join(f"{n}=FUZZ" for n in qp)
            out.append({"url": url, "method": str(method).upper(),
                        "query": qp, "path_id": has_path, "body": sorted(set(body))})
    return out


def _http_body_kind(content_type: str, body: str) -> str:
    lowered = (content_type or "").lower()
    head = (body or "")[:4096].lstrip().lower()
    if "\x00" in (body or "")[:4096]:
        return "binary"
    if lowered.startswith(("image/", "audio/", "video/", "font/")) or any(
            marker in lowered for marker in ("octet-stream", "application/pdf", "application/zip")):
        return "binary"
    if "html" in lowered or head.startswith(("<!doctype html", "<html")):
        return "html"
    if "json" in lowered or head.startswith(("{", "[")):
        return "json"
    if "xml" in lowered or head.startswith("<?xml"):
        return "xml"
    return "text"


def _save_http_request_evidence(label, method, url, req_headers, req_body,
                                status, resp_headers, resp_body) -> dict | None:
    """Persist a full request+response transcript as a proof artifact under
    data/recon_evidence/ with a sha256. Unlike recon_capture_evidence (a bare
    GET), this captures the ACTUAL exploit request - forged cookies, POST body,
    injected payload - so a breach is verifiable and the report can cite it."""
    import hashlib
    evidence_dir = DATA / "recon_evidence"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(label or "http_evidence"))[:60]
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", urllib.parse.urlparse(url).netloc or "target")
    path = evidence_dir / f"{ts}_{safe_label}_{host}.txt"
    lines = ["### REQUEST", f"{method} {url}"]
    for k, v in (req_headers or {}).items():
        lines.append(f"{k}: {v}")
    if req_body:
        lines.append("")
        lines.append(req_body if isinstance(req_body, str) else req_body.decode("utf-8", "replace"))
    lines += ["", "### RESPONSE", f"HTTP {status}"]
    for k, v in (resp_headers or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", resp_body or ""]
    transcript = "\n".join(lines)
    try:
        path.write_text(transcript, encoding="utf-8")
    except Exception:
        return None
    return {"evidence_saved": str(path),
            "evidence_sha256": hashlib.sha256(transcript.encode("utf-8", "replace")).hexdigest()}


def _access_segments(body: str) -> list[str]:
    """Quotable pieces of a response: JSON leaf values, or text between tags."""
    text = (body or "")[:200000]
    found: list[str] = []
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if isinstance(parsed, (dict, list)):
        stack = [parsed]
        while stack and len(found) < 600:
            item = stack.pop()
            if isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, str):
                if 4 <= len(item.strip()) <= 200:
                    found.append(item.strip())
            elif isinstance(item, (int, float)) and not isinstance(item, bool) and len(str(item)) >= 4:
                found.append(str(item))
    else:
        for chunk in re.split(r"<[^>]*>|[\r\n]+", text):
            chunk = chunk.strip()
            if 6 <= len(chunk) <= 200:
                found.append(chunk)
                if len(found) >= 600:
                    break
    return list(dict.fromkeys(found))


def _split_sql_select_list(text: str) -> list[str]:
    """Split "a, group_concat(b, ','), 'x,y'" on top-level commas only."""
    parts, depth, quote, cur = [], 0, "", []
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _decode_jwt_claims(token: str) -> tuple[dict | None, str]:
    try:
        head_raw, payload_raw, _sig = token.split(".", 2)

        def decode(part: str) -> Any:
            raw = _b64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
            return json.loads(raw.decode("utf-8"))

        header, payload = decode(head_raw), decode(payload_raw)
        if not isinstance(header, dict) or not isinstance(payload, dict):
            return None, "JWT header or payload is not an object"
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            return payload, "expired JWT"
        return payload, "valid JWT structure"
    except Exception:
        return None, "invalid JWT structure"


def _frontend_resource_urls(base_url: str, text: str, depth: int) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    patterns = (
        ("javascript", re.compile(r"<script[^>]+src=['\"]([^'\"]+)['\"]", re.IGNORECASE)),
        ("javascript", re.compile(r"<link[^>]+href=['\"]([^'\"]+\.(?:m?js)(?:\?[^'\"]*)?)['\"]", re.IGNORECASE)),
        ("source_map", re.compile(r"(?://[#@]\s*sourceMappingURL=|/\*[#@]\s*sourceMappingURL=)([^\s*]+)", re.IGNORECASE)),
        ("javascript", re.compile(r"['\"]([^'\"]+\.(?:m?js)(?:\?[^'\"]*)?)['\"]", re.IGNORECASE)),
        ("source_map", re.compile(r"['\"]([^'\"]+\.map(?:\?[^'\"]*)?)['\"]", re.IGNORECASE)),
    )
    for kind, regex in patterns:
        for match in regex.finditer(text):
            raw = match.group(1).strip().rstrip("*/")
            if raw.startswith(("data:", "blob:", "javascript:", "#")):
                continue
            absolute = urllib.parse.urljoin(base_url, raw)
            parsed = urllib.parse.urlsplit(absolute)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            absolute = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                                parsed.query, ""))
            found.append((absolute, kind, depth + 1))
    return list(dict.fromkeys(found))
