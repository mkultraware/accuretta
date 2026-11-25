# SPDX-License-Identifier: AGPL-3.0-only
"""Where a tool runs: this machine, a paired remote machine, or the inference host, and
which tools each target allows.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.action_audit import _DESKTOP_TOOL_NAMES
from accuretta.core.chats import _get_current_chat
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.runtime.model_observation import _client_context_by_chat
from accuretta.tools.remote_machines import _load_remote_machines


_REMOTE_TOOL_NAMES = {
    "remote_shell", "remote_list_directory", "remote_read_file",
    "remote_write_file",
    "remote_file_begin", "remote_file_append", "remote_file_commit", "remote_file_abort",
    "remote_copy_to", "remote_copy_from",
}


_HOST_TARGET_TOOL_NAMES = {
    "read_file", "write_file", "edit_file", "delete_file",
    "list_directory", "find_files", "grep_files", "read_bytes", "file_inspect",
    "extract_archive", "project_map", "read_skeleton", "replace_ast_node",
    "find_references", "find_symbol", "check_syntax", "run_tests", "check_deps",
    "run_powershell", "run_python", "open_program", "network_snapshot", "parse_event_logs",
    "persistence_hunt",
    # shows this PC's files and screen
    "show_image",
}


def _tool_targets_inference_host(name: str) -> bool:
    """Whether a tool acts on the PC selected away by a remote target."""
    canon = _resolve_tool_name(name)
    return bool(
        canon in _HOST_TARGET_TOOL_NAMES
        or canon in _DESKTOP_TOOL_NAMES
        or canon in _ANALYSIS_SUITE_TOOL_NAMES
        or canon.startswith("git_")
        or canon.startswith("session_")
    )


def _selected_execution_target(chat_id: str = "") -> str:
    ctx = _client_context_by_chat.get(chat_id or _get_current_chat() or "", {})
    return str(ctx.get("execution_target") or "host")


def _execution_target_allows_tool(name: str, chat_id: str = "") -> bool:
    """Whether the active target may see a device-affine tool."""
    canon = _resolve_tool_name(name)
    if _selected_execution_target(chat_id) == "host":
        return canon not in _REMOTE_TOOL_NAMES
    return not _tool_targets_inference_host(canon)


def _ready_remote_machines() -> list[dict]:
    return [
        row for row in _load_remote_machines().get("machines", [])
        if row.get("status") == "ready" and row.get("enabled", True)
    ]


# Reverse-engineering / forensics / firmware analysis suite — gated behind
# `analysis_tools_enabled` so a normal coding turn doesn't carry the schema
# token cost of tools it will never call. Visibility only; distinct from
# _ANALYSIS_TOOL_NAMES (result size caps — do NOT merge them). Front-end/JS
# secret scanning (scan_js_secrets, cors_probe) stays in the red-team set
# above; general helpers (find_files, grep_files, read_bytes, file_inspect,
# extract_archive, check_deps) stay ungated for normal coding turns.
_ANALYSIS_SUITE_TOOL_NAMES = {
    # APK / Android.
    "scan_apk", "decompile_apk",
    # native binary RE / disassembly.
    "binary_inspect", "ghidra_analyze", "disasm_at", "strings_dump",
    # firmware scan / carve / extraction.
    "binwalk_scan", "carve_file", "extract_squashfs",
    # signature scan + forensics.
    "yara_scan", "parse_evtx", "analyze_pcap", "scan_secrets",
}
