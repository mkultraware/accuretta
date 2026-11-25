# SPDX-License-Identifier: AGPL-3.0-only
"""
Accuretta bridge — local llama.cpp proxy + tools + approvals + static server.

One process. Serves the frontend (index.html / app.js / app.css / Design Change)
and exposes JSON/SSE endpoints for chat streaming, tool invocation, workspace,
versioning, settings, and command approvals.

Runs on 0.0.0.0:8787 so Tailscale / LAN peers can reach it from phones.
"""

from __future__ import annotations

import ast
import atexit
import codecs
from memory_store import MemoryStore, memory_kind
import ctypes
import difflib
import http.client
import json
import copy
import getpass
import math
import os
import posixpath
import random
import re
import shlex
import shutil
import stat
import contextlib
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any
import webbrowser
import base64 as _b64
import io as _io
import ipaddress
import hashlib
import secrets
import tempfile
import gzip
import socket
import struct
import ssl
import datetime
from concurrent.futures import ThreadPoolExecutor

from security_overview import SecurityOverviewService
from presence_notify import PresenceNotify
from task_state import UndoStore, file_revision
from conversation_tools import SteeringInbox, search_conversations
from deep_research import ResearchStore, RESEARCH_TOOLS, RESEARCH_PROMPT, brief as research_brief, tool_specs as research_tool_specs
import subagents
from agent_reliability import EvidenceStore, compare_responses, review_evidence, response_state, identity_fingerprint, feedback, verification_advice, digest
from cvss_v31 import base_score as cvss_base_score, normalize_cwe

from accuretta.security.approvals import (  # noqa: F401  re-export: moved 2026-11-23
    _steering,
    PENDING_DIR,
    _SOFT_AUTO_KINDS,
    _MCP_SOFT_BLOCK,
    _approval_mode,
    APPROVAL_LOG_FILE,
    _APPROVAL_LOG_MAX,
    _approval_log_lock,
    _log_gate_decision,
    _approvals,
    _approval_events,
    _approvals_lock,
    _SAFE_WRITE_KINDS,
    _GOAL_GATE_KINDS,
    _goal_mode_chats,
    _goal_mode_lock,
    _chat_in_goal_mode,
    _goal_action_is_gated,
    _announce_approval,
    _GOAL_ASK_WAIT_S,
    request_approval,
    _shell_concerns_for,
    _request_approval_core,
    _gate_reasons,
    decide_approval,
)
from network_policy import install_offline_guard
import ghidra_worker

# ---- console encoding hardening -------------------------------------------
# Redirected stdout on Windows is cp1252, so a print() holding an emoji raised
# UnicodeEncodeError and aborted whatever ran after it (inside an event handler,
# the approval decision itself). Escape unencodable characters instead;
# pythonw's None streams are left alone.
for _std_stream in (sys.stdout, sys.stderr):
    try:
        if _std_stream is not None and hasattr(_std_stream, "reconfigure"):
            _std_stream.reconfigure(errors="backslashreplace")
    except Exception:
        pass

from accuretta.core.optional_imports import _HAVE_PYAUTOGUI, pyautogui  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import Image, _HAVE_PIL  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.window_lib import _pgw, _HAVE_PGW  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.optional_imports import _HAVE_UIA, _uia  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _HAVE_PLAYWRIGHT, _sync_playwright  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.optional_imports import SquashFsImage, _HAVE_SQUASHFS  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import ELFFile, _HAVE_ELFTOOLS  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _HAVE_CAPSTONE, _capstone  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _AndroidAPK, _HAVE_ANDROGUARD  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.optional_imports import _HAVE_YARA, _yara  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _HAVE_PEFILE, _pefile  # noqa: F401  re-export: moved 2026-11-24
from accuretta.core.optional_imports import _ELFFile, _HAVE_PYELFTOOLS  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _HAVE_PYGHIDRA, _ilu  # noqa: F401  re-export: moved 2026-11-24

from accuretta.core.optional_imports import _HAVE_EVTX_RUST, _PyEvtxParser  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.optional_imports import _Evtx, _HAVE_EVTX_PY  # noqa: F401  re-export: moved 2026-11-23

from accuretta.core.optional_imports import _HAVE_DPKT, _dpkt  # noqa: F401  re-export: moved 2026-11-24


from accuretta.tools.desktop_guard import (  # noqa: F401  re-export: moved 2026-11-24
    _desktop_panic,
    _desktop_action_times,
    _desktop_action_lock,
    _chat_desktop_disabled,
    _desktop_preflight,
    _wake_screen_saver,
    _screen_saver_running,
    _desktop_rate_check,
    _grab_view,
    _gate_action,
    _remember_desktop_target,
    _capture_desktop_target,
    _reassert_desktop_target,
)


from accuretta.tools.desktop_snapshot import (  # noqa: F401  re-export: moved 2026-11-24
    _desktop_snapshot_cache,
    _desktop_snapshot_lock,
    _DESKTOP_SNAPSHOT_TTL_S,
    _DESKTOP_SNAPSHOT_CACHE_MAX,
    _uia_window,
    _desktop_cache_put,
    _desktop_cache_descriptor,
    _uia_resolve_descriptor,
    _uia_capture,
    _uia_record_key,
    _uia_diff,
)


# ---- Child process environment ---------------------------------------------
# Every program the bridge starts gets an environment built by _child_env()
# instead of a copy of the bridge's own, so API keys, tokens and passwords that
# sit in the bridge's environment never reach a child. Only the names listed
# here are copied. Names match without regard to case (Windows treats them
# that way); a trailing * matches every name with that prefix. To let one more
# variable through, add it to the right list below.

from accuretta.security.child_env import (  # noqa: F401  re-export: moved 2026-11-23
    _CHILD_ENV_BASE,
    _CHILD_ENV_PROXY,
    _CHILD_ENV_GROUPS,
    _ENV_TOOLS,
    _env_name_matches,
    _child_env,
)


from accuretta.security.tool_env import (  # noqa: F401  re-export: moved 2026-11-23
    _tool_child_env,
)


from accuretta.core.paths import ROOT, USER_DIR, DATA  # noqa: F401  re-export: moved 2026-11-23
from accuretta.agent.research_runs import (  # noqa: F401  re-export: moved 2026-11-24
    _research_store,
)
from accuretta.agent.research_runs import (  # noqa: F401  re-export: moved 2026-11-24
    _research_runs,
    _research_context,
)
from accuretta.core.versions import (  # noqa: F401  re-export: moved 2026-11-24
    VERSIONS_DIR,
    SNAPSHOTS_DIR,
    save_version,
    list_versions,
    read_version,
)
from accuretta.agent.turn_journal import (  # noqa: F401  re-export: moved 2026-11-23
    TURN_JOURNAL_DIR,
    _turn_journal_path,
    _turn_journal_clear,
    _recover_turn_journals,
    _TASK_ANCHOR_ORIGINAL_CHARS,
    _TASK_ANCHOR_CURRENT_CHARS,
    _ACTIVITY_MAX,
    _build_continuity_state,
    _refresh_continuity_state,
)
from accuretta.tools.remote_machines import (  # noqa: F401  re-export: moved 2026-11-23
    REMOTE_DIR,
    REMOTE_MACHINES_FILE,
    _REMOTE_ID_RE,
    _CLIENT_OS_VALUES,
    _load_remote_machines,
    _remote_machine,
    _sanitize_client_hint,
    _handler_client_context,
)
from accuretta.tools.remote_pairing import (  # noqa: F401  re-export: moved 2026-11-23
    REMOTE_KEYS_DIR,
    _REMOTE_USER_RE,
    _save_remote_machines,
    _find_executable,
    _tailscale_exe,
    _ssh_keygen_exe,
    _run_small,
    _tailscale_snapshot,
    _tailscale_serve_status,
    enable_tailscale_serve,
    _protect_remote_private_key,
    prepare_remote_pairing,
    _refresh_remote_machine,
)
from accuretta.tools.remote_machines import (  # noqa: F401  re-export: moved 2026-11-24
    REMOTE_BACKUPS_DIR,
)
from accuretta.tools.remote_tools import (  # noqa: F401  re-export: moved 2026-11-23
    REMOTE_STAGING_DIR,
    _remote_file_reads,
    _remote_file_reads_lock,
    _remote_staged_writes,
    _remote_staged_writes_lock,
    _normalize_remote_path,
    _confirm_remote_path,
    _selected_remote_machine,
    _REMOTE_READ_ONLY_COMMANDS,
    _remote_command_refusal,
    _remote_command_is_read_only,
    _remote_text_result,
    tool_remote_shell,
    tool_remote_list_directory,
    _remote_file_metadata,
    tool_remote_read_file,
    _remote_write_bytes,
    _remote_payload_limit,
    _remote_expected_hash,
    _remote_write_approved_payload,
    _cleanup_remote_staging,
    tool_remote_file_begin,
    _owned_remote_stage,
    tool_remote_file_append,
    tool_remote_file_commit,
    tool_remote_write_file,
    tool_remote_copy_to,
    tool_remote_copy_from,
)
from accuretta.tools.remote_ssh import (  # noqa: F401  re-export: moved 2026-11-23
    REMOTE_KNOWN_HOSTS_FILE,
    _ssh_exe,
    _remote_ssh_argv,
    _run_remote_process,
    verify_remote_pairing,
)
from accuretta.core.paths import CHATS_FILE, SETTINGS_FILE  # noqa: F401  re-export: moved 2026-11-23
from accuretta.runtime.model_config import (  # noqa: F401  re-export: moved 2026-11-23
    MODELS_CONFIG_FILE,
    _MODEL_TUNE_KEYS,
    _MODEL_TUNE_RESET_KEYS,
    _MODEL_PROJECTOR_KEYS,
    _MODEL_PREFERENCE_KEYS,
    _MODEL_CONFIG_KEYS,
    _models_config,
    _apply_model_config,
    _save_model_config,
    _proven_cfg,
)
from accuretta.core.workspace import (  # noqa: F401  re-export: moved 2026-11-23
    WORKSPACE_FILE,
    get_workspace,
    normalize_path,
    _PROJECT_MARKERS,
    _root_is_project,
    _workspace_roots,
)
from accuretta.core.savings import (  # noqa: F401  re-export: moved 2026-11-23
    SAVINGS_FILE,
    _SAVINGS_LOCK,
    _SAVINGS_DEFAULT,
    _savings_ensure_locked,
    _savings_get,
    _savings_add,
)
from accuretta.runtime.model_advisor import (  # noqa: F401  re-export: moved 2026-11-23
    MODEL_RUNTIME_FILE,
    _MODEL_RUNTIME_LOCK,
    _model_profile_key,
    _MODEL_ADVISOR_BASELINE_TURNS,
    _MODEL_ADVISOR_BASELINE_SESSIONS,
    _MODEL_ADVISOR_TRIAL_TURNS,
    _MODEL_ADVISOR_TRIAL_MAX_AGE_S,
    _MODEL_ADVISOR_CONFIG_KEYS,
    _MODEL_ADVISOR_OPTIONAL_KEYS,
    _RUNTIME_LAUNCH,
    _MODEL_ADVISOR_LABELS,
    _model_runtime_config,
    _model_config_signature,
    _HARNESS_INTERVENTION_KEYS,
    _sanitize_interventions,
    _model_observed,
    _model_runtime_profiles,
    _advisor_changes,
    _advisor_trial_public,
    _LOAD_SAMPLE_CAP,
    _LOAD_SAMPLE_MAX_AGE_S,
    _FIT_SLOW,
    _FIT_SAME,
    _FIT_CHECK_STEP,
    _FIT_IGNORED_KEYS,
    _fit_group,
    _fit_key,
    _record_load_sample,
    _fit_speeds,
    _fit_ceiling,
    _fit_ceiling_for,
    _silent_spill_possible,
    _fit_full_gpu_dense,
    _advisor_fit_advice,
    _fit_settle,
    _FIT_CTX_CAP,
    _model_is_dense,
)
from accuretta.core.errors import (  # noqa: F401  re-export: moved 2026-11-23
    ERRORS_FILE,
    _error_store,
)
from accuretta.core.data_files import (  # noqa: F401  re-export: moved 2026-11-23
    MODEL_DATASET_FILE,
    PUSHED_REPOS_FILE,
    FOLD_EVENTS_FILE,
)
from accuretta.agent.usage_stats import (  # noqa: F401  re-export: moved 2026-11-23
    _usage_stats,
)
from accuretta.core.updates import (  # noqa: F401  re-export: moved 2026-11-23
    UPDATE_CHECK_FILE,
    _UPDATE_CHECK_LOCK,
    _UPDATE_AUTO_TTL_S,
    _UPDATE_MANUAL_COOLDOWN_S,
    _UPDATE_ERROR_RETRY_S,
    _UPDATE_REPO,
    _UPDATE_API_URL,
    _UPDATE_RELEASES_URL,
    _app_version,
    _version_key,
    _release_summary,
    _installer_info,
    _INSTALLER_ASSET_RE,
    _update_response,
    app_update_status,
)
from accuretta.agent.system_prompt import (  # noqa: F401  re-export: moved 2026-11-24
    SYSTEM_CONTEXT_FILE,
    _active_tools,
    _compact_agent_core,
    _text_tool_lines,
    build_system_prompt,
)
from accuretta.core.memories import (  # noqa: F401  re-export: moved 2026-11-24
    MEMORIES_FILE,
    _memory_store,
    _select_memories_for_prompt,
)
from error_log import ErrorStore


from accuretta.server.startup import (  # noqa: F401  re-export: moved 2026-11-24
    _install_error_hooks,
    _bootstrap_skill_assets,
    main,
    _main_owned,
)

from accuretta.agent.skills import (  # noqa: F401  re-export: moved 2026-11-23
    SKILLS_DIR,
    SKILL_BODY_CAP_CHARS,
    SKILL_MAX_BODY_CHARS,
    _FRONTMATTER_RE,
    _SKILL_NAME_RE,
    _SKILLS_CACHE,
    _parse_skill_frontmatter,
    _scan_skills,
    _skills_catalog,
    _read_skill_body,
    _active_skill_section,
)


from accuretta.core.safe_fs import (  # noqa: F401  re-export: moved 2026-11-23
    safe_exists,
    safe_is_dir,
    safe_rglob_files,
)


