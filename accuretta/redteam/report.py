# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team report: findings, evidence and coverage written up for the mission.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
import re
import time
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_rt_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import CHATS_FILE
from accuretta.core.paths import DATA
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.redteam.findings import tool_review_finding
from accuretta.redteam.governor import _rt_gov_ledger_count
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission_logs import _rt_log_mission_ended
from agent_reliability import EvidenceStore
from agent_reliability import response_state
from typing import Any


def tool_rt_generate_report(args: dict) -> dict:
    """Assemble a markdown engagement report: mission header (objective /
    target / scope), model-supplied findings body, and the evidence manifest
    from data/recon_evidence. Saves to data/reports/."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    title = (args.get("title") or "Red Team Engagement Report").strip()
    body = (args.get("findings") or "").strip()
    template = str(args.get("template") or "generic").strip().lower()
    if template not in {"generic", "hackerone", "bugcrowd"}:
        template = "generic"
    chats_data = None
    try:
        chat_id = _current_chat_id.get() or ""
        chats_data = get_chats()
        chat = (chats_data.get("chats", {}) or {}).get(chat_id) if chat_id else None
    except Exception:
        chat = None
    live_chat = _current_rt_chat.get()
    if (isinstance(chat, dict) and isinstance(live_chat, dict)
            and isinstance(live_chat.get("mission"), dict)):
        chat["mission"] = copy.deepcopy(live_chat["mission"])
    mission = _rt_mission_get(chat) if isinstance(chat, dict) else None
    mission_id = str((mission or {}).get("authorization_id") or "")
    ledger = [copy.deepcopy(x) for x in ((chat or {}).get("findings") or [])
              if isinstance(x, dict) and x.get("mission_id") == mission_id] if isinstance(chat, dict) else []
    for item in ledger:
        if item.get("status") in ("validated", "reproduced"):
            checked = tool_review_finding({**item, "challenge": (item.get("review") or {}).get("challenge") or {}})
            if not checked.get("eligible"):
                item.update(status="candidate", source_observed=False, review=checked)
    if not body and not ledger:
        return {"error": "no structured findings or findings markdown available"}
    ev_dir = DATA / "recon_evidence"
    evidence = []
    observations = []
    try:
        receipt_folder = EvidenceStore(ev_dir / "receipts").folder(chat_id, mission_id)
        for f in sorted(receipt_folder.glob("*.json")):
            digest = hashlib.sha256(f.read_bytes()).hexdigest()
            evidence.append(f"| `{f.relative_to(ev_dir).as_posix()}` | {f.stat().st_size} bytes | `{digest}` |")
            try:
                receipt = EvidenceStore(ev_dir / "receipts").read(chat_id, mission_id, f.stem)
                receipt["state"] = response_state(receipt["result"])
                observations.append(receipt)
            except (ValueError, OSError, KeyError):
                observations.append({"id": f.stem, "state": "evidence_unavailable"})
    except Exception:
        pass
    lines = [f"# {title}", "",
             f"_generated {datetime.datetime.now().isoformat(timespec='seconds')} by accuretta_", ""]
    if mission:
        lines += ["## Mission", ""]
        for k in ("objective", "target", "scope"):
            if mission.get(k):
                lines.append(f"- **{k}:** {mission[k]}")
        lines.append("")
    lines += [
        "## Verification status", "",
        "Validated means a specific recorded result assertion passed on two separate tests and failed on a meaningful control. "
        "HTTP, browser, scalar, and timing observations are supported; a justified equivalent control may use another endpoint "
        "on the authorized origin. This verifies that observation, not every "
        "claim in the model's description, severity, or impact. Scanner detection alone stays a candidate. Missing evidence leaves a claim "
        "as a candidate. Only this engagement's findings and receipts are included.", "",
        "Report generation closes the engagement; it does not establish exhaustive assessment coverage.", "",
    ]
    if ledger:
        def _md(value: Any, cap: int = 300) -> str:
            return re.sub(r"\s+", " ", str(value or "")).replace("|", "\\|")[:cap]
        lines += ["## Harness findings ledger", "",
                  "| id | severity | status | confidence | finding | target | evidence source |",
                  "|---|---|---|---:|---|---|---|"]
        for item in ledger:
            artifact = item.get("artifact") if isinstance(item.get("artifact"), dict) else {}
            source = ""
            if artifact:
                source = f"artifact `{_md(artifact.get('path'), 180)}` sha256 `{_md(artifact.get('sha256'), 80)}`"
            elif item.get("source_observed"):
                source = f"observed tool `{_md(item.get('source_tool'), 80)}`"
            else:
                source = "unvalidated evidence references" if item.get("evidence_ids") else "model assertion only"
            lines.append(
                f"| `{_md(item.get('id'), 70)}` | {_md(item.get('severity'), 20)} | "
                f"{_md(item.get('status'), 30)} | {int(item.get('confidence') or 0)}% | "
                f"{_md(item.get('title'))} | {_md(item.get('target'), 180)} | {source} |"
            )
        lines.append("")
        for item in ledger:
            lines += [f"### {_md(item.get('id'), 70)} — {_md(item.get('title'))}", "",
                      f"- Status: **{_md(item.get('status'), 30)}**",
                      f"- Severity: **{_md(item.get('severity'), 20)}**",
                      f"- Harness-observed source: **{'yes' if item.get('source_observed') else 'no'}**"]
            if isinstance(item.get("cvss"), dict):
                lines.append(f"- CVSS (validated): `{_md(item['cvss'].get('vector'), 200)}` "
                             f"({item['cvss'].get('score')}, {_md(item['cvss'].get('severity'), 20)})")
            if isinstance(item.get("cwe"), dict):
                _cwe_name = f" · {_md(item['cwe'].get('name'), 120)}" if item["cwe"].get("name") else ""
                lines.append(f"- Weakness: {_md(item['cwe'].get('id'), 30)}{_cwe_name}")
            if item.get("description"):
                lines += ["", "Model-authored claim (interpretation, not a verified impact statement):", "", _md(item.get("description"), 2000)]
            if item.get("inferred_impact"):
                lines += ["", "Inferred impact, not demonstrated: " + _md(item["inferred_impact"], 2000)]
            if item.get("evidence"):
                lines += ["", "Model-authored evidence note:", "", _md(item.get("evidence"), 3000)]
            review = item.get("review") or {}
            if review.get("issues"):
                lines += ["", "Verification gaps:"] + ["- " + _md(issue, 1000) for issue in review["issues"]]
            if review.get("eligible"):
                lines += ["", "Verified response assertion: `" + _md(json.dumps(review["challenge"]["test"], ensure_ascii=False), 3000).replace("`", "'") + "`",
                          "", "Only the response assertion above was checked. The impact interpretation below remains model-authored."]
            if item.get("evidence_ids"):
                lines += ["", "Evidence IDs: " + ", ".join(_md(value, 80) for value in item["evidence_ids"])]
            if review.get("control_id"):
                lines.append("Control ID: " + _md(review["control_id"], 80))
            for key, value in (review.get("challenge") or {}).items():
                if isinstance(value, str):
                    label = "model-authored impact interpretation" if key == "demonstrated_impact" else key.replace('_', ' ')
                    lines.append(f"- {_md(label)}: {_md(value, 2000)}")
            lines.append("")
    if template in ("hackerone", "bugcrowd") and ledger:
        platform = "HackerOne" if template == "hackerone" else "Bugcrowd"
        lines += [f"## {platform} submission fields", "",
                  "One block per finding in the platform's field shape. Severity, CVSS, and CWE "
                  "are harness-validated; description, impact, and remediation text is "
                  "model-authored and labeled as such — verify before submitting.", ""]
        for item in ledger:
            cvss = item.get("cvss") if isinstance(item.get("cvss"), dict) else None
            cwe = item.get("cwe") if isinstance(item.get("cwe"), dict) else None
            artifact = item.get("artifact") if isinstance(item.get("artifact"), dict) else {}
            lines += [f"### {_md(item.get('id'), 70)} — {_md(item.get('title'))}", "",
                      f"- **Title:** {_md(item.get('title'))}"]
            if cvss:
                lines.append(f"- **Severity / CVSS:** {_md(item.get('severity'), 20)} · "
                             f"`{_md(cvss.get('vector'), 200)}` ({cvss.get('score')}, {_md(cvss.get('severity'), 20)})")
            else:
                lines.append(f"- **Severity / CVSS:** {_md(item.get('severity'), 20)} (no validated CVSS vector)")
            if cwe:
                _cwe_name = f" — {_md(cwe.get('name'), 120)}" if cwe.get("name") else ""
                lines.append(f"- **Weakness (CWE):** {_md(cwe.get('id'), 30)}{_cwe_name}")
            lines += [f"- **Asset / endpoint:** {_md(item.get('target'), 240)}",
                      f"- **Status / confidence:** {_md(item.get('status'), 30)} · {int(item.get('confidence') or 0)}%"]
            if item.get("evidence_ids"):
                lines.append("- **Evidence IDs:** " + ", ".join(_md(v, 80) for v in item["evidence_ids"]))
            if artifact:
                lines.append(f"- **Artifact:** `{_md(artifact.get('path'), 180)}` sha256 `{_md(artifact.get('sha256'), 80)}`")
            receipt_ids = set(item.get("evidence_ids") or [])
            steps = [r for r in observations if r.get("id") in receipt_ids]
            if steps:
                lines += ["", "Steps to reproduce (from this engagement's recorded receipts):"]
                for r in steps:
                    lines.append(f"1. `{_md(r.get('tool'), 60)}` against `{_md(r.get('target'), 200)}` → {_md(r.get('state'), 60)}")
            if item.get("description"):
                lines += ["", "Description (model-authored; not independently verified):", "",
                          _md(item.get("description"), 2000)]
            if item.get("inferred_impact"):
                lines += ["", "Impact (inferred by the model; not demonstrated):", "",
                          _md(item["inferred_impact"], 2000)]
            lines.append("")
        if body:
            lines += ["## Remediation / analyst narrative (model-authored)", "", body, ""]
    coverage = [copy.deepcopy(p) for p in (chat or {}).get("security_coverage", []) if p.get("mission_id") == mission_id]
    by_id = {r["id"]: r for r in observations}
    for planned in coverage:
        linked = [by_id.get(value, {"state": "evidence_unavailable"}) for value in planned.get("evidence_ids", [])]
        planned["state"] = (next((r["state"] for r in linked if r["state"] != "observed"), "observed")
                            if linked else "untested")
    if coverage:
        lines += ["## Coverage and limitations", "", "Observed does not mean secure. Identity labels are declared test context.", ""]
        for item in coverage:
            lines.append("- " + re.sub(r"[\r\n]+", " ", f"{item.get('feature')} | {item.get('target')} | {item.get('identity')} | {item.get('state')} | {item.get('limitations', '')}"))
    if body:
        lines += ["## Unverified analyst narrative", "",
                  "The following is model-authored text. It cannot override the verified assertions or coverage gaps above. "
                  "Claims of database access, write capability, clean endpoints, and exhaustive coverage are not certified by this section.", "", body, ""]
    if observations:
        lines += ["## Recorded checks", "", "These rows come from execution receipts. An observed response is not a clean security test. "
                  "Planned checks without linked evidence remain untested even when another tool visited the same URL.", "",
                  "| receipt | tool | target | outcome | limitations |", "|---|---|---|---|---|"]
        for receipt in observations:
            result = receipt.get("result") or {}
            detail = result.get("error") or result.get("limitations") or result.get("fetch_errors") or result.get("scan_warnings") or ""
            values = [receipt["id"], receipt.get("tool", "unknown"), receipt.get("target", "unknown"), receipt["state"], detail]
            lines.append("| " + " | ".join(re.sub(r"\s+", " ", str(value)).replace("|", "\\|")[:1500] for value in values) + " |")
        lines.append("")
    unresolved = sum(p["state"] != "observed" for p in coverage)
    incomplete = sum(r["state"] != "observed" for r in observations)
    recorded_plan_complete = bool(coverage) and unresolved == 0
    ledger_sent = _rt_gov_ledger_count(mission_id) if mission_id else 0
    lines += ["## Assessment completeness", "",
              f"{unresolved} planned checks unresolved; {incomplete} recorded checks blocked, inconclusive, or unavailable.",
              (f"{ledger_sent} outbound HTTP request(s) recorded in the engagement ledger "
               f"(`data/redteam/request_ledger.jsonl`) — the audit trail for program or client "
               f"questions about traffic volume and touched hosts." if ledger_sent else
               "No outbound HTTP requests were recorded in the engagement ledger."),
              ("Every check in the recorded coverage plan has linked observations. This does not certify that the plan was exhaustive."
               if recorded_plan_complete else "The recorded coverage plan was incomplete. Report ready does not mean all phases passed."), ""]
    if evidence:
        lines += ["## Evidence artifacts", "", "| file | size | sha256 |", "|---|---:|---|"] + evidence + ["",
                 "_evidence files live in `data/recon_evidence/`._"]
    rep_dir = DATA / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    path = rep_dir / f"report-{int(time.time())}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    # Generating the final report closes the server-side mission. Later model
    # drift cannot silently resume recon/exploitation; a fresh gate launch is
    # required to create a new active authorization record.
    if isinstance(chat, dict) and isinstance(chat.get("mission"), dict):
        reviewed = {item["id"]: item for item in ledger if item.get("id")}
        chat["findings"] = [reviewed.get(item.get("id"), item) if item.get("mission_id") == mission_id else item
                            for item in chat.get("findings", [])]
        chat["mission"]["status"] = "closed"
        chat["mission"]["closed_at"] = int(time.time())
        _rt_log_mission_ended(chat["mission"])
        if isinstance(live_chat, dict):
            live_chat["mission"] = copy.deepcopy(chat["mission"])
        if isinstance(chats_data, dict):
            save_json(CHATS_FILE, chats_data)
    return {"ok": True, "report": str(path), "evidence_count": len(evidence),
            "finding_count": len(ledger),
            "validated_findings": sum(1 for x in ledger if x.get("status") in ("validated", "reproduced")),
            "assessment_complete": recorded_plan_complete,
            "recorded_plan_complete": recorded_plan_complete,
            "unresolved_planned_checks": unresolved,
            "incomplete_recorded_checks": incomplete,
            "mission_status": "closed"}
