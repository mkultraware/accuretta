# SPDX-License-Identifier: AGPL-3.0-only
"""What the agent already called this turn: call signatures, repeat limits (soft and
hard), URL fetch counts across tools, per-family budgets and progress.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from accuretta.agent.action_audit import _DESKTOP_TOOL_NAMES
from accuretta.agent.action_audit import _WRITE_CAPABLE_TOOLS
from accuretta.agent.execution_target import _HOST_TARGET_TOOL_NAMES
from accuretta.agent.execution_target import _REMOTE_TOOL_NAMES
from accuretta.agent.tool_sets import _RED_TEAM_TOOL_NAMES
from accuretta.core.chats import _get_current_chat


# Per-turn tool-call history, reset at the top of run_chat_turn and checked
# in invoke_tool to break loops where the model repeats the same tool with the
# same args (typically misreading a truncated response as a transient
# failure). Keyed by chat_id behind a lock so parallel tool calls share one
# history instead of each worker thread keeping its own.
_tool_history_lock = threading.Lock()


_tool_call_history: dict[str, dict[str, dict]] = {}    # chat_id -> sig -> rec


_tool_family_history: dict[str, dict[str, int]] = {}   # chat_id -> family -> consecutive calls


# Cross-tool URL tracking: web_fetch and http_request have independent
# loop-breaker streaks, and models ping-pong between them on the same URL, so
# fetches are also counted per URL across all fetch-like tools.
_tool_url_history: dict[str, dict[str, dict]] = {}     # chat_id -> url_key -> {count, fp}


# Turn-level forward-progress counter. A loop is the absence of progress, not
# mere repetition: this ticks whenever a call tries a new signature or returns
# a changed result, and each signature records the tick it last ran at. The
# streak breaker fires only when a signature keeps returning the same result
# and nothing else moved in between.
_tool_progress: dict[str, int] = {}                    # chat_id -> monotonic progress ticks


_FETCH_LIKE_TOOLS = frozenset({"web_fetch", "http_request"})


TOOL_REPEAT_LIMIT = 3       # 4th SAME-RESULT call in a row gets refused.


TOOL_REPEAT_HARD_LIMIT = 20  # absolute backstop for wobbly-output tools (below max_tool_rounds).


TOOL_SOFT_REPEAT_LIMIT = 2   # cached-result warnings for an idempotent read before it's refused.


URL_CROSS_TOOL_LIMIT = 5     # same URL hit 5× via ANY fetch tool combo → refuse.


_DIRECTORY_WALK_TOOLS = frozenset({"list_directory", "remote_list_directory"})


def _reserve_tool_family_call(canon: str) -> int:
    """Reserve a consecutive family slot and reset the streak on a new strategy."""
    chat_id = _get_current_chat()
    with _tool_history_lock:
        families = _tool_family_history.setdefault(chat_id, {})
        if canon not in _DIRECTORY_WALK_TOOLS:
            families["directory_walk"] = 0
            return 0
        count = int(families.get("directory_walk", 0) or 0) + 1
        families["directory_walk"] = count
        return count


# Idempotent reads: host-target tools that neither mutate state nor leave the
# machine. Re-running them on unchanged inputs is expected, so when the streak
# breaker trips for one of these the turn gets the cached result back as a
# warning instead of a refusal. Derived from the existing read/write/egress
# sets, so a new tool is classified automatically.
_STATE_CHANGING_OR_EGRESS_TOOLS = (
    _WRITE_CAPABLE_TOOLS | _FETCH_LIKE_TOOLS | _REMOTE_TOOL_NAMES
    | _RED_TEAM_TOOL_NAMES | _DESKTOP_TOOL_NAMES
    | {"run_powershell", "run_tests", "open_program", "delete_file",
       "extract_archive", "switch_execution_target"})


_IDEMPOTENT_READ_TOOLS = frozenset(
    _HOST_TARGET_TOOL_NAMES - _STATE_CHANGING_OR_EGRESS_TOOLS)


def _tool_call_sig(canon: str, args: dict) -> str:
    return canon + ":" + json.dumps(args or {}, sort_keys=True, default=str)


def _tool_repeat_state(sig: str):
    """(streak, total, progress_at, last_result) for this signature. progress_at
    is the turn progress tick when it last ran; comparing it to the current tick
    tells the breaker whether anything else moved since. last_result is a capped
    cache so a refusal can still hand the turn the data it already has."""
    with _tool_history_lock:
        rec = _tool_call_history.get(_get_current_chat(), {}).get(sig)
        if not rec:
            return (0, 0, 0, None)
        return (rec.get("streak", 0), rec.get("total", 0),
                rec.get("progress_at", 0), rec.get("last_result"))


def _current_turn_progress() -> int:
    with _tool_history_lock:
        return _tool_progress.get(_get_current_chat(), 0)


def _note_soft_repeat(sig: str) -> int:
    """Count a repeat answered from the cache (the call didn't run); returns
    how many this signature has had since it last really ran."""
    with _tool_history_lock:
        rec = _tool_call_history.get(_get_current_chat(), {}).get(sig)
        if rec is None:
            return 1
        rec["soft"] = rec.get("soft", 0) + 1
        return rec["soft"]


def _record_tool_result(sig: str, result) -> None:
    """Update loop-breaker bookkeeping after a tool runs.

    A result that differs from the last one for this signature means state moved
    (a file was edited, tests now pass), so the stuck streak resets to 1; only
    identical results grow it toward the limit. That lets a model re-read a file
    it just edited or re-run a check after a fix. `total` is an absolute
    backstop for tools whose output wobbles every call (timestamps, ids).

    Also advances the turn progress counter (a new signature or changed result
    forgives other signatures' streaks) and caches a capped copy of the result
    so a loop-breaker refusal can still return data."""
    try:
        fp = hash(json.dumps(result, sort_keys=True, default=str)[:200000])
    except Exception:
        fp = None
    try:
        cached = json.dumps(result, ensure_ascii=False, default=str)[:4000]
    except Exception:
        cached = None
    cid = _get_current_chat()
    with _tool_history_lock:
        history = _tool_call_history.setdefault(cid, {})
        existing = history.get(sig)
        rec = existing or {"streak": 0, "total": 0, "fp": None,
                           "progress_at": 0, "last_result": None}
        rec["total"] += 1
        rec["soft"] = 0          # it really ran: cached warnings start over
        result_changed = fp is not None and rec.get("fp") != fp
        if existing is None or result_changed:
            rec["streak"] = 1
            rec["fp"] = fp
            # A new action, or state that actually moved: forward progress.
            _tool_progress[cid] = _tool_progress.get(cid, 0) + 1
        else:
            rec["streak"] += 1
        rec["progress_at"] = _tool_progress.get(cid, 0)
        rec["last_result"] = cached
        history[sig] = rec


def _fetch_url_key(args: dict) -> str:
    """Normalize a URL to scheme://host:port/path for cross-tool dedup.
    Query params are stripped so GET /login.php and POST /login.php with a body
    share the same key — the model is hitting the same endpoint either way."""
    raw = (args.get("url") or args.get("target") or "").strip()
    if not raw:
        return ""
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or "").lower()
        port = f":{u.port}" if u.port else ""
        scheme = (u.scheme or "http").lower()
        return f"{scheme}://{host}{port}{u.path}"
    except Exception:
        return raw[:200]


def _record_url_fetch(canon: str, args: dict, result=None) -> None:
    """Track cross-tool URL hits, counting only consecutive identical responses.
    Replaying an endpoint with a flipped cookie or new payload is core
    exploitation work; the counter only climbs when the same URL keeps
    returning the same content via web_fetch/http_request."""
    if canon not in _FETCH_LIKE_TOOLS:
        return
    key = _fetch_url_key(args)
    if not key:
        return
    try:
        fp = hash(json.dumps(result, sort_keys=True, default=str)[:200000]) if result is not None else None
    except Exception:
        fp = None
    cid = _get_current_chat()
    with _tool_history_lock:
        urls = _tool_url_history.setdefault(cid, {})
        rec = urls.get(key)
        if isinstance(rec, int):            # migrate any legacy int shape
            rec = {"count": rec, "fp": None}
        rec = rec or {"count": 0, "fp": None}
        if fp is not None and rec.get("fp") == fp:
            rec["count"] += 1               # same response again — no new info
        else:
            rec["count"] = 1                # response changed (progress) — reset
            rec["fp"] = fp
        urls[key] = rec


def _url_fetch_count(args: dict) -> int:
    """How many times this URL has returned the SAME content this turn."""
    key = _fetch_url_key(args)
    if not key:
        return 0
    cid = _get_current_chat()
    with _tool_history_lock:
        rec = _tool_url_history.get(cid, {}).get(key)
        if isinstance(rec, int):
            return rec
        return int((rec or {}).get("count", 0))
