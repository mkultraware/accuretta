# SPDX-License-Identifier: AGPL-3.0-only
"""Approvals: the approval mode, quiet gating's decision (run, run because the user
asked, ask, or block), goal-mode waiting, the approval cards and their answers, and the
decision log.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
import uuid
from accuretta.core.chat_events import _chat_emitters
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.events import broadcast_event
from accuretta.core.paths import DATA
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.security.desktop_intent import _DESKTOP_INPUT_KINDS
from accuretta.security.desktop_intent import _DESKTOP_SESSION_SENTINEL
from accuretta.security.desktop_intent import _desktop_session_unlocked
from accuretta.security.desktop_intent import _desktop_target_owned
from accuretta.security.desktop_intent import _user_asked_desktop_interaction
from accuretta.security.git_rules import _git_concern
from accuretta.security.session_allow import _approval_session_allow
from accuretta.security.soft_commands import _big_delete_concern
from accuretta.security.soft_commands import _soft_command_concerns
from accuretta.security.soft_commands import _soft_delete_reason
from accuretta.security.soft_paths import _soft_target_ok
from accuretta.security.soft_patterns import _is_destructive_command
from accuretta.security.soft_patterns import _soft_open_concern
from accuretta.security.user_words import _CHAT_ALLOWABLE
from accuretta.security.user_words import _concern_authorized
from accuretta.security.user_words import _concern_category
from accuretta.security.user_words import _remember_turn_approval
from accuretta.security.web_guard import _SOFT_COMMAND_KINDS
from accuretta.security.web_guard import _is_desktop_approval_kind
from accuretta.security.web_guard import _web_guard_reason
from conversation_tools import SteeringInbox
from accuretta.security.path_guards import is_blocked_path


_steering = SteeringInbox()


PENDING_DIR = DATA / "pending"


# Tool kinds soft mode runs without asking: analysis tools that read, or write
# only their own output. A kind not named here or handled by its own rule asks,
# so a new tool can't slip through soft mode unreviewed.
_SOFT_AUTO_KINDS = {
    "analyze_pcap", "decompile_apk", "carve_file", "extract_archive", "extract_squashfs",
    "event_log", "ghidra_analyze", "network_snapshot", "parse_event_log", "persistence_hunt",
    "recon_s3", "save_skill", "secret_scan",
}


_MCP_SOFT_BLOCK = re.compile(r"delete|remove|drop|destroy|terminate|unregister", re.IGNORECASE)


def _approval_mode() -> str:
    """soft|medium|hard. Legacy fallback: auto_approve_write -> medium/hard."""
    try:
        m = str(get_settings().get("approval_mode") or "").strip().lower()
        if m in ("soft", "medium", "hard"):
            return m
        return "medium" if get_settings().get("auto_approve_write") else "hard"
    except Exception:
        return "hard"


# -- decision log ---------------------------------------------------------------------
# What the gate decided, without the command text: tool kind, reason categories,
# ran / asked / blocked, the answer and how long it took. Shows which questions
# are noise (approved nearly every time) and which ones matter.
APPROVAL_LOG_FILE = DATA / "approval_log.jsonl"


_APPROVAL_LOG_MAX = 5000


_approval_log_lock = threading.Lock()


def _log_gate_decision(details: dict, result: dict, waited_s: float, reason: str = "") -> None:
    try:
        details = details or {}
        status = str(result.get("status") or "")
        if status in ("auto-approved", "session-approved", "goal-auto-approved", "words-approved"):
            outcome = "ran"
        elif status == "blocked":
            outcome = "blocked"
        else:
            outcome = "asked"
        reasons = list(details.get("soft_reasons") or ([reason] if reason else []))
        row = {"t": int(time.time()), "chat_id": _get_current_chat() or "",
               "kind": str(details.get("kind") or ""), "outcome": outcome, "status": status,
               "decision": result.get("decision"), "critical": bool(details.get("critical")),
               "categories": sorted({_concern_category(r)[0] for r in reasons}) if reasons else [],
               "reasons": [re.sub(r":\s.*$", "", str(r))[:80] for r in reasons[:4]],
               "mode": _approval_mode(), "goal": bool(details.get("goal_gated")),
               "waited_s": round(float(waited_s or 0), 1)}
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _approval_log_lock:
            with open(APPROVAL_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
            if APPROVAL_LOG_FILE.stat().st_size > 2_000_000:
                lines = APPROVAL_LOG_FILE.read_text(encoding="utf-8").splitlines(True)
                APPROVAL_LOG_FILE.write_text("".join(lines[-_APPROVAL_LOG_MAX:]), encoding="utf-8")
    except Exception:
        pass


_approvals: dict[str, dict] = {}


_approval_events: dict[str, threading.Event] = {}


_approvals_lock = threading.Lock()


# Approval kinds that are ordinary in-workspace file writes; "trust writes"
# skips the prompt for them. The calling tools have already checked the path
# is inside the workspace and not a blocked system path.
_SAFE_WRITE_KINDS = {"write_file", "edit_file", "replace_ast_node"}


# ---- /goal full-autonomy mode ---------------------------------------------
# "/goal" latches hands-free mode on for the chat: approvals auto-pass except
# deletions and other destructive actions. The latch persists across turns until
# "/goal off" (or stop / end / done / cancel) or a click on the Goal chip;
# "/goal <task>" latches and runs the task. Critical / force_prompt /
# web-tainted / protected-path requests are excluded before the goal check, so
# /goal cannot bypass the ClickFix guard or a protected-path gate.
#
# The latch lives in memory (a bridge restart clears it) and is mirrored onto
# chat["goal_mode"] so it survives a UI reload and is rehydrated next turn.
#
# Kinds below stay gated even under /goal: they delete data or make a
# hard-to-reverse change. remote_* act on a paired Mac (a separate device) and
# desktop.close can discard unsaved work.
_GOAL_GATE_KINDS = {
    "registry", "dangerous_import", "security_change",
    "remote_shell", "remote_file_write", "remote_file_copy",
    "desktop.close",
}


_goal_mode_chats: set[str] = set()


_goal_mode_lock = threading.Lock()


def _chat_in_goal_mode(chat_id: str) -> bool:
    if not chat_id:
        return False
    with _goal_mode_lock:
        return chat_id in _goal_mode_chats


def _goal_action_is_gated(kind, command: str, cwd: str | None = None,
                          run_cwd: str | None = None) -> bool:
    """True when a request must keep its approval gate even under /goal: a
    hard-to-reverse kind, a deletion Soft would ask about (outside the
    workspace, a whole project, a big folder), throwing away work or
    rewriting Git history, or code it runs that Soft would stop for. Deleting
    its own scratch files inside the workspace no longer stops a long run."""
    if kind in _GOAL_GATE_KINDS:
        return True
    if kind == "delete":
        kind = "powershell"
    if kind in _SOFT_COMMAND_KINDS:
        code_concerns: list[str] = []
        try:
            if kind == "git":
                try:
                    argv = shlex.split(command or "")
                except ValueError:
                    argv = (command or "").split()
                concern = _git_concern(argv[1:] if argv[:1] == ["git"] else argv)
                concerns = [concern] if concern else []
            else:
                concerns = _soft_command_concerns(command or "", kind, cwd, code_out=code_concerns,
                                                  run_cwd=run_cwd)
        except Exception:
            return True
        if code_concerns:
            return True
        return any(_concern_category(c)[0] in ("delete", "history") or "registry" in c
                   or c.startswith(("stops Accuretta", "formats", "reads a private key"))
                   for c in concerns)
    return _is_destructive_command(command or "")


def _announce_approval(entry: dict) -> None:
    """Tell every window about a new approval, and the device running the
    turn first, down its own chat stream. That stream is live by definition;
    the shared event stream can arrive minutes late on a remote browser
    (measured: a Mac over Tailscale showed the card ~2 min after the PC)."""
    emit = _chat_emitters.get(str(entry.get("chat_id") or ""))
    if emit:
        # Its own type: other windows ignore it and keep using approval:new,
        # and a turn whose emitter only broadcasts (the notch) or drops events
        # (Discord) loses nothing.
        try:
            emit({"type": "approval_request", "approval": entry})
        except Exception:
            pass               # the turn's client went away
    broadcast_event({"type": "approval:new", "approval": entry})


# Under /goal a question waits this long, then the action is skipped and the
# model is told to carry on and list it at the end, instead of a long run
# sitting idle for ten minutes on every question.
_GOAL_ASK_WAIT_S = 90


def request_approval(title: str, command: str, details: dict | None = None, timeout_s: int = 600) -> dict:
    """Create a pending approval, block the worker until the user responds, and
    return the decision (see _request_approval_core). Every decision goes into
    the approval log, without the command text."""
    details = details if details is not None else {}
    started = time.monotonic()
    result = _request_approval_core(title, command, details, timeout_s)
    waited = time.monotonic() - started if result.get("status") in ("decided", "timeout") or str(
        result.get("status") or "").startswith("skipped") else 0.0
    _log_gate_decision(details, result, waited)
    return result


def _shell_concerns_for(kind, command: str, details: dict) -> list[str]:
    """Soft mode's reasons to ask for a shell-ish request."""
    if kind == "git":
        argv = details.get("argv")
        if isinstance(argv, list):
            concern = _git_concern(argv)
            return [concern] if concern else []
        return ["changes the Git repository"]
    return _soft_command_concerns(command or "", kind, details.get("cwd"), run_cwd=details.get("run_cwd"))


