# SPDX-License-Identifier: AGPL-3.0-only
"""The danger gate for code the agent runs: what a script really does, and the hold-to-
approve or block for the dangerous cases.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.security.approvals import _log_gate_decision
from accuretta.security.approvals import request_approval
from accuretta.security.danger import _SECRET_PY_NET_RE
from accuretta.security.danger import _danger_assess
from accuretta.security.soft_patterns import _SOFT_CODE_STRING_RE
from accuretta.security.soft_patterns import _SOFT_PY_SPAWN_RE
from accuretta.security.soft_patterns import _code_secret_named


def _danger_assess_code(code: str) -> tuple[str, str] | None:
    """The same for Python/JS source: its strings are commands only when it
    starts programs; a secret path plus a network library is an upload."""
    if not code:
        return None
    strings = [m.group(2) for m in _SOFT_CODE_STRING_RE.finditer(code)]
    if _code_secret_named(strings, code) and _SECRET_PY_NET_RE.search(code):
        return "block", "sends a private key or saved password off this PC"
    if not (_SOFT_PY_SPAWN_RE.search(code) or re.search(r"\b(?:child_process|execSync|spawnSync|execa)\b", code)):
        return None
    # Each string on its own, and each list literal as one argv: a detection
    # pattern list or a regex is data, a command string or argv is a command.
    pieces = list(strings)
    for lst in re.finditer(r"\[([^\[\]]{0,600})\]", code):
        items = [m.group(2) for m in _SOFT_CODE_STRING_RE.finditer(lst.group(1))]
        if len(items) > 1:
            pieces.append(" ".join(items))
    for piece in pieces:
        if re.search(r"[\\(\[]\S*[|?*+]", piece) and not re.search(r"\s", piece.strip()):
            continue                                   # a bare regex
        hit = _danger_assess(piece)
        if hit:
            return hit
    return None


def _danger_gate(text: str, title: str, *, code: str = "", target: str = "host") -> dict | None:
    """Apply the hard stops to a command (and the code it runs). Returns an
    error dict when refused or not approved, else None. The hold-to-approve
    card never auto-passes, not in Soft and not under /goal."""
    hit = _danger_assess(text) or (_danger_assess_code(code) if code else None)
    if not hit:
        return None
    level, why = hit
    if level == "block":
        _log_gate_decision({"kind": "security"}, {"status": "blocked"}, 0.0, reason=why)
        return {"error": (f"refused: this {why}. Accuretta never does this for an agent; "
                          "run it yourself if you truly mean to."),
                "blocked": True, "not_executed": True, "executed": False}
    approval = request_approval(
        title=title, command=text,
        details={"kind": "security_change", "critical": True, "target": target,
                 "critical_note": f"This {why}. It never auto-approves: press and HOLD to confirm.",
                 "soft_reasons": [why]},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied: this {why} ({approval.get('status')})",
                "not_executed": True, "executed": False}
    return {"approved": True}
