# SPDX-License-Identifier: AGPL-3.0-only
"""The rolling summary: asking the model to fold old turns into the session summary,
checking its sections, and the compaction debug log.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
import time
from accuretta.agent.compaction import _SUMMARY_REQUIRED_HEADINGS
from accuretta.agent.compaction import _assemble_summary
from accuretta.agent.compaction import _split_summary_sections
from accuretta.agent.compaction import _summary_bullets
from accuretta.agent.compaction import _summary_norm
from accuretta.agent.compaction import _summary_output_budget
from accuretta.agent.compaction import _summary_request_chars
from accuretta.agent.compaction import _summary_request_prefix
from accuretta.agent.compaction import _summary_tool_facts
from accuretta.agent.visible_text import _render_msgs_for_summary
from accuretta.agent.visible_text import _take_summary_chunk
from accuretta.core.events import broadcast_event
from accuretta.core.paths import DATA
from accuretta.runtime.llama_post import llama_post


_SUMMARY_MIN_OUTPUT_CHARS = 40  # summarizer output below this is degenerate (e.g. 11-char) — treat as failure


# The fold note has two owners. The model rewrites the short sections on every
# fold (what is being done, what blocks it, settled decisions, the next step,
# and what the new messages achieved). The bridge keeps the lists that only
# grow: the progress history, the files touched and the checks run. It appends
# each fold's new entries and keeps the latest few, so the model never re-copies
# a growing list. When the model had to copy one, the note grew into its output
# cap and the model handed the prior note back unchanged, so the fold committed
# nothing (11 of 20 folds in one 3-hour run). Now the note stays bounded on any
# run length or context size, and when the model fails or copies, the bridge's
# half still commits and the fold moves forward.
_SUMMARY_INSTR = (
    "You are compressing an earlier slice of a coding-assistant conversation into a short state block. "
    "Update the PRIOR STATE (if any) with what the NEW MESSAGES show. "
    "The request may also contain harness-extracted VERBATIM USER INTENT and EXACT SOURCE ANCHORS. "
    "Treat those as authoritative source evidence and copy exact identifiers without altering them. "
    "The harness itself records the files touched, the commands and checks run, and the earlier "
    "progress, so do not list those again. "
    "You MUST use this EXACT markdown format and preserve exact file paths, variable names, values and error codes:\n\n"
    "## ACTIVE TASK:\n(One or two lines: what is being worked on right now)\n"
    "## NEW PROGRESS:\n(Only what the NEW MESSAGES established: what worked, verified findings, answers "
    "or results with their exact values. Nothing already under ALREADY RECORDED PROGRESS)\n"
    "## ROADBLOCKS:\n(Unresolved errors and failed approaches, exact. Drop ones that are now resolved)\n"
    "## DECISIONS AND CONSTRAINTS:\n(User requirements and settled technical choices that must not be "
    "undone. Keep prior ones that still hold)\n"
    "## NEXT ACTION:\n(The exact unfinished step the assistant should perform next)\n\n"
    "Use at most six short bullets per section and write '- None recorded.' for an empty one. "
    "Record task state only — never record the assistant's internal deliberation "
    "about tool-call formats, fences, or harness protocols. "
    "Output ONLY the new markdown state block and nothing else."
)


# What the model writes on each fold (see _SUMMARY_INSTR).
_SUMMARY_MODEL_HEADINGS = (
    "## ACTIVE TASK:", "## NEW PROGRESS:", "## ROADBLOCKS:",
    "## DECISIONS AND CONSTRAINTS:", "## NEXT ACTION:",
)


# A fold whose slice overflows one summarizer request is merged in sequential
# chunks rather than head-elided (the elided head ended up in neither summary).
# Capped so a huge fold cannot chain calls indefinitely; past the cap the last
# chunk falls back to head elision.
_SUMMARY_MAX_CHUNKS = 4


_SUMMARY_ALL_HEADINGS = tuple(dict.fromkeys(_SUMMARY_MODEL_HEADINGS + _SUMMARY_REQUIRED_HEADINGS))


def _validate_model_summary(text: str, prior: dict) -> tuple[bool, str, dict]:
    """Check the model's half of a fold. Returns (ok, why, sections)."""
    text = str(text or "").strip()
    if len(text) < _SUMMARY_MIN_OUTPUT_CHARS:
        return False, f"degenerate output ({len(text)} chars)", {}
    sections = _split_summary_sections(text, _SUMMARY_ALL_HEADINGS)
    missing = [h for h in ("## ACTIVE TASK:", "## NEXT ACTION:") if not sections.get(h)]
    if missing:
        return False, "missing or empty section(s): " + ", ".join(missing), sections
    if prior:
        known = {_summary_norm(b) for b in _summary_bullets(prior.get("## COMPLETED:", ""))}
        progress = (sections.get("## NEW PROGRESS:") or sections.get("## COMPLETED:") or "")
        fresh = [b for b in _summary_bullets(progress) if _summary_norm(b) not in known]
        same = all(_summary_norm(sections.get(h, prior.get(h, ""))) == _summary_norm(prior.get(h, ""))
                   for h in ("## ACTIVE TASK:", "## ROADBLOCKS:",
                             "## DECISIONS AND CONSTRAINTS:", "## NEXT ACTION:"))
        if same and not fresh:
            return False, "returned the prior state unchanged", sections
    return True, "ok", sections