def _request_approval_core(title: str, command: str, details: dict, timeout_s: int) -> dict:
    """Soft mode allows routine, non-destructive project work and what the
    user's own words asked for; medium trusts only validated in-workspace
    writes; hard prompts for every action. Critical, remote, web-tainted and
    forced actions always keep their stricter gate."""
    kind = details.get("kind")
    chat_id = _get_current_chat()
    # Web-taint gate: this action comes from untrusted web text (see
    # _web_guard_reason). That is the ClickFix chain, so prompt even in soft
    # mode, under /goal and for a session-remembered kind.
    if details.get("web_tainted") is None:
        _web_reason = _web_guard_reason(kind, command, details)
        if _web_reason:
            details["web_tainted"] = True
            details["force_prompt"] = True
            details.setdefault("soft_reasons", [_web_reason])
    # The frontend may only offer "remember for this session" on kinds the
    # bridge actually honors in decide_approval (safe writes + MCP tools + the
    # desktop input group, which ticks the single per-session switch).
    details.setdefault("allow_always",
                       bool(kind) and (kind in _SAFE_WRITE_KINDS or kind.startswith("mcp_")
                                       or kind in _DESKTOP_INPUT_KINDS))
    if kind in _DESKTOP_INPUT_KINDS:
        details.setdefault("allow_always_label", "Allow desktop input for this session")
    try:
        if not details.get("critical") and not details.get("force_prompt"):
            # /goal: auto-pass everything except destructive actions, overriding the
            # soft/medium/hard mode. Critical, force_prompt and web-tainted requests were
            # already excluded above; destructive ones fall through to the normal gate.
            if _chat_in_goal_mode(chat_id):
                if _goal_action_is_gated(kind, command, details.get("cwd"), details.get("run_cwd")):
                    details.setdefault("goal_gated", True)
                    reasons = _gate_reasons(kind, command, details)
                    if reasons and all(_concern_authorized(r, chat_id, command) for r in reasons):
                        details["soft_reasons"] = reasons[:4]      # for the decision log
                        return {"status": "words-approved", "decision": "approve", "auto": True,
                                "goal": True, "details": details}
                    if reasons:
                        details.setdefault("soft_reasons", reasons[:4])
                else:
                    return {"status": "goal-auto-approved", "decision": "approve",
                            "auto": True, "goal": True, "details": details}
            # protected ops and explicitly remote mutations NEVER auto-approve
            mode = _approval_mode()
            if kind and kind in _approval_session_allow:
                return {"status": "session-approved", "decision": "approve", "auto": True, "details": details}
            # An app the user listed in the desktop allowlist opens without a
            # question; Hard mode still asks for everything.
            if kind == "desktop.launch" and details.get("allowlisted") and mode != "hard":
                return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
            # (B) The per-session desktop switch: one sentinel unlocks every
            # desktop input kind, in any mode (an explicit user opt-in, like other
            # session-remembers). force_prompt / web-taint still override above.
            if kind in _DESKTOP_INPUT_KINDS and _desktop_session_unlocked():
                return {"status": "session-approved", "decision": "approve", "auto": True, "details": details}
            if mode != "hard" and kind in _SAFE_WRITE_KINDS:
                return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
            if mode == "soft":
                if kind == "registry":
                    pass                                    # registry stays gated
                elif kind in ("delete", "launch") or kind in _SOFT_COMMAND_KINDS:
                    # delete_file / open_program / shell-ish: ask only for what
                    # can't be undone and wasn't asked for.
                    concerns = _gate_reasons(kind, command, details)
                    if not concerns:
                        return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
                    if all(_concern_authorized(c, chat_id, command) for c in concerns):
                        details["soft_reasons"] = concerns[:4]      # for the decision log
                        return {"status": "words-approved", "decision": "approve", "auto": True,
                                "details": details}
                    # Shown on the approval card: why Soft stopped for this one.
                    details["soft_reasons"] = concerns[:4]
                    cats = {_concern_category(c)[0] for c in concerns}
                    if cats and cats <= _CHAT_ALLOWABLE:
                        details["allow_always"] = True
                        details["allow_always_label"] = "Allow this kind of action for the rest of this chat"
                elif _is_desktop_approval_kind(kind):
                    # (A) Intent: desktop INPUT runs free in soft mode when the
                    # user's own last message asked for on-screen/browser action,
                    # or when it is aimed at a program Accuretta started itself
                    # (testing the app it just built). Launch/close still prompt.
                    if kind in _DESKTOP_INPUT_KINDS and (_user_asked_desktop_interaction(STATE.last_user_text)
                                                         or _desktop_target_owned()):
                        return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
                    # otherwise desktop stays gated
                elif (kind or "").startswith("mcp_"):
                    if not _MCP_SOFT_BLOCK.search(kind):    # playwright runs free; delete-ish MCP still prompts
                        return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
                elif kind in _SOFT_AUTO_KINDS:
                    return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
    except Exception:
        pass
    goal_wait = _chat_in_goal_mode(chat_id) and timeout_s > _GOAL_ASK_WAIT_S
    if goal_wait:
        timeout_s = _GOAL_ASK_WAIT_S
    aid = uuid.uuid4().hex[:12]
    ev = threading.Event()
    entry = {
        "id": aid,
        "chat_id": _get_current_chat(),
        "title": title,
        "command": command,
        "details": details or {},
        "created": time.time(),
        "status": "pending",
        "decision": None,
    }
    with _approvals_lock:
        _approvals[aid] = entry
        _approval_events[aid] = ev
    save_json(PENDING_DIR / f"{aid}.json", entry)
    _announce_approval(entry)
    deadline = time.monotonic() + timeout_s
    got = False
    while time.monotonic() < deadline:
        if _steering.pending(_current_chat_id.get()):
            decide_approval(aid, "deny")
        if ev.wait(timeout=min(.2, max(0, deadline - time.monotonic()))):
            got = True
            break
    with _approvals_lock:
        final = _approvals.pop(aid, entry)
        _approval_events.pop(aid, None)
    (PENDING_DIR / f"{aid}.json").unlink(missing_ok=True)
    if not got:
        final["decision"] = "deny"
        if goal_wait:
            # Nobody answered: keep the long run moving and tell the model what to do.
            final["status"] = ("skipped: needs the user's OK. Carry on with the rest of the task, "
                               "and list this action in your final report so the user can do or approve it")
            broadcast_event({"type": "approval:decided", "id": aid, "decision": "deny"})
        else:
            final["status"] = "timeout"
    return final


