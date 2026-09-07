"""Evidence provenance and factual guidance for the agent loop."""

import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid
from urllib.parse import urlsplit


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def feedback(name, result):
    if not isinstance(result, dict):
        return result
    failed = (result.get("error") or result.get("ok") is False or result.get("not_executed")
              or result.get("exit_code") not in (None, 0))
    if not failed:
        return result
    result = dict(result)
    message = str(result.get("error") or result.get("reason") or "").lower()
    if result.get("scope_blocked") or result.get("wrong_execution_target"):
        kind, step = "scope_blocked", "Check the selected target and authorized scope. Do not retry through another tool."
    elif "denied" in message or result.get("reason") == "denied":
        kind, step = "permission_denied", "The action was denied. Continue with permitted work or report the blocked step."
    elif result.get("stale") or "stale" in message or "read_file on it" in message:
        kind, step = "stale_content", "Read the current file, then prepare a targeted edit against those contents."
    elif result.get("missing_arguments") or result.get("invalid_arguments"):
        kind, step = "invalid_arguments", "Correct the listed arguments using the tool schema before retrying."
    elif result.get("loop_breaker"):
        kind, step = "repeat_blocked", "Review the recorded results and choose a different check. This limit does not establish success."
    elif any(word in message for word in ("not installed", "not found on path", "no module named", "missing dependency")):
        kind, step = "missing_dependency", "Use capability_report to check available tools and runtimes before choosing an alternative."
    elif name in {"run_tests", "check_syntax"} and not result.get("not_executed"):
        kind, step = "check_failed", "Inspect the failing output, distinguish an existing failure from a regression, and rerun the relevant check after a fix."
    elif result.get("status") in (401, 403):
        kind, step = "access_response", "Access was denied by the server. Check the intended identity; this response alone proves neither a vulnerability nor its absence."
    elif result.get("status"):
        kind, step = "http_response", "Compare the recorded response with a baseline and control. An HTTP status alone does not validate a finding."
    elif "timeout" in message or "timed out" in message:
        kind, step = "timeout", "Check whether the action is still running and inspect available output before retrying."
    else:
        kind, step = "action_failed", "Inspect the original error and current state. Partial effects may remain; do not assume success or rollback."
    result["failure_guidance"] = {"category": kind, "next_step": step}
    return result


def verification_advice(path="", kind="change"):
    if kind == "bug":
        return "Choose a check that reproduces the reported problem before editing; rerun that same check after the fix. Record pre-existing failures separately."
    if kind == "startup":
        return "Inspect the resulting startup registration and executable path. Process launch and actual sign-in behavior are separate checks; report which was observed."
    if kind == "ui" or Path(path).suffix.lower() in {".html", ".css", ".jsx", ".tsx", ".vue", ".svelte"}:
        return "Exercise the affected UI interaction at the relevant size. For a text or color-only change, a focused visual check is enough; parsing alone does not verify interaction."
    return "Choose the smallest check that demonstrates the requested behavior. Use syntax checks for parsing, relevant tests for behavior, and report anything not exercised."


def response_state(result):
    if result.get("scope_blocked") or result.get("not_executed"):
        return "blocked"
    if result.get("error") or result.get("potentially_truncated") or result.get("exit_code") not in (None, 0):
        return "inconclusive"
    headers = {str(k).lower(): str(v) for k, v in (result.get("headers") or {}).items()}
    destination = headers.get("location", "") or str(result.get("final_url") or "")
    if result.get("status") in (401, 403) or re.search(r"/(login|signin|sign-in)(?:[/?#]|$)", destination, re.I):
        return "authentication_unavailable"
    if result.get("status") in (429, 503):
        return "inconclusive"
    if result.get("ok") is False and not result.get("status"):
        return "inconclusive"
    return "observed"


