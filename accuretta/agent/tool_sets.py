# SPDX-License-Identifier: AGPL-3.0-only
"""Which tools the model sees: the core sets per context size and mode, the bundles a
chat unlocks, red-team and review sets, and tools hidden when their runtime is missing.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import shutil
import subagents
from accuretta.agent.action_audit import _DESKTOP_TOOL_NAMES
from accuretta.agent.execution_target import _ANALYSIS_SUITE_TOOL_NAMES
from accuretta.agent.execution_target import _REMOTE_TOOL_NAMES
from accuretta.agent.execution_target import _execution_target_allows_tool
from accuretta.agent.execution_target import _ready_remote_machines
from accuretta.agent.execution_target import _selected_execution_target
from accuretta.agent.research_runs import _research_context
from accuretta.core.chats import get_chats
from accuretta.core.optional_imports import _HAVE_CAPSTONE
from accuretta.core.optional_imports import _HAVE_DPKT
from accuretta.core.optional_imports import _HAVE_EVTX_PY
from accuretta.core.optional_imports import _HAVE_EVTX_RUST
from accuretta.core.optional_imports import _HAVE_PIL
from accuretta.core.optional_imports import _HAVE_PLAYWRIGHT
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import _HAVE_PYGHIDRA
from accuretta.core.optional_imports import _HAVE_SQUASHFS
from accuretta.core.optional_imports import _HAVE_UIA
from accuretta.core.optional_imports import _HAVE_YARA
from accuretta.core.persistence import _normalize_composer_mode
from accuretta.core.settings import get_settings
from accuretta.core.tool_registry import TOOLS
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_mission_get
from accuretta.redteam.mission import _rt_passive_mission
from accuretta.redteam.network_scope import _RT_AUTOMATED_TOOL_NAMES
from accuretta.redteam.network_scope import _RT_BOUNTY_HIDDEN_TOOLS
from accuretta.redteam.network_scope import _RT_EXPLOIT_TOOL_NAMES
from accuretta.redteam.network_scope import _RT_PASSIVE_TOOL_NAMES
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.model_presets import _model_features
from accuretta.runtime.sandbox_state import _sandbox_ready_cached
from accuretta.runtime.vision_ready import _vision_observation_available
from accuretta.tools.apk import _resolve_jadx_bin
from accuretta.tools.remote_machines import _load_remote_machines
from accuretta.tools.ytdlp import _resolve_ytdlp_cmd
from deep_research import RESEARCH_TOOLS


# Off-brief for an access-control check: hidden (not refused) in that engagement
# so a small model stays on the two-account comparison.
_RT_ACCESS_HIDDEN_TOOLS = {
    "sql_injection", "batch_probe", "recon_auth_spray", "tcp_send",
    "sandbox_sqlmap", "recon_injection_probe",
}


_SECURITY_REVIEW_TOOLS = {"review_finding", "compare_evidence", "security_coverage"}


# Always-on recon/analysis set when red_team_enabled. Passive-to-benign probes,
# the finding validator, the Burp-Decoder encoder, and
# front-end/CORS scanners — the model needs these to FIND something worth the
# exploit set, so they carry every red-team turn.
_RT_RECON_TOOL_NAMES = {
    "recon_dns", "recon_subdomains", "recon_rdap", "recon_web_archive", "recon_shodan",
    "recon_tls_audit", "recon_http_fingerprint",
    "recon_port_scan", "recon_content_discovery", "recon_check_exposure",
    "recon_subdomain_takeover", "recon_cve_match", "recon_injection_probe",
    "recon_open_services", "recon_capture_evidence",
    # attack-surface mapper: crawl + JS endpoint mining -> ready-to-act inventory.
    "recon_map",
    # sandbox-backed scanners (guest does the noisy work) + report builder.
    "sandbox_nmap", "sandbox_sqlmap", "rt_generate_report",
    # Token encoder/decoder (Burp Decoder equivalent) — inspect/forge a token
    # without hand-computing base64. Benign on its own; stays in recon.
    "encode_decode",
    # Finding validator — the recon->exploit trigger; obviously always on.
    "validate_finding",
    # front-end secret hunting + CORS misconfig testing.
    "scan_js_secrets",
    "cors_probe",
}
# observe_response is recon too. It must join here, before the unions below are
# built from this set: left behind in bridge.py on 2026-11-24 it was added after
# them, and _RED_TEAM_TOOL_NAMES lost it (red-team gating skipped it).
_RT_RECON_TOOL_NAMES |= {"observe_response"}


_RT_PASSIVE_ALLOWED_TOOL_NAMES = _RT_PASSIVE_TOOL_NAMES | {
    "web_search",
    "record_finding", "list_findings", "update_plan",
    "pin_note", "unpin_note", "remember", "forget", "edit_memory", "search_memories",
    "compact_history", "capability_report",
} | _SECURITY_REVIEW_TOOLS | {"load_skill", "list_skills"}


# Full suite = recon + exploit. Existing call sites (whole-suite tool gating in
# _active_tools, mission-target detection at tool-result time) key off this
# union; the split above only drives per-phase gating.
_RED_TEAM_TOOL_NAMES = _RT_RECON_TOOL_NAMES | _RT_EXPLOIT_TOOL_NAMES


# Schemas dropped from the wire while an engagement is active: every tool a
# small model can see is one more it can pick by mistake. They stay registered,
# so a call that names one still runs.
_RT_MISSION_HIDDEN_TOOLS = {
    "check_syntax", "project_map", "save_skill", "verification_guidance", "run_tests",
    "review_finding", "git_status", "git_diff", "web_watch_video", "web_image_search",
}


_CORE_TOOL_NAMES = {
    # file / workspace (always needed)
    "read_file", "write_file", "edit_file", "delete_file", "list_directory",
    "find_files", "grep_files", "read_bytes", "file_inspect", "extract_archive",
    "project_map", "capability_report", "read_skeleton", "replace_ast_node", "find_references", "find_symbol",
    "check_syntax", "run_tests", "check_deps", "run_powershell", "run_python", "open_program",
    # planning / memory / probe
    "update_plan", "record_finding", "list_findings", "pin_note", "unpin_note", "remember", "forget", "edit_memory", "search_memories",
    "list_more_tools", "compact_history", "save_skill", "switch_execution_target",
    # web
    "web_search", "web_image_search", "web_fetch", "web_view_image", "web_render", "web_watch_video",
    # pictures for the user, inline in the chat
    "show_image",
    # interactive sessions
    "session_start", "session_send", "session_read", "session_stop", "session_list",
    # host visibility
    "network_snapshot", "parse_event_logs", "persistence_hunt",
    # HTTP header audit — ungated today; keeping it always-visible preserves
    # existing behavior (scope-guarding would change how it runs).
    "audit_http_headers",
    # git essentials (the rest of git lives in the "git" bundle)
    "git_status", "git_diff", "git_log", "git_branch",
}


_SMALL_CTX_CORE_TOOL_NAMES = {
    # The irreducible coding loop. On <=16K windows the former 44-tool core
    # consumed most of the context before the first user token arrived.
    "project_map", "capability_report", "read_file", "write_file", "edit_file", "list_directory",
    "find_files", "grep_files", "check_syntax", "run_tests", "run_powershell", "run_python",
    "update_plan", "record_finding", "list_findings", "pin_note", "unpin_note", "list_more_tools",
    "compact_history", "save_skill", "switch_execution_target", "git_status", "git_diff", "search_memories",
}


_TINY_CTX_CORE_TOOL_NAMES = {
    "project_map", "read_file", "write_file", "edit_file", "list_directory",
    "grep_files", "check_syntax", "run_powershell", "update_plan",
    "list_more_tools", "compact_history", "save_skill", "switch_execution_target", "search_memories",
}


# Balanced lean default for ALL ctx sizes (model-agnostic starting point).
# Full descriptions kept — fewer visible tools, not shorter ones. Everything
# removed here stays loadable via list_more_tools bundles or RT auto-load.
_BALANCED_CORE_TOOL_NAMES = {
    "project_map", "capability_report", "read_file", "write_file", "edit_file",
    "list_directory", "find_files", "grep_files", "check_syntax", "run_tests",
    "run_powershell", "run_python", "update_plan", "pin_note", "unpin_note", "list_more_tools",
    "compact_history", "save_skill", "switch_execution_target", "git_status",
    "git_diff", "search_memories", "web_search", "web_fetch", "web_view_image", "web_render", "web_watch_video",
    "show_image",
}


# IDE mode: file ops still needed for case-(C) saves, but no sessions/host/
# analysis/RT. Bare ```html``` fence stays the default via the IDE prompt.
_IDE_CORE_TOOL_NAMES = {
    "project_map", "read_file", "write_file", "edit_file", "list_directory",
    "find_files", "grep_files", "check_syntax", "run_tests", "run_powershell",
    "update_plan", "list_more_tools", "compact_history", "save_skill",
    "git_status", "git_diff", "search_memories",
}


# None = dynamic (all TOOLS keys with the given prefix, resolved at call time).
_TOOL_BUNDLES: dict[str, set[str] | None] = {
    "workspace-extra": {
        "show_image",
        "delete_file", "read_bytes", "file_inspect", "extract_archive",
        "read_skeleton", "replace_ast_node", "find_references", "find_symbol",
        "find_files", "check_deps", "run_tests", "open_program",
    },
    "web": {"web_search", "web_image_search", "web_fetch", "web_view_image", "web_render", "web_watch_video", "audit_http_headers"},
    "memory": {"remember", "forget", "edit_memory", "search_memories", "pin_note", "unpin_note"},
    "sessions": {"session_start", "session_send", "session_read", "session_stop", "session_list"},
    "host": {"network_snapshot", "parse_event_logs", "persistence_hunt"},
    "git": {
        "git_status", "git_diff", "git_log", "git_branch", "git_show", "git_remote",
        "git_add", "git_commit", "git_push",
        "git_pull", "git_fetch", "git_checkout", "git_restore", "git_reset",
        "git_init", "git_clone",
    },
    "desktop": _DESKTOP_TOOL_NAMES,
    "rt-recon": _RT_RECON_TOOL_NAMES,
    "rt-exploit": _RT_EXPLOIT_TOOL_NAMES,
    "analysis": _ANALYSIS_SUITE_TOOL_NAMES,
    "findings": {"record_finding", "list_findings"},
    "sandbox": {"sandbox_run"},
    "remote": _REMOTE_TOOL_NAMES,
    "mcp": None,  # dynamic prefix: mcp_<server>_<tool>
}


_unlocked_bundles_by_chat: dict[str, set[str]] = {}


def _base_core_tool_names(chat_id: str = "") -> set[str]:
    """Choose a schema core that leaves actual room for the conversation.

    Model-agnostic: lean balanced core is the default at any ctx (static cost
    stays <20% even on 8k windows); full legacy core is opt-in via
    tool_verbosity=full. IDE mode gets its own smaller core. Tiny/small tiers
    still guard very small ctx windows regardless of verbosity.
    """
    try:
        settings = get_settings()
    except Exception:
        settings = {}
    try:
        ctx = _llama_props_ctx() or int(settings.get("num_ctx") or 32768)
    except Exception:
        ctx = 32768
    if ctx <= 8192:
        return _TINY_CTX_CORE_TOOL_NAMES
    if ctx <= 16384:
        return _SMALL_CTX_CORE_TOOL_NAMES
    if (settings.get("tool_verbosity") or "lean") == "full":
        return _CORE_TOOL_NAMES
    # Mode-aware: IDE turns never need sessions/host/RT/analysis schemas.
    try:
        mode = ""
        if chat_id:
            chat = (get_chats().get("chats", {}) or {}).get(chat_id)
            if isinstance(chat, dict) and chat.get("last_mode"):
                mode = _normalize_composer_mode(chat.get("last_mode"))
        if not mode:
            mode = _normalize_composer_mode(settings.get("composer_mode") or "agent")
    except Exception:
        mode = "agent"
    if mode == "ide":
        return _IDE_CORE_TOOL_NAMES
    return _BALANCED_CORE_TOOL_NAMES


def _unavailable_runtime_tools() -> dict[str, str]:
    """Strict dependency failures that would make a tool fail every call."""
    missing: dict[str, str] = {}
    if not _HAVE_PYAUTOGUI:
        for n in ("desktop_click", "desktop_type_text", "desktop_press_keys", "desktop_drag"):
            missing[n] = "raw mouse/keyboard fallback requires pyautogui"
    if not _HAVE_PYAUTOGUI or not _HAVE_PIL:
        for n in ("screenshot", "describe_screen"):
            missing[n] = "screen capture requires pyautogui and Pillow"
    elif not _vision_observation_available():
        # screenshot only saves a file now; looking at it is what needs vision.
        for n in ("describe_screen", "view_image"):
            missing[n] = "no vision backend is loaded; use desktop_snapshot"
    if not _HAVE_PIL:
        missing["view_image"] = "image viewing requires Pillow"
    if not _HAVE_PGW:
        for n in ("list_windows", "desktop_focus_window", "desktop_close_window", "desktop_open_url"):
            missing[n] = "window control requires pygetwindow"
    if not _HAVE_UIA:
        for n in ("desktop_snapshot", "desktop_invoke", "desktop_set_value", "desktop_scroll"):
            missing[n] = "blind semantic control requires uiautomation on Windows"
    if not _HAVE_PLAYWRIGHT:
        missing["rt_browser"] = "requires playwright and its Chromium browser"
        missing["web_render"] = "requires playwright and its Chromium browser"
    if not _resolve_ytdlp_cmd() or not shutil.which("ffmpeg"):
        missing["web_watch_video"] = "requires yt-dlp (pip install yt-dlp) and ffmpeg on PATH"
    if not _HAVE_SQUASHFS:
        missing["extract_squashfs"] = "requires PySquashfsImage"
    if not _HAVE_CAPSTONE:
        missing["disasm_at"] = "requires capstone"
    if not _resolve_jadx_bin():
        missing["decompile_apk"] = "requires JADX"
    if not _HAVE_PYGHIDRA:
        missing["ghidra_analyze"] = "requires pyghidra and Ghidra/JDK"
    if not _HAVE_YARA:
        missing["yara_scan"] = "requires yara-python"
    if not (_HAVE_EVTX_RUST or _HAVE_EVTX_PY):
        missing["parse_evtx"] = "requires evtx or python-evtx"
    if not _HAVE_DPKT:
        missing["analyze_pcap"] = "requires dpkt"
    return missing


def _excluded_tools(include_exploit: bool = True, chat_id: str = "") -> set[str]:
    """The tool names hidden by CURRENT settings — the single source of truth
    for both _active_tools and the lazy-bundle visibility filter, so the two
    can never drift apart."""
    s = get_settings()
    excluded: set[str] = set()
    if "subagents" not in _model_features(str(s.get("model_path") or "")):
        excluded.add(subagents.SPAWN_TOOL)
    if not s.get("desktop_enabled"):
        excluded |= _DESKTOP_TOOL_NAMES
    if not s.get("red_team_enabled"):
        excluded |= _RED_TEAM_TOOL_NAMES
    else:
        chat = None
        if chat_id:
            try:
                chat = (get_chats().get("chats", {}) or {}).get(chat_id)
            except Exception:
                chat = None
        # The global toggle makes the capability available; a fresh per-chat
        # UI authorization record makes the red-team schemas visible.
        if not _rt_authorized_mission(chat):
            excluded |= _RED_TEAM_TOOL_NAMES
        elif _rt_passive_mission(chat):
            excluded |= set(TOOLS) - _RT_PASSIVE_ALLOWED_TOOL_NAMES
        elif not include_exploit:
            excluded |= _RT_EXPLOIT_TOOL_NAMES
        # A program or client that bans automated testing loses the scanner
        # and fuzzer schemas entirely — pruning beats a runtime refusal, which
        # small models treat as a loop invitation.
        _mission = _rt_mission_get(chat) if isinstance(chat, dict) else None
        if _mission and _mission.get("automated_allowed") is False:
            excluded |= _RT_AUTOMATED_TOOL_NAMES
        # The account comparison only works with stored test accounts, and an
        # access-control run does not carry the injection and spray tools.
        if not (_mission and _mission.get("identities")):
            excluded.add("rt_access_check")
        if _mission and _mission.get("engagement") == "access_control":
            excluded |= _RT_ACCESS_HIDDEN_TOOLS
        if _mission and _mission.get("engagement") == "bug_bounty":
            excluded |= _RT_BOUNTY_HIDDEN_TOOLS
    if not s.get("analysis_tools_enabled"):
        excluded |= _ANALYSIS_SUITE_TOOL_NAMES
    if (not s.get("remote_access_enabled", True)
            or not any(m.get("status") == "ready" and m.get("enabled", True)
                       for m in _load_remote_machines().get("machines", []))):
        excluded |= _REMOTE_TOOL_NAMES
    # The sandbox tool is only useful once the guest is provisioned; don't pay
    # its token cost (or tempt the model to call it) before then.
    if not _sandbox_ready_cached():
        excluded.add("sandbox_run")
    # Do not spend scarce context advertising tools which are guaranteed to
    # fail on this machine. capability_report remains visible and explains
    # exactly what is missing and how to restore the capability.
    excluded |= set(_unavailable_runtime_tools())
    return excluded


def _visible_tool_names(include_exploit: bool = True, chat_id: str = "") -> set[str]:
    """Core + this chat's unlocked bundles, minus settings-gated tools and any
    name that isn't actually registered (MCP tools disappear when their server
    dies between unlock and request)."""
    excluded = _excluded_tools(include_exploit, chat_id)
    if _research_context(chat_id)[1]:
        return {name for name in RESEARCH_TOOLS | {subagents.SPAWN_TOOL}
                if name in TOOLS and name not in excluded
                and _execution_target_allows_tool(name, chat_id)}
    visible: set[str] = set(_base_core_tool_names(chat_id))
    visible.add("verification_guidance")
    if subagents.SPAWN_TOOL not in excluded:
        visible.add(subagents.SPAWN_TOOL)
    if get_settings().get("red_team_enabled") and chat_id:
        if _rt_authorized_mission((get_chats().get("chats", {}) or {}).get(chat_id)):
            visible |= _SECURITY_REVIEW_TOOLS
            # Findings ledger is RT-only in lean mode; auto-load so missions
            # never lose record_finding to the lazy bundle.
            visible |= {"record_finding", "list_findings"}
    active_rt = False
    if chat_id:
        try:
            _chat = (get_chats().get("chats", {}) or {}).get(chat_id)
        except Exception:
            _chat = None
        # An authorized gate launch is itself the request to use the suite:
        # auto-load discovery tools for any mission and active-testing tools for Red
        # Team mode, rather than relying on a small model to find the bundle loader.
        active_rt = bool(_rt_authorized_mission(_chat))
        if active_rt:
            if _rt_passive_mission(_chat):
                visible |= _RT_PASSIVE_TOOL_NAMES | {"web_search"}
            else:
                visible |= _RT_RECON_TOOL_NAMES
            if include_exploit and not _rt_passive_mission(_chat):
                visible |= _RT_EXPLOIT_TOOL_NAMES
        for b in _unlocked_bundles_by_chat.get(chat_id, ()):
            btools = _TOOL_BUNDLES.get(b)
            if btools is None:
                if b == "mcp":
                    visible |= {n for n in TOOLS if n.startswith("mcp_")}
                continue
            visible |= btools
    if active_rt:
        visible -= _DESKTOP_TOOL_NAMES
        # Coding helpers the engagement never uses, unless this chat loaded a
        # bundle that carries one. review_finding is folded into record_finding.
        _kept = set()
        for b in _unlocked_bundles_by_chat.get(chat_id, ()):
            _kept |= _TOOL_BUNDLES.get(b) or set()
        _hide = set(_RT_MISSION_HIDDEN_TOOLS)
        if not _ready_remote_machines():
            _hide.add("switch_execution_target")
        visible -= _hide - _kept
        # Only the playwright MCP server stays visible during a mission: its
        # calls are URL-filtered at the wrapper (same gate as rt_browser),
        # which is what makes a third-party browser safe to extend the
        # first-party worker. Every other MCP connector remains hidden.
        visible = {n for n in visible
                   if not (n.startswith("mcp_") and not n.startswith("mcp_playwright_"))}
    # Execution target is a backend capability boundary. Project only the
    # selected device's file/shell surface instead of relying on prose routing.
    visible = {n for n in visible if _execution_target_allows_tool(n, chat_id)}
    if _selected_execution_target(chat_id) != "host":
        visible |= _REMOTE_TOOL_NAMES
    # The system prompt advertises the guest whenever it is ready. Keep the
    # actual schema in lockstep so the model never substitutes host PowerShell
    # merely because the sandbox bundle was still lazy-hidden.
    if "sandbox_run" in TOOLS and "sandbox_run" not in excluded:
        visible.add("sandbox_run")
    return {n for n in visible if n in TOOLS and n not in excluded}
