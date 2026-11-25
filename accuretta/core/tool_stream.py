# SPDX-License-Identifier: AGPL-3.0-only
"""A running tool's output on its way to the chat: live output streaming, execution
events, the command history file, and masking GitHub tokens in what is shown.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.core.chat_events import _chat_emitters
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _current_tool_name
from accuretta.core.chats import _get_current_chat
from accuretta.core.paths import DATA
from accuretta.core.storage import _cmd_history_lock
from accuretta.core.tool_names import _resolve_tool_name


# PowerShell command history — append-only JSON Lines, one row per command
# that hits _run_powershell. Surfaced via /api/cmd-history and the History
# drawer in the UI. Kept rolling: anything past CMD_HISTORY_MAX gets trimmed
# on each write so the file never grows unbounded.
CMD_HISTORY_FILE = DATA / "cmd_history.jsonl"


CMD_HISTORY_MAX = 2000


def _execution_tool_name(fallback: str) -> str:
    return _current_tool_name.get() or _resolve_tool_name(fallback)


def _emit_execution_event(state: str, target: str, command: str, *,
                          cwd: str = "", chat_id: str = "", **details) -> None:
    """Report the real command lifecycle, separately from the model tool call."""
    cid = chat_id or _current_chat_id.get() or _get_current_chat()
    emit = _chat_emitters.get(cid) if cid else None
    if not emit:
        return
    event = {
        "type": f"command_{state}",
        "name": _execution_tool_name("run_powershell"),
        "call_id": _current_tool_call_id.get(),
        "target": target,
        "command": command,
        "cwd": cwd,
    }
    event.update({k: v for k, v in details.items() if v is not None})
    try:
        emit(event)
    except Exception:
        pass


def _execution_not_run(message: str, target: str, command: str, *,
                       reason: str = "blocked", cwd: str = "", **details) -> dict:
    event_details = dict(details)
    event_details.setdefault("reason", reason)
    event_details.setdefault("message", message)
    _emit_execution_event("not_executed", target, command, cwd=cwd,
                          **event_details)
    result = {
        "error": message,
        "not_executed": True,
        "executed": False,
        "execution_target": target,
        "execution_state": reason,
    }
    result.update(details)
    return result


# GitHub tokens in what a tool printed: `git credential fill` can print a live
# gh login token, and from there it would go into the model's context, the
# saved chat and the command log. Each one keeps its prefix, so the model
# still knows a token was there.
_GITHUB_TOKEN_RE = re.compile(r"\b(gh[opsur]_)[A-Za-z0-9]{30,}\b|\b(github_pat_)[A-Za-z0-9_]{40,}")


def _mask_github_tokens(value):
    """`value` (text, or a dict/list of them) with every GitHub token hidden."""
    if isinstance(value, str):
        if "gh" not in value and "github_pat_" not in value:
            return value
        return _GITHUB_TOKEN_RE.sub(lambda m: (m.group(1) or m.group(2)) + "…(hidden)", value)
    if isinstance(value, dict):
        return {k: _mask_github_tokens(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask_github_tokens(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_mask_github_tokens(v) for v in value)
    return value


def _emit_tool_stream(name: str, text: str, chat_id: str = "", target: str = "",
                      call_id: str = "") -> None:
    """If a chat turn is active, emit a tool_stream SSE event.

    Reader threads do not inherit ContextVars, so subprocess callers capture
    the chat id before spawning and pass it explicitly.
    """
    cid = chat_id or _current_chat_id.get()
    emit = _chat_emitters.get(cid) if cid else None
    if emit:
        try:
            call_id = call_id or _current_tool_call_id.get() or ""
            # A command that reads an image or other binary file prints lines of
            # undecodable bytes. They mean nothing to the reader and can come by the
            # thousand, so a run of them shows as one short note.
            run_key = (cid, call_id)
            if _looks_binary(text):
                if _STREAM_BINARY_RUNS.get(run_key):
                    return
                if len(_STREAM_BINARY_RUNS) > 512:
                    _STREAM_BINARY_RUNS.clear()
                _STREAM_BINARY_RUNS[run_key] = True
                text = "[binary data hidden]"
            elif _STREAM_BINARY_RUNS:
                _STREAM_BINARY_RUNS.pop(run_key, None)
            emit({
                "type": "tool_stream",
                "name": _execution_tool_name(name),
                "call_id": call_id,
                "target": target,
                "text": _mask_github_tokens(text)[:240],
            })
        except Exception:
            pass


# (chat id, tool call id) -> True while that call's live output is in a run of
# binary lines (see _emit_tool_stream).
_STREAM_BINARY_RUNS: dict[tuple[str, str], bool] = {}


def _looks_binary(text: str) -> bool:
    """True for a line that is mostly undecodable bytes (U+FFFD) or control
    characters: what a command prints when it reads a binary file."""
    sample = (text or "")[:2000]
    if len(sample) < 8:
        return False
    bad = sum(1 for ch in sample if ch == "�" or (ch < " " and ch not in "\t\r\n\x1b"))
    return bad >= 8 and bad * 5 >= len(sample)


def _append_cmd_history(entry: dict) -> None:
    """Append a started host or WSL command to cmd_history.jsonl, then trim
    the file to CMD_HISTORY_MAX lines if it's grown past that. Failures are
    swallowed — command logging must never block a tool call from returning."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        entry = _mask_github_tokens(entry)
        with _cmd_history_lock:
            with open(CMD_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            # Cheap rotation: the file is capped (a few hundred KB at worst), so rewrite
            # it with the trailing CMD_HISTORY_MAX entries when it grows too long.
            try:
                with open(CMD_HISTORY_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > CMD_HISTORY_MAX + 200:  # only rotate when meaningfully over
                    keep = lines[-CMD_HISTORY_MAX:]
                    with open(CMD_HISTORY_FILE, "w", encoding="utf-8") as f:
                        f.writelines(keep)
            except Exception:
                pass
    except Exception:
        pass


def _read_cmd_history(limit: int = 200, chat_id: str = "") -> list[dict]:
    """Return the last `limit` history entries, newest first. Optionally
    filter to entries that ran in a specific chat."""
    if not CMD_HISTORY_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with _cmd_history_lock:
            with open(CMD_HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if chat_id and entry.get("chat_id") != chat_id:
            continue
        out.append(entry)
    out.reverse()
    return out[:limit]
