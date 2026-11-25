# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's working notes: pinned notes, the task plan, and the evidence and
verification helpers it keeps beside them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.chat_events import _emit_chat_event
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.redteam.findings import _evidence_context
from accuretta.redteam.recon_fetch import _recon_fetch
from agent_reliability import compare_responses
from agent_reliability import verification_advice


def tool_pin_note(args: dict) -> dict:
    """Pin a fact, decision, or fix to THIS session. Pins are injected verbatim
    at the recency end of every turn's context (a tail message, so the cached
    prompt prefix survives) and NEVER trimmed or summarized away — use them for
    the critical 'do not undo / do not repeat' items (a bug's root cause and
    fix, a user constraint, a chosen approach)."""
    note = (args.get("note") or "").strip()
    if not note:
        return {"error": "note required"}
    cid = _get_current_chat()
    if not cid:
        return {"error": "no active chat to pin to"}
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid)
    if not chat:
        return {"error": "active chat not found"}
    note = note[:300]
    pins = chat.get("pins") or []
    if note in pins:
        return {"already_pinned": note, "total_pins": len(pins)}
    pins.append(note)
    chat["pins"] = pins[-20:]   # keep the 20 most-recent
    save_json(CHATS_FILE, chats)
    return {"pinned": note, "total_pins": len(chat["pins"]),
            "note": "Pinned for this session — stays in context permanently, won't be trimmed."}


def tool_update_plan(args: dict) -> dict:
    """Show/update the docked task-progress checklist the user sees. Call once at
    the start of a multi-step task with all steps, then call again to flip a
    step's status as you go. Statuses: pending | active | done. Stored on the
    chat, streamed to the panel, and re-injected as a compact recency-tail
    checkpoint so long one-shot runs retain what is done and what remains."""
    raw = args.get("steps") or []
    if not isinstance(raw, list) or not raw:
        return {"error": "steps must be a non-empty list of {title, status}"}
    steps = []
    for s in raw:
        if isinstance(s, str):
            steps.append({"title": s[:120], "status": "pending"})
        elif isinstance(s, dict):
            st = str(s.get("status") or "pending").lower()
            if st not in ("pending", "active", "done"):
                st = "pending"
            title = str(s.get("title") or s.get("step") or s.get("name") or "")[:120]
            steps.append({"title": title, "status": st})
    steps = [s for s in steps if s["title"]][:12]
    if not steps:
        return {"error": "no valid steps"}
    cid = _get_current_chat()
    if cid:
        chats = get_chats()
        chat = chats.get("chats", {}).get(cid)
        if chat is not None:
            chat["plan"] = steps
            save_json(CHATS_FILE, chats)
    _emit_chat_event({"type": "plan", "steps": steps})
    done = sum(1 for s in steps if s["status"] == "done")
    return {"ok": True, "steps": len(steps), "done": done, "active": next(
        (s["title"] for s in steps if s["status"] == "active"), None),
        "note": "Plan shown to the user. Flip each step to 'done' as you finish it."}


def tool_compare_evidence(args):
    store, cid, mission_id = _evidence_context()
    return compare_responses([store.read(cid, mission_id, args[key]) for key in ("baseline_id", "test_id", "control_id")])


def tool_observe_response(args):
    url = str(args.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return {"error": "Provide an explicit HTTP(S) URL without embedded credentials.", "invalid_arguments": True, "not_executed": True}
    headers = args.get("headers") or {}
    if not isinstance(headers, dict) or any(str(k).lower() not in {"authorization", "cookie", "accept", "accept-language"} or not isinstance(v, str) for k, v in headers.items()):
        return {"error": "Only Authorization, Cookie, Accept and Accept-Language string headers are supported.", "invalid_arguments": True, "not_executed": True}
    result = _recon_fetch(url, max_bytes=128000, extra_headers=headers, follow=False)
    result["capture_limit_bytes"] = 128000
    result["potentially_truncated"] = len(str(result.get("body") or "").encode("utf-8")) >= 128000
    return result


def tool_verification_guidance(args):
    return {"next_step": verification_advice(str(args.get("path") or ""), str(args.get("kind") or "change")),
            "note": "Guidance only. No check was run and no verification status changed."}


def tool_unpin_note(args: dict) -> dict:
    """Remove a pin from this session by exact text or 1-based index."""
    cid = _get_current_chat()
    if not cid:
        return {"error": "no active chat"}
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid)
    if not chat:
        return {"error": "active chat not found"}
    pins = chat.get("pins") or []
    target = args.get("note")
    idx = args.get("index")
    removed = None
    if target and target in pins:
        pins.remove(target)
        removed = target
    elif isinstance(idx, int) and 1 <= idx <= len(pins):
        removed = pins.pop(idx - 1)
    else:
        return {"error": "pin not found", "current_pins": pins}
    chat["pins"] = pins
    save_json(CHATS_FILE, chats)
    return {"unpinned": removed, "total_pins": len(pins)}