from accuretta.core.tool_stream import (  # noqa: F401  re-export: moved 2026-11-23
    CMD_HISTORY_FILE,
    CMD_HISTORY_MAX,
    _execution_tool_name,
    _emit_execution_event,
    _execution_not_run,
    _GITHUB_TOKEN_RE,
    _mask_github_tokens,
    _emit_tool_stream,
    _STREAM_BINARY_RUNS,
    _looks_binary,
    _append_cmd_history,
    _read_cmd_history,
)
from accuretta.redteam.mission_logs import (  # noqa: F401  re-export: moved 2026-11-23
    ACTION_AUDIT_FILE,
    _RT_UNKEPT_LOGS_FILE,
    _RT_UNKEPT_LOCK,
    _rt_keeps_log,
    _rt_filter_jsonl,
    _rt_log_purge,
    _rt_log_mission_ended,
    _rt_log_sweep,
    _ACTION_AUDIT_LOCK,
)
from accuretta.agent.action_audit import (  # noqa: F401  re-export: moved 2026-11-23
    ACTION_AUDIT_MAX,
    _DESKTOP_TOOL_NAMES,
    _WRITE_CAPABLE_TOOLS,
    _ACTION_AUDIT_ALWAYS,
    _is_audited_action_tool,
    _action_audit_target,
    _record_action_audit,
)
from accuretta.core.ignore_rules import (  # noqa: F401  re-export: moved 2026-11-23
    IGNORE_FILE_NAME,
    _IGNORE_CACHE,
    _read_ignore_rules,
    _workspace_root_for,
    resolve_workspace_file,
    is_ignored,
)


# Which chat the current tool call belongs to, so the per-chat kill switch can
# short-circuit desktop tools without threading chat_id through every function.
# Set on the worker thread before run_chat_turn and cleared after.
import contextvars
from accuretta.core.chats import (  # noqa: F401  re-export: moved 2026-11-23
    _current_chat_id,
    _current_rt_chat,
    _current_tool_name,
    _current_tool_call_id,
    get_chats,
    _chat_ctx,
    _chat_cv,
    _get_current_chat,
)

from accuretta.core.chat_events import (  # noqa: F401  re-export: moved 2026-11-23
    _chat_emitters,
    _emit_chat_event,
)
from accuretta.agent.turn_records import (  # noqa: F401  re-export: moved 2026-11-24
    _chat_live_activity,
    _chat_live_activity_lock,
    _last_dropped_by_chat,
    _turn_end_reason,
    _tool_error_kind,
    _turn_model_label,
    _turn_journal_begin,
    _turn_journal_checkpoint,
    _update_task_anchor,
    _tool_activity_record,
    _validate_summary_output,
    _context_trim_last_notice_by_chat,
    _chat_note,
)

from accuretta.runtime.llama_process import (  # noqa: F401  re-export: moved 2026-11-23
    _last_prompt_tokens_by_chat,
    _CTX_EST_CACHE,
    _REASONING_CAP_CACHE,
    _llama_props_data,
    _llama_props_ctx,
    _llama_props_ctx_invalidate,
    _rt_trailing_system_ok,
    _tools_spec_overhead_tokens,
    _watchdog_self_heal,
    _child_job,
    LlamaProcess,
    _llama,
)

from accuretta.core.cancels import (  # noqa: F401  re-export: moved 2026-11-23
    _chat_cancels,
    _chat_cancels_lock,
)


from accuretta.core.cancels import (  # noqa: F401  re-export: moved 2026-11-24
    _register_cancel,
    _set_cancel_resp,
    _unregister_cancel,
)


from accuretta.runtime.command_kill import (  # noqa: F401  re-export: moved 2026-11-24
    cancel_chat,
)

from accuretta.core.tool_pool import (  # noqa: F401  re-export: moved 2026-11-24
    _tool_executor,
    _tool_jobs,
    _tool_jobs_lock,
)


for d in (
    DATA, VERSIONS_DIR, PENDING_DIR, SNAPSHOTS_DIR, TURN_JOURNAL_DIR,
    REMOTE_DIR, REMOTE_KEYS_DIR, REMOTE_BACKUPS_DIR,
):
    d.mkdir(parents=True, exist_ok=True)

from accuretta.core.endpoints import (  # noqa: F401  re-export: moved 2026-11-23
    _resolve_llama_url,
    LLAMA,
    PORT,
)

# Values changed while running and read across areas (vision server address,
# the user's last words, the last prompt size, the desktop target) live on
# STATE, so code split out of this file always sees the current value.
from accuretta.core.state import STATE  # noqa: E402

# ---- persistence helpers ---------------------------------------------------

from accuretta.core.persistence import (  # noqa: F401  re-export: moved 2026-11-23
    _FILE_LOCK,
    _CHATS_TX_LOCK,
    _ChatSnapshot,
    _chat_payload_digest,
    _normalize_composer_mode,
    _normalize_chats_data,
    _write_json_atomic_unlocked,
    _save_chats_snapshot,
    load_json,
    save_json,
)


from accuretta.server.http_handler import (  # noqa: F401  re-export: moved 2026-11-24
    _shutdown_ready,
    _WS_FILE_MIME,
    _WS_FILE_MAX_BYTES,
    _web_ui_focused,
    MIME,
    STATIC_WHITELIST,
    _REQUEST_TOKEN,
    _MAX_JSON_BYTES,
    _PREVIEW_SANDBOX,
    RequestError,
    _json_default,
    Handler,
    AccurettaHTTPServer,
)


from accuretta.runtime.llama_post import (  # noqa: F401  re-export: moved 2026-11-23
    _savings_note_completion,
    llama_post,
)


from accuretta.runtime.model_observation import (  # noqa: F401  re-export: moved 2026-11-23
    _MODEL_ADVISOR_OBSERVATION_CAP,
    _record_model_observation,
    _sanitize_tool_outcomes,
    _TOOL_ERROR_PATTERNS,
    _TOOL_ERROR_KINDS,
    _TURN_END_REASONS,
    _append_model_dataset,
    _client_context_by_chat,
)
from accuretta.runtime.model_health import (  # noqa: F401  re-export: moved 2026-11-23
    _MODEL_ADVISOR_APPLY_KEYS,
    _fit_action,
    _model_health_action,
)


from accuretta.runtime.model_health_report import (  # noqa: F401  re-export: moved 2026-11-24
    _advisor_suggest_settings,
    _advisor_recommendation,
    _public_model_health,
)


from accuretta.runtime.launch_helpers import (  # noqa: F401  re-export: moved 2026-11-23
    _LOAD_PROBE_PROMPT,
    _LOAD_PROBE_TOKENS,
    _measure_load_speed,
    _mmproj_on_cpu,
    _runtime_spec_strategy,
    _no_mmap_flags,
    _stamp_proven_boot,
)


from accuretta.core.settings import (  # noqa: F401  re-export: moved 2026-11-23
    DEFAULT_SETTINGS,
    _SHORTCUT_ACTIONS,
    _SHORTCUT_MODIFIERS,
    _SHORTCUT_KEY_RE,
    _normalized_keyboard_shortcuts,
    _normalized_mmproj_mode,
    get_settings,
    _offline_active,
)


from accuretta.runtime.settings_update import (  # noqa: F401  re-export: moved 2026-11-23
    _SETTINGS_LOCK,
    update_settings,
    _update_settings_locked,
    _SAMPLING_KEYS,
    _model_sampling_profile,
    _model_sampling_remember_edit,
)


from accuretta.runtime.auto_tune import (  # noqa: F401  re-export: moved 2026-11-23
    _AUTOTUNE_KV_CHOICES,
    _QUANT_BPW,
    _quant_bpw,
    _running_instance_mem_mb,
    _win_gpu_tree_mem_mb,
    _win_gpu_process_mem_mb,
    _WIN_GPU_MEM_CACHE,
    _free_vram_gb_for_tune,
    _ram_bandwidth_gbps,
    _DECODE_BW_EFFICIENCY,
    _CPU_DECODE_EFFICIENCY,
    _SPEC_GAIN_MOE,
    _SPEC_GAIN_DEFAULT,
    _MTP_LEN_FACTOR,
    _typical_fill_tokens,
    _effective_gpu_bw,
    _decode_tok_s,
    _decode_observations,
    _decode_calibration,
    _draft_length_choice,
    _FIT_PARAMS_CACHE,
    _fit_params_measure,
    auto_tune,
    _MAYA_DRAFT_MIN_CTX,
    _maya_setup_gib,
    _maya_auto_tune,
)


from accuretta.tools.remote_helpers import (  # noqa: F401  re-export: moved 2026-11-23
    _public_remote_machine,
    _normalize_pairing_roots,
    _resolved_remote_roots,
    _text_on_wire,
)


from accuretta.agent.system_prompt import (  # noqa: F401  re-export: moved 2026-11-24
    _client_context_prompt,
    ensure_system_context,
    rescan_system_context,
)


from accuretta.tools.remote_pairing import (  # noqa: F401  re-export: moved 2026-11-24
    remove_remote_pairing,
)


from accuretta.tools.folders import (  # noqa: F401  re-export: moved 2026-11-24
    _SUBFOLDER_LIST_CAP,
    _list_subfolders,
    _path_in_workspace,
    _open_folder_in_file_manager,
)


from accuretta.agent.chat_index import (  # noqa: F401  re-export: moved 2026-11-24
    get_chat_index,
    _chat_task_state,
    CHAT_HISTORY_MAX,
    _enforce_chat_retention,
    _STOPWORDS,
    _title_from_prompt,
)


from accuretta.agent.workbench_status import (  # noqa: F401  re-export: moved 2026-11-23
    _WORKBENCH_CACHE,
    _workbench_cached,
    _tokens_today,
    _workbench_status,
    _workspace_git_status,
    _ACTIVE_MCP_CLIENTS,
)


from accuretta.agent.compaction import (  # noqa: F401  re-export: moved 2026-11-23
    _TASK_ACK_RE,
    _task_anchor_text,
    _CONTINUITY_VERIFY_PATHS,
    _render_continuity_state,
    _SUMMARY_REQUEST_CHARS,
    _SUMMARY_MAX_OUTPUT_TOKENS,
    _COMPACTION_ORIGINAL_USER_CHARS,
    _COMPACTION_RECENT_USER_CHARS,
    _COMPACTION_RECENT_USER_MESSAGES,
    _COMPACTION_ANCHOR_MAX,
    _COMPACTION_ANCHOR_CHARS,
    _COMPACTION_SENSITIVE_KEY_RE,
    _COMPACTION_KEY_VALUE_RE,
    _COMPACTION_WINDOWS_PATH_RE,
    _COMPACTION_POSIX_PATH_RE,
    _COMPACTION_URL_RE,
    _COMPACTION_SHA_RE,
    _COMPACTION_ISSUE_RE,
    _compaction_message_text,
    _compaction_excerpt,
    _safe_compaction_url,
    _ANCHOR_VALUE_JUNK_CHARS,
    _looks_like_anchor_value,
    _hex_is_color_or_uuid,
    _extract_compaction_anchors,
    _anchor_is_worth_keeping,
    _bounded_compaction_anchors,
    _build_compaction_memory,
    _compaction_render_limits,
    _render_anchor_subset,
    _render_compaction_memory,
    _summary_request_chars,
    _SUMMARY_KEEP_COMPLETED,
    _SUMMARY_KEEP_FILES,
    _SUMMARY_KEEP_CHECKS,
    _SUMMARY_SECTION_MAX_CHARS,
    _SUMMARY_BULLET_MAX_CHARS,
    _SUMMARY_STALE_NEXT,
    _SUMMARY_REQUIRED_HEADINGS,
    _summary_output_budget,
    _summary_request_prefix,
    _SUMMARY_WRITE_TOOLS,
    _SUMMARY_READ_TOOLS,
    _SUMMARY_FILE_ARG_KEYS,
    _SUMMARY_CMD_ARG_KEYS,
    _SUMMARY_NOISE_PREFIXES,
    _summary_norm,
    _summary_bullets,
    _merge_summary_bullets,
    _summary_file_key,
    _summary_cap_section,
    _summary_result_failed,
    _summary_outcome,
    _summary_cmd_digest,
    _summary_clip,
    _summary_tool_facts,
    _assemble_summary,
    _SUMMARY_STICKY_HEADINGS,
    _SUMMARY_EMPTY_SECTION_RE,
    _FIDELITY_MAX_REINJECT_CHARS,
    _split_summary_sections,
    _section_is_empty,
    _reinject_under_heading,
    _reconcile_summary_fidelity,
)


from accuretta.agent.context_estimate import (  # noqa: F401  re-export: moved 2026-11-24
    _render_activity_tail,
    _TOOL_RESULT_KEEP_RECENT,
    _estimate_context_tokens,
    _security_investigation_prompt,
    _is_gemma_model,
)


from accuretta.agent.token_budget import (  # noqa: F401  re-export: moved 2026-11-23
    CHARS_PER_TOKEN,
    _approx_tokens,
    _count_msg_tokens,
    truncate_messages,
    _STATE_NOTE_SLOW_PREFIXES,
    _STATE_NOTE_REFRESH_EVERY,
    _STATE_NOTE_FULL_EVERY,
    _STATE_NOTE_KEEP_TOKENS,
    _StateNotes,
)


from accuretta.agent.history_fold import (  # noqa: F401  re-export: moved 2026-11-24
    _payload_msg_cost,
    _SUMMARY_MIN_KEEP,
    _SUMMARY_MIN_KEEP_HARD,
    _SUMMARY_TRIGGER_FRAC,
    _SUMMARY_KEEP_FRAC,
    _summary_trigger_limit,
    _SUMMARY_FAIL_BACKOFF_S,
    _summary_last_fail_by_chat,
    _summary_failure_seq_by_chat,
    _SUMMARY_NOTICE_INTERVAL_S,
    _summary_last_notice_by_chat,
    _summary_fold_lock,
    _summary_folds_inflight,
    list_inflight_folds,
    _summary_failure_backoff_active,
    _fold_events_lock,
    _record_fold_event,
    _align_fold_cut,
    _maybe_roll_summary_unlocked,
    _maybe_roll_summary,
    _SUMMARY_SECTION_RE,
    _splice_rolling_summary,
    _mid_turn_fold,
    _compact_chat,
    tool_compact_history,
)


from accuretta.agent.ctx_stats import (  # noqa: F401  re-export: moved 2026-11-24
    _ctx_prompt_tokens,
    _ctx_breakdown,
)


from accuretta.runtime.llama_http import (  # noqa: F401  re-export: moved 2026-11-23
    _conversation_token_scale,
    llama_get,
    _llama_tokenize,
    recommended_settings,
    llama_ping,
    llama_ready,
    wait_for_llama,
)


from accuretta.agent.rolling_summary import (  # noqa: F401  re-export: moved 2026-11-23
    _SUMMARY_MIN_OUTPUT_CHARS,
    _SUMMARY_INSTR,
    _SUMMARY_MODEL_HEADINGS,
    _SUMMARY_MAX_CHUNKS,
    _SUMMARY_ALL_HEADINGS,
    _validate_model_summary,
    _update_rolling_summary,
    _summarize_slice,
    _SUMMARY_CALL_TIMEOUT_S,
    _COMPACT_DEBUG_FILE,
    _compact_debug_lock,
    _log_compact,
)
from accuretta.agent.turn_notes import (  # noqa: F401  re-export: moved 2026-11-24
    _COMPACTION_STOP_MESSAGE,
    _ROUND_LIMIT_ANSWER_NOTE,
    _STEER_CONTINUE_NOTE,
    _with_steer_continue_note,
    _MAYA_LOOP_STOP_NOTE,
    _maya_helper_followup,
)


