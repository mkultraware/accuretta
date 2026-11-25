# SPDX-License-Identifier: AGPL-3.0-only
"""The local action audit: which tool calls are recorded, what target each names, and
appending a bounded record.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from accuretta.redteam.mission_logs import ACTION_AUDIT_FILE
from accuretta.redteam.mission_logs import _ACTION_AUDIT_LOCK
from typing import Any


ACTION_AUDIT_MAX = 4000


_DESKTOP_TOOL_NAMES = {
    "screenshot", "describe_screen", "list_windows", "desktop_snapshot",
    "desktop_launch_app", "desktop_focus_window", "desktop_click",
    "desktop_type_text", "desktop_press_keys", "desktop_close_window",
    "desktop_invoke", "desktop_set_value", "desktop_scroll",
    "desktop_drag", "desktop_open_url", "view_image",
}


# Tools that mutate a file named by their `path` argument. Used by the
# parallel tool-execution batcher in run_chat_turn: two calls writing the SAME
# path must not run concurrently (last-write-wins races), so they land in the
# same serial batch.
_WRITE_CAPABLE_TOOLS = frozenset(
    {"write_file", "edit_file", "delete_file", "replace_ast_node",
     "remote_write_file", "remote_file_begin",
     "save_skill"})


_ACTION_AUDIT_ALWAYS = frozenset({
    "run_powershell", "run_tests", "session_start", "session_send", "session_stop",
    "remote_shell", "remote_write_file",
    "remote_file_begin", "remote_file_append", "remote_file_commit", "remote_file_abort",
    "remote_copy_to", "remote_copy_from",
    "open_program", "network_snapshot", "parse_event_logs",
    "hunt_persistence", "git_init", "git_clone", "git_add", "git_commit",
    "git_checkout", "git_branch", "git_merge", "git_pull", "git_push",
    "git_stash", "git_restore", "git_clean",
})


def _is_audited_action_tool(name: str) -> bool:
    return bool(
        name in _WRITE_CAPABLE_TOOLS
        or name in _DESKTOP_TOOL_NAMES
        or name in _ACTION_AUDIT_ALWAYS
        or name.startswith(("sandbox_", "recon_", "rt_", "mcp_"))
    )


def _action_audit_target(args: dict) -> str:
    """Return a useful target without query strings, credentials, or content."""
    if not isinstance(args, dict):
        return ""
    for key in ("path", "dest", "target", "host", "domain", "url", "session_id"):
        raw = str(args.get(key) or "").strip()
        if not raw:
            continue
        if "://" in raw:
            try:
                u = urllib.parse.urlsplit(raw)
                host = u.hostname or ""
                port = f":{u.port}" if u.port else ""
                return f"{u.scheme}://{host}{port}{u.path}"[:240]
            except Exception:
                return "[url]"
        return re.sub(r"\s+", " ", raw)[:240]
    return ""


def _record_action_audit(name: str, args: dict, result: Any) -> None:
    """Append a bounded local action record. Logging must never block work."""
    if not _is_audited_action_tool(name):
        return
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, ensure_ascii=False,
                                  default=str).encode("utf-8", errors="replace")
        encoded_result = json.dumps(result, sort_keys=True, ensure_ascii=False,
                                    default=str).encode("utf-8", errors="replace")
        status = "ok"
        if isinstance(result, dict):
            if result.get("scope_blocked"):
                status = "scope_blocked"
            elif result.get("loop_breaker"):
                status = "loop_blocked"
            elif result.get("error"):
                status = "error"
            elif result.get("ok") is False:
                status = "failed"
            elif result.get("exit_code") not in (None, 0):
                status = f"exit_{result.get('exit_code')}"
        chat_id = _current_chat_id.get() or _get_current_chat() or ""
        entry = {
            "t": int(time.time()),
            "chat_id": chat_id,
            "tool": name,
            "status": status,
            "target": _action_audit_target(args or {}),
            "args_sha256": hashlib.sha256(encoded_args).hexdigest(),
            "result_sha256": hashlib.sha256(encoded_result).hexdigest(),
        }
        try:
            chat = ((get_chats().get("chats", {}) or {}).get(chat_id)
                    if chat_id else None)
            mission = chat.get("mission") if isinstance(chat, dict) else None
            if isinstance(mission, dict) and mission.get("authorization_id"):
                entry["authorization_id"] = str(mission["authorization_id"])[:100]
                entry["phase"] = str(mission.get("phase") or "")[:30]
        except Exception:
            pass
        DATA.mkdir(parents=True, exist_ok=True)
        with _ACTION_AUDIT_LOCK:
            with open(ACTION_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            try:
                with open(ACTION_AUDIT_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > ACTION_AUDIT_MAX + 300:
                    with open(ACTION_AUDIT_FILE, "w", encoding="utf-8") as f:
                        f.writelines(lines[-ACTION_AUDIT_MAX:])
            except Exception:
                pass
    except Exception:
        pass