class EvidenceStore:
    """Per-engagement append-only observations; summaries never replace raw records."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def folder(self, chat_id, mission_id):
        return self.root / digest([chat_id, mission_id])

    def record(self, chat_id, mission_id, call_id, tool, args, result):
        receipt_id = uuid.uuid4().hex
        target = str(args.get("url") or args.get("target") or args.get("path") or args.get("host") or args.get("domain") or result.get("url") or "")
        headers = args.get("headers") or {}
        credentials = {str(k).lower(): v for k, v in headers.items()
                       if str(k).lower() in {"authorization", "cookie"}}
        credentials["cookies"] = args.get("cookies") or {}
        record = {
            "id": receipt_id, "chat_id": chat_id, "mission_id": mission_id,
            "call_id": call_id or uuid.uuid4().hex, "tool": tool, "target": target,
            "identity": str(args.get("session") or ("explicit credentials" if any(credentials.values()) else "anonymous")),
            "identity_fingerprint": digest([args.get("session"), credentials]),
            "request_sha256": digest(args), "method": str(args.get("method") or "GET").upper(),
            "created": time.time(), "state": response_state(result),
            "result": result, "result_sha256": digest(result),
        }
        with self.lock:
            folder = self.folder(chat_id, mission_id)
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / (receipt_id + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, default=str)
        return self.summary(record)

    @staticmethod
    def summary(record):
        return {k: v for k, v in record.items() if k != "result"}

    def read(self, chat_id, mission_id, receipt_id):
        if not isinstance(receipt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
            raise ValueError("Invalid evidence ID")
        record = json.loads((self.folder(chat_id, mission_id) / (receipt_id + ".json")).read_text(encoding="utf-8"))
        if (record["chat_id"] != chat_id or record["mission_id"] != mission_id
                or record["id"] != receipt_id or digest(record["result"]) != record["result_sha256"]):
            raise ValueError("Evidence integrity check failed")
        return record

    def coverage(self, chat_id, mission_id, limit=50, offset=0):
        folder = self.folder(chat_id, mission_id)
        paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        rows = []
        for path in paths[offset:offset + limit]:
            try:
                rows.append(self.summary(self.read(chat_id, mission_id, path.stem)))
            except (OSError, ValueError, KeyError):
                rows.append({"id": path.stem, "state": "evidence_unavailable"})
        return {"observations": rows, "total": len(paths), "next_offset": offset + len(rows) if offset + len(rows) < len(paths) else None,
                "note": "Observed requests are not vulnerability coverage. Untested features remain untested; identity labels are not verified account roles."}


def compare_responses(records):
    if len(records) != 3 or len({r["id"] for r in records}) != 3:
        raise ValueError("Provide three distinct evidence IDs: baseline, test, and control")
    for record in records:
        if "status" not in record["result"]:
            raise ValueError("Comparison requires recorded HTTP responses")
    base, test, control = records
    rows = []
    for role, record in zip(("baseline", "test", "control"), records):
        result = record["result"]
        body = str(result.get("body") or result.get("snippet") or "")
        headers = {str(k).lower(): str(v) for k, v in (result.get("headers") or {}).items()}
        rows.append({"role": role, **EvidenceStore.summary(record), "status": result["status"],
                     "body_sha256": digest(body), "body_characters": len(body),
                     "location": headers.get("location", ""), "final_url": result.get("final_url"),
                     "content_type": headers.get("content-type", ""),
                     "cache": {k: headers[k] for k in ("age", "vary", "x-cache", "cache-control") if k in headers}})
    changed_headers = {}
    baseline_headers = {str(k).lower(): str(v) for k, v in (base["result"].get("headers") or {}).items()}
    for role, record in (("test", test), ("control", control)):
        headers = {str(k).lower(): str(v) for k, v in (record["result"].get("headers") or {}).items()}
        changed_headers[role] = sorted(k for k in baseline_headers.keys() | headers.keys() if baseline_headers.get(k) != headers.get(k))
    return {"responses": rows, "changed_header_names": changed_headers,
            "test_matches_baseline": rows[1]["body_sha256"] == rows[0]["body_sha256"],
            "test_matches_control": rows[1]["body_sha256"] == rows[2]["body_sha256"],
            "same_identity": len({r["identity_fingerprint"] for r in records}) == 1,
            "same_origin": len({urlsplit(r["target"]).netloc for r in records}) == 1,
            "inconclusive": any(r["state"] != "observed" for r in records),
            "note": "Differences, identical bodies, and status codes do not by themselves prove or disprove a vulnerability. Raw responses remain in their evidence records."}


def review_evidence(finding, records, challenge):
    """Check provenance and review completeness, never infer exploitability from prose."""
    issues = []
    by_id = {r["id"]: r for r in records}
    if len(by_id) < 2:
        issues.append("A separate reproduction observation is required.")
    if len({r["call_id"] for r in records}) != len(records):
        issues.append("Reproduction must come from a separate tool call.")
    if not finding.get("target") or not finding.get("description"):
        issues.append("Provide the exact target and the claim being evaluated.")
    if any(r["target"] != finding.get("target") for r in records):
        issues.append("Every supporting observation must match the exact finding target.")
    if any(r["state"] != "observed" for r in records):
        issues.append("Blocked, authentication-unavailable, or inconclusive observations cannot confirm the claim.")
    bindings = challenge.get("observations") or []
    bound = set()
    for item in bindings:
        record = by_id.get(item.get("evidence_id")) if isinstance(item, dict) else None
        quote = item.get("quote", "") if isinstance(item, dict) else ""
        if record and isinstance(quote, str) and len(quote.strip()) >= 12 and quote in encoded(record["result"]).decode("utf-8"):
            bound.add(record["id"])
        else:
            issues.append("Each observation needs an exact quote from its recorded result (at least 12 characters).")
    if bound != set(by_id):
        issues.append("Bind every supporting evidence ID to an exact observation.")
    for field in ("expected_boundary", "demonstrated_impact", "alternative_explanation", "disproof_check", "limitations"):
        if not isinstance(challenge.get(field), str) or len(challenge[field].strip()) < 12:
            issues.append(f"Explain {field.replace('_', ' ')}.")
    control = challenge.get("control")
    if not isinstance(control, dict) or not control.get("evidence_id") or not control.get("quote"):
        issues.append("Supply a separate control observation with its evidence ID and exact quote.")
    return {"eligible": not issues, "issues": issues, "finding_sha256": digest(finding),
            "evidence_ids": list(by_id), "challenge": challenge,
            "note": "Eligibility confirms evidence binding and review completeness, not independent proof of the model's interpretation. Report limitations and actual demonstrated impact."}
