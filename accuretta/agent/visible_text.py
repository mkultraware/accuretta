# SPDX-License-Identifier: AGPL-3.0-only
"""What the user sees versus what the model thought: think markers, earlier
reasoning kept or dropped per model, visible answers and code, and messages
rendered for a summary.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import re
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.settings import get_settings
from accuretta.runtime.model_presets import _model_features
from typing import Any


def _call_digest(tc: dict) -> str:
    """A tool call with the arguments that say what it touched, so a summary
    can name the file or command behind each result."""
    fn = tc.get("function") or {}
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    shown = ", ".join(f"{k}={str(args[k])[:120]}"
                      for k in ("path", "command", "code", "pattern", "url", "query") if args.get(k))
    return f"{fn.get('name', '')}({shown})"


def _render_msgs_for_summary(slice_msgs: list[dict]) -> str:
    def _excerpt(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        half = max(1, (limit - 80) // 2)
        return text[:half] + "\n...[middle elided for compaction]...\n" + text[-half:]

    out = []
    for m in slice_msgs:
        role = m.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = str(c or "")
        if role == "assistant":
            # Never feed internal reasoning to the summarizer: a fold would turn
            # tool-protocol deliberation into established state for later turns.
            # Legacy marker-form reasoning is closed first; an unclosed block is
            # reasoning to the end.
            c = _close_think_marker(c)
            c = re.sub(r"<\s*think\s*>[\s\S]*?<\s*/\s*think\s*>", " ", c, flags=re.IGNORECASE)
            c = re.sub(r"<\s*think\s*>[\s\S]*$", " ", c, flags=re.IGNORECASE)
            c = re.sub(r"<\s*/\s*think\s*>", " ", c, flags=re.IGNORECASE)
        if role == "tool":
            c = "[tool result] " + _excerpt(c, 4000)
        elif m.get("tool_calls"):
            names = ", ".join(_call_digest(tc) for tc in m.get("tool_calls", []))
            c = (_excerpt(c, 1600) + f" [called tools: {names}]").strip()
        else:
            c = _excerpt(c, 2400 if role == "user" else 1600)
        if c.strip():
            out.append(f"{role}: {c}")
    return "\n".join(out)


def _take_summary_chunk(msgs: list[dict], budget: int) -> int:
    """How many leading messages render within `budget` chars (at least one).
    Never ends a chunk right before a tool result, so a call and its results
    are summarized together."""
    used = 0
    take = 0
    for m in msgs:
        line = _render_msgs_for_summary([m])
        cost = len(line) + 1 if line else 0
        if take and used + cost > budget:
            break
        used += cost
        take += 1
    while 1 < take < len(msgs) and msgs[take].get("role") == "tool":
        take -= 1
    return take


from accuretta.core.visible_reply import (  # noqa: F401  re-export: moved 2026-11-23
    _CHAT_CODE_FENCE_RE,
    _visible_assistant_text,
    _live_turn_messages,
    _recent_visible_code_blocks,
    _CODE_FAMILIES,
    _CODE_FAMILY_OF,
    _code_only_in_thinking,
    _visible_code_source,
    _THINK_MARKER_RE,
    _close_think_marker,
    _LEADING_THINK_RE,
    _split_leading_think,
)


def _visible_final_answer(text: str) -> str:
    """What the user can actually see after reasoning/tool markup is removed."""
    return _strip_unparsed_tool_markup(_visible_assistant_text(text)).strip()


def _steer_kept_partial(reasoning_text: str, answer_text: str) -> dict | None:
    """The reply the model had written when the user's update arrived, kept so
    the next request carries on from it instead of starting over (the owner's
    choice, 2026-11-20). Only what the user could see is kept: an unfinished tool
    call is dropped (it never ran), and a reply that was still only thinking
    returns None, since there is nothing to carry on from."""
    answer = _strip_unparsed_tool_markup(answer_text or "")
    if not _visible_final_answer(answer):
        return None
    return {"role": "assistant", "content": (reasoning_text or "") + answer,
            "_steer_partial": True}


def _strip_unparsed_tool_markup(text: str) -> str:
    """Remove leaked tool syntax before an assistant bubble is persisted."""
    if not text:
        return ""
    cleaned = re.sub(
        r"(?:<|&lt;|\\<)\s*tool_call(?:>|&gt;|\\>)?[\s\S]*?"
        r"(?:(?:<|&lt;|\\<)\s*/\s*tool_call(?:>|&gt;|\\>)?|$)",
        "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"```(?:tool_call|tool_code)[\s\S]*?(?:```|$)",
                     "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _is_invalid_call_result(content: Any) -> bool:
    """True for the result of a call that never ran because its arguments
    were missing or malformed."""
    if not isinstance(content, str) or '"not_executed"' not in content:
        return False
    try:
        result = json.loads(content)
    except ValueError:
        return False
    return (isinstance(result, dict) and bool(result.get("not_executed"))
            and bool(result.get("missing_arguments") or result.get("invalid_path")))


def _drop_invalid_call_pairs(msgs: list[dict]) -> list[dict]:
    """Messages to save for replay, without calls that never ran for lack of
    valid arguments and without their error results.

    A model copies the calls it sees in its history, so a saved run of empty
    calls teaches the next turn to send more. The activity ledger still records
    each failure. Results are matched to the assistant message before them,
    because text-parsed call ids repeat across rounds."""
    drop: set[tuple[int, str]] = set()
    owner = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            owner = i
        elif m.get("role") == "tool" and owner >= 0 and _is_invalid_call_result(m.get("content")):
            drop.add((owner, str(m.get("tool_call_id") or "")))
    if not drop:
        return list(msgs)
    out: list[dict] = []
    owner = -1
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role == "assistant":
            owner = i
            calls = m.get("tool_calls") or []
            kept = [tc for tc in calls if (i, str(tc.get("id") or "")) not in drop]
            if len(kept) != len(calls):
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                if kept:
                    m["tool_calls"] = kept
                elif not _visible_final_answer(m.get("content") or ""):
                    continue
        elif role == "tool" and (owner, str(m.get("tool_call_id") or "")) in drop:
            continue
        out.append(m)
    return out


def _graceful_server_failure(conversation: list[dict], start_len: int,
                             turn_id: str, reasons: list[str]) -> dict:
    """Build a final result that keeps the turn's completed tool work when
    llama-server faults. The caller persists `_appended_intermediate`, so the
    evidence survives and the next turn can continue from a sane state."""
    notes = " ".join(reasons) or "llama-server failed mid-turn"
    return {
        "role": "assistant",
        "content": f"(response lost — {notes})",
        "_appended_intermediate": _drop_invalid_call_pairs(conversation[start_len:]),
        "_build": "server-fault-v1",
        "_turn_id": turn_id,
        "_server_fault": True,
    }


# Rewrite prior assistant <think>...</think> blocks as plain-text short-term
# memory notes so they survive templates that drop earlier reasoning (older
# Qwen3 and DeepSeek templates do; Qwen3.5+ templates keep it). The wire marker
# stays `[scratchpad-from-earlier-turn]` so older chat histories keep rendering.
_PRIOR_THINK_RE = re.compile(r"<think>([\s\S]*?)</think>", re.IGNORECASE)


# With preserve_prior_thinking off, strip old <think> blocks ourselves:
# Qwen3.5-family templates (and llama.cpp's reasoning-preserve default) replay
# every prior <think> verbatim, 31% of a real 91K-token agent prompt.
#
# Reasoning is stripped only from tasks the user has moved on from: every
# assistant message since the user's latest real message keeps it. Stripping
# within a task (v1.0.0 kept just the newest round) rewrote the previous
# round's message on every round. On a plain transformer llama.cpp re-prefills
# from that point, a few hundred tokens. Hybrid models (Qwen3.5/3.6/3.8, whose
# recurrent state cannot rewind) fall back to the last context checkpoint
# instead, 8K+ tokens apart by default: measured 6-10x more prompt processing
# per long agent turn. Now the prompt only grows within a task, and the
# reprocess happens once, when the user sends a new message.
#
# Mid-task messages the harness injects (automatic notes, recovery and loop
# guard prompts, thinking notes, steering updates) are not a new task.
_SYNTHETIC_USER_PREFIXES = ("[automatic", "[loop guard]")


def _is_task_boundary(m: dict) -> bool:
    """True for a real user message, the start of a new task."""
    if not isinstance(m, dict) or m.get("role") != "user":
        return False
    if m.get("_thinking_note") or m.get("_steering_id") or m.get("_synthetic"):
        return False
    content = m.get("content")
    if isinstance(content, list):
        content = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return not str(content or "").lstrip().startswith(_SYNTHETIC_USER_PREFIXES)


def _strip_prior_thinking(text: str) -> str:
    if not text or "<think>" not in text.lower():
        return text
    return _PRIOR_THINK_RE.sub("", text).lstrip()


def _prior_thinking_mode(settings: dict | None = None) -> str:
    """How earlier assistant reasoning is sent: "native" (verbatim <think>
    blocks, for models whose template renders them back as reasoning),
    "scratchpad" (rewritten as plain text, the global preserve setting) or
    "strip" (removed, the default)."""
    try:
        s = settings if settings is not None else get_settings()
        model_path = str(s.get("model_path") or get_settings().get("model_path") or "")
        if "native_prior_thinking" in _model_features(model_path):
            return "native"
        return "scratchpad" if s.get("preserve_prior_thinking", False) else "strip"
    except Exception:
        return "scratchpad"


def _thinking_wire_view(msgs: list[dict], settings: dict | None = None,
                        keep_reasoning_from: dict | None = None) -> list[dict]:
    """What the model is actually sent for prior reasoning, as message copies.

    The context budget (trim/fold decisions) and the payload both go through
    this, so the fill they measure matches the prompt. The stored conversation
    — the compaction summarizer's input — is never changed.

    `keep_reasoning_from` is a reasoning cleanup's cut point (see
    _reasoning_cleanup_anchor): an assistant message from `msgs` before which
    reasoning is stripped even inside the current task."""
    # Cut point by identity in the caller's list; normalizing below copies
    # some messages, and the positions stay one-to-one.
    cut = (next((i for i, m in enumerate(msgs) if m is keep_reasoning_from), -1)
           if keep_reasoning_from is not None else -1)
    # Every assistant message goes out with real <think>…</think> tags (never
    # the bare marker), whether prior reasoning is then stripped or preserved.
    normalized = []
    for m in msgs:
        if m.get("role") == "assistant" and isinstance(m.get("content"), str):
            closed = _close_think_marker(m["content"])
            if closed != m["content"]:
                m = dict(m, content=closed)
        normalized.append(m)
    if any(a is not b for a, b in zip(normalized, msgs)):
        msgs = normalized
    if _prior_thinking_mode(settings) != "strip":
        return msgs
    # Keep reasoning from the user's latest real message on (see
    # _SYNTHETIC_USER_PREFIXES); strip it from earlier tasks, and from before a
    # reasoning cleanup's cut point.
    keep_from = max((i for i, m in enumerate(msgs) if _is_task_boundary(m)), default=-1)
    keep_from = max(keep_from, cut)
    if keep_from <= 0:
        return msgs
    out = []
    for i, m in enumerate(msgs):
        if i < keep_from and m.get("role") == "assistant" and isinstance(m.get("content"), str):
            stripped = _strip_prior_thinking(m["content"])
            if stripped != m["content"]:
                m = dict(m, content=stripped)
        out.append(m)
    return out