from accuretta.core.visible_reply import (  # noqa: F401  re-export: moved 2026-11-23
    _CHAT_CODE_FENCE_RE,
    _visible_assistant_text,
    _live_turn_messages,
    _recent_visible_code_blocks,
    _CODE_FAMILIES,
    _CODE_FAMILY_OF,
    _code_only_in_thinking,
    _visible_code_source,
    _THINK_MARKER_RE,
    _close_think_marker,
    _LEADING_THINK_RE,
    _split_leading_think,
)
from accuretta.agent.visible_text import (  # noqa: F401  re-export: moved 2026-11-23
    _call_digest,
    _render_msgs_for_summary,
    _take_summary_chunk,
    _visible_final_answer,
    _steer_kept_partial,
    _strip_unparsed_tool_markup,
    _is_invalid_call_result,
    _drop_invalid_call_pairs,
    _graceful_server_failure,
    _PRIOR_THINK_RE,
    _SYNTHETIC_USER_PREFIXES,
    _is_task_boundary,
    _strip_prior_thinking,
    _prior_thinking_mode,
    _thinking_wire_view,
)


# ---- the bridge's half of the fold note ------------------------------------


# ---- compaction telemetry ---------------------------------------------------
# One JSONL line per compaction attempt in data/fold_events.jsonl: the fill at
# decision time, which path fired (auto gate / pre-turn / mid-turn / manual),
# and how many messages and tokens moved. "Plenty of headroom" non-fires are
# deliberately not recorded.


from accuretta.agent.wire_replay import (  # noqa: F401  re-export: moved 2026-11-24
    IMAGE_WIRE_MAX_EDGE,
    _clamped_image_cache,
    _CLAMPED_IMAGE_CACHE_MAX,
    _clamp_image_data_url,
    _replay_wire_msg,
)


from accuretta.agent.user_anchor import (  # noqa: F401  re-export: moved 2026-11-24
    _USER_ANCHOR_PREFIX,
    _USER_ANCHOR_MAX_CHARS,
    _ensure_user_anchor,
)


from accuretta.agent.plan_notes import (  # noqa: F401  re-export: moved 2026-11-24
    tool_pin_note,
    tool_update_plan,
    tool_compare_evidence,
    tool_observe_response,
    tool_verification_guidance,
    tool_unpin_note,
)


from accuretta.redteam.findings import (  # noqa: F401  re-export: moved 2026-11-23
    _FINDING_STATUSES,
    _evidence_context,
    _CHALLENGE_TEXT_FIELDS,
    _finding_expand_shorthand,
    _review_finding_args,
    tool_review_finding,
    _EVIDENCE_PAGE_CHARS,
    _evidence_page,
    _endpoint_key,
    tool_security_coverage,
    tool_list_findings,
)
from accuretta.redteam.findings import (  # noqa: F401  re-export: moved 2026-11-24
    _FINDING_SEVERITIES,
    _finding_artifact,
    tool_record_finding,
)


# ---- durable mission state (authorized red-team runs) ----------------------
# On long runs the mission (target, scope, objective, confirmed access) gets
# summarized away or falls out of attention. This derives it from what actually
# happens (the initiating instruction, target-touching tool calls, captured
# flags) and re-injects it, independent of pin_note. Same storage idiom as pins
# (dedup + [-N:] cap + save_json) under a separate chat key.

from accuretta.redteam.mission_flow import (  # noqa: F401  re-export: moved 2026-11-24
    _RT_MISSION_FACTS_MAX,
    _RT_ATTN_SPAN,
    _rt_target_from_args,
    _rt_frontend_secret_mission,
    _rt_close_frontend_secret_mission,
    _RT_IDENTITY_KEEP_SECONDS,
    _rt_identities_sweep,
    tool_rt_browser,
    _rt_mission_seed,
    _rt_mission_note,
    _rt_mission_set_phase,
    _rt_result_confirms_finding,
    _rt_mission_mark_tool,
    _prune_red_team_evidence,
)


from accuretta.redteam.mission import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_url_host,
    _rt_authorized_mission,
    _rt_passive_mission,
    _rt_context_chat,
    _rt_mission_get,
)


# ---- red-team scope enforcement (HARD, not advisory) -----------------------
# The mission's scope line is only a hint to the model, and models routinely
# wander onto localhost services. These guards make the tools REFUSE to touch
# accuretta's own bridge / llama.cpp ports, or any host/port the user marked
# out of scope, regardless of what the model decides.

from accuretta.redteam.network_scope import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_self_ports,
    _rt_resolves_to_local,
    _RT_NETWORK_SUFFIX_RE,
    _rt_endpoints_from_args,
    _rt_network_block,
    _RT_HOST_METADATA_TOOLS,
    _rt_metadata_parent_ok,
    _RT_SCOPE_IP_CACHE,
    _RT_SCOPE_IP_LOCK,
    _RT_SCOPE_IP_TTL,
    _rt_resolved_ips,
    _rt_metadata_ip_ok,
    _rt_allow_matches,
    _RT_AUTOMATED_TOOL_NAMES,
    _MCP_URL_RE,
    _rt_mcp_args_urls,
    _rt_scope_mcp_calls,
    _rt_scope_block,
    _rt_connect_block,
    _rt_incl_exploit_for,
    _RT_BOUNTY_HIDDEN_TOOLS,
    _RT_EXPLOIT_TOOL_NAMES,
    _RT_PASSIVE_TOOL_NAMES,
)


from accuretta.redteam.scope import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_is_local_host,
    _rt_endpoint_from_args,
    _rt_tok_matches,
    _rt_scope_token_host,
)


from accuretta.redteam.destructive import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_TARGET_ARG_KEYS,
    _RT_TARGET_LIST_KEYS,
    _rt_target_values,
    _RT_DESTRUCTIVE_PATH_RE,
    _RT_DESTRUCTIVE_WORDS,
    _RT_ACTION_QUERY_KEYS,
    _RT_DESTRUCTIVE_PAYLOAD_RE,
    _RT_DESTRUCTIVE_URL_TOOLS,
    _RT_DESTRUCTIVE_HINT,
    _rt_destructive_url_reason,
    _rt_destructive_block,
)


from accuretta.redteam.command_scope import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_scope_out_tokens,
    _rt_scope_in_tokens,
    _RT_NETWORK_CMD_RE,
    _RT_CODE_NETWORK_RE,
    _RT_CMD_FILE_EXTS,
    _RT_CMD_GENERIC_TLDS,
    _rt_command_hosts,
    _rt_command_data_block,
    _rt_command_scope_block,
    _sandbox_scope_block,
)


from accuretta.redteam.request_headers import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_HEADER_NAME_RE,
    _rt_clean_required_headers,
    _bounded_int,
    _rt_clean_user_agent,
    _rt_required_user_agent,
    _rt_apply_mission_user_agent,
    _browser_headers,
    _RT_UA_POOL,
    _rt_ua,
    _rt_stealth_headers,
    _rt_jitter,
)


from accuretta.redteam.identities import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_IDENTITY_LABELS,
    _RT_IDENTITY_ANON,
    _RT_IDENTITY_HEADER_MAX,
    _RT_IDENTITY_VALUE_MAX,
    _RT_IDENTITY_BLOCKED_HEADERS,
    _RT_IDENTITY_ALIASES,
    _RT_IDENTITIES_FILE,
    _RT_IDENTITIES_LOCK,
    _RT_IDENTITY_SEEN,
    _rt_clean_identity_headers,
    _rt_clean_identities,
    _rt_identities_usable,
    _rt_clean_owned_urls,
    _rt_identities_read,
    _rt_identities_save,
    _rt_identities_for,
    _rt_active_identities,
    _rt_identities_forget,
    _rt_identity_label,
    _rt_identities_missing_reason,
    _rt_identity_request,
    _rt_identity_reserved_headers,
)
from accuretta.redteam.recon_fetch import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_IDENTITY_SEEN_TTL,
    _rt_identity_mark_seen,
    _rt_identity_seen,
    _ScopeCheckedRedirect,
    _IdentityRedirect,
    _rt_proxy_handler,
    _ReconNoRedirect,
    _recon_fetch,
)


from accuretta.redteam.mission_panel import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_scope_tokens_from_panel,
    _RT_NOTES_MAX,
    _rt_mission_apply_panel,
    _RT_APP_STORE_HOSTS,
    _rt_program_host,
    _rt_program_fields,
)


from accuretta.redteam.recon_map_tool import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_browser_policy,
    _RT_BROWSER_WORKER,
    _MAP_HREF_RE,
    _MAP_FORM_RE,
    _MAP_ATTR_RE,
    _MAP_ACTION_RE,
    _MAP_METHOD_RE,
    _MAP_FIELD_RE,
    _MAP_JS_URL_RE,
    _MAP_JS_CALL_RE,
    _MAP_SKIP_RE,
    _MAP_JUICY_RE,
    _map_same_origin,
    _map_abs,
    _map_template,
    _map_params,
    _map_forms,
    _map_js_endpoints,
    _map_score,
    _MAP_SPEC_PATHS,
    _map_fetch_openapi,
    _map_render,
    tool_recon_map,
)


from accuretta.redteam.scoped_browser import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_browser_url_block,
    _rt_browser_session_name,
    _SAFE_CHROMIUM_ARGS,
    _SAFE_CHROMIUM_CHANNELS,
    _launch_safe_chromium,
    _ScopedBrowserWorker,
    _RT_BROWSER_ASSET_TYPES,
)


from accuretta.tools.casual_browser import (  # noqa: F401  re-export: moved 2026-11-23
    _CasualBrowserWorker,
    _page_screenshot_prompt,
    _native_vision_ready,
    _IMAGE_MAGIC,
    _sniff_image_mime,
    _normalize_image_bytes,
    _host_is_named,
    _LOOPBACK_HOSTS,
    _page_port,
    _agent_page,
    _casual_action_block,
)


from accuretta.tools.casual_browser import (  # noqa: F401  re-export: moved 2026-11-24
    _CASUAL_BROWSER_WORKER,
    _URL_HOST_RE,
    _BARE_HOST_RE,
    _NOT_TLDS,
    _hosts_in_text,
    _user_named_hosts,
    tool_web_render,
)


from accuretta.redteam.js_secrets_tool import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_DISCOVERED_SECRET_LOCK,
    _RT_DISCOVERED_SECRET_VALUES,
    _rt_secret_guard_id,
    _rt_remember_frontend_secret_values,
    _frontend_secret_record_for_report,
    tool_scan_js_secrets,
)


from accuretta.agent.rt_tool_guards import (  # noqa: F401  re-export: moved 2026-11-24
    _rt_args_contain_discovered_secret,
    _rt_discovered_secret_submission_block,
    _rt_log_note,
    _RT_SCOPE_AWARE_GENERIC_TOOLS,
    _rt_unify_target_args,
    _RT_SCOPE_AWARE_COMMAND_TOOLS,
)


from accuretta.agent.rt_program_reader import (  # noqa: F401  re-export: moved 2026-11-24
    _RT_PROGRAM_TEXT_MAX,
    _RT_PROGRAM_CALL_TIMEOUT_S,
    _RT_PROGRAM_INSTR,
    _rt_program_json,
    _rt_read_program,
)


from accuretta.redteam.playbook_table import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_PLAYBOOKS,
    _RT_PLAYBOOK_TOOLS,
)
from accuretta.agent.tool_sets import (  # noqa: F401  re-export: moved 2026-11-24
    _RT_ACCESS_HIDDEN_TOOLS,
    _SECURITY_REVIEW_TOOLS,
    _RT_RECON_TOOL_NAMES,
    _RT_PASSIVE_ALLOWED_TOOL_NAMES,
    _RED_TEAM_TOOL_NAMES,
    _RT_MISSION_HIDDEN_TOOLS,
    _CORE_TOOL_NAMES,
    _SMALL_CTX_CORE_TOOL_NAMES,
    _TINY_CTX_CORE_TOOL_NAMES,
    _BALANCED_CORE_TOOL_NAMES,
    _IDE_CORE_TOOL_NAMES,
    _TOOL_BUNDLES,
    _unlocked_bundles_by_chat,
    _base_core_tool_names,
    _unavailable_runtime_tools,
    _excluded_tools,
    _visible_tool_names,
)


from accuretta.redteam.mission_panel import (  # noqa: F401  re-export: moved 2026-11-24
    _rt_playbook_lines,
    _rt_mission_render,
    _rt_gov_usage,
)


from accuretta.redteam.playbook import (  # noqa: F401  re-export: moved 2026-11-23
    _rt_next_step,
)


# ---- system context (ACCURETTA.md) ----------------------------------------
# First-run scan of the user's machine so models know where things are.
# Not automatically readable by tools — it's injected into the system prompt.

from accuretta.agent.system_scan import (  # noqa: F401  re-export: moved 2026-11-23
    _SYSTEM_CONTEXT_TTL,
    _scan_system_context,
)


from accuretta.agent.system_context import (  # noqa: F401  re-export: moved 2026-11-23
    _render_system_context_md,
)


# ---- workspace / path safety ----------------------------------------------

from accuretta.security.path_guards import (  # noqa: F401  re-export: moved 2026-11-23
    BLOCKED_PATH_PATTERNS,
    is_blocked_path,
    DATA_PATH_ERROR,
    _is_data_dir,
    _data_file_in_walk,
    _desktop_wide_enabled,
    is_in_workspace,
    _fw_check_path,
)


from accuretta.security.data_guard import (  # noqa: F401  re-export: moved 2026-11-23
    _DATA_READABLE_SUBDIRS,
    _soft_data_text_hit,
)
from accuretta.security.data_paths import (  # noqa: F401  re-export: moved 2026-11-23
    _KEY_FILE_NAMES,
    _path_within,
    _key_files,
    is_key_file,
    is_data_path,
)


# ---- .accurettaignore -----------------------------------------------------
# gitignore-lite: each workspace folder may hold a `.accurettaignore` with one
# glob per line (blank lines and `#` comments skipped, `!` negates). Globs are
# matched with fnmatch against the basename and against the POSIX path relative
# to the workspace root, so `node_modules` catches any subtree of that name and
# `build/*.map` targets nested files.
import fnmatch


from accuretta.security.registry_writes import (  # noqa: F401  re-export: moved 2026-11-23
    _REG_SYSTEM_HIVES,
    _REG_USER_HIVES,
    _REG_WRITE_VERBS,
    _REG_OPAQUE_IMPORT,
    _REG_LAUNCH_REGEDIT,
    registry_assess,
)


from accuretta.security.registry_writes import (  # noqa: F401  re-export: moved 2026-11-24
    _notch_registry_diff,
)


