# SPDX-License-Identifier: AGPL-3.0-only
"""Per-tool facts the agent loop uses: result size caps, analysis and chat-state tool
names, and each tool's contract row.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.action_audit import _ACTION_AUDIT_ALWAYS
from accuretta.agent.action_audit import _DESKTOP_TOOL_NAMES
from accuretta.agent.action_audit import _WRITE_CAPABLE_TOOLS
from accuretta.agent.skills import SKILL_BODY_CAP_CHARS


def _tool_contract_row(name: str, visible: set[str], unavailable: dict[str, str]) -> dict:
    git_mutating = name.startswith("git_") and name not in {
        "git_status", "git_diff", "git_log", "git_show", "git_remote", "git_branches"
    }
    if name in _WRITE_CAPABLE_TOOLS:
        side_effect, scope, approval = "workspace mutation", "workspace", "policy gated"
    elif name in _DESKTOP_TOOL_NAMES:
        side_effect, scope, approval = "host UI/action", "desktop allowlist", "policy gated"
    elif name.startswith(("recon_", "rt_")):
        side_effect, scope, approval = "target network traffic", "authorized mission allowlist", "engagement-mode gated"
    elif name.startswith("sandbox_"):
        side_effect, scope, approval = "sandbox process/network", "authorized mission + guest", "policy gated"
    elif name.startswith("mcp_"):
        side_effect, scope, approval = "connector defined", "connector defined", "connector/policy"
    elif name in _ACTION_AUDIT_ALWAYS or git_mutating:
        side_effect, scope, approval = "host/repository action", "workspace or host", "policy gated"
    elif name in _CHAT_STATE_TOOLS or name in {"record_finding"}:
        side_effect, scope, approval = "session state", "current chat", "none"
    else:
        side_effect, scope, approval = "read only", "workspace/local data", "none"
    return {
        "tool": name,
        "exposed": name in visible,
        "available": name not in unavailable,
        "unavailable_reason": unavailable.get(name, ""),
        "approval": approval,
        "scope": scope,
        "side_effect": side_effect,
        "result_cap_chars": int(_tool_result_cap(name)),
        "result_compaction": "bulk-field aware" if name in _ANALYSIS_TOOL_NAMES else "bounded JSON",
        "self_test": "schema/handler validated",
    }


# Tools whose results are bulky-by-design (string dumps, grep hits, disasm
# listings, signature scans). Truncated looser so the model can actually
# reason over the output instead of seeing the head + a cliff.
_ANALYSIS_TOOL_NAMES = {
    "strings_dump", "grep_files", "disasm_at",
    "binwalk_scan", "find_files", "file_inspect", "read_bytes",
    "network_snapshot",
    "scan_apk", "decompile_apk", "ghidra_analyze",
    "binary_inspect", "yara_scan",
    "scan_secrets", "parse_evtx", "analyze_pcap", "check_deps",
    "recon_port_scan", "recon_tls_audit", "recon_http_fingerprint",
    "recon_subdomains", "recon_dns",
    "recon_content_discovery", "recon_check_exposure", "recon_subdomain_takeover",
    "recon_cve_match", "recon_injection_probe", "recon_open_services",
    "recon_auth_spray", "recon_capture_evidence",
    "project_map", "read_skeleton", "check_syntax", "run_tests",
    # git diffs and logs are bulky-by-design too — the model needs to read the
    # whole patch to write a sane commit message or to verify a push payload.
    "git_diff", "git_log", "git_show", "git_status",
}


# Per-tool result caps for the model context. Unlisted tools use
# _tool_result_cap() (16K for analysis tools, 4K otherwise). network_snapshot
# routinely returns ~20 KB on a busy host and is useless chopped mid-JSON;
# scan_apk with deep:true on a real APK lands around 30-40 KB.
_TOOL_RESULT_CAPS = {
    "security_coverage": 24000,
    "compare_evidence": 16000,
    "rt_access_check": 12000,
    "review_finding": 12000,
    "record_finding": 12000,
    # read_file pages its own 20KB chunks with an offset hint. This cap must sit
    # above the serialized chunk or compress_tool_result elides it a second time,
    # mangling the chunk and the continuation hint.
    "read_file": 32000,

    # Accessibility snapshots are structured UI trees. Four kilobytes leaves
    # only a handful of controls and makes blind navigation useless; 24K keeps
    # a practical window view while remaining tolerable for local contexts.
    "desktop_snapshot": 24000,
    "desktop_invoke": 16000,
    "desktop_set_value": 16000,
    "desktop_scroll": 16000,

    "scan_secrets": 48000,
    "parse_evtx": 48000,
    "analyze_pcap": 48000,
    "check_deps": 24000,
    "parse_event_logs": 32000,   # up to 500 log entries, each with a ~900-char message
    "persistence_hunt": 24000,   # full inventory can be a few hundred entries

    "network_snapshot": 32000,
    # Native-vision descriptions (mmproj + selected GGUF): the model's own
    # detailed read of an image/page render. 12K keeps a full design read.
    "web_view_image": 12000,
    "web_render": 12000,
    "web_watch_video": 12000,
    # recon_subdomains can return a few hundred CT-log subdomains; the rest
    # are compact JSON. Give the suite room so the report isn't chopped.
    "recon_subdomains": 32000,
    "recon_dns": 16000,
    "recon_port_scan": 16000,
    "recon_content_discovery": 24000,
    "recon_check_exposure": 16000,
    "recon_subdomain_takeover": 24000,
    "recon_cve_match": 24000,
    "recon_injection_probe": 16000,
    "recon_open_services": 16000,
    # The full bundle catalog behind list_more_tools is big; a blind
    # "_truncated" envelope makes models re-emit the call with the same args.
    "list_more_tools": 48000,
    # Large exploit responses are summarized (status/headers/preview) with the
    # full body in the evidence receipt; small responses keep their body. This
    # cap covers either shape and the receipt summary.
    "http_request": 16000,
    "rt_browser": 32000,
    # Interactive sessions self-cap their output in read_model(); these are the
    # outer safety nets for the serialized envelope.
    "session_start": 16000,
    "session_send": 16000,
    "session_read": 16000,
    "sql_injection": 16000,  # dumped rows can be large
    "fuzz": 16000,           # many per-value response summaries
    "batch_probe": 16000,    # hits across many endpoints
    "scan_js_secrets": 48000,  # exact values, including complete private-key blocks
    "tcp_send": 32000,         # raw service/bot output can be verbose
    "scan_apk": 48000,
    "decompile_apk": 24000,
    # ghidra_analyze: imports + exports + 200 strings + a decompiled function fits
    # in 32K on real .so files; 40K leaves room for a chunky decompile.
    "ghidra_analyze": 40000,
    # binary_inspect: header + sections + imports reach 20-30 KB on a dll with
    # hundreds of imports; 32K shows the full import table.
    "binary_inspect": 32000,
    # yara_scan: matches across a directory can balloon. 32K is enough room
    # for a full report on ~50 hit files without trampling the rest of context.
    "yara_scan": 32000,
    # git_diff with a multi-file refactor easily clears 50K. The model needs
    # the whole patch to write an accurate commit message; truncation here
    # produces fabricated change descriptions.
    "git_diff": 64000,
    "git_show": 48000,
    "git_log": 24000,
    # Skill bodies are user-curated procedures; the cap keeps a loaded skill
    # from crowding out the conversation (tool_load_skill also refuses over-cap
    # files outright, so compress never has to chop a procedure mid-step).
    "load_skill": SKILL_BODY_CAP_CHARS,
}


def _tool_result_cap(name: str) -> int:
    if name in _TOOL_RESULT_CAPS:
        return _TOOL_RESULT_CAPS[name]
    # Playwright aria snapshots run big even compressed, and chopping them
    # mid-tree sends models into a re-snapshot loop. 24K keeps the tree usable on
    # all but the largest pages.
    if name.startswith("mcp_playwright_"):
        return 24000
    return 16000 if name in _ANALYSIS_TOOL_NAMES else 4000


# Tools that persist to shared stores (chats.json, memories) via read-modify-
# write. Under parallel execution two of these in one batch would each load
# stale state and drop the other's update (e.g. pin_note + update_plan in the
# same round), so they serialize with each other within a batch.
_CHAT_STATE_TOOLS = frozenset(
    {"pin_note", "unpin_note", "update_plan", "remember", "forget",
     "edit_memory", "compact_history", "load_skill", "save_skill", "security_coverage", "record_finding"})