def _update_rolling_summary(old_summary: str, slice_msgs: list[dict], chat_id: str = "",
                            notify: bool = True, ctx_limit: int | None = None,
                            compaction_memory: dict | None = None,
                            outcome: dict | None = None) -> str | None:
    """Merge old summary + new slice into an updated summary: one model call,
    or a few sequential ones when the slice overflows a single request.

    The bridge's half (progress history, files, checks) is always updated, so
    a model call that fails keeps the prior model sections and the fold still
    moves forward. `outcome["model_fallbacks"]` counts those chunks."""
    if not _render_msgs_for_summary(slice_msgs).strip():
        return old_summary
    request_chars = _summary_request_chars(ctx_limit)
    summary = old_summary
    remaining = list(slice_msgs)
    calls = 0
    fallbacks = 0
    while remaining:
        prefix = _summary_request_prefix(summary, ctx_limit, compaction_memory)
        # A lean recent slice is faster to prefill and leaves the deterministic
        # evidence above responsible for exact old identifiers and user wording.
        # The fit cap remains conservative for dense code/JSON and small contexts.
        new_budget = max(2000, request_chars - len(prefix) - 32)
        if calls < _SUMMARY_MAX_CHUNKS - 1:
            take = _take_summary_chunk(remaining, new_budget)
        else:
            take = len(remaining)
        chunk, remaining = remaining[:take], remaining[take:]
        if remaining or calls:
            _log_compact(chat_id, f"summarize chunk {calls + 1}: {len(chunk)} msgs, "
                                  f"{len(remaining)} left, budget={new_budget}")
        rendered = _render_msgs_for_summary(chunk)
        if not rendered.strip():
            continue
        if len(rendered) > new_budget:
            rendered = ("…(older new messages elided to fit the compaction request)…\n"
                        + rendered[-new_budget:])
        prior = _split_summary_sections(summary) if summary else {}
        model = _summarize_slice(prefix, rendered, chat_id, notify, ctx_limit, prior)
        if model is None:
            fallbacks += 1
            _log_compact(chat_id, "summarize fallback: kept the prior task sections and "
                                  "updated the bridge-kept lists")
        files, checks = _summary_tool_facts(chunk)
        summary = _assemble_summary(prior, model, files, checks)
        calls += 1
    if outcome is not None:
        outcome["model_fallbacks"] = fallbacks
    return summary


