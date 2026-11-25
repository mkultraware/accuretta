# SPDX-License-Identifier: AGPL-3.0-only
"""Running one tool call: name and argument checks, execution target, red-team scope,
repeat limits, the call itself, and what is recorded afterwards.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import subagents
import time
import traceback
from accuretta.agent.action_audit import _DESKTOP_TOOL_NAMES
from accuretta.agent.action_audit import _WRITE_CAPABLE_TOOLS
from accuretta.agent.action_audit import _record_action_audit
from accuretta.agent.execution_target import _REMOTE_TOOL_NAMES
from accuretta.agent.execution_target import _selected_execution_target
from accuretta.agent.execution_target import _tool_targets_inference_host
from accuretta.agent.invalid_calls import _clear_invalid_call
from accuretta.agent.invalid_calls import _invalid_call_result
from accuretta.agent.invalid_calls import _invalid_tool_path
from accuretta.agent.invalid_calls import _missing_required_tool_args
from accuretta.agent.research_runs import _research_context
from accuretta.agent.rt_tool_guards import _RT_SCOPE_AWARE_COMMAND_TOOLS
from accuretta.agent.rt_tool_guards import _RT_SCOPE_AWARE_GENERIC_TOOLS
from accuretta.agent.rt_tool_guards import _rt_discovered_secret_submission_block
from accuretta.agent.rt_tool_guards import _rt_log_note
from accuretta.agent.rt_tool_guards import _rt_unify_target_args
from accuretta.agent.tool_call_parse import _normalize_tool_argument_shapes
from accuretta.agent.tool_history import TOOL_REPEAT_HARD_LIMIT
from accuretta.agent.tool_history import TOOL_REPEAT_LIMIT
from accuretta.agent.tool_history import TOOL_SOFT_REPEAT_LIMIT
from accuretta.agent.tool_history import URL_CROSS_TOOL_LIMIT
from accuretta.agent.tool_history import _FETCH_LIKE_TOOLS
from accuretta.agent.tool_history import _IDEMPOTENT_READ_TOOLS
from accuretta.agent.tool_history import _current_turn_progress
from accuretta.agent.tool_history import _fetch_url_key
from accuretta.agent.tool_history import _note_soft_repeat
from accuretta.agent.tool_history import _record_tool_result
from accuretta.agent.tool_history import _record_url_fetch
from accuretta.agent.tool_history import _reserve_tool_family_call
from accuretta.agent.tool_history import _tool_call_sig
from accuretta.agent.tool_history import _tool_repeat_state
from accuretta.agent.tool_history import _url_fetch_count
from accuretta.agent.tool_sets import _RED_TEAM_TOOL_NAMES
from accuretta.agent.tool_sets import _RT_PASSIVE_ALLOWED_TOOL_NAMES
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import pyautogui
from accuretta.core.settings import _offline_active
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.tool_registry import TOOLS
from accuretta.core.tool_stream import _mask_github_tokens
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from accuretta.redteam.command_scope import _rt_command_scope_block
from accuretta.redteam.findings import _evidence_context
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission import _rt_passive_mission
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.network_scope import _RT_AUTOMATED_TOOL_NAMES
from accuretta.redteam.network_scope import _rt_incl_exploit_for
from accuretta.redteam.network_scope import _rt_scope_block
from accuretta.redteam.playbook import _rt_next_step
from agent_reliability import feedback
from agent_reliability import verification_advice
from deep_research import RESEARCH_TOOLS
from typing import Any


from accuretta.tools.desktop_state import (  # noqa: F401  re-export: moved 2026-11-24
    _desktop_post_state,
)


_REMOTE_TOOL_EQUIVALENTS = {
    "read_file": "remote_read_file",
    "write_file": "remote_write_file",
    "list_directory": "remote_list_directory",
    "run_powershell": "remote_shell",
}


DIRECTORY_WALK_LIMIT = 8     # directory browsing must change strategy before it becomes a maze.


def invoke_tool(name: str, args: dict) -> dict:
    canon = _resolve_tool_name(name)
    args = _normalize_tool_argument_shapes(canon, args)
    if canon in _RED_TEAM_TOOL_NAMES:
        args = _rt_unify_target_args(canon, args)
    if _research_context()[1] and canon not in RESEARCH_TOOLS and canon != subagents.SPAWN_TOOL:
        return {"error": "Deep Research supports reading and evidence collection only.", "not_executed": True}
    if _offline_active and (canon.startswith(("web_", "recon_", "rt_", "browser_", "remote_", "mcp_", "sandbox_")) or canon in {"search_web", "fetch_url"}):
        return {"error": "This network integration is disabled in offline mode.", "not_executed": True}
    t = TOOLS.get(canon)
    if not t:
        # Surface the available names so a repair-retry round can fix a typo.
        return {"error": f"unknown tool: {name}", "available": sorted(TOOLS.keys())}
    def _finish(result: dict) -> dict:
        if canon not in _RED_TEAM_TOOL_NAMES:
            # Never into the model's context, the chat or the audit log
            # (red-team evidence keeps its exact bytes).
            result = _mask_github_tokens(result)
        result = feedback(canon, result)
        if (canon in _DESKTOP_TOOL_NAMES
                and canon not in ("screenshot", "describe_screen", "list_windows", "desktop_snapshot",
                                  "view_image")
                and isinstance(result, dict) and not result.get("error")
                and result.get("ok") is not False):
            result.setdefault("post_state", _desktop_post_state())
        _record_action_audit(canon, args or {}, result)
        if isinstance(result, dict) and canon in (_RED_TEAM_TOOL_NAMES | _RT_SCOPE_AWARE_GENERIC_TOOLS) and _rt_authorized_mission(_rt_context_chat()):
            try:
                store, cid, mission_id = _evidence_context()
                result["evidence_receipt"] = store.record(cid, mission_id, _current_tool_call_id.get(), canon, args or {}, result.copy())
                _rt_log_note(_rt_mission_get(_rt_context_chat() or {}), result,
                             store.folder(cid, mission_id))
            except (ValueError, OSError, KeyError) as exc:
                result["evidence_warning"] = "Evidence was not recorded: " + str(exc)
        if isinstance(result, dict):
            _hint_chat = _rt_context_chat()
            _hint_mission = _rt_authorized_mission(_hint_chat)
            if _hint_mission:
                _hint = _rt_next_step(canon, result, _rt_incl_exploit_for(_hint_chat),
                                      str(_hint_mission.get("engagement") or ""))
                # Never point at a tool this engagement has switched off.
                if _hint and not (_hint_mission.get("automated_allowed") is False
                                  and any(t in _hint for t in _RT_AUTOMATED_TOOL_NAMES)):
                    result.setdefault("next_step", _hint)
        if isinstance(result, dict) and canon in _WRITE_CAPABLE_TOOLS and not result.get("error"):
            result["verification_next_step"] = verification_advice(str(result.get("path") or args.get("path") or ""))
        return result
    selected_target = _selected_execution_target()
    if selected_target == "host" and canon in _REMOTE_TOOL_NAMES:
        return _finish({
            "error": (
                "refused: the selected execution target is the inference PC, but "
                f"{canon} acts on a paired Mac. Use switch_execution_target first."
            ),
            "wrong_execution_target": True,
            "selected_target": selected_target,
            "tool": canon,
            "suggested_tool": "switch_execution_target",
            "not_executed": True,
            "loop_breaker": True,
        })
    if selected_target != "host" and _tool_targets_inference_host(canon):
        suggested = _REMOTE_TOOL_EQUIVALENTS.get(canon)
        remote_alternative = suggested or "an available remote_* tool"
        return _finish({
            "error": (
                "refused: the selected execution target is the paired Mac, but "
                f"{canon} acts on the inference PC. If the user explicitly named the PC, "
                f"call switch_execution_target with target='host'. If the Mac remains intended, "
                f"use {remote_alternative} instead."
            ),
            "wrong_execution_target": True,
            "selected_target": selected_target,
            "tool": canon,
            "suggested_tool": suggested or "",
            "not_executed": True,
            "loop_breaker": True,
        })
    if _invalid_tool_path(canon, args):
        return _finish(_invalid_call_result(canon, {
            "error": "Action was not executed: the file path contains line breaks or a null character. "
                     "Check whether an example was mistaken for an action; follow the user's request.",
            "invalid_path": True,
            "not_executed": True,
        }))
    missing = _missing_required_tool_args(canon, args)
    if missing:
        return _finish(_invalid_call_result(canon, {
            "error": (
                f"Tool call was not executed because required argument(s) are missing: "
                f"{', '.join(missing)}. Re-emit the call with those values."
            ),
            "tool": canon,
            "missing_arguments": missing,
            "not_executed": True,
        }))
    _mission_chat = _rt_context_chat()
    if (_rt_passive_mission(_mission_chat)
            and canon not in _RT_PASSIVE_ALLOWED_TOOL_NAMES):
        return _finish({
            "error": ("refused: passive OSINT permits only public-source queries and local "
                      "reporting. This tool could contact the target or run unscoped code."),
            "scope_blocked": True,
            "tool": canon,
        })
    _secret_block = _rt_discovered_secret_submission_block(
        canon, args or {}, _mission_chat)
    if _secret_block:
        return _finish({
            "error": _secret_block,
            "scope_blocked": True,
            "credential_submission_blocked": True,
            "tool": canon,
        })
    # HARD scope guard for red-team tools: refuse accuretta's own bridge/llama
    # ports and any user-declared out-of-scope host/port BEFORE running. The
    # model's judgement is advisory; this is not.
    if canon in _RED_TEAM_TOOL_NAMES:
        _blk = _rt_scope_block(canon, args or {})
        if _blk:
            return _finish({"error": _blk, "scope_blocked": True, "tool": canon})
    elif canon in _RT_SCOPE_AWARE_GENERIC_TOOLS:
        # Generic fetch/header tools stay ungated outside red-team work, but during
        # an active engagement they obey the same host allowlist; otherwise picking a
        # non-red-team HTTP primitive would bypass the questionnaire.
        _chat = _mission_chat
        if _rt_authorized_mission(_chat):
            _blk = _rt_scope_block(canon, args or {})
            if _blk:
                return _finish({"error": _blk, "scope_blocked": True, "tool": canon})
            _url = str((args or {}).get("url") or "")
            _blk = _rt_gov_admit(_rt_url_host(_url), "GET", url=_url) if _url else None
            if _blk:
                return _finish({"error": _blk, "rate_limited": True, "tool": canon})
    elif canon in _RT_SCOPE_AWARE_COMMAND_TOOLS:
        _chat = _mission_chat
        if _rt_authorized_mission(_chat):
            _cmd = str((args or {}).get(_RT_SCOPE_AWARE_COMMAND_TOOLS[canon]) or "")
            _blk = _rt_command_scope_block(_cmd, _chat)
            if _blk:
                return _finish({"error": _blk, "scope_blocked": True, "tool": canon})
    elif canon.startswith("mcp_") or canon in _DESKTOP_TOOL_NAMES:
        _chat = _mission_chat
        if _rt_authorized_mission(_chat):
            return _finish({
                "error": ("refused: untyped MCP/desktop automation is unavailable during an active "
                          "red-team mission because the harness cannot prove its network scope. Use "
                          "the typed scope-aware red-team tools instead."),
                "scope_blocked": True,
                "tool": canon,
            })
    # Cross-tool URL dedup: catches the model switching between web_fetch and
    # http_request on the same URL to dodge the per-tool streaks.
    if canon in _FETCH_LIKE_TOOLS:
        _url_hits = _url_fetch_count(args or {})
        if _url_hits >= URL_CROSS_TOOL_LIMIT:
            _url_key = _fetch_url_key(args or {})
            _, _, _, _url_cached = _tool_repeat_state(_tool_call_sig(canon, args or {}))
            return _finish({
                "error": (
                    f"refused: the URL {_url_key} has returned the SAME content "
                    f"{_url_hits} times this turn via web_fetch/http_request. "
                    f"Switching between fetch tools on the same URL does not "
                    f"help \u2014 the page content is the same. Try a DIFFERENT "
                    f"approach: use a different tool (e.g. recon_content_discovery, "
                    f"sql_injection, fuzz), change the URL path or the request "
                    f"(payload/cookie, which resets this), or report what you found."
                ),
                "cached_result": _url_cached,
                "repeat_count": _url_hits,
                "tool": canon,
                "loop_breaker": True,
                "cross_tool_url_dedup": True,
            })
    # Loop-breaker (result-aware): refuse only when the same call has returned the
    # same result TOOL_REPEAT_LIMIT times running. A changed result resets the
    # streak (see _record_tool_result). `total` is the absolute backstop for
    # tools whose output wobbles every call. Session tools poll live state and are
    # exempt.
    sig = _tool_call_sig(canon, args or {})
    streak, total, progress_at, cached = _tool_repeat_state(sig)
    # If anything else moved the task forward since this signature last ran, it
    # is not spinning, so forgive the streak. Only the absolute backstop survives.
    progressed_since = _current_turn_progress() > progress_at
    stuck_streak = streak >= TOOL_REPEAT_LIMIT and not progressed_since
    hard_backstop = total >= TOOL_REPEAT_HARD_LIMIT
    if not canon.startswith("session_") and (stuck_streak or hard_backstop):
        n = max(streak, total)
        # Idempotent reads: hand back the cached result as a warning (unless the hard
        # backstop tripped) so the turn keeps its data instead of ending on an error.
        # Only TOOL_SOFT_REPEAT_LIMIT times: a served-from-cache call never counts
        # as run, so without this a model that ignores the warning got it forever
        # (one reverse-engineering run sent the same run_python 60 times and spent
        # its whole round budget). After that it's refused like any other repeat,
        # which also lets the turn-level loop guard end the turn with an answer.
        if (canon in _IDEMPOTENT_READ_TOOLS and not hard_backstop
                and cached is not None and _note_soft_repeat(sig) <= TOOL_SOFT_REPEAT_LIMIT):
            # NOT a loop_breaker: this returns the data, so it must stay out of the
            # refused / loop_blocked accounting that keys on a truthy loop_breaker.
            return _finish({
                "_repeat_warning": (
                    f"{canon} returned the same result {n}\u00d7 this turn with the same "
                    f"arguments. Returning the cached result \u2014 don't call it again "
                    f"unchanged; change the arguments or move on."
                ),
                "cached_result": cached,
                "repeat_count": n,
                "tool": canon,
                "repeat_softened": True,
            })
        suggestions = []
        if canon == "read_file":
            suggestions.append("for large files, use `offset` to read the next chunk (see the `hint` field in the truncated response)")
        if canon == "list_directory":
            suggestions.append("if you need a deeper level, pass the subdirectory as `path`")
        suggestion_blob = (". " + "; ".join(suggestions)) if suggestions else ""
        reason = ("returned the SAME result" if stuck_streak else "been called")
        return _finish({
            "error": (
                f"refused: {canon} has {reason} {n} times this turn with the SAME "
                f"arguments and nothing changed. Repeating won\u2019t help \u2014 change the "
                f"arguments{suggestion_blob}, do something that actually moves the state "
                f"(edit the file, apply the fix, then re-check), or end the turn and "
                f"tell the user what you found so far."
            ),
            # The data from the last identical call, so the turn can still answer
            # instead of dying on the refusal.
            "cached_result": cached,
            "repeat_count": n,
            "tool": canon,
            "loop_breaker": True,
        })
    directory_walk_count = _reserve_tool_family_call(canon)
    if directory_walk_count > DIRECTORY_WALK_LIMIT:
        return _finish({
            "error": (
                f"refused: directory listing has been used {directory_walk_count} times consecutively "
                "without changing strategy. Stop walking folders one by one. Use recursive search, "
                "switch_execution_target if the user named another device, or report that the item "
                "was not found in the locations already checked."
            ),
            "repeat_count": directory_walk_count,
            "tool": canon,
            "loop_breaker": True,
            "tool_family": "directory_walk",
        })
    try:
        result = t["fn"](args or {})
    except Exception as e:
        traceback.print_exc()
        result = {"error": str(e)}
    _record_tool_result(sig, result)
    _clear_invalid_call(canon)
    _record_url_fetch(canon, args or {}, result)
    return _finish(result)
