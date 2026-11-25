# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team findings and evidence: reviewing and listing findings, paging stored
evidence, and the coverage report.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import json
import urllib.error
import urllib.parse
import urllib.request
import hashlib
import re
import time
import uuid
from accuretta.redteam.http_summary import _HTTP_SUMMARY_COOKIE_CHARS
from accuretta.redteam.http_summary import _HTTP_SUMMARY_COOKIE_COUNT
from accuretta.redteam.http_summary import _clip_headers
from accuretta.redteam.http_summary import _clip_text
from accuretta.redteam.http_summary import _http_body_preview
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import CHATS_FILE
from accuretta.core.paths import DATA
from accuretta.core.persistence import save_json
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from agent_reliability import EvidenceStore
from agent_reliability import review_evidence
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.workspace import normalize_path
from accuretta.security.path_guards import is_in_workspace
from cvss_v31 import base_score as cvss_base_score
from cvss_v31 import normalize_cwe
from pathlib import Path


_FINDING_STATUSES = {"candidate", "validated", "reproduced", "mitigated", "false_positive"}


def _evidence_context():
    cid = _current_chat_id.get() or _get_current_chat()
    mission = _rt_mission_get(_rt_context_chat() or {})
    if not cid or not mission or mission.get("authorized") is not True or not mission.get("authorization_id"):
        raise ValueError("An authorized engagement record is required for evidence review.")
    return EvidenceStore(DATA / "recon_evidence" / "receipts"), cid, str(mission["authorization_id"])


_CHALLENGE_TEXT_FIELDS = ("expected_boundary", "demonstrated_impact", "alternative_explanation",
                          "disproof_check", "limitations")


def _finding_expand_shorthand(args):
    """Build the nested review object from flat fields and the stored receipts.

    Small models handle a handful of flat strings far better than a nested
    object, and the bridge already holds what the object repeats: the receipts
    record the endpoint, tool, method and parameters. The model supplies the
    receipt ids, the control id, the proof text and its reasoning; everything
    mechanical is read from the receipts. Every review rule still runs on the
    result, and an explicit `challenge` value is never overwritten."""
    if not isinstance(args, dict):
        return args
    ids = args.get("evidence_ids")
    flat = ("control_id", "proof", "parameter", *_CHALLENGE_TEXT_FIELDS)
    if not any(args.get(key) for key in flat) and (args.get("target") or not ids):
        return args
    out = dict(args)
    if not out.get("description") and out.get("title"):
        # The claim under review defaults to the title rather than failing the call.
        out["description"] = out["title"]
    challenge = dict(args["challenge"]) if isinstance(args.get("challenge"), dict) else {}
    for field in _CHALLENGE_TEXT_FIELDS:
        if isinstance(args.get(field), str) and not challenge.get(field):
            challenge[field] = args[field]
    control = dict(challenge["control"]) if isinstance(challenge.get("control"), dict) else {}
    if args.get("control_id") and not control.get("evidence_id"):
        control["evidence_id"] = str(args["control_id"]).strip()
    if control:
        challenge["control"] = control
    first = None
    try:
        store, cid, mission_id = _evidence_context()
        if isinstance(ids, list) and ids:
            first = store.read(cid, mission_id, ids[0])
    except (ValueError, OSError, KeyError, TypeError):
        first = None
    # rt_access_check sends the control itself: the same request, signed out or
    # as the other account. When that control was refused, the refusal is the
    # expected result, so fill in what the review needs to accept a denial.
    if control.get("evidence_id") and "expected_status" not in control:
        try:
            held = store.read(cid, mission_id, control["evidence_id"])
        except (NameError, ValueError, OSError, KeyError, TypeError):
            held = None
        if (held and held.get("tool") == "rt_access_check"
                and held.get("state") == "authentication_unavailable"):
            control["expected_status"] = (held.get("result") or {}).get("status")
            control.setdefault("purpose", "The same request sent by rt_access_check without the "
                                          "owner's sign-in; a refusal is the expected result.")
            challenge["control"] = control
    if first:
        if not out.get("target"):
            out["target"] = str(first.get("target") or "")
        if not out.get("source_tool"):
            out["source_tool"] = str(first.get("tool") or "")
        if not out.get("evidence") and isinstance(args.get("proof"), str):
            out["evidence"] = args["proof"]
        test = dict(challenge["test"]) if isinstance(challenge.get("test"), dict) else {}
        context = first.get("request_context") or {}
        test.setdefault("method", context.get("method"))
        parameter = args.get("parameter") if isinstance(args.get("parameter"), str) else test.get("parameter")
        parameter = (parameter or "").strip()
        if "location" not in test:
            recorded = context.get("parameters") or {}
            found = next((where for where in ("query", "body", "header", "interaction")
                          if parameter and parameter in (recorded.get(where) or [])), None)
            if parameter and found is None and first.get("tool") == "rt_access_check":
                # An access check replays one request unchanged and varies only the
                # account. A model naming the object id in the path ("id") is
                # describing the URL, not an input the test changed.
                parameter = ""
            test["location"] = (found or "query") if parameter else "none"
            test["parameter"] = parameter
        test.setdefault("parameter", parameter)
        if isinstance(args.get("proof"), str) and not isinstance(test.get("assertion"), dict):
            result = first.get("result") if isinstance(first.get("result"), dict) else {}
            field = next((key for key in ("body", "text", "snippet")
                          if isinstance(result.get(key), str)), "body")
            test["assertion"] = {"path": [field], "operation": "contains", "value": args["proof"]}
        challenge["test"] = test
    if challenge:
        out["challenge"] = challenge
    return out


