# SPDX-License-Identifier: AGPL-3.0-only
"""The web server: every /api route, static files with their MIME types, the request
token, preview sandboxing, and the threaded HTTP server.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import gzip
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from accuretta.agent.chat_index import _chat_task_state
from accuretta.agent.chat_index import _enforce_chat_retention
from accuretta.agent.chat_index import _title_from_prompt
from accuretta.agent.chat_index import get_chat_index
from accuretta.agent.chat_loop import run_chat_turn
from accuretta.agent.context_estimate import _estimate_context_tokens
from accuretta.agent.context_estimate import _is_gemma_model
from accuretta.agent.context_estimate import _security_investigation_prompt
from accuretta.agent.file_tools import _tool_write_history
from accuretta.agent.goal_mode import _apply_goal_latch
from accuretta.agent.goal_mode import _goal_summary
from accuretta.agent.goal_mode import _goal_time_limit
from accuretta.agent.goal_mode import _parse_goal_command
from accuretta.agent.goal_mode import _set_goal_mode
from accuretta.agent.history_fold import _compact_chat
from accuretta.agent.history_fold import _maybe_roll_summary
from accuretta.agent.history_fold import _summary_failure_backoff_active
from accuretta.agent.history_fold import _summary_failure_seq_by_chat
from accuretta.agent.history_fold import _summary_trigger_limit
from accuretta.agent.history_fold import list_inflight_folds
from accuretta.agent.html_extract import extract_html
from accuretta.agent.mcp_servers import _mcp_delete_server
from accuretta.agent.mcp_servers import _mcp_import
from accuretta.agent.mcp_servers import _mcp_list_servers
from accuretta.agent.mcp_servers import _mcp_restart_server
from accuretta.agent.mcp_servers import _mcp_save_server
from accuretta.agent.native_tools import _native_tools_broken
from accuretta.agent.native_tools import _text_tools_prompt
from accuretta.agent.native_tools import _warmup_tool_grammar
from accuretta.agent.research_runs import _research_runs
from accuretta.agent.research_runs import _research_store
from accuretta.agent.rolling_summary import _log_compact
from accuretta.agent.rt_program_reader import _rt_read_program
from accuretta.agent.skill_load import tool_load_skill
from accuretta.agent.skills import _skills_catalog
from accuretta.agent.system_prompt import SYSTEM_CONTEXT_FILE
from accuretta.agent.system_prompt import _client_context_prompt
from accuretta.agent.system_prompt import build_system_prompt
from accuretta.agent.system_prompt import ensure_system_context
from accuretta.agent.system_prompt import rescan_system_context
from accuretta.agent.tool_bundles import _TOOL_BUNDLE_LOCK
from accuretta.agent.tool_bundles import _auto_unlock_bundles
from accuretta.agent.tool_bundles import _unlock_bundle
from accuretta.agent.tool_dispatch import invoke_tool
from accuretta.agent.tool_history import _tool_call_history
from accuretta.agent.tool_history import _tool_family_history
from accuretta.agent.tool_history import _tool_history_lock
from accuretta.agent.tool_history import _tool_url_history
from accuretta.agent.tool_sets import _unlocked_bundles_by_chat
from accuretta.agent.turn_journal import _ACTIVITY_MAX
from accuretta.agent.turn_journal import _recover_turn_journals
from accuretta.agent.turn_journal import _refresh_continuity_state
from accuretta.agent.turn_journal import _turn_journal_clear
from accuretta.agent.turn_notes import _COMPACTION_STOP_MESSAGE
from accuretta.agent.turn_notes import _maya_helper_followup
from accuretta.agent.turn_records import _chat_note
from accuretta.agent.turn_records import _last_dropped_by_chat
from accuretta.agent.turn_records import _turn_model_label
from accuretta.agent.turn_records import _update_task_anchor
from accuretta.agent.usage_stats import _usage_stats
from accuretta.agent.user_anchor import _ensure_user_anchor
from accuretta.agent.verification import _set_current_chat
from accuretta.agent.wire_replay import _clamp_image_data_url
from accuretta.agent.wire_replay import _replay_wire_msg
from accuretta.agent.workbench_status import _workbench_status
from accuretta.agent.workbench_status import _workspace_git_status
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import get_chats
from accuretta.core.command_cwd import _project_workspace_metadata
from accuretta.core.effort import _normalize_effort_choice
from accuretta.core.endpoints import LLAMA
from accuretta.core.errors import _error_store
from accuretta.core.events import broadcast_event
from accuretta.core.events import replay_events_since
from accuretta.core.events import subscribe
from accuretta.core.events import unsubscribe
from accuretta.core.github_names import _GITHUB_OWNER_RE
from accuretta.core.github_names import _GITHUB_REPO_RE
from accuretta.core.github_names import _github_valid_branch_name
from accuretta.core.github_names import _github_worktree_metadata
from accuretta.core.ignore_rules import resolve_workspace_file
from accuretta.core.memories import MEMORIES_FILE
from accuretta.core.memories import _memory_store
from accuretta.core.optional_imports import _HAVE_PIL
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import _HAVE_UIA
from accuretta.core.paths import CHATS_FILE
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import _normalize_composer_mode
from accuretta.core.persistence import save_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.safe_fs import safe_rglob_files
from accuretta.core.savings import _savings_get
from accuretta.core.settings import _normalized_mmproj_mode
from accuretta.core.settings import _offline_active
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.core.storage import _cmd_history_lock
from accuretta.core.storage import storage_clear
from accuretta.core.storage import storage_report
from accuretta.core.tool_pool import _tool_executor
from accuretta.core.tool_pool import _tool_jobs
from accuretta.core.tool_pool import _tool_jobs_lock
from accuretta.core.tool_stream import CMD_HISTORY_FILE
from accuretta.core.tool_stream import CMD_HISTORY_MAX
from accuretta.core.tool_stream import _read_cmd_history
from accuretta.core.undo import _undo_commit_turn
from accuretta.core.undo import _undo_restore
from accuretta.core.undo import _undo_store
from accuretta.core.updates import app_update_status
from accuretta.core.versions import SNAPSHOTS_DIR
from accuretta.core.versions import VERSIONS_DIR
from accuretta.core.versions import list_versions
from accuretta.core.versions import read_version
from accuretta.core.versions import save_version
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.workspace import WORKSPACE_FILE
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.redteam.identities import _rt_identities_usable
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission_flow import _rt_mission_seed
from accuretta.redteam.mission_panel import _rt_mission_apply_panel
from accuretta.runtime.auto_tune import _tune_budget_gb
from accuretta.runtime.auto_tune import auto_tune
from accuretta.runtime.command_kill import _kill_current_command
from accuretta.runtime.command_kill import cancel_chat
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.gpu_detect import detect_hardware_specs
from accuretta.runtime.gpu_detect import detect_vram_gb
from accuretta.runtime.llama_bin import find_llama_bin
from accuretta.runtime.llama_bin import llama_knows_arch
from accuretta.runtime.llama_download import DOWNLOAD_LOCK
from accuretta.runtime.llama_download import DOWNLOAD_STATE
from accuretta.runtime.llama_download import download_and_extract_llama
from accuretta.runtime.llama_update import llama_update_status
from accuretta.runtime.llama_update import start_llama_update
from accuretta.runtime.maya_install import maya_install_status
from accuretta.runtime.maya_install import start_maya_install
from accuretta.runtime.llama_http import llama_ping
from accuretta.runtime.llama_http import llama_ready
from accuretta.runtime.llama_http import recommended_settings
from accuretta.runtime.llama_post import llama_post
from accuretta.runtime.llama_process import _CTX_EST_CACHE
from accuretta.runtime.llama_process import _last_prompt_tokens_by_chat
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.maya import _maya_drafter_for
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.maya_find import _maya_only_model
from accuretta.runtime.maya_find import find_maya_engine
from accuretta.runtime.maya_sizing import maya_needed_message
from accuretta.runtime.model_config import MODELS_CONFIG_FILE
from accuretta.runtime.model_config import _MODEL_PROJECTOR_KEYS
from accuretta.runtime.model_config import _MODEL_TUNE_KEYS
from accuretta.runtime.model_config import _models_config
from accuretta.runtime.model_config import _proven_cfg
from accuretta.runtime.model_config import _save_model_config
from accuretta.runtime.model_display import model_display_info_for
from accuretta.runtime.model_files import _is_mmproj_name
from accuretta.runtime.model_files import _spec_fingerprint
from accuretta.runtime.model_files import find_all_gguf_files
from accuretta.runtime.model_files import find_mmproj_for
from accuretta.runtime.model_files import find_spec_draft_for
from accuretta.runtime.model_files import list_spec_drafts
from accuretta.runtime.model_files import scan_gguf_dir
from accuretta.runtime.model_files import spec_draft_match
from accuretta.runtime.model_folders import suggest_models_dir
from accuretta.runtime.model_health import _model_health_action
from accuretta.runtime.model_health_report import _public_model_health
from accuretta.runtime.model_inspect import inspect_model
from accuretta.runtime.model_observation import _client_context_by_chat
from accuretta.runtime.model_sampling import _SWITCH_RESTORE_KEYS
from accuretta.runtime.model_sampling import _apply_model_sampling
from accuretta.runtime.model_sampling import _auto_tune_for_load
from accuretta.runtime.model_sampling import _preserve_manual_model_spec
from accuretta.runtime.model_sampling import draft_match_payload
from accuretta.runtime.model_sampling import llama_too_old_message
from accuretta.runtime.notch_process import notch_sync
from accuretta.runtime.reasoning_capability import _reasoning_capability
from accuretta.runtime.sandbox_rootfs import _sbx_set
from accuretta.runtime.sandbox_setup import sandbox_selftest
from accuretta.runtime.sandbox_setup import start_sandbox_setup
from accuretta.runtime.sandbox_state import SANDBOX_STATE_FILE
from accuretta.runtime.sandbox_state import wsl_probe
from accuretta.runtime.settings_update import _SETTINGS_LOCK
from accuretta.runtime.settings_update import update_settings
from accuretta.runtime.vision_ready import _separate_vision_server_ready
from accuretta.runtime.vision_ready import _vision_observation_available
from accuretta.runtime.vision_server import _start_vision_fallback_if_needed
from accuretta.runtime.vision_server import _vision_llama
from accuretta.runtime.wsl_sandbox import SANDBOX_DISTRO
from accuretta.runtime.wsl_sandbox import _wsl_run
from accuretta.security.approval_list import list_approvals
from accuretta.security.approvals import _approvals
from accuretta.security.approvals import _approvals_lock
from accuretta.security.approvals import _chat_in_goal_mode
from accuretta.security.approvals import _steering
from accuretta.security.approvals import decide_approval
from accuretta.security.desktop_intent import _user_asked_desktop_interaction
from accuretta.security.registry_writes import _notch_registry_diff
from accuretta.security.user_words import _note_user_words
from accuretta.tools.desktop_guard import _chat_desktop_disabled
from accuretta.tools.desktop_guard import _desktop_panic
from accuretta.tools.discord_approvals import _presence_notify
from accuretta.tools.email_settings import email_settings
from accuretta.tools.email_settings import email_test_connection
from accuretta.tools.email_settings import update_email_settings
from accuretta.tools.folders import _list_subfolders
from accuretta.tools.folders import _open_folder_in_file_manager
from accuretta.tools.folders import _path_in_workspace
from accuretta.tools.github_account import _github_activity
from accuretta.tools.github_account import _github_cred
from accuretta.tools.github_account import _github_repositories
from accuretta.tools.github_account import _github_save_cred
from accuretta.tools.github_projects import _github_branch_flow
from accuretta.tools.github_projects import _github_create_project
from accuretta.tools.github_projects import _github_rest
from accuretta.tools.memory_tools import _load_memories
from accuretta.tools.memory_tools import _save_memories
from accuretta.tools.memory_tools import tool_edit_memory
from accuretta.tools.memory_tools import tool_forget
from accuretta.tools.memory_tools import tool_remember
from accuretta.tools.remote_helpers import _public_remote_machine
from accuretta.tools.remote_machines import _handler_client_context
from accuretta.tools.remote_machines import _load_remote_machines
from accuretta.tools.remote_pairing import _ssh_keygen_exe
from accuretta.tools.remote_pairing import _tailscale_serve_status
from accuretta.tools.remote_pairing import _tailscale_snapshot
from accuretta.tools.remote_pairing import enable_tailscale_serve
from accuretta.tools.remote_pairing import prepare_remote_pairing
from accuretta.tools.remote_pairing import remove_remote_pairing
from accuretta.tools.remote_ssh import _ssh_exe
from accuretta.tools.remote_ssh import verify_remote_pairing
from accuretta.tools.remote_tools import _remote_payload_limit
from accuretta.tools.security_watch import _security_apply_schedule
from accuretta.tools.security_watch import _security_collect_processes
from accuretta.tools.security_watch import _security_overview
from accuretta.tools.sessions import _session_mgr
from accuretta.tools.show_image import CHAT_MEDIA_MIME, _shown_images, chat_media_file
from accuretta.tools.vision import describe_image
from accuretta.tools.web_fetch import fetch_link_preview
from agent_reliability import EvidenceStore
from conversation_tools import search_conversations
from deep_research import RESEARCH_PROMPT
from deep_research import brief as research_brief
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from memory_store import memory_kind
from pathlib import Path
from queue import Empty
from typing import Any


_shutdown_ready = threading.Event()


# MIME map for /api/wsfs/. Anything unlisted is served as
# application/octet-stream, which the browser downloads instead of rendering.
_WS_FILE_MIME = {
    ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2",
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".txt":  "text/plain; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".xml":  "application/xml; charset=utf-8",
    ".map":  "application/json; charset=utf-8",
}


# Cap workspace-served file size — keeps a 4GB log from being streamed to a
# browser tab, OOMing both the bridge and the renderer.
_WS_FILE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def _web_ui_focused() -> bool:
    """Whether any fresh web client (the main app window) currently has focus.

    chat_end carries this so the notch knows whether a finished turn's reply
    has a human in front of it in the app. Unknown counts as watched — the
    auto pop-out must never fire on a probe failure."""
    try:
        return bool(_presence_notify.presence().get("web_focused_fresh"))
    except Exception:
        return True


MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".jsx": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".webmanifest": "application/manifest+json",
}


# app.js sections that live in their own file (ui-<name>.js). Each is served like
# app.js (whitelisted, never cached) and has a <script> tag before app.js in
# index.html; tests/test_ui_sections.py checks that the three agree.
UI_SECTION_FILES = frozenset({
    "ui-terminal-jump.js",
    "ui-mcp-quick-look.js",
    "ui-sidebar.js",
    "ui-composer-controls.js",
    "ui-explorer-tools.js",
    "ui-status-bar.js",
    "ui-composer-context.js",
    "ui-wb-git.js",
    "ui-wb-viewer.js",
    "ui-file-index.js",
    "ui-file-refs.js",
    "ui-replies.js",
    "ui-sidebar-groups.js",
    "ui-session-tabs.js",
    "ui-wb-actions.js",
    "ui-shortcuts.js",
    "ui-small-formatters.js",
    "ui-workbench.js",
    "ui-mention-menu.js",
    "ui-event-wiring.js",
    "ui-mobile-tabs.js",
    "ui-model-menu.js",
    "ui-mobile-preview.js",
    "ui-usage-stats.js",
    "ui-savings-card.js",
    "ui-savings-widget.js",
    "ui-shell-tab.js",
    "ui-draft-picker.js",
    "ui-cmd-history.js",
    "ui-sandbox.js",
    "ui-file-tree.js",
    "ui-preview-tools.js",
    "ui-console-forwarder.js",
    "ui-multi-file.js",
    "ui-versions.js",
    "ui-security-overview.js",
    "ui-task-progress.js",
    "ui-change-rollup.js",
    "ui-streaming-fade.js",
    "ui-finish-batch.js",
    "ui-reveal-pacing.js",
    "ui-paint-pipeline.js",
    "ui-osint-card.js",
    "ui-recon-exploit.js",
    "ui-exposure-rail.js",
    "ui-assessment-progress.js",
    "ui-send-stream.js",
    "ui-attachments.js",
    "ui-desktop-kill-switch.js",
    "ui-chat.js",
    "ui-data-loading.js",
    "ui-bootstrap.js",
    "ui-hover-popover.js",
    "ui-autolinker.js",
    "ui-markdown.js",
    "ui-stream-helpers.js",
    "ui-email-cards.js",
    "ui-diff-render.js",
    "ui-highlighter.js",
    "ui-tool-card.js",
    "ui-tool-labels.js",
    "ui-tool-icons.js",
    "ui-presence.js",
    "ui-render-helpers.js",
    "ui-llama-update.js",
    "ui-maya-install.js",
    "ui-effort-stream.js",
    "ui-ctx-breakdown.js",
    "ui-live-events.js",
    "ui-notifications.js",
    "ui-utilities.js",
    "ui-state.js",
    "ui-viewport-presets.js",
    "ui-mcp-servers.js",
    "ui-backend-log.js",
    "ui-memories-panel.js",
    "ui-vram-tune.js",
    "ui-settings.js",
    "ui-file-actions.js",
    "ui-github-activity.js",
    "ui-command-palette.js",
    "ui-cost-widget-init.js",
    "ui-turn-changes.js",
})


STATIC_WHITELIST = {
    *UI_SECTION_FILES,
    "bridge-client.js", "preview-runtime.js",
    "index.html", "app.js", "appearance.js", "app.css", "workspace-shell.css", "colors_and_type.css", "glyph-field.js",
    "research-ui.js", "research-ui.css", "dropdown-menus.js",
    "security-scan-field.js", "rt-visuals.js", "agent-orb.js", "ui-refresh.css", "ui-pass.css",
    "dot-matrix.js", "workbench.css", "opal.css", "showcase.css", "showcase-field.js", "sound-cues.js",
    # notch overlay: the island UI (served unchanged) + its bridge wiring
    "accuretta-notch.html", "notch-wire.js", "notch-markdown.js",
    # brand assets — see index.html <link rel="..."> tags
    "assets/brand/logo-mark-dark.png", "assets/brand/logo-mark-light.png",
    "assets/icons/favicon.png", "assets/icons/favicon-32.png",
    "assets/icons/apple-touch-icon.png",
    "assets/icons/app-icon-192.png", "assets/icons/app-icon-512.png",
    "assets/reactions/celebrate.png", "assets/reactions/cool.png",
    "assets/reactions/frustrated.png", "assets/reactions/happy.png",
    "assets/reactions/like.png", "assets/reactions/tired.png",
    "manifest.webmanifest",
}


_REQUEST_TOKEN = secrets.token_urlsafe(32)


_MAX_JSON_BYTES = 32 * 1024 * 1024


_PREVIEW_SANDBOX = "sandbox allow-scripts allow-forms allow-modals allow-popups; object-src 'none'"


class RequestError(ValueError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def _json_default(value: Any):
    """Last-resort encoder for API payloads. Sets (GGUF identity fingerprints)
    are the common case; anything else falls back to its string form so one odd
    field can never turn a whole endpoint into a 500."""
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "Accuretta/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- helpers

    def _set_cors(self):
        # Same-origin only: the UI is served by this bridge and needs no CORS.
        # Access-Control-Allow-Origin:* would let any website read the agent's
        # responses or drive it from the user's browser.
        return

    def _host_ok(self) -> bool:
        # DNS-rebinding defense. Legitimate access is via localhost or an IP literal
        # (LAN or Tailscale); a Host header that is a domain name is the signature of
        # a rebinding attack aimed at this port.
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True
        if host.startswith("["):                       # IPv6, e.g. [::1]:8787
            hostname = host[1:host.find("]")] if "]" in host else host[1:]
        else:
            hostname = host.split(":", 1)[0]
        hostname = hostname.strip().lower()
        # localhost + Tailscale MagicDNS names (….ts.net is Tailscale-owned, not
        # publicly registrable, so it can't be used to rebind at your machine).
        if hostname == "localhost" or hostname.endswith(".ts.net"):
            return True
        import ipaddress
        try:
            ipaddress.ip_address(hostname)             # any IP literal is fine
            return True
        except ValueError:
            return False

    def _peer_ok(self) -> bool:
        # Only this machine (loopback) or the user's tailnet (100.64.0.0/10 and
        # fd7a:115c:a1e0::/48) may connect; everyone else on the local network is
        # refused. Tailscale already proves the device is the user's, so no password
        # is needed. client_address is the real TCP peer and can't be forged.
        import ipaddress
        try:
            ip = ipaddress.ip_address((self.client_address or ("",))[0])
        except (ValueError, IndexError, TypeError):
            return False
        if ip.is_loopback:
            return True
        for cidr in ("100.64.0.0/10", "fd7a:115c:a1e0::/48"):
            try:
                if ip in ipaddress.ip_network(cidr):
                    return True
            except ValueError:
                pass
        return False

    def _peer_is_loopback(self) -> bool:
        import ipaddress
        try:
            return ipaddress.ip_address((self.client_address or ("",))[0]).is_loopback
        except (ValueError, IndexError, TypeError):
            return False

    def _request_ok(self) -> bool:
        if not (self._peer_ok() and self._host_ok()):
            return False
        path = urllib.parse.urlsplit(self.path).path
        preview = path.startswith(("/api/wsfs/", "/api/snapshots/")) or (
            path.startswith("/api/versions/") and len(path.split("/")) == 5)
        origin = self.headers.get("Origin")
        fetch_site = self.headers.get("Sec-Fetch-Site", "")
        if self.command == "GET" and (preview or path.startswith("/assets/vendor/") or path == "/preview-runtime.js"):
            return True
        if fetch_site in {"cross-site", "same-site"}:
            return False
        if origin:
            parsed = urllib.parse.urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != self.headers.get("Host", "").lower():
                return False
        if path.startswith("/api/") and self.command not in {"GET", "HEAD", "OPTIONS"}:
            supplied = self.headers.get("X-Accuretta-Token", "")
            if not secrets.compare_digest(supplied, _REQUEST_TOKEN):
                return False
        return True

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if _offline_active:
            self.send_header("Content-Security-Policy", "default-src 'self' data: blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-src 'self' data: blob:")
        super().end_headers()

    def _send_json(self, status: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8")
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        encoded = accepts_gzip and len(body) >= 1024
        if encoded:
            body = gzip.compress(body, compresslevel=5)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        # API payloads carry conversations, approvals and settings. Without an
        # explicit directive Chromium heuristically caches GET JSON in the WebView2
        # profile on disk (full chat histories turned up in its Cache), so never
        # store them.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self._set_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_bytes(self, status: int, data: bytes, ctype: str, extra_headers: dict | None = None):
        headers = dict(extra_headers or {})
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/") and ("text/html" in ctype or "image/svg+xml" in ctype or "application/xhtml+xml" in ctype):
            headers["Content-Security-Policy"] = _PREVIEW_SANDBOX
            if "text/html" in ctype and self.headers.get("Sec-Fetch-Dest") in {"iframe", "document"}:
                runtime = b'<script src="/preview-runtime.js"></script>'
                if re.search(br"<head\b[^>]*>", data, re.I):
                    data = re.sub(br"<head\b[^>]*>", lambda match: match[0] + runtime, data, count=1, flags=re.I)
                else:
                    data = runtime + data
        elif path in {"/", "/index.html"}:
            headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
            headers["X-Frame-Options"] = "DENY"
        cache_control = headers.pop("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        if "no-cache" in cache_control or "no-store" in cache_control:
            self.send_header("Pragma", "no-cache")
        for k, v in headers.items():
            self.send_header(k, v)
        self._set_cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _read_json(self) -> dict:
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Content-Type must be application/json")
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "Transfer-Encoding is not supported")
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise RequestError(400, "Invalid Content-Length")
        if ln < 0 or ln > _MAX_JSON_BYTES:
            raise RequestError(413, "JSON request is too large")
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise RequestError(400, "Invalid JSON")
        if not isinstance(value, dict):
            raise RequestError(400, "JSON body must be an object")
        return value

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    # ---- dispatch

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
        try:
            if p == "/" or p == "":
                return self._serve_static("index.html")
            if p == "/notch":
                return self._serve_notch_page()
            if _offline_active and p.startswith(("/api/github/", "/api/remote/", "/api/mcp", "/api/link_preview", "/api/sandbox/setup")):
                return self._send_json(409, {"error": "This integration is disabled in offline mode."})
            if p.startswith("/api/"):
                return self._handle_api_get(p, parsed)
            name = p.lstrip("/")
            vendor_path = (ROOT / name).resolve()
            is_vendor = (name.startswith("assets/vendor/")
                         and vendor_path.is_relative_to((ROOT / "assets/vendor").resolve())
                         and vendor_path.is_file())
            if name in STATIC_WHITELIST or is_vendor:
                return self._serve_static(name, versioned=bool(parsed.query))
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
        try:
            if p == "/api/client-files/upload":
                return self._handle_client_file_upload()
            if _offline_active and p.startswith(("/api/github/", "/api/remote/", "/api/mcp", "/api/link_preview", "/api/sandbox/setup")):
                return self._send_json(409, {"error": "This integration is disabled in offline mode."})
            if p.startswith("/api/"):
                return self._handle_api_post(p, parsed)
            return self._send_json(404, {"error": "not found"})
        except RequestError as e:
            self._send_json(e.status, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
        try:
            if p.startswith("/api/chats/"):
                cid = p.split("/")[-1]
                chats = get_chats()
                if cid in chats["chats"]:
                    chats["chats"].pop(cid)
                    chats["order"] = [x for x in chats["order"] if x != cid]
                    save_json(CHATS_FILE, chats)
                    shutil.rmtree(VERSIONS_DIR / cid, ignore_errors=True)
                    # Free this chat's in-memory state (loop-breaker history, unlocked bundles,
                    # ctx-estimate cache) so deleted chats don't leak an entry each for the life
                    # of the process.
                    with _tool_history_lock:
                        _tool_call_history.pop(cid, None)
                        _tool_write_history.pop(cid, None)
                        _tool_url_history.pop(cid, None)
                        _tool_family_history.pop(cid, None)
                    with _TOOL_BUNDLE_LOCK:
                        _unlocked_bundles_by_chat.pop(cid, None)
                    _client_context_by_chat.pop(cid, None)
                    _CTX_EST_CACHE.pop(cid, None)
                    return self._send_json(200, {"ok": True})
                return self._send_json(404, {"error": "not found"})
            if p == "/api/cmd-history":
                # Wipe the whole cmd history file; there is no per-chat delete.
                try:
                    with _cmd_history_lock:
                        if CMD_HISTORY_FILE.exists():
                            CMD_HISTORY_FILE.unlink()
                except Exception as e:
                    return self._send_json(500, {"error": str(e)})
                return self._send_json(200, {"ok": True, "cleared": True})
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _serve_static(self, name: str, *, versioned: bool = False):
        full = ROOT / name
        if not full.is_file():
            return self._send_json(404, {"error": f"missing: {name}"})
        stat = full.stat()
        source_etag = f'{stat.st_mtime_ns:x}-{stat.st_size:x}'
        if name == "index.html":
            source_etag += _REQUEST_TOKEN
        live_ui_asset = name in UI_SECTION_FILES or name in {
            "index.html", "app.js", "appearance.js", "app.css", "workspace-shell.css", "colors_and_type.css",
            "research-ui.js", "research-ui.css", "dropdown-menus.js",
            "glyph-field.js", "security-scan-field.js", "rt-visuals.js", "agent-orb.js", "ui-refresh.css", "ui-pass.css",
            "dot-matrix.js", "workbench.css", "opal.css", "showcase.css", "showcase-field.js", "sound-cues.js",
            "accuretta-notch.html", "notch-wire.js", "notch-markdown.js",
            # The request-token wrapper: a stale cached copy can't recover a
            # page from a bridge restart.
            "bridge-client.js",
        }
        cache_control = ("no-cache, must-revalidate" if live_ui_asset
                         else ("public, max-age=31536000, immutable" if versioned
                               else "public, max-age=3600, must-revalidate"))
        data = full.read_bytes()
        if name == "index.html":
            bootstrap = (f'<meta name="accuretta-request-token" content="{_REQUEST_TOKEN}">'
                         '<script src="/bridge-client.js"></script>')
            data = data.replace(b"<head>", b"<head>" + bootstrap.encode("ascii"), 1)
        ctype = MIME.get(full.suffix, "application/octet-stream")
        accepts_gzip = "gzip" in (self.headers.get("Accept-Encoding") or "").lower()
        compressible = full.suffix.lower() in {
            ".html", ".js", ".css", ".json", ".svg", ".md", ".txt", ".webmanifest"
        }
        encoded = accepts_gzip and compressible and len(data) >= 256
        if encoded:
            data = gzip.compress(data, compresslevel=6)
        etag = f'"{source_etag}{"-gz" if encoded else ""}"'
        headers = {"Cache-Control": cache_control, "ETag": etag, "Vary": "Accept-Encoding"}
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            for key, value in headers.items():
                self.send_header(key, value)
            self._set_cors()
            self.end_headers()
            return
        if encoded:
            headers["Content-Encoding"] = "gzip"
        self._send_bytes(200, data, ctype, extra_headers=headers)

    def _serve_notch_page(self):
        """Serve accuretta-notch.html for the overlay host window.

        The file on disk stays byte-identical to the approved UI; what the host
        needs is injected into the response at serve time (as index.html gets its
        request token):
          - the per-process API token meta (the wire script authorizes POSTs with it)
          - a transparent html/body background (the host window is see-through)
          - the wiring script tag, after the page's own script so window.notch
            exists when it runs (notch-markdown.js, its reply renderer, loads first)
        Never cached: the token rotates with every bridge process."""
        full = ROOT / "accuretta-notch.html"
        if not full.is_file():
            return self._send_json(404, {"error": "notch UI missing"})
        data = full.read_bytes()
        head_inject = (
            f'<meta name="accuretta-request-token" content="{_REQUEST_TOKEN}">'
            "<style>html, body { background: transparent !important; }</style>"
        ).encode("utf-8")
        tail_inject = (b'<script src="/notch-markdown.js"></script>'
                       b'<script src="/notch-wire.js"></script>')
        if b"<head>" in data:
            data = data.replace(b"<head>", b"<head>" + head_inject, 1)
        if b"</body>" in data:
            data = data.replace(b"</body>", tail_inject + b"</body>", 1)
        headers = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                                        "img-src 'self' data:; frame-ancestors 'none'"),
            "X-Frame-Options": "DENY",
        }
        self._send_bytes(200, data, "text/html; charset=utf-8", extra_headers=headers)

    # ---- API routes

    def _handle_client_file_upload(self):
        """Stream one browser-selected file into Workspace/Incoming."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        limit = min(_remote_payload_limit(), 250 * 1024 * 1024)
        if length <= 0:
            return self._send_json(400, {"error": "empty upload"})
        if length > limit:
            return self._send_json(413, {"error": f"file exceeds the {limit // (1024 * 1024)} MB upload limit"})
        raw_name = urllib.parse.unquote(self.headers.get("X-File-Name") or "")
        name = Path(raw_name.replace("\\", "/")).name.strip()
        name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]", "_", name)[:180]
        if not name or name in {".", ".."}:
            return self._send_json(400, {"error": "invalid file name"})
        folders = get_workspace().get("folders") or []
        if not folders:
            return self._send_json(409, {"error": "No workspace folder is configured."})
        root = Path(normalize_path(folders[0]))
        try:
            incoming = (root / "Incoming").resolve()
            incoming.relative_to(root.resolve())
            incoming.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return self._send_json(500, {"error": f"Could not prepare Incoming: {exc}"})
        target = incoming / name
        if target.exists():
            target = incoming / f"{target.stem}-{uuid.uuid4().hex[:6]}{target.suffix}"
        tmp = incoming / f".{target.name}.{uuid.uuid4().hex[:8]}.part"
        remaining = length
        try:
            with tmp.open("xb") as handle:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise IOError("upload ended early")
                    handle.write(chunk)
                    remaining -= len(chunk)
            tmp.replace(target)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return self._send_json(500, {"error": f"upload failed: {exc}"})
        return self._send_json(200, {
            "ok": True, "name": target.name, "path": str(target),
            "root": str(root), "rel": str(target.relative_to(root)).replace("\\", "/"),
            "bytes": length,
        })

    def _handle_api_get(self, p: str, parsed):
        if p == "/api/mcp/servers":
            out = _mcp_list_servers()
            out["can_edit"] = self._peer_is_loopback()
            return self._send_json(200, out)
        if p == "/api/health":
            return self._send_json(200, {
                "ok": True,
                "application": "accuretta",
                "llama": LLAMA,
                "vision_llama": STATE.vision_llama,
                "llama_up": llama_ready(timeout=1.0),
                # legacy alias so older frontend builds don't break
                "ollama": LLAMA,
            })
        if p == "/api/security/overview":
            return self._send_json(200, _security_overview.get_overview())
        if p == "/api/security/processes":
            result = _security_collect_processes()
            return self._send_json(200, {
                "processes": result.get("processes") or [],
                "error": result.get("error") or "",
            })
        if p == "/api/app-update":
            qs = urllib.parse.parse_qs(parsed.query)
            manual = (qs.get("manual") or [""])[0] == "1"
            return self._send_json(200, app_update_status(manual=manual))
        if p == "/api/llama/update":
            # The llama.cpp build in use and, with check=1, the newest release.
            qs = urllib.parse.parse_qs(parsed.query)
            return self._send_json(200, llama_update_status(check=(qs.get("check") or [""])[0] == "1"))
        if p == "/api/maya/install":
            # The Maya engine in use, whether this PC can run it, and with check=1
            # the newest ready-made package.
            qs = urllib.parse.parse_qs(parsed.query)
            return self._send_json(200, maya_install_status(check=(qs.get("check") or [""])[0] == "1"))
        if p == "/api/llama-log":
            # Live tail of the llama.cpp backend's stdout/stderr — powers the
            # Backend terminal tab so model loads/reloads show real progress.
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                tail = int(qs.get("tail", ["400"])[0])
            except Exception:
                tail = 400
            return self._send_json(200, _llama.read_log(max(1, min(tail, 800))))
        if p == "/api/session/list":
            return self._send_json(200, {"sessions": _session_mgr.list()})
        if p == "/api/session/read":
            qs = urllib.parse.parse_qs(parsed.query)
            sid = (qs.get("id", [""])[0] or "").strip()
            try:
                since = int(qs.get("since", ["0"])[0])
            except Exception:
                since = 0
            s = _session_mgr.get(sid)
            if not s:
                return self._send_json(404, {"error": "no such session", "alive": False})
            out, offset = s.read_since(since)
            return self._send_json(200, {"output": out, "offset": offset, "alive": s.alive(),
                                         "exit_code": s.exit_code(), "command": s.command})
        if p == "/api/models":
            # List .gguf files under settings.models_dir. Each entry includes a
            # `loaded` flag so the UI can highlight the active model.
            s = get_settings()
            mdir = (s.get("models_dir") or "").strip()
            files = scan_gguf_dir(mdir)
            loaded = _llama.loaded_model() or s.get("model_path") or ""
            # The remembered model is gone from disk (moved/deleted): report
            # it instead of presenting it as the active model.
            missing = "" if _llama.loaded_model() or safe_exists(loaded) else loaded
            if missing:
                loaded = ""
            for f in files:
                f["loaded"] = (f["path"] == loaded)
                f.update(model_display_info_for(f))
                # False = the installed llama.cpp is too old for this model's
                # architecture (the menu warns instead of trying a load). A model
                # maya runs doesn't need llama.cpp at all; one made for maya
                # can't run without it (needs_maya: the menu says so).
                if _maya_only_model(f["path"]):
                    f["engine"] = "maya"
                    f["llama_supported"] = bool(find_maya_engine())
                    if not f["llama_supported"]:
                        f["needs_maya"] = True
                else:
                    f["llama_supported"] = llama_knows_arch(f.get("architecture") or "")
            reasoning_capability = _reasoning_capability(loaded, s) if loaded else _reasoning_capability("", s)
            return self._send_json(200, {
                "models_dir": mdir,
                "loaded_model": loaded,
                "missing_model": missing,
                "llama_running": _llama.is_running() or llama_ping(timeout=0.5),
                "vision_capable": _llama.is_vision_capable(),
                "loaded_mmproj": _llama.loaded_mmproj(),
                "reasoning_capability": reasoning_capability,
                "models": files,
            })
        if p == "/api/storage":
            return self._send_json(200, storage_report())
        if p == "/api/email":
            return self._send_json(200, {**email_settings(), "can_edit": self._peer_is_loopback()})
        if p == "/api/models/spec-drafts":
            # Draft GGUFs (DFlash/DSpark) available for the selected model,
            # best metadata-validated match first. `paired` is what auto-tune
            # and the spawn path would use; `drafts` feeds the settings picker.
            # `draft_path` additionally validates one explicitly chosen file.
            qs = urllib.parse.parse_qs(parsed.query)
            model_path = (qs.get("model_path") or [""])[0].strip()
            chosen = (qs.get("draft_path") or [""])[0].strip()
            if model_path and safe_exists(model_path) and _maya_engine_for(model_path):
                # maya's helper is Thinking Machines' drafter beside the model
                # (mtp.safetensors), not a draft GGUF.
                drafter = _maya_drafter_for(model_path)
                match = {"path": drafter, "matched": bool(drafter), "score": 100 if drafter else 0,
                         "reasons": ["Thinking Machines' drafter for the maya engine"] if drafter else []}
                return self._send_json(200, {
                    "model_path": model_path, "engine": "maya",
                    "model": {"name": Path(model_path).stem, "architecture": "inkling2", "vocab": 0,
                              "identities": []},
                    "drafts": ([{"path": drafter, "name": os.path.basename(drafter),
                                 "size": os.path.getsize(drafter), "architecture": "", "base": "",
                                 "matched": True, "score": 100, "reasons": match["reasons"]}]
                               if drafter else []),
                    "paired": drafter, "paired_match": draft_match_payload(match),
                    "selected_match": draft_match_payload(match) if chosen and chosen == drafter else {},
                })
            target = _spec_fingerprint(model_path) if model_path and safe_exists(model_path) else {}
            paired = find_spec_draft_for(model_path) if model_path else {}
            selected: dict = {}
            if chosen and model_path and safe_exists(chosen):
                selected = draft_match_payload(
                    {"path": chosen, **spec_draft_match(model_path, chosen)})
            return self._send_json(200, {
                "model_path": model_path,
                "model": {
                    "name": target.get("name", ""),
                    "architecture": target.get("architecture", ""),
                    "vocab": target.get("vocab", 0),
                    "identities": sorted(target.get("identities") or []),
                },
                "drafts": list_spec_drafts(model_path),
                "paired": paired.get("path", ""),
                "paired_match": draft_match_payload(paired),
                "selected_match": selected,
            })
        if p == "/api/models/draft-pairs":
            # Which models in the models dir have a validated DFlash/DSpark pair
            # beside them. Cached fingerprints keep repeat calls cheap; the UI
            # fetches this once to mark model-picker rows with a badge.
            s = get_settings()
            mdir = (s.get("models_dir") or "").strip()
            pairs: dict[str, dict] = {}
            for item in (scan_gguf_dir(mdir) if mdir else []):
                found = find_spec_draft_for(item["path"])
                if found.get("matched") and found.get("path"):
                    pairs[item["path"]] = {
                        "draft": os.path.basename(found["path"]),
                        "reasons": found.get("reasons") or [],
                    }
            return self._send_json(200, {"pairs": pairs})
        if p.startswith("/api/model-info/"):
            name = urllib.parse.unquote(p[len("/api/model-info/"):])
            return self._send_json(200, recommended_settings(name))
        if p == "/api/settings":
            return self._send_json(200, get_settings())
        if p == "/api/client-context":
            qs = urllib.parse.parse_qs(parsed.query)
            hint = {
                "os": (qs.get("os") or ["unknown"])[0],
                "mobile": (qs.get("mobile") or [""])[0] == "1",
                "secure_context": (qs.get("secure") or [""])[0] == "1",
                "execution_target": (qs.get("target") or ["host"])[0],
            }
            snapshot = _tailscale_snapshot()
            peers = [p for p in snapshot.get("peers", [])
                     if str(p.get("os") or "").lower() in {"macos", "darwin"}]
            return self._send_json(200, {
                "context": _handler_client_context(self, hint),
                "machines": [_public_remote_machine(m) for m in _load_remote_machines().get("machines", [])],
                "mac_peers": peers,
                "tailscale": {
                    "ok": snapshot.get("ok", False), "state": snapshot.get("state", "unknown"),
                    "health": snapshot.get("health", []), "self_dns": snapshot.get("self_dns", ""),
                    "self_ips": snapshot.get("self_ips", []),
                },
                "serve": _tailscale_serve_status(snapshot),
                "ssh_available": bool(_ssh_exe() and _ssh_keygen_exe()),
            })
        if p == "/api/model-health":
            return self._send_json(200, _public_model_health())
        if p == "/api/savings":
            # Lifetime token totals for the "saved vs cloud" card. Server-side so
            # it survives restarts and follows the machine, not the browser.
            return self._send_json(200, _savings_get())
        if p == "/api/usage-stats":
            return self._send_json(200, _usage_stats())
        if p == "/api/workspace":
            return self._send_json(200, get_workspace())
        if p == "/api/workspace/files":
            # Flat, bounded file list across all workspace folders — powers the
            # @-mention picker in the composer. Skips heavy/vendor dirs.
            skip = ("/node_modules/", "/.git/", "/__pycache__/", "/.venv/",
                    "/venv/", "/dist/", "/build/", "/.next/", "/.cache/")
            ws = get_workspace()
            out = []
            seen = set()
            for root in (ws.get("folders") or []):
                if len(out) >= 4000:
                    break
                try:
                    base = Path(root)
                    for f in safe_rglob_files(root, "*", max_depth=8):
                        ap = str(f.resolve())
                        if ap in seen:
                            continue
                        seen.add(ap)
                        try:
                            rel = str(f.relative_to(base)).replace("\\", "/")
                        except Exception:
                            rel = f.name
                        if any(sd in ("/" + rel) for sd in skip):
                            continue
                        out.append({"name": f.name, "rel": rel, "path": ap})
                        if len(out) >= 4000:
                            break
                except Exception:
                    continue
            out.sort(key=lambda m: m["rel"].lower())
            return self._send_json(200, {"files": out, "count": len(out), "truncated": len(out) >= 4000})
        if p == "/api/workbench/status":
            return self._send_json(200, _workbench_status())
        if p == "/api/workspace/git":
            qs = urllib.parse.parse_qs(parsed.query)
            return self._send_json(200, _workspace_git_status((qs.get("path") or [""])[0]))
        if p == "/api/skills":
            # Catalog of user-curated markdown skills (frontmatter
            # name/description/budget). Names only — bodies load via load_skill.
            lst = _skills_catalog()
            return self._send_json(200, {"skills": lst, "count": len(lst)})
        if p == "/api/link_preview":
            # Hover preview for links in chat bubbles: fetch the page, pull Open Graph +
            # <title>, cache in-process. The frontend only asks on first hover.
            qs = urllib.parse.parse_qs(parsed.query)
            url = (qs.get("url", [""])[0] or "").strip()
            if not url:
                return self._send_json(400, {"error": "url required"})
            return self._send_json(200, fetch_link_preview(url))
        if p == "/api/ctx-stats":
            s = get_settings()
            # Live server ctx is the real capacity — settings.num_ctx can drift
            # from how llama-server was actually launched.
            cap = _llama_props_ctx() or int(s.get("num_ctx") or 32768)
            qs = urllib.parse.parse_qs(parsed.query)
            chat_id = (qs.get("chat_id", [""])[0] or "").strip()
            # Per-chat real count (llama-server's prompt_eval_count) is the
            # ground truth once a turn has completed. Before that, tokenize the
            # assembled prompt for an accurate cold-start number.
            real = _last_prompt_tokens_by_chat.get(chat_id, 0) if chat_id else STATE.last_prompt_tokens
            if real > 0:
                return self._send_json(200, {"prompt_tokens": real, "capacity": cap, "source": "live"})
            est = _estimate_context_tokens(chat_id) if chat_id else 0
            return self._send_json(200, {"prompt_tokens": est, "capacity": cap,
                                         "source": "estimate" if est else "none"})
        if p == "/api/chats":
            qs = urllib.parse.parse_qs(parsed.query)
            chats = get_chat_index() if qs.get("summary", [""])[0] == "1" else get_chats()
            return self._send_json(200, chats)
        if p == "/api/chat-search":
            query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0]
            return self._send_json(200, {"results": search_conversations(get_chats(), query)})
        if p == "/api/approvals":
            return self._send_json(200, {"pending": list_approvals()})
        if p.startswith("/api/versions/"):
            parts = p.split("/")
            # /api/versions/<chat_id>          -> list
            # /api/versions/<chat_id>/<vid>    -> html
            if len(parts) == 4:
                return self._send_json(200, {"versions": list_versions(parts[3])})
            if len(parts) == 5:
                html = read_version(parts[3], parts[4])
                if html is None:
                    return self._send_json(404, {"error": "not found"})
                return self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/events":
            return self._serve_sse()
        if p == "/api/system-context":
            try:
                md = ensure_system_context()
            except Exception as e:
                return self._send_json(200, {"md": "", "path": str(SYSTEM_CONTEXT_FILE), "exists": False, "error": str(e)})
            return self._send_json(200, {
                "md": md,
                "path": str(SYSTEM_CONTEXT_FILE),
                "exists": SYSTEM_CONTEXT_FILE.exists(),
            })
        if p == "/api/memories":
            return self._send_json(200, {"memories": [{**m, "kind": memory_kind(m), "legacy": "kind" not in m} for m in _load_memories()], "profile": _memory_store.profile(), "path": str(MEMORIES_FILE)})
        if p == "/api/cmd-history":
            # GET ?limit=200&chat_id=XYZ — newest entries first. Optional
            # chat_id filter scopes the list to one conversation; omit it
            # for the global view.
            ch_qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(ch_qs.get("limit", ["200"])[0])
            except Exception:
                limit = 200
            limit = max(1, min(limit, 2000))
            chat_id = (ch_qs.get("chat_id", [""])[0] or "").strip()
            return self._send_json(200, {
                "entries": _read_cmd_history(limit=limit, chat_id=chat_id),
                "limit": limit,
                "chat_id": chat_id,
                "cap": CMD_HISTORY_MAX,
            })
        if p.startswith("/api/desktop/chat-state/"):
            # GET the per-chat desktop-disabled flag
            cid = p.split("/", 4)[4]
            return self._send_json(200, {
                "chat_id": cid,
                "disabled": cid in _chat_desktop_disabled,
            })
        if p.startswith("/api/chats/") and p.count("/") == 3:
            # GET a single chat's metadata (for restoring last_mode on switch)
            cid = p.split("/")[3]
            chats = get_chats()
            if cid in chats["chats"]:
                chat = chats["chats"][cid]
                return self._send_json(200, {**chat, "task_state": _chat_task_state(cid, chat)})
            return self._send_json(404, {"error": "not found"})
        if p == "/api/snapshots":
            out = []
            for f in sorted(SNAPSHOTS_DIR.glob("*.html")):
                try:
                    st = f.stat()
                    out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
                except Exception:
                    continue
            return self._send_json(200, {"snapshots": out, "path": str(SNAPSHOTS_DIR)})
        if p.startswith("/api/snapshots/"):
            # serve a single saved snapshot by filename (no path traversal)
            fname = p.split("/", 3)[3]
            if "/" in fname or "\\" in fname or ".." in fname:
                return self._send_json(400, {"error": "bad name"})
            fp = SNAPSHOTS_DIR / fname
            if not fp.exists() or not fp.is_file():
                return self._send_json(404, {"error": "not found"})
            return self._send_bytes(200, fp.read_bytes(), "text/html; charset=utf-8")
        if p.startswith("/api/jobs/"):
            job_id = p.split("/")[3]
            with _tool_jobs_lock:
                job = _tool_jobs.get(job_id)
            if not job:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {
                "id": job_id,
                "status": job.get("status"),
                "result": job.get("result"),
                "started": job.get("started"),
                "finished": job.get("finished"),
            })
        if p == "/api/desktop/status":
            s = get_settings()
            vision_ready = _vision_observation_available()
            blind_ready = bool(_HAVE_UIA)
            return self._send_json(200, {
                "enabled": bool(s.get("desktop_enabled")),
                "panic": _desktop_panic.is_set(),
                "have_pyautogui": _HAVE_PYAUTOGUI,
                "have_pil": _HAVE_PIL,
                "have_pygetwindow": _HAVE_PGW,
                "have_uiautomation": _HAVE_UIA,
                "blind_control_ready": blind_ready,
                "vision_ready": vision_ready,
                "observation_mode": ("accessibility+vision" if blind_ready and vision_ready
                                     else "accessibility" if blind_ready
                                     else "vision" if vision_ready else "none"),
                "allowlist": s.get("desktop_app_allowlist") or [],
                "max_actions_per_minute": int(s.get("desktop_max_actions_per_minute") or 30),
            })
        if p.startswith("/api/chat-media/"):
            # A picture show_image put in the chat. Names are content hashes made
            # by show_image; anything else is refused, so no other file is reachable.
            target = chat_media_file(p[len("/api/chat-media/"):])
            if target is None:
                return self._send_json(404, {"error": "no such picture"})
            try:
                data = target.read_bytes()
            except OSError as e:
                return self._send_json(500, {"error": f"read failed: {e}"})
            return self._send_bytes(200, data, CHAT_MEDIA_MIME[target.suffix], extra_headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=31536000, immutable",
            })
        if p.startswith("/api/wsfs/"):
            # Stream one file from inside a configured workspace folder, as
            # `/api/wsfs/<token>/<relative>`. Path-style on purpose: the browser resolves
            # an HTML file's `./style.css` against the URL path (not the query), so every
            # asset it fetches routes through this same endpoint.
            #
            # The token is urlsafe-base64 of the workspace root path, re-validated against
            # the live workspace list on every request, so removing a folder revokes
            # outstanding URLs.
            rest = p[len("/api/wsfs/"):]
            if "/" not in rest:
                return self._send_json(400, {"error": "missing path"})
            token, rel = rest.split("/", 1)
            try:
                # add padding back for b64decode (we strip it on the JS side)
                pad = "=" * (-len(token) % 4)
                root = _b64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
            except Exception:
                return self._send_json(400, {"error": "bad root token"})
            rel = urllib.parse.unquote(rel)
            target, err = resolve_workspace_file(root, rel)
            if err:
                msg = err.get("error", "")
                code = 403 if ("escape" in msg or "absolute" in msg or "not in workspace" in msg) else 404
                return self._send_json(code, err)
            try:
                size = target.stat().st_size
            except Exception:
                return self._send_json(500, {"error": "stat failed"})
            if size > _WS_FILE_MAX_BYTES:
                return self._send_json(413, {"error": f"file too large (>{_WS_FILE_MAX_BYTES // (1024*1024)} MB)"})
            try:
                data = target.read_bytes()
            except Exception as e:
                return self._send_json(500, {"error": f"read failed: {e}"})
            ct = _WS_FILE_MIME.get(target.suffix.lower(), "application/octet-stream")
            extra = {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
            qs = urllib.parse.parse_qs(parsed.query)
            if (qs.get("download") or [""])[0] == "1":
                encoded_name = urllib.parse.quote(target.name, safe="")
                extra["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_name}"
            return self._send_bytes(200, data, ct, extra_headers=extra)
        if p == "/api/llama/detect-vram":
            # GET — best-effort GPU VRAM probe via nvidia-smi. Used by the
            # Settings drawer to pre-fill the VRAM tier dropdown.
            return self._send_json(200, detect_vram_gb())
        if p == "/api/list-subfolders":
            # The project picker's folder browser: folder names only, anywhere on
            # this computer (the same reach as the native "Open folder…" dialog).
            # A picked folder outside the workspace is added to it, which is what
            # scopes the agent's file tools. No path = the starting points.
            qs = urllib.parse.parse_qs(parsed.query)
            return self._send_json(200, _list_subfolders((qs.get("path") or [""])[0]))
        if p == "/api/list-folder":
            qs = urllib.parse.parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            if not raw:
                return self._send_json(400, {"error": "path required"})
            target = Path(normalize_path(raw))
            # only allow listing inside configured workspace folders
            ws = get_workspace().get("folders", [])
            target_resolved = None
            try:
                target_resolved = target.resolve()
            except Exception:
                return self._send_json(400, {"error": "bad path"})
            if not _path_in_workspace(target_resolved, ws):
                return self._send_json(403, {"error": "path outside workspace"})
            if not target_resolved.exists() or not target_resolved.is_dir():
                return self._send_json(404, {"error": "not a directory"})
            entries = []
            try:
                for child in sorted(target_resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    try:
                        is_dir = child.is_dir()
                        info = {
                            "name": child.name,
                            "path": str(child),
                            "is_dir": is_dir,
                            "size": 0 if is_dir else child.stat().st_size,
                            "ext": "" if is_dir else child.suffix.lstrip(".").lower(),
                        }
                        entries.append(info)
                    except Exception:
                        continue
            except PermissionError:
                return self._send_json(403, {"error": "permission denied"})
            return self._send_json(200, {"path": str(target_resolved), "entries": entries})
        if p == "/api/setup/sysinfo":
            info = detect_hardware_specs()
            return self._send_json(200, info)
        if p == "/api/setup/download-status":
            with DOWNLOAD_LOCK:
                return self._send_json(200, DOWNLOAD_STATE)
        if p == "/api/sandbox/status":
            return self._send_json(200, wsl_probe())
        if p == "/api/setup/detect":
            # Scan common locations for llama-server.exe and list available models for
            # the first-run setup wizard.
            s = get_settings()
            mdir = (s.get("models_dir") or "").strip()
            
            # Detect llama-server paths
            llama_paths = []
            explicit = (s.get("llama_bin") or "").strip()
            if explicit and safe_exists(explicit):
                llama_paths.append({"path": explicit, "found": True, "label": "Custom path (settings)"})
                
            # Auto-detect via find_llama_bin
            auto = find_llama_bin()
            if auto and not any(lp["path"] == auto for lp in llama_paths):
                llama_paths.append({"path": auto, "found": True, "label": "Auto-detected"})
                
            # Scan common fallback locations
            home = Path.home()
            candidates = [
                (home / ".unsloth/llama.cpp/build/bin/Release/llama-server.exe", "Unstable build (Release)"),
                (home / ".unsloth/llama.cpp/build/bin/llama-server.exe", "Unstable build"),
                (home / ".docker/bin/inference/llama-server.exe", "Docker inference"),
                (home / "llama.cpp/build/bin/Release/llama-server.exe", "llama.cpp Release"),
                (home / "llama.cpp/llama-server.exe", "llama.cpp root"),
                (Path("C:/llama.cpp/build/bin/Release/llama-server.exe"), "C:/llama.cpp Release"),
                (Path("C:/llama.cpp/llama-server.exe"), "C:/llama.cpp root"),
            ]
            for c, label in candidates:
                if safe_exists(c) and not any(lp["path"] == str(c) for lp in llama_paths):
                    llama_paths.append({"path": str(c), "found": True, "label": label})
                    
            # List models in the configured directory if any
            models = []
            if mdir and safe_exists(mdir):
                models = scan_gguf_dir(mdir)
                
            # If no models found in configured directory or models_dir is empty,
            # scan other drives/common folders to auto-suggest models
            auto_models = find_all_gguf_files()
            if not models and auto_models:
                models = auto_models
                # Suggest the folder that holds the most found models, so a model
                # in its own subfolder doesn't hide the ones next to it.
                if not mdir:
                    mdir = suggest_models_dir(auto_models)
                        
            return self._send_json(200, {
                "llama_paths": llama_paths,
                "models_dir": mdir,
                "models": models,
                "settings": s,
            })
        if p == "/api/github/status":
            cred = _github_cred()
            return self._send_json(200, {
                "connected": bool(cred.get("pat")),
                "login": cred.get("login", ""),
                "avatar_url": cred.get("avatar_url", ""),
            })
        if p == "/api/github/activity":
            return self._send_json(200, _github_activity())
        if p == "/api/github/repos":
            qs = urllib.parse.parse_qs(parsed.query)
            cursor = (qs.get("after", [""])[0] or "").strip()
            try:
                limit = int(qs.get("limit", ["12"])[0] or 12)
            except (TypeError, ValueError):
                limit = 12
            return self._send_json(200, _github_repositories(limit, cursor))
        return self._send_json(404, {"error": "not found"})

    def _handle_api_post(self, p: str, parsed):
        body = self._read_json()
        if p == "/api/storage/clear":
            # Deleting app data is for the machine itself, not a paired device.
            if not self._peer_is_loopback():
                return self._send_json(403, {"error": "App data can only be cleared on the machine running Accuretta."})
            try:
                return self._send_json(200, storage_clear(str(body.get("key") or "")))
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
        if p in {"/api/email", "/api/email/test"}:
            # The mail account and its password belong to the machine itself;
            # a paired device can see the account but not change or probe it.
            if not self._peer_is_loopback():
                return self._send_json(403, {"error": "The email account can only be changed on the machine running Accuretta."})
            if p == "/api/email/test":
                if _offline_active:
                    return self._send_json(409, {"error": "Email is unavailable in offline mode."})
                return self._send_json(200, email_test_connection())
            try:
                out = update_email_settings(body)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            return self._send_json(200, {**out, "can_edit": True})
        if p in {"/api/mcp/servers", "/api/mcp/servers/delete", "/api/mcp/servers/restart", "/api/mcp/import"}:
            # A server entry is a command this machine will run, so only the
            # desktop window (loopback) may change them; a paired Tailscale
            # device can look but not add.
            if not self._peer_is_loopback():
                return self._send_json(403, {"error": "MCP servers can only be changed on the machine running Accuretta."})
            try:
                if p == "/api/mcp/servers":
                    out = _mcp_save_server(body)
                elif p == "/api/mcp/servers/delete":
                    out = _mcp_delete_server(str(body.get("name") or ""))
                elif p == "/api/mcp/servers/restart":
                    out = _mcp_restart_server(str(body.get("name") or ""))
                else:
                    out = _mcp_import(body.get("text") or "")
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            out["can_edit"] = True
            return self._send_json(200, out)
        if p == "/api/security/refresh":
            result = _security_overview.refresh()
            return self._send_json(202 if result.get("started") else 200, result)
        if p == "/api/security/summarize":
            result = _security_overview.summarize_now()
            return self._send_json(200 if result.get("ok") else 409, result)
        if p == "/api/security/whitelist":
            result = _security_overview.add_whitelist(
                str(body.get("alert_id") or ""),
                str(body.get("reason") or ""),
            )
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/security/whitelist/remove":
            result = _security_overview.remove_whitelist(str(body.get("id") or ""))
            return self._send_json(200 if result.get("ok") else 404, result)
        if p == "/api/security/alerts/filter":
            result = _security_overview.filter_alert(
                str(body.get("alert_id") or ""), str(body.get("mode") or ""))
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/security/alerts/restore":
            result = _security_overview.remove_alert_filter(str(body.get("id") or ""))
            return self._send_json(200 if result.get("ok") else 404, result)
        if p == "/api/security/investigate":
            result = _security_overview.investigate(str(body.get("alert_id") or ""))
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/security/watchlist":
            conditions = body.get("conditions") if isinstance(body.get("conditions"), dict) else None
            result = _security_overview.add_process_watch(
                str(body.get("process_key") or ""),
                str(body.get("label") or ""),
                conditions,
                acknowledge_compute=body.get("acknowledge_compute") is True,
            )
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/security/watchlist/remove":
            result = _security_overview.remove_process_watch(str(body.get("id") or ""))
            return self._send_json(200 if result.get("ok") else 404, result)
        if p == "/api/security/attention/ack":
            result = _security_overview.acknowledge_attention()
            return self._send_json(200, result)
        if p == "/api/setup/download-llama":
            build_type = body.get("build_type", "CPU")
            with DOWNLOAD_LOCK:
                if DOWNLOAD_STATE.get("status") in {"resolving", "downloading", "extracting"}:
                    return self._send_json(409, {"success": False, "error": "download already running"})
                DOWNLOAD_STATE.update({
                    "status": "resolving", "bytes_written": 0, "bytes_total": 0,
                    "speed": "0 KB/s", "error_msg": "", "release": "",
                })
            threading.Thread(target=download_and_extract_llama, args=(build_type,), daemon=True).start()
            return self._send_json(200, {"success": True})
        if p == "/api/llama/update":
            # Download, test and switch to the newest llama.cpp (rolls back on failure).
            code, payload = start_llama_update()
            return self._send_json(code, payload)
        if p == "/api/maya/install":
            # Install or update the ready-made Maya engine (rolls back on failure).
            code, payload = start_maya_install()
            return self._send_json(code, payload)
        if p == "/api/sandbox/setup":
            return self._send_json(200, start_sandbox_setup(reinstall=bool(body.get("reinstall"))))
        if p == "/api/sandbox/test":
            return self._send_json(200, sandbox_selftest())
        if p == "/api/sandbox/remove":
            _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=60)
            rc, out = _wsl_run(["--unregister", SANDBOX_DISTRO], timeout=120)
            try:
                if SANDBOX_STATE_FILE.exists():
                    SANDBOX_STATE_FILE.unlink()
            except Exception:
                pass
            _sbx_set(status="idle", step="", pct=0, error="", log=[])
            broadcast_event({"type": "sandbox:update"})
            return self._send_json(200, {"removed": rc == 0, "output": (out or "").strip()[:400]})
        if p == "/api/model-health/action":
            result = _model_health_action(
                str(body.get("action") or ""),
                str(body.get("recommendation_id") or ""),
            )
            return self._send_json(200 if result.get("ok") else 409, result)
        if p == "/api/github/validate":
            token = (body.get("token") or "").strip()
            if not token:
                return self._send_json(400, {"error": "token is required"})
            status, data = _github_rest("GET", "/user", token=token)
            if status != 200:
                return self._send_json(400, {"ok": False, "error": str(
                    data.get("message") or f"GitHub rejected the token (HTTP {status})")})
            login = str(data.get("login") or "")
            avatar = str(data.get("avatar_url") or "")
            _github_save_cred(token, login, avatar)
            return self._send_json(200, {"ok": True, "login": login, "avatar_url": avatar})
        if p == "/api/github/branch":
            owner = (body.get("owner") or "").strip()
            repo = (body.get("repo") or "").strip()
            branch = (body.get("branch") or "").strip()
            if not _GITHUB_OWNER_RE.fullmatch(owner) or not _GITHUB_REPO_RE.fullmatch(repo):
                return self._send_json(400, {"error": "invalid owner or repo name"})
            bname_err = _github_valid_branch_name(branch)
            if bname_err:
                return self._send_json(400, {"error": bname_err})
            result = _github_branch_flow(owner, repo, branch)
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/github/project":
            result = _github_create_project(
                str(body.get("name") or ""),
                str(body.get("brief") or ""),
                str(body.get("workspace_root") or ""),
                body.get("publish") is True,
                body.get("private") is not False,
            )
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/chat/note":
            result = _chat_note(str(body.get("chat_id") or ""), str(body.get("text") or ""))
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/redteam/read-program":
            # Bug Bounty questionnaire: read a pasted program into form values.
            # Nothing is authorized here; the user reviews the form and runs it.
            if not get_settings().get("red_team_enabled"):
                return self._send_json(403, {"ok": False, "error": "Enable Red team tools in Settings first."})
            result = _rt_read_program(str(body.get("text") or ""))
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/chat/goal":
            # Toggle the /goal hands-free latch for a chat without a turn. The
            # composer calls this for a bare "/goal" / "/goal off" and for the
            # chip's click-to-disable; the state is persisted and broadcast.
            cid = str(body.get("chat_id") or "").strip()[:64]
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            active = _apply_goal_latch(cid, body.get("active") is True)
            return self._send_json(200, {"ok": True, "goal_mode": active})
        if p == "/api/ui/presence":
            # Notification-routing heartbeat: {id: tab id, focused: bool}. Sent only on
            # visibility changes or a 30 s visible-tab keepalive.
            client_id = str(body.get("id") or "").strip()
            if not client_id or len(client_id) > 64:
                return self._send_json(400, {"error": "presence id required"})
            _presence_notify.note_web(client_id, body.get("focused") is True)
            snapshot = _presence_notify.presence()
            snapshot["ok"] = True
            return self._send_json(200, snapshot)
        if p == "/api/ui/open-chat":
            # The notch's "Open in main app": the overlay host raises the
            # desktop window; this broadcast is what makes that window select
            # the session (an already-running pywebview window never sees a
            # browser deep-link).
            cid = str(body.get("chat_id") or "").strip()[:64]
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            broadcast_event({"type": "ui:open-chat", "chat_id": cid})
            return self._send_json(200, {"ok": True})
        if p == "/api/settings":
            try:
                cur = update_settings(body)
            except ValueError as exc:
                return self._send_json(400, {"error": str(exc)})
            try:
                if _maya_helper_followup(body):
                    cur = get_settings()
            except Exception:
                traceback.print_exc()
            if {"security_scheduled", "security_interval_minutes", "security_notify_threshold"} & set(body):
                # live-apply scheduled-scan changes without a bridge restart
                _security_apply_schedule(cur)
            if "notch_enabled" in body:
                # live-apply the overlay toggle without a bridge restart
                notch_sync()
            broadcast_event({"type": "settings:update"})
            return self._send_json(200, cur)
        if p == "/api/remote/serve/enable":
            result = enable_tailscale_serve()
            return self._send_json(200 if result.get("ok") else 500, result)
        if p == "/api/remote/pair/prepare":
            result = prepare_remote_pairing(
                body.get("node_id") or "", body.get("username") or "",
                body.get("label") or "", body.get("allowed_roots"),
            )
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/remote/pair/verify":
            result = verify_remote_pairing(body.get("machine_id") or "")
            return self._send_json(200 if result.get("ok") else 400, result)
        if p == "/api/remote/pair/remove":
            result = remove_remote_pairing(body.get("machine_id") or "")
            return self._send_json(200 if result.get("ok") else 404, result)
        if p == "/api/compact":
            # Manual compaction (the "compact now" button / model tool). Body: {chat_id}.
            # Folds the oldest turns into the rolling summary regardless of context fill.
            chat_id = (body.get("chat_id") or "").strip()
            if not chat_id:
                return self._send_json(400, {"error": "chat_id required"})
            return self._send_json(200, _compact_chat(chat_id, force=True))
        if p == "/api/workspace":
            folders = body.get("folders") or []
            folders = [normalize_path(f) for f in folders if isinstance(f, str) and f.strip()]
            save_json(WORKSPACE_FILE, {"folders": folders})
            resolved_ws = get_workspace()
            broadcast_event({"type": "workspace:update"})
            return self._send_json(200, resolved_ws)
        if p == "/api/save-to-workspace":
            # Save preview HTML straight to a workspace folder (the preview pane's "Save
            # to workspace" button), without another model round.
            # Body: {root: "<configured workspace folder>", filename: "x.html",
            #        html: "<!DOCTYPE...>", overwrite?: bool}
            root = (body.get("root") or "").strip()
            filename = (body.get("filename") or "").strip()
            html = body.get("html")
            overwrite = bool(body.get("overwrite"))
            if not root or not filename or not isinstance(html, str):
                return self._send_json(400, {"error": "root, filename, and html required"})
            # Filename safety: a bare name, no slashes or traversal. The extension is not
            # constrained (.html / .htm / .txt are all reasonable).
            if "/" in filename or "\\" in filename or ".." in filename:
                return self._send_json(400, {"error": "filename must be a bare name (no slashes, no ..)"})
            if not filename.strip("."):
                return self._send_json(400, {"error": "invalid filename"})
            # validate root against configured workspace
            configured = [normalize_path(f) for f in get_workspace().get("folders", [])]
            root_norm = normalize_path(root)
            if root_norm not in configured:
                return self._send_json(400, {"error": "root not in configured workspace folders"})
            try:
                root_path = Path(root_norm).resolve(strict=True)
            except Exception:
                return self._send_json(400, {"error": "workspace root unreadable"})
            target = (root_path / filename).resolve()
            # belt & braces: ensure the resolved target is still inside root
            try:
                target.relative_to(root_path)
            except ValueError:
                return self._send_json(400, {"error": "resolved path escapes workspace root"})
            if target.exists() and not overwrite:
                return self._send_json(409, {"error": "file exists", "path": str(target), "exists": True})
            try:
                target.write_text(html, encoding="utf-8")
                return self._send_json(200, {
                    "ok": True,
                    "path": str(target),
                    "bytes": len(html.encode("utf-8")),
                })
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/py-check":
            # Pure syntax check: compile() validates structure without executing or
            # importing anything, and SyntaxError gives line/col/msg for the banner.
            # Accepts {root, path} (read from the workspace) or {code} (a raw snippet).
            code = body.get("code")
            file_label = "<snippet>"
            if code is None:
                root = body.get("root") or ""
                rel = body.get("path") or ""
                target, err = resolve_workspace_file(root, rel)
                if err:
                    return self._send_json(400, err)
                # 5 MB cap on Python files for the syntax check — anything
                # bigger is almost certainly not a real source file.
                try:
                    if target.stat().st_size > 5 * 1024 * 1024:
                        return self._send_json(413, {"error": "file too large for syntax check"})
                    code = target.read_text(encoding="utf-8", errors="replace")
                    file_label = target.name
                except Exception as e:
                    return self._send_json(500, {"error": f"read failed: {e}"})
            else:
                code = str(code)
            try:
                compile(code, file_label, "exec")
                return self._send_json(200, {
                    "ok": True,
                    "file": file_label,
                    "lines": code.count("\n") + 1,
                    "msg": "syntax OK",
                })
            except SyntaxError as e:
                return self._send_json(200, {
                    "ok": False,
                    "file": file_label,
                    "line": e.lineno,
                    "col": e.offset,
                    "end_line": getattr(e, "end_lineno", None),
                    "end_col": getattr(e, "end_offset", None),
                    "msg": e.msg,
                    "hint": (e.text or "").rstrip("\n"),
                })
            except ValueError as e:
                # null bytes in source etc.
                return self._send_json(200, {"ok": False, "file": file_label, "msg": str(e)})
        if p == "/api/llama/auto-tune":
            # POST {model_path?, vram_gb, min_ctx?} -> suggested llama-server
            # settings. If model_path is omitted, uses the currently configured
            # model_path from settings. If vram_gb is omitted or 0, tries
            # nvidia-smi. min_ctx makes context grow-only: the tuner honors a
            # saved ctx larger than its pick when it still fits the budget.
            mp = (body.get("model_path") or get_settings().get("model_path") or "").strip()
            vram = float(body.get("vram_gb") or 0)
            min_ctx = int(body.get("min_ctx") or 0)
            detected = None
            if vram <= 0 and not float(get_settings().get("vram_tier_gb") or 0):
                detected = detect_vram_gb()
            # Same budget as every other tuner entry point (a save+reload follows
            # the suggest, so the running instance's VRAM counts as free).
            vram = _tune_budget_gb(vram)
            profile = inspect_model(mp)
            suggested = auto_tune(mp, vram, profile=profile, min_ctx=min_ctx,
                                  proven_cfg=_proven_cfg(mp))
            suggested = _preserve_manual_model_spec(mp, suggested)
            # Remember the tune for this model — the next /api/models/load
            # restores it without re-running the tuner. A background re-tune
            # (the boot self-heal) only suggests: the page applies it through a
            # settings save, which records it per model, or leaves it when it
            # would make the running setup worse.
            if mp and suggested.get("num_ctx") and not body.get("background"):
                _save_model_config(mp, suggested)
            return self._send_json(200, {
                "vram_gb": vram,
                "vram_source": (detected or {}).get("source", "user"),
                "vram_name": (detected or {}).get("name", ""),
                "model": profile,
                "suggested": suggested,
            })
        if p == "/api/browse-folder":
            # native OS folder picker, only on the machine running the bridge.
            title = (body.get("title") or "Pick a folder").strip()
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title=title)
                root.destroy()
                return self._send_json(200, {"path": path or ""})
            except Exception as e:
                return self._send_json(200, {"path": "", "error": str(e)})
        if p == "/api/models/browse-mmproj":
            title = "Pick a vision projector GGUF"
            model_path = str(body.get("model_path") or "").strip()
            initial_dir = str(Path(model_path).parent) if model_path else ""
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askopenfilename(
                    title=title,
                    initialdir=initial_dir or None,
                    filetypes=[("GGUF projector", "*.gguf"), ("All files", "*.*")],
                )
                root.destroy()
                if path and Path(path).suffix.lower() != ".gguf":
                    return self._send_json(400, {"error": "projector must be a .gguf file"})
                return self._send_json(200, {"path": path or ""})
            except Exception as e:
                return self._send_json(200, {"path": "", "error": str(e)})
        if p == "/api/models/browse-spec-draft":
            model_path = str(body.get("model_path") or "").strip()
            initial_dir = str(Path(model_path).parent) if model_path else ""
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askopenfilename(
                    title="Pick a DFlash or DSpark draft GGUF",
                    initialdir=initial_dir or None,
                    filetypes=[("GGUF draft model", "*.gguf"), ("All files", "*.*")],
                )
                root.destroy()
                if path and Path(path).suffix.lower() != ".gguf":
                    return self._send_json(400, {"error": "draft model must be a .gguf file"})
                return self._send_json(200, {"path": path or ""})
            except Exception as e:
                return self._send_json(200, {"path": "", "error": str(e)})
        if p == "/api/models/scan-dir":
            # Save settings.models_dir and immediately return the scan.
            new_dir = (body.get("path") or "").strip()
            if new_dir and not Path(new_dir).is_dir():
                return self._send_json(400, {"error": f"not a directory: {new_dir}"})
            s = get_settings()
            s["models_dir"] = new_dir
            save_json(SETTINGS_FILE, s)
            files = scan_gguf_dir(new_dir)
            loaded = _llama.loaded_model() or s.get("model_path") or ""
            for f in files:
                f["loaded"] = (f["path"] == loaded)
            broadcast_event({"type": "models:update"})
            return self._send_json(200, {
                "models_dir": new_dir,
                "loaded_model": loaded,
                "models": files,
            })
        if p == "/api/session/send":
            # The human operator typing into the shared session — no approval
            # gate here (that's the user at their own keyboard, not the model).
            sid = (body.get("id") or body.get("session_id") or "").strip()
            s = _session_mgr.get(sid)
            if not s:
                return self._send_json(404, {"error": "no such session"})
            ok = s.send(str(body.get("input", "")), enter=bool(body.get("enter", True)))
            return self._send_json(200, {"ok": ok, "alive": s.alive()})
        if p == "/api/session/stop":
            sid = (body.get("id") or body.get("session_id") or "").strip()
            return self._send_json(200, {"stopped": _session_mgr.stop(sid)})
        if p == "/api/models/unload-draft":
            # Explicit decoder unload: drop the DFlash/DSpark sidecar so its
            # VRAM goes back to context, auto-apply the best draft-less
            # strategy (source stays auto, so the user can change it later),
            # and retune immediately so the regrown ctx is visible before the
            # reload the UI triggers next.
            try:
                want = (body.get("model_path") or "").strip() if isinstance(body, dict) else ""
            except Exception:
                want = ""
            s = get_settings()
            if not want:
                want = (s.get("model_path") or "").strip() or _llama.loaded_model()
            if not want or not safe_exists(want):
                return self._send_json(400, {"error": "no model file to unload the draft for"})
            try:
                update_settings({
                    "spec_draft_model": "",
                    "spec_draft_mode": "off",
                    "spec_strategy_source": "auto",
                })
            except Exception as exc:
                return self._send_json(400, {"error": str(exc)})
            tune_applied = False
            tune_notes: list = []
            try:
                vram = _tune_budget_gb()
                if vram > 0:
                    profile = inspect_model(want)
                    suggested = auto_tune(want, vram, profile=profile,
                                          proven_cfg=_proven_cfg(want))
                    if suggested.get("num_ctx"):
                        suggested["spec_strategy_source"] = "auto"
                        s2 = get_settings()
                        for k in _MODEL_TUNE_KEYS:
                            if k in suggested:
                                s2[k] = suggested[k]
                        save_json(SETTINGS_FILE, s2)
                        _save_model_config(want, suggested)
                        tune_applied = True
                        tune_notes = suggested.get("notes", "")
            except Exception:
                traceback.print_exc()
            try:
                # The cleared draft must also be forgotten per-model; on tune
                # failure drop the stale draft-sized tune keys too, so the
                # next load retunes fresh instead of restoring them.
                if not tune_applied:
                    try:
                        allc = _models_config()
                        ent = allc.get(want)
                        if isinstance(ent, dict):
                            for k in _MODEL_TUNE_KEYS:
                                ent.pop(k, None)
                            save_json(MODELS_CONFIG_FILE, allc)
                    except Exception:
                        pass
                _save_model_config(want, {"spec_draft_model": ""})
            except Exception:
                pass
            fin = get_settings()
            return self._send_json(200, {
                "ok": True,
                "model_path": want,
                "spec_draft_model": "",
                "spec_draft_mode": "off",
                "spec_strategy": fin.get("spec_strategy"),
                "spec_strategy_source": "auto",
                "num_ctx": fin.get("num_ctx"),
                "tune_applied": tune_applied,
                "suggested": {k: fin.get(k) for k in (
                    "num_ctx", "num_gpu", "num_batch", "n_ubatch", "n_cpu_moe",
                    "num_thread", "kv_cache_type", "kv_cache_type_v",
                    "flash_attn", "spec_strategy")},
                "notes": tune_notes,
            })
        if p == "/api/models/load":
            # Switch the active model. Kills current llama-server, spawns new.
            target = (body.get("path") or "").strip()
            if not target:
                return self._send_json(400, {"error": "path required"})
            if not safe_exists(target):
                return self._send_json(400, {"error": f"file not found: {target}"})
            # A build that doesn't know the architecture can only fail; say why
            # before stopping the running model or touching any settings.
            _target_arch = str((read_gguf_metadata(target, max_bytes=1024 * 1024, retry_bytes=0)
                                .get("keys") or {}).get("general.architecture") or "")
            if _maya_only_model(target) and not find_maya_engine():
                return self._send_json(409, {"ok": False, "needs_maya": True,
                                             "error": maya_needed_message(target)})
            if (_target_arch and llama_knows_arch(_target_arch) is False
                    and not _maya_engine_for(target)):
                return self._send_json(409, {"ok": False, "llama_too_old": True,
                                             "error": llama_too_old_message(_target_arch)})

            # Per-model config memory + auto-tune on load: restore this model's tuned
            # flags (or compute them on first load) before the spawn reads settings, so
            # no model inherits the previous one's config.
            target_cfg = _models_config().get(target)
            target_cfg = target_cfg if isinstance(target_cfg, dict) else {}
            _before_switch = get_settings()
            _prev_model_settings = {k: _before_switch[k] for k in _SWITCH_RESTORE_KEYS
                                    if k in _before_switch}
            _auto_tune_for_load(target)
            _sampling_swap = _apply_model_sampling(target)
            if _sampling_swap:
                print(f"[llama] sampling: {'model profile applied' if _sampling_swap == 'profile' else 'baseline restored'}"
                      f" for {os.path.basename(target)}", file=sys.stderr)

            s = get_settings()
            if target != _llama.loaded_model():
                # Restore a projector choice remembered for this model. For a
                # new model with no projector profile, never carry a manual
                # path across: an incompatible projector can crash at startup.
                if not any(k in target_cfg for k in _MODEL_PROJECTOR_KEYS):
                    mode = _normalized_mmproj_mode(s)
                    if mode == "manual":
                        s["mmproj_mode"] = "off"
                        s["mmproj_auto"] = False
                        s["mmproj_path"] = ""
                        save_json(SETTINGS_FILE, s)
                
            # Big models load slowly from a cold disk (a 24 GB GGUF on an HDD takes 5-8
            # min before llama-server answers), so scale the wait with file size: floor
            # 120s, cap 15 min.
            try:
                _sz_gb = os.path.getsize(target) / 1e9
            except Exception:
                _sz_gb = 0.0
            wait_s = max(120, min(900, int(_sz_gb * 20)))
            res = _llama.start(target, wait_seconds=wait_s)
            if res.get("ok"):
                s = get_settings()
                s["model_path"] = target
                # The model id sent to llama-server is the file's stem (any id works; this
                # one reads well in logs and the UI).
                s["model"] = Path(target).stem
                save_json(SETTINGS_FILE, s)
                _start_vision_fallback_if_needed(s)
                broadcast_event({"type": "models:update", "loaded_model": target})
                # Compile the tool-call grammar now (background) so the first real turn
                # emits structured tool_calls (see _warmup_tool_grammar). Skipped for Gemma,
                # which runs text-tools.
                if not _is_gemma_model(s.get("model") or target):
                    threading.Thread(target=_warmup_tool_grammar, daemon=True).start()
            else:
                if not _llama.is_running():
                    # Nothing came up, and model_path still names the previous
                    # model: give it back the tune, projector and draft that
                    # were swapped for the target's above, so the watchdog
                    # restarts it as it was. (Still loading: leave it be.)
                    with _SETTINGS_LOCK:
                        s = get_settings()
                        s.update(_prev_model_settings)
                        save_json(SETTINGS_FILE, s)
                if _sampling_swap:
                    # Same for sampling: rather than leave the failed
                    # target's profile.
                    _apply_model_sampling(str(get_settings().get("model_path") or ""))
            return self._send_json(200 if res.get("ok") else 500, res)
        if p == "/api/models/stop":
            # User-initiated stop also tells the watchdog to leave it down; the next
            # /api/models/load re-arms it.
            _llama.stop_permanent()
            _vision_llama.stop_permanent()
            broadcast_event({"type": "models:update", "loaded_model": ""})
            return self._send_json(200, {"ok": True})
        if p == "/api/models/watchdog":
            return self._send_json(200, _llama.watchdog_status())
        if p == "/api/models/probe-mmproj":
            # Settings auto-detect: is there a vision projector next to this model?
            # Returns the resolved path or "".
            target = (body.get("path") or "").strip()
            if not target:
                # default to the currently loaded / configured model
                target = _llama.loaded_model() or (get_settings().get("model_path") or "")
            mmproj = find_mmproj_for(target) if target else ""
            reason = ""
            if not mmproj:
                if not target or not safe_exists(target):
                    reason = "the selected chat model file is missing"
                else:
                    nearby = []
                    mp = Path(target)
                    for folder in (mp.parent, mp.parent.parent):
                        try:
                            nearby.extend(p.name for p in folder.glob("*.gguf")
                                          if _is_mmproj_name(p.name))
                        except Exception:
                            pass
                    reason = (
                        "projector candidates were found, but their names are too ambiguous to pair safely; choose the file manually"
                        if nearby else
                        "no projector-named .gguf file was found beside the selected model"
                    )
            return self._send_json(200, {
                "ok": True,
                "model_path": target,
                "mmproj_path": mmproj,
                "reason": reason,
            })
        if p == "/api/chats":
            chat_id = body.get("id") or uuid.uuid4().hex[:12]
            chats = get_chats()
            if chat_id not in chats["chats"]:
                # `origin` records where the session was started. The chat list
                # uses it to distinguish mobile and GitHub worktree sessions.
                origin = (body.get("origin") or "desktop").strip().lower()
                if origin not in ("mobile", "desktop", "github", "project", "security"):
                    origin = "desktop"
                security_context = None
                security_alert_id = str(body.get("security_alert_id") or "").strip()
                if security_alert_id:
                    resolved = _security_overview.investigation_context(security_alert_id)
                    if not resolved.get("ok"):
                        return self._send_json(404, {"error": resolved.get("error") or "Alert not found."})
                    security_context = resolved["context"]
                    origin = "security"
                record = {
                    "id": chat_id,
                    "title": (
                        f"Investigate: {security_context.get('title') or security_context.get('entity') or 'security alert'}"
                        if security_context else str(body.get("title") or "new session")
                    )[:180],
                    "created": int(time.time()),
                    "updated": int(time.time()),
                    "messages": [],
                    "origin": origin,
                }
                if security_context:
                    record["security_investigation"] = security_context
                worktree = _github_worktree_metadata(body.get("github_worktree"))
                if worktree:
                    record["github_worktree"] = worktree
                project = _project_workspace_metadata(body.get("project_workspace"))
                if project:
                    record["project_workspace"] = project
                chats["chats"][chat_id] = record
                if chat_id in chats["order"]:
                    chats["order"].remove(chat_id)
                chats["order"].insert(0, chat_id)
                save_json(CHATS_FILE, chats)
            return self._send_json(200, chats["chats"][chat_id])
        if p.startswith("/api/chats/") and p.endswith("/rename"):
            cid = p.split("/")[3]
            chats = get_chats()
            if cid in chats["chats"]:
                chats["chats"][cid]["title"] = (body.get("title") or "").strip() or chats["chats"][cid]["title"]
                save_json(CHATS_FILE, chats)
                return self._send_json(200, chats["chats"][cid])
            return self._send_json(404, {"error": "not found"})
        if p.startswith("/api/chats/") and p.endswith("/mode"):
            cid = p.split("/")[3]
            raw_mode = str(body.get("mode") or "").strip().lower()
            if raw_mode not in {"auto", "agent", "ide", "research"}:
                return self._send_json(400, {"error": "invalid chat mode"})
            mode = _normalize_composer_mode(raw_mode)
            chats = get_chats()
            if cid in chats["chats"]:
                chats["chats"][cid]["last_mode"] = mode
                save_json(CHATS_FILE, chats)
                return self._send_json(200, {"ok": True, "mode": mode})
            return self._send_json(404, {"error": "not found"})
        if p.startswith("/api/chats/") and p.endswith("/project"):
            cid = p.split("/")[3]
            chats = get_chats()
            if cid in chats["chats"]:
                raw_project = body.get("project_workspace")
                if raw_project is None:
                    chats["chats"][cid].pop("project_workspace", None)
                else:
                    project = _project_workspace_metadata(raw_project)
                    if not project:
                        return self._send_json(400, {"error": "choose a folder inside a configured workspace folder"})
                    chats["chats"][cid]["project_workspace"] = project
                save_json(CHATS_FILE, chats)
                return self._send_json(200, chats["chats"][cid])
            return self._send_json(404, {"error": "not found"})
        if p == "/api/approvals/decide":
            ok = decide_approval(body.get("id") or "", body.get("decision") or "deny",
                                 always=bool(body.get("always")))
            return self._send_json(200 if ok else 404, {"ok": ok})
        if p == "/api/notch/reg-query":
            # Read-only helper for the notch overlay's registry-gate diff view.
            # Parses the pending command, reads the CURRENT value with
            # `reg query` (never executes the pending command itself).
            cmd = str(body.get("command") or "")
            if len(cmd) > 4096:
                return self._send_json(400, {"error": "command too long"})
            return self._send_json(200, _notch_registry_diff(cmd))
        if p == "/api/undo":
            return self._send_json(200, _undo_restore(body.get("turn_id") or "", chat_id=body.get("chat_id") or "", file_index=body.get("file_index")))
        if p == "/api/investigation-review":
            chat_id = str(body.get("chat_id") or "")
            chat = get_chats().get("chats", {}).get(chat_id)
            if not chat:
                return self._send_json(404, {"error": "Session not found"})
            mission = chat.get("mission") or {}
            mission_id = str(mission.get("authorization_id") or "")
            if not mission_id:
                return self._send_json(200, {"mission": mission, "findings": [], "planned_checks": [], "observations": [], "total": 0})
            store = EvidenceStore(DATA / "recon_evidence" / "receipts")
            try:
                if body.get("evidence_id"):
                    return self._send_json(200, {"evidence": store.read(chat_id, mission_id, str(body["evidence_id"]))})
                offset = max(0, int(body.get("offset") or 0))
                return self._send_json(200, {"mission": mission,
                    "findings": [f for f in chat.get("findings", []) if f.get("mission_id") == mission_id],
                    "planned_checks": [p for p in chat.get("security_coverage", []) if p.get("mission_id") == mission_id],
                    **store.coverage(chat_id, mission_id, offset=offset)})
            except (ValueError, OSError) as exc:
                return self._send_json(400, {"error": str(exc)})
        if p == "/api/task-review":
            result = _undo_store.review(body.get("turn_id"), body.get("chat_id"), body.get("file_index"))
            return self._send_json(404 if result.get("error") else 200, result)
        if p == "/api/open-folder":
            return self._send_json(200, _open_folder_in_file_manager(body.get("path") or ""))
        if p == "/api/tools/call":
            job_id = uuid.uuid4().hex[:12]
            name = body.get("name") or ""
            args = body.get("arguments") or {}

            def _do_job():
                with _tool_jobs_lock:
                    _tool_jobs[job_id]["status"] = "running"
                try:
                    result = invoke_tool(name, args)
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["status"] = "done"
                        _tool_jobs[job_id]["result"] = result
                except Exception as e:
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["status"] = "error"
                        _tool_jobs[job_id]["result"] = {"error": str(e)}
                finally:
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["finished"] = int(time.time())

            with _tool_jobs_lock:
                _tool_jobs[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "name": name,
                    "started": int(time.time()),
                    "finished": None,
                    "result": None,
                }
            _tool_executor.submit(_do_job)
            return self._send_json(202, {"job_id": job_id, "status": "queued"})
        if p == "/api/skills/clear":
            # Unload the active skill for a chat (route form of
            # load_skill with an empty name — emits skill:cleared to SSE).
            return self._send_json(200, tool_load_skill({"name": "", "chat_id": body.get("chat_id") or ""}))
        if p == "/api/chat":
            return self._handle_chat(body)
        if p == "/api/shutdown":
            save = bool(body.get("save"))
            if save:
                messages = body.get("messages") or []
                text_history = []
                for m in messages:
                    role = m.get("role", "")
                    if role in ("user", "assistant"):
                        text_history.append(f"{role.upper()}: {m.get('content')}")
                history_str = "\n\n".join(text_history)[-20000:]
                
                prompt = (
                    "System: You are a summarizer. Summarize this session in 1-3 sentences. "
                    "Focus on what was worked on, any decisions made, and unresolved threads. Be concise.\n\n"
                    "Conversation:\n" + history_str + "\n\n"
                    "Provide your response exactly in this format:\n"
                    "SUMMARY: <your 1-3 sentence summary>\n"
                    "TAGS: <tag1>, <tag2>, <tag3>\n"
                )
                
                settings = get_settings()
                payload = {
                    "model": settings.get("model", ""),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 1500,
                    "stream": False,
                }
                try:
                    res = llama_post("/v1/chat/completions", payload, timeout=120)
                    content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                    summary = ""
                    tags = []
                    lines = content.strip().split("\n")
                    for line in lines:
                        if line.startswith("SUMMARY:"):
                            summary = line.replace("SUMMARY:", "").strip()
                        elif line.startswith("TAGS:"):
                            tags_str = line.replace("TAGS:", "").strip()
                            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                    
                    if not summary:
                        summary = content.strip()
                    if not summary:
                        raise ValueError("The model returned an empty session summary")

                    if summary:
                        with _memory_store.lock:
                            memories = _load_memories()
                            new_mem = {
                                "id": uuid.uuid4().hex[:12],
                                "text": summary,
                                "kind": "fact",
                                "tags": tags,
                                "created": int(time.time()),
                                "use_count": 0
                            }
                            memories.append(new_mem)
                            _save_memories(memories)
                except Exception as e:
                    print("Shutdown summary error:", e)
                    return self._send_json(500, {"ok": False, "error": "Could not save the session summary. Retry or choose Close Without Saving."})
            
            def hard_exit():
                time.sleep(1)
                _shutdown_ready.set()
                # Watchdog first, as the Ctrl+C path does: otherwise it sees the
                # server it was told to stop as a crash and logs a false
                # ModelServerCrash in the last second before exit.
                try:
                    _llama.shutdown_watchdog()
                    _vision_llama.shutdown_watchdog()
                except Exception:
                    pass
                try:
                    _llama.stop()
                    _vision_llama.stop()
                except:
                    pass
                os._exit(0)
            threading.Thread(target=hard_exit, daemon=True).start()
            return self._send_json(200, {"ok": True})
        if p == "/api/chat/steer-status":
            cid, message_id = body.get("chat_id"), body.get("id")
            if not isinstance(cid, str) or not isinstance(message_id, str):
                return self._send_json(400, {"error": "chat_id and id required"})
            return self._send_json(200, {"status": _steering.status(cid, message_id)})
        if p == "/api/chat/steer":
            cid, message_id, text = body.get("chat_id"), body.get("id"), body.get("text", "")
            images = body.get("images") or []
            if (not isinstance(cid, str) or not isinstance(message_id, str) or not 1 <= len(message_id) <= 80
                    or not isinstance(text, str) or len(text) > 100000
                    or not isinstance(images, list) or len(images) > 8
                    or any(not isinstance(img, str) or not img.startswith("data:image/") for img in images)
                    or not (text.strip() or images)):
                return self._send_json(400, {"error": "Invalid task update"})
            if images and not _llama.is_vision_capable():
                return self._send_json(400, {"error": "The current model cannot read images. Send text or a file instead."})
            status = _steering.submit(cid, {"id": message_id, "text": text, "images": images})
            return self._send_json(409 if status == "inactive" else 429 if status == "full" else 200,
                                   {"status": status})
        if p == "/api/cancel":
            cid = (body.get("chat_id") or "").strip()
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            ok = cancel_chat(cid)
            return self._send_json(200, {"ok": ok, "chat_id": cid})
        if p == "/api/kill-command":
            # Kill JUST the shell command running for this chat (not the turn).
            cid = (body.get("chat_id") or "").strip()
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            ok = _kill_current_command(cid)
            return self._send_json(200, {"ok": ok, "chat_id": cid})
        if p == "/api/prewarm":
            # llama-server keeps the model resident after startup, so "prewarm"
            # is just a 1-token ping that forces any lazy mmap to fault in.
            model = (body.get("model") or "local").strip() or "local"
            try:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                    "stream": False,
                }
                llama_post("/v1/chat/completions", payload, timeout=120)
                # Also compile the tool-call grammar so the first tool-using turn
                # is clean (see _warmup_tool_grammar). Background; non-Gemma only.
                if not _is_gemma_model(get_settings().get("model") or model):
                    threading.Thread(target=_warmup_tool_grammar, daemon=True).start()
                return self._send_json(200, {"ok": True, "model": model})
            except Exception as e:
                return self._send_json(200, {"ok": False, "error": str(e)})
        if p == "/api/desktop/panic":
            _desktop_panic.set()
            # deny every pending approval so any in-flight action unblocks fast
            with _approvals_lock:
                pending = [a["id"] for a in _approvals.values() if a.get("status") == "pending"]
            for aid in pending:
                decide_approval(aid, "deny")
            broadcast_event({"type": "desktop:panic", "on": True})
            return self._send_json(200, {"ok": True, "panic": True})
        if p == "/api/desktop/resume":
            _desktop_panic.clear()
            broadcast_event({"type": "desktop:panic", "on": False})
            return self._send_json(200, {"ok": True, "panic": False})
        if p == "/api/memories/edit":
            r = tool_edit_memory(body)
            return self._send_json(400 if r.get("error") else 200, r)
        if p == "/api/memories/forget":
            mid = (body.get("id") or "").strip()
            if not mid:
                return self._send_json(400, {"error": "id required"})
            r = tool_forget({"id": mid})
            broadcast_event({"type": "memories:update"})
            return self._send_json(200, r)
        if p == "/api/memories/clear":
            try:
                _save_memories([])
                broadcast_event({"type": "memories:update"})
                return self._send_json(200, {"ok": True})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/memories":
            # manual add from the memories panel in Settings
            text = (body.get("text") or "").strip()
            tags = body.get("tags") or []
            if not text:
                return self._send_json(400, {"error": "text required"})
            r = tool_remember({"text": text, "kind": body.get("kind", "fact"), "tags": tags if isinstance(tags, list) else []})
            broadcast_event({"type": "memories:update"})
            return self._send_json(400 if r.get("error") else 200, r)
        if p == "/api/client-error":
            try:
                _error_store.record("interface", str(body.get("type") or "Error")[:80],
                                    str(body.get("message") or "")[:500],
                                    str(body.get("location") or "interface")[:120],
                                    str(body.get("trace") or ""))
            except Exception:
                pass
            return self._send_json(200, {"ok": True})
        if p == "/api/errors/clear":
            try:
                return self._send_json(200, _error_store.clear())
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/errors/report":
            try:
                result = _error_store.report(str(body.get("id") or ""))
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
            return self._send_json(404 if result.get("error") else 200, result)
        if p == "/api/snapshots":
            # save the currently-rendered preview html (or any html blob the
            # client wants to keep) to data/snapshots/ with a safe filename.
            raw_name = (body.get("name") or "snapshot").strip()
            html = body.get("html") or ""
            if not html:
                return self._send_json(400, {"error": "html required"})
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name)[:60] or "snapshot"
            if not safe.lower().endswith(".html"):
                safe = safe + ".html"
            ts = time.strftime("%Y%m%d-%H%M%S")
            final_name = f"{ts}-{safe}"
            out_path = SNAPSHOTS_DIR / final_name
            out_path.write_text(html, encoding="utf-8")
            return self._send_json(200, {
                "ok": True,
                "name": final_name,
                "path": str(out_path),
                "url": f"/api/snapshots/{final_name}",
            })
        if p == "/api/desktop/chat-toggle":
            cid = (body.get("chat_id") or "").strip()
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            disabled = bool(body.get("disabled"))
            if disabled:
                _chat_desktop_disabled.add(cid)
            else:
                _chat_desktop_disabled.discard(cid)
            return self._send_json(200, {"chat_id": cid, "disabled": disabled})
        if p == "/api/system-context/refresh":
            try:
                md = rescan_system_context()
                broadcast_event({"type": "system-context:update"})
                return self._send_json(200, {"md": md, "path": str(SYSTEM_CONTEXT_FILE)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/system-context":
            md = (body.get("md") or "").strip()
            if not md:
                return self._send_json(400, {"error": "md empty"})
            try:
                SYSTEM_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
                SYSTEM_CONTEXT_FILE.write_text(md + "\n", encoding="utf-8")
                broadcast_event({"type": "system-context:update"})
                return self._send_json(200, {"md": md, "path": str(SYSTEM_CONTEXT_FILE)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        return self._send_json(404, {"error": "not found"})

    # ---- SSE

    def _serve_sse(self):
        # Reconnect support: EventSource resends Last-Event-ID after a drop. Replay
        # missed events from the ring buffer, then resume live. The subscribe()
        # snapshot id is the cut-off: newer events arrive via the queue, older ones
        # were seen already or rolled off the buffer (a `lost` marker says so).
        last_id = 0
        raw = (self.headers.get("Last-Event-ID") or "").strip()
        if raw.isdigit():
            try:
                last_id = int(raw)
            except Exception:
                last_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self._set_cors()
        self.end_headers()
        q, snapshot_id = subscribe()
        try:
            # Hello includes the snapshot id so the client can detect a
            # bridge restart (snapshot_id reset to a small number).
            self._sse_send(
                {"type": "hello", "t": int(time.time()),
                 "snapshot_id": snapshot_id,
                 "replayed_from": last_id if last_id else None},
                evt_id=snapshot_id or None,
            )
            # Replay missed events.
            if last_id and last_id < snapshot_id:
                missed = replay_events_since(last_id, snapshot_id)
                if missed:
                    # If the gap exceeds the buffer, the oldest replayed id is > last_id + 1;
                    # flag it so the client can decide whether to reload.
                    oldest_replayed = missed[0][0]
                    if oldest_replayed > last_id + 1:
                        self._sse_send({
                            "type": "events:gap",
                            "lost_from": last_id + 1,
                            "lost_to": oldest_replayed - 1,
                            "note": "bridge buffer overflowed during disconnect; some events were lost",
                        })
                    for evt_id, evt in missed:
                        self._sse_send(evt, evt_id=evt_id)
            # Pending approvals are *state*, not history — always re-emit
            # so a fresh tab finds them even if the original event aged out.
            for a in list_approvals():
                self._sse_send({"type": "approval:new", "approval": a})
            # An in-flight fold's start event is transient and not in the resume ring, so
            # re-emit it on every (re)connect; a late client still draws the compaction
            # row.
            for fold_chat_id in list_inflight_folds():
                self._sse_send({"type": "summary_folding", "chat_id": fold_chat_id})
            last_ping = time.time()
            while True:
                try:
                    evt = q.get(timeout=15)
                    self._sse_send(evt, evt_id=evt.get("_id"))
                except Empty:
                    if time.time() - last_ping > 14:
                        try:
                            # A named heartbeat reaches JavaScript (a bare
                            # comment ping does not), so the client can tell a
                            # live stream from a silently dead one.
                            self._sse_send({"type": "heartbeat", "t": int(time.time())})
                            last_ping = time.time()
                        except Exception:
                            break
        except Exception:
            pass
        finally:
            unsubscribe(q)

    def _sse_send(self, obj: dict, evt_id: int = None):
        try:
            if evt_id is not None and evt_id > 0:
                self.wfile.write(f"id: {evt_id}\n".encode("utf-8"))
            self.wfile.write(b"data: ")
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except Exception:
            raise

    # ---- chat streaming endpoint

    def _handle_chat(self, body: dict):
        """Run one chat turn, and promise every other surface its end.

        A page or notch mirroring this turn finishes only on chat_end from the
        shared bus. When the requesting page is gone (Stop, a closed tab), a
        write raises and the turn can leave by an early exit before its own
        chat_end; send one here if chat_start went out and chat_end did not."""
        self._turn_bus = None
        try:
            return self._handle_chat_turn(body)
        finally:
            bus = self._turn_bus
            self._turn_bus = None
            if bus and not bus["ended"]:
                try:
                    broadcast_event({"type": "chat_end", "chat_id": bus["chat_id"],
                                     "web_focused": _web_ui_focused()})
                except Exception:
                    pass

    def _handle_chat_turn(self, body: dict):
        chat_id = body.get("chat_id") or uuid.uuid4().hex[:12]
        user_text = (body.get("message") or "").strip()
        # /goal latch command: strip it from the task text so the model sees a
        # clean instruction. goal_action is "on" | "off" | None.
        user_text, goal_action = _parse_goal_command(user_text)
        raw_mode = str(body.get("mode") or "agent").strip().lower()
        if raw_mode not in {"auto", "ide", "agent", "research"}:
            return self._send_json(400, {"error": "invalid chat mode"})
        mode = _normalize_composer_mode(raw_mode)
        images = body.get("images") or []  # list of base64 data URLs
        research_request = None
        resume_research_id = str(body.get("research_id") or "")
        if mode == "research":
            try:
                previous_brief = None
                if resume_research_id:
                    if chat_id in _research_runs:
                        return self._send_json(409, {"error": "This research is already running."})
                    previous_brief = _research_store.get(resume_research_id, chat_id)["brief"]
                if body.get("regenerate"):
                    previous_chat = get_chats().get("chats", {}).get(chat_id, {})
                    previous_user = next((m for m in reversed(previous_chat.get("messages", [])) if m.get("role") == "user"), {})
                    if user_text:
                        # edit & resend: the replacement text wins for the brief topic
                        previous_brief = previous_user.get("_research_brief") or {"topic": user_text}
                    else:
                        previous_brief = previous_user.get("_research_brief") or {"topic": previous_user.get("content")}
                research_request = research_brief(previous_brief or body.get("research_brief") or {"topic": user_text})
            except (ValueError, OSError) as error:
                return self._send_json(400, {"error": str(error)})
            if body.get("mission"):
                return self._send_json(400, {"error": "Research and red-team missions are separate modes."})
        regenerate = bool(body.get("regenerate"))
        # A surface that omits the effort (the notch composer) must not reset the
        # user's choice to "auto"; fall back to the saved setting.
        _effort = body.get("reasoning_effort")
        if _effort is None or not str(_effort).strip():
            _effort = get_settings().get("reasoning_effort")
        reasoning_effort = _normalize_effort_choice(_effort)
        client_ctx = _handler_client_context(self, body.get("client_context"))
        _client_context_by_chat[chat_id] = client_ctx
        if client_ctx.get("execution_target") != "host":
            _unlock_bundle(chat_id, "remote")
        # Bare "/goal" (or "/goal off") with no task: a latch toggle, not a
        # turn. Flip the hands-free latch and acknowledge over plain JSON — no
        # model call, no chat bubble. "/goal <task>" falls through and runs.
        if goal_action and not user_text and not images and not regenerate:
            active = _apply_goal_latch(chat_id, goal_action == "on")
            return self._send_json(200, {"ok": True, "goal_mode": active,
                "message": ("Goal mode on — this chat runs hands-free. Deletions and destructive actions still ask."
                            if active else
                            "Goal mode off — back to your standing permission mode.")})
        if not user_text and not images and not regenerate:
            return self._send_json(400, {"error": "empty message"})
        mission_panel = body.get("mission")
        if mission_panel is not None:
            if not get_settings().get("red_team_enabled"):
                return self._send_json(403, {"error": "red-team tools are disabled in Settings"})
            if (not isinstance(mission_panel, dict)
                    or mission_panel.get("authorized") is not True
                    or not str(mission_panel.get("target") or "").strip()):
                return self._send_json(400, {
                    "error": "red-team mission requires an explicit authorization confirmation and target"
                })
            if (str(mission_panel.get("engagement") or "").strip().lower() == "access_control"
                    and not _rt_identities_usable(mission_panel.get("identities"))):
                return self._send_json(400, {
                    "error": "The Access Control playbook needs a sign-in for two different test accounts."
                })

        # Two paths for incoming images:
        #   (a) The loaded model has its own vision tower (--mmproj): pass the images
        #       through as image_url blocks and persist the data URLs for replay.
        #   (b) Text-only model + a separate vision server (VISION_LLAMA_HOST): run
        #       each image through that side model and inline its description.
        # With neither available, fail up front rather than inlining a "[vision
        # server failed]" note the chat model would then make excuses about.
        vision_native = bool(images) and _llama.is_vision_capable()
        ocr_available = _separate_vision_server_ready()
        if images and not vision_native and not ocr_available:
            loaded = _llama.loaded_model() or "(none)"
            loaded_name = loaded.split("\\")[-1].split("/")[-1] if loaded != "(none)" else loaded
            side_configured = bool(STATE.vision_llama and STATE.vision_llama != LLAMA)
            side_note = (
                " A separate vision server is configured but is not answering."
                if side_configured else
                " No separate vision server is configured."
            )
            return self._send_json(400, {"error":
                f"This model can't see images. The loaded model "
                f"'{loaded_name}' has no active vision projector (mmproj)."
                f"{side_note}\n\n"
                f"Fix: load a vision-capable GGUF (one with a sibling "
                f"mmproj-*.gguf, e.g. Qwen2.5-VL, LLaVA, MiniCPM-V) and "
                f"point Settings → 'Vision projector (mmproj)' at the "
                f"projector file. Then relaunch the model from Settings."
            })
        if images and not vision_native:
            descriptions = []
            for i, img in enumerate(images):
                desc = describe_image(img, hint=user_text)
                descriptions.append(f"[image {i + 1} — transcribed by vision model]\n{desc}")
            vision_block = "\n\n".join(descriptions)
            user_text = (user_text + "\n\n" + vision_block).strip() if user_text else vision_block

        chats = get_chats()
        if chat_id not in chats["chats"]:
            chats["chats"][chat_id] = {
                "id": chat_id,
                "title": _title_from_prompt(user_text),
                "created": int(time.time()),
                "updated": int(time.time()),
                "messages": [],
            }
            # Tag notch-born sessions so the main app's sidebar recognizes and surfaces a
            # conversation it never created.
            if str(body.get("surface") or "").strip().lower() == "notch":
                chats["chats"][chat_id]["origin"] = "island"
            if chat_id in chats["order"]:
                chats["order"].remove(chat_id)
            chats["order"].insert(0, chat_id)
        chat = chats["chats"][chat_id]
        # An untagged session receiving notch turns is tagged too, so the sidebar
        # treatment survives a bridge restart; an explicit origin
        # (github/project/security) is never rewritten.
        if (str(body.get("surface") or "").strip().lower() == "notch"
                and not chat.get("origin")):
            chat["origin"] = "island"
        # remember the mode this chat was last used in so the client can
        # restore it on session switch
        chat["last_mode"] = mode
        # The sidebar row and the reply header name the model that ran the
        # session; the duration feeds "worked for" / "Finished" on reload.
        _turn_started_at = time.time()
        _turn_model = _turn_model_label()
        if _turn_model:
            chat["last_model"] = _turn_model
        research_id = ""
        if research_request:
            try:
                notebook = (_research_store.resume(resume_research_id, chat_id) if resume_research_id
                            else _research_store.start(chat_id, research_request))
            except (ValueError, OSError) as error:
                return self._send_json(400, {"error": str(error)})
            research_id = notebook["id"]
            chat["research_id"] = research_id
        # regenerate: drop the last assistant turn and re-run on the same prior user
        # message (only valid when the latest message is an assistant reply).
        if regenerate:
            # Pop the whole prior agentic tail (final assistant plus every intermediate
            # assistant and tool message) back to the last user turn, so the re-run starts
            # from exactly that message.
            while chat["messages"] and chat["messages"][-1].get("role") in ("assistant", "tool"):
                chat["messages"].pop()
            if not chat["messages"] or chat["messages"][-1].get("role") != "user":
                save_json(CHATS_FILE, chats)
                return self._send_json(400, {"error": "nothing to regenerate"})
            if user_text:
                # Edit & resend: replace the stored user turn's text; stored metadata
                # (_research_brief, images) stays attached.
                chat["messages"][-1]["content"] = user_text
            user_text = chat["messages"][-1].get("content", "")
        else:
            # auto-name any chat still using the default placeholder when its first
            # user message comes in
            is_first_user_msg = not any(m.get("role") == "user" for m in chat.get("messages", []))
            if is_first_user_msg and chat.get("title", "").strip().lower() in ("", "new session", "new conversation"):
                chat["title"] = _title_from_prompt(user_text)
                broadcast_event({"type": "chat:rename", "chat_id": chat_id, "title": chat["title"]})
            user_msg: dict = {"role": "user", "content": user_text, "t": int(time.time())}
            if research_request:
                user_msg["_research_brief"] = research_request
            if body.get("invisible"):
                user_msg["invisible"] = True
            if vision_native:
                # Stored as data URLs so replay on later turns (or after a reload) reattaches
                # them; clamped once here so storage and every replay stay bounded.
                user_msg["images"] = [_clamp_image_data_url(u) for u in images]
            chat["messages"].append(user_msg)
            STATE.last_user_text = user_text
            if not body.get("invisible"):
                # Only words the user typed may authorize an action (see _concern_authorized).
                _note_user_words(chat_id, user_text)
            _update_task_anchor(chat, user_text)
        client_ctx["_user_text"] = user_text
        chat["updated"] = int(time.time())
        save_json(CHATS_FILE, chats)

        # /goal latch: apply an explicit on/off from this message, otherwise rehydrate
        # the persisted latch, so follow-up turns without /goal stay hands-free until
        # the user turns it off.
        # The turn saves its own copy of the chat later, so it carries the goal too.
        if goal_action == "on":
            chat["goal_mode"] = True
            if user_text:
                chat["goal_text"] = _goal_summary(user_text)
                chat["goal_started"] = int(time.time())   # a new task restarts the clock
                chat["goal_limit_s"] = _goal_time_limit(user_text)
            elif not chat.get("goal_started"):
                chat["goal_started"] = int(time.time())
            _apply_goal_latch(chat_id, True, chat.get("goal_text"))
        elif goal_action == "off":
            chat["goal_mode"] = False
            chat.pop("goal_text", None)
            chat.pop("goal_started", None)
            chat.pop("goal_limit_s", None)
            _apply_goal_latch(chat_id, False)
        elif chat.get("goal_mode"):
            _set_goal_mode(chat_id, True)
        goal_active_now = _chat_in_goal_mode(chat_id)

        # An explicit request to operate a desktop app unlocks the desktop bundle
        # right away instead of making a small model discover it (and drift toward
        # PowerShell).
        if (get_settings().get("desktop_enabled")
                and _user_asked_desktop_interaction(user_text)):
            _unlock_bundle(chat_id, "desktop")
        # The same for reverse engineering, interactive programs and git.
        try:
            _auto_unlock_bundles(chat_id, user_text)
        except Exception as _bundle_error:
            print(f"[tools] bundle auto-load failed: {_bundle_error}", file=sys.stderr)

        # IDE mode keeps tools available ("save that", "read this file"). The IDE
        # prompt is what stops the model wrapping every HTML reply in write_file;
        # disabling tools entirely leaves it confused about what it may do.
        use_tools = True
        # Gemma emits a python-shaped call (```tool_code``` + name(arg=value)) that
        # llama.cpp's native parser can't decode; with the `tools` array on the wire
        # the call silently no-ops and the model narrates fake success. So Gemma runs
        # text-tools mode: tools are described in the system prompt and
        # extract_tool_calls (case 7b) parses the reply. Every model id (settings
        # name, configured path, loaded model) is checked so a stale field can't
        # misroute. See the in-app FAQ.
        native_tools = True
        tools_off_reason = ""
        _s = get_settings()
        _model_ids = (_s.get("model"), _s.get("model_path"), _llama.loaded_model())
        if use_tools and (any(_is_gemma_model(m) for m in _model_ids)
                          or _native_tools_broken()):
            native_tools = False
        # Rolling summary: fold the oldest un-kept turns into a compact summary
        # instead of letting truncate_messages delete them, so the model keeps the
        # lessons of earlier fixes. The extra model call only fires once the
        # conversation nears the context limit; on failure the prior summary stays.
        _pre_turn_compaction_failed = False
        if get_settings().get("summarize_history", True):
            try:
                _ctx_for_summary = _llama_props_ctx() or int(get_settings().get("num_ctx") or 32768)
                # Force the fold when the last round had to trim: the drop is the overflow
                # signal (the 0.85 trigger can't fire once the trimmer caps fill below it).
                _drop_force = _last_dropped_by_chat.get(chat_id, False)
                if _drop_force:
                    # A mid-turn fold has usually recovered that overflow already, and forcing
                    # anyway would compact the very next message at 10-20% fill. Only force while
                    # the prompt is still near the ceiling (above ~70% of the trigger).
                    _fill_now = _last_prompt_tokens_by_chat.get(chat_id, 0)
                    if _fill_now > 0 and _fill_now < _summary_trigger_limit(_ctx_for_summary) * 0.7:
                        _drop_force = False
                _log_compact(chat_id, f"pre_turn web: fill={_last_prompt_tokens_by_chat.get(chat_id, 0)} "
                                      f"trigger={_summary_trigger_limit(_ctx_for_summary)} ctx={_ctx_for_summary} "
                                      f"drop_flag={_last_dropped_by_chat.get(chat_id, False)} force={_drop_force}")
                _pre_turn_fold_required = (
                    _drop_force or
                    _last_prompt_tokens_by_chat.get(chat_id, 0) >=
                    _summary_trigger_limit(_ctx_for_summary)
                )
                _failure_seq_before = _summary_failure_seq_by_chat.get(chat_id, 0)
                if _maybe_roll_summary(chat, _ctx_for_summary,
                                       force=_drop_force,
                                       reason_hint="pre_turn"):
                    save_json(CHATS_FILE, chats)
                elif (_summary_failure_seq_by_chat.get(chat_id, 0) > _failure_seq_before or
                      (_pre_turn_fold_required and
                       _summary_failure_backoff_active(chat_id))):
                    _pre_turn_compaction_failed = True
            except Exception:
                traceback.print_exc()
        # Durable mission state (authorized red-team runs): seed the objective from
        # the initiating instruction, saved here so run_chat_turn's snapshot sees it.
        if get_settings().get("red_team_enabled") and get_settings().get("rt_mission_state", True):
            try:
                # Scope/auth panel data (Step 3) applied first so its explicit
                # objective wins over the auto-seed from the run instruction.
                _dirty = (_rt_mission_apply_panel(chat, mission_panel)
                          if isinstance(mission_panel, dict) else False)
                if _rt_authorized_mission(chat) and _rt_mission_seed(chat, user_text):
                    _dirty = True
                if _dirty:
                    save_json(CHATS_FILE, chats)
            except Exception:
                traceback.print_exc()
        # Native-tools turns carry the full JSON tool schemas, so the abbreviated
        # prompt list would duplicate them (~1.5K tokens); text-tools mode (Gemma)
        # needs it. The prompt is built here on the handler thread, so pin the chat
        # first, or the list falls back to core-only and misses bundles unlocked
        # earlier in the session.
        _set_current_chat(chat_id)
        system_prompt = build_system_prompt(include_tools=use_tools, chat_mode=mode,
                                            include_tool_list=not native_tools)
        system_prompt += "\n\n" + _client_context_prompt(client_ctx)
        if not research_id:
            system_prompt += _security_investigation_prompt(chat)
        if research_id:
            system_prompt += "\n\n" + RESEARCH_PROMPT
            system_prompt += "\nRESEARCH BRIEF (user-provided context):\n" + json.dumps(research_request, ensure_ascii=False)
            if resume_research_id:
                system_prompt += "\nResume the saved notebook. Read research_notebook(), then page through its notes. Preserve existing evidence and question numbers; do not restart collection. Resolve gaps and publish the presentation."
        # Mission block + pins are not spliced here: they change mid-session and
        # would invalidate llama.cpp's cached prompt prefix. run_chat_turn appends
        # them after the history instead. Only slow-changing sections stay in the
        # system prompt: the rolling summary and the static pin_note nudge below.
        _roll = chat.get("rolling_summary", "")
        if _roll:
            system_prompt += (
                "\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; "
                "treat these as established facts — do not redo work or re-ask) ===\n" + _roll
            )
        if use_tools:
            system_prompt += (
                "\n\nWhen you fix a bug, settle a design decision, or the user states a constraint, "
                "call pin_note to lock it in for this session so you never undo it or repeat the mistake."
            )
        # /goal hands-free run: tell the model to finish without pausing for
        # confirmation. Approvals auto-pass except deletions/destructive actions,
        # which the bridge still gates.
        if goal_active_now:
            system_prompt += (
                "\n\nGOAL MODE (hands-free): the user launched this task with /goal and wants it carried "
                "to completion autonomously. Do not stop to ask whether you may proceed with ordinary "
                "steps (reading, writing files, running commands, running tests) — just do them and keep "
                "going until the whole task is finished. The system auto-approves non-destructive actions; "
                "deletions and destructive actions still require the user, so when one is truly needed, do "
                "it via the proper tool (the user will be asked) and continue. Report only when done or genuinely blocked."
            )
        # Text-tools mode: pin the exact shape we can parse, in the model family's own
        # dialect, and never claim a tool ran unless a result came back.
        if not native_tools:
            system_prompt += _text_tools_prompt(any(_is_gemma_model(m) for m in _model_ids))
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        # Replay the full stored history, including intermediate assistant turns
        # (with tool_calls) and tool results from prior agentic loops, so the model
        # resumes with its working memory instead of a bubble-only transcript.
        # On the vision-native path the model re-sees images on every replay; on the
        # text-only path descriptions were inlined into user_text at append time.
        replay_vision = _llama.is_vision_capable()
        # Replay only turns not yet folded into the rolling summary (the summary
        # stands in for them). The mapping lives in _replay_wire_msg so the mid-turn
        # fold rebuilds the exact same wire shape.
        _replay_from = int(chat.get("summary_through", 0) or 0)
        for m in chat["messages"][_replay_from:]:
            out = _replay_wire_msg(m, replay_vision)
            if out is not None:
                msgs.append(out)
        msgs = _ensure_user_anchor(msgs, chat["messages"], _replay_from)

        # set up SSE response — Connection: close so the browser reader resolves done
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self._set_cors()
        self.end_headers()

        final_sent = False

        def emit(evt: dict):
            nonlocal final_sent
            if evt.get("type") == "final":
                final_sent = True
            if research_id and evt.get("type") == "final":
                evt["message"]["_research"] = _research_store.finish(research_id, chat_id)
            evt = {**evt, "chat_id": chat_id}
            # The shared bus first. Once the requesting page has gone (Stop aborts
            # its fetch, a tab closes) the write below raises, and an event put on
            # the bus only after it never reached the other surfaces. That lost
            # chat_end while a few late deltas still went out, so a page mirroring
            # the turn waited for an end that never came and sat in "streaming".
            broadcast_event(evt)
            if evt.get("type") == "chat_start":
                self._turn_bus = {"chat_id": chat_id, "ended": False}
            elif evt.get("type") == "chat_end" and self._turn_bus:
                self._turn_bus["ended"] = True
            self.wfile.write(b"data: ")
            self.wfile.write(json.dumps(evt, ensure_ascii=False).encode("utf-8"))
            self.wfile.write(b"\n\n")
            self.wfile.flush()

        emit({"type": "chat_start", "chat_id": chat_id})
        # Reflect the hands-free state on every surface for this turn. The latch
        # persists across turns (see _apply_goal_latch); only "/goal off" or the chip
        # clears it. Announce once when this message turned it on.
        emit({"type": "goal_mode", "active": goal_active_now,
              "goal": str(chat.get("goal_text") or "") if goal_active_now else ""})
        if goal_action == "on":
            emit({"type": "notice", "note":
                  "Goal mode on — running this chat hands-free. Deletions and destructive actions still ask."})
        if research_id:
            emit({"type": "research_update", "research": _research_store.get(research_id, chat_id)})
        if _pre_turn_compaction_failed:
            emit({"type": "error", "error": _COMPACTION_STOP_MESSAGE})
            emit({"type": "chat_end", "web_focused": _web_ui_focused()})
            return
        if tools_off_reason:
            emit({"type": "tools_unavailable", "message": tools_off_reason})
        tok = _current_chat_id.set(chat_id)
        if research_id:
            _research_runs[chat_id] = research_id
        try:
            final = run_chat_turn(chat_id, msgs, use_tools=use_tools, emit=emit,
                                  native_tools=native_tools,
                                  reasoning_effort=reasoning_effort)
        except Exception as e:
            traceback.print_exc()
            try:
                emit({"type": "error", "error": str(e)})
            except Exception:
                return
            final = None
        finally:
            _current_chat_id.reset(tok)
            _research_runs.pop(chat_id, None)

        if research_id:
            research_result = _research_store.finish(research_id, chat_id)
            if final is None:
                final = {"role": "assistant", "content": "Research ended before a presentation could be completed.", "_failed": True}
            final["_research"] = research_result
            emit({"type": "research_update", "research": research_result})

        # Several run_chat_turn paths return a reply without emitting `final`
        # themselves: a cancel at the top of a round, a cancel mid-stream, and the
        # catch-all that salvages a turn after an exception. The reply is persisted
        # below, but without this the client would see chat_start ... chat_end with
        # nothing between. Announce whatever came back, once.
        if final and not final_sent:
            try:
                # Only what a surface renders — the salvaged dict still carries
                # the tool-call working memory and stats we persist below.
                emit({"type": "final", "message": {
                    k: v for k, v in final.items()
                    if k in ("role", "content", "_stopped", "_failed", "_build", "_stats")
                }})
            except Exception:
                pass

        if final:
            chats = get_chats()
            if chat_id in chats["chats"]:
                chat = chats["chats"][chat_id]
                _completed_turn_id = str(final.pop("_turn_id", "") or "")
                # Persist the agentic loop's working memory (tool calls, results,
                # intermediate assistant messages) before the final bubble. Assistants are
                # marked _internal=true so the renderer skips them; they exist for replay.
                appended = final.pop("_appended_intermediate", None) or []
                # Mission state advanced during the turn (target host, confirmed-access
                # facts): merge onto the chat for the next turn. Already deduped/capped by
                # _rt_mission_note.
                _mu = final.pop("_mission_updates", None)
                if isinstance(_mu, dict):
                    chat["mission"] = _mu
                _au = final.pop("_activity_updates", None)
                if isinstance(_au, list):
                    chat["activity"] = [a for a in _au if isinstance(a, dict)][-_ACTIVITY_MAX:]
                _vu = final.pop("_verification_updates", None)
                if isinstance(_vu, list):
                    chat["verification_debt"] = [x for x in _vu if isinstance(x, dict)]
                _cu = final.pop("_continuity_updates", None)
                if isinstance(_cu, dict):
                    chat["continuity"] = _cu
                else:
                    _refresh_continuity_state(chat)
                now_t = int(time.time())
                for im in appended:
                    role = im.get("role")
                    if role not in ("assistant", "tool") and not (role == "user" and im.get("_steering_id")):
                        # the empty-retry "system" nudge isn't worth persisting
                        continue
                    persisted = {
                        "role": role,
                        "content": im.get("_steering_text", im.get("content", "")) or "",
                        "t": now_t,
                    }
                    if im.get("_steering_id"):
                        persisted["_steering_id"] = im["_steering_id"]
                        persisted["images"] = im.get("_steering_images", [])
                    if _completed_turn_id:
                        persisted["_turn_id"] = _completed_turn_id
                    if role == "assistant":
                        persisted["_internal"] = True
                        if im.get("_steer_partial"):
                            persisted["_steer_partial"] = True   # shown: the user saw it written
                        if im.get("tool_calls"):
                            persisted["tool_calls"] = im["tool_calls"]
                    elif role == "tool":
                        if im.get("tool_call_id"):
                            persisted["tool_call_id"] = im["tool_call_id"]
                        if im.get("name"):
                            persisted["name"] = im["name"]
                    chat["messages"].append(persisted)

                msg = {
                    "role": "assistant",
                    "content": final.get("content", ""),
                    "t": now_t,
                }
                if _turn_model:
                    msg["model"] = _turn_model
                msg["_worked_s"] = max(0, int(round(time.time() - _turn_started_at)))
                if _completed_turn_id:
                    msg["_turn_id"] = _completed_turn_id
                if final.get("_build"):
                    msg["_build"] = final["_build"]
                if final.get("_carry_diag"):
                    msg["_carry_diag"] = final["_carry_diag"]
                if final.get("_research"):
                    msg["_research"] = final["_research"]
                # Pictures show_image put in front of the user this turn stay with
                # the reply, so they are still there when the chat is reopened.
                _shown = _shown_images(appended)
                if _shown:
                    msg["_images"] = _shown
                if final.get("_stopped"):
                    msg["_stopped"] = True
                if final.get("_failed"):
                    msg["_failed"] = True
                stats = final.get("_stats") or {}
                if stats.get("eval_count") is not None:
                    msg["tokens"] = stats["eval_count"]
                if stats.get("prompt_eval_count") is not None:
                    msg["prompt_tokens"] = stats["prompt_eval_count"]
                chat["messages"].append(msg)
                chat["updated"] = now_t
                # Retention cap (CHAT_HISTORY_MAX) to keep the JSON file bounded; anchors
                # (system + first user) are always kept.
                _enforce_chat_retention(chat)
                html = extract_html(final.get("content", ""))
                if html:
                    entry = save_version(chat_id, html, label=user_text[:80])
                    chat["last_version"] = entry["id"]
                    try:
                        emit({"type": "version_saved", "version": entry})
                    except Exception:
                        pass
                save_json(CHATS_FILE, chats)
                _turn_journal_clear(chat_id, _completed_turn_id)
        else:
            # A transport failure after one or more completed tool calls should
            # not wait for a full application restart to recover its work.
            _recover_turn_journals()

        _changes = _undo_commit_turn()
        if _changes:
            try:
                emit({"type": "turn_changes", **_changes})
            except Exception:
                pass

        try:
            emit({"type": "chat_end", "web_focused": _web_ui_focused()})
        except Exception:
            pass


class AccurettaHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