def _summarize_slice(prefix: str, rendered: str, chat_id: str, notify: bool,
                     ctx_limit: int | None, prior: dict | None = None) -> dict | None:
    """One non-streaming summarizer call (plus one bounded repair attempt) for
    the model's sections. Returns them as {heading: body}, or None on failure."""
    prior = prior or {}
    output_tokens = _summary_output_budget(ctx_limit)
    user = prefix + f"NEW MESSAGES:\n{rendered}"
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": _SUMMARY_INSTR},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "temperature": 0.1,
        "max_tokens": output_tokens,
        # Summary tokens must be spent on the state block, not an invisible
        # reasoning preamble. llama.cpp ignores unknown template kwargs, so
        # this remains safe for non-reasoning models.
        "chat_template_kwargs": {"enable_thinking": False, "thinking_budget": 0},
    }
    try:
        # This synchronous call can take 2-60s on a local model; tell the UI so it
        # shows a live compaction row meanwhile.
        if notify:
            broadcast_event({"type": "summary_folding", "chat_id": chat_id})
    except Exception:
        pass
    try:
        def _call_summary(attempt_payload: dict, attempt: int) -> tuple[dict, bool, str, str, int]:
            resp = llama_post(
                "/v1/chat/completions", attempt_payload,
                timeout=_SUMMARY_CALL_TIMEOUT_S,
            )
            choice = (resp.get("choices") or [{}])[0]
            txt = ((choice.get("message") or {}).get("content", "") or "").strip()
            valid, why, sections = _validate_model_summary(txt, prior)
            finish = str(choice.get("finish_reason") or "unknown")
            if not valid:
                _log_compact(
                    chat_id,
                    f"summarize attempt {attempt} rejected: {why}; "
                    f"finish={finish} chars={len(txt)}",
                )
            return sections, valid, why, finish, len(txt)

        sections, valid, why, _finish, _n = _call_summary(payload, 1)
        if valid:
            return sections

        # One bounded repair attempt for a response that arrived but failed the
        # check. Reuse the original source messages, not the rejected draft.
        # Not at temperature 0: greedy decoding is what copied the prior state.
        repair_instr = (
            _SUMMARY_INSTR
            + "\n\nREPAIR ATTEMPT: The previous state block was rejected because "
            + why
            + ". Produce the complete block again, every heading exactly once."
        )
        if "unchanged" in why:
            repair_instr += (" Rewrite ACTIVE TASK and NEXT ACTION from the NEW MESSAGES, and list "
                             "under NEW PROGRESS what they established.")
        repair_payload = {
            **payload,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": repair_instr},
                {"role": "user", "content": user},
            ],
        }
        sections, repaired_valid, repaired_why, repaired_finish, repaired_n = _call_summary(
            repair_payload, 2)
        if repaired_valid:
            _log_compact(
                chat_id,
                f"summarize repaired on attempt 2; finish={repaired_finish} "
                f"chars={repaired_n}",
            )
            return sections
        raise ValueError(
            "summarizer output rejected after repair: "
            f"{repaired_why} (first attempt: {why})"
        )
    except Exception as _e:
        try:
            _log_compact(chat_id, f"summarize failed: {type(_e).__name__}: {_e}")
        except Exception:
            pass
        return None


# Hard cap on one synchronous summarizer call. Local models and hardware vary
# widely, and preserving a long autonomous run matters more than making the
# fold feel instant. Five minutes gives a slow model room to finish while still
# bounding a genuinely wedged server; failure backoff prevents retry storms.
_SUMMARY_CALL_TIMEOUT_S = 300


# ---- compaction debug log ---------------------------------------------------
# Append-only runtime trace (data/compaction_debug.log) for the trim/fold
# call-site arithmetic that fold_events doesn't capture: the trimmer's
# ctx_limit / reserve / token_scale / dropped at the moment of a trim, and the
# pre-turn / proactive fold gates. Never raises.
_COMPACT_DEBUG_FILE = DATA / "compaction_debug.log"


_compact_debug_lock = threading.Lock()


def _log_compact(chat_id: str, msg: str) -> None:
    try:
        line = f"{time.strftime('%H:%M:%S')} [{chat_id}] {msg}\n"
        with _compact_debug_lock:
            with open(_COMPACT_DEBUG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass
