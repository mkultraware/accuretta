# SPDX-License-Identifier: AGPL-3.0-only
"""Unlocking more tools: the list_more_tools tool, bundles unlocked by what the user
asks, and switching where tools run when the user asks for it.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import threading
from accuretta.agent.execution_target import _execution_target_allows_tool
from accuretta.agent.execution_target import _ready_remote_machines
from accuretta.agent.execution_target import _selected_execution_target
from accuretta.agent.tool_sets import _TOOL_BUNDLES
from accuretta.agent.tool_sets import _base_core_tool_names
from accuretta.agent.tool_sets import _excluded_tools
from accuretta.agent.tool_sets import _unlocked_bundles_by_chat
from accuretta.core.chats import _get_current_chat
from accuretta.core.events import broadcast_event
from accuretta.core.tool_registry import TOOLS
from accuretta.runtime.model_observation import _client_context_by_chat
from accuretta.security.approvals import request_approval
from accuretta.tools.remote_machines import _remote_machine


# Bundles the user's own words plainly call for. Small models rarely call
# list_more_tools before reaching for PowerShell (a reverse-engineering run never
# found ghidra_analyze), so a request that names the work loads its bundle at the
# start of the turn, as the desktop bundle already did. Patterns stay narrow: a
# false hit only costs the bundle's schemas, but it costs them every request.
_BUNDLE_INTENT_RES = {
    "analysis": re.compile(
        r"\b(?:decompil\w*|disassembl\w*|reverse[- ]?engineer\w*|ghidra|radare2?|jadx|"
        r"firmware|binwalk|squashfs|yara|malware|pcap\w*|packet captures?|evtx|"
        r"binary (?:file|blob|dump|analysis|exploitation)|crackme|keygen|"
        r"ELF (?:file|binary|executable|program)|"
        r"(?:x86[-_]?64|x64|arm64|aarch64|i386) (?:binary|program|executable)|"
        r"\w+\.(?:apk|pcapng?|evtx)\b|"
        r"(?:analy[sz]e|inspect|crack|patch|disassemble|decompile|reverse)\b[^\n]{0,60}"
        r"\.(?:exe|dll|so|elf|bin|img)\b)",
        re.IGNORECASE),
    "sessions": re.compile(
        r"\b(?:repl|interactive (?:shell|session|program|prompt)|debugger|gdb|lldb|pdb|"
        r"netcat|ncat|reverse shell|ssh (?:into|to|session)|telnet|"
        r"(?:dev|development|local) server|keep (?:it|the server|the app) running|long[- ]running)\b",
        re.IGNORECASE),
    "git": re.compile(
        r"\b(?:commit (?:this|that|it|these|the|my|all)|pull request|rebase|cherry[- ]pick|"
        r"git (?:add|commit|push|pull|fetch|reset|restore|clone|init|remote|show|checkout))\b",
        re.IGNORECASE),
}


_MSG_PATH_RE = re.compile(r"""(?:[A-Za-z]:[\\/]|\\\\|/)[^\s"'<>|*?`]+""")


_NATIVE_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                 b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def _message_binary_paths(text: str, limit: int = 8) -> list[str]:
    """Paths in a message that exist and start like a native program (ELF, PE,
    Mach-O). Reads 4 bytes per file; at most `limit` paths are looked at."""
    found = []
    for raw in _MSG_PATH_RE.findall(text or "")[:limit]:
        path = raw.rstrip(".,;:)]}")
        try:
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    head = fh.read(4)
                if any(head.startswith(m) for m in _NATIVE_MAGIC):
                    found.append(path)
        except OSError:
            continue
    return found


