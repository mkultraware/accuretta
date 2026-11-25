# SPDX-License-Identifier: AGPL-3.0-only
"""Checking the agent's own work: verification debt after writes, task check records and
their summary, and the per-call context a tool runs with.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import time
from accuretta.agent.action_audit import _WRITE_CAPABLE_TOOLS
from accuretta.agent.execution_target import _selected_execution_target
from accuretta.agent.file_tools import _tool_write_history
from accuretta.agent.invalid_calls import _invalid_call_streaks
from accuretta.agent.project_follow import _project_follow
from accuretta.agent.research_runs import _research_context
from accuretta.agent.research_runs import _research_store
from accuretta.agent.tool_contracts import _CHAT_STATE_TOOLS
from accuretta.agent.tool_dispatch import invoke_tool
from accuretta.agent.tool_history import _tool_call_history
from accuretta.agent.tool_history import _tool_family_history
from accuretta.agent.tool_history import _tool_history_lock
from accuretta.agent.tool_history import _tool_progress
from accuretta.agent.tool_history import _tool_url_history
from accuretta.core.chat_events import _emit_chat_event
from accuretta.core.chats import _chat_ctx
from accuretta.core.chats import _chat_cv
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _current_tool_name
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.security.approvals import _steering
from accuretta.tools.run_tests import _verification_key
from agent_reliability import feedback
from task_state import file_revision
from typing import Any


def _reset_tool_call_history(chat_id: str) -> None:
    with _tool_history_lock:
        _tool_call_history[chat_id] = {}
        _tool_write_history[chat_id] = {}
        _tool_url_history[chat_id] = {}
        _tool_family_history[chat_id] = {}
        _tool_progress[chat_id] = 0
        _invalid_call_streaks[chat_id] = {}


def _update_verification_debt(debt: list[dict], name: str, args: dict,
                              result: Any) -> tuple[list[dict], bool]:
    current = [dict(x) for x in (debt or []) if isinstance(x, dict) and x.get("path")]
    canon = _resolve_tool_name(name)
    if not isinstance(result, dict) or result.get("error") or result.get("ok") is False or result.get("not_executed") or result.get("exit_code") not in (None, 0):
        return current, False
    before = json.dumps(current, sort_keys=True, default=str)
    target = _selected_execution_target()
    if canon in _WRITE_CAPABLE_TOOLS and canon != "save_skill":
        path = str(result.get("path") or args.get("path") or "").strip()
        if path:
            key = _verification_key(path)
            current = [x for x in current if not (_verification_key(x["path"]) == key and x.get("target", "host") == target)]
            current.append({"path": path, "target": target, "via": canon,
                            "since": int(time.time()), "status": "unchecked",
                            "sha256": file_revision(path) if target == "host" else result.get("sha256")})
    elif canon == "check_syntax":
        key = _verification_key(args.get("path"))
        proof = result.get("verification") or {}
        for entry in current:
            if (_verification_key(entry["path"]) == key and entry.get("target", "host") == "host"
                    and proof.get("sha256") and entry.get("sha256") == proof["sha256"]
                    and file_revision(entry["path"]) == proof["sha256"]):
                entry.update(status="syntax_checked", syntax_checked_at=int(time.time()))
    elif canon == "run_tests":
        proof = result.get("verification") or {}
        checked = proof.get("revisions") or {}
        if proof.get("tests_ran") and proof.get("target") == "host":
            current = [x for x in current if not (
                x.get("target", "host") == "host" and x.get("sha256")
                and checked.get(_verification_key(x["path"])) == x["sha256"]
                and file_revision(x["path"]) == x["sha256"])]
    return current, before != json.dumps(current, sort_keys=True, default=str)


def _task_check_record(name: str, args: dict, result: Any) -> dict | None:
    name = _resolve_tool_name(name)
    if name not in {"check_syntax", "run_tests"} or not isinstance(result, dict):
        return None
    failed = bool(result.get("error") or result.get("ok") is False
                  or result.get("not_executed") or result.get("exit_code") not in (None, 0))
    unchecked = name == "check_syntax" and result.get("check_status") == "unchecked"
    proof = result.get("verification") or {}
    revisions = proof.get("revisions", {}) if name == "run_tests" else {
        _verification_key(args.get("path")): proof.get("sha256")}
    return {"name": name, "key": name + ":" + str(args.get("path") or args.get("command") or args.get("cwd") or ""),
            "status": "unchecked" if unchecked else "failed" if failed else "passed",
            "label": "Project tests" if name == "run_tests" else "Syntax check",
            "detail": str(result.get("error") or result.get("reason") or args.get("path") or args.get("command") or "")[:500],
            "full_tests": bool(name == "run_tests" and proof.get("tests_ran") and proof.get("target") == "host"),
            "revisions": revisions}


def _task_verification_summary(files: list[dict], checks: list[dict]) -> dict:
    covered, syntax = set(), set()
    latest = {check.get("key", check["name"]): check for check in checks}
    for check in latest.values():
        if check["status"] != "passed":
            continue
        revisions = check.get("revisions") or {}
        for item in files:
            key = _verification_key(item["path"])
            if item.get("after_hash") and revisions.get(key) == item["after_hash"]:
                if check.get("full_tests"):
                    covered.add(key)
                elif check["name"] == "check_syntax":
                    syntax.add(key)
    keys = {_verification_key(item["path"]) for item in files}
    status = ("failed" if any(c["status"] == "failed" for c in latest.values())
              else "passed" if keys and keys <= covered
              else "partial" if covered or syntax or any(c["status"] == "passed" for c in latest.values())
              else "unchecked")
    # syntax_files: files whose final revision passed a syntax check or full
    # tests. Lets the UI say "Syntax OK" instead of a vague "partial".
    return {"status": status, "checked_files": len(covered),
            "syntax_files": len((covered | syntax) & keys), "total_files": len(files),
            "checks": [{k: v for k, v in check.items() if k not in {"revisions", "full_tests", "key"}}
                       for check in checks]}


def _call_write_target(call: dict) -> str:
    """Normalized write target of a parsed tool call, or "" when the call is
    not a path-mutating write (no serialization needed for those)."""
    if _resolve_tool_name(call.get("name") or "") not in _WRITE_CAPABLE_TOOLS:
        return ""
    a = call.get("arguments")
    p = a.get("path") if isinstance(a, dict) else None
    return str(p) if p else ""


def _call_serial_key(call: dict) -> str:
    """Batcher serialization key: same key means the calls must not run
    concurrently. Path-writing tools key on their write target (normalized via
    the loop-breaker's raw path form), shared-store tools key on a constant so
    they serialize with each other; "" = no constraint (full parallelism)."""
    canon = _resolve_tool_name(call.get("name") or "")
    if canon.startswith("research_"):
        return "research-notebook"
    if canon in {"run_powershell", "run_tests"} or canon.startswith("sandbox_"):
        return "local-process"
    tgt = _call_write_target(call)
    if tgt:
        # Normalize so "./x.py" and "x.py" (and case variants on Windows)
        # serialize together — raw spelling would let the same file race.
        try:
            tgt = os.path.normcase(os.path.normpath(tgt))
        except Exception:
            pass
        return "write:" + tgt
    if canon in _CHAT_STATE_TOOLS:
        return "state"
    return ""


def _set_current_chat(chat_id: str) -> None:
    _chat_ctx.chat_id = chat_id
    try:
        _chat_cv.set(chat_id)
    except Exception:
        pass


def _invoke_tool_with_context(name: str, args: dict, call_id: str) -> dict:
    canon = _resolve_tool_name(name)
    name_token = _current_tool_name.set(canon)
    call_token = _current_tool_call_id.set(call_id)
    try:
        if _steering.pending(_current_chat_id.get()):
            return {"error": "Action skipped because the user updated the current task.",
                    "not_executed": True, "superseded": True}
        result = feedback(canon, invoke_tool(canon, args))
        _project_follow(canon, args, result)
        chat_id, research_id = _research_context()
        if research_id:
            observed = _research_store.observe(research_id, chat_id, canon, args, result)
            if observed:
                if observed["source_id"]:
                    result = {**result, "research_source_id": observed["source_id"]}
                _emit_chat_event({"type": "research_update", "research": observed["research"]})
        return result
    finally:
        _current_tool_call_id.reset(call_token)
        _current_tool_name.reset(name_token)
