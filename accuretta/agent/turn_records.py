# SPDX-License-Identifier: AGPL-3.0-only
"""What a turn leaves behind: the journal checkpoints, why it ended, the model label,
tool error kinds, the live activity record, the task anchor and state, and notes about
dropped context.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import copy
import re
import threading
import time
import uuid
from accuretta.agent.chat_index import CHAT_HISTORY_MAX
from accuretta.agent.compaction import _SUMMARY_REQUIRED_HEADINGS
from accuretta.agent.compaction import _TASK_ACK_RE
from accuretta.agent.compaction import _task_anchor_text
from accuretta.agent.rolling_summary import _SUMMARY_MIN_OUTPUT_CHARS
from accuretta.agent.turn_journal import _ACTIVITY_MAX
from accuretta.agent.turn_journal import _TASK_ANCHOR_CURRENT_CHARS
from accuretta.agent.turn_journal import _TASK_ANCHOR_ORIGINAL_CHARS
from accuretta.agent.turn_journal import _turn_journal_path
from accuretta.core.chats import get_chats
from accuretta.core.events import broadcast_event
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.model_observation import _TOOL_ERROR_PATTERNS
from pathlib import Path
from typing import Any


_chat_live_activity: dict[str, list[dict]] = {}


_chat_live_activity_lock = threading.Lock()


# Per-chat "the trimmer dropped messages last round" flag. The rolling-summary
# auto-trigger (0.85 fill) is measured on the ALREADY-TRIMMED prompt, so once
# the trimmer bites it can never fire again (the trimmer caps fill ~25% below
# the trigger). A drop IS the overflow signal — these flags let the next user
# turn fold proactively instead of replaying the middle into another drop.
_last_dropped_by_chat: dict[str, bool] = {}


def _turn_end_reason(terminal_error) -> str:
    """A fixed label for how a turn that reached its final answer ended."""
    text = str(terminal_error or "").lower()
    if not text:
        return "ok"
    if "tool-round limit" in text:
        return "round_limit"
    if "kept planning" in text or "thinking" in text:
        return "thinking_stall"
    if "did not produce" in text or "no executable tool call" in text:
        return "no_answer"
    return "error"


def _tool_error_kind(result) -> str:
    """A fixed label for why a tool result failed ("" when it did not)."""
    if not isinstance(result, dict):
        return ""
    error = result.get("error")
    code = result.get("exit_code")
    if not error and result.get("ok") is not False and code in (None, 0):
        return ""
    text = str(error or result.get("stderr") or result.get("reason") or "").lower()[:600]
    for kind, pattern in _TOOL_ERROR_PATTERNS:
        if text and re.search(pattern, text):
            return kind
    if code not in (None, 0):
        return "nonzero_exit"
    return "other"


def _turn_model_label() -> str:
    """Short name of the model running a turn ("Sage-A1.5-Q4_K_M"), or ""."""
    try:
        raw = _llama.loaded_model() or get_settings().get("model_path") or ""
    except Exception:
        return ""
    return Path(str(raw)).stem[:80] if raw else ""


def _turn_journal_begin(chat_id: str) -> str:
    """Allocate an id; the journal file is created only after a tool succeeds."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:12]}"


def _turn_journal_checkpoint(chat_id: str, turn_id: str, entries: list[dict],
                             activity: list[dict] | None = None,
                             mission: dict | None = None,
                             verification_debt: list[dict] | None = None) -> None:
    """Durably checkpoint completed tool work during a long autonomous turn.

    Tool outputs in ``entries`` are already size-capped for model replay. A hard
    process crash can therefore lose at most the model generation currently in
    flight, not every tool result since the user's original prompt.
    """
    if not chat_id or not turn_id or not entries:
        return
    clean_entries = []
    for row in entries:
        if not isinstance(row, dict) or (row.get("role") not in ("assistant", "tool")
                                        and not (row.get("role") == "user" and row.get("_steering_id"))):
            continue
        clean = {k: copy.deepcopy(v) for k, v in row.items()
                 if k in ("role", "content", "tool_calls", "tool_call_id", "name",
                          "_steering_id", "_steering_text", "_steering_images", "_steer_partial")}
        if row.get("_steering_id"):
            clean["content"] = row.get("_steering_text", row.get("content", ""))
            clean["images"] = row.get("_steering_images", [])
        clean_entries.append(clean)
    if not clean_entries:
        return
    payload = {
        "version": 1,
        "chat_id": chat_id,
        "turn_id": turn_id,
        "updated": int(time.time()),
        "entries": clean_entries,
        "activity": [copy.deepcopy(x) for x in (activity or []) if isinstance(x, dict)][-_ACTIVITY_MAX:],
    }
    if isinstance(mission, dict):
        payload["mission"] = copy.deepcopy(mission)
    if isinstance(verification_debt, list):
        payload["verification_debt"] = [copy.deepcopy(x) for x in verification_debt
                                        if isinstance(x, dict)]
    save_json(_turn_journal_path(chat_id), payload)