def _review_finding_args(args):
    args = _finding_expand_shorthand(args)
    store, cid, mission_id = _evidence_context()
    ids = args.get("evidence_ids") or []
    if not isinstance(ids, list) or not 2 <= len(ids) <= 8:
        return {"eligible": False, "issues": ["Supply 2-8 supporting evidence IDs, including a separate reproduction."]}
    records = [store.read(cid, mission_id, value) for value in ids]
    challenge = args.get("challenge") or {}
    if not isinstance(challenge, dict):
        raise ValueError("challenge must be an object")
    claim = {key: args.get(key) for key in ("title", "target", "description", "severity")}
    control = challenge.get("control") or {}
    control_record = (store.read(cid, mission_id, control["evidence_id"])
                      if isinstance(control, dict) and control.get("evidence_id") else None)
    review = review_evidence(claim, records, challenge, control_record)
    if isinstance(control, dict) and control.get("evidence_id"):
        record = control_record
        quote = control.get("quote")
        raw = json.dumps(record["result"], ensure_ascii=False, sort_keys=True, default=str)
        expected_denial = (record["state"] == "authentication_unavailable"
                           and control.get("expected_status") == record["result"].get("status")
                           and isinstance(control.get("purpose"), str) and len(control["purpose"].strip()) >= 12)
        if (record["id"] in ids or (record["state"] != "observed" and not expected_denial)
                or (quote and (not isinstance(quote, str) or quote not in raw))):
            review["issues"].append("The control must be a separate conclusive result; an optional quote must match exactly.")
        target_origin = urllib.parse.urlsplit(str(args.get("target") or "")).netloc
        if target_origin and urllib.parse.urlsplit(record["target"]).netloc != target_origin:
            review["issues"].append("The control must belong to the same target origin.")
        review["control_id"] = record["id"]
    review["eligible"] = not review["issues"]
    return review


def tool_review_finding(args):
    try:
        return _review_finding_args(args)
    except (ValueError, OSError, KeyError) as exc:
        return {"eligible": False, "issues": [str(exc)]}


_EVIDENCE_PAGE_CHARS = 12000


def _evidence_page(result, offset: int, page_size: int = _EVIDENCE_PAGE_CHARS) -> dict:
    """Render one readable page of a stored tool result.

    HTTP bodies and browser snapshots are paged as their original plain text;
    other results are pretty-printed before paging. Offsets always refer to the
    text this view returns."""
    if not isinstance(result, dict):
        raw = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        chunk = raw[offset:offset + page_size]
        return {"read_mode": "json",
                "content": {"field": "result", "offset": offset,
                            "total_characters": len(raw), "returned_characters": len(chunk),
                            "text": chunk,
                            "next_offset": offset + len(chunk) if offset + len(chunk) < len(raw) else None},
                "note": "Pretty-printed stored result; offset counts characters of this view."}
    content_field = next((key for key in ("body", "text") if isinstance(result.get(key), str)), None)
    if content_field:
        content = result[content_field]
        chunk = content[offset:offset + page_size]
        page = {
            "read_mode": "response" if content_field == "body" else "text",
            "content": {
                "field": content_field,
                "offset": offset,
                "total_characters": len(content),
                "returned_characters": len(chunk),
                "text": chunk,
                "next_offset": offset + len(chunk) if offset + len(chunk) < len(content) else None,
            },
        }
        if content_field == "body":
            raw_headers = result.get("headers") if isinstance(result.get("headers"), dict) else {}
            content_type = str(result.get("content_type")
                               or raw_headers.get("Content-Type")
                               or raw_headers.get("content-type") or "")
            for key in ("ok", "status", "method", "url", "final_url", "content_type",
                        "elapsed_ms", "potentially_truncated", "note"):
                if key in result:
                    page[key] = result[key]
            if offset == 0:
                headers, omitted_headers = _clip_headers(raw_headers)
                page["headers"] = headers
                if omitted_headers:
                    page["headers_omitted"] = omitted_headers
                cookies = result.get("set_cookie") if isinstance(result.get("set_cookie"), list) else []
                page["set_cookie"] = [_clip_text(value, _HTTP_SUMMARY_COOKIE_CHARS)
                                      for value in cookies[:_HTTP_SUMMARY_COOKIE_COUNT]]
                if len(cookies) > _HTTP_SUMMARY_COOKIE_COUNT:
                    page["set_cookie_omitted"] = len(cookies) - _HTTP_SUMMARY_COOKIE_COUNT
                page["body_preview"] = _http_body_preview(content, content_type)
            else:
                page["headers_note"] = "Headers and the body preview are returned on offset=0."
        else:
            for key in ("ok", "status", "action", "url", "final_url", "title", "note"):
                if key in result:
                    page[key] = result[key]
        return page
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    chunk = raw[offset:offset + page_size]
    return {"read_mode": "json",
            "content": {"field": "result", "offset": offset,
                        "total_characters": len(raw), "returned_characters": len(chunk),
                        "text": chunk,
                        "next_offset": offset + len(chunk) if offset + len(chunk) < len(raw) else None},
            "note": "Pretty-printed stored result; offset counts characters of this view."}


