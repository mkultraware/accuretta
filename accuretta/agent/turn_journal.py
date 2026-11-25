# SPDX-License-Identifier: AGPL-3.0-only
"""The turn journal (recovering a turn cut off by a crash) and the continuity
state carried across folds.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import re
import time
from accuretta.agent.compaction import _COMPACTION_ANCHOR_MAX
from accuretta.agent.compaction import _COMPACTION_RECENT_USER_MESSAGES
from accuretta.core.chats import get_chats
from accuretta.core.paths import CHATS_FILE
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from pathlib import Path


TURN_JOURNAL_DIR = DATA / "turn_journal"


def _turn_journal_path(chat_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(chat_id or ""))[:80] or "unknown"
    return TURN_JOURNAL_DIR / f"{safe}.json"


def _turn_journal_clear(chat_id: str, turn_id: str = "") -> None:
    path = _turn_journal_path(chat_id)
    try:
        if turn_id and path.exists():
            row = load_json(path, {})
            if isinstance(row, dict) and row.get("turn_id") not in (None, "", turn_id):
                return
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _recover_turn_journals(now: float | None = None) -> dict:
    """Recover completed tool rounds left behind by a bridge/process crash."""
    now = time.time() if now is None else float(now)
    stats = {"recovered": 0, "entries": 0, "stale": 0, "invalid": 0}
    try:
        paths = list(TURN_JOURNAL_DIR.glob("*.json"))
    except Exception:
        return stats
    if not paths:
        return stats
    chats = get_chats()
    dirty = False
    cleared: list[tuple[str, str]] = []
    for path in paths:
        row = load_json(path, None)
        if not isinstance(row, dict):
            stats["invalid"] += 1
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        chat_id = str(row.get("chat_id") or "")
        turn_id = str(row.get("turn_id") or "")
        updated = float(row.get("updated") or 0)
        if not chat_id or not turn_id or now - updated > 7 * 86400:
            stats["stale"] += 1
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        chat = chats.get("chats", {}).get(chat_id)
        if not isinstance(chat, dict):
            stats["invalid"] += 1
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            continue
        # The final chat save may have succeeded just before the process died.
        # A turn stamp makes recovery idempotent across that crash window.
        if any(isinstance(m, dict) and m.get("_turn_id") == turn_id
               for m in (chat.get("messages") or [])):
            cleared.append((chat_id, turn_id))
            continue
        recovered = []
        for entry in row.get("entries") or []:
            if not isinstance(entry, dict) or (entry.get("role") not in ("assistant", "tool")
                                               and not (entry.get("role") == "user" and entry.get("_steering_id"))):
                continue
            item = copy.deepcopy(entry)
            item["t"] = int(updated or now)
            item["_turn_id"] = turn_id
            item["_recovered"] = True
            if item.get("role") == "assistant":
                item["_internal"] = True
            recovered.append(item)
        if not recovered:
            cleared.append((chat_id, turn_id))
            continue
        recovered.append({
            "role": "assistant",
            "content": ("[automatic recovery note] Accuretta recovered completed tool results from "
                        "an interrupted turn. Treat them as executed evidence, inspect the activity "
                        "ledger, and continue only the unfinished steps."),
            "_internal": True,
            "_turn_id": turn_id,
            "_recovered": True,
            "t": int(now),
        })
        chat.setdefault("messages", []).extend(recovered)
        if isinstance(row.get("activity"), list):
            chat["activity"] = [x for x in row["activity"] if isinstance(x, dict)][-_ACTIVITY_MAX:]
        if isinstance(row.get("mission"), dict):
            chat["mission"] = copy.deepcopy(row["mission"])
        if isinstance(row.get("verification_debt"), list):
            chat["verification_debt"] = [copy.deepcopy(x) for x in row["verification_debt"]
                                         if isinstance(x, dict)]
        _refresh_continuity_state(chat)
        chat["turn_recovery"] = {"turn_id": turn_id, "recovered_at": int(now),
                                 "entries": len(recovered) - 1}
        chat["updated"] = int(now)
        dirty = True
        stats["recovered"] += 1
        stats["entries"] += len(recovered) - 1
        cleared.append((chat_id, turn_id))
    if dirty:
        save_json(CHATS_FILE, chats)
    for chat_id, turn_id in cleared:
        _turn_journal_clear(chat_id, turn_id)
    return stats


_TASK_ANCHOR_ORIGINAL_CHARS = 420


_TASK_ANCHOR_CURRENT_CHARS = 620


_ACTIVITY_MAX = 16


def _build_continuity_state(chat: dict, activity: list[dict] | None = None,
                            plan: list[dict] | None = None) -> dict:
    """Deterministic state beside the model-authored prose summary.

    A summary is useful compression but cannot be the only record of the goal,
    verified tool outcomes, and unfinished plan. This structure is derived from
    harness-owned state, so even a mediocre summarizer cannot invent or erase it.
    """
    anchor = chat.get("task_anchor") if isinstance(chat.get("task_anchor"), dict) else {}
    activity = activity if isinstance(activity, list) else (chat.get("activity") or [])
    plan = plan if isinstance(plan, list) else (chat.get("plan") or [])
    pins = [str(x).strip()[:300] for x in (chat.get("pins") or []) if str(x).strip()]
    mission = chat.get("mission") if isinstance(chat.get("mission"), dict) else {}
    compaction_memory = (
        chat.get("compaction_memory")
        if isinstance(chat.get("compaction_memory"), dict) else {}
    )
    constraints = list(pins[-12:])
    if mission:
        for label, value in (("authorized target", mission.get("target")),
                             ("authorized scope", mission.get("scope")),
                             ("operator notes", mission.get("notes")),
                             ("required user agent", mission.get("user_agent")),
                             ("mission phase", mission.get("phase"))):
            if value:
                constraints.append(f"{label}: {value}"[:300])
    completed, failures, artifacts = [], [], []
    for item in activity[-_ACTIVITY_MAX:]:
        if not isinstance(item, dict):
            continue
        line = f"{item.get('tool', 'tool')}: {item.get('target') or item.get('detail') or item.get('status', 'ok')}"
        if str(item.get("status") or "ok") in ("ok", "success"):
            completed.append(line[:320])
        else:
            failures.append(line[:320])
        target = str(item.get("target") or "").strip()
        if target and (re.match(r"^[A-Za-z]:[\\/]", target) or target.startswith(("/", "http://", "https://"))):
            artifacts.append({"location": target[:300], "via": str(item.get("tool") or "tool")[:80],
                              "status": str(item.get("status") or "ok")[:40]})
    next_steps = []
    for item in plan[:20]:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending")
        title = str(item.get("title") or item.get("step") or "").strip()
        if title and status not in ("completed", "done"):
            next_steps.append({"status": status[:32], "step": title[:320]})
    state = {
        "version": 2,
        "goal": str(anchor.get("original") or "")[:_TASK_ANCHOR_ORIGINAL_CHARS],
        "current_task": str(anchor.get("current") or anchor.get("original") or "")[:_TASK_ANCHOR_CURRENT_CHARS],
        "verbatim_original_user": str(compaction_memory.get("original_user") or ""),
        "recent_user_messages": [
            str(item) for item in (compaction_memory.get("recent_user_messages") or [])
            if str(item).strip()
        ][-_COMPACTION_RECENT_USER_MESSAGES:],
        "exact_anchors": [
            str(item) for item in (compaction_memory.get("exact_anchors") or [])
            if str(item).strip()
        ][-_COMPACTION_ANCHOR_MAX:],
        "constraints": list(dict.fromkeys(constraints))[-16:],
        "completed": completed[-12:],
        "failures": failures[-8:],
        "artifacts": artifacts[-12:],
        "findings": [
            {"id": str(x.get("id") or "")[:60], "status": str(x.get("status") or "candidate")[:32],
             "severity": str(x.get("severity") or "info")[:16], "title": str(x.get("title") or "")[:240]}
            for x in (chat.get("findings") or [])[-20:] if isinstance(x, dict)
        ],
        "verification_required_count": len(chat.get("verification_debt") or []),
        "verification_required": [
            {"path": str(x.get("path") or "")[:300],
             "status": str(x.get("status") or "unchecked"),
             "via": str(x.get("via") or "edit")[:80],
             "since": int(x.get("since") or 0)}
            for x in (chat.get("verification_debt") or [])[-20:]
            if isinstance(x, dict) and str(x.get("path") or "").strip()
        ],
        "next_steps": next_steps[:12],
        "updated": int(time.time()),
    }
    return state


def _refresh_continuity_state(chat: dict, activity: list[dict] | None = None,
                              plan: list[dict] | None = None) -> dict:
    state = _build_continuity_state(chat, activity=activity, plan=plan)
    chat["continuity"] = state
    return state
