# SPDX-License-Identifier: AGPL-3.0-only
"""The capability_report tool: what this install can do right now (model, vision,
desktop, sandbox, optional packages).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import shutil
import time
from accuretta.agent.tool_contracts import _tool_contract_row
from accuretta.agent.tool_sets import _unavailable_runtime_tools
from accuretta.agent.tool_sets import _visible_tool_names
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.optional_imports import _HAVE_ANDROGUARD
from accuretta.core.optional_imports import _HAVE_CAPSTONE
from accuretta.core.optional_imports import _HAVE_DPKT
from accuretta.core.optional_imports import _HAVE_ELFTOOLS
from accuretta.core.optional_imports import _HAVE_EVTX_PY
from accuretta.core.optional_imports import _HAVE_EVTX_RUST
from accuretta.core.optional_imports import _HAVE_PEFILE
from accuretta.core.optional_imports import _HAVE_PIL
from accuretta.core.optional_imports import _HAVE_PLAYWRIGHT
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import _HAVE_PYELFTOOLS
from accuretta.core.optional_imports import _HAVE_PYGHIDRA
from accuretta.core.optional_imports import _HAVE_SQUASHFS
from accuretta.core.optional_imports import _HAVE_UIA
from accuretta.core.optional_imports import _HAVE_YARA
from accuretta.core.settings import get_settings
from accuretta.core.tool_registry import TOOLS
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.network_scope import _rt_incl_exploit_for
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_advisor import _model_runtime_profiles
from accuretta.runtime.sandbox_state import _sandbox_ready_cached
from accuretta.runtime.vision_ready import _vision_observation_available
from accuretta.tools.apk import _resolve_jadx_bin
from accuretta.tools.desktop_guard import _desktop_panic


def _capability_health(chat_id: str = "") -> dict:
    """Truthful local capability inventory; no tools are executed."""
    settings = get_settings()
    schema_issues = []
    for name, spec in TOOLS.items():
        if not isinstance(spec, dict) or not callable(spec.get("fn")):
            schema_issues.append({"tool": name, "issue": "missing callable handler"})
            continue
        params = spec.get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            schema_issues.append({"tool": name, "issue": "parameters must be an object schema"})
            continue
        props = params.get("properties") or {}
        for req in params.get("required") or []:
            if req not in props:
                schema_issues.append({"tool": name, "issue": f"required field not declared: {req}"})

    py_caps = {
        "desktop_automation": bool(_HAVE_PYAUTOGUI),
        "desktop_accessibility": bool(_HAVE_UIA),
        "scoped_browser": bool(_HAVE_PLAYWRIGHT),
        "image_processing": bool(_HAVE_PIL),
        "apk_deep_metadata": bool(_HAVE_ANDROGUARD),
        "native_pe": bool(_HAVE_PEFILE),
        "native_elf": bool(_HAVE_PYELFTOOLS or _HAVE_ELFTOOLS),
        "disassembly": bool(_HAVE_CAPSTONE),
        "ghidra": bool(_HAVE_PYGHIDRA),
        "yara": bool(_HAVE_YARA),
        "evtx": bool(_HAVE_EVTX_RUST or _HAVE_EVTX_PY),
        "pcap": bool(_HAVE_DPKT),
        "squashfs": bool(_HAVE_SQUASHFS),
    }
    external = {
        "git": shutil.which("git") or "",
        "node": shutil.which("node") or "",
        "powershell": shutil.which("pwsh") or shutil.which("powershell") or "",
        "jadx": _resolve_jadx_bin() or "",
        "java": shutil.which("java") or "",
    }
    try:
        chat = (get_chats().get("chats", {}) or {}).get(chat_id) if chat_id else None
    except Exception:
        chat = None
    visible = _visible_tool_names(_rt_incl_exploit_for(chat, settings), chat_id)
    unavailable = _unavailable_runtime_tools()
    recommendations = []
    if settings.get("desktop_enabled") and not py_caps["desktop_accessibility"]:
        recommendations.append("Install uiautomation for CPU-only blind desktop snapshots and semantic actions.")
    if settings.get("desktop_enabled") and not py_caps["desktop_automation"]:
        recommendations.append("Install pyautogui only if raw mouse/keyboard fallback is needed.")
    if settings.get("analysis_tools_enabled"):
        if not py_caps["yara"]:
            recommendations.append("Install yara-python for rule-based file triage.")
        if not py_caps["pcap"]:
            recommendations.append("Install dpkt for PCAP analysis.")
        if not py_caps["evtx"]:
            recommendations.append("Install evtx or python-evtx for Windows event-log analysis.")
        if not py_caps["ghidra"]:
            recommendations.append("Install pyghidra plus Ghidra/JDK 21 for decompilation; binary triage still works without it.")
    if settings.get("red_team_enabled") and not py_caps["scoped_browser"]:
        recommendations.append(
            "Install playwright and its Chromium browser to validate JavaScript-heavy findings inside the engagement scope.")
    try:
        current_profile = (_model_runtime_profiles().get("models", {}) or {}).get(
            _model_profile_key(settings.get("model") or ""), {})
    except Exception:
        current_profile = {}
    return {
        "ok": not schema_issues,
        "generated": int(time.time()),
        "registered_tools": len(TOOLS),
        "visible_tools": len(visible),
        "schema_issues": schema_issues,
        "unavailable_tools": unavailable,
        "python_capabilities": py_caps,
        "external_programs": {k: {"available": bool(v), "path": v} for k, v in external.items()},
        "feature_groups": {
            "ide": {"enabled": True},
            "system_agent": {"enabled": bool(settings.get("desktop_enabled")),
                             "panic": _desktop_panic.is_set(),
                             "blind_control": bool(_HAVE_UIA),
                             "vision": _vision_observation_available()},
            "blue_team_analysis": {"enabled": bool(settings.get("analysis_tools_enabled"))},
            "red_team": {"enabled": bool(settings.get("red_team_enabled")),
                         "authorized_this_chat": bool(_rt_authorized_mission(chat))},
            "sandbox": {"available": bool(_sandbox_ready_cached())},
        },
        "current_model_runtime": current_profile,
        "model_tuning_policy": ("Normal work supplies content-free observations. Advice begins after 8 turns; "
                                "no setting is changed automatically and no first-run benchmark is required."),
        "recommendations": recommendations,
        "note": ("Availability is checked without executing project or target code. A tool may still fail on a "
                 "specific input; its result remains the source of truth."),
    }


def tool_capability_report(args: dict) -> dict:
    """Show which Accuretta capabilities can actually work on this machine."""
    chat_id = str(args.get("chat_id") or _current_chat_id.get() or _get_current_chat() or "")
    report = _capability_health(chat_id)
    detail = str(args.get("detail") or "summary").lower()
    if detail in ("visible", "all"):
        visible = _visible_tool_names(True, chat_id)
        unavailable = report.get("unavailable_tools") or {}
        names = sorted(TOOLS if detail == "all" else visible)
        report["tool_contracts"] = [_tool_contract_row(n, visible, unavailable) for n in names]
    return report