def _gate_reasons(kind, command: str, details: dict) -> list[str]:
    """Soft mode's reasons to ask for delete_file, open_program and shell-ish
    requests (empty: it can run)."""
    if kind == "delete":
        path = str(details.get("path") or "")
        res = ("host", os.path.normcase(os.path.normpath(path))) if path else None
        if not _soft_target_ok(res, delete=True):
            return [_soft_delete_reason(res, path)]
        big = _big_delete_concern(res[1], path)
        return [big] if big else []
    if kind == "launch":
        path = str(details.get("path") or "")
        res = ("host", os.path.normcase(os.path.normpath(path))) if path else None
        concern = _soft_open_concern("start-process", [], f'"{path}"' if path else None, set(),
                                     lambda tok: res, lambda tok: path, command or "")
        return [concern] if concern else []
    if kind in _SOFT_COMMAND_KINDS:
        return _shell_concerns_for(kind, command, details)
    return []


def decide_approval(aid: str, decision: str, always: bool = False) -> bool:
    with _approvals_lock:
        entry = _approvals.get(aid)
        ev = _approval_events.get(aid)
        if not entry or not ev:
            return False
        entry["decision"] = "approve" if decision == "approve" else "deny"
        entry["status"] = "decided"
        if entry["decision"] == "approve":
            # The same reason won't ask again this turn; "allow for the rest of
            # this chat" covers its kind (stopping its programs, launching, pushing).
            _remember_turn_approval(str(entry.get("chat_id") or ""),
                                    list((entry.get("details") or {}).get("soft_reasons") or []), always)
        if always and entry["decision"] == "approve":
            kind = (entry.get("details") or {}).get("kind")
            # Session-allow only safe kinds, whatever the client asked to remember.
            if kind and (kind in _SAFE_WRITE_KINDS or kind.startswith("mcp_")):
                _approval_session_allow.add(kind)
            # A desktop input kind flips the ONE sentinel, so the whole input
            # group (click/type/keys/scroll/focus) is unlocked for the session.
            elif kind in _DESKTOP_INPUT_KINDS:
                _approval_session_allow.add(_DESKTOP_SESSION_SENTINEL)
        ev.set()
    broadcast_event({"type": "approval:decided", "id": aid, "decision": entry["decision"]})
    return True


def _critical_path_gate(path: str, kind: str, title: str) -> dict | None:
    """Hold-to-approve gate for writes under Windows/System32. Returns an error
    dict when denied, None when the path isn't protected or the user
    held-and-approved. Never auto-approves (details['critical'])."""
    if not is_blocked_path(path):
        return None
    approval = request_approval(
        title=title,
        command=f"modify protected system path: {path}",
        details={"kind": kind, "critical": True, "path": path},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied protected-path change ({approval.get('status')})"}
    return None
