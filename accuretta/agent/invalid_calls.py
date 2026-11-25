# SPDX-License-Identifier: AGPL-3.0-only
"""Tool calls the model got wrong: missing arguments, bad paths, unknown tools, and the
streak limit that stops a model repeating the same mistake.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.tool_history import _tool_history_lock
from accuretta.core.chats import _get_current_chat
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.tool_registry import TOOLS
from accuretta.security.blocked_writes import _FILE_IN_PARTS
from accuretta.security.blocked_writes import _blocked_write
from typing import Any


# Consecutive calls per tool that never ran because their arguments were
# missing or malformed. They return before the repeat bookkeeping, so without
# this count a model that keeps losing its arguments is never stopped.
_invalid_call_streaks: dict[str, dict[str, int]] = {}  # chat_id -> tool -> consecutive invalid calls


INVALID_CALL_LIMIT = 2       # 2nd invalid call in a row to the same tool is refused.


def _note_invalid_call(canon: str) -> int:
    """Count one more consecutive invalid call to this tool; return the streak."""
    chat_id = _get_current_chat()
    with _tool_history_lock:
        streaks = _invalid_call_streaks.setdefault(chat_id, {})
        streaks[canon] = streaks.get(canon, 0) + 1
        return streaks[canon]


def _clear_invalid_call(canon: str) -> None:
    with _tool_history_lock:
        _invalid_call_streaks.get(_get_current_chat(), {}).pop(canon, None)


def _invalid_call_result(canon: str, result: dict) -> dict:
    """Mark a not-executed invalid call, refusing it once the streak is reached."""
    streak = _note_invalid_call(canon)
    result["invalid_streak"] = streak
    if streak >= INVALID_CALL_LIMIT:
        result["loop_breaker"] = True
        result["error"] = (
            f"refused: {canon} arrived without valid arguments {streak} times in a row. "
            "Repeating the same call will fail the same way. " + result["error"])
        if canon == "write_file":
            result["error"] += " Instead, " + _FILE_IN_PARTS + "."
        elif canon == "remote_write_file":
            result["error"] += (" Instead, write the file in parts with remote_file_begin, "
                                "remote_file_append and remote_file_commit.")
    return result


def _missing_required_tool_args(name: str, args: Any) -> list[str]:
    """Return required schema fields that are absent or materially empty."""
    canon = _resolve_tool_name(name)
    spec = TOOLS.get(canon) or {}
    required = (((spec.get("parameters") or {}).get("required")) or [])
    values = args if isinstance(args, dict) else {}
    missing: list[str] = []
    for key in required:
        if key not in values:
            missing.append(key)
            continue
        value = values.get(key)
        if value is None:
            missing.append(key)
        elif isinstance(value, str) and not value.strip():
            missing.append(key)
        elif isinstance(value, (list, dict)) and not value:
            missing.append(key)
    alternatives = (spec.get("parameters") or {}).get("anyOf") or []
    if alternatives:
        choices = [branch.get("required", []) for branch in alternatives]
        if not any(all(values.get(key) and (
                not isinstance(values[key], str) or values[key].strip())
                for key in choice) for choice in choices):
            missing.extend(key for key in choices[0] if key not in missing)
    # force=true right after a blocked write reuses that write's content.
    if (canon == "write_file" and missing == ["content"] and values.get("force")
            and _blocked_write(str(values.get("path") or "")) is not None):
        return []
    return missing


def _invalid_tool_path(name: str, args: Any) -> bool:
    """Reject multiline paths before tool dispatch, including native calls."""
    spec = TOOLS.get(_resolve_tool_name(name)) or {}
    properties = (spec.get("parameters") or {}).get("properties") or {}
    values = args if isinstance(args, dict) else {}
    return any(
        key in properties and isinstance(values.get(key), str)
        and any(ch in values[key] for ch in ("\r", "\n", "\0"))
        for key in ("path", "file_path", "directory", "cwd")
    )
