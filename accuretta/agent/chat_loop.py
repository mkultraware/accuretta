# SPDX-License-Identifier: AGPL-3.0-only
"""The agent loop: one chat turn from the user's message to the final answer, with model
calls, tool rounds, steering, folds, red-team missions and every recovery path.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import contextvars
import copy
import http.client
import json
import math
import re
import socket
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from accuretta.agent.compaction import _TASK_ACK_RE
from accuretta.agent.compaction import _render_continuity_state
from accuretta.agent.context_estimate import _TOOL_RESULT_KEEP_RECENT
from accuretta.agent.context_estimate import _render_activity_tail
from accuretta.agent.ctx_stats import _ctx_breakdown
from accuretta.agent.ctx_stats import _ctx_prompt_tokens
from accuretta.agent.goal_mode import _goal_clock_text
from accuretta.agent.goal_mode import _goal_open_steps
from accuretta.agent.goal_mode import _goal_time_limit
from accuretta.agent.goal_mode import _goal_window_progressed
from accuretta.agent.history_fold import _SUMMARY_NOTICE_INTERVAL_S
from accuretta.agent.history_fold import _mid_turn_fold
from accuretta.agent.history_fold import _summary_trigger_limit
from accuretta.agent.invalid_calls import _invalid_tool_path
from accuretta.agent.invalid_calls import _missing_required_tool_args
from accuretta.agent.llama_request import _LLAMA_FIRST_TOKEN_TIMEOUT_S
from accuretta.agent.llama_request import _LLAMA_STALL_TIMEOUT_S
from accuretta.agent.llama_request import _MODEL_LOAD_WAIT_S
from accuretta.agent.llama_request import _http_error_body
from accuretta.agent.llama_request import _is_ctx_overflow
from accuretta.agent.llama_request import _is_model_loading
from accuretta.agent.llama_request import _is_transient_server_error
from accuretta.agent.llama_request import _no_think_prefill
from accuretta.agent.llama_request import llama_options
from accuretta.agent.llama_request import llama_post_stream
from accuretta.agent.native_tools import _TURN_BOUNDARY_RE
from accuretta.agent.native_tools import _mark_native_tools_broken
from accuretta.agent.native_tools import _mark_turn_boundary_runaway
from accuretta.agent.native_tools import _turn_boundary_stops
from accuretta.agent.research_runs import _research_context
from accuretta.agent.research_runs import _research_store
from accuretta.agent.rolling_summary import _log_compact
from accuretta.agent.skills import _active_skill_section
from accuretta.agent.system_prompt import _text_tool_lines
from accuretta.agent.thinking_recovery import _REASONING_CLEANUP_KEEP_ROUNDS
from accuretta.agent.thinking_recovery import _REASONING_CLEANUP_MIN_FRAC
from accuretta.agent.thinking_recovery import _THINKING_ONLY_RETRIES
from accuretta.agent.thinking_recovery import _reasoning_cleanup_anchor
from accuretta.agent.thinking_recovery import _thinking_body
from accuretta.agent.thinking_recovery import _thinking_recovery_note
from accuretta.agent.thinking_recovery import _with_thinking_note
from accuretta.agent.token_budget import CHARS_PER_TOKEN
from accuretta.agent.token_budget import _StateNotes
from accuretta.agent.token_budget import _count_msg_tokens
from accuretta.agent.token_budget import truncate_messages
from accuretta.agent.tool_call_parse import _normalize_tool_argument_shapes
from accuretta.agent.tool_call_parse import _tool_text_view
from accuretta.agent.tool_call_parse import extract_tool_calls
from accuretta.agent.tool_call_stream import TOOL_SYNTAX_HINT_RE
from accuretta.agent.tool_call_stream import _CARRY_OTHER_CALL_MAX_CHARS
from accuretta.agent.tool_call_stream import _FLAG_RE
from accuretta.agent.tool_call_stream import _cut_off_call_name
from accuretta.agent.tool_call_stream import _flush_tool_stream
from accuretta.agent.tool_call_stream import _has_unclosed_tool_call
from accuretta.agent.tool_call_stream import _is_breach_capable_tool
from accuretta.agent.tool_call_stream import _is_file_write_call
from accuretta.agent.tool_call_stream import _log_tool_parse_failure
from accuretta.agent.tool_call_stream import _looks_unfinished
from accuretta.agent.tool_call_stream import _maybe_emit_call_progress
from accuretta.agent.tool_call_stream import _merge_carried_tool_call
from accuretta.agent.tool_call_stream import _recover_trailing_calls
from accuretta.agent.tool_call_stream import _strip_tool_markup
from accuretta.agent.tool_call_stream import _text_call_progress
from accuretta.agent.tool_call_stream import _unclosed_tool_tail
from accuretta.agent.tool_call_text import _TOOL_CODE_CLOSER_RE
from accuretta.agent.tool_call_text import _TOOL_CODE_OPENER_RE
from accuretta.agent.tool_call_text import _TOOL_STREAM_PAIRS
from accuretta.agent.tool_call_text import _classify_tool_stream
from accuretta.agent.tool_call_text import repair_tool_args
from accuretta.agent.tool_contracts import _tool_result_cap
from accuretta.agent.tool_output import compress_tool_result
from accuretta.agent.tool_parsing import _find_balanced_json_objects
from accuretta.agent.tool_schemas import tools_for_llama
from accuretta.agent.tool_sets import _RED_TEAM_TOOL_NAMES
from accuretta.agent.turn_journal import _ACTIVITY_MAX
from accuretta.agent.turn_journal import _TASK_ANCHOR_CURRENT_CHARS
from accuretta.agent.turn_journal import _TASK_ANCHOR_ORIGINAL_CHARS
from accuretta.agent.turn_journal import _build_continuity_state
from accuretta.agent.turn_notes import _COMPACTION_STOP_MESSAGE
from accuretta.agent.turn_notes import _MAYA_LOOP_STOP_NOTE
from accuretta.agent.turn_notes import _ROUND_LIMIT_ANSWER_NOTE
from accuretta.agent.turn_notes import _with_steer_continue_note
from accuretta.agent.turn_records import _chat_live_activity
from accuretta.agent.turn_records import _chat_live_activity_lock
from accuretta.agent.turn_records import _context_trim_last_notice_by_chat
from accuretta.agent.turn_records import _last_dropped_by_chat
from accuretta.agent.turn_records import _tool_activity_record
from accuretta.agent.turn_records import _tool_error_kind
from accuretta.agent.turn_records import _turn_end_reason
from accuretta.agent.turn_records import _turn_journal_begin
from accuretta.agent.turn_records import _turn_journal_checkpoint
from accuretta.agent.verification import _call_serial_key
from accuretta.agent.verification import _invoke_tool_with_context
from accuretta.agent.verification import _reset_tool_call_history
from accuretta.agent.verification import _set_current_chat
from accuretta.agent.verification import _task_check_record
from accuretta.agent.verification import _task_verification_summary
from accuretta.agent.verification import _update_verification_debt
from accuretta.agent.visible_text import _drop_invalid_call_pairs
from accuretta.agent.visible_text import _graceful_server_failure
from accuretta.agent.visible_text import _steer_kept_partial
from accuretta.agent.visible_text import _strip_unparsed_tool_markup
from accuretta.agent.visible_text import _thinking_wire_view
from accuretta.agent.visible_text import _visible_final_answer
from accuretta.agent.wire_messages import _sanitize_messages_for_openai
from accuretta.agent.wire_messages import _verification_wanted
from accuretta.agent.wire_replay import _clamp_image_data_url
from accuretta.agent.wire_replay import _replay_wire_msg
from accuretta.core.cancels import _register_cancel
from accuretta.core.cancels import _set_cancel_resp
from accuretta.core.cancels import _unregister_cancel
from accuretta.core.chat_events import _chat_emitters
from accuretta.core.chats import _current_rt_chat
from accuretta.core.chats import get_chats
from accuretta.core.effort import _REASONING_LEVELS
from accuretta.core.effort import _normalize_effort_choice
from accuretta.core.endpoints import LLAMA
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.savings import _savings_add
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.tool_pool import _tool_executor
from accuretta.core.tool_registry import TOOLS
from accuretta.core.undo import _undo_begin_turn
from accuretta.core.undo import _undo_commit_turn
from accuretta.core.visible_reply import _live_turn_messages
from accuretta.core.visible_reply import _split_leading_think
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_passive_mission
from accuretta.redteam.mission_flow import _RT_ATTN_SPAN
from accuretta.redteam.mission_flow import _rt_close_frontend_secret_mission
from accuretta.redteam.mission_flow import _rt_mission_mark_tool
from accuretta.redteam.mission_flow import _rt_mission_note
from accuretta.redteam.mission_flow import _rt_mission_set_phase
from accuretta.redteam.mission_flow import _rt_result_confirms_finding
from accuretta.redteam.mission_flow import _rt_target_from_args
from accuretta.redteam.mission_logs import _rt_log_mission_ended
from accuretta.redteam.mission_panel import _rt_mission_render
from accuretta.redteam.network_scope import _RT_EXPLOIT_TOOL_NAMES
from accuretta.redteam.network_scope import _rt_incl_exploit_for
from accuretta.runtime.llama_http import _conversation_token_scale
from accuretta.runtime.llama_http import llama_ready
from accuretta.runtime.llama_process import _CTX_EST_CACHE
from accuretta.runtime.llama_process import _last_prompt_tokens_by_chat
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.llama_process import _rt_trailing_system_ok
from accuretta.runtime.llama_process import _tools_spec_overhead_tokens
from accuretta.runtime.llama_stream import _is_llama_unreachable
from accuretta.runtime.llama_stream import _llama_error_text
from accuretta.runtime.model_advisor import _HARNESS_INTERVENTION_KEYS
from accuretta.runtime.model_observation import _record_model_observation
from accuretta.runtime.model_presets import _model_features
from accuretta.runtime.model_sampling import _mark_model_reasoning_observed
from accuretta.runtime.model_sampling import _model_no_tools_sampling
from accuretta.runtime.reasoning_capability import _reasoning_capability
from accuretta.runtime.reasoning_request import _resolve_reasoning_request
from accuretta.runtime.tuning_math import _reasoning_payload_overrides
from accuretta.security.approvals import _chat_in_goal_mode
from accuretta.security.approvals import _steering
from accuretta.security.blocked_writes import _FILE_IN_PARTS
from accuretta.tools.powershell import _clip_head_tail


def run_chat_turn(chat_id: str, messages: list[dict], use_tools: bool, emit,
                  native_tools: bool = True, reasoning_effort: str = "auto"):
    """
    messages: list of {role, content, [tool_calls], [tool_call_id]}
    emit(event_dict): pushes a chunk to the caller (SSE producer).
    Returns the final assistant message dict (with content fully assembled).

    use_tools     — tools are in play this turn (listed in the system prompt,
                    and content is scanned by the text fallback parser).
    native_tools  — ALSO hand llama-server the `tools` array so it can emit
                    structured tool_calls. False = text-tools mode (Gemma):
                    the model still sees/uses tools, but only via content we
                    parse ourselves, since llama.cpp can't parse Gemma's dialect.
    """
    settings = get_settings()
    model = settings.get("model") or ""
    if not model:
        emit({"type": "error", "error": "no model selected. Pick one in Settings."})
        return None
    requested_reasoning_effort = _normalize_effort_choice(reasoning_effort)
    reasoning_capability = _reasoning_capability(
        _llama.loaded_model() or settings.get("model_path") or model, settings)

    cancel_ev = _register_cancel(chat_id)
    if cancel_ev is None:
        emit({"type": "error", "error": "this session already has a turn running; wait for it to finish or stop it first"})
        return None
    _undo_begin_turn(chat_id)
    _turn_id = _turn_journal_begin(chat_id)
    _steering.begin(chat_id)
    # Passive model telemetry must count failures too, or timeouts and exhausted
    # recovery paths vanish from the denominator. Initialize before any request
    # can return; the finally block records an incomplete observation unless the
    # final-message path already committed one.
    turn_started_at = time.time()
    turn_prompt_total = 0
    turn_eval_total = 0
    turn_peak_prompt = 0
    # Lifetime counter (savings.json) inputs. turn_prompt_total keeps
    # llama-server's KV-cache-incremental prompt_n (what speed telemetry needs),
    # which skips the cached prefix: on a long agentic turn that is most of every
    # request. The counter takes the full prompt of every round, as a cloud API
    # bills it.
    turn_full_prompt_total = 0
    _open_round: dict | None = None   # round whose stream opened but sent no stats yet
    _savings_recorded = False
    rounds = 0
    turn_tool_calls = 0
    turn_tool_errors = 0
    turn_tool_outcomes: list[dict] = []
    turn_prompt_ms_total = 0.0
    turn_eval_ms_total = 0.0
    turn_draft_n = 0
    turn_draft_accepted = 0
    turn_compactions = 0
    turn_compaction_failures = 0
    turn_interventions = {key: 0 for key in _HARNESS_INTERVENTION_KEYS}

    def note_intervention(kind: str) -> None:
        if kind in turn_interventions:
            turn_interventions[kind] += 1

    def _commit_turn_savings(extra_out: int = 0) -> dict | None:
        """Add this turn's tokens to the lifetime counter exactly once: from the
        final-reply path, or from `finally` for a stopped / failed turn. A round cut
        off mid-stream never reported stats but was prefilled and partly generated,
        so it is estimated rather than dropped."""
        nonlocal _savings_recorded
        if _savings_recorded:
            return None
        _savings_recorded = True
        tok_in = turn_full_prompt_total
        tok_out = turn_eval_total + max(0, int(extra_out or 0))
        if _open_round:
            try:
                tok_in += int(_ctx_prompt_tokens(_open_round["msgs"], _open_round["ovh"],
                                                 _open_round["scale"]) or 0)
            except Exception:
                pass
            tok_out += len("".join(_live_content_buf())) // 4
        if not (tok_in or tok_out):
            return None
        return _savings_add(tok_in, tok_out)

    def _live_content_buf() -> list:
        # content_buf is rebound per round; read the live one at commit time.
        try:
            return content_buf  # noqa: F821 — assigned later in this function
        except NameError:
            return []

    _profile_recorded = False
    _chat_emitters[chat_id] = emit
    with _chat_live_activity_lock:
        _chat_live_activity[chat_id] = []
    # Fresh tool-call history for this turn (see _record_tool_result /
    # _tool_repeat_state). Keyed by chat so concurrent turns never share streaks,
    # and reset before the executor hop (workers read the same bucket via the
    # contextvar).
    _reset_tool_call_history(chat_id)
    # Stash chat_id thread-locally and in the contextvar that survives the
    # executor hop, so deep tool callers can tag their output with the chat.
    _set_current_chat(chat_id)
    _rt_chat_ctx_token = None
    try:
        try:
            max_tool_rounds = int(settings.get("max_tool_rounds") or 120)
        except Exception:
            max_tool_rounds = 60
        max_tool_rounds = max(1, min(max_tool_rounds, 500))
        _goal_round_budget = max_tool_rounds   # one /goal stretch (renewed in place)
        rounds = 0
        empty_retries = 0    # blank-reply-after-tools nudges used this turn
        research_retries = 0
        auto_continues = 0   # mid-plan stall bumps used this turn (see _looks_unfinished)
        # Turn-level loop guard: consecutive rounds where every call came back as a
        # loop-breaker refusal. Two in a row -> tools off, one forced final
        # generation, turn ends. The per-call refusal alone doesn't stop this; it is
        # just another message the model reads and ignores.
        refused_streak = 0
        refused_broke = False
        refused_names: set = set()
        # Out of tool rounds with a call still pending: one more request with
        # tool_choice "none" (tools stay listed, so the prompt cache holds) asks
        # for the answer, instead of ending the turn on a canned line that threw
        # away everything the model had found.
        final_answer_round = False
        # Carry-over of an in-flight <tool_call> truncated by the output budget. A
        # big write_file body can exceed even the expanded max_tokens, so re-emitting
        # the whole call never fits. Hold the partial call here, prepend it to the
        # next round's text and ask the model to continue the same call until it
        # closes; extract_tool_calls then sees the whole body.
        # Build fingerprint: stamped on every persisted final reply so a chat can be
        # attributed to the bridge code that produced it. Bump it only when the
        # mid-call carry-over behavior changes.
        _BUILD_TAG = "carry-over-v6"
        _carry_call = ""
        carry_rounds = 0     # continuation laps used this turn (bounded)
        carry_last_size = 0
        carry_stalls = 0
        carry_recoveries = 0
        # Diagnostics: why the mid-call carry guard didn't fire (or which reason kept
        # a round from continuing), persisted on the final reply.
        _carry_diag: list[str] = []
        # Turn-level token totals summed across rounds, so the persisted message
        # reflects the whole turn's cost rather than just the final round.
        turn_eval_total = 0
        turn_prompt_total = 0
        # Breach state for the red-team / cyber-range flow: a FLAG{...} captured
        # in a tool response IS confirmed access. Track distinct flags seen this
        # turn so we report each stage once and can summarize at the end.
        captured_flags: list = []
        # Durable mission state, loaded once into a working holder and kept fresh in
        # memory as tools capture target/flags, so the recency-end tail reflects
        # mid-turn progress without a per-round disk read. Changes go back on
        # final["_mission_updates"] for the caller to persist once. Pins are
        # snapshotted alongside; both ride as a tail message (below) rather than a
        # system-prompt splice, so llama.cpp's cached prefix survives their changes.
        _rt_on = False
        _rt_chat: dict = {}
        _rt_dirty = False
        _pins_tail: list = []
        _plan_tail: list[dict] = []
        _skill_tail = ""
        _activity_tail: list[dict] = []
        _activity_dirty = False
        _task_state: dict = {}
        _continuity_state: dict = {}
        _compaction_memory: dict = {}
        _findings_tail: list[dict] = []
        _verification_debt: list[dict] = []
        _turn_checks: list[dict] = []
        _verification_dirty = False
        _roll_active = False
        # /goal: the round cap is a checkpoint, not a stop (see _goal_window_progressed).
        _goal_started = 0
        _goal_limit = 0
        _goal_renewals = 0
        _goal_window_from = 0      # index into turn_tool_outcomes where this window began
        _state_notes = _StateNotes()
        # Cut point of the latest reasoning cleanup (see _reasoning_cleanup_anchor).
        _reasoning_keep_from: dict | None = None
        try:
            _c0 = get_chats().get("chats", {}).get(chat_id) or {}
            if settings.get("rt_mission_state", True) and isinstance(_c0.get("mission"), dict):
                _rt_chat["mission"] = dict(_c0["mission"])
            _rt_on = bool(settings.get("red_team_enabled")
                          and _c0.get("last_mode") != "research"
                          and settings.get("rt_mission_state", True)
                          and _rt_authorized_mission(_c0))
            _pins_tail = [str(p) for p in (_c0.get("pins") or [])]
            _plan_tail = [dict(p) for p in (_c0.get("plan") or []) if isinstance(p, dict)]
            _activity_tail = [dict(a) for a in (_c0.get("activity") or []) if isinstance(a, dict)][-_ACTIVITY_MAX:]
            if isinstance(_c0.get("task_anchor"), dict):
                _task_state = dict(_c0["task_anchor"])
            if isinstance(_c0.get("compaction_memory"), dict):
                _compaction_memory = dict(_c0["compaction_memory"])
            _verification_debt = [dict(x) for x in (_c0.get("verification_debt") or [])
                                  if isinstance(x, dict)]
            _continuity_state = _build_continuity_state(
                _c0, activity=_activity_tail, plan=_plan_tail)
            _findings_tail = [dict(x) for x in (_c0.get("findings") or []) if isinstance(x, dict)][-20:]
            _roll_active = bool(_c0.get("rolling_summary"))
            _skill_tail = _active_skill_section(_c0)
            _goal_started = int(_c0.get("goal_started") or 0)
            # The limit is read when /goal starts (from the whole request); chats
            # latched before that fall back to the stored one-line goal.
            _goal_limit = int(_c0.get("goal_limit_s") or 0) or _goal_time_limit(
                str(_c0.get("goal_text") or ""))
        except Exception:
            pass
        _rt_chat_ctx_token = _current_rt_chat.set({} if _research_context(chat_id)[1] else _rt_chat if _rt_on else None)
        if _rt_on:
            _mission = _rt_chat.get("mission") or {}
            emit({
                "type": "rt_mission",
                "status": str(_mission.get("status") or "active"),
                "phase": str(_mission.get("phase") or "recon"),
                "target": str(_mission.get("target") or ""),
                "engagement": str(_mission.get("engagement") or "recon"),
            })
        # Objective anchor for long-horizon drift. Only authorized red-team runs have
        # a mission block, and pin_note depends on the model calling it, so a plain
        # long task has no durable statement of its goal once folds or the trimmer
        # remove the first user message. Capture both the original ask and the
        # current task (latest substantive user message before the live turn):
        # multi-task sessions outgrow the first message, and a fold that hides the
        # switch makes the model forget the task in flight.
        _user_texts: list[str] = []
        for _m in messages:
            if _m.get("role") != "user":
                continue
            _uc = _m.get("content")
            if isinstance(_uc, list):
                _uc = " ".join(p.get("text", "") for p in _uc
                               if isinstance(p, dict) and p.get("type") == "text")
            _uc = re.sub(r"\s+", " ", str(_uc or "")).strip()
            if _uc:
                _user_texts.append(_uc)
        _objective = str(_task_state.get("original") or "")[:_TASK_ANCHOR_ORIGINAL_CHARS]
        if not _objective and _user_texts:
            _objective = _user_texts[0][:_TASK_ANCHOR_ORIGINAL_CHARS]
        _current_task = str(_task_state.get("current") or "")[:_TASK_ANCHOR_CURRENT_CHARS]
        if not _current_task:
            for _uc in reversed(_user_texts):
                if not _TASK_ACK_RE.fullmatch(_uc):
                    _current_task = _uc[:_TASK_ANCHOR_CURRENT_CHARS]
                    break
                break
        # Long-session detector: past the reliable attention span the objective
        # anchor rides the tail every round, not only on rounds where the trimmer
        # fired.
        _session_long = False
        try:
            _session_long = sum(_count_msg_tokens(_m) for _m in messages) > _RT_ATTN_SPAN
        except Exception:
            pass
        force_no_think = False   # set when the thinking-budget enforcer aborts a runaway
        reasoning_seen_this_turn = False
        # Rounds in a row that ended with only thinking: cut by the guard, stopped at
        # the output cap, or ended there. A round with an answer or a call resets it.
        thinking_only_streak = 0
        request_no = 0           # model requests this turn; tags thinking for the UI
        # A tools round hit the output cap (finish_reason "length"): the budget went
        # to reasoning and the <tool_call> body never got tokens. Force thinking off
        # next round so the tokens go to the call; greedy reasoners otherwise just
        # re-narrate the plan and hit the cap again.
        tool_cap_no_think = False
        arg_loss_retries = 0
        # Text-tools turns already list the tools in the system prompt.
        tool_card_sent = not native_tools
        conversation = list(messages)
        # Anything appended past this index is this turn's working memory
        # (intermediate assistant tool calls, tool results, nudges). It goes back via
        # `final["_appended_intermediate"]` so it is persisted and replayed next turn.
        _start_len = len(messages)

        def _apply_task_updates(updates: list[dict], kept_partial: dict | None = None) -> bool:
            # kept_partial: the reply the update interrupted (_steer_kept_partial).
            # It goes in before the update, and the model's copy of the first update
            # asks it to carry on from there.
            if updates and kept_partial:
                conversation.append(kept_partial)
            for n, update in enumerate(updates):
                update_images = [_clamp_image_data_url(u) for u in (update.get("images") or [])]
                stored = {"role": "user", "content": update["text"], "images": update_images}
                wire = _replay_wire_msg(stored, _llama.is_vision_capable())
                if kept_partial and n == 0:
                    wire = _with_steer_continue_note(wire)
                wire.update(_steering_id=update["id"], _steering_text=update["text"],
                            _steering_images=update_images)
                conversation.append(wire)
                _turn_journal_checkpoint(chat_id, _turn_id, conversation[_start_len:],
                                         activity=_activity_tail, mission=_rt_chat.get("mission"),
                                         verification_debt=_verification_debt)
                _steering.applied(chat_id, update["id"])
                event = {"type": "steering_applied", "chat_id": chat_id, **update, "images": update_images}
                if kept_partial and n == 0:
                    event["kept_partial"] = kept_partial["content"]
                emit(event)
            return bool(updates)

        def _finish_compaction_failure() -> dict:
            """Persist a clear stop result after _mid_turn_fold saved the work."""
            nonlocal turn_compaction_failures
            turn_compaction_failures += 1
            assistant_msg: dict = {
                "role": "assistant",
                "content": _COMPACTION_STOP_MESSAGE,
                "_appended_intermediate": [],
                "_build": _BUILD_TAG,
                "_turn_id": _turn_id,
                "_compaction_failed": True,
            }
            if _rt_dirty and _rt_chat.get("mission"):
                assistant_msg["_mission_updates"] = dict(_rt_chat["mission"])
            if _activity_dirty:
                assistant_msg["_activity_updates"] = list(_activity_tail[-_ACTIVITY_MAX:])
                assistant_msg["_continuity_updates"] = dict(_continuity_state)
            if _verification_dirty:
                assistant_msg["_verification_updates"] = list(_verification_debt)
            if _carry_diag:
                assistant_msg["_carry_diag"] = list(_carry_diag)
            emit({"type": "final", "message": assistant_msg})
            return assistant_msg

        while True:
            if cancel_ev.is_set():
                emit({"type": "notice", "note": "stopped by user", "quiet": True})
                return {
                    "role": "assistant", "content": "", "_stopped": True,
                    "_appended_intermediate": _drop_invalid_call_pairs(conversation[_start_len:]),
                    "_turn_id": _turn_id,
                }
            if _apply_task_updates(_steering.take(chat_id)):
                _carry_call = ""
            # Use llama-server's actual slot context when /props answers; a smaller
            # settings default would make the trimmer chop a conversation the server
            # holds fine.
            ctx_limit = _llama_props_ctx() or int(settings.get("num_ctx") or 32768)
            _reasoning_cfg = _resolve_reasoning_request(
                requested_reasoning_effort, reasoning_capability, settings, ctx_limit)
            # Reserve headroom for the response + thinking, capped at 16K: 25% of a 262K
            # window would waste ~65K on headroom no answer needs.
            reserve = max(min(int(ctx_limit * 0.25), 16000), 1024)
            # Tool spec overhead: llama-server's Jinja template inlines the full tools
            # array into the system message (6-10K tokens), invisible to us until the
            # server rejects with "exceeds context size". /tokenize gives an exact count
            # (cached per spec) so the trimmer's budget matches what is sent.
            # The red-team capability gate is recomputed each round from the selected
            # engagement mode and mission state.
            _rt_incl_exploit = _rt_incl_exploit_for(_rt_chat, settings)
            tools_overhead = 0
            if use_tools and native_tools:
                try:
                    _round_tools = tools_for_llama(_rt_incl_exploit, chat_id)
                    _tools_json = json.dumps(_round_tools, ensure_ascii=False)
                    tools_overhead = _tools_spec_overhead_tokens(_tools_json)
                except Exception:
                    _round_tools = []
                    tools_overhead = 4096  # conservative fallback
            # Floor the messages budget at 2048 tokens (room for at least system + last
            # user), plus a 768-token margin because the tools overhead is an estimate
            # and --no-context-shift hard-errors a prompt that slips over ctx.
            # Rolling-summary mode keeps full tool evidence in the prompt until a fold
            # preserves it. With summarization disabled, keep the age-stub fallback and
            # inflate its trim budget by exactly what the sanitizer will elide.
            summarize_enabled = bool(use_tools and settings.get("summarize_history", True))
            stub_old_tools = not summarize_enabled
            _stub_cutoff = len(conversation) - _TOOL_RESULT_KEEP_RECENT
            _stub_savings = (sum(_count_msg_tokens(m) for m in conversation[:_stub_cutoff]
                                 if m.get("role") == "tool")
                             if stub_old_tools else 0)
            # Task-state notes kept where they were sent ride on top of the trimmed
            # messages, so their size comes out of the trim budget too.
            effective_reserve = max(1024, min(reserve + tools_overhead + 768 - _stub_savings
                                              + _state_notes.kept_tokens(),
                                              ctx_limit - 2048))
            # Budget the trim in the server's real token units: one /tokenize call yields
            # a correction factor for the char heuristic, so the trimmer and the slot
            # agree (the overflow retry below stays a rare safety net).
            token_scale = _conversation_token_scale(conversation)
            _fold = None
            # Proactive mid-turn fold: the last round's real fill (recomputed from the
            # actual payload, see the stats handler) is ground truth. Once it crosses the
            # trigger, fold before the trimmer drops the middle. The fold is deep (~10% of
            # ctx kept raw), so the round gets real runway again.
            try:
                _cleaned = False
                if (summarize_enabled
                        and _last_prompt_tokens_by_chat.get(chat_id, 0)
                        >= _summary_trigger_limit(ctx_limit)):
                    # Reasoning cleanup first: drop older rounds' reasoning, keep
                    # everything else word for word. Compact only when that frees
                    # less than _REASONING_CLEANUP_MIN_FRAC of the window.
                    _cut = _reasoning_cleanup_anchor(conversation, settings)
                    if _cut is not None and _cut is not _reasoning_keep_from:
                        _units_now = sum(_count_msg_tokens(m) for m in _thinking_wire_view(
                            conversation, settings, keep_reasoning_from=_reasoning_keep_from))
                        _units_cut = sum(_count_msg_tokens(m) for m in _thinking_wire_view(
                            conversation, settings, keep_reasoning_from=_cut))
                        _freed = int((_units_now - _units_cut) * token_scale)
                        if _freed >= ctx_limit * _REASONING_CLEANUP_MIN_FRAC:
                            _reasoning_keep_from = _cut
                            _cleaned = True
                            _log_compact(chat_id, f"reasoning_cleanup: map={_last_prompt_tokens_by_chat.get(chat_id, 0)} "
                                                  f"freed~{_freed} kept_rounds={_REASONING_CLEANUP_KEEP_ROUNDS} "
                                                  f"ctx={ctx_limit}")
                            emit({"type": "notice", "quiet": True,
                                  "note": f"context nearly full: dropped older reasoning (~{_freed // 1000}k tokens) "
                                          f"and kept every tool result, instead of compacting"})
                if (not _cleaned and summarize_enabled
                        and _last_prompt_tokens_by_chat.get(chat_id, 0)
                        >= _summary_trigger_limit(ctx_limit)):
                    _log_compact(chat_id, f"proactive: map={_last_prompt_tokens_by_chat.get(chat_id, 0)} "
                                          f"trigger={_summary_trigger_limit(ctx_limit)} ctx={ctx_limit}")
                    _fold = _mid_turn_fold(chat_id, conversation, _start_len,
                                           ctx_limit, _llama.is_vision_capable())
                    if _fold:
                        if _fold.get("failed"):
                            return _finish_compaction_failure()
                        if _fold.get("folded"):
                            turn_compactions += 1
                        _start_len = _fold["start_len"]
                        if _fold.get("conversation") is not None:
                            conversation = _fold["conversation"]
                            _roll_active = True
                            if isinstance(_fold.get("continuity"), dict):
                                _continuity_state = dict(_fold["continuity"])
                            if isinstance(_fold.get("compaction_memory"), dict):
                                _compaction_memory = dict(_fold["compaction_memory"])
                            token_scale = _conversation_token_scale(conversation)
            except Exception:
                traceback.print_exc()
            # Budget against what is actually sent (old reasoning stripped), so
            # stripping buys real runway before the trim/fold below fires.
            # `conversation` itself stays whole for the fold summarizer.
            _wire_conv = _thinking_wire_view(conversation, settings,
                                             keep_reasoning_from=_reasoning_keep_from)
            trimmed = truncate_messages(_wire_conv, ctx_limit, reserve=effective_reserve,
                                        token_scale=token_scale)
            dropped = max(0, len(conversation) - len(trimmed))
            if dropped > 0:
                _log_compact(chat_id, f"trim: dropped={dropped} ctx={ctx_limit} reserve={effective_reserve} "
                                      f"scale={token_scale} use_tools={use_tools} "
                                      f"summarize={settings.get('summarize_history', True)} "
                                      f"fold_before={bool(_fold)} "
                                      f"conv_n={len(conversation)} conv_units={int(sum(_count_msg_tokens(m) for m in conversation))} "
                                      f"budget={int(ctx_limit - effective_reserve)}")
            # A trimmer drop is not automatically an overflow. Besides budget overflows
            # it also pops orphan tool results whose assistant parent was folded away
            # (harmless cleanup; llama rejects parentless tool messages), and those are
            # common right after a mid-turn fold. Treating an orphan pop as overflow
            # refolded the history, pushed the boundary into the next tool run, created
            # more orphans, and compacted on every message at ~5% fill. Only a genuine
            # overflow (fill past the trim budget in real token units) may trigger the
            # mid-turn fold or set the dropped flag.
            _over_budget = (sum(_count_msg_tokens(m) for m in _wire_conv) * token_scale
                            > (ctx_limit - effective_reserve))
            if dropped > 0 and summarize_enabled and _over_budget:
                # The trimmer is about to delete the middle of a live agentic turn: fold into
                # the rolling summary instead, then re-trim the rebuilt, shorter conversation.
                # The KV reprocess this costs is already being paid by the hard trim.
                try:
                    _fold = _mid_turn_fold(chat_id, conversation, _start_len,
                                           ctx_limit, _llama.is_vision_capable())
                    if _fold:
                        if _fold.get("failed"):
                            return _finish_compaction_failure()
                        if _fold.get("folded"):
                            turn_compactions += 1
                        _start_len = _fold["start_len"]
                        if _fold.get("conversation") is not None:
                            conversation = _fold["conversation"]
                            _roll_active = True
                            if isinstance(_fold.get("continuity"), dict):
                                _continuity_state = dict(_fold["continuity"])
                            if isinstance(_fold.get("compaction_memory"), dict):
                                _compaction_memory = dict(_fold["compaction_memory"])
                            token_scale = _conversation_token_scale(conversation)
                            _wire_conv = _thinking_wire_view(
                                conversation, settings, keep_reasoning_from=_reasoning_keep_from)
                            trimmed = truncate_messages(_wire_conv, ctx_limit,
                                                        reserve=effective_reserve,
                                                        token_scale=token_scale)
                            dropped = max(0, len(conversation) - len(trimmed))
                            # Judge the rebuilt post-fold prompt, not the stale
                            # overflowing pre-fold conversation. Boundary orphan
                            # cleanup after a successful fold must not set the
                            # next-turn force flag or emit another trim notice.
                            _over_budget = (
                                sum(_count_msg_tokens(m) for m in _wire_conv)
                                * token_scale > (ctx_limit - effective_reserve)
                            )
                except Exception:
                    traceback.print_exc()
            if dropped > 0 and _over_budget:
                # The trimmer had to cut: remember it so the next user turn folds
                # proactively pre-turn (see _maybe_roll_summary / _last_dropped_by_chat).
                _last_dropped_by_chat[chat_id] = True
                # "folded" tells the UI whether these messages were compacted into the
                # summary (mid-turn fold ran) or just elided from the prompt, so the pill
                # doesn't say "summarized" for a plain drop.
                _trim_now = time.time()
                if (_trim_now - _context_trim_last_notice_by_chat.get(chat_id, 0)
                        >= _SUMMARY_NOTICE_INTERVAL_S):
                    _context_trim_last_notice_by_chat[chat_id] = _trim_now
                    emit({"type": "context_trimmed", "dropped": dropped,
                          "total": len(conversation),
                          "folded": bool(_fold and _fold.get("folded")),
                          "fold_pending": bool(_fold is not None and not _fold.get("folded"))})
            else:
                _last_dropped_by_chat.pop(chat_id, None)

            # Volatile session state (mission block + pins + objective) goes after the
            # history, right before generation, where attention lands. It changes
            # mid-session, so splicing it into the system prompt would invalidate
            # llama.cpp's cached prefix (~15K+ tokens re-prefilled); on the tail, only
            # this small message is re-evaluated. The mission therefore always sits at the
            # recency end. Mutates only the per-round `trimmed` copy, never
            # `conversation`, so it is never persisted or double-counted.
            try:
                _session_long_now = (_session_long or
                                     sum(_count_msg_tokens(_m) for _m in conversation)
                                     > _RT_ATTN_SPAN)
            except Exception:
                _session_long_now = _session_long
            _tail_secs: list = []
            _mission_shown = False
            if use_tools:
                _mtxt = (_rt_mission_render(_rt_chat)
                         if _rt_on and settings.get("rt_mission_state", True) else "")
                if not _mtxt:
                    _tail_secs.append("Verification: for a bug fix, capture a reproducing check before editing and rerun it afterward. Match verification to the requested behavior; use verification_guidance for UI/startup changes. A trivial label or color edit needs only a focused check. Report untested behavior honestly.")
                if _mtxt:
                    _tail_secs.append(
                        "=== MISSION (authorized run — hold this; do NOT drift off-task) ===\n"
                        + _mtxt)
                    _mission_shown = True
                if _skill_tail:
                    _tail_secs.append(_skill_tail)
                if _pins_tail:
                    _tail_secs.append(
                        "=== PINNED FOR THIS SESSION (established facts/fixes — do NOT undo or repeat) ===\n"
                        + "\n".join(f"- {p}" for p in _pins_tail))
                if _plan_tail:
                    _plan_lines = []
                    for _p in _plan_tail[:12]:
                        _st = str(_p.get("status") or "pending")
                        _title = str(_p.get("title") or "").strip()
                        if _title:
                            _plan_lines.append(f"- {_st}: {_title}")
                    if _plan_lines:
                        _tail_secs.append(
                            "=== ACTIVE PLAN (authoritative progress — continue the active/pending work; "
                            "do not redo done steps) ===\n" + "\n".join(_plan_lines))
                if _activity_tail and (_roll_active or _session_long_now):
                    _activity_text = _render_activity_tail(_activity_tail)
                    if _activity_text:
                        _tail_secs.append(
                            "=== RECENT TOOL LEDGER (harness-recorded outcomes — do not claim more "
                            "than these results prove) ===\n" + _activity_text)
                if _continuity_state and (_roll_active or _session_long_now):
                    # Request copies still in this prompt are not repeated, and
                    # the goal lines go when the OBJECTIVE section below carries them.
                    _wire_user_text = " ".join(
                        str(_m.get("content") or "") for _m in trimmed
                        if isinstance(_m, dict) and _m.get("role") == "user"
                        and isinstance(_m.get("content"), str))
                    _continuity_text = _render_continuity_state(
                        _continuity_state, ctx_limit=ctx_limit, wire_text=_wire_user_text,
                        objective_shown=bool((not _mission_shown) and (_objective or _current_task)))
                    if _continuity_text:
                        _tail_secs.append(
                            "=== STRUCTURED CONTINUITY (harness-owned facts; authoritative over a prose "
                            "summary if they conflict) ===\n" + _continuity_text)
            # General long-horizon anchor (no red-team mission in play), for tool and
            # plain chats once folds or the trimmer can hide the current work. Carries the
            # original ask and the latest substantive instruction so task switches
            # survive compaction.
            if (not _mission_shown) and (_objective or _current_task) and (
                    dropped > 0 or _roll_active or _session_long_now):
                _obj_lines = []
                if _objective:
                    _obj_lines.append("original ask: " + _objective)
                if _current_task and _current_task != _objective:
                    _obj_lines.append("current task: " + _current_task)
                _tail_secs.append(
                    "=== OBJECTIVE (what this session is working toward — keep pushing it "
                    "forward from where you are; do NOT restart it, and do NOT ask what "
                    "we're working on) ===\n" + "\n".join(_obj_lines))
            # /goal clock and the round budget. Both ride inside the task-state
            # note (never the system prompt, whose change would make a hybrid
            # model re-read the whole chat). The clock is a slow section: it
            # changes every minute but only travels with the periodic refresh.
            if use_tools:
                _goal_live = _chat_in_goal_mode(chat_id)
                if _goal_live and _goal_started:
                    _tail_secs.append(_goal_clock_text(time.time() - _goal_started, _goal_limit))
                _rounds_left = max_tool_rounds - rounds
                if _rounds_left <= 2 and not (
                        _goal_live and not refused_broke
                        and _goal_window_progressed(turn_tool_outcomes[_goal_window_from:])):
                    _tail_secs.append(
                        "=== ROUND BUDGET === "
                        + (f"{_rounds_left} tool round{'s' if _rounds_left != 1 else ''} left in this turn. "
                           "Finish the step in hand, then" if _rounds_left > 0 else
                           "No tool rounds are left in this turn. Do not call a tool:")
                        + " reply with a short status: what is done, what is left, and the next step.")
            # An unfinished tool call must remain the final assistant message so
            # llama.cpp can prefill and continue that exact message. Adding the
            # volatile session tail after it converts this into a new turn and
            # makes models restart the call from its first byte.
            # Earlier notes stay on the messages they were sent with, and a new
            # one is added only when its text changed (see _StateNotes), so the
            # prompt only grows within a task and the cache is reused whole.
            # Neutral wording: "re-sent every turn" made models open their reasoning
            # with "Session state has been resent" (30 of 368 logged rounds).
            # The periodic refresh of an unchanged note is a short reminder: the
            # open plan steps, the current task and the clock. An authorized
            # engagement keeps the whole note on every refresh, so its mission
            # and scope stay at the recency end.
            _brief: list[str] | None = None
            if not _mission_shown:
                _brief_lines = [f"- {str(_p.get('status') or 'pending')}: {str(_p.get('title') or '').strip()}"
                                for _p in _plan_tail
                                if isinstance(_p, dict) and str(_p.get("title") or "").strip()
                                and str(_p.get("status") or "pending") != "done"][:4]
                if not _brief_lines and _current_task:
                    _brief_lines = ["current task: " + _current_task[:240]]
                _brief = ["=== TASK STATE (unchanged; the full note above still applies) ==="
                          + ("\n" + "\n".join(_brief_lines) if _brief_lines else "")]
                _brief += [s for s in _tail_secs
                           if s.startswith(("=== GOAL CLOCK", "=== ROUND BUDGET"))]
            trimmed = _state_notes.apply(
                trimmed,
                "[task state from the harness: follow it, stay on the task, do not undo it, "
                "and do not restate or comment on it]",
                _tail_secs,
                "system" if _rt_trailing_system_ok() else "user",
                rounds,
                allow_new=not _carry_call,
                brief=_brief)

            payload = {
                "model": model or "local",
                "messages": _sanitize_messages_for_openai(
                    trimmed, stub_old_tools=stub_old_tools),
                "stream": True,
                **llama_options(settings),
            }
            if (_carry_call and payload["messages"]
                    and payload["messages"][-1].get("role") == "assistant"):
                # Current llama.cpp supports content-prefill continuation. The
                # partial XML stays plain assistant content rather than a
                # structured tool_calls object, avoiding the tool-prefill path
                # that can drop calls on affected server builds.
                payload["continue_final_message"] = "content"
                payload["add_generation_prompt"] = False
            # Output cap vs. headroom: max_tokens must never exceed the room left after
            # the prompt (the reserve above), or a runaway generation overflows n_ctx
            # mid-stream, which with --no-context-shift fails the whole turn.
            try:
                _mo_raw = payload.get("max_tokens")
                if _mo_raw is None:
                    _mo_raw = settings.get("max_output_tokens") or 8192
                _mo = int(_mo_raw)
            except (ValueError, TypeError):
                _mo = 8192
            if _mo > 0:
                # llama-server counts reasoning tokens against the same max_tokens, so a
                # heavy-thinking model halves the room for the answer and a bulky single call
                # like write_file gets cut mid-body (and a truncated file is written as "ok").
                # Add the thinking budget back on top; the clamp below still bounds it by the
                # reserved headroom.
                try:
                    _tb_plus = int(_reasoning_cfg.get("response_headroom") or 0)
                except (ValueError, TypeError):
                    _tb_plus = 0
                if use_tools and _tb_plus > 0:
                    _mo = _mo + _tb_plus
                payload["max_tokens"] = max(256, min(_mo, max(reserve - 1024, 256)))
            # Agentic turns need a tighter, deterministic sampling profile. Global sliders
            # tuned for design work (high temp, presence penalty) make a capable model
            # second-guess correct tool work and loop, because the penalties push it away
            # from reaffirming what it just did. With tools in play, clamp temperature and
            # zero the presence/frequency penalties, as coding agents do; IDE turns keep
            # the user's settings. Models whose vendor sampling is the agentic profile
            # (Sage-A1.5: temp 1.0 + presence 1.5; Nanbeige4.3: temp 1.0) keep theirs.
            # The repeat penalty goes too: it pushes down every token seen in the last
            # 64, and code and JSON repeat quotes, brackets, indentation and path parts
            # on purpose. Both vendor profiles above run with it off.
            if use_tools and "vendor_agentic_sampling" not in _model_features():
                try:
                    _t = float(payload.get("temperature", 0.4) or 0.0)
                except (ValueError, TypeError):
                    _t = 0.4
                payload["temperature"] = min(_t, 0.4)
                payload["presence_penalty"] = 0.0
                payload["frequency_penalty"] = 0.0
                payload["repeat_penalty"] = 1.0
            elif not use_tools:
                # Vendors with separate chat sampling (Nanbeige4.3: temp 0.6 for
                # reasoning/chat vs 1.0 agentic) get it on turns without tools.
                payload.update(_model_no_tools_sampling())
            # Reasoner-family chat_template_kwargs: llama-server forwards these into the
            # Jinja template, toggling thinking per request and capping thinking tokens.
            tpl_kwargs: dict = {}
            enable_thinking = _reasoning_cfg.get("enabled")
            if enable_thinking is not None:
                tpl_kwargs["enable_thinking"] = bool(enable_thinking)
            if _reasoning_cfg.get("native_effort"):
                # llama.cpp exposes model-native effort through template kwargs
                # using the exact variable declared by that model's template.
                # Muse Glimmer, for example, calls it `reasoning_strength`.
                _native_param = str(
                    reasoning_capability.get("native_parameter") or "reasoning_effort")
                tpl_kwargs[_native_param] = _reasoning_cfg["native_effort"]
            # Templates without the switch ignore it, so a round that should not think
            # gets a tighter thinking guard below.
            no_think_forced = False
            if force_no_think:
                # Previous round blew the thinking budget — force a direct answer
                # this round regardless of the model's default thinking mode.
                tpl_kwargs["enable_thinking"] = False
                force_no_think = False
                no_think_forced = True
            if use_tools and tool_cap_no_think:
                # A previous round hit the output cap while working with tools: skip
                # thinking this round so the whole budget goes to the output, even for a
                # model that thinks by default.
                tpl_kwargs["enable_thinking"] = False
                no_think_forced = True
            tb = _reasoning_cfg.get("budget")
            try:
                tb_int = int(tb) if tb is not None else 2048
            except Exception:
                tb_int = 2048
            if tb_int >= 0:
                tpl_kwargs["thinking_budget"] = tb_int
            # A no-think round on a template whose switch rewrites the system
            # block: keep the turn's flags and prefill an empty think block
            # instead (see _no_think_prefill), so the cached prompt survives.
            # A carried tool call already owns the final assistant message.
            _no_think_prefill_text = ""
            if (no_think_forced and not _carry_call and enable_thinking is not False
                    and tpl_kwargs.get("enable_thinking") is False):
                _normal_kwargs = dict(tpl_kwargs)
                if enable_thinking is None:
                    _normal_kwargs.pop("enable_thinking", None)
                else:
                    _normal_kwargs["enable_thinking"] = True
                _no_think_prefill_text = _no_think_prefill(_normal_kwargs)
                if _no_think_prefill_text:
                    tpl_kwargs = _normal_kwargs
            # Sage-A1.5 Verification Mode: the template appends its trained
            # trigger to the system message. The self-review happens inside
            # the reasoning, so it only applies to rounds that think, and the
            # template rejects it without a non-empty leading system message.
            _first_msg = (payload.get("messages") or [{}])[0]
            if (_first_msg.get("role") == "system"
                    and str(_first_msg.get("content") or "").strip()
                    and tpl_kwargs.get("enable_thinking") is not False
                    and _verification_wanted(settings, _reasoning_cfg.get("effort"),
                                             str(settings.get("model_path") or ""))):
                tpl_kwargs["verification"] = True
            if tpl_kwargs:
                payload["chat_template_kwargs"] = tpl_kwargs
            if _no_think_prefill_text:
                payload["messages"] = list(payload["messages"]) + [
                    {"role": "assistant", "content": _no_think_prefill_text}]
                payload["continue_final_message"] = "content"
                payload["add_generation_prompt"] = False
            payload.update(_reasoning_payload_overrides(
                _reasoning_cfg, reasoning_capability))
            # Hard turn-boundary stops for models that sail past their own end-of-turn
            # token (seen on Qwen3.6 Q4): llama-server halts as soon as one of these
            # literal markers appears. Sent only once the model has done it (see
            # _turn_boundary_stops).
            _stops = _turn_boundary_stops()
            if _stops:
                payload["stop"] = _stops
            if use_tools and native_tools:
                payload["tools"] = _round_tools
                payload["tool_choice"] = "none" if final_answer_round else "auto"

            # Lead the context gauge: push the assembled prompt size before the round
            # starts. /api/ctx-stats only knows the last completed round, so the gauge
            # otherwise lags a round behind. Char-based estimate plus cached tools
            # overhead.
            def _emit_ctx_fill() -> None:
                try:
                    # Take the max of this round's estimate and the last real count: the estimate
                    # undercounts tool traffic, and the round-end stats event corrects the gauge
                    # with the true prompt_eval_count either way.
                    est = _ctx_prompt_tokens(payload["messages"], tools_overhead, token_scale)
                    real = _last_prompt_tokens_by_chat.get(chat_id, 0)
                    try:
                        _breakdown = _ctx_breakdown(payload["messages"], tools_overhead, token_scale)
                    except Exception:
                        _breakdown = None
                    if real >= est:
                        emit({"type": "ctx_fill", "prompt_tokens": real,
                              "capacity": ctx_limit, "source": "live",
                              "breakdown": _breakdown})
                    else:
                        emit({"type": "ctx_fill", "prompt_tokens": est,
                              "capacity": ctx_limit, "source": "estimate",
                              "breakdown": _breakdown})
                except Exception:
                    pass
            _emit_ctx_fill()

            # Open the stream. If the prompt overflows n_ctx (--no-context-shift rejects
            # instead of sliding), trim harder and retry; the tools-overhead estimate can
            # undershoot. Transient 5xx / OOM bodies get a short backoff retry. A fatal
            # path returns _graceful_server_failure(...) so the turn's completed tool work
            # is still persisted.
            resp = None
            request_no += 1
            _transient_retries = 0
            _llama_retries = 0
            _model_loading_waits = 0
            for _ctx_attempt in range(4):
                try:
                    resp = llama_post_stream("/v1/chat/completions", payload)
                    _open_round = {"msgs": payload.get("messages") or [],
                                   "ovh": tools_overhead, "scale": token_scale}
                    break
                except socket.timeout:
                    # No first byte within the generous first-token window —
                    # the server is prefilling something enormous or wedged.
                    emit({"type": "error",
                          "error": (f"llama-server produced no output within "
                                    f"{_LLAMA_FIRST_TOKEN_TIMEOUT_S}s — it is either prefilling a very "
                                    f"large prompt or stuck. Try the turn again; if this repeats, "
                                    f"restart the server (pick the model again in the model menu).")})
                    return _graceful_server_failure(
                        conversation, _start_len, _turn_id,
                        [f"llama-server produced no output within "
                         f"{_LLAMA_FIRST_TOKEN_TIMEOUT_S}s (prefill stall)"])
                except Exception as e:
                    if _ctx_attempt < 3 and _is_ctx_overflow(e):
                        pad = max(2048, int(ctx_limit * 0.12)) * (_ctx_attempt + 1)
                        tighter = min(effective_reserve + pad, ctx_limit - 1024)
                        trimmed = truncate_messages(_thinking_wire_view(
                            conversation, settings, keep_reasoning_from=_reasoning_keep_from),
                                                    ctx_limit, reserve=tighter,
                                                    token_scale=token_scale)
                        payload["messages"] = _sanitize_messages_for_openai(
                            trimmed, stub_old_tools=stub_old_tools)
                        _log_compact(chat_id, f"overflow_retry: attempt={_ctx_attempt + 1} "
                                              f"reserve={tighter} kept={len(trimmed)} "
                                              f"conv_n={len(conversation)} scale={token_scale:.3f} "
                                              f"last_fill={_last_prompt_tokens_by_chat.get(chat_id, 0)}")
                        _emit_ctx_fill()
                        note_intervention("context_retry")
                        emit({"type": "notice", "quiet": True,
                              "note": "prompt exceeded the context window — trimmed older turns and retrying"})
                        continue
                    if _model_loading_waits < 2 and _is_model_loading(e):
                        # The server is up but still loading the model (a reload,
                        # Auto-tune, the load-time speed check). That takes from
                        # seconds to minutes, so wait until it reports ready
                        # instead of two short retries failing the turn.
                        _model_loading_waits += 1
                        note_intervention("llama_wait")
                        emit({"type": "notice", "quiet": True,
                              "note": "the model is still loading — waiting for it before continuing"})
                        _load_deadline = time.time() + _MODEL_LOAD_WAIT_S
                        while time.time() < _load_deadline and not cancel_ev.is_set():
                            if llama_ready(timeout=2.0):
                                break
                            time.sleep(2.0)
                        if cancel_ev.is_set():
                            return _graceful_server_failure(
                                conversation, _start_len, _turn_id, ["stopped while the model was loading"])
                        continue
                    if _transient_retries < 2 and _is_transient_server_error(e):
                        _transient_retries += 1
                        _wait = 2.0 * _transient_retries
                        time.sleep(_wait)
                        _det = getattr(e, "code", None) or type(e).__name__
                        _body_tail = _http_error_body(e).strip().replace("\n", " ")[-160:]
                        note_intervention("server_retry")
                        emit({"type": "notice", "quiet": True,
                              "note": (f"llama-server faulted ({_det}) — retrying in {int(_wait)}s"
                                       + (f" · server said: …{_body_tail}" if _body_tail else ""))})
                        continue
                    if _llama_retries < 4 and _is_llama_unreachable(e):
                        # llama is not answering at all, most often because the watchdog is
                        # respawning it after a crash; wait for the server instead of failing the
                        # turn.
                        _llama_retries += 1
                        _wait = 4.0 * _llama_retries
                        _det = type(e).__name__
                        note_intervention("llama_wait")
                        emit({"type": "notice", "quiet": True,
                              "note": (f"llama-server is not answering ({_det}) — "
                                       f"waiting {int(_wait)}s for it to come back; "
                                       f"it may be restarting after a crash")})
                        time.sleep(_wait)
                        continue
                    if _is_ctx_overflow(e):
                        emit({"type": "error",
                              "error": ("prompt still exceeds the context window after trimming — the system "
                                        "prompt + tool specs alone are near your num_ctx. Raise num_ctx in "
                                        "Settings, or turn off some tools for this turn.")})
                        return _graceful_server_failure(
                            conversation, _start_len, _turn_id,
                            ["prompt still exceeds the context window after trimming"])
                    if isinstance(e, urllib.error.HTTPError):
                        _detail = (_llama_error_text(_http_error_body(e))[-400:] or str(e))
                        _note = (f"llama-server returned HTTP {e.code} — {_detail}. "
                                 "The inference server faulted mid-turn; the turn's completed tool "
                                 "work was preserved. Retry the turn, or restart the server "
                                 "(pick the model again in the model menu) if this repeats.")
                        emit({"type": "error", "error": _note})
                        return _graceful_server_failure(
                            conversation, _start_len, _turn_id, [_note])
                    _note = (f"llama-server request failed at {LLAMA}: {e}. Is it running? "
                             f"Start it with: llama-server -m <model.gguf> --host 127.0.0.1 "
                             f"--port 8080 --jinja. The turn's completed tool work was preserved.")
                    emit({"type": "error", "error": _note})
                    return _graceful_server_failure(
                        conversation, _start_len, _turn_id, [_note])
            if resp is None:
                return _graceful_server_failure(
                    conversation, _start_len, _turn_id,
                    ["could not open the inference stream"])
            _set_cancel_resp(chat_id, resp)

            content_buf: list[str] = []
            # The same stream split by channel: tool calls are parsed from the answer
            # only, so a call the model merely mentions while thinking never runs.
            reasoning_buf: list[str] = []
            answer_buf: list[str] = []
            tool_calls_by_index: dict[int, dict] = {}
            last_stats: dict = {}
            finish_reason = None
            # Text-tools mode streams the model's <tool_call> XML as content. Keep it out
            # of the live bubble: after an opener, suppress deltas until a closer (or end
            # of stream) and let extract_tool_calls parse the answer. If last round's call
            # was truncated and carried over, start this round already inside the block,
            # since the model continues the body without an opener of its own.
            stream_tool_suppress = False
            if use_tools and not native_tools and _carry_call:
                stream_tool_suppress = (
                    _TOOL_CODE_CLOSER_RE
                    if _TOOL_CODE_OPENER_RE.search(_carry_call)
                    else _TOOL_STREAM_PAIRS[0][1]
                )
            # With --reasoning-format deepseek, thinking arrives as its own
            # `reasoning_content` delta. Forward it on a dedicated
            # thinking_start/thinking_delta/thinking_end channel so the UI never guesses.
            # The persisted content_buf keeps literal <think>...</think> markers so
            # splitThinking works on stored history.
            reasoning_open = False
            runaway_stop = False   # tripped if a turn-boundary marker leaks into the answer
            # Harness-enforced thinking budget: chat templates often ignore
            # thinking_budget (Qwen3.6 ran 48k tokens on a 2048 cap), so count reasoning
            # chars here and force-close a runaway.
            think_chars = 0
            think_overflow = False
            think_stop_reason = ""
            _loop_buf: list[str] = []
            _loop_hits = 0
            # Live progress for a tool call whose arguments are still streaming:
            # nothing else shows until the call is complete.
            call_progress: dict = {}
            progress_scan_at = 0
            try:
                _think_cap = int(_reasoning_cfg.get("budget") if _reasoning_cfg.get("budget") is not None else 2048)
            except Exception:
                _think_cap = 2048
            # The guard fires at 2.5x the budget, or at 1x (at least 1024 tokens) in a
            # round that was asked not to think at all.
            _think_ceiling = (max(_think_cap, 1024) if no_think_forced else _think_cap * 2.5)

            try:
                for raw in resp:
                    if cancel_ev.is_set():
                        break
                    if _steering.pending(chat_id):
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        # Don't break: llama-server sends a bare timings object after [DONE], and the
                        # per-chat fill signal depends on it.
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue                    # llama-server may emit a bare timings object after [DONE]
                    if "timings" in obj and "choices" not in obj:
                        t = obj["timings"]
                        last_stats = {
                            "eval_count": t.get("predicted_n"),
                            "eval_duration": int((t.get("predicted_ms") or 0) * 1e6),
                            "prompt_eval_count": t.get("prompt_n"),
                            "prompt_cache_n": t.get("cache_n"),
                            "prompt_eval_duration": int((t.get("prompt_ms") or 0) * 1e6),
                            "draft_n": t.get("draft_n"),
                            "draft_n_accepted": t.get("draft_n_accepted"),
                        }
                        continue
                    if "usage" in obj and "choices" not in obj:
                        u = obj["usage"]
                        last_stats.setdefault("eval_count", u.get("completion_tokens"))
                        last_stats.setdefault("prompt_eval_count", u.get("prompt_tokens"))
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    fr = ch.get("finish_reason")
                    if fr:
                        finish_reason = fr
                    delta = ch.get("delta") or ch.get("message") or {}
                    # reasoning first — wrap as <think>…</think> for the UI
                    rpiece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    _rb = _TURN_BOUNDARY_RE.search(rpiece) if rpiece else None
                    if _rb:
                        rpiece = rpiece[:_rb.start()]
                        runaway_stop = True
                    if rpiece:
                        if not reasoning_seen_this_turn:
                            reasoning_seen_this_turn = True
                            _mark_model_reasoning_observed(
                                _llama.loaded_model() or settings.get("model_path") or model)
                            observed_cap = dict(reasoning_capability)
                            if not observed_cap.get("supported"):
                                observed_cap = {
                                    "supported": True, "mode": "budget",
                                    "source": "observed_output",
                                    "levels": list(_REASONING_LEVELS),
                                    "description": "Accuretta observed a reasoning stream and can adjust its guarded token budget.",
                                }
                            emit({"type": "reasoning_capability",
                                  "capability": observed_cap})
                        if not reasoning_open:
                            content_buf.append("<think>")
                            reasoning_buf.append("<think>")
                            emit({"type": "thinking_start", "round": request_no})
                            reasoning_open = True
                        content_buf.append(rpiece)
                        reasoning_buf.append(rpiece)
                        emit({"type": "thinking_delta", "content": rpiece})
                        # The thinking budget is a soft target passed to the template; this is only
                        # a runaway guard, firing at _think_ceiling tokens.
                        think_chars += len(rpiece)
                        # Degeneration loop detector: a stuck model (often a quantized MoE reasoner)
                        # repeats its own reasoning verbatim. Cheap immediate-repeat test on the last
                        # 24 chunks, only past 400 chars so "1. 2. 3." lists can't trip it. Loop cuts
                        # count toward thinking_only_streak like budget cuts, so a model that keeps
                        # looping ends the turn.
                        _loop_buf.append(rpiece)
                        if len(_loop_buf) > 24:
                            _loop_buf.pop(0)
                        if len(_loop_buf) == 24 and _loop_buf[-6:] == _loop_buf[-12:-6]:
                            _loop_hits += 1
                        _in_loop = (_loop_hits >= 3 and think_chars > 400)
                        _over_budget = (
                            _think_cap > 0
                            and think_chars / CHARS_PER_TOKEN > _think_ceiling)
                        if _over_budget or _in_loop:
                            content_buf.append("</think>")
                            reasoning_buf.append("</think>")
                            emit({"type": "thinking_end"})
                            reasoning_open = False
                            think_overflow = True
                            think_stop_reason = "budget" if _over_budget else "loop"
                            if _in_loop and not _over_budget:
                                print(f"[chat] reasoning loop detected at {think_chars} chars — "
                                      "forcing the answer", flush=True)
                            break
                    if runaway_stop:
                        break
                    piece = delta.get("content") or ""
                    if piece:
                        # A turn-boundary marker in the content means llama-server's stop didn't fire
                        # and the model is starting a fresh turn. Keep what came before and cut here.
                        _b = _TURN_BOUNDARY_RE.search(piece)
                        if _b:
                            piece = piece[:_b.start()]
                            runaway_stop = True
                        if reasoning_open:
                            content_buf.append("</think>")
                            reasoning_buf.append("</think>")
                            emit({"type": "thinking_end"})
                            reasoning_open = False
                        if piece:
                            content_buf.append(piece)
                            answer_buf.append(piece)
                            # Suppress raw <tool_call> XML in text-tools mode, keeping only the prose; the
                            # call is parsed from the answer afterwards.
                            if use_tools and not native_tools:
                                _vn, stream_tool_suppress = _classify_tool_stream(
                                    piece, stream_tool_suppress)
                                if _vn:
                                    emit({"type": "delta", "content": _vn})
                                progress_scan_at += len(piece)
                                if progress_scan_at >= 512:
                                    progress_scan_at = 0
                                    _open_call = _text_call_progress("".join(answer_buf))
                                    if _open_call:
                                        _maybe_emit_call_progress(emit, call_progress, "text", *_open_call)
                            else:
                                emit({"type": "delta", "content": piece})
                        if runaway_stop:
                            break
                    # Tool-call deltas arrive as fragments; `arguments` is a string that
                    # concatenates into a JSON blob across chunks.
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        slot = tool_calls_by_index.setdefault(idx, {
                            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "name": "",
                            "arguments": "",
                        })
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        args_chunk = fn.get("arguments")
                        if args_chunk:
                            if isinstance(args_chunk, dict):
                                # non-streaming mode occasionally returns a dict directly
                                slot["arguments"] = json.dumps(args_chunk, ensure_ascii=False)
                            else:
                                slot["arguments"] += args_chunk
                            _maybe_emit_call_progress(emit, call_progress, idx, slot["name"],
                                                      slot["arguments"])
                    if obj.get("usage"):
                        u = obj["usage"]
                        last_stats.setdefault("eval_count", u.get("completion_tokens"))
                        last_stats.setdefault("prompt_eval_count", u.get("prompt_tokens"))
                    # This llama-server sends no OpenAI-style "usage" in streamed chunks; the
                    # final chunk carries "timings" (prompt_n / predicted_n) next to choices,
                    # followed by data: [DONE]. The per-chat fill signal comes from here.
                    t = obj.get("timings")
                    if t:
                        last_stats.setdefault("eval_count", t.get("predicted_n"))
                        last_stats.setdefault("prompt_eval_count", t.get("prompt_n"))
                        last_stats.setdefault("prompt_cache_n", t.get("cache_n"))
                        last_stats.setdefault("eval_duration", int((t.get("predicted_ms") or 0) * 1e6))
                        last_stats.setdefault("prompt_eval_duration", int((t.get("prompt_ms") or 0) * 1e6))
                        # Speculative acceptance feeds the tuner's per-model
                        # draft-length choice (absent when spec is off).
                        last_stats.setdefault("draft_n", t.get("draft_n"))
                        last_stats.setdefault("draft_n_accepted", t.get("draft_n_accepted"))
                if runaway_stop:
                    _mark_turn_boundary_runaway()
                # if the stream ended while still inside reasoning (no answer
                # tokens came), close the channel so the UI can render it cleanly.
                if reasoning_open:
                    content_buf.append("</think>")
                    reasoning_buf.append("</think>")
                    emit({"type": "thinking_end"})
                    reasoning_open = False
                if last_stats.get("eval_count") is not None:
                    # timings.prompt_n is the KV-cache-incremental eval count (only the newly
                    # evaluated tail when the prefix is reused), not the prompt size. Fed to the
                    # gauge and summary trigger it read as tens of tokens after every cached
                    # round. Recompute the real full-prompt count from the payload sent, with the
                    # trimmer's /tokenize-derived scale (system + tools overhead included).
                    _raw_pe = last_stats.get("prompt_eval_count") or 0
                    try:
                        _sent_msgs = payload.get("messages") or []
                        _real_prompt = _ctx_prompt_tokens(_sent_msgs, tools_overhead, token_scale)
                        try:
                            last_stats["ctx_breakdown"] = _ctx_breakdown(
                                _sent_msgs, tools_overhead, token_scale)
                        except Exception:
                            pass
                        try:
                            _log_compact(chat_id,
                                         f"round: n={len(_sent_msgs)} sum={int(sum(_count_msg_tokens(m) for m in _sent_msgs))} "
                                         f"scale={token_scale:.3f} ovh={tools_overhead} raw_pe={_raw_pe} real={_real_prompt}")
                        except Exception:
                            pass
                    except Exception:
                        _real_prompt = _raw_pe
                    # The scale is measured on the whole conversation (old reasoning
                    # included) but applied to the stripped wire view, so the estimate
                    # ran ~20% low and the proactive fold never fired. The server's own
                    # count is ground truth: cached + fresh when reported, else at least
                    # the freshly evaluated part.
                    _cache_n = last_stats.get("prompt_cache_n")
                    if isinstance(_cache_n, (int, float)) and _cache_n >= 0:
                        _server_prompt = int(_cache_n) + int(_raw_pe or 0)
                    else:
                        _server_prompt = int(_raw_pe or 0)
                    if _server_prompt > int(_real_prompt or 0):
                        _real_prompt = _server_prompt
                    last_stats["prompt_eval_count"] = _real_prompt
                    emit({"type": "stats", **last_stats})
                    # Turn totals keep the raw incremental counts; summing full-prompt counts
                    # would count the shared prefix every round.
                    turn_eval_total += last_stats.get("eval_count") or 0
                    turn_prompt_total += _raw_pe
                    # The lifetime counter takes the whole prompt this request
                    # carried: llama-server's exact cached + fresh split when
                    # the build reports cache_n, else the payload-derived size.
                    if isinstance(_cache_n, (int, float)) and _cache_n >= 0:
                        turn_full_prompt_total += int(_cache_n) + int(_raw_pe or 0)
                    else:
                        turn_full_prompt_total += max(int(_real_prompt or 0), int(_raw_pe or 0))
                    _open_round = None
                    turn_eval_ms_total += max(0.0, float(last_stats.get("eval_duration", 0) or 0) / 1e6)
                    turn_prompt_ms_total += max(0.0, float(last_stats.get("prompt_eval_duration", 0) or 0) / 1e6)
                    turn_draft_n += max(0, int(last_stats.get("draft_n") or 0))
                    turn_draft_accepted += max(0, int(last_stats.get("draft_n_accepted") or 0))
                    turn_peak_prompt = max(turn_peak_prompt, int(_real_prompt or 0))
                    STATE.last_prompt_tokens = _real_prompt
                    # Per-chat truth for the gauge + summary trigger. Once set,
                    # the cold-start tokenize estimate for this chat is moot.
                    if STATE.last_prompt_tokens:
                        _last_prompt_tokens_by_chat[chat_id] = STATE.last_prompt_tokens
                        _CTX_EST_CACHE.pop(chat_id, None)
            except socket.timeout:
                # Inter-token stall: llama-server stopped emitting. A wedged generation is
                # not a finished answer, so don't persist the partial buffer; what already
                # streamed to the UI stays there.
                _stall_txt = "".join(content_buf)
                print(f"[chat] turn stalled: no tokens for {_LLAMA_STALL_TIMEOUT_S}s "
                      f"(streamed {len(_stall_txt)} chars)", flush=True)
                try:
                    emit({"type": "error",
                          "error": (f"llama-server went silent for {_LLAMA_STALL_TIMEOUT_S}s mid-generation — "
                                    f"the turn was stopped. It may be a wedged slot; retry the turn, or "
                                    f"restart the server (pick the model again in the model menu) if it repeats.")})
                except Exception:
                    pass
                return None
            except (ConnectionError, TimeoutError, http.client.HTTPException, ssl.SSLError) as e:
                # llama-server died mid-generation (process killed, slot wedged). Unlike the
                # stall above, tokens were flowing, so return the partial marked failed: the
                # caller persists it and both surfaces show where the turn stopped instead of
                # leaving a user message with no reply.
                _dead_txt = "".join(content_buf)
                print(f"[chat] llama-server stream cut mid-generation: {type(e).__name__} "
                      f"(streamed {len(_dead_txt)} chars)", flush=True)
                try:
                    emit({"type": "error",
                          "error": ("llama-server stopped mid-generation — the stream was cut. "
                                    "The partial reply is saved below marked as failed. "
                                    "Restart the server (pick the model again in the model menu), then retry the turn.")})
                except Exception:
                    pass
                partial = {"role": "assistant", "content": _dead_txt}
                partial["_failed"] = True
                partial["_appended_intermediate"] = _drop_invalid_call_pairs(conversation[_start_len:])
                partial["_build"] = _BUILD_TAG
                partial["_turn_id"] = _turn_id
                return partial
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                _set_cancel_resp(chat_id, None)

            if use_tools and not native_tools:
                _pending_visible = _flush_tool_stream(stream_tool_suppress)
                if _pending_visible:
                    emit({"type": "delta", "content": _pending_visible})

            if cancel_ev.is_set():
                try:
                    emit({"type": "notice", "note": "stopped by user", "quiet": True})
                except Exception:
                    pass
                partial = {"role": "assistant", "content": "".join(content_buf)}
                partial["_stopped"] = True
                partial["_appended_intermediate"] = _drop_invalid_call_pairs(conversation[_start_len:])
                partial["_build"] = _BUILD_TAG
                partial["_turn_id"] = _turn_id
                return partial

            _updates = _steering.take(chat_id)
            if _updates:
                # The update cut this reply short: keep what was written (not a
                # carried-over tool-call body, which is only call syntax).
                _kept = None
                if not _carry_call:
                    _it, _at = _split_leading_think("".join(answer_buf))
                    _kept = _steer_kept_partial("".join(reasoning_buf) + _it, _at)
                _apply_task_updates(_updates, _kept)
                _carry_call = ""
                continue
            # maya's server has a loop guard of its own: it ends a looping reply with
            # this note as the whole answer, and the user got nothing else (twice on
            # 2026-11-21). Treat it as our own loop stop: drop the note and re-ask
            # without thinking, below.
            if not think_overflow and _MAYA_LOOP_STOP_NOTE in "".join(answer_buf):
                think_overflow = True
                think_stop_reason = "loop"
                answer_buf = ["".join(answer_buf).replace(_MAYA_LOOP_STOP_NOTE, "").rstrip()]
            # Thinking-budget enforcer tripped: we force-closed a runaway <think>
            # block. Re-prompt with thinking disabled so the model goes straight into
            # the answer. Once the retries are spent, fall through: the thinking-only
            # check below ends the turn.
            if think_overflow:
                thinking_only_streak += 1
                rounds += 1
                _retry = (thinking_only_streak <= _THINKING_ONLY_RETRIES
                          and rounds < max_tool_rounds)
                _then = "writing the answer now" if _retry else "stopping the turn"
                if think_stop_reason == "budget":
                    approx_used = max(1, round(think_chars / CHARS_PER_TOKEN))
                    safety_ceiling = max(1, math.ceil(_think_ceiling))
                    note = (f"reasoning safety ceiling reached at ~{approx_used:,} tokens "
                            f"({_think_cap:,} target; ~{safety_ceiling:,} ceiling) — {_then}")
                else:
                    note = f"repetitive reasoning loop stopped — {_then}"
                note_intervention("reasoning_guard")
                emit({"type": "notice", "note": note, "quiet": True})
                if _retry:
                    stop_explanation = (
                        "You reached the reasoning safety ceiling."
                        if think_stop_reason == "budget"
                        else "Your reasoning entered a repetition loop."
                    )
                    conversation = _with_thinking_note(conversation, _start_len, _thinking_recovery_note(
                        stop_explanation, "".join(reasoning_buf), use_tools))
                    force_no_think = True
                    continue

            # Tool calls are detected, parsed and stripped in the answer only. The saved
            # message is the reasoning block followed by that answer.
            reasoning_text = "".join(reasoning_buf)
            inline_think, answer_text = _split_leading_think("".join(answer_buf))
            reasoning_text += inline_think
            if use_tools and not tool_calls_by_index and not answer_text.strip():
                reasoning_text, answer_text = _recover_trailing_calls(reasoning_text)

            # Carry-over merge: last round's <tool_call> was truncated and stashed, and
            # the model was told to continue it without reopening tags, so this round's
            # text is only the missing part. Prepend the stub so extract_tool_calls sees
            # the whole call.
            _continued_carried_call = bool(use_tools and not native_tools and _carry_call)
            if _continued_carried_call:
                answer_text = _merge_carried_tool_call(_carry_call, answer_text)
                # Remove the transient assistant-prefill message. If the merged
                # call is still incomplete, the guard below installs one updated
                # prefill message. It must never be persisted as a separate turn.
                conversation = [
                    _m for _m in conversation
                    if not (isinstance(_m, dict) and _m.get("_tool_call_prefill"))
                ]
                _carry_call = ""

            thinking_only = (not tool_calls_by_index and not _thinking_body(answer_text)
                             and bool(think_overflow or _thinking_body(reasoning_text)))
            if not thinking_only:
                thinking_only_streak = 0

            # Mid-call truncation guard. When llama.cpp cuts the stream at the
            # token/thinking budget the reply can end inside a <tool_call>, and the
            # tolerant parser would write the partial `content` as a truncated file
            # reported "ok". An opener with no closer anywhere means the body is only a
            # prefix. The fix is carry, not re-emit: a big file can exceed even the raised
            # budget, so store the partial and have the model continue the same call next
            # round.
            # Not in the round-limit answer round: no call can run there, and treating
            # call text the model wrote anyway as a cut-off file write ended a red-team
            # turn on "the oversized file operation stopped making progress".
            if use_tools and not final_answer_round and _has_unclosed_tool_call(answer_text):
                _next_carry = _unclosed_tool_tail(answer_text)
                _next_size = len(_next_carry)
                if _continued_carried_call:
                    if _next_size <= carry_last_size + 16:
                        carry_stalls += 1
                    else:
                        carry_stalls = 0
                else:
                    carry_stalls = 0

                _cut_name = _cut_off_call_name(_next_carry)
                _runaway = bool(_cut_name) and not _is_file_write_call(_cut_name) \
                    and _next_size > _CARRY_OTHER_CALL_MAX_CHARS
                # A server that can't continue a trailing assistant message (maya)
                # would start the call over; the fresh-turn recovery below fits it.
                _can_continue = _llama.continues_replies()
                _can_carry = (
                    not _runaway
                    and carry_stalls < 2
                    and carry_rounds < 8
                    and rounds < max_tool_rounds - 1
                    and _can_continue
                )
                if _can_carry:
                    carry_rounds += 1
                    carry_last_size = _next_size
                    _carry_call = _next_carry
                    _carry_diag.append(
                        f"carried call mid-stream (round {rounds}, carry {carry_rounds}, "
                        f"chars={_next_size}, stalls={carry_stalls}, native={native_tools})")
                    if native_tools:
                        # The reply was cut inside an XML call that native-tools also framed as a
                        # tool_call. Don't parse a truncated call natively; force the text path for
                        # the rest of this turn so the carried tail parses consistently. Turn-local
                        # only: hitting the output budget is not evidence that llama.cpp's parser
                        # drops args, so it must not mark the model persistently.
                        native_tools = False
                    conversation.append({
                        "role": "assistant",
                        "content": reasoning_text + answer_text,
                        "_tool_call_prefill": True,
                    })
                    note_intervention("tool_call_repair")
                    emit({"type": "notice", "quiet": True,
                          "note": f"reply hit the output budget inside a tool call — continuing it "
                                  f"across rounds ({carry_rounds})"})
                    continue

                # One bounded recovery gives the model a fresh turn and an
                # explicit chunked-write strategy. The incomplete call was never
                # executed. If recovery also stalls, finish safely instead of
                # spending another eight output budgets on the same prefix.
                _reason = (
                    f"{_cut_name} call ran away ({_next_size} chars), not a file write"
                    if _runaway else
                    "continuation made no forward progress"
                    if carry_stalls >= 2 else
                    "the model server can't continue a cut-off reply"
                    if not _can_continue else
                    "continuation budget was exhausted"
                )
                _carry_diag.append(
                    f"carry recovery: {_reason} (round {rounds}, carry {carry_rounds}, "
                    f"chars={_next_size}, stalls={carry_stalls})")
                _carry_call = ""
                carry_rounds = 0
                carry_last_size = 0
                carry_stalls = 0
                conversation = [
                    _m for _m in conversation
                    if not (isinstance(_m, dict) and _m.get("_tool_call_prefill"))
                ]
                if carry_recoveries < 1 and rounds < max_tool_rounds - 1:
                    carry_recoveries += 1
                    tool_cap_no_think = True
                    if _runaway:
                        _recovery = (
                            f"[automatic recovery] Your {_cut_name} call ran past the output limit "
                            f"({_next_size:,} characters) without finishing, so it was NOT executed; "
                            "its arguments most likely repeated. Send it again with short arguments, "
                            "no repeated text. Continue now without explaining the recovery.")
                        _recovery_note = f"{_cut_name} call ran past the output limit; asking for it again, short"
                    else:
                        _recovery = (
                            "[automatic recovery] The oversized tool call did not finish and was NOT "
                            "executed. Do not restart the same full-file call. Instead, "
                            + _FILE_IN_PARTS + ". Keep each call comfortably below the output "
                            "limit. Continue now without explaining the recovery.")
                        _recovery_note = "oversized tool call stopped advancing; switching to smaller file edits"
                    conversation.append({
                        "role": "system" if _rt_trailing_system_ok() else "user",
                        "content": _recovery,
                    })
                    note_intervention("tool_call_repair")
                    emit({"type": "notice", "quiet": True, "note": _recovery_note})
                    continue

                dangling = _unclosed_tool_tail(answer_text)
                if dangling:
                    answer_text = answer_text[:max(0, len(answer_text) - len(dangling))].strip()
                _completed_tools = [item.get("name") for item in turn_tool_outcomes
                                    if item.get("ok")]
                _completed_note = (
                    " A previous tool action did complete before the follow-up stalled."
                    if _completed_tools else " No tool action was executed from the incomplete call."
                )
                if _runaway:
                    answer_text = (answer_text + "\n\n" if answer_text else "") + (
                        f"My {_cut_name} calls kept running past the output limit without finishing, "
                        f"so I stopped here; those calls did not run." + _completed_note
                        + " Ask again and I'll continue from the findings above.")
                    emit({"type": "error",
                          "error": f"The model's {_cut_name} call ran past the output limit twice; "
                                   f"it did not run."})
                else:
                    answer_text = (answer_text + "\n\n" if answer_text else "") + (
                        "The oversized file operation stopped making progress, so I stopped the follow-up safely."
                        + _completed_note + " Retry the request and I will write the file in smaller sections."
                    )
                    emit({"type": "error",
                          "error": ("Oversized file generation stalled after a previous action completed."
                                    if _completed_tools else
                                    "Oversized file generation stalled; no incomplete tool call was executed.")})

            # assemble native tool calls
            parsed_calls: list[dict] = []
            for idx in sorted(tool_calls_by_index.keys()):
                slot = tool_calls_by_index[idx]
                if not slot.get("name"):
                    continue
                args = _normalize_tool_argument_shapes(
                    slot["name"], repair_tool_args(slot.get("arguments", "")))
                parsed_calls.append({"id": slot["id"], "name": slot["name"], "arguments": args})

            # llama.cpp sometimes keeps one argument (often `path`) while dropping a bulky
            # one (`content`), so repair each missing required field independently
            # instead of skipping recovery because the call isn't empty.
            _merge_fallback_used = False
            if use_tools and parsed_calls and answer_text:
                for _pc in parsed_calls:
                    _pc_args = _pc.get("arguments")
                    if not isinstance(_pc_args, dict):
                        _pc["arguments"] = {}
                        _pc_args = _pc["arguments"]
                    _missing = _missing_required_tool_args(
                        _pc.get("name") or "", _pc_args)
                    if not _missing:
                        continue
                    for _fc in extract_tool_calls(answer_text, python_calls=not native_tools):
                        _fname = (_fc.get("name") or _fc.get("tool") or "").lower()
                        _fargs = _fc.get("arguments")
                        if (_fname == (_pc.get("name") or "").lower()
                                and isinstance(_fargs, dict)):
                            for _key in _missing:
                                _value = _fargs.get(_key)
                                if (_value is not None
                                        and not (isinstance(_value, str) and not _value.strip())
                                        and not (isinstance(_value, (list, dict)) and not _value)):
                                    _pc_args[_key] = _value
                                    _merge_fallback_used = True
                            break
            if _merge_fallback_used:
                # the <tool_call> block is what produced the parsed args, so don't
                # leak it into the bubble as raw text.
                answer_text = _strip_tool_markup(
                    answer_text,
                    r"(?:<|&lt;|\\<)?tool_call(?:>|&gt;|\\>)?[\s\S]*?"
                    r"((?:<|&lt;|\\<)?/tool_call(?:>|&gt;|\\>)?|$)")
                answer_text = _strip_tool_markup(answer_text, r"```tool_call[\s\S]*?(?:```|$)")
                answer_text = answer_text.strip()

            # fallback: parse tool calls emitted in content (hermes/qwen/llama/mistral/named)
            if not parsed_calls and use_tools:
                for c in extract_tool_calls(answer_text, python_calls=not native_tools):
                    name = c.get("name") or c.get("tool")
                    args = c.get("arguments") or c.get("args") or {}
                    if not isinstance(args, dict):
                        args = repair_tool_args(args)
                    if name:
                        parsed_calls.append({
                            "id": f"call_{uuid.uuid4().hex[:12]}",
                            "name": name,
                            "arguments": args,
                        })
                if parsed_calls:
                    # Strip fallback tool-call blocks so they don't leak into the UI as clear text
                    answer_text = _strip_tool_markup(answer_text, r"(?:<|&lt;|\\<)?tool_call(?:>|&gt;|\\>)?[\s\S]*?((?:<|&lt;|\\<)?/tool_call(?:>|&gt;|\\>)?|$)")
                    answer_text = _strip_tool_markup(answer_text, r"```tool_call[\s\S]*?(?:```|$)")
                    # Gemma python-call fence (```tool_code``` / ```python``` that
                    # held a real call we just parsed) — drop it so the bubble
                    # doesn't show the raw call text.
                    for _nm in {c["name"] for c in parsed_calls}:
                        answer_text = re.sub(
                            r"```(?:tool_code|python)[\s\S]*?" + re.escape(_nm) + r"\s*\([\s\S]*?(?:```|$)",
                            "", answer_text, flags=re.IGNORECASE)
                        # bare/bracketed one-liner form of the same call
                        answer_text = re.sub(
                            r"^[ \t]*\[?\s*(?:print\()?\s*" + re.escape(_nm) + r"\s*\([^\n]*\)\s*\)?\s*\]?[ \t]*$",
                            "", answer_text, flags=re.IGNORECASE | re.MULTILINE)
                    answer_text = re.sub(r"\[TOOL_CALLS\][\s\S]*?(?:\]|$)", "", answer_text, flags=re.IGNORECASE)
                    if '"name"' in answer_text or "'name'" in answer_text or '"function"' in answer_text or "'function'" in answer_text:
                        for s, e in reversed(_find_balanced_json_objects(_tool_text_view(answer_text))):
                            chunk = answer_text[s:e]
                            try:
                                parsed = json.loads(chunk)
                                if isinstance(parsed, dict) and (parsed.get("name") or parsed.get("function", {}).get("name")):
                                    answer_text = answer_text[:s] + answer_text[e:]
                            except Exception:
                                pass
                    answer_text = answer_text.strip()

                # The model emitted tool-call syntax but nothing parsed: almost always a chat
                # template / dialect mismatch, meaning the rest of the reply narrates a tool
                # that never ran. Surface that to the UI.
                if not parsed_calls and TOOL_SYNTAX_HINT_RE.search(_tool_text_view(answer_text or "")):
                    print(
                        "[tool] WARNING: model emitted tool-call syntax but "
                        "no dialect matched - likely chat-template mismatch",
                        flush=True,
                    )
                    emit({
                        "type": "tool_dialect_warning",
                        "message": (
                            "Model produced tool-call syntax that this build "
                            "couldn't parse. The reply may be a hallucination "
                            "- no tool actually ran."
                        ),
                    })
                    # Never persist malformed tool syntax as an ordinary
                    # assistant answer. Keep any surrounding prose, but hide
                    # the broken call body from the chat bubble.
                    answer_text = _strip_unparsed_tool_markup(answer_text)

            _updates = _steering.take(chat_id)
            if _updates:
                # The reply finished just as the update arrived. Its tool calls are
                # dropped (they never ran, and the update may change them); the text
                # the user saw stays.
                _apply_task_updates(_updates, _steer_kept_partial(reasoning_text, answer_text))
                _carry_call = ""
                continue
            turn_tool_calls += len(parsed_calls)
            assistant_msg = {"role": "assistant", "content": reasoning_text + answer_text}
            if parsed_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in parsed_calls
                ]

            # /goal: the round cap is a checkpoint. While the stretch since the
            # last checkpoint got real work done, the same turn carries on (so
            # the task keeps its reasoning and the prompt cache) and the call
            # just generated runs instead of being dropped. A loop-guard stop,
            # a cancelled turn or a stretch without progress still ends it.
            if (parsed_calls and rounds >= max_tool_rounds and use_tools
                    and not refused_broke and not cancel_ev.is_set()
                    and _chat_in_goal_mode(chat_id)
                    and _goal_window_progressed(turn_tool_outcomes[_goal_window_from:])):
                _goal_renewals += 1
                _goal_window_from = len(turn_tool_outcomes)
                max_tool_rounds = rounds + _goal_round_budget
                note_intervention("goal_round_renewal")
                emit({"type": "notice", "quiet": True,
                      "note": f"/goal: {rounds} tool rounds done and still making progress, "
                              f"continuing (stretch {_goal_renewals + 1})"})
            if not parsed_calls or rounds >= max_tool_rounds:
                terminal_error = None
                _thinking_stopped = False   # the thinking-only guard is ending the turn
                research_id = _research_context(chat_id)[1]
                if research_id and not parsed_calls and use_tools and rounds < max_tool_rounds and research_retries < 2:
                    notebook = _research_store.get(research_id, chat_id)
                    if notebook["notes"] and notebook["status"] != "complete":
                        research_retries += 1
                        conversation.append({"role": "user", "_synthetic": True, "content":
                            "The research notebook has evidence but no published presentation. Call research_notebook, "
                            "resolve uncovered questions or declare the gaps, then call research_publish. "
                            "Do not claim the presentation exists before the tool succeeds."})
                        rounds += 1
                        continue
                _final_text = _visible_final_answer(assistant_msg.get("content") or "")
                if research_id and not parsed_calls and not _final_text:
                    notebook = _research_store.get(research_id, chat_id)
                    if notebook["status"] == "complete" and notebook.get("presentation"):
                        assistant_msg["content"] = "Your research presentation is ready. Open it to review the findings and sources."
                        _final_text = assistant_msg["content"]
                        finish_reason = "stop"
                if (parsed_calls and rounds >= max_tool_rounds and not final_answer_round
                        and not refused_broke):
                    # (refused_broke: the loop guard already asked for the final answer.)
                    final_answer_round = True
                    if _visible_final_answer(assistant_msg.get("content") or ""):
                        conversation.append({"role": "assistant",
                                             "content": assistant_msg.get("content") or ""})
                    conversation.append({
                        "role": "system" if _rt_trailing_system_ok() else "user",
                        "content": _ROUND_LIMIT_ANSWER_NOTE.format(n=max_tool_rounds),
                    })
                    note_intervention("round_limit_answer")
                    emit({"type": "notice", "quiet": True,
                          "note": f"used all {max_tool_rounds} tool rounds; asking for the answer "
                                  f"without more tools"})
                    continue
                if final_answer_round or (parsed_calls and rounds >= max_tool_rounds):
                    # The answer round (or the loop guard's forced final reply): any
                    # call it still wrote does not run, and the answer says the turn
                    # stopped at the limit.
                    assistant_msg["tool_calls"] = []
                    parsed_calls = []
                    # Call syntax it wrote as text anyway is not part of the answer.
                    assistant_msg["content"] = _strip_unparsed_tool_markup(
                        assistant_msg.get("content") or "")
                    _final_text = _visible_final_answer(assistant_msg.get("content") or "")
                    if _final_text:
                        assistant_msg["content"] = (assistant_msg.get("content") or "").rstrip() + (
                            f"\n\n_(Stopped at this turn's tool-round limit of {max_tool_rounds}; "
                            f"the last tool call did not run.)_")
                    else:
                        assistant_msg["content"] = (
                            "I reached the tool-round limit before the final tool call could run. "
                            "That call was not executed."
                        )
                        _final_text = assistant_msg["content"]
                        terminal_error = assistant_msg["content"]
                elif thinking_only:
                    # Stopped at the output cap or ended inside the thinking. Guard cuts
                    # retry above and only reach this once their retries are spent. Ask
                    # again with thinking off; after that, end the turn so the model
                    # cannot draft the same file over and over.
                    if not think_overflow:
                        thinking_only_streak += 1
                        rounds += 1
                    if (not think_overflow and thinking_only_streak <= _THINKING_ONLY_RETRIES
                            and rounds < max_tool_rounds):
                        conversation = _with_thinking_note(conversation, _start_len, _thinking_recovery_note(
                            "Your previous reply hit the output limit while you were still thinking."
                            if finish_reason == "length" else
                            "Your previous reply had only thinking: no answer and no tool call.",
                            reasoning_text, use_tools))
                        force_no_think = True
                        note_intervention("reasoning_guard")
                        emit({"type": "notice", "quiet": True,
                              "note": "reply had only thinking; asking again with thinking off"})
                        continue
                    completed = any(item.get("ok") for item in turn_tool_outcomes)
                    _thinking_stopped = True
                    terminal_error = (
                        "The model kept planning in its thinking and never wrote an answer or a tool "
                        f"call, so I stopped the turn after {thinking_only_streak} tries. "
                        + ("A tool action earlier in this turn did complete." if completed
                           else "Nothing was changed."))
                elif not _final_text and empty_retries < 2:
                    empty_retries += 1
                    conversation.append({
                        "role": "system" if _rt_trailing_system_ok() else "user",
                        "content": (
                            "[automatic recovery] Your previous output contained no user-visible answer and no "
                            "executable tool call. Complete the request now. If the user asked to save code that "
                            "already exists in a visible fence, call write_file with source='visible_code_block' "
                            "(or remote_write_file for the selected Mac). Do not repeat the file body, do not "
                            "explain, and do not ask for permission."
                        ),
                    })
                    note_intervention("empty_reply_retry")
                    emit({"type": "notice", "quiet": True,
                          "note": "model returned no executable action or visible answer — retrying"})
                    continue
                elif not _final_text:
                    completed = any(item.get("ok") for item in turn_tool_outcomes)
                    terminal_error = (
                        "The requested tool action completed, but the model did not produce a final response."
                        if completed else
                        "I could not complete the requested action because the model did not produce an "
                        "executable tool call after two retries. Nothing was changed."
                    )
                # Token-limit truncation (finish_reason "length"): continue the same answer in
                # a follow-up request so long reports don't end mid-sentence. At most 3
                # continuations per turn.
                if (finish_reason == "length" and auto_continues < 3
                        and rounds < max_tool_rounds and not terminal_error):
                    auto_continues += 1
                    if use_tools:
                        # The budget was spent; the next round must NOT re-enter
                        # thinking and reproduce the plan, it must emit output.
                        tool_cap_no_think = True
                    conversation.append(assistant_msg)
                    _cap_note = ("[automatic note] Your previous reply was cut off at the token limit. "
                                 "Continue from exactly where it stopped and finish the remaining part of "
                                 "the answer or code — do not repeat, do not summarize, do not ask. If you "
                                 "were mid-tool-call, emit the rest of that call now.")
                    if use_tools and not native_tools:
                        # A tool turn whose thinking-verbosity cost us the whole
                        # budget without a single call. Demand the call directly.
                        _cap_note += (" DO NOT write a plan, do not explain your approach, do not use "
                                      "thinking tags. Begin IMMEDIATELY with the tool call XML "
                                      "(<tool_call>…</tool_call>) and put the entire requested file "
                                      "body inside it.")
                    conversation.append({"role": "system" if _rt_trailing_system_ok() else "user",
                                         "content": _cap_note})
                    continue
                # Mid-plan stall: tools already ran and the reply announces more steps (or
                # asks "shall I continue?") without a tool call. Small models, Gemma
                # especially, do this after every tool result; bump automatically, within the
                # same continuation budget.
                if (_final_text and rounds > 0 and rounds < max_tool_rounds
                        and auto_continues < 3 and _looks_unfinished(_final_text, finish_reason)):
                    auto_continues += 1
                    conversation.append(assistant_msg)
                    _fence_hint = ("" if native_tools
                                   else " Emit the ```tool_code``` fence for the next call.")
                    conversation.append({
                        "role": "user",
                        "content": (
                            "[automatic note] You announced more steps but stopped without a tool call. "
                            "Continue NOW with the next step — make the call instead of describing it."
                            + _fence_hint +
                            " Do not ask for permission and do not restate the plan. If every step is "
                            "actually complete, write the final summary of what was done instead."
                        ),
                    })
                    note_intervention("auto_continue")
                    emit({"type": "notice", "quiet": True,
                          "note": f"model paused mid-plan — auto-continuing ({auto_continues}/3)"})
                    continue
                # /goal: a reply that would end the turn while the model's own plan
                # still has open steps is a checkpoint too, like the round cap. It
                # carries on only after real progress since the last checkpoint, so
                # a model stuck thinking, or waiting on the user, stops at its next
                # stall exactly as before. Normal turns are unchanged. After a
                # thinking-only stop the draft is still not fed back (the note
                # replaces the thinking note) and the next round runs without
                # thinking, as the thinking-only retry does.
                _open_steps = _goal_open_steps(_plan_tail)
                if (_open_steps and (not terminal_error or _thinking_stopped)
                        and use_tools and not parsed_calls and not refused_broke
                        and not cancel_ev.is_set()
                        and rounds < max_tool_rounds - 1
                        and _chat_in_goal_mode(chat_id)
                        and _goal_window_progressed(turn_tool_outcomes[_goal_window_from:])):
                    _goal_renewals += 1
                    _goal_window_from = len(turn_tool_outcomes)
                    _goal_note = (
                        "[automatic note] /goal is still on and your plan has open steps: "
                        + "; ".join(_open_steps[:4])
                        + ". Continue with the next one now: make the tool call. If a step cannot "
                          "be done, record that with update_plan and move on. If everything is "
                          "really finished, mark the plan done and write the final summary.")
                    if _thinking_stopped:
                        conversation = _with_thinking_note(conversation, _start_len, _goal_note)
                        thinking_only_streak = 0
                        force_no_think = True
                    else:
                        conversation.append(assistant_msg)
                        conversation.append({"role": "user", "content": _goal_note})
                    note_intervention("goal_plan_continue")
                    emit({"type": "notice", "quiet": True,
                          "note": f"/goal: the plan still has {len(_open_steps)} open "
                                  f"step{'s' if len(_open_steps) != 1 else ''}, continuing"})
                    continue
                # Persist whole-turn token totals: keep the last round's timing fields for
                # tok/s display but override the counts with the turn totals.
                turn_stats = dict(last_stats)
                if turn_eval_total:
                    turn_stats["eval_count"] = turn_eval_total
                if turn_prompt_total:
                    turn_stats["prompt_eval_count"] = turn_prompt_total
                if turn_eval_ms_total:
                    turn_stats["eval_duration"] = int(turn_eval_ms_total * 1e6)
                if turn_prompt_ms_total:
                    turn_stats["prompt_eval_duration"] = int(turn_prompt_ms_total * 1e6)
                assistant_msg["_stats"] = turn_stats
                _updates = _steering.take(chat_id, close_if_empty=True)
                if _updates:
                    # The answer was complete when the update arrived: keep it, and
                    # let the model act on the update from there.
                    _apply_task_updates(_updates, _steer_kept_partial(
                        "", str(assistant_msg.get("content") or "")))
                    continue
                if terminal_error:
                    assistant_msg["content"] = terminal_error
                    assistant_msg["_failed"] = True
                    emit({"type": "error", "error": terminal_error})
                # Lifetime savings: add this turn's tokens to the durable counters
                # (every round's full prompt, as a cloud API would bill) and push
                # the new totals to the widget. Fall back to a char estimate when
                # the backend reported no usage, mirroring the frontend fallback.
                try:
                    _sv = _commit_turn_savings(
                        0 if turn_eval_total else len(assistant_msg.get("content") or "") // 4)
                    if _sv:
                        emit({"type": "savings", "tok_in": _sv["tok_in"],
                              "tok_out": _sv["tok_out"], "turns": _sv["turns"],
                              "since": _sv["since"]})
                except Exception:
                    pass
                # Breach summary for the red-team flow: which flags were captured
                # this turn and how many stages that clears.
                if captured_flags:
                    assistant_msg["_breach"] = {
                        "flags": captured_flags,
                        "count": len(captured_flags),
                    }
                # Passive OSINT is a bounded one-turn collection. Close it at
                # the answer boundary so later ordinary chat is not trapped in
                # the passive allowlist. Active recon/pentest missions keep
                # their existing explicit report-driven lifecycle.
                if _rt_on and _rt_passive_mission(_rt_chat):
                    _rt_chat["mission"]["status"] = "closed"
                    _rt_chat["mission"]["closed_at"] = int(time.time())
                    _rt_log_mission_ended(_rt_chat["mission"])
                    _rt_dirty = True
                    emit({
                        "type": "rt_mission",
                        "status": "closed",
                        "phase": "recon",
                        "target": str(_rt_chat["mission"].get("target") or ""),
                        "engagement": "passive_osint",
                    })
                elif _rt_on and _rt_close_frontend_secret_mission(_rt_chat):
                    # If the model somehow failed to call the one scanner, do
                    # not leave later chat turns trapped in an active audit.
                    _rt_dirty = True
                    emit({
                        "type": "rt_mission",
                        "status": "closed",
                        "phase": "recon",
                        "target": str(_rt_chat["mission"].get("target") or ""),
                        "engagement": "frontend_secrets",
                    })
                # Durable mission state advanced this turn — the caller persists
                # the merged mission onto chat["mission"] (one save).
                if _rt_dirty and _rt_chat.get("mission"):
                    assistant_msg["_mission_updates"] = dict(_rt_chat["mission"])
                if _activity_dirty:
                    assistant_msg["_activity_updates"] = list(_activity_tail[-_ACTIVITY_MAX:])
                    assistant_msg["_continuity_updates"] = dict(_continuity_state)
                if _verification_dirty:
                    assistant_msg["_verification_updates"] = list(_verification_debt)
                # Intermediate working memory for this turn: every tool result
                # and intermediate-assistant-with-tool-calls the loop appended.
                # The caller persists these so the next user turn replays the
                # full agentic context, not just the final bubble.
                assistant_msg["_appended_intermediate"] = _drop_invalid_call_pairs(conversation[_start_len:])
                assistant_msg["_build"] = _BUILD_TAG
                assistant_msg["_turn_id"] = _turn_id
                if _carry_diag:
                    assistant_msg["_carry_diag"] = list(_carry_diag)
                emit({"type": "final", "message": assistant_msg})
                try:
                    _record_model_observation(
                        model,
                        prompt_tokens=turn_prompt_total,
                        output_tokens=turn_eval_total,
                        rounds=max(1, rounds + 1),
                        tool_calls=turn_tool_calls,
                        tool_errors=turn_tool_errors,
                        peak_prompt_tokens=turn_peak_prompt,
                        elapsed_s=time.time() - turn_started_at,
                        native_fallback=not native_tools,
                        completed=True,
                        end_reason=_turn_end_reason(locals().get("terminal_error")),
                        chat_id=chat_id,
                        prompt_ms=turn_prompt_ms_total,
                        eval_ms=turn_eval_ms_total,
                        draft_n=turn_draft_n,
                        draft_accepted=turn_draft_accepted,
                        compactions=turn_compactions,
                        compaction_failures=turn_compaction_failures,
                        tool_outcomes=turn_tool_outcomes,
                        interventions=turn_interventions,
                    )
                except Exception:
                    # Passive profiling is advisory. A damaged/unwritable
                    # profile must never turn a completed chat into a failure.
                    pass
                _profile_recorded = True
                return assistant_msg

            conversation.append(assistant_msg)
            _live_turn_messages[chat_id] = list(conversation[_start_len:])
            # Required-args guard. Never execute a partial call. Native parsing
            # can preserve a path while losing only the large content field,
            # and text mode can also return an empty retry if the model spends
            # the round planning instead of emitting values.
            _arg_loss: list[tuple[str, list[str]]] = []
            _bad_paths = False
            if use_tools and parsed_calls:
                for _pc in parsed_calls:
                    _nm = _resolve_tool_name(_pc.get("name") or "")
                    if _invalid_tool_path(_nm, _pc.get("arguments")):
                        _bad_paths = True
                        _arg_loss.append((_pc.get("name") or _nm, ["valid path"]))
                        continue
                    _missing = _missing_required_tool_args(
                        _nm, _pc.get("arguments"))
                    if _missing:
                        _arg_loss.append((_pc.get("name") or _nm, _missing))
            if (_arg_loss and rounds < max_tool_rounds - 1
                    and arg_loss_retries < 2):
                arg_loss_retries += 1
                _log_tool_parse_failure(chat_id, rounds, native_tools, finish_reason,
                                        "".join(answer_buf), tool_calls_by_index)
                # None of this round's calls run, so none may stay in the history as
                # an example: a small model copies the calls it sees.
                conversation[-1] = {k: v for k, v in assistant_msg.items() if k != "tool_calls"}
                _summary = "; ".join(
                    f"`{name}` missing {', '.join(fields)}"
                    for name, fields in _arg_loss)
                _example_name, _example_missing = _arg_loss[0]
                _example_canon = _resolve_tool_name(_example_name)
                _example_spec = TOOLS.get(_example_canon) or {}
                _example_required = list(
                    ((_example_spec.get("parameters") or {}).get("required")) or [])
                if not _bad_paths:
                    _example_required.extend(
                        field for field in _example_missing if field not in _example_required)
                _example_call = next(
                    (_pc for _pc in parsed_calls
                     if _resolve_tool_name(_pc.get("name") or "") == _example_canon),
                    {},
                )
                _example_args = _example_call.get("arguments") or {}
                _example_lines = ["<tool_call>", f"<function={_example_name}>"]
                for _field in _example_required:
                    _known = _example_args.get(_field)
                    if (isinstance(_known, str) and _known.strip()
                            and len(_known) <= 500):
                        _placeholder = (_known.replace("&", "&amp;")
                                        .replace("<", "&lt;")
                                        .replace(">", "&gt;"))
                    else:
                        _placeholder = (
                            "<the complete file body>" if _field == "content"
                            else f"<the real {_field} value>")
                    _example_lines.extend([
                        f"<parameter={_field}>", _placeholder, "</parameter>",
                    ])
                _example_lines.extend(["</function>", "</tool_call>"])
                _body_tip = ""
                if any(_resolve_tool_name(name) in ("write_file", "remote_write_file")
                       for name, _ in _arg_loss):
                    _body_tip = "\nFor a long file, you can also " + _FILE_IN_PARTS + "."
                conversation.append({
                    "role": "user",
                    "content": (
                        "[automatic note] The previous tool call was not executed because required "
                        "fields were lost: " + _summary + ". Re-emit the SAME call now using the "
                        "text-parsed XML form below. Do not plan or explain. "
                        "Begin with <tool_call> and include the actual values.\n" +
                        "\n".join(_example_lines) + _body_tip
                    ),
                })
                if _bad_paths:
                    conversation[-1]["content"] = (
                        "[automatic recovery] The previous action was not executed because its file "
                        "path contained line breaks or a null character. An example in your answer "
                        "may have been mistaken for a tool call. Answer the user's request again. "
                        "Do not turn the rejected text into a file or repeat that action unless the "
                        "user's request actually calls for it. Put examples inside ordinary Markdown "
                        "code spans or code fences. Only use tool envelopes for intended actions."
                    )
                note_intervention("tool_call_repair")
                emit({"type": "notice", "quiet": True,
                      "note": ("A malformed file action was blocked. Regenerating the reply."
                               if _bad_paths else
                               "Tool arguments were incomplete. Retrying through the text parser.")})
                # Remember which tools llama.cpp's native parser dropped arguments for, so
                # later turns can start in text-tools mode (see _mark_native_tools_broken).
                # A multiline path is a model mistake, not a parser loss.
                if native_tools:
                    _mark_native_tools_broken(
                        {_resolve_tool_name(name) for name, fields in _arg_loss
                         if fields != ["valid path"]})
                    # Without the `tools` array the model no longer sees any schema, so the
                    # first switch of the turn lists the tools it can still call.
                    if not tool_card_sent:
                        tool_card_sent = True
                        conversation[-1]["content"] += (
                            "\n\nNative tool calls are off for the rest of this turn. "
                            "Tools you can call (* = required):\n"
                            + "\n".join(_text_tool_lines(chat_id)))
                # Stop sending the `tools` array for the rest of this turn: while it sees the
                # schema the server keeps swallowing the <parameter> blocks. On the text path
                # the XML call lands in content and extract_tool_calls decodes it.
                native_tools = False
                # The failed round spent its budget planning and emitted only a
                # tool name. The retry spends every token on the real call.
                tool_cap_no_think = True
                continue
            # Parallel tool execution: submit the whole batch to the executor (8 workers)
            # and collect in the model's original call order, so independent tools run
            # concurrently while conversation and UI ordering stay the same. Calls that
            # write the same path are grouped into serial batches.
            _batches: list[list[dict]] = []
            # Target switches run first and alone so a same-round file call sees
            # the new boundary. A report runs last and alone so a parallel
            # exploit cannot race the server-side close gate.
            _round_serial_tools = {"switch_execution_target", "rt_generate_report"}
            _ordered_calls = (
                [c for c in parsed_calls
                 if _resolve_tool_name(c.get("name") or "") == "switch_execution_target"]
                + [c for c in parsed_calls
                   if _resolve_tool_name(c.get("name") or "") not in _round_serial_tools]
                + [c for c in parsed_calls
                   if _resolve_tool_name(c.get("name") or "") == "rt_generate_report"]
            )
            for _call in _ordered_calls:
                if _resolve_tool_name(_call.get("name") or "") in _round_serial_tools:
                    _batches.append([_call])
                    continue
                _key = _call_serial_key(_call)
                for _b in _batches:
                    if any(_resolve_tool_name(x.get("name") or "") in _round_serial_tools for x in _b):
                        continue
                    if not _key or not any(_call_serial_key(x) == _key for x in _b):
                        _b.append(_call)
                        break
                else:
                    _batches.append([_call])
            _round_all_refused = True
            for batch in _batches:
                if cancel_ev.is_set():
                    # Stopped mid-round: start nothing new. Unanswered calls are
                    # dropped by _drop_invalid_call_pairs on the stop path.
                    break
                _cancel_seen_at = None
                _futures: dict[int, object] = {}
                _call_ids: dict[int, str] = {}
                for call in batch:
                    requested_name = call.get("name") or ""
                    name = _resolve_tool_name(requested_name)
                    args = call.get("arguments") or {}
                    call_id = str(call.get("id") or f"tool-{uuid.uuid4().hex[:12]}")
                    _call_ids[id(call)] = call_id
                    emit({
                        "type": "tool_start",
                        "name": name,
                        "requested_name": requested_name,
                        "call_id": call_id,
                        "arguments": args,
                    })
                    _ctx = contextvars.copy_context()
                    _futures[id(call)] = _tool_executor.submit(
                        _ctx.run, _invoke_tool_with_context, name,
                        args if isinstance(args, dict) else {}, call_id)
                for call in batch:
                    requested_name = call.get("name") or ""
                    name = _resolve_tool_name(requested_name)
                    call_id = _call_ids[id(call)]
                    args = call.get("arguments") or {}
                    future = _futures[id(call)]
                    while not future.done():
                        try:
                            future.result(timeout=1.0)
                        except Exception:
                            pass
                        if future.done():
                            break
                        if cancel_ev.is_set():
                            # Give a killed command a moment to report back, then
                            # stop waiting so Stop ends the turn even when the tool
                            # ignores the kill.
                            if _cancel_seen_at is None:
                                _cancel_seen_at = time.monotonic()
                            elif time.monotonic() - _cancel_seen_at >= 5.0:
                                break
                        try:
                            emit({"type": "heartbeat", "note": f"waiting for {name}…"})
                        except Exception:
                            pass
                    if future.done():
                        result = future.result()
                    else:
                        result = {"error": f"stopped by user before {name} finished; "
                                           "its result was discarded",
                                  "cancelled": True}
                    _tool_err = bool(isinstance(result, dict) and
                                     (result.get("error") or result.get("ok") is False or
                                      result.get("exit_code") not in (None, 0)))
                    # A received HTTP response is a successful call even when its
                    # status is 4xx/5xx: to http_request the 401/403/404 IS the
                    # result. Counted as failures they inflated its failure rate
                    # and could make a red-team /goal stretch look like no progress.
                    if (_tool_err and isinstance(result, dict) and not result.get("error")
                            and isinstance(result.get("status"), int)
                            and 100 <= result["status"] <= 599):
                        _tool_err = False
                    if _tool_err:
                        turn_tool_errors += 1
                    _ec = result.get("exit_code", 0) if isinstance(result, dict) else 0
                    _ec_i = 0
                    if isinstance(_ec, bool):
                        _ec_i = 1 if _ec else 0
                    elif isinstance(_ec, (int, float)):
                        _ec_i = int(_ec)
                    elif isinstance(_ec, str) and _ec.strip().lstrip("-").isdigit():
                        _ec_i = int(_ec)
                    try:
                        _canon = _resolve_tool_name(name)
                    except Exception:
                        _canon = str(name or "unknown")
                    turn_tool_outcomes.append({
                        "name": _canon,
                        "ok": not _tool_err,
                        "error_kind": _tool_error_kind(result) if _tool_err else "",
                        "exit_code": _ec_i,
                        "refused": bool(result.get("loop_breaker")) if isinstance(result, dict) else False,
                    })
                    if isinstance(result, dict) and result.get("loop_breaker"):
                        refused_names.add(_resolve_tool_name(name))
                    else:
                        _round_all_refused = False
                    # Shell results now carry their whole output (up to 400K) so it
                    # can be saved for read_file; the UI card needs far less.
                    _ui_result = result
                    if isinstance(result, dict) and any(
                            isinstance(result.get(_k), str) and len(result[_k]) > 65536
                            for _k in ("stdout", "stderr")):
                        _ui_result = {**result, **{
                            _k: _clip_head_tail(result[_k], 65536)
                            for _k in ("stdout", "stderr") if isinstance(result.get(_k), str)}}
                    emit({
                        "type": "tool_result",
                        "name": name,
                        "requested_name": requested_name,
                        "call_id": call_id,
                        "result": _ui_result,
                    })
                    if (_rt_on
                            and _rt_close_frontend_secret_mission(_rt_chat, name)):
                        _rt_dirty = True
                        emit({
                            "type": "rt_mission",
                            "status": "closed",
                            "phase": "recon",
                            "target": str(_rt_chat["mission"].get("target") or ""),
                            "engagement": "frontend_secrets",
                        })
                    _activity_tail.append(_tool_activity_record(name, args, result))
                    _activity_tail = _activity_tail[-_ACTIVITY_MAX:]
                    with _chat_live_activity_lock:
                        _chat_live_activity[chat_id] = list(_activity_tail)
                    _activity_dirty = True
                    _verification_debt, _debt_changed = _update_verification_debt(
                        _verification_debt, name, args, result)
                    check = _task_check_record(name, args, result)
                    if check:
                        _turn_checks.append(check)
                    if _debt_changed:
                        _verification_dirty = True
                        snapshot = get_chats()
                        if chat_id in snapshot.get("chats", {}):
                            snapshot["chats"][chat_id]["verification_debt"] = copy.deepcopy(_verification_debt)
                            save_json(CHATS_FILE, snapshot)
                    _continuity_state = _build_continuity_state(
                        {"task_anchor": _task_state, "pins": _pins_tail,
                         "compaction_memory": _compaction_memory,
                         "mission": _rt_chat.get("mission") if _rt_chat else {},
                         "findings": _findings_tail,
                         "verification_debt": _verification_debt},
                        activity=_activity_tail, plan=_plan_tail)
                    # Session-state tools write chats.json inside the worker. Refresh the
                    # in-memory tail right away so a pin or plan update is visible on the very
                    # next inference round.
                    if _resolve_tool_name(name) in ("pin_note", "unpin_note", "update_plan",
                                                    "record_finding", "list_findings", "security_coverage"):
                        try:
                            _fresh = (get_chats().get("chats", {}) or {}).get(chat_id) or {}
                            _rt_chat["security_coverage"] = copy.deepcopy(_fresh.get("security_coverage") or [])
                            _pins_tail = [str(p) for p in (_fresh.get("pins") or [])]
                            _plan_tail = [dict(p) for p in (_fresh.get("plan") or [])
                                          if isinstance(p, dict)]
                            _findings_tail = [dict(x) for x in (_fresh.get("findings") or [])
                                              if isinstance(x, dict)][-20:]
                            _continuity_state = _build_continuity_state(
                                {"task_anchor": _task_state, "pins": _pins_tail,
                                 "compaction_memory": _compaction_memory,
                                 "mission": _rt_chat.get("mission") if _rt_chat else {},
                                 "findings": _findings_tail,
                                 "verification_debt": _verification_debt},
                                activity=_activity_tail, plan=_plan_tail)
                        except Exception:
                            pass
                    if _rt_on and _resolve_tool_name(name) == "rt_generate_report":
                        try:
                            _fresh = (get_chats().get("chats", {}) or {}).get(chat_id) or {}
                            if isinstance(_fresh.get("mission"), dict):
                                _rt_chat["mission"] = dict(_fresh["mission"])
                                _rt_dirty = True
                                emit({
                                    "type": "rt_mission",
                                    "status": str(_fresh["mission"].get("status") or "active"),
                                    "phase": str(_fresh["mission"].get("phase") or "recon"),
                                    "target": str(_fresh["mission"].get("target") or ""),
                                    "engagement": str(_fresh["mission"].get("engagement") or "recon"),
                                    "report": str(result.get("report") or "") if isinstance(result, dict) else "",
                                })
                        except Exception:
                            pass
                    # Breach detection: a FLAG{...} in a tool response is confirmed access
                    # (cyber-range / CTF scoring); report each distinct flag once. Only
                    # target-touching tools count, so reading notes that mention FLAG{...} doesn't
                    # light up the attack-chain rail.
                    if _is_breach_capable_tool(name):
                        try:
                            for _flag in _FLAG_RE.findall(json.dumps(result, ensure_ascii=False, default=str)):
                                if _flag not in captured_flags:
                                    captured_flags.append(_flag)
                                    emit({"type": "breach", "stage": len(captured_flags),
                                          "flag": _flag, "via": name,
                                          "total": len(captured_flags)})
                                    print(f"[breach] stage {len(captured_flags)} — {_flag} (via {name})", flush=True)
                                    if _rt_on and _rt_mission_note(_rt_chat, fact=f"access via {name}: {_flag}"):
                                        _rt_dirty = True
                        except Exception:
                            pass
                    # Mission target: the first host a red-team tool points at.
                    if _rt_on and name in _RED_TEAM_TOOL_NAMES:
                        _t = _rt_target_from_args(name, args)
                        if _t and _rt_mission_note(_rt_chat, target=_t):
                            _rt_dirty = True
                    if (_rt_on and isinstance(result, dict) and not result.get("error")
                            and _rt_mission_mark_tool(_rt_chat, name)):
                        _rt_dirty = True
                    # Phase is activity history for the rail. Tool availability
                    # comes from the user-selected engagement mode, not evidence
                    # status, so verification cannot deadlock its own capture.
                    _rt_used_active_tool = _resolve_tool_name(name) in _RT_EXPLOIT_TOOL_NAMES
                    if _rt_on and (_rt_used_active_tool or _rt_result_confirms_finding(name, result)):
                        if _rt_mission_set_phase(_rt_chat, "exploit"):
                            _rt_dirty = True
                            emit({"type": "rt_phase", "phase": "exploit",
                                  "via": _resolve_tool_name(name),
                                  "reason": "active_test" if _rt_used_active_tool else "validated_finding"})
                    # Analysis tools produce large structured output and get a looser cap.
                    # compress_tool_result elides known bulk fields line-aware before serializing,
                    # so the JSON stays valid and the trailing diagnostics survive.
                    _trunc = _tool_result_cap(name)
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": compress_tool_result(name, result, _trunc),
                    })
                    try:
                        _turn_journal_checkpoint(
                            chat_id, _turn_id, list(conversation[_start_len:]),
                            activity=_activity_tail,
                            mission=(_rt_chat.get("mission") if _rt_chat else None),
                            verification_debt=_verification_debt,
                        )
                    except Exception as _journal_error:
                        print(f"[turn-journal] checkpoint failed for {chat_id}: {_journal_error}",
                              file=sys.stderr)
            if _batches and _round_all_refused and not _steering.pending(chat_id):
                # Every call this round was a loop-breaker refusal. Once can be a transient
                # misunderstanding; twice in a row means the model is re-emitting refused
                # calls, so end the turn with a real answer instead of burning the remaining
                # rounds.
                refused_streak += 1
            else:
                refused_streak = 0
            if refused_streak >= 2 and not refused_broke:
                refused_broke = True
                use_tools = False
                rounds = max_tool_rounds
                _rb_names = ", ".join(sorted(refused_names))[:200] or "a tool"
                conversation.append({
                    "role": "system" if _rt_trailing_system_ok() else "user",
                    "content": (
                        "[loop guard] The harness refused your repeated identical "
                        f"tool call(s) ({_rb_names}) several times this turn and the "
                        "result never changed. The turn is being ended. Do NOT call "
                        "any tool again. Write your FINAL answer now: summarize what "
                        "you actually accomplished this turn and what blocked you, "
                        "plain text only."
                    ),
                })
                note_intervention("loop_guard")
                emit({"type": "notice", "quiet": True,
                      "note": f"loop guard: repeated identical tool refusals ({_rb_names}) — "
                              "ending the turn with a final answer"})
            if tool_cap_no_think and _batches:
                # A real tool call executed this round, so release the no-thinking mode that
                # protected the write body; the model may reason about the result again.
                tool_cap_no_think = False
            rounds += 1
    except Exception as e:
        print(f"run_chat_turn interrupted: {e}")
        partial = {"role": "assistant", "content": "".join(content_buf) if 'content_buf' in locals() else ""}
        if cancel_ev.is_set():
            partial["_stopped"] = True
        else:
            partial["_failed"] = True
            # Say so on the wire; otherwise the client sees a silent chat_start ...
            # chat_end and never learns the turn failed.
            try:
                emit({"type": "error", "error": (
                    f"The turn was interrupted before it finished: {e}. "
                    "Whatever had already been produced is shown above; nothing "
                    "further was run.")})
            except Exception:
                pass
        partial["_appended_intermediate"] = _drop_invalid_call_pairs(conversation[_start_len:]) if 'conversation' in locals() else []
        partial["_build"] = (_BUILD_TAG if '_BUILD_TAG' in locals() else "")
        partial["_turn_id"] = _turn_id if '_turn_id' in locals() else ""
        return partial
    finally:
        try:
            changes = _undo_commit_turn()
            if changes:
                changes["verification"] = _task_verification_summary(changes["files"], locals().get("_turn_checks", []))
                snapshot = get_chats()
                if chat_id in snapshot.get("chats", {}):
                    chat_row = snapshot["chats"][chat_id]
                    chat_row["undo_turns"] = (chat_row.get("undo_turns", []) + [changes])[-20:]
                    save_json(CHATS_FILE, snapshot)
                emit({"type": "turn_changes", **changes})
        except Exception as exc:
            print(f"Undo journal finalization failed: {exc}")
        if _rt_chat_ctx_token is not None:
            _current_rt_chat.reset(_rt_chat_ctx_token)
        # A stopped / failed / stalled turn still ran on the GPU: count it.
        try:
            _sv = _commit_turn_savings()
            if _sv:
                emit({"type": "savings", "tok_in": _sv["tok_in"],
                      "tok_out": _sv["tok_out"], "turns": _sv["turns"],
                      "since": _sv["since"]})
        except Exception:
            pass
        if not _profile_recorded:
            try:
                _record_model_observation(
                    model,
                    prompt_tokens=turn_prompt_total,
                    output_tokens=turn_eval_total,
                    rounds=rounds,
                    tool_calls=turn_tool_calls,
                    tool_errors=turn_tool_errors,
                    peak_prompt_tokens=turn_peak_prompt,
                    elapsed_s=time.time() - turn_started_at,
                    native_fallback=not native_tools,
                    completed=False,
                    end_reason=("stopped" if (cancel_ev is not None and cancel_ev.is_set())
                                else "failed"),
                    chat_id=chat_id,
                    prompt_ms=turn_prompt_ms_total,
                    eval_ms=turn_eval_ms_total,
                    draft_n=turn_draft_n,
                    draft_accepted=turn_draft_accepted,
                    compactions=turn_compactions,
                    compaction_failures=turn_compaction_failures,
                    tool_outcomes=turn_tool_outcomes,
                    interventions=turn_interventions,
                )
            except Exception:
                pass
        deferred_updates = _steering.finish(chat_id)
        if deferred_updates:
            try:
                emit({"type": "steering_deferred", "chat_id": chat_id,
                      "ids": [update["id"] for update in deferred_updates]})
            except Exception:
                pass
        _chat_emitters.pop(chat_id, None)
        _live_turn_messages.pop(chat_id, None)
        with _chat_live_activity_lock:
            _chat_live_activity.pop(chat_id, None)
        _unregister_cancel(chat_id)