def _endpoint_key(url: str) -> tuple[str, str, str]:
    """(scheme, host[:port], path) of a URL, written one way: case-insensitive
    scheme and host, no default port, no trailing slash. `HTTPS://Shop.test:443/api/`
    and `https://shop.test/api` are the same endpoint."""
    parts = urllib.parse.urlsplit(str(url or "").strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return scheme, host, parts.path.rstrip("/") or "/"


def tool_security_coverage(args):
    store, cid, mission_id = _evidence_context()
    if args.get("evidence_id"):
        record = store.read(cid, mission_id, args["evidence_id"])
        offset = max(0, int(args.get("offset") or 0))
        page = _evidence_page(record["result"], offset)
        return {**store.summary(record), **page, "next_offset": page["content"]["next_offset"]}
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid, {})
    plans = list(chat.get("security_coverage") or [])
    if args.get("feature"):
        feature, identity = str(args["feature"])[:240], str(args.get("identity") or "unspecified")[:120]
        target = str(args.get("target") or "")[:2000]
        ids = args.get("evidence_ids") or []
        if not isinstance(ids, list) or len(ids) > 8:
            raise ValueError("evidence_ids must contain at most 8 IDs")
        observations = [store.read(cid, mission_id, value) for value in ids]
        if any(_endpoint_key(r["target"]) != _endpoint_key(target) for r in observations):
            raise ValueError("Coverage evidence must match the target endpoint (scheme, host "
                             "and path; the query string may differ)")
        state = "untested" if not observations else "observed"
        if any(r["state"] != "observed" for r in observations):
            state = next(r["state"] for r in observations if r["state"] != "observed")
        plans = [p for p in plans if (p.get("mission_id"), p.get("feature"), p.get("identity"), p.get("target")) != (mission_id, feature, identity, target)]
        plans.append({"mission_id": mission_id, "feature": feature, "identity": identity, "target": target,
                      "state": state, "evidence_ids": ids, "limitations": str(args.get("limitations") or "")[:1000]})
        chat["security_coverage"] = plans
        save_json(CHATS_FILE, chats)
    offset = max(0, int(args.get("offset") or 0))
    current = [p for p in plans if p.get("mission_id") == mission_id]
    return {**store.coverage(cid, mission_id, offset=offset), "planned_checks": current[offset:offset + 50],
            "planned_total": len(current), "planned_next_offset": offset + 50 if offset + 50 < len(current) else None}


def tool_list_findings(args: dict) -> dict:
    cid = _current_chat_id.get() or _get_current_chat()
    if not cid:
        return {"error": "no active chat"}
    chat = (get_chats().get("chats", {}) or {}).get(cid) or {}
    findings = [copy.deepcopy(x) for x in (chat.get("findings") or []) if isinstance(x, dict)]
    for finding in findings:
        if finding.get("status") in {"validated", "reproduced"}:
            checked = tool_review_finding({**finding, "challenge": (finding.get("review") or {}).get("challenge") or {}})
            if not checked.get("eligible"):
                finding.update(status="candidate", source_observed=False, review=checked)
    status = str(args.get("status") or "").lower()
    if status:
        findings = [x for x in findings if str(x.get("status") or "").lower() == status]
    return {"count": len(findings), "findings": findings,
            "states": sorted(_FINDING_STATUSES)}


