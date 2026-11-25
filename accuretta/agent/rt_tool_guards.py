# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team checks on a tool call's arguments: one target field for every tool, no
discovered secrets sent back out, and the mission log note.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from accuretta.agent.tool_sets import _RED_TEAM_TOOL_NAMES
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.tool_registry import TOOLS
from accuretta.redteam.js_secrets_tool import _RT_DISCOVERED_SECRET_LOCK
from accuretta.redteam.js_secrets_tool import _RT_DISCOVERED_SECRET_VALUES
from accuretta.redteam.js_secrets_tool import _rt_secret_guard_id
from accuretta.redteam.mission_logs import _RT_UNKEPT_LOCK
from accuretta.redteam.mission_logs import _RT_UNKEPT_LOGS_FILE
from accuretta.redteam.mission_logs import _rt_keeps_log
from pathlib import Path
from typing import Any


def _rt_args_contain_discovered_secret(args: Any, chat: dict | None) -> bool:
    guard_id = _rt_secret_guard_id(chat)
    if not guard_id:
        return False
    with _RT_DISCOVERED_SECRET_LOCK:
        protected = _RT_DISCOVERED_SECRET_VALUES.get(guard_id, ())
    if not protected:
        return False

    def contains(value: Any) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in protected)
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, (list, tuple, set)):
            return any(contains(item) for item in value)
        return False

    return contains(args)


def _rt_discovered_secret_submission_block(name: str, args: dict,
                                             chat: dict | None) -> str | None:
    """Block discovered frontend values from request-capable tools."""
    reporting_tools = {
        "scan_js_secrets", "rt_generate_report", "record_finding", "list_findings",
        "encode_decode", "write_file", "edit_file",
    }
    request_capable = (
        (name in _RED_TEAM_TOOL_NAMES and name not in reporting_tools)
        or name in _RT_SCOPE_AWARE_GENERIC_TOOLS
        or name in _RT_SCOPE_AWARE_COMMAND_TOOLS
        or name.startswith("mcp_")
    )
    if request_capable and _rt_args_contain_discovered_secret(args, chat):
        return (
            "refused: this request contains a credential found by scan_js_secrets. "
            "Discovered values may be displayed and written into reports, but the harness will "
            "not submit or use them in network, provider, authentication, or validation requests."
        )
    return None


def _rt_log_note(mission: dict | None, result: Any, folder: Path | None = None) -> None:
    """Remember evidence files a no-log engagement wrote, for later deletion."""
    if _rt_keeps_log(mission) or not mission.get("authorization_id"):
        return
    ev_root = (DATA / "recon_evidence").resolve()
    paths = [str(folder)] if folder else []
    stack = [result]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for k, v in item.items():
                if k in ("evidence_saved", "saved") and isinstance(v, str):
                    paths.append(v)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(item, list):
            stack.extend(x for x in item if isinstance(x, (dict, list)))
    keep = []
    for p in paths:
        try:
            if ev_root in Path(p).resolve().parents:
                keep.append(str(Path(p).resolve()))
        except Exception:
            continue
    if not keep:
        return
    key = str(mission["authorization_id"])
    with _RT_UNKEPT_LOCK:
        reg = load_json(_RT_UNKEPT_LOGS_FILE, {})
        reg = reg if isinstance(reg, dict) else {}
        files = reg.setdefault(key, [])
        new = [p for p in dict.fromkeys(keep) if p not in files]
        if new:
            files.extend(new)
            save_json(_RT_UNKEPT_LOGS_FILE, reg)


_RT_SCOPE_AWARE_GENERIC_TOOLS = {"web_fetch", "web_view_image", "web_render", "audit_http_headers"}


def _rt_unify_target_args(name: str, args):
    """Let every security tool accept the same destination names.

    Tools grew up reading `url`, `host`, `target` or `domain`. A model that
    uses the wrong one gets a "required" error and burns a turn, so a
    destination given under any of those names is moved to the one this tool
    reads. Nothing is added when the tool's own name is already present."""
    if not isinstance(args, dict):
        return args
    tool = TOOLS.get(name) or {}
    properties = ((tool.get("parameters") or {}).get("properties") or {})
    accepted = [key for key in ("url", "host", "domain", "target") if key in properties]
    if not accepted or any(isinstance(args.get(key), str) and args[key].strip() for key in accepted):
        return args
    given_key = next((key for key in ("url", "target", "host", "domain", "endpoint")
                      if key not in accepted and isinstance(args.get(key), str)
                      and args[key].strip()), None)
    if not given_key:
        return args
    value = args[given_key].strip()
    wanted = accepted[0]
    repaired = {k: v for k, v in args.items() if k != given_key}
    if wanted != "url" and "://" in value:
        # A host-style argument was handed a full URL: keep the host, and the
        # port when the tool takes one and the URL names it.
        try:
            parts = urllib.parse.urlsplit(value)
            if parts.port and "port" in properties and repaired.get("port") in (None, ""):
                repaired["port"] = parts.port
            value = parts.hostname or value
        except ValueError:
            pass
    repaired[wanted] = value
    return repaired


_RT_SCOPE_AWARE_COMMAND_TOOLS = {
    "run_powershell": "command",
    "run_python": "code",
    "remote_shell": "command",
    "session_start": "command",
    "session_send": "input",
    "sandbox_run": "command",
}
