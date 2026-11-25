# SPDX-License-Identifier: AGPL-3.0-only
"""How an authorised red-team mission moves along: seeding it, phases, notes, marking
tools used, confirmed findings, the identities sweep, the scoped browser tool, and
pruning old evidence.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import shutil
import time
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from accuretta.core.persistence import save_json
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.redteam.identities import _RT_IDENTITIES_FILE
from accuretta.redteam.identities import _RT_IDENTITIES_LOCK
from accuretta.redteam.identities import _rt_identities_read
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.mission_logs import _rt_log_mission_ended
from accuretta.redteam.playbook_table import _RT_PLAYBOOK_TOOLS
from accuretta.redteam.recon_map_tool import _RT_BROWSER_WORKER
from accuretta.redteam.recon_map_tool import _rt_browser_policy
from accuretta.redteam.scoped_browser import _rt_browser_session_name


_RT_MISSION_FACTS_MAX = 8       # keep the N most-recent established facts (pin-style cap)


# Reliable-attention span: the rough token distance a model attends over
# cleanly regardless of window size (a model property, not a GPU tuning).
_RT_ATTN_SPAN = 16000


def _rt_target_from_args(name: str, args: dict) -> str:
    """Pull the target host from a red-team tool's arguments, if any."""
    if not isinstance(args, dict):
        return ""
    for k in ("url", "base_url", "target", "host", "domain", "endpoint", "origin"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            return _rt_url_host(v)
    return ""


def _rt_frontend_secret_mission(chat: dict | None) -> dict | None:
    mission = _rt_mission_get(chat) if isinstance(chat, dict) else None
    return mission if mission and mission.get("engagement") == "frontend_secrets" else None


def _rt_close_frontend_secret_mission(chat: dict | None, tool_name: str = "") -> bool:
    """Close the bounded one-tool frontend audit after its scanner returns."""
    mission = _rt_frontend_secret_mission(chat)
    if not mission or str(mission.get("status") or "active").lower() == "closed":
        return False
    if tool_name and _resolve_tool_name(tool_name) != "scan_js_secrets":
        return False
    mission["status"] = "closed"
    mission["closed_at"] = int(time.time())
    chat["mission"] = mission
    _rt_log_mission_ended(mission)
    return True


# Sign-ins of an engagement left open (never reported, chat abandoned) are
# deleted at the next startup once it is this old.
_RT_IDENTITY_KEEP_SECONDS = 7 * 86400


def _rt_identities_sweep() -> int:
    """At startup, delete sign-ins whose engagement is no longer active (closed,
    replaced, or its chat deleted while the app was not running)."""
    with _RT_IDENTITIES_LOCK:
        stored = _rt_identities_read()
        if not stored:
            return 0
        active = set()
        cutoff = time.time() - _RT_IDENTITY_KEEP_SECONDS
        for chat in (get_chats().get("chats") or {}).values():
            mission = _rt_authorized_mission(chat) if isinstance(chat, dict) else None
            # An engagement nobody closed is not kept alive forever: a sign-in
            # that old has expired at the target anyway.
            if mission and float(mission.get("authorized_at") or 0) >= cutoff:
                active.add(str(mission.get("authorization_id") or ""))
        ended = [key for key in stored if key not in active]
        for key in ended:
            stored.pop(key, None)
        if ended:
            save_json(_RT_IDENTITIES_FILE, stored)
        return len(ended)


def tool_rt_browser(args: dict) -> dict:
    """Run a persistent Chromium session bound to the active mission policy."""
    chat = _rt_context_chat()
    policy = _rt_browser_policy(chat)
    if not policy:
        return {"error": "an active authorized Red Team mission is required"}
    payload = dict(args or {})
    payload["session"] = _rt_browser_session_name(payload.get("session"))
    payload["policy"] = policy
    return _RT_BROWSER_WORKER.call(payload)


def _rt_mission_seed(chat: dict, user_text: str) -> bool:
    """Seed objective from the initiating instruction on the first red-team turn.
    Only fills empty fields — never overwrites an established objective."""
    obj = (user_text or "").strip()
    if not obj:
        return False
    m = chat.get("mission")
    if not isinstance(m, dict):
        m = {"target": "", "scope": "", "objective": "", "facts": [], "phase": "recon", "ts": int(time.time())}
    if m.get("objective"):
        return False
    m["objective"] = obj[:300]
    chat["mission"] = m
    return True


def _rt_mission_note(chat: dict, target: str = "", fact: str = "", scope: str = "") -> bool:
    """Set the target if not yet known, set an explicit scope, and append a
    deduped, capped fact. Returns True if the mission changed. Same cap idiom as
    pins."""
    m = chat.get("mission")
    if not isinstance(m, dict):
        m = {"target": "", "scope": "", "objective": "", "facts": [], "phase": "recon", "ts": int(time.time())}
    changed = False
    if target and not m.get("target"):
        m["target"] = target[:120]
        # In-scope defaults to the confirmed target host when nothing else is known.
        if not m.get("scope"):
            m["scope"] = target[:120]
        changed = True
    if scope:
        # Explicit scope from the panel overrides the target-derived default.
        m["scope"] = scope[:200]
        changed = True
    if fact:
        fact = fact.strip()[:200]
        facts = m.get("facts") or []
        if fact and fact not in facts:
            facts.append(fact)
            m["facts"] = facts[-_RT_MISSION_FACTS_MAX:]
            changed = True
    if changed:
        chat["mission"] = m
    return changed


def _rt_mission_set_phase(chat: dict, phase: str) -> bool:
    """Advance the mission phase (recon -> exploit). Returns True if it changed.
    Creates the holder if absent so the flip survives even before a target or
    objective was recorded (e.g. a lone validate_finding confirm)."""
    m = chat.get("mission")
    if not isinstance(m, dict):
        m = {"target": "", "scope": "", "objective": "", "facts": [], "phase": "recon", "ts": int(time.time())}
    if m.get("phase") == phase:
        chat["mission"] = m
        return False
    m["phase"] = phase
    chat["mission"] = m
    return True


def _rt_result_confirms_finding(name: str, result: dict | None) -> bool:
    """Whether harness-observed output is strong enough to open exploit tools."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    canon = _resolve_tool_name(name)
    if canon == "record_finding":
        finding = result.get("finding") or {}
        return bool(
            isinstance(finding, dict)
            and finding.get("source_observed") is True
            and (finding.get("review") or {}).get("eligible") is True
            and str(finding.get("status") or "").lower() in {"validated", "reproduced"}
        )
    return False


def _rt_mission_mark_tool(chat: dict, name: str) -> bool:
    """Remember that a playbook tool ran in this engagement. True if new."""
    mission = chat.get("mission") if isinstance(chat, dict) else None
    canon = _resolve_tool_name(name)
    if not isinstance(mission, dict) or canon not in _RT_PLAYBOOK_TOOLS:
        return False
    used = [str(t) for t in (mission.get("tools_used") or [])]
    if canon in used:
        return False
    used.append(canon)
    mission["tools_used"] = used[-40:]
    return True


def _prune_red_team_evidence(settings: dict, now: float | None = None) -> dict:
    """Apply the configured evidence retention policy at startup."""
    ev_dir = DATA / "recon_evidence"
    if not ev_dir.exists():
        return {"removed": 0, "kept": 0, "days": None}
    try:
        days = max(0, min(3650, int(settings.get("rt_evidence_retention_days", 30))))
    except (TypeError, ValueError):
        days = 30
    now = time.time() if now is None else float(now)
    cutoff = now - days * 86400
    removed = 0
    kept = 0
    try:
        for item in ev_dir.rglob("*"):
            try:
                if not item.is_file():
                    continue
                expired = days == 0 or item.stat().st_mtime < cutoff
                if not expired:
                    kept += 1
                    continue
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
                removed += 1
            except Exception:
                kept += 1
    except Exception:
        pass
    return {"removed": removed, "kept": kept, "days": days}