def _auto_unlock_bundles(chat_id: str, text: str) -> list[str]:
    """Load the bundles the user's message plainly asks for. Only bundles with a
    tool the current settings allow; returns the names newly loaded."""
    if not chat_id or not text:
        return []
    loaded: list[str] = []
    excluded = None
    names_binary = None
    for bundle, pattern in _BUNDLE_INTENT_RES.items():
        if bundle in _unlocked_bundles_by_chat.get(chat_id, ()):
            continue
        if not pattern.search(text):
            if bundle != "analysis":
                continue
            # Words miss ("keycheck is a Linux program that validates a key"), so
            # also look at the files the message names: a native program there
            # means the reverse-engineering tools are what the task needs.
            if names_binary is None:
                names_binary = bool(_message_binary_paths(text))
            if not names_binary:
                continue
        if excluded is None:
            excluded = _excluded_tools(True, chat_id)
        if any(n in TOOLS and n not in excluded for n in (_TOOL_BUNDLES.get(bundle) or ())):
            if _unlock_bundle(chat_id, bundle):
                loaded.append(bundle)
    return loaded


_TARGET_ACTION = r"(?:on|in|from|to|use|using|check|search|scan|inspect|open|look\s+(?:at|in)|work\s+on)"


_HOST_TARGET_INTENT_RE = re.compile(
    rf"(?:\b{_TARGET_ACTION}\s+(?:the\s+)?(?:pc|inference\s+(?:pc|device|host)|windows\s+(?:pc|host|machine))\b"
    r"|\bpc\s+workspace\b|\bmeant\s+(?:on\s+)?(?:the\s+)?(?:pc|inference\s+(?:pc|device|host))\b)",
    re.IGNORECASE,
)


_REMOTE_TARGET_INTENT_RE = re.compile(
    rf"(?:\b{_TARGET_ACTION}\s+(?:the\s+)?(?:mac|macbook|macos|paired\s+mac|remote\s+mac(?:hine)?)\b"
    r"|\bmac(?:book)?\s+workspace\b|\bmeant\s+(?:on\s+)?(?:the\s+)?(?:mac|macbook|paired\s+mac)\b)",
    re.IGNORECASE,
)


def _user_explicitly_requested_execution_target(target: str, machine: dict | None = None) -> bool:
    """Conservative user-intent gate for a model-requested target change."""
    context = _client_context_by_chat.get(_get_current_chat() or "", {})
    text = str(context.get("_user_text") or "")
    if target == "host":
        return bool(_HOST_TARGET_INTENT_RE.search(text))
    if _REMOTE_TARGET_INTENT_RE.search(text):
        return True
    label = str((machine or {}).get("label") or "").strip()
    if not label or label.casefold() not in text.casefold():
        return False
    prefix = text.casefold().split(label.casefold(), 1)[0][-80:]
    return bool(re.search(_TARGET_ACTION + r"\s+(?:the\s+)?$", prefix, re.IGNORECASE))


def tool_switch_execution_target(args: dict) -> dict:
    """Change the chat's device routing without performing work on either device."""
    chat_id = _get_current_chat()
    if not chat_id:
        return {"error": "No active chat is available for an execution-target switch."}

    requested = str(args.get("target") or "").strip()
    remotes = _ready_remote_machines()
    resolved = requested
    machine = None
    if requested.lower() in {"pc", "windows", "inference_pc", "inference-host"}:
        resolved = "host"
    elif requested.lower() in {"mac", "remote", "paired_mac", "paired-mac"}:
        if len(remotes) != 1:
            return {
                "error": "A paired Mac alias is only unambiguous when exactly one ready Mac exists.",
                "available_targets": [
                    {"id": "host", "label": "Inference PC"},
                    *[{"id": str(row.get("id") or ""), "label": str(row.get("label") or "Paired Mac")}
                      for row in remotes],
                ],
            }
        machine = remotes[0]
        resolved = str(machine.get("id") or "")
    elif requested != "host":
        machine = next((row for row in remotes if str(row.get("id") or "") == requested), None)
        if machine is None:
            return {
                "error": f"Unknown or unavailable execution target: {requested}",
                "available_targets": [
                    {"id": "host", "label": "Inference PC"},
                    *[{"id": str(row.get("id") or ""), "label": str(row.get("label") or "Paired Mac")}
                      for row in remotes],
                ],
            }

    previous = _selected_execution_target(chat_id)
    if previous != resolved and not _user_explicitly_requested_execution_target(resolved, machine):
        previous_machine = _remote_machine(previous) if previous != "host" else None
        previous_label = str((previous_machine or {}).get("label") or "Inference PC")
        next_label = str((machine or {}).get("label") or "Inference PC")
        approval = request_approval(
            title="Switch command target",
            command=f"{previous_label} -> {next_label}",
            details={
                "kind": "execution_target",
                "force_prompt": True,
                "from_target": previous,
                "target": resolved,
            },
        )
        if approval.get("decision") != "approve":
            return {
                "error": f"User denied execution-target switch ({approval.get('status')}).",
                "not_executed": True,
                "execution_target": previous,
            }
    current = dict(_client_context_by_chat.get(chat_id) or {})
    current["execution_target"] = resolved
    current["target_label"] = str(machine.get("label") or "Paired Mac") if machine else "Inference PC"
    _client_context_by_chat[chat_id] = current
    if resolved != "host":
        _unlock_bundle(chat_id, "remote")

    if resolved == "host":
        next_step = "Use the inference-PC workspace tools now exposed, such as list_directory, find_files, and read_file."
    else:
        next_step = "Use the remote Mac tools now exposed, such as remote_list_directory and remote_read_file."
    return {
        "ok": True,
        "execution_target_changed": previous != resolved,
        "execution_target": resolved,
        "previous_target": previous,
        "target_label": current["target_label"],
        "next_step": next_step,
    }