# ---- bridge self-protection ------------------------------------------------
# Models iterating on a web UI sometimes kill the python process running this
# file along with the dev server they meant to restart. Approval doesn't help
# (the model has lost track of which python is which), so any tool call naming
# bridge.py, the bridge's PID, or destructive verbs against port 8787 is
# refused regardless of approval state.

from accuretta.security.self_protect import (  # noqa: F401  re-export: moved 2026-11-23
    _BRIDGE_PID,
    _BRIDGE_FILE_ABS,
    _SERVER_PACKAGE_ABS,
    is_bridge_self_path,
    _PROT_BRIDGE_FILE_RE,
    _PROT_DESTROY_FILE_RE,
    _PROT_KILL_VERB_RE,
    _PROT_PORT_RE,
    bridge_self_threat,
    _bridge_image_names_re,
    _SELF_KILL_RES,
)


# ---- approval policy (soft / medium / hard) --------------------------------
# approval_mode drives how much the agent may do without asking:
#   soft   = autonomous except deletions, program launches, desktop actions and
#            protected ops (registry / System32) - those still prompt, the
#            protected ones via the hold-to-approve card.
#   medium = "trust writes": in-workspace file writes/edits auto-approve.
#   hard   = every action prompts.
# The REFUSE set (drive wipes, fork bombs, bridge self-harm) is mode-independent.

from accuretta.security.soft_commands import (  # noqa: F401  re-export: moved 2026-11-24
    _SOFT_PROMPT_KINDS,
    _soft_command_needs_approval,
)
from accuretta.security.web_guard import (  # noqa: F401  re-export: moved 2026-11-23
    _SOFT_COMMAND_KINDS,
    _web_derived_paths,
    _recent_web_texts,
    _web_copied,
    _DOWNLOAD_RUN_RE,
    _web_guard_reason,
    _is_desktop_approval_kind,
)

from accuretta.security.soft_patterns import (  # noqa: F401  re-export: moved 2026-11-23
    _GIT_READONLY_VERBS,
    _SOFT_DESTRUCTIVE_RES,
    _is_destructive_command,
    _SOFT_SENSITIVE_COMMAND_RES,
    _soft_command_needs_approval_raw,
    _SOFT_STRUCTURAL_PATTERNS,
    _SOFT_ALWAYS_ASK_RES,
    _SOFT_ARGUMENT_RES,
    _SOFT_PATTERN_LABELS,
    _soft_pattern_label,
    _SOFT_DOC_EXTS,
    _soft_protected,
    _SOFT_DATA_REASON,
    _SOFT_KEYS_REASON,
    _SOFT_KEYS_TEXT_RE,
    _soft_data_text_reason,
    _soft_param_values,
    _SOFT_PY_NAME_RE,
    _SOFT_JS_RUNNERS,
    _SOFT_PS_HOSTS,
    _SOFT_SH_HOSTS,
    _SOFT_SCRIPT_EXTS,
    _SOFT_CODE_DELETE_RES,
    _SOFT_CODE_WRITE_RES,
    _SOFT_CODE_SPAWN_RE,
    _SOFT_PY_SPAWN_RE,
    _soft_code_spawns,
    _SOFT_CODE_ARGV_RE,
    _SOFT_CODE_ARGV_READONLY,
    _soft_code_argv_mutates,
    _SOFT_CODE_REGISTRY_RE,
    _SOFT_CODE_HOME_RE,
    _SOFT_CODE_SELF_DIR_RE,
    _SOFT_CODE_STRING_RE,
    _SOFT_CODE_SHELL_RES,
    _SOFT_DOWNLOAD_RE,
    _soft_code_run,
    _soft_scan_code,
    _SOFT_PY_TEMP_ASSIGN_RE,
    _SOFT_PY_DELETE_CALL_RE,
    _SOFT_PY_PATH_ASSIGN_RE,
    _soft_code_deletes_only_temp,
    _workspace_project_names,
    _SOFT_SCRIPTY_EXTS,
    _soft_open_concern,
    _code_secret_named,
    _app_matches_allowlist,
)
from accuretta.security.git_rules import (  # noqa: F401  re-export: moved 2026-11-23
    _GIT_REMOTE_MUTATING_VERBS,
    _GIT_QUIET_VERBS,
    _GIT_RESET_DISCARD,
    _GIT_EXEC_CONFIG_RE,
    _git_concern,
)


from accuretta.security.soft_patterns import (  # noqa: F401  re-export: moved 2026-11-24
    _git_argv_is_readonly,
    _SOFT_GIT_EXTRA_VERBS,
)


# ---- soft-mode command assessment ---------------------------------------------
# Soft mode should stop only for commands that can do real damage. Grepping the
# raw text stopped on a `2>&1`, a `<script>` inside a regex, or the word
# "delete" in an echo. The assessment reads the command instead:
#   - only command words count: quoted strings, here-strings, heredocs and
#     comments are data. A line that hands text to another evaluator (iex,
#     cmd /c, bash -c, ...) falls back to the raw-text check, and a
#     double-quoted string holding `$(...)` stays code, since it runs;
#   - redirects to the null device and stream merges (2>&1) are ignored;
#   - writes and deletions pass only when every target resolves to a literal
#     path inside a workspace project or the temp folder (deletions inside a
#     project, never a project or workspace root). Relative paths need an
#     explicit `cd` first: the bridge's own cwd is the app folder;
#   - Start-Process / Invoke-Item may open a document, not a program, and may
#     start a dev tool (python, node, npm, ...) in the background;
#   - git may also clone, fetch, init and add;
#   - copies and moves into system, startup, profile, .ssh or app folders ask;
#   - code the command runs is read too (see "code a command runs" below).
# Anything it cannot resolve (a path in an unknown variable, a pipeline-fed
# Remove-Item) still asks.

from accuretta.security.soft_commands import (  # noqa: F401  re-export: moved 2026-11-23
    _SOFT_EVALUATOR_RE,
    _SOFT_OPAQUE_HEADS,
    _SOFT_CMD_SWITCH_RE,
    _SOFT_HTTP_DELETE_RE,
    _SOFT_DELETE_VERBS,
    _SOFT_WRITE_VERBS,
    _SOFT_PLACE_VERBS,
    _SOFT_OPEN_VERBS,
    _SOFT_CD_VERBS,
    _SOFT_PREFIX_WORDS,
    _SOFT_VALUE_PARAMS,
    _SOFT_NULL_TARGETS,
    _soft_split_commands,
    _SOFT_DATA_SEARCH_REASON,
    _SOFT_KEYS_SEARCH_REASON,
    _SOFT_LISTERS,
    _SOFT_CONTENT_READERS,
    _SOFT_RECURSIVE_BY_DEFAULT,
    _SOFT_RECURSIVE_FLAGS,
    _soft_glob_reason,
    _soft_private_reason,
    _soft_holds_private,
    _soft_data_concerns,
    _soft_positional,
    _SOFT_DEV_TOOLS,
    _SOFT_SCRIPT_MAX_BYTES,
    _soft_local_path,
    _soft_read_script,
    _soft_code_concerns,
    _soft_delete_reason,
    _soft_command_concerns,
    _soft_partial_target,
    _soft_cwd_chunks,
    _SOFT_KILL_VERBS,
    _REBUILDABLE_DIRS,
    _BIG_DELETE_FILES,
    _BIG_DELETE_BYTES,
    _big_delete_concern,
)
from accuretta.security.soft_paths import (  # noqa: F401  re-export: moved 2026-11-23
    _SOFT_REAL_LINUX,
    _soft_temp_roots,
    _soft_depth_under,
    _soft_target_ok,
)

assert all(any(p.pattern == s for p in (*_SOFT_DESTRUCTIVE_RES, *_SOFT_SENSITIVE_COMMAND_RES))
           for s in _SOFT_STRUCTURAL_PATTERNS), "structural pattern missing from the raw lists"
assert len(_SOFT_ARGUMENT_RES) == 4, "argument-sensitive soft patterns changed"


from accuretta.security.shell_text import (  # noqa: F401  re-export: moved 2026-11-23
    _shell_subexpressions,
    _HereString,
    _shell_code_view,
    _BLOCK_DATA_PARAMS,
    _BLOCK_PRINT_HEADS,
    _BLOCK_EXEC_BEFORE_RE,
    _BLOCK_EXEC_AFTER_RE,
    _command_words,
)


from accuretta.security.shell_vars import (  # noqa: F401  re-export: moved 2026-11-23
    _soft_assignments,
    _soft_expand,
)


from accuretta.security.shell_paths import (  # noqa: F401  re-export: moved 2026-11-23
    _soft_resolve,
    _soft_expand_delete_targets,
)


# ---- code a command runs ------------------------------------------------------
# The command line is only half the story when it runs code. Reading it alone
# let `python -c "shutil.rmtree(...)"`, `node -e`, or a cleanup.py the model
# had just written run without a question. Soft now reads that code too:
#   - PowerShell scripts, `powershell -Command` text and, in the sandbox, shell
#     scripts and `bash -c` text go through this same assessment;
#   - Python and JavaScript (-c / -e code, heredocs and pipes into the
#     interpreter, script files) are scanned for deletions, registry writes,
#     shell commands that would ask on their own, and writes into protected
#     folders. A deletion passes when the code runs from inside the workspace
#     or temp (or works from its own folder there) and names no path outside;
#   - code that can't be read (a missing or huge script, -EncodedCommand)
#     asks.
# A tripwire for a careless or misled model, not a sandbox: code that pulls its
# damage from another module, or builds paths at runtime, still gets through.


# ---- quiet gating ---------------------------------------------------------------
# Replaying saved chats through the rules showed Soft asking about one action in
# nine, nearly all harmless: starting the program it had just built, stopping its
# own dev server, read-only `schtasks /query` or `git branch -a`, deleting its own
# scratch files. Meanwhile turning off Defender, deleting shadow copies, posting
# ~/.aws/credentials or killing every python.exe (Accuretta included) ran without
# a word. These helpers judge what an action does rather than how it is spelled:
#   runs       - reads, work inside the workspace, processes Accuretta started,
#                local git;
#   runs because the user asked - the user's own typed words name the action;
#   asks       - can't be undone and nobody asked (push, deletes outside the
#                workspace, other programs, system changes);
#   blocked / hold-to-approve - weakening security, destroying backups, secrets
#                leaving the PC, killing Accuretta itself. Every mode, /goal too.


from accuretta.security.danger import (  # noqa: F401  re-export: moved 2026-11-23
    _DANGER_BLOCK_RES,
    _DANGER_CRITICAL_RES,
    _SECRET_PATH_RE,
    _SECRET_SEND_RE,
    _SECRET_PY_NET_RE,
    _SECRET_IDENTITY_ARG_RE,
    _SECRET_ANCHOR_RE,
    _SECRET_UNANCHORED_RE,
    _secret_named,
    _danger_assess,
)


from accuretta.security.danger_gate import (  # noqa: F401  re-export: moved 2026-11-23
    _danger_assess_code,
    _danger_gate,
)


from accuretta.core.processes import (  # noqa: F401  re-export: moved 2026-11-23
    _process_table,
    _proc_image_path,
    _process_parent_map,
    _proc_create_time,
    _local_listeners,
)


from accuretta.security.stop_concerns import (  # noqa: F401  re-export: moved 2026-11-23
    _model_server_pids,
    _pid_stop_reason,
    _stop_name_reasons,
    _process_stop_concerns,
    _own_command_line,
    _cim_stop_targets,
)


from accuretta.security.process_stops import (  # noqa: F401  re-export: moved 2026-11-23
    _stop_pid_file_values,
    _stop_var_ok,
)


from accuretta.tools.host_shell import (  # noqa: F401  re-export: moved 2026-11-23
    _snapshot_shell_deletes,
    _checkpoint_shell_deletes,
    tool_run_powershell,
    _run_host_command,
    _python_launcher,
    tool_run_python,
)


from accuretta.security.user_words import (  # noqa: F401  re-export: moved 2026-11-23
    _USER_WORDS,
    _TURN_OK,
    _CHAT_OK,
    _CHAT_ALLOWABLE,
    _NEGATION,
    _note_user_words,
    _said,
    _CONCERN_WRAPPER_RE,
    _concern_category,
    _concern_authorized,
    _remember_turn_approval,
)


from accuretta.agent.file_tools import (  # noqa: F401  re-export: moved 2026-11-24
    _diff_preview_rows,
    tool_list_directory,
    _file_staleness,
    _decode_text_file,
    _encode_text_file,
    tool_write_file,
    tool_edit_file,
    tool_delete_file,
    _tool_write_history,
    REWRITE_LOOP_LIMIT,
    _write_count,
    _record_write,
)


from accuretta.security.approvals import (  # noqa: F401  re-export: moved 2026-11-23
    _critical_path_gate,
)


# ---- approval queue --------------------------------------------------------


from accuretta.security.desktop_intent import (  # noqa: F401  re-export: moved 2026-11-23
    _DESKTOP_INPUT_KINDS,
    _DESKTOP_SESSION_SENTINEL,
    _desktop_session_unlocked,
    _DESKTOP_INTENT_RE,
    _user_asked_desktop_interaction,
    _desktop_target_owned,
    _desktop_target_lock,
    _is_accuretta_window,
)


from accuretta.security.session_allow import (  # noqa: F401  re-export: moved 2026-11-23
    _approval_session_allow,
)


from accuretta.agent.goal_mode import (  # noqa: F401  re-export: moved 2026-11-24
    _set_goal_mode,
    _GOAL_OFF_RE,
    _GOAL_COMMAND_RE,
    _parse_goal_command,
    _GOAL_LOOK_ONLY_TOOLS,
    _goal_window_progressed,
    _goal_open_steps,
    _GOAL_NUM_WORDS,
    _GOAL_NUM,
    _GOAL_UNIT,
    _GOAL_LIMIT_RE,
    _goal_time_limit,
    _goal_clock_text,
    _goal_elapsed_text,
    _goal_summary,
    _apply_goal_latch,
)


from accuretta.security.web_taint import (  # noqa: F401  re-export: moved 2026-11-23
    _WEB_TAINT_WINDOW_S,
    _WEB_TEXT_KEEP_CHARS,
    _web_texts,
    _web_taint_lock,
    _norm_web,
    _mark_web_taint,
    _recent_web_taint,
)


from accuretta.tools.code_edit import (  # noqa: F401  re-export: moved 2026-11-23
    _note_web_derived_file,
    _run_linter,
    _with_code_check,
    tool_replace_ast_node,
)


# ---- browser-interaction intent (ClickFix click gating) ---------------------
# Playwright MCP interactive verbs (click/fill/type/...) run freely only when the
# user's own last message asked for page interaction; a page-driven "click the
# download button" goes through an approval card even in soft mode. The words
# live on STATE.last_user_text.

