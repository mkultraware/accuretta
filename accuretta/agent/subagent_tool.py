# SPDX-License-Identifier: AGPL-3.0-only
"""The spawn_subagent tool: a read-only helper model run with its own small tool set,
token budget and call parsing.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import subagents
import urllib.error
import urllib.parse
import urllib.request
from accuretta.agent.execution_target import _execution_target_allows_tool
from accuretta.agent.invalid_calls import _invalid_tool_path
from accuretta.agent.invalid_calls import _missing_required_tool_args
from accuretta.agent.llama_request import llama_options
from accuretta.agent.native_tools import _TURN_BOUNDARY_STOPS
from accuretta.agent.research_runs import _research_context
from accuretta.agent.research_runs import _research_store
from accuretta.agent.tool_call_parse import extract_tool_calls
from accuretta.agent.tool_schemas import _wire_tool_parameters
from accuretta.agent.tool_sets import _excluded_tools
from accuretta.agent.verification import _invoke_tool_with_context
from accuretta.agent.wire_messages import _sanitize_messages_for_openai
from accuretta.agent.wire_messages import _verification_wanted
from accuretta.core.cancels import _chat_cancels
from accuretta.core.cancels import _chat_cancels_lock
from accuretta.core.cancels import _set_cancel_resp
from accuretta.core.chat_events import _emit_chat_event
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.effort import _effort_tier
from accuretta.core.endpoints import LLAMA
from accuretta.core.settings import get_settings
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.tool_registry import TOOLS
from accuretta.runtime.llama_post import _savings_note_completion
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.model_presets import _model_features
from deep_research import RESEARCH_TOOLS


def _research_action(name: str, args: dict) -> dict:
    chat_id, research_id = _research_context()
    if not research_id:
        return {"error": "Start Deep Research from the mode menu and provide a brief first."}
    try:
        result = _research_store.apply(research_id, chat_id, name, args)
        _emit_chat_event({"type": "research_update", "research": result["research"]})
        return result["model_result"]
    except (ValueError, OSError) as error:
        return {"error": str(error)}


# ---- spawn_subagent (Sage-A1.5 agentic orchestration) -----------------------
# Only shown to models trained on this exact schema (see _MODEL_SAMPLING_PRESETS
# "subagents"). Sub-agents get read-only tools so they never raise approvals in
# a conversation nobody is looking at, and never see spawn_subagent itself.
_SUBAGENT_READ_TOOLS = (
    "web_search", "web_fetch", "read_file", "list_directory", "find_files", "grep_files",
    "file_inspect", "project_map", "read_skeleton", "find_symbol", "find_references",
    "search_memories",
)


_SUBAGENT_TOOL_SUMMARY = ("read-only tools (web search, web page reading, and reading and "
                          "searching files in the workspace)")


_SUBAGENT_MAX_TOKENS = 10240


_SUBAGENT_THINK_BUDGET = 3072


def _subagent_tools(chat_id: str) -> list[dict]:
    excluded = _excluded_tools(True, chat_id)
    names = [n for n in _SUBAGENT_READ_TOOLS
             if n in TOOLS and n not in excluded and _execution_target_allows_tool(n, chat_id)]
    if _research_context(chat_id)[1]:
        names = [n for n in names if n in RESEARCH_TOOLS]
    return [{"type": "function",
             "function": {"name": n, "description": TOOLS[n]["description"],
                          "parameters": _wire_tool_parameters(TOOLS[n]["parameters"])}}
            for n in names]


def _subagent_complete(chat_id: str, messages: list[dict], tools: list[dict]) -> tuple[dict, dict]:
    s = get_settings()
    payload = {
        "model": s.get("model") or "local",
        "messages": _sanitize_messages_for_openai(messages),
        "stream": False,
        **llama_options(s),
        "stop": list(_TURN_BOUNDARY_STOPS),
        "thinking_budget_tokens": _SUBAGENT_THINK_BUDGET,
    }
    payload["max_tokens"] = min(int(payload.get("max_tokens") or _SUBAGENT_MAX_TOKENS), _SUBAGENT_MAX_TOKENS)
    tpl_kwargs = {"enable_thinking": True, "thinking_budget": _SUBAGENT_THINK_BUDGET}
    if _verification_wanted(s, _effort_tier(s.get("reasoning_effort")), str(s.get("model_path") or "")):
        tpl_kwargs["verification"] = True
    payload["chat_template_kwargs"] = tpl_kwargs
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        f"{LLAMA}/v1/chat/completions", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=900) as resp:
        # Registered so Stop closes the in-flight request, as it does for
        # the main stream.
        _set_cancel_resp(chat_id, resp)
        data = json.loads(resp.read().decode("utf-8"))
    _savings_note_completion(data)
    choice = (data.get("choices") or [{}])[0]
    return (choice.get("message") or {}), (data.get("usage") or {})


def _subagent_parse_calls(message: dict) -> list[dict]:
    calls = []
    for tc in message.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {}
        calls.append({"id": str(tc.get("id") or subagents.call_id()),
                      "name": _resolve_tool_name(fn.get("name") or ""),
                      "arguments": args if isinstance(args, dict) else {}})
    content = str(message.get("content") or "")
    if not calls and ("<tool_call>" in content or "<function=" in content):
        for c in extract_tool_calls(content, python_calls=False):
            calls.append({"id": subagents.call_id(),
                          "name": _resolve_tool_name(c.get("name") or ""),
                          "arguments": c.get("arguments") if isinstance(c.get("arguments"), dict) else {}})
    return calls


def _subagent_call_tool(name: str, args: dict) -> dict:
    if _invalid_tool_path(name, args):
        return {"error": "Paths must be a single line.", "not_executed": True}
    missing = _missing_required_tool_args(name, args)
    if missing:
        return {"error": f"Missing required argument(s): {', '.join(missing)}", "not_executed": True}
    return _invoke_tool_with_context(name, args, subagents.call_id())


def _spawn_subagent_tool(args: dict) -> dict:
    if "subagents" not in _model_features():
        return {"error": "spawn_subagent is not available for the loaded model.", "not_executed": True}
    s = get_settings()
    tasks, error = subagents.validate_tasks(args, int(s.get("subagent_max_tasks") or 5))
    if error:
        return {"error": error, "not_executed": True}
    chat_id = _get_current_chat()
    parent_call = _current_tool_call_id.get()
    tools = _subagent_tools(chat_id)
    if not tools:
        return {"error": "No read-only tools are available to sub-agents right now.", "not_executed": True}
    try:
        slots = max(1, int(s.get("n_parallel") or 1))
    except (TypeError, ValueError):
        slots = 1
    ctx_limit = _llama_props_ctx() or int(s.get("num_ctx") or 32768)
    with _chat_cancels_lock:
        cancel_ev = (_chat_cancels.get(chat_id) or {}).get("cancel")
    total = len(tasks)

    def progress(index: int, note: str) -> None:
        label = f"Sub-agent {index + 1}/{total} · {note}"
        _emit_chat_event({"type": "subagent_progress", "call_id": parent_call,
                          "label": label, "log": label})

    results = subagents.run_batch(
        tasks, concurrency=slots, progress=progress,
        complete=lambda messages, tools_: _subagent_complete(chat_id, messages, tools_),
        parse_calls=_subagent_parse_calls, call_tool=_subagent_call_tool, tools=tools,
        max_turns=int(s.get("subagent_max_turns") or 20),
        ctx_budget=int(ctx_limit * 0.7),
        cancelled=lambda: bool(cancel_ev and cancel_ev.is_set()),
    )
    out = {"ok": True, "results": results,
           "note": "Results are in task order. Check each status before relying on an answer."}
    if _research_context(chat_id)[1]:
        out["note"] += (" Evidence sources the sub-agents fetched are already stored in this "
                        "notebook: cite them with research_note by source ID after reading the "
                        "stored text with research_notebook(source_id=...).")
    return out
