# SPDX-License-Identifier: AGPL-3.0-only
"""Folding old history into the rolling summary: when to fold, where to cut, failure
backoff, the mid-turn fold, folds in flight, and the compact_history tool.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import threading
import time
from accuretta.agent.compaction import _build_compaction_memory
from accuretta.agent.compaction import _reconcile_summary_fidelity
from accuretta.agent.context_estimate import _TOOL_RESULT_KEEP_RECENT
from accuretta.agent.context_estimate import _estimate_context_tokens
from accuretta.agent.rolling_summary import _log_compact
from accuretta.agent.rolling_summary import _update_rolling_summary
from accuretta.agent.token_budget import _count_msg_tokens
from accuretta.agent.turn_journal import _refresh_continuity_state
from accuretta.agent.user_anchor import _ensure_user_anchor
from accuretta.agent.visible_text import _thinking_wire_view
from accuretta.agent.wire_replay import _replay_wire_msg
from accuretta.core.cancels import _chat_cancels
from accuretta.core.cancels import _chat_cancels_lock
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.data_files import FOLD_EVENTS_FILE
from accuretta.core.events import broadcast_event
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.llama_http import _conversation_token_scale
from accuretta.runtime.llama_process import _last_prompt_tokens_by_chat
from accuretta.runtime.llama_process import _llama_props_ctx


def _payload_msg_cost(m: dict, idx: int, stub_cutoff: int,
                      stub_old_tools: bool = True) -> int:
    """Token cost of message `m` as sent.

    In the bounded fallback path, old tool messages become ~12-token stubs. In
    rolling-summary mode they remain full until folded. Every caller declares
    which wire shape it is budgeting so trimming and llama see the same cost.
    """
    if stub_old_tools and m.get("role") == "tool" and idx < stub_cutoff:
        return 12
    return _count_msg_tokens(m)


# ---- rolling conversation summary -----------------------------------------
# Rather than letting truncate_messages delete the middle of a long chat (and
# the model repeat mistakes it already fixed), the oldest turns are folded into
# a dense summary in the system message. Recent turns stay raw. Fractions are
# of the live context limit.
#
# Cache cost: a fold changes the prompt prefix, so llama.cpp must reprocess the
# whole window on that turn (seconds at large ctx). The trigger therefore sits
# near the real limit, making compaction a rare safety net rather than a
# routine tax; truncate_messages is the hard backstop below it.
_SUMMARY_MIN_KEEP = 6          # always keep at least this many recent messages raw


_SUMMARY_MIN_KEEP_HARD = 2     # absolute floor when recent messages are giant (token-aware fold)


_SUMMARY_TRIGGER_FRAC = 0.85


_SUMMARY_KEEP_FRAC = 0.10     # aggressively fold most of the history, keeping only the recent tail raw


def _summary_trigger_limit(ctx_limit: int) -> int:
    """Pre-turn fold threshold (real prompt tokens, incl. system + tools).

    The trimmer's ceiling is ctx - reserve, so a fixed 0.85 trigger was
    unreachable whenever reserve + tool overhead exceeded 15% of ctx and every
    overflow became a mid-turn fold. Fire just under the trimmer's ceiling
    (tools estimated high, so slightly early, never late), capped at 0.85."""
    reserve = max(min(int(ctx_limit * 0.25), 16000), 1024)
    ceiling = ctx_limit - reserve - 8192 - 768
    return min(int(ctx_limit * _SUMMARY_TRIGGER_FRAC),
               max(int(ctx_limit * 0.50), ceiling))


# After a summarizer failure, don't retry for this long — otherwise a wedged
# llama-server makes every subsequent round eat a 90s timeout mid-turn.
_SUMMARY_FAIL_BACKOFF_S = 180


_summary_last_fail_by_chat: dict[str, float] = {}


_summary_failure_seq_by_chat: dict[str, int] = {}


# Automatic folds can legitimately happen more than once during a very long
# unattended turn, but each fold does not need its own pair of UI messages.
# Keep full telemetry while rate-limiting only user-facing automatic notices.
# Manual compaction always remains visible.
_SUMMARY_NOTICE_INTERVAL_S = 300


_summary_last_notice_by_chat: dict[str, float] = {}


_summary_fold_lock = threading.Lock()


_summary_folds_inflight: set[str] = set()


def list_inflight_folds() -> list[str]:
    """Chat ids with a summarizer call currently in flight. The SSE handler
    re-emits these as `summary_folding` on connect: the start event is
    transient, so a reconnect would otherwise never redraw the live row."""
    with _summary_fold_lock:
        return sorted(_summary_folds_inflight)


def _summary_failure_backoff_active(chat_id: str) -> bool:
    """True while automatic compaction is cooling down after a final failure."""
    last_failure = _summary_last_fail_by_chat.get(chat_id, 0)
    return bool(last_failure and time.time() - last_failure < _SUMMARY_FAIL_BACKOFF_S)


_fold_events_lock = threading.Lock()


def _record_fold_event(chat: dict, reason: str, force: bool, ok: bool,
                       outcome: str, ctx_limit: int, fill: int | None,
                       through: int, cut: int | None,
                       folded_tokens: int | None = None,
                       repairs: list | None = None) -> None:
    """Append one compaction-attempt record. Never raises — telemetry must
    not break the fold path it observes."""
    try:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "chat_id": chat.get("id") or "",
            "title": chat.get("title", ""),
            "reason": reason,
            "force": force,
            "ok": ok,
            "outcome": outcome,
            "fill": fill,
            "fill_frac": round(fill / ctx_limit, 4) if (fill or 0) > 0 else None,
            "ctx": ctx_limit,
            "through": through,
            "cut": cut,
            "folded": (cut - through) if (ok and cut is not None) else 0,
            "folded_tokens": folded_tokens,
            "summary_chars": len(chat.get("rolling_summary", "") or ""),
            "repairs": list(repairs or []),
        }
        with _fold_events_lock:
            with open(FOLD_EVENTS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _align_fold_cut(msgs: list[dict], through: int, cut: int) -> int:
    """Keep the summary boundary out of an assistant/tool transaction.

    If the proposed raw tail starts with a tool result, prefer folding the rest
    of that result batch too. When doing so would leave too little recent raw
    history, keep the parent assistant call and the complete result batch raw.
    This prevents a successful fold from manufacturing orphan cleanup and from
    losing a result that landed on the opposite side of the summary boundary.
    """
    if cut <= through or cut >= len(msgs) or msgs[cut].get("role") != "tool":
        return cut
    forward = cut
    while forward < len(msgs) and msgs[forward].get("role") == "tool":
        forward += 1
    if len(msgs) - forward >= _SUMMARY_MIN_KEEP_HARD:
        return forward
    backward = cut - 1
    while backward > through and msgs[backward].get("role") == "tool":
        backward -= 1
    if msgs[backward].get("role") == "assistant":
        return backward
    return cut


def _maybe_roll_summary_unlocked(chat: dict, ctx_limit: int, force: bool = False,
                                 reason_hint: str = "auto") -> bool:
    """Fold the oldest un-kept turns into chat['rolling_summary'], but only when
    the conversation actually approaches the context window.

    `force=True` skips the fill gate; manual compaction (compact_history tool,
    /api/compact) uses it to fold at a comfortable 60-80% before the auto
    trigger makes it an emergency. Mutates chat in place; returns True if the
    summary advanced."""
    msgs = chat.get("messages", [])
    through = int(chat.get("summary_through", 0) or 0)
    tail = msgs[through:]
    if len(tail) <= _SUMMARY_MIN_KEEP:
        _record_fold_event(chat, reason_hint, force, False, "tail_too_small",
                           ctx_limit, _last_prompt_tokens_by_chat.get(chat.get("id") or "", 0),
                           through, None)
        return False
    chat_id = chat.get("id") or ""
    # Manual compaction is an explicit retry, so let it bypass the automatic
    # cooldown. Automatic and mid-turn attempts stay blocked to prevent a
    # broken summarizer from stalling every model round.
    if reason_hint != "manual" and _summary_failure_backoff_active(chat_id):
        _record_fold_event(chat, reason_hint, force, False, "fail_backoff",
                           ctx_limit, _last_prompt_tokens_by_chat.get(chat_id, 0),
                           through, None)
        return False
    # Fill signal: llama-server's real prompt_eval_count from this chat's last
    # turn (system + tools + history). The char estimate is only the fallback for
    # a chat that has not run a turn yet.
    real_fill = _last_prompt_tokens_by_chat.get(chat_id, 0)
    if not force:
        # Trigger just under the trimmer's ceiling (see _summary_trigger_limit):
        # the fold must win the race against truncate_messages, or the trimmer
        # drops the middle and forces mid-turn folds on every overflow.
        trigger_limit = _summary_trigger_limit(ctx_limit)
        # A stale or missing fill map (bridge restart, edited chat, first message) must
        # not skip a fold the stored conversation needs, so take the max with the
        # char-based tail estimate; both are roughly the same units and the trigger has
        # ~2K of slack under the trimmer ceiling. Old tool results stay live until a
        # fold preserves them, so count their full cost rather than the age-based stub.
        # Old <think> blocks are stripped on the wire, so cost the wire view; the
        # stored tail would fold at ~59% real fill on reasoning-heavy chats.
        _stub_cutoff = len(msgs) - _TOOL_RESULT_KEEP_RECENT
        wire_tail = _thinking_wire_view(tail)
        tail_units = sum(_payload_msg_cost(m, through + i, _stub_cutoff,
                                           stub_old_tools=False)
                         for i, m in enumerate(wire_tail))
        tail_tokens = tail_units * _conversation_token_scale(wire_tail)
        if max(real_fill, tail_tokens) <= trigger_limit:
            return False  # plenty of headroom — don't summarize yet
    # Fold oldest turns until the remaining RAW tail is ~KEEP_FRAC of ctx. Walk
    # newest-first, keeping recent turns until the keep budget fills; the rest
    # (older) get folded into the summary.
    keep_budget = ctx_limit * _SUMMARY_KEEP_FRAC
    kept, cut = 0, through
    for i in range(len(msgs) - 1, through - 1, -1):
        kept += _count_msg_tokens(msgs[i])
        if kept >= keep_budget:
            cut = i
            break
    # never fold so far that fewer than MIN_KEEP recent messages remain raw
    cut = min(cut, len(msgs) - _SUMMARY_MIN_KEEP)
    # Token-aware floor: MIN_KEEP recent messages can be giant tool results
    # that exceed the whole keep budget, leaving almost no runway after the
    # fold. Fold further (hard floor _SUMMARY_MIN_KEEP_HARD) until the kept
    # tail is back near the token budget.
    tail_kept = sum(_count_msg_tokens(m) for m in msgs[cut:])
    while cut < len(msgs) - _SUMMARY_MIN_KEEP_HARD and tail_kept > keep_budget:
        tail_kept -= _count_msg_tokens(msgs[cut])
        cut += 1
    cut = _align_fold_cut(msgs, through, cut)
    if cut <= through:
        # The trigger fired but the raw tail never reached keep_budget: the system
        # prompt and tool schemas, not chat volume, fill the window. Bailing out here
        # would let truncate_messages delete the middle instead, so fold down to the
        # minimum raw keep anyway.
        cut = len(msgs) - _SUMMARY_MIN_KEEP
        if cut <= through:
            _record_fold_event(chat, reason_hint, force, False, "cannot_advance",
                               ctx_limit, real_fill, through, None)
            return False
    cut = _align_fold_cut(msgs, through, cut)
    if cut <= through:
        _record_fold_event(chat, reason_hint, force, False, "cannot_align_boundary",
                           ctx_limit, real_fill, through, None)
        return False
    # Every real summarizer run emits a lifecycle event so the client can show a
    # temporary inline status row (mostly for mid-turn folds, where the model
    # would otherwise look frozen). Nothing is persisted to chat content.
    _now = time.time()
    _notify = True
    if reason_hint == "manual":
        _summary_last_notice_by_chat[chat_id] = _now
    folded_messages = msgs[through:cut]
    candidate_memory = _build_compaction_memory(chat, folded_messages)
    _fold_outcome: dict = {}
    new_summary = _update_rolling_summary(
        chat.get("rolling_summary", ""),
        folded_messages,
        chat_id=chat_id,
        notify=_notify,
        ctx_limit=ctx_limit,
        compaction_memory=candidate_memory,
        outcome=_fold_outcome,
    )
    if new_summary is None:
        _summary_last_fail_by_chat[chat_id] = time.time()
        _summary_failure_seq_by_chat[chat_id] = (
            _summary_failure_seq_by_chat.get(chat_id, 0) + 1
        )
        _record_fold_event(chat, reason_hint, force, False, "summarize_failed",
                           ctx_limit, real_fill, through, cut)
        return False
    # Fidelity gate: restore anything the new note dropped versus the prior note
    # before the flash commits. Add-only and wrapped so it can never break the
    # fold. Every fold path funnels through here.
    _fidelity_repairs: list = []
    if get_settings().get("summary_fidelity_gate", True):
        try:
            new_summary, _fidelity_repairs = _reconcile_summary_fidelity(
                chat.get("rolling_summary", ""), new_summary,
                candidate_memory, chat.get("pins") or [])
            if _fidelity_repairs:
                _log_compact(chat_id, "fidelity gate restored: " + ", ".join(_fidelity_repairs))
        except Exception as _fe:
            try:
                _log_compact(chat_id, f"fidelity gate error (ignored): {type(_fe).__name__}: {_fe}")
            except Exception:
                pass
            _fidelity_repairs = []
    if _fold_outcome.get("model_fallbacks"):
        _fidelity_repairs = list(_fidelity_repairs) + [
            f"model_fallback:{_fold_outcome['model_fallbacks']}"]
    # Commit whenever a note came back, even one identical to the prior: the
    # folded messages leave the raw history either way. Declining an unchanged
    # note left the same slice to be re-folded every round while the trimmer
    # cut the middle instead.
    if new_summary:
        _summary_last_fail_by_chat.pop(chat_id, None)
        chat["rolling_summary"] = new_summary
        chat["summary_through"] = cut
        chat["compaction_memory"] = candidate_memory
        _refresh_continuity_state(chat)
        # Persistent fold marker for the UI's inline divider at this boundary. It
        # survives reloads; _enforce_chat_retention keeps summary_through aligned with
        # the same message list, so the marker stays in place.
        _fold_tokens = int(sum(_count_msg_tokens(m) for m in msgs[through:cut]))
        chat["fold_mark"] = {
            "through": cut,
            "folded": cut - through,
            "folded_tokens": _fold_tokens,
            "summary_chars": len(new_summary),
            "t": int(time.time()),
        }
        # Refresh the per-chat fill right away so the gauge drops after a fold instead
        # of waiting for the next round's stats event.
        try:
            # Pass the live chat: the on-disk copy is still pre-fold until the caller
            # saves, and that stale estimate re-fired the proactive mid-turn fold.
            _post_fold = _estimate_context_tokens(chat_id, chat=chat)
            if _post_fold > 0:
                _last_prompt_tokens_by_chat[chat_id] = _post_fold
        except Exception:
            pass
        _record_fold_event(chat, reason_hint, force, True, "ok", ctx_limit, real_fill,
                           through, cut, _fold_tokens, repairs=_fidelity_repairs)
        try:
            if _notify:
                broadcast_event({"type": "summary_folded", "chat_id": chat_id,
                                 "folded": cut - through, "through": cut,
                                 "folded_tokens": _fold_tokens,
                                 "summary_chars": len(new_summary)})
        except Exception:
            pass
        return True
    _record_fold_event(chat, reason_hint, force, False, "no_change",
                       ctx_limit, real_fill, through, cut)
    try:
        if _notify:
            broadcast_event({"type": "summary_fold_finished", "chat_id": chat_id,
                             "outcome": "no_change"})
    except Exception:
        pass
    return False


def _maybe_roll_summary(chat: dict, ctx_limit: int, force: bool = False,
                        reason_hint: str = "auto") -> bool:
    """Serialize folds per chat so duplicate requests cannot race the pointer.

    Two summarizer calls over the same `summary_through` boundary (UI double
    submit, web/Discord overlap) could move the pointer backward or fold twice.
    The lock is per chat, so unrelated chats still compact in parallel."""
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return _maybe_roll_summary_unlocked(chat, ctx_limit, force, reason_hint)
    with _summary_fold_lock:
        if chat_id in _summary_folds_inflight:
            _record_fold_event(
                chat, reason_hint, force, False, "already_inflight", ctx_limit,
                _last_prompt_tokens_by_chat.get(chat_id, 0),
                int(chat.get("summary_through", 0) or 0), None,
            )
            return False
        _summary_folds_inflight.add(chat_id)
    try:
        return _maybe_roll_summary_unlocked(chat, ctx_limit, force, reason_hint)
    finally:
        with _summary_fold_lock:
            _summary_folds_inflight.discard(chat_id)


# Matches the "=== EARLIER IN THIS SESSION ===" block both call sites splice
# into the system prompt (web and discord use slightly different wording —
# the [^)]* covers both). Non-greedy up to the next instruction block.
_SUMMARY_SECTION_RE = re.compile(
    r"\n\n=== EARLIER IN THIS SESSION \(older turns condensed to save context;[^)]*\) ===\n"
    r"[\s\S]*?(?=\n\n(?:When you fix a bug|TOOL CALLING)|$)"
)


def _splice_rolling_summary(system_content: str, new_summary: str) -> str:
    """Replace (or append) the rolling-summary section of an assembled system
    prompt, for a fold that happens mid-turn. The replacement is a callable so
    backslashes in the summary (Windows paths, regexes) stay literal; a string
    replacement is parsed as a template and raises "bad escape"."""
    section = ""
    if new_summary:
        section = (
            "\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; "
            "treat these as established facts — do not redo work or re-ask) ===\n" + new_summary
        )
    if _SUMMARY_SECTION_RE.search(system_content):
        return _SUMMARY_SECTION_RE.sub(lambda _m: section, system_content)
    return system_content + section


def _mid_turn_fold(chat_id: str, conversation: list[dict], start_len: int,
                   ctx_limit: int, replay_vision: bool):
    """Compaction inside the agentic loop.

    The rolling summary otherwise only advances between user turns, so a long
    unattended turn that fills the window would hit truncate_messages, lose its
    progress and loop. Instead: persist this turn's working memory, advance the
    summary, and rebuild the conversation from the new fold boundary.

    Returns {"conversation", "start_len", "folded"} on success, or a result with
    conversation=None when the fold did not advance; failed=True there means the
    summarizer failed or is in cooldown, so the caller must stop rather than trim
    older context. Returns None when there is no stored chat."""
    if not chat_id:
        return None
    chats = get_chats()
    chat = chats.get("chats", {}).get(chat_id)
    if not chat:
        return None
    chat.setdefault("id", chat_id)
    old_through = int(chat.get("summary_through", 0) or 0)
    # Persist this turn's intermediates first — the fold operates on the
    # STORED message list, and this turn's tool work is part of that history.
    # Same persisted shape the chat handler uses at end-of-turn.
    now_t = int(time.time())
    for im in conversation[start_len:]:
        role = im.get("role")
        # A mid-turn update from the user ("stay local") belongs to this turn's
        # history too; dropping it would erase it from the rebuilt conversation.
        if role not in ("assistant", "tool") and not (role == "user" and im.get("_steering_id")):
            continue
        persisted: dict = {"role": role, "content": im.get("_steering_text", im.get("content", "")) or "",
                           "t": now_t}
        if im.get("_steering_id"):
            persisted["_steering_id"] = im["_steering_id"]
            persisted["images"] = im.get("_steering_images", [])
        if role == "assistant":
            persisted["_internal"] = True
            if im.get("_steer_partial"):
                persisted["_steer_partial"] = True   # shown: the user saw it written
            if im.get("tool_calls"):
                persisted["tool_calls"] = im["tool_calls"]
        else:
            if im.get("tool_call_id"):
                persisted["tool_call_id"] = im["tool_call_id"]
            if im.get("name"):
                persisted["name"] = im["name"]
        chat["messages"].append(persisted)
    persisted_upto = len(conversation)
    failure_seq_before = _summary_failure_seq_by_chat.get(chat_id, 0)
    failure_backoff_before = _summary_failure_backoff_active(chat_id)
    # force=True: this path is only reached after the trimmer dropped messages,
    # which is itself the overflow signal (the 0.85 gate is unreachable here).
    if not _maybe_roll_summary(chat, ctx_limit, force=True, reason_hint="mid_turn"):
        save_json(CHATS_FILE, chats)
        return {
            "conversation": None,
            "start_len": persisted_upto,
            "folded": 0,
            "failed": (failure_backoff_before or
                       _summary_failure_seq_by_chat.get(chat_id, 0) > failure_seq_before),
        }
    save_json(CHATS_FILE, chats)
    new_through = int(chat.get("summary_through", 0) or 0)
    sys_content = ""
    if conversation and conversation[0].get("role") == "system":
        sys_content = conversation[0].get("content", "") or ""
    rebuilt: list[dict] = [{
        "role": "system",
        "content": _splice_rolling_summary(sys_content, chat.get("rolling_summary", "")),
    }]
    for m in chat["messages"][new_through:]:
        out = _replay_wire_msg(m, replay_vision)
        if out is not None:
            rebuilt.append(out)
    rebuilt = _ensure_user_anchor(rebuilt, chat["messages"], new_through)
    return {
        "conversation": rebuilt,
        "start_len": len(rebuilt),
        "folded": new_through - old_through,
        "continuity": dict(chat.get("continuity") or {}),
        "compaction_memory": dict(chat.get("compaction_memory") or {}),
    }


def _compact_chat(chat_id: str, force: bool = True,
                  reason_hint: str = "manual") -> dict:
    """Manually fold a chat's oldest turns into its rolling summary, regardless of
    how close the context is to the auto trigger. Shared by the compact_history
    tool and /api/compact; folding at a task boundary beats waiting for the
    overflow. Returns stats, or {"error": ...} when there's nothing to fold."""
    if not chat_id:
        return {"error": "no chat_id"}
    if reason_hint == "manual":
        with _chat_cancels_lock:
            if chat_id in _chat_cancels:
                return {"error": "this session is currently working; let the active turn compact itself or stop it before compacting manually"}
    chats = get_chats()
    chat = chats.get("chats", {}).get(chat_id)
    if not chat:
        return {"error": "chat not found"}
    chat.setdefault("id", chat_id)
    old_through = int(chat.get("summary_through", 0) or 0)
    msgs = chat.get("messages", [])
    if len(msgs) - old_through <= _SUMMARY_MIN_KEEP + 2:
        return {"note": "history is already compact — nothing left to fold",
                "summary_through": old_through, "folded": 0}
    ctx_limit = _llama_props_ctx() or int(get_settings().get("num_ctx") or 32768)
    if not _maybe_roll_summary(chat, ctx_limit, force=force, reason_hint=reason_hint):
        return {"error": "compaction did not run (summarizer call failed or no foldable history)",
                "summary_through": old_through, "folded": 0}
    save_json(CHATS_FILE, chats)
    new_through = int(chat.get("summary_through", 0) or 0)
    return {"folded": new_through - old_through, "summary_through": new_through,
            "note": "older turns condensed into the session summary"}


def tool_compact_history(args: dict) -> dict:
    """Model-driven compaction: fold the oldest turns into the rolling summary now,
    e.g. at a task boundary, instead of waiting for the 0.85 auto trigger."""
    cid = _get_current_chat()
    if not cid:
        return {"error": "no active chat for compaction"}
    out = _compact_chat(cid, force=True, reason_hint="model")
    return out