from accuretta.tools.mcp_calls import (  # noqa: F401  re-export: moved 2026-11-23
    _BROWSER_INTENT_RE,
    _user_asked_browser_interaction,
    _INTERACTIVE_MCP_RE,
    _MCP_BROWSER_TOOL_RE,
    _ARIA_NOISE_RE,
    _compress_aria_text,
    _MCP_SHODAN_STATUS_RE,
    _mcp_explain_refusal,
    _mcp_make_tool_callable,
)


from accuretta.agent.tool_bundles import (  # noqa: F401  re-export: moved 2026-11-24
    _BUNDLE_INTENT_RES,
    _MSG_PATH_RE,
    _NATIVE_MAGIC,
    _message_binary_paths,
    _auto_unlock_bundles,
    _TARGET_ACTION,
    _HOST_TARGET_INTENT_RE,
    _REMOTE_TARGET_INTENT_RE,
    _user_explicitly_requested_execution_target,
    tool_switch_execution_target,
    _TOOL_BUNDLE_LOCK,
    _unlock_bundle,
    tool_list_more_tools,
)


from accuretta.security.approval_list import (  # noqa: F401  re-export: moved 2026-11-23
    list_approvals,
)


# ---- notch overlay (lite desktop widget) -------------------------------------
# A small transparent always-on-top island (accuretta-notch.html) rendered by its
# own host process and wired to this bridge over /api/events + the REST API:
# notch_host.py on Windows (Win32 + pywebview's WebView2), notch_host_mac.py on
# macOS (AppKit + WebKit via PyObjC). Being a separate process, it keeps working
# while the main window is minimized; it dies with the bridge (atexit + its own
# health poll).

from accuretta.runtime.notch_process import (  # noqa: F401  re-export: moved 2026-11-23
    _notch_lock,
    _notch_host_script,
    _notch_host_available,
    notch_start,
    notch_stop,
)


from accuretta.runtime.notch_process import (  # noqa: F401  re-export: moved 2026-11-24
    notch_sync,
)


# ---- SSE event bus ---------------------------------------------------------
# Reconnect-friendly pub/sub. Each broadcast gets a monotonic id and goes into a
# small ring buffer (EVENT_LOG_MAX). The SSE handler replays anything after the
# request's `Last-Event-ID`, then resumes the live stream, so brief disconnects
# don't lose tool_result or approval events. Subscribers get the snapshot id at
# subscribe time, so replay and live queue never duplicate an event.

from collections import deque

from accuretta.core.events import (  # noqa: F401  re-export: moved 2026-11-23
    EVENT_LOG_MAX,
    _subscribers,
    _subs_lock,
    _event_log,
    subscribe,
    unsubscribe,
    _TRANSIENT_EVENT_TYPES,
    _BEST_EFFORT_EVENT_TYPES,
    broadcast_event,
)


from accuretta.core.events import (  # noqa: F401  re-export: moved 2026-11-24
    replay_events_since,
)


# ---- tool implementations --------------------------------------------------


from accuretta.tools.pdf_text import (  # noqa: F401  re-export: moved 2026-11-23
    _PDF_TEXT_CAP,
    _extract_pdf_text,
)


from accuretta.tools.file_read import (  # noqa: F401  re-export: moved 2026-11-23
    _chat_file_reads,
    _chat_file_reads_lock,
    _file_md5,
    _record_file_read,
    _missing_file_error,
    tool_read_file,
    tool_read_bytes,
    tool_read_skeleton,
)


from accuretta.core.undo import (  # noqa: F401  re-export: moved 2026-11-23
    _UNDO_DIR,
    _undo_store,
    _undo_snapshot,
)


from accuretta.core.undo import (  # noqa: F401  re-export: moved 2026-11-24
    _undo_begin_turn,
    _undo_commit_turn,
    _undo_restore,
)


from accuretta.tools.ps_bom import (  # noqa: F401  re-export: moved 2026-11-23
    _UTF8_BOM,
    _PS_REDIRECT_TARGET_RE,
    _PS_OUTFILE_RE,
    _PS_TOKEN_RE,
    _ps_write_targets,
    _ps_bom_candidates,
)


from accuretta.agent.edit_helpers import (  # noqa: F401  re-export: moved 2026-11-23
    _new_file_eol,
    _closest_region,
)


from accuretta.agent.loose_match import (  # noqa: F401  re-export: moved 2026-11-23
    _indent_of,
    _indent_width,
    _level_map,
    _loose_block_key,
    _trim_blank_edges,
    _loose_line_match,
)


from accuretta.security.blocked_writes import (  # noqa: F401  re-export: moved 2026-11-24
    _blocked_writes,
    _BLOCKED_WRITE_TTL_S,
    _blocked_write_key,
    _blocked_write,
    _FILE_IN_PARTS,
)


from accuretta.tools.file_search import (  # noqa: F401  re-export: moved 2026-11-23
    tool_find_references,
    _SYMBOL_DEF_TYPES,
    _SYMBOL_CACHE,
    _symbols_in_file,
    tool_find_symbol,
    tool_find_files,
    _GREP_SKIP_DIRS,
    _GREP_MAX_FILE_BYTES,
    _grep_candidates,
    tool_grep_files,
    _PROJECT_MAP_SKIP_DIRS,
    _PROJECT_MAP_IMPORTANT,
    tool_project_map,
)


from accuretta.core.storage import (  # noqa: F401  re-export: moved 2026-11-23
    _cmd_history_lock,
    _STORAGE_KINDS,
    _storage_entries,
    _storage_measure,
    storage_report,
    storage_clear,
)


from accuretta.runtime.command_kill import (  # noqa: F401  re-export: moved 2026-11-23
    _running_cmds_lock,
    _running_cmds,
    _running_cmd_targets,
    _killed_procs,
    _kill_current_command,
)


from accuretta.core.windows_bin import (  # noqa: F401  re-export: moved 2026-11-23
    _trusted_windows_binary,
    _NO_WINDOW,
)


from accuretta.tools.syntax_check import (  # noqa: F401  re-export: moved 2026-11-23
    _trusted_node_binary,
    _syntax_unchecked,
    _check_css_syntax,
    tool_check_syntax,
    _check_syntax_impl,
)


from accuretta.runtime.process_tree import (  # noqa: F401  re-export: moved 2026-11-23
    _terminate_process_tree,
)


from accuretta.tools.powershell import (  # noqa: F401  re-export: moved 2026-11-23
    _PS_STATUS,
    _PS_ERROR_ID_RE,
    _strip_new_boms,
    _clip_head_tail,
    _ps_quote,
    _ps_script,
    _ps_outcome,
    _run_powershell,
)


from accuretta.core.github_names import (  # noqa: F401  re-export: moved 2026-11-23
    _GITHUB_OWNER_RE,
    _GITHUB_REPO_RE,
    _GITHUB_BRANCH_RE,
    _GITHUB_BRANCH_BAD,
    _github_valid_branch_name,
    _github_worktree_metadata,
)
from accuretta.core.command_cwd import (  # noqa: F401  re-export: moved 2026-11-23
    _default_command_cwd,
    _project_workspace_metadata,
)
from accuretta.agent.project_follow import (  # noqa: F401  re-export: moved 2026-11-23
    _project_workspace_prompt,
    _PROJECT_FOLLOW_FILE_TOOLS,
    _PROJECT_FOLLOW_GIT_TOOLS,
    _PROJECT_FOLLOW_SHELL_TOOLS,
    _GIT_WRITE_VERBS,
    _GIT_CLONE_VALUE_OPTS,
    _project_follow_folder,
    _git_command_folders,
    _project_follow_target,
    _project_follow,
)


from accuretta.security.hard_blocks import (  # noqa: F401  re-export: moved 2026-11-23
    _catastrophic_cmd,
    _CLICKFIX_DL_EXEC,
    _CLICKFIX_FETCH_THEN_EXEC,
    _CLICKFIX_PIPE_EXEC,
    _clickfix_cmd,
)


from accuretta.tools.sandbox_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _HOST_CODE_EXEC_RE,
    _WSL_HOST_INTEROP_RE,
    _host_command_executes_code,
    _sandbox_security_profile_active,
    _win_to_wsl_path,
    sandbox_run,
    tool_sandbox_run,
    tool_sandbox_nmap,
    tool_sandbox_sqlmap,
)


from accuretta.agent.tool_call_stream import (  # noqa: F401  re-export: moved 2026-11-24
    _CARRY_OTHER_CALL_MAX_CHARS,
    _cut_off_call_name,
    _is_file_write_call,
    _TOOL_CALL_OPENER_RE,
    _TOOL_CALL_CLOSER_RE,
    _unclosed_tool_code_tail,
    _has_unclosed_tool_call,
    _unclosed_tool_tail,
    _merge_carried_tool_call,
    _strip_tool_markup,
    _flush_tool_stream,
    TOOL_SYNTAX_HINT_RE,
    _FLAG_RE,
    _is_breach_capable_tool,
    _looks_unfinished,
    _log_tool_parse_failure,
    _TRAILING_CALL_RUN_RE,
    _recover_trailing_calls,
    _PROGRESS_PATH_RES,
    _TEXT_CALL_OPEN_RE,
    _TEXT_CALL_NAME_RE,
    _text_call_progress,
    _maybe_emit_call_progress,
)


from accuretta.tools.remote_tools import (  # noqa: F401  re-export: moved 2026-11-24
    tool_remote_file_abort,
)


from accuretta.tools.sessions import (  # noqa: F401  re-export: moved 2026-11-23
    _SESSION_BUF_CAP,
    Session,
    SessionManager,
    _session_mgr,
    _session_wait,
    _session_not_executed,
    _session_workspace_cwd,
    _SESSION_PY_REPL_RE,
    _SESSION_NODE_REPL_RE,
    _session_repl_command,
    tool_session_start,
    tool_session_send,
    tool_session_read,
    tool_session_stop,
    tool_session_list,
)


from accuretta.tools.host_forensics import (  # noqa: F401  re-export: moved 2026-11-23
    _NETSNAP_PS,
    _NET_PORT_SERVICES,
    _NET_KNOWN_RANGES,
    _NETSNAP_MAX_CMDLINE,
    _netsnap_ip_hint,
    _netsnap_debug_dump,
    _EVTLOG_PS,
    _PERSIST_PS,
    _HOST_EVT_LABELS,
    tool_network_snapshot,
    tool_parse_event_logs,
    tool_persistence_hunt,
)

from accuretta.tools.netsnap_labels import (  # noqa: F401  re-export: moved 2026-11-23
    _NET_KNOWN_PROCESSES,
    _netsnap_process_label,
)


from accuretta.tools.blue_baseline import (  # noqa: F401  re-export: moved 2026-11-23
    _BLUE_BASELINE_DIR,
    _blue_baseline_id,
    _network_snapshot_signature,
    _network_baseline_apply,
)


from accuretta.tools.host_shell import (  # noqa: F401  re-export: moved 2026-11-24
    tool_open_program,
)


# ---- git -------------------------------------------------------------------
# Wraps the `git` CLI. Read-only verbs (status/log/diff/branch/show/remote) run
# freely; anything that mutates the index, working tree or a remote goes
# through request_approval. Repos outside the workspace are rejected like file
# edits are. GIT_TERMINAL_PROMPT=0 makes a missing credential or host-key
# prompt fail fast instead of hanging the worker.


from accuretta.tools.git_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _resolve_git_cwd,
    _git_approve,
    _is_accuretta_repo,
    tool_git_status,
    tool_git_log,
    tool_git_diff,
    tool_git_branch,
    tool_git_show,
    tool_git_remote,
    tool_git_add,
    tool_git_commit,
    _record_repo_push,
    tool_git_push,
    tool_git_pull,
    tool_git_fetch,
    tool_git_checkout,
    tool_git_restore,
    tool_git_reset,
    tool_git_init,
    tool_git_clone,
)


from accuretta.tools.git_runner import (  # noqa: F401  re-export: moved 2026-11-23
    _git_env,
    _GITHUB_GIT_HELPER,
    _github_git_login,
    _run_git,
)


from accuretta.tools.github_account import (  # noqa: F401  re-export: moved 2026-11-23
    _github_forget_saved_keys,
    _github_save_cred,
)


from accuretta.tools.github import (  # noqa: F401  re-export: moved 2026-11-23
    _parse_push_repo_key,
    _github_repo_page,
    _gh_quartile_levels,
)


# ---- github integration ----------------------------------------------------
# GitHub Activity dashboard + branch workflow. The PAT stays in
# data/github_cred.json and never leaves the bridge; the frontend only sees the
# connected state (login + avatar). All calls use the helpers below with a hard
# timeout so a hung request can't wedge a worker.

from accuretta.tools.github_account import (  # noqa: F401  re-export: moved 2026-11-23
    GITHUB_CRED_FILE,
    GITHUB_GRAPHQL_API,
    _github_cred,
    _github_headers,
    _github_graphql,
    _GH_ACTIVITY_QUERY,
    _GH_REPOS_QUERY,
    _github_repositories,
    _github_activity,
)
from accuretta.tools.github_projects import (  # noqa: F401  re-export: moved 2026-11-23
    GITHUB_REST_API,
    _github_rest,
    _github_rest_h,
    _github_worktree_prompt,
    _project_workspace_root,
    _github_create_project,
    _github_find_workspace_repo,
    _github_branch_flow,
)


from accuretta.tools.memory_tools import (  # noqa: F401  re-export: moved 2026-11-24
    _load_memories,
    _save_memories,
    tool_remember,
    tool_forget,
    tool_edit_memory,
    tool_search_memories,
)


from accuretta.tools.web_fetch import (  # noqa: F401  re-export: moved 2026-11-23
    _LINK_PREVIEW_CACHE,
    _LINK_PREVIEW_CACHE_LOCK,
    _LINK_PREVIEW_CACHE_MAX,
    _remember_profile,
    _open_with_rotation,
    fetch_link_preview,
    tool_web_fetch,
    _DDG_RESULT_RE,
    _DDG_SNIPPET_RE,
    _strip_tags,
    tool_web_search,
)


from accuretta.tools.link_preview import (  # noqa: F401  re-export: moved 2026-11-23
    _link_preview_extract,
)


# =====================================================================
# Outbound HTTP: believable browser identity rotation.
#
# Most WAFs 403 anything that looks like a script, and a single custom UA only
# gets past the laziest filters. Below is a small pool of real browser
# fingerprints (UA + matching Sec-Ch-Ua / Accept-* / Sec-Fetch-* headers, each
# set captured from a fresh install of that browser). _pick_profile() picks
# one, _browser_headers() materializes it, and _open_with_rotation() does the
# 403/429 swap-profile-and-retry loop.
#
# This is bot-detection mitigation, not bypass: TLS fingerprinting (JA3/JA4)
# or JS challenges will still block us.
# =====================================================================

