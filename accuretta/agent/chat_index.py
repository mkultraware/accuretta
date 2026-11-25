# SPDX-License-Identifier: AGPL-3.0-only
"""The chat list: the summary index, the retention limit on old chats, and a title made
from the first prompt.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.agent.rolling_summary import _log_compact
from accuretta.core.chat_events import _chat_emitters
from accuretta.core.chats import get_chats
from accuretta.security.approval_list import list_approvals


def get_chat_index() -> dict:
    snapshot = get_chats()
    # The rail sorts by recency, not creation: `order` tracks creation only, so a
    # session created days ago but updated minutes ago would sit mid-list. The
    # sort is stable, so ties keep their relative order.
    order = sorted(
        snapshot["order"],
        key=lambda cid: int((snapshot["chats"].get(cid) or {}).get("updated") or 0),
        reverse=True,
    )
    index: dict[str, dict] = {}
    for chat_id in order:
        record = snapshot["chats"].get(chat_id)
        if not isinstance(record, dict):
            continue
        summary = {key: value for key, value in record.items() if key != "messages"}
        summary["_summary"] = True
        summary["task_state"] = _chat_task_state(chat_id, record)
        index[chat_id] = summary
    return {"chats": index, "order": order}


def _chat_task_state(chat_id: str, chat: dict) -> str:
    if any(a.get("chat_id") == chat_id and a.get("status") == "pending" for a in list_approvals()):
        return "needs-you"
    if chat_id in _chat_emitters:
        return "working"
    messages = chat.get("messages", [])
    latest = next((m for m in reversed(messages) if m.get("role") == "assistant" and not m.get("tool_calls")), {})
    recovery = chat.get("turn_recovery") or {}
    if recovery and recovery.get("recovered_at", 0) >= latest.get("t", 0):
        return "interrupted"
    if latest.get("_stopped"):
        return "stopped"
    if latest.get("_failed"):
        return "failed"
    return "finished" if latest else "ready"


# Disk-hygiene cap on messages kept per chat in chats.json (the request-time
# trimmer is what bounds the model's context). Retention is anchored on the
# first user message, the task statement.
CHAT_HISTORY_MAX = 1000


def _enforce_chat_retention(chat: dict) -> None:
    msgs = chat.get("messages") or []
    if len(msgs) <= CHAT_HISTORY_MAX:
        return
    # Retention may remove only history the rolling summary has already absorbed;
    # one autonomous turn can exceed this soft cap before its next fold, and
    # dropping unsummarized messages loses them for good.
    first_user_idx = next(
        (i for i, m in enumerate(msgs) if m.get("role") == "user"),
        None,
    )
    overflow = len(msgs) - CHAT_HISTORY_MAX
    through = max(0, min(int(chat.get("summary_through", 0) or 0), len(msgs)))
    if through <= 0:
        _log_compact(chat.get("id") or "",
                     f"retention deferred: n={len(msgs)} cap={CHAT_HISTORY_MAX} unsummarized=all")
        return

    eligible = [i for i in range(through) if i != first_user_idx]
    drop_n = min(overflow, len(eligible))
    if drop_n:
        drop_idx = set(eligible[:drop_n])
        chat["messages"] = [m for i, m in enumerate(msgs) if i not in drop_idx]
        chat["summary_through"] = max(0, through - drop_n)
    if len(chat["messages"]) > CHAT_HISTORY_MAX:
        _log_compact(chat.get("id") or "",
                     f"retention deferred: n={len(chat['messages'])} cap={CHAT_HISTORY_MAX} "
                     f"unsummarized={len(chat['messages']) - int(chat.get('summary_through', 0) or 0)}")


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "to", "of", "in", "on",
    "at", "for", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "do", "does", "did", "have", "has", "had", "you", "your", "me", "my",
    "i", "we", "us", "our", "it", "its", "this", "that", "these", "those", "can",
    "could", "would", "should", "will", "just", "please", "hey", "hi", "hello",
    "some", "any", "all", "only", "also", "much", "more", "most", "very", "how",
    "what", "when", "where", "why", "which", "who", "whom", "whose", "make",
    "create", "build", "need", "want", "like", "get", "got", "put", "see", "tell",
    "say", "know", "good", "bad", "new", "old", "here", "there", "them", "now",
}


def _title_from_prompt(text: str, max_len: int = 44) -> str:
    """Produce a concise, readable chat title from the first user message.
    Grabs the most distinctive keywords; falls back to a truncation of the raw
    text if nothing useful remains after filtering."""
    if not text:
        return "new session"
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)        # drop code fences
    cleaned = re.sub(r"https?://\S+", " ", cleaned)        # drop urls
    cleaned = re.sub(r"[^\w\s\-']", " ", cleaned)          # keep letters/digits
    words = [w for w in cleaned.split() if w]
    keywords: list[str] = []
    seen: set[str] = set()
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS:
            continue
        if len(lw) < 2:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        keywords.append(w)
    if not keywords:
        fallback = " ".join(words[:6]) or text.strip()
        return (fallback[: max_len - 1].rstrip() + "…") if len(fallback) > max_len else fallback
    title = " ".join(keywords[:6])
    if len(title) > max_len:
        title = title[: max_len - 1].rstrip() + "…"
    return title.lower()