_TOOL_BUNDLE_LOCK = threading.Lock()


def _unlock_bundle(chat_id: str, bundle: str) -> bool:
    """Load a bundle's schemas for this chat. Returns True if the bundle was
    newly unlocked (False = unknown bundle or already loaded)."""
    if not chat_id or bundle not in _TOOL_BUNDLES:
        return False
    with _TOOL_BUNDLE_LOCK:
        st = _unlocked_bundles_by_chat.setdefault(chat_id, set())
        if bundle in st:
            return False
        st.add(bundle)
    try:
        broadcast_event({"type": "tools_unlocked", "chat_id": chat_id, "bundle": bundle})
    except Exception:
        pass
    return True


def tool_list_more_tools(args: dict) -> dict:
    """Probe tool for the lazy tool bundles: shows which specialized bundles
    exist (and which are loaded), or loads one by name so its schemas join the
    tool list for the rest of this chat."""
    chat_id = _get_current_chat()
    bundle = str(args.get("bundle") or "").strip().lower()
    excluded = _excluded_tools(True, chat_id)
    enabled: dict[str, list[str]] = {}
    for bname, btools in _TOOL_BUNDLES.items():
        if btools is None:
            btools = {n for n in TOOLS if n.startswith("mcp_")}
        if not btools:
            continue
        # Advertise only bundles at least one of whose tools the current
        # settings would actually load (desktop / red-team / analysis gates).
        usable = sorted(
            n for n in btools
            if n in TOOLS and n not in excluded
            and _execution_target_allows_tool(n, chat_id)
        )
        if usable:
            enabled[bname] = usable
    if not bundle:
        loaded = set(_unlocked_bundles_by_chat.get(chat_id, ()))
        return {
            "note": "Call again with one of the bundle names shown below to inspect its tools and load it into this chat's schema.",
            "core": sorted(
                n for n in _base_core_tool_names(chat_id)
                if n in TOOLS and n not in excluded
                and _execution_target_allows_tool(n, chat_id)
            ),
            "bundles": {b: {"tools": names, "loaded": b in loaded}
                        for b, names in enabled.items()},
        }
    if bundle not in enabled:
        return {"error": f"unknown bundle '{bundle}' — available: {sorted(enabled)}"}
    names = enabled[bundle]
    details = {n: (TOOLS[n].get("description", "") or "")[:400] for n in names}
    loaded_now = _unlock_bundle(chat_id, bundle)
    return {
        "bundle": bundle,
        "loaded": True,
        "loaded_now": loaded_now,
        "tools": details,
        "note": ("This bundle's tools are now in the tool schema for the rest of this chat. "
                 "Call them directly — no need to re-list."),
    }
