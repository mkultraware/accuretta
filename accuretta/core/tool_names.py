# SPDX-License-Identifier: AGPL-3.0-only
"""Tool name aliases: the other names models use for real tools, and the lookup that
maps a called name back to the registry.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.tool_registry import TOOLS


# Common synonyms the model invents instead of the canonical tool names. Map
# them so a single typo doesn't burn an entire tool round on "unknown tool".
TOOL_ALIASES = {

    "repo_map": "project_map",
    "repository_map": "project_map",
    "map_project": "project_map",
    "skeleton": "read_skeleton",
    "ast": "read_skeleton",
    "syntax": "check_syntax",
    "lint": "check_syntax",
    "test": "run_tests",
    "pytest": "run_tests",


    "pcap": "analyze_pcap",
    "pcapng": "analyze_pcap",
    "analyze_packet": "analyze_pcap",
    "evtx": "parse_evtx",
    "secret_scan": "scan_secrets",
    "find_secrets": "scan_secrets",
    "cve_scan": "check_deps",
    "deps_scan": "check_deps",
    "audit_headers": "audit_http_headers",
    "security_headers": "audit_http_headers",

    "create_file": "write_file",
    "save_file": "write_file",
    "make_file": "write_file",
    "new_file": "write_file",
    "patch_file": "edit_file",
    "modify_file": "edit_file",
    "update_file": "edit_file",
    "view_file": "read_file",
    "open_file": "read_file",
    "cat_file": "read_file",
    "cat": "read_file",
    "list_dir": "list_directory",
    "ls": "list_directory",
    "ls_dir": "list_directory",
    "dir": "list_directory",
    "rm": "delete_file",
    "remove_file": "delete_file",
    "rm_file": "delete_file",
    "powershell": "run_powershell",
    "shell": "sandbox_run",
    "bash": "sandbox_run",
    "sandbox": "sandbox_run",
    "sandbox_shell": "sandbox_run",
    "guest_shell": "sandbox_run",
    "search_web": "web_search",
    "google": "web_search",
    "duckduckgo": "web_search",
    "image_search": "web_image_search",
    "search_images": "web_image_search",
    "fetch": "web_fetch",
    "http_get": "web_fetch",
    # native-vision tools — models reach for "look"/"see"/"render" as verbs.
    "look_at_image": "web_view_image",
    "view_image": "web_view_image",
    "see_image": "web_view_image",
    "read_image": "web_view_image",
    "render_page": "web_render",
    "browse": "web_render",
    "screenshot_page": "web_render",
    "open_page": "web_render",
    "watch_video": "web_watch_video",
    "watch_clip": "web_watch_video",
    "see_video": "web_watch_video",
    "screenshot_screen": "screenshot",
    "take_screenshot": "screenshot",
    "windows": "list_windows",
    "save_memory": "remember",
    "delete_memory": "forget",
    "netstat": "network_snapshot",
    "network_scan": "network_snapshot",
    "inspect_network": "network_snapshot",
    "list_connections": "network_snapshot",
    "sniff_network": "network_snapshot",
    # blue-team aliases — models naturally reach for these synonyms.
    "event_logs": "parse_event_logs",
    "event_log": "parse_event_logs",
    "read_event_logs": "parse_event_logs",
    "get_winevent": "parse_event_logs",
    "winevent": "parse_event_logs",
    "persistence_check": "persistence_hunt",
    "persistence_scan": "persistence_hunt",
    "startup_audit": "persistence_hunt",
    # APK analysis aliases — models naturally reach for these synonyms.
    "apk_scan": "scan_apk",
    "analyze_apk": "scan_apk",
    "apk_analyze": "scan_apk",
    "android_scan": "scan_apk",
    "apk_security_scan": "scan_apk",
    "apk_decompile": "decompile_apk",
    "jadx": "decompile_apk",
    "jadx_decompile": "decompile_apk",
    "decompile": "decompile_apk",
    # Ghidra aliases — models reach for "ghidra" as a verb regularly.
    "ghidra": "ghidra_analyze",
    "pyghidra": "ghidra_analyze",
    "analyze_native": "ghidra_analyze",
    "analyze_binary": "ghidra_analyze",
    "decompile_native": "ghidra_analyze",
    "disasm_binary": "ghidra_analyze",
    "static_analyze": "ghidra_analyze",
    # binary_inspect aliases — models reach for these synonyms when triaging
    # native binaries before deciding whether to call ghidra_analyze.
    "pe_info": "binary_inspect",
    "pe_inspect": "binary_inspect",
    "elf_info": "binary_inspect",
    "elf_inspect": "binary_inspect",
    "binary_info": "binary_inspect",
    "exe_inspect": "binary_inspect",
    "inspect_binary": "binary_inspect",
    "inspect_pe": "binary_inspect",
    "inspect_elf": "binary_inspect",
    "file_format": "binary_inspect",
    "binary_triage": "binary_inspect",
    "triage_binary": "binary_inspect",
    # yara_scan aliases.
    "yara": "yara_scan",
    "scan_yara": "yara_scan",
    "yara_match": "yara_scan",
    "malware_scan": "yara_scan",
    "ioc_scan": "yara_scan",
    "ioc_match": "yara_scan",
}


def _resolve_tool_name(name: str) -> str:
    if not name:
        return name
    if name in TOOLS:
        return name
    lc = name.lower()
    if lc in TOOLS:
        return lc
    if lc in TOOL_ALIASES:
        return TOOL_ALIASES[lc]
    return name