from accuretta.redteam.browser_profiles import (  # noqa: F401  re-export: moved 2026-11-23
    _BROWSER_PROFILES,
    _PROFILE_LOCK,
    _PROFILE_BY_HOST,
    _PROFILE_STICKY_TTL,
    _pick_profile,
)


from accuretta.tools.web_media import (  # noqa: F401  re-export: moved 2026-11-24
    tool_web_image_search,
    tool_web_watch_video,
)


# ---- native vision (link → image → model) ----------------------------------
# Deliberately native-only: these tools hand pixels to the MAIN llama-server,
# which must be booted with an mmproj belonging to the selected GGUF. There is
# no fallback to the separate describe side-server — a caption from a small
# side model is exactly the fidelity ceiling "replicate this design" cannot
# work under, so the tool refuses with instructions instead of degrading.


from accuretta.tools.vision import (  # noqa: F401  re-export: moved 2026-11-23
    _describe_image_native,
    _describe_image_multi,
    describe_image,
)


from accuretta.tools.screen_view import (  # noqa: F401  re-export: moved 2026-11-24
    tool_web_view_image,
    _SCREEN_SHOT_DIR,
    _SCREEN_MAX_DIM,
    _SCREEN_POS_RE,
    _BLANK_CONTENT_NOTE,
    _content_looks_blank,
    _vision_position_hint,
    _screen_coords_in_text,
    _png_b64,
    _describe_frame,
    tool_view_image,
)


# ---- watch video (link → frames → storyboard) --------------------------------

from accuretta.tools.ytdlp import (  # noqa: F401  re-export: moved 2026-11-23
    _resolve_ytdlp_cmd,
)


from accuretta.tools.video_frames import (  # noqa: F401  re-export: moved 2026-11-23
    _run_process_capture,
    _extract_video_frames,
)


# ---- desktop automation ---------------------------------------------------
# Layered defense:
#   1. Feature flag (`desktop_enabled`) must be ON in settings.
#   2. Required libraries must be present (pyautogui / PIL; pygetwindow is nice-to-have).
#   3. Kill switch (`_desktop_panic`) — any pending or new action is refused immediately.
#   4. Rate limit — hard cap on actions per minute regardless of agent intent.
#   5. Allowlist — only apps the user explicitly whitelisted can be launched.
#   6. Approval — every action tool routes through request_approval with full context.
#
# Read-only tools (screenshot/describe/list) run without approval when
# `desktop_auto_approve_read` is on (default), because observation can't
# mutate the machine and constant approval prompts would be unusable.


from accuretta.tools.desktop_procs import (  # noqa: F401  re-export: moved 2026-11-23
    _screen_saver_pids,
    _process_path,
)


from accuretta.tools.desktop_windows import (  # noqa: F401  re-export: moved 2026-11-24
    _list_windows_raw,
    _window_id,
    _window_handle_from_id,
    _window_process_id,
)


from accuretta.tools.desktop_windows import (  # noqa: F401  re-export: moved 2026-11-23
    _find_window,
    _window_by_selector,
    _capture_target_window,
)


from accuretta.tools.uia import (  # noqa: F401  re-export: moved 2026-11-23
    _uia_safe,
    _uia_pattern,
    _uia_bounds,
    _uia_control_record,
    _uia_descriptor_matches,
)


from accuretta.runtime.vision_ready import (  # noqa: F401  re-export: moved 2026-11-24
    _vision_observation_available,
    _separate_vision_server_ready,
)


# ---- desktop tools: read-only (observation) ---------------------------------


from accuretta.tools.desktop_tools import (  # noqa: F401  re-export: moved 2026-11-24
    tool_screenshot,
    tool_describe_screen,
    tool_list_windows,
    tool_desktop_snapshot,
    tool_desktop_launch_app,
    tool_desktop_focus_window,
    tool_desktop_click,
    _element_at_point,
    tool_desktop_drag,
    _default_browser_exe,
    tool_desktop_open_url,
    tool_desktop_type_text,
    tool_desktop_press_keys,
    tool_desktop_close_window,
    _desktop_element_action,
    tool_desktop_invoke,
    tool_desktop_set_value,
    tool_desktop_scroll,
)


# ---- desktop tools: actions (gated) -----------------------------------------


from accuretta.tools.desktop_state import _desktop_post_state  # noqa: F401  re-export: moved 2026-11-24
from accuretta.agent.tool_dispatch import (  # noqa: F401  re-export: moved 2026-11-24
    _REMOTE_TOOL_EQUIVALENTS,
    DIRECTORY_WALK_LIMIT,
    invoke_tool,
)


# The target itself lives on STATE.desktop_target_window.


# ============================================================================
# Firmware analysis tools — read-only triage on binaries dropped in the
# workspace. Designed for first-pass router / IoT firmware audits. No system
# tools, no shells. Pure Python where possible, optional pip libs elsewhere.
# Extraction is the only destructive op and uses the standard approval card.
# ============================================================================

from accuretta.tools.firmware import (  # noqa: F401  re-export: moved 2026-11-23
    _FW_MAGIC_TABLE,
    _fw_identify_magic,
    _FW_SCAN_SIGNATURES,
    tool_binwalk_scan,
    tool_strings_dump,
    tool_carve_file,
)


from accuretta.tools.archives import (  # noqa: F401  re-export: moved 2026-11-24
    tool_file_inspect,
    _safe_member,
    tool_extract_archive,
    tool_extract_squashfs,
)


from accuretta.tools.binary_tools import (  # noqa: F401  re-export: moved 2026-11-24
    _capstone_md,
    tool_disasm_at,
    _GHIDRA_DANGER_IMPORTS,
    _resolve_ghidra_install_dir,
    _ghidra_idle_s,
    tool_ghidra_analyze,
    _PE_MACHINE,
    _PACKER_SECTION_HINTS,
    _inspect_pe,
    _ELF_MACHINE,
    _inspect_elf,
    tool_binary_inspect,
)


# ---- APK static analysis -------------------------------------------------
#   scan_apk(path)     - pure-Python triage: manifest, permissions, components,
#                        cert info, printable strings from DEX + .so, and a regex
#                        pass for common secrets (cloud keys, JWTs, hardcoded
#                        URLs). Returns one structured JSON report.
#   decompile_apk(...) - runs JADX and writes .java sources into the workspace
#                        for read_file / grep_files. Needs java + jadx
#                        (settings.jadx_path).
# Both refuse paths outside the workspace via _fw_check_path; decompile_apk
# writes a folder and is gated by request_approval.

from accuretta.tools.secret_hunt import (  # noqa: F401  re-export: moved 2026-11-23
    _APK_SECRET_RE,
    _REPO_SECRET_RE,
    _hunt_secrets_in_file,
)

from accuretta.tools.apk_scan import (  # noqa: F401  re-export: moved 2026-11-23
    _APK_DANGEROUS_PERMS,
    _apk_hunt_secrets,
    tool_scan_apk,
)


from accuretta.tools.apk import (  # noqa: F401  re-export: moved 2026-11-23
    _resolve_jadx_bin,
    tool_decompile_apk,
)


from accuretta.runtime.system_ram import (  # noqa: F401  re-export: moved 2026-11-23
    _ghidra_heap_mb,
    _system_ram_info,
)


from accuretta.tools.ghidra_worker import (  # noqa: F401  re-export: moved 2026-11-23
    _ghidra_worker_env,
    _GhidraWorker,
    _GHIDRA_WORKER,
)


atexit.register(_GHIDRA_WORKER.stop)


# ---- Binary triage (PE/ELF/Mach-O) + YARA scanning -----------------------
# binary_inspect takes tens of milliseconds on a 5 MB exe versus Ghidra's ~30s,
# so the model uses it to decide whether to escalate to ghidra_analyze.
# yara_scan ships a small default rule set for users without rules of their own.


from accuretta.tools.binary_triage import (  # noqa: F401  re-export: moved 2026-11-23
    _shannon_entropy,
    _detect_format,
    _yara_string_to_dict,
)


from accuretta.tools.yara_scan import (  # noqa: F401  re-export: moved 2026-11-24
    _DEFAULT_YARA_RULES,
    _yara_compile,
    tool_yara_scan,
)


from accuretta.tools.secrets_scan import (  # noqa: F401  re-export: moved 2026-11-23
    tool_scan_secrets,
)

from accuretta.tools.evtx import (  # noqa: F401  re-export: moved 2026-11-23
    _EVTX_EVENT_LABELS,
    tool_parse_evtx,
)


from accuretta.tools.pcap import (  # noqa: F401  re-export: moved 2026-11-24
    tool_analyze_pcap,
)

from accuretta.tools.pcap import (  # noqa: F401  re-export: moved 2026-11-23
    _analyze_pcap_dpkt,
)

from accuretta.redteam.recon_tools import (  # noqa: F401  re-export: moved 2026-11-24
    tool_audit_http_headers,
    tool_recon_tls_audit,
    tool_recon_rdap,
    tool_recon_web_archive,
    tool_recon_cve_match,
    tool_recon_capture_evidence,
)

# ============================================================
# AUTHORIZED RECON TOOLS
# Low-and-slow reconnaissance for AUTHORIZED security testing only. The UI
# gates these behind an explicit "do you have authorization?" prompt; the
# tools themselves are strictly read-only (no exploitation, no payloads,
# no writes). Designed to be quieter than nmap: capped concurrency,
# randomized port order, jitter between probes. stdlib-only.
# ============================================================

from accuretta.redteam.port_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _RECON_TOP_PORTS,
    _RECON_PORT_NAMES,
    _recon_resolve,
    tool_recon_port_scan,
    _recon_tcp_cmd,
    tool_recon_open_services,
)


from accuretta.redteam.doh import (  # noqa: F401  re-export: moved 2026-11-23
    _recon_clean_host,
    _recon_http_doh,
    _RECON_UA,
)


from accuretta.redteam.http_audit import (  # noqa: F401  re-export: moved 2026-11-23
    _recon_prep_url,
    _recon_cert_from_der,
)


from accuretta.core.public_tls import (  # noqa: F401  re-export: moved 2026-11-23
    _public_source_tls,
)


from accuretta.redteam.waf import (  # noqa: F401  re-export: moved 2026-11-23
    _hget,
    _recon_waf_classify,
    _RECON_WAF_MARKERS,
    _recon_classify_body,
)


from accuretta.redteam.http_fingerprint import (  # noqa: F401  re-export: moved 2026-11-23
    _SEC_HEADERS,
    _recon_grade_security_headers,
    tool_recon_http_fingerprint,
)


from accuretta.redteam.subdomains import (  # noqa: F401  re-export: moved 2026-11-23
    _ct_names_crtsh,
    _ct_names_certspotter,
    tool_recon_subdomains,
    _RECON_TAKEOVER_FINGERPRINTS,
    tool_recon_subdomain_takeover,
)


from accuretta.redteam.whois import (  # noqa: F401  re-export: moved 2026-11-23
    _WHOIS_SERVERS,
    _WHOIS_QUERY_FORMAT,
    _WHOIS_FIELDS,
    _WHOIS_NS_KEYS,
    _WHOIS_STATUS_KEYS,
    _WHOIS_FREE,
    _whois_server_for,
    _recon_whois,
)


from accuretta.redteam.osint import (  # noqa: F401  re-export: moved 2026-11-23
    _whois_query,
    _shodan_refusal,
    _shodan_host_summary,
)


from accuretta.redteam.shodan import (  # noqa: F401  re-export: moved 2026-11-23
    _SHODAN_MAX_IPS,
    _shodan_get_json,
    _SHODAN_CDN_WORDS,
    _SHODAN_SHARED_WORDS,
    _recon_registered_domain,
    _shodan_shared_hosting,
    tool_recon_shodan,
)


from accuretta.redteam.dns_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _recon_dns_email_auth,
    _dns_build_query,
    _recon_dns_axfr,
    tool_recon_dns,
)


from accuretta.redteam.governor import (  # noqa: F401  re-export: moved 2026-11-23
    _RT_GOV_LOCK,
    _RT_GOV_LEDGER_LOCK,
    _RT_GOV_STATE,
    _RT_GOV_LEDGER_MAX_BYTES,
    _rt_gov_state,
    _rt_gov_limits,
    _rt_gov_preflight,
    _rt_gov_ledger_append,
    _RT_GOV_JITTER,
    _RT_GOV_SOFT_INTERVAL_S,
    _RT_GOV_MAX_WAIT_S,
    _RT_GOV_PREFLIGHT_MAX_MIN,
    _rt_gov_interval,
    _rt_gov_sleep_until,
    _rt_gov_wait_turn,
    _rt_gov_admit,
    _rt_gov_ledger_count,
)


from accuretta.redteam.content_discovery import (  # noqa: F401  re-export: moved 2026-11-23
    _RECON_COMMON_PATHS,
    _RECON_QUIET_PATHS,
    _recon_baseline,
    _recon_same_as_baseline,
    _RECON_API_PATHS,
    _RECON_BACKUP_SUFFIXES,
    _RECON_BACKUP_SEEDS,
    _RECON_VHOST_WORDS,
    _recon_backup_wordlist,
    _recon_vhost_fuzz,
    tool_recon_content_discovery,
    tool_validate_finding,
)


from accuretta.redteam.s3_exposure import (  # noqa: F401  re-export: moved 2026-11-23
    _recon_s3_candidates,
    _recon_check_s3,
    tool_recon_check_exposure,
)


from accuretta.redteam.injection_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _RECON_SQL_ERRORS,
    tool_recon_injection_probe,
    tool_sql_injection,
    _BATCH_PROBE_SETS,
    tool_batch_probe,
)


# ---- recon_map: attack-surface discovery -----------------------------------
# A good surface map is what lets a WEAKER local model succeed: it stops the
# model guessing where to attack and hands it a ready-to-act list. The quality
# bar is the discovery itself — crucially, reading the site's JavaScript (where
# modern apps hide their real API endpoints), not just following <a> links.

from accuretta.redteam.http_tools import (  # noqa: F401  re-export: moved 2026-11-23
    _MAP_ID_SEG_RE,
    _map_path_id,
    _HTTP_SESSIONS,
    _HTTP_RESPONSE_HISTORY,
    _HTTP_RESPONSE_HISTORY_LIMIT,
    _HTTP_RESPONSE_HISTORY_LOCK,
    _http_history_key,
    _http_identity_label,
    _http_compare_with_previous,
    tool_http_request,
    _ACCESS_SAFE_METHODS,
    _ACCESS_SAME,
    _ACCESS_PREVIEW_CHARS,
    _access_similarity,
    _access_proof_candidates,
    _access_final_path,
    _access_verdict,
    _access_owner_ok,
    _access_row,
    tool_rt_access_check,
)


from accuretta.redteam.graphql import (  # noqa: F401  re-export: moved 2026-11-23
    _MAP_GQL_PATHS,
    _MAP_GQL_INTROSPECT,
    _map_post_json,
    _map_graphql_introspect,
    _map_find_graphql,
)