def _update_task_anchor(chat: dict, user_content: Any) -> bool:
    """Persist the original ask and latest substantive instruction.

    This state is deterministic and model-independent: compaction may rewrite
    prose, but it cannot erase what the user asked or which task is active.
    Short acknowledgements such as "go ahead" deliberately do not replace the
    active task they refer to.
    """
    text = _task_anchor_text(user_content)
    if not text:
        return False
    old = chat.get("task_anchor")
    anchor = dict(old) if isinstance(old, dict) else {}
    changed = False
    if not anchor.get("original"):
        anchor["original"] = text[:_TASK_ANCHOR_ORIGINAL_CHARS]
        changed = True
    if not _TASK_ACK_RE.fullmatch(text):
        current = text[:_TASK_ANCHOR_CURRENT_CHARS]
        if anchor.get("current") != current:
            anchor["current"] = current
            changed = True
    elif not anchor.get("current"):
        anchor["current"] = anchor.get("original", "")
        changed = True
    if changed:
        anchor["updated"] = int(time.time())
        chat["task_anchor"] = anchor
    return changed


def _tool_activity_record(name: str, args: dict, result: Any) -> dict:
    """Small, non-sensitive progress breadcrumb derived from a tool result."""
    args = args if isinstance(args, dict) else {}
    target = ""
    for key in ("path", "url", "target", "host", "domain", "session_id"):
        val = args.get(key)
        if val not in (None, ""):
            target = re.sub(r"\s+", " ", str(val)).strip()[:180]
            break
    status = "ok"
    detail = ""
    if isinstance(result, dict):
        if result.get("error"):
            status = "error"
            detail = re.sub(r"\s+", " ", str(result.get("error"))).strip()[:180]
        elif result.get("ok") is False:
            status = "failed"
        elif result.get("exit_code") not in (None, 0):
            status = f"exit {result.get('exit_code')}"
        for key in ("note", "message", "summary", "report", "changed", "matches", "count"):
            if not detail and result.get(key) not in (None, "", [], {}):
                detail = re.sub(r"\s+", " ", str(result.get(key))).strip()[:180]
                break
    return {
        "tool": _resolve_tool_name(name),
        "status": status,
        "target": target,
        "detail": detail,
        "t": int(time.time()),
    }


def _validate_summary_output(text: str) -> tuple[bool, str]:
    """Reject malformed fold output before it replaces known-good state."""
    text = str(text or "").strip()
    if len(text) < _SUMMARY_MIN_OUTPUT_CHARS:
        return False, f"degenerate output ({len(text)} chars)"
    missing = [heading for heading in _SUMMARY_REQUIRED_HEADINGS if heading not in text]
    if missing:
        return False, "missing section(s): " + ", ".join(missing)
    positions = [text.index(heading) for heading in _SUMMARY_REQUIRED_HEADINGS]
    if positions != sorted(positions):
        return False, "sections are out of order"
    active = text.split("## ACTIVE TASK:", 1)[1].split("## COMPLETED:", 1)[0].strip()
    next_action = text.split("## NEXT ACTION:", 1)[1].strip()
    if not active or not next_action:
        return False, "active task or next action is empty"
    return True, "ok"


_context_trim_last_notice_by_chat: dict[str, float] = {}


def _chat_note(chat_id: str, text: str) -> dict:
    """Append a UI-only note to a chat's persisted history (never replayed to the model)."""
    if not chat_id or not text:
        return {"error": "chat_id and text are required"}
    chats = get_chats()
    chat = (chats.get("chats", {}) or {}).get(chat_id)
    if not chat:
        return {"error": "no such chat"}
    chat.setdefault("messages", [])
    chat["messages"].append({
        "role": "system", "content": str(text)[:2000],
        "t": int(time.time()), "_note": True,
    })
    if len(chat["messages"]) > CHAT_HISTORY_MAX:
        chat["messages"] = chat["messages"][-CHAT_HISTORY_MAX:]
    save_json(CHATS_FILE, chats)
    broadcast_event({"type": "chat:note", "chat_id": chat_id, "content": str(text)[:2000]})
    return {"ok": True}