_FINDING_SEVERITIES = {"info", "low", "medium", "high", "critical"}


def _finding_artifact(path_text: str) -> dict | None:
    if not path_text:
        return None
    try:
        path = Path(normalize_path(path_text)).resolve()
        evidence_root = (DATA / "recon_evidence").resolve()
        allowed = is_in_workspace(str(path)) or path == evidence_root or evidence_root in path.parents
        if not allowed or not path.is_file():
            return None
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": str(path), "bytes": size, "sha256": digest.hexdigest()}
    except Exception:
        return None


def tool_record_finding(args: dict) -> dict:
    """Create/update a structured finding backed by harness-observed evidence."""
    cid = _current_chat_id.get() or _get_current_chat()
    if not cid:
        return {"error": "no active chat"}
    title = re.sub(r"\s+", " ", str(args.get("title") or "")).strip()[:180]
    if not title:
        return {"error": "title required"}
    args = _finding_expand_shorthand(args)
    if not args.get("description"):
        # The claim under review defaults to the title rather than failing the call.
        args = {**args, "description": title}
    requested = str(args.get("status") or "candidate").lower()
    status = requested if requested in _FINDING_STATUSES else "candidate"
    severity = str(args.get("severity") or "info").lower()
    severity = severity if severity in _FINDING_SEVERITIES else "info"
    source_tool = _resolve_tool_name(str(args.get("source_tool") or ""))
    mission_id = str((_rt_mission_get(_rt_context_chat() or {}) or {}).get("authorization_id") or "")
    artifact = _finding_artifact(str(args.get("artifact_path") or ""))
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid)
    if not isinstance(chat, dict):
        return {"error": "active chat not found"}
    downgraded = ""
    review = None
    if status in ("validated", "reproduced"):
        review = tool_review_finding(args)
        if not review.get("eligible"):
            downgraded = "Claim remains a candidate: " + "; ".join(review.get("issues") or [])
            status = "candidate"
    source_ok = bool(review and review.get("eligible"))
    finding_id = str(args.get("finding_id") or "").strip()
    findings = [x for x in (chat.get("findings") or []) if isinstance(x, dict)]
    existing = None
    if finding_id:
        existing = next((x for x in findings if x.get("id") == finding_id and x.get("mission_id", "") == mission_id), None)
        if existing is None:
            return {"error": "Finding ID does not belong to this engagement.", "invalid_arguments": True}
    if existing is None:
        target = str(args.get("target") or "").strip()[:240]
        existing = next((x for x in findings
                         if str(x.get("title") or "").lower() == title.lower()
                         and x.get("mission_id", "") == mission_id
                         and str(x.get("target") or "") == target), None)
    now = int(time.time())
    is_new = existing is None
    row = existing if existing is not None else {
        "id": f"F-{now}-{uuid.uuid4().hex[:6]}", "created": now,
    }
    row.update({
        "title": title,
        "severity": severity,
        "status": status,
        "confidence": max(0, min(100, int(args.get("confidence") or 0))),
        "target": str(args.get("target") or row.get("target") or "")[:240],
        "description": str(args.get("description") or row.get("description") or "")[:2000],
        "inferred_impact": str(args.get("inferred_impact") or "")[:2000],
        "evidence": str(args.get("evidence") or row.get("evidence") or "")[:3000],
        "source_tool": source_tool,
        "source_observed": bool(source_ok),
        "evidence_ids": args.get("evidence_ids") or [],
        "review": review,
        "mission_id": mission_id,
        "updated": now,
    })
    if artifact:
        row["artifact"] = artifact
    # CVSS and CWE are validated and computed server-side. A local model can
    # judge qualitative impact, but a wrong vector or invented CWE name must
    # never reach a platform or client report; invalid input is rejected with
    # a repairable error while the rest of the finding is still recorded.
    if args.get("cvss_vector"):
        scored = cvss_base_score(str(args.get("cvss_vector")))
        if scored:
            row["cvss"] = scored
            row.pop("cvss_error", None)
        else:
            row["cvss_error"] = (
                "Unrecognized CVSS v3.1 base vector. Expected all eight base "
                "metrics, e.g. CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H.")
    if args.get("cwe"):
        cwe = normalize_cwe(args.get("cwe"))
        if cwe:
            row["cwe"] = cwe
            row.pop("cwe_error", None)
        else:
            row["cwe_error"] = "CWE must look like CWE-79 or 79."
    if is_new:
        findings.append(row)
    chat["findings"] = findings[-200:]
    save_json(CHATS_FILE, chats)
    return {"ok": True, "finding": copy.deepcopy(row), "downgraded": downgraded,
            "total_findings": len(chat["findings"]),
            "note": "Finding stored in this session's evidence ledger."}