from accuretta.redteam.recon_map import (  # noqa: F401  re-export: moved 2026-11-23
    _map_openapi,
    _http_body_kind,
    _save_http_request_evidence,
    _access_segments,
    _split_sql_select_list,
    _decode_jwt_claims,
    _frontend_resource_urls,
)


from accuretta.redteam.auth_spray import (  # noqa: F401  re-export: moved 2026-11-23
    _RECON_DEFAULT_CREDS,
    _RateLimited,
    _recon_try_login,
    tool_recon_auth_spray,
)


from accuretta.redteam.http_summary import (  # noqa: F401  re-export: moved 2026-11-23
    _HTTP_SUMMARY_MIN_BODY,
    _HTTP_SUMMARY_TEXT_CHARS,
    _HTTP_SUMMARY_HEADER_CHARS,
    _HTTP_SUMMARY_HEADER_COUNT,
    _HTTP_SUMMARY_COOKIE_CHARS,
    _HTTP_SUMMARY_COOKIE_COUNT,
    _HTTP_NOTABLE_HEADERS,
    _clip_text,
    _clip_headers,
    _html_visible_text,
    _http_body_preview,
    _http_summary,
    _http_model_view,
)
from accuretta.tools.saved_output import (  # noqa: F401  re-export: moved 2026-11-23
    _TOOL_OUTPUT_DIR,
    _TOOL_OUTPUT_KEEP,
    _save_full_output,
    _is_saved_tool_output,
)
from accuretta.agent.tool_output import (  # noqa: F401  re-export: moved 2026-11-23
    _BULK_TOOL_FIELDS,
    _PLAIN_RESULT_SHOWN,
    _plain_tool_text,
    compress_tool_result,
)


from accuretta.redteam.probe_tools import (  # noqa: F401  re-export: moved 2026-11-24
    tool_encode_decode,
    tool_fuzz,
    tool_cors_probe,
    tool_tcp_send,
)


from accuretta.redteam.jwt_tool import (  # noqa: F401  re-export: moved 2026-11-23
    _JWT_WEAK_SECRETS,
    tool_jwt_tool,
)


from accuretta.redteam.frontend_secrets import (  # noqa: F401  re-export: moved 2026-11-23
    _FRONTEND_SECRET_SPECS,
    _FRONTEND_PUBLIC_SPECS,
    _FRONTEND_JWT_RE,
    _GENERIC_SECRET_RE,
    _SECRET_PLACEHOLDER_RE,
    _SECRET_CONTEXT_NEGATIVE_RE,
    _SECRET_CONTEXT_POSITIVE_RE,
    _secret_entropy,
    _secret_is_placeholder,
    _secret_redact,
    _private_key_complete,
    _scan_frontend_source,
)


# ---- passive frontend-surface extraction (endpoints + source maps) ----------
# Both run ONLY over bytes scan_js_secrets already fetched (the page, its linked
# and lazy JavaScript, and any source maps) — zero additional requests to the
# target. This is extraction, never probing: discovered endpoints are reported
# as attack surface and are never called; reconstructed sources are just the
# original files the server already published inside its .map artifacts. Active
# testing of any of this stays on the gated Red Team engagements.

from accuretta.redteam.frontend_endpoints import (  # noqa: F401  re-export: moved 2026-11-23
    _ENDPOINT_STATIC_EXT,
    _ENDPOINT_WS_RE,
    _ENDPOINT_ABS_RE,
    _ENDPOINT_PATH_RE,
    _ENDPOINT_CALL_RE,
    _endpoint_is_noise,
    _classify_endpoint,
    _extract_frontend_endpoints,
)


from accuretta.redteam.source_maps import (  # noqa: F401  re-export: moved 2026-11-23
    _reconstruct_source_maps,
)


from accuretta.tools.deps_check import (  # noqa: F401  re-export: moved 2026-11-23
    _DEPENDENCY_MANIFEST_PRIORITY,
    tool_check_deps,
)


from accuretta.tools.deps import (  # noqa: F401  re-export: moved 2026-11-23
    _parse_dependency_manifest,
)


# =====================================================================
# AGENTIC CODING TOOLS
# =====================================================================


from accuretta.agent.capability_report import (  # noqa: F401  re-export: moved 2026-11-24
    _capability_health,
    tool_capability_report,
)


from accuretta.agent.tool_contracts import (  # noqa: F401  re-export: moved 2026-11-23
    _tool_contract_row,
    _ANALYSIS_TOOL_NAMES,
    _TOOL_RESULT_CAPS,
    _tool_result_cap,
    _CHAT_STATE_TOOLS,
)


from accuretta.tools.html_syntax import (  # noqa: F401  re-export: moved 2026-11-23
    _HTML_VOID_TAGS,
    _HTML_OPTIONAL_CLOSE,
    _check_html_syntax,
)


from accuretta.tools.run_tests import (  # noqa: F401  re-export: moved 2026-11-23
    _full_test_run,
    _NO_TESTS_RE,
    _no_tests_ran,
    tool_run_tests,
    _verification_key,
)


# =====================================================================
# MCP CLIENT IMPLEMENTATION
# =====================================================================

from accuretta.tools.mcp_client import (  # noqa: F401  re-export: moved 2026-11-23
    MCPClient,
)


from accuretta.agent.mcp_servers import (  # noqa: F401  re-export: moved 2026-11-24
    _prune_playwright_mcp,
    _load_mcp_servers,
    _MCP_LOCK,
    _MCP_STATE,
    _MCP_NAME_RE,
    _MCP_ENV_KEY_RE,
    _mcp_read_config,
    _mcp_write_config,
    _mcp_clean_server,
    _mcp_stop_server,
    _mcp_start_server,
    _mcp_error_text,
    _mcp_next_gen,
    _mcp_start_async,
    _mcp_tool_tokens,
    _mcp_unsandboxed,
    _mcp_list_servers,
    _mcp_save_server,
    _mcp_delete_server,
    _mcp_restart_server,
    _mcp_import,
)


from accuretta.core.mcp_paths import (  # noqa: F401  re-export: moved 2026-11-23
    _MCP_CONFIG_FILE,
)


from accuretta.core.tool_registry import TOOLS  # noqa: E402  the shared registry: fill it, never rebind
from accuretta.agent.tool_table import (  # noqa: F401,E402  fills the shared TOOLS registry; re-exports
    _OBSERVATION_SCHEMA,
    _CHALLENGE_SCHEMA,
)


from accuretta.agent.truncation import (  # noqa: F401  re-export: moved 2026-11-23
    _elide_text,
    _truncation_envelope,
)


from accuretta.agent.execution_target import (  # noqa: F401  re-export: moved 2026-11-24
    _REMOTE_TOOL_NAMES,
    _HOST_TARGET_TOOL_NAMES,
    _tool_targets_inference_host,
    _selected_execution_target,
    _execution_target_allows_tool,
    _ready_remote_machines,
    _ANALYSIS_SUITE_TOOL_NAMES,
)


from accuretta.agent.subagent_tool import (  # noqa: F401  re-export: moved 2026-11-24
    _research_action,
    _SUBAGENT_READ_TOOLS,
    _SUBAGENT_TOOL_SUMMARY,
    _SUBAGENT_MAX_TOKENS,
    _SUBAGENT_THINK_BUDGET,
    _subagent_tools,
    _subagent_complete,
    _subagent_parse_calls,
    _subagent_call_tool,
    _spawn_subagent_tool,
)


# ---- skills: user-curated markdown procedures -------------------------------
# A skill is a plain .md file in skills/ with a `---` frontmatter block:
#   ---
#   name: rt-report-loop
#   description: Produce engagement reports for red-team missions...
#   budget: 2400          # token-ish estimate shown in the picker (informational)
#   ---
#   <procedure body>
# One active slot per chat; loading replaces the previous skill. The body
# reaches the model two ways: a model-side load_skill returns it as a tool
# result, and a user pick via # injects it into the recency tail every turn
# (run_chat_turn + _active_skill_section).


from accuretta.agent.skill_save import (  # noqa: F401  re-export: moved 2026-11-23
    _skill_slug,
    _skill_description,
    _skill_token_budget,
    tool_save_skill,
)


from accuretta.agent.skill_load import (  # noqa: F401  re-export: moved 2026-11-23
    _set_active_skill,
    tool_load_skill,
)


from accuretta.agent.tool_schemas import (  # noqa: F401  re-export: moved 2026-11-24
    tools_for_llama,
    _WIRE_SCHEMA_COMBINATORS,
    _wire_tool_parameters,
)


from accuretta.core.tool_names import (  # noqa: F401  re-export: moved 2026-11-23
    TOOL_ALIASES,
    _resolve_tool_name,
)


from accuretta.agent.tool_call_parse import (  # noqa: F401  re-export: moved 2026-11-24
    _normalize_tool_argument_shapes,
    TOOL_CALL_RE,
    TOOL_CALL_FENCE_RE,
    _EXAMPLE_MARKER_RE,
    _tool_text_view,
    TOOL_CALL_NAMED_RE,
    TOOL_CALL_PYTAG_RE,
    TOOL_CALL_MISTRAL_RE,
    TOOL_CALL_XMLTAG_RE,
    TOOL_PARAM_XMLTAG_RE,
    TOOL_CALL_GEMMA_RE,
    TOOL_CALL_QWEN_RE,
    TOOL_CALL_SELFCLOSING_RE,
    TOOL_CALL_TOOLCODE_RE,
    TOOL_CALL_PYFUNC_RE,
    _parse_python_tool_call,
    _FAIL,
    extract_tool_calls,
)


from accuretta.agent.native_tools import (  # noqa: F401  re-export: moved 2026-11-24
    _NATIVE_TOOLS_BROKEN_TTL_S,
    _BULKY_ARG_TOOLS,
    _native_tools_broken_at,
    _mark_native_tools_broken,
    _native_tools_broken,
    _GEMMA_TOOL_CALL_PROMPT,
    _XML_TOOL_CALL_PROMPT,
    _text_tools_prompt,
    _warmup_tool_grammar,
    _TURN_BOUNDARY_STOPS,
    _TURN_BOUNDARY_RE,
    _turn_boundary_seen,
    _turn_boundary_stops,
    _mark_turn_boundary_runaway,
    _attach_session_state,
)


from accuretta.agent.tool_call_text import (  # noqa: F401  re-export: moved 2026-11-23
    _TOOL_CODE_OPENER_RE,
    _TOOL_CODE_CLOSER_RE,
    _TOOL_STREAM_PAIRS,
    _TOOL_STREAM_OPEN_PREFIXES,
    _tool_stream_pending_suffix,
    _classify_tool_stream,
    _js_to_json,
    repair_tool_args,
)


from accuretta.agent.tool_history import (  # noqa: F401  re-export: moved 2026-11-24
    _tool_history_lock,
    _tool_call_history,
    _tool_family_history,
    _tool_url_history,
    _tool_progress,
    _FETCH_LIKE_TOOLS,
    TOOL_REPEAT_LIMIT,
    TOOL_REPEAT_HARD_LIMIT,
    TOOL_SOFT_REPEAT_LIMIT,
    URL_CROSS_TOOL_LIMIT,
    _DIRECTORY_WALK_TOOLS,
    _reserve_tool_family_call,
    _STATE_CHANGING_OR_EGRESS_TOOLS,
    _IDEMPOTENT_READ_TOOLS,
    _tool_call_sig,
    _tool_repeat_state,
    _current_turn_progress,
    _note_soft_repeat,
    _record_tool_result,
    _fetch_url_key,
    _record_url_fetch,
    _url_fetch_count,
)
from accuretta.agent.invalid_calls import (  # noqa: F401  re-export: moved 2026-11-24
    _invalid_call_streaks,
    INVALID_CALL_LIMIT,
    _note_invalid_call,
    _clear_invalid_call,
    _invalid_call_result,
    _missing_required_tool_args,
    _invalid_tool_path,
)


from accuretta.agent.verification import (  # noqa: F401  re-export: moved 2026-11-24
    _reset_tool_call_history,
    _update_verification_debt,
    _task_check_record,
    _task_verification_summary,
    _call_write_target,
    _call_serial_key,
    _set_current_chat,
    _invoke_tool_with_context,
)


# ---- tool-call parsing (fallback for models w/o native tools) -------------
# Self-healing — accepts lightly broken JSON from the model, since local
# models routinely emit trailing prose, unbalanced braces, or code fences.


from accuretta.agent.tool_parsing import (  # noqa: F401  re-export: moved 2026-11-23
    _find_balanced_json_objects,
)


# ---- llama-server helpers --------------------------------------------------
# Everything below talks to llama.cpp's `llama-server` over its
# OpenAI-compatible /v1 endpoints.


from accuretta.agent.llama_request import (  # noqa: F401  re-export: moved 2026-11-24
    _no_think_prefill_cache,
    _NO_THINK_PROBE_SYS,
    _NO_THINK_PROBE_USER,
    _no_think_prefill,
    _LLAMA_FIRST_TOKEN_TIMEOUT_S,
    _LLAMA_STALL_TIMEOUT_S,
    llama_post_stream,
    _HTTP_ERROR_BODY_CACHE,
    _http_error_body,
    _is_ctx_overflow,
    _MODEL_LOAD_WAIT_S,
    _is_model_loading,
    _is_transient_server_error,
    llama_options,
)


from accuretta.runtime.llama_stream import (  # noqa: F401  re-export: moved 2026-11-23
    _StallGuardedStream,
    _llama_error_text,
    _is_llama_unreachable,
)


# ---- system prompt ---------------------------------------------------------


from accuretta.agent.chat_loop import (  # noqa: F401  re-export: moved 2026-11-24
    run_chat_turn,
)


from accuretta.agent.wire_messages import (  # noqa: F401  re-export: moved 2026-11-24
    _preserve_prior_thinking,
    _REASONING_FIELD_CACHE,
    _template_reads_reasoning_content,
    _split_reasoning_field,
    _sanitize_messages_for_openai,
    _VERIFY_CAP_CACHE,
    _template_supports_verification,
    _verification_wanted,
)


from accuretta.agent.thinking_recovery import (  # noqa: F401  re-export: moved 2026-11-24
    _THINKING_ONLY_RETRIES,
    _NUMBERED_LINE_RE,
    _thinking_body,
    _thinking_draft,
    _thinking_recovery_note,
    _with_thinking_note,
    _REASONING_CLEANUP_KEEP_ROUNDS,
    _REASONING_CLEANUP_MIN_FRAC,
    _reasoning_cleanup_anchor,
)


# ---- versioning ------------------------------------------------------------

from accuretta.agent.html_extract import (  # noqa: F401  re-export: moved 2026-11-23
    HTML_BLOCK_RE,
    extract_html,
)


from accuretta.agent.write_recovery import (  # noqa: F401  re-export: moved 2026-11-23
    _maybe_unescape_json_html,
    _extract_write_file_content,
)


# ---- Llama-Server 1-Click Bootstrapper -------------------------------------
import zipfile

from accuretta.runtime.llama_download import (  # noqa: F401  re-export: moved 2026-11-23
    DOWNLOAD_LOCK,
    DOWNLOAD_STATE,
    _extract_zip_safely,
    download_and_extract_llama,
)

from accuretta.runtime.gpu_detect import (  # noqa: F401  re-export: moved 2026-11-24
    detect_hardware_specs,
)


from accuretta.runtime.llama_releases import (  # noqa: F401  re-export: moved 2026-11-23
    _LLAMA_RELEASES_API,
    _LLAMA_DOWNLOAD_PREFIXES,
    _latest_llama_release_assets,
)


# ---- WSL guest for loot inspection and offensive tooling -------------------
# In a red-team run exploits execute on the target; the local risks are
# parsing untrusted loot (firmware/binaries pulled back) and the agent being
# steered by target content into touching the host. This provides an opt-in,
# separate Ubuntu WSL2 guest, `accuretta-sbx`, created via `wsl --import` so it
# never touches the user's other distros and needs no interactive first run
# (imported distros default to root). The Windows workspace is visible inside
# at /mnt/... (paths translated with wslpath).
#
# With WSL2 already present there is no reboot and no admin: download a clean
# rootfs, import it, apt-install the toolset. Only when WSL itself is absent
# does `wsl --install` need one UAC prompt and a reboot (plus firmware
# virtualization if it is off); the setup wizard surfaces those explicitly.

from accuretta.runtime.wsl_sandbox import (  # noqa: F401  re-export: moved 2026-11-23
    SANDBOX_DISTRO,
    _wsl_exe,
    _sandbox_exit_stop_lock,
    _stop_sandbox_on_exit,
    _note_sandbox_use,
    _wsl_args_enter_sandbox,
    _wsl_env,
    _wsl_run,
)
from accuretta.runtime.sandbox_setup import (  # noqa: F401  re-export: moved 2026-11-23
    SANDBOX_DIR,
    SANDBOX_APT_TOOLS,
    _SBX_BINWALK_WRAPPER,
    _sandbox_hardening_lock,
    _wsl_bash,
    _wsl_in,
    _SANDBOX_WSL_CONF,
    _SANDBOX_INTEROP_VERIFY,
    _sandbox_apply_security_profile,
    _sbx_decompress_xz,
    _sbx_provision_thread,
    start_sandbox_setup,
    sandbox_selftest,
)
from accuretta.runtime.sandbox_state import (  # noqa: F401  re-export: moved 2026-11-23
    SANDBOX_STATE_FILE,
    SANDBOX_SECURITY_PROFILE,
    _write_wsl_script,
    _wsl_distros,
    wsl_probe,
    _sbx_stream_provision,
    _SBX_READY_CACHE,
    _sandbox_ready_cached,
)
from accuretta.runtime.sandbox_rootfs import (  # noqa: F401  re-export: moved 2026-11-23
    SANDBOX_ROOTFS_URLS,
    SANDBOX_LOCK,
    SANDBOX_PROGRESS,
    _sbx_set,
    _sbx_log,
    _sbx_download_rootfs,
)


from accuretta.redteam.report import (  # noqa: F401  re-export: moved 2026-11-23
    tool_rt_generate_report,
)


# ---- Security Overview ----------------------------------------------------
# This is deliberately separate from chat. Collectors are fixed, read-only
# functions; the optional model call receives no history, memories, skills, or
# tool schemas and cannot execute anything.

from accuretta.tools.security_watch import (  # noqa: F401  re-export: moved 2026-11-24
    _security_collect_actions,
    _security_collect_processes,
    _security_collect_commands,
    _is_admin,
    _security_notify_alerts,
    _security_overview,
)


from accuretta.tools.security_model import (  # noqa: F401  re-export: moved 2026-11-23
    _security_model_busy,
    _security_json_from_model,
    _security_model_summary,
)


from accuretta.tools.security_watch import (  # noqa: F401  re-export: moved 2026-11-24
    _security_apply_schedule,
    _security_boot_scan,
)


from accuretta.tools.discord_approvals import (  # noqa: F401  re-export: moved 2026-11-23
    _presence_notify,
    _discord_state,
    _discord_approvals,
    _discord_approvals_lock,
    _APPROVAL_DM_AFTER_S,
    _discord_approval_listener,
)


# ---- HTTP handler ----------------------------------------------------------


# ---- llama-server management -----------------------------------------------
# The bridge owns the llama-server subprocess, so models can be swapped from
# the UI: set settings.model_path to the .gguf and it is (re)spawned.


# ---- VRAM detection + auto-tune --------------------------------------------
# Detect (or take) the VRAM tier and fill in sensible llama-server flags;
# every field can still be overridden. Conservative on purpose: leaving 10-15%
# VRAM headroom beats an OOM mid-generation.

from accuretta.runtime.gpu_detect import (  # noqa: F401  re-export: moved 2026-11-23
    detect_vram_gb,
    _GPU_BANDWIDTH_GBPS,
    _gpu_bandwidth_gbps,
    _tune_card_gb,
    _tune_gpus,
)


from accuretta.runtime.model_inspect import (  # noqa: F401  re-export: moved 2026-11-23
    _MOE_FILENAME_HINTS,
    _ACTIVE_RE,
    _TOTAL_RE,
    _QUANT_RE,
    inspect_model,
)


from accuretta.runtime.gguf import (  # noqa: F401  re-export: moved 2026-11-23
    read_gguf_metadata,
)


from accuretta.core.effort import (  # noqa: F401  re-export: moved 2026-11-23
    _REASONING_LEVELS,
    _EFFORT_RANK,
    _EFFORT_CHOICES,
    _normalize_effort_choice,
    _effort_tier,
    _normalize_reasoning_effort,
)


from accuretta.runtime.reasoning_capability import (  # noqa: F401  re-export: moved 2026-11-23
    _model_reasoning_was_observed,
    _EFFORT_VAR,
    _effort_names,
    _parse_native_effort_levels,
    _reasoning_capability,
)


from accuretta.runtime.model_sampling import (  # noqa: F401  re-export: moved 2026-11-24
    _mark_model_reasoning_observed,
    llama_too_old_message,
    draft_match_payload,
    _SWITCH_RESTORE_KEYS,
    _model_no_tools_sampling,
    _apply_model_sampling,
    _preserve_manual_model_spec,
    _auto_tune_for_load,
)


from accuretta.runtime.reasoning_request import (  # noqa: F401  re-export: moved 2026-11-23
    _nearest_native_level,
    _reasoning_budget_for_effort,
    _resolve_reasoning_request,
)


from accuretta.runtime.tuning_math import (  # noqa: F401  re-export: moved 2026-11-23
    _reasoning_payload_overrides,
    _mla_dims,
    _layer_passes,
    _speculative_choice,
    _probe_ram_bandwidth_gbps,
)


from accuretta.runtime.kv_sizing import (  # noqa: F401  re-export: moved 2026-11-23
    _KV_DTYPE_BYTES,
    _HYBRID_KEY_FRAGMENTS,
    _HYBRID_INTERVAL_SUFFIXES,
    _hybrid_info,
    _loop_factor,
    _kv_bytes_per_token,
    _kv_per_1k_mb,
    _SWA_PATTERN_SUFFIXES,
    _swa_info,
    _kv_total_bytes,
    _kv_total_mb,
    _decode_bytes_per_token,
    _checkpoint_spawn_flags,
)


# ---- system memory probes (auto-tune safety) --------------------------------


from accuretta.runtime.model_files import (  # noqa: F401  re-export: moved 2026-11-23
    _resolve_mmproj_for_tune,
    _resolve_spec_draft_for_tune,
    _gguf_model_identities,
    find_mmproj_for,
    _is_mmproj_name,
    _DRAFT_NAME_RE,
    _SPEC_FINGERPRINT_CACHE,
    _SPEC_FINGERPRINT_CACHE_MAX,
    _is_draft_name,
    _spec_fingerprint,
    _identity_match,
    spec_draft_match,
    _draft_candidates,
    find_spec_draft_for,
    list_spec_drafts,
    scan_gguf_dir,
    find_all_gguf_files,
)


# ---- decode speed model + exact fit (auto-tune) ------------------------------
# Decode is memory-bound: every generated token reads the GPU-resident weights
# (for MoE only the active experts), the KV cache filled so far, and whatever
# weights live in system RAM. Predicted tok/s =
#     spec_gain × calibration / (gpu_bytes / gpu_bw + cpu_bytes / ram_bw)
# where gpu_bw starts from the card's published bandwidth and calibration is
# learned from the passive per-turn telemetry (_record_model_observation), so
# the estimate converges on this machine's real speed with no benchmark run.


from accuretta.runtime.auto_tune import (  # noqa: F401  re-export: moved 2026-11-24
    _tune_budget_gb,
)


from accuretta.runtime.llama_bin import (  # noqa: F401  re-export: moved 2026-11-23
    _fit_params_bin,
    _LLAMA_ARCH_TABLE,
    _ARCH_NAME_RE,
    llama_knows_arch,
    find_llama_bin,
)


from accuretta.runtime.llama_caps import (  # noqa: F401  re-export: moved 2026-11-23
    _LLAMA_CAPS_CACHE,
    llama_caps,
)


from accuretta.runtime.maya_find import (  # noqa: F401  re-export: moved 2026-11-23
    _MAYA_DIR_NAMES,
    _MAYA_ARCHES,
    _MAYA_META_CACHE,
    find_maya_engine,
    _maya_gguf_meta,
    _maya_only_model,
    _maya_engine_for,
)
from accuretta.runtime.maya_sizing import (  # noqa: F401  re-export: moved 2026-11-23
    _MAYA_FIXED_GIB,
    _MAYA_DRAFT_EXTRA_GIB,
    _MAYA_DRAFT_CTX,
    _MAYA_MARGIN_GIB,
    _MAYA_VISION_GPU_GIB,
    _GIB,
    maya_needed_message,
    _maya_weights_gib,
    maya_fit,
    _maya_vision_on_gpu,
    _maya_tokenizer_dir,
    _trim_maya_engine_memory,
)


from accuretta.runtime.maya import (  # noqa: F401  re-export: moved 2026-11-23
    _maya_kv_bytes_per_token,
    _maya_drafter_for,
    _maya_cuda_dirs,
    _process_children,
)


# ---- servers the agent started -----------------------------------------------
# The agent's browser may click and type on a local page only when the server
# behind it is one the agent's own tools started (a site it is building, a test
# page it serves). Everything else listening on this PC (other apps' local
# pages, Accuretta itself, the model server) stays read-only for it.
# On Windows the tools' processes go into _agent_job, which settles it even when
# the shell in between has exited (`cmd /c start ...`). Elsewhere, or when
# Windows refuses the job, the parent chain is walked instead, with creation
# times so that a reused process id can't pass.

from accuretta.security.agent_processes import (  # noqa: F401  re-export: moved 2026-11-23
    _AGENT_PROCS,
    _AGENT_PROCS_LOCK,
    _started_by_agent,
    _agent_served_ports,
    _parse_llama_port,
    _agent_job,
)
from accuretta.security.agent_process_note import (  # noqa: F401  re-export: moved 2026-11-23
    _AGENT_PROCS_MAX,
    _note_agent_process,
)


from accuretta.security.agent_process_note import (  # noqa: F401  re-export: moved 2026-11-23
    _note_agent_process_end,
)


# ---- speculative draft models (DFlash / DSpark) -----------------------------
# A draft GGUF is a small network trained against one specific target model; it
# runs alongside it via --spec-draft-model and is not loadable on its own. The
# picker must hide it, and auto-tune must pair it by metadata — never by name
# alone, because pairing a mismatched draft wastes VRAM and can crash the server.


from accuretta.tools.email_settings import (  # noqa: F401  re-export: moved 2026-11-23
    EMAIL_PLUGIN_DIR,
    EMAIL_CONFIG_FILE,
    _EMAIL_LOCK,
    _EMAIL_ADDR_RE,
    _EMAIL_HOST_RE,
    _email_config_raw,
    _email_port,
    email_settings,
    _email_text,
    _email_address,
    update_email_settings,
)


from accuretta.tools.email_settings import (  # noqa: F401  re-export: moved 2026-11-24
    _email_imap_host,
    email_test_connection,
)


# ---- model menu: clean names, families and quant levels ----
# The menu shows "Qwen3.8 27B" rather than "Qwen3.8-27B-UD-Q4_K_XL.gguf": the
# quant and the uploader's build tags come off the name (the version rows show
# them again), files of one model share a group, and the family behind the
# rail's logo comes from the GGUF header.

from accuretta.runtime.model_display import (  # noqa: F401  re-export: moved 2026-11-23
    _GGUF_FILE_TYPES,
    _NAME_QUANT_RE,
    _NAME_FILE_TAGS,
    _NAME_ACRONYMS,
    _NAME_SIZE_RE,
    MODEL_FAMILIES,
    _FAMILY_BY_ORG,
    _FAMILY_BY_ARCH,
    _FAMILY_BY_NAME,
    _model_family,
    _quant_bits,
    _display_word,
    model_display_info,
    _MODEL_INFO_CACHE,
    model_display_info_for,
)

from accuretta.runtime.logos import (  # noqa: F401  re-export: moved 2026-11-23
    _LOGO_DIR,
    _match_logo,
)


from accuretta.runtime.model_folders import suggest_models_dir  # noqa: E402


from accuretta.runtime.model_presets import (  # noqa: F401  re-export: moved 2026-11-23
    _MODEL_SAMPLING_PRESETS,
    _model_preset_entry,
    _model_preset,
    _model_features,
)


from accuretta.core.jobs import (  # noqa: F401  re-export: moved 2026-11-23
    _KillOnCloseJob,
)


from accuretta.runtime.vision_server import (  # noqa: F401  re-export: moved 2026-11-24
    _vision_llama,
    _start_vision_fallback_if_needed,
)


# ---- main ------------------------------------------------------------------

from accuretta.agent.discord_chat import (  # noqa: F401  re-export: moved 2026-11-24
    DISCORD_CHAT_ID,
    _discord_split,
    _discord_strip_think,
    _DISCORD_FRIEND_PROMPT,
    _run_discord_turn,
    _discord_start,
)


from accuretta.core.workspace_watch import (  # noqa: F401  re-export: moved 2026-11-23
    _WORKSPACE_WATCH_STOP,
    _WORKSPACE_WATCH_LOCK,
    _WORKSPACE_WATCHERS,
    _broadcast_workspace_update_debounced,
    _stop_workspace_watching,
)


from accuretta.core.workspace_native_watch import (  # noqa: F401  re-export: moved 2026-11-23
    _WindowsWorkspaceWatcher,
    _workspace_native_supervisor,
    _workspace_fallback_scanner,
    _start_workspace_watching,
)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            with open("crash_log.txt", "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
