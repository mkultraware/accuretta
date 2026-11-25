# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team rate governor: per-mission request limits, pacing with jitter,
the request ledger, and the preflight wait.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import random
import threading
import time
from accuretta.core.cancels import _chat_cancels
from accuretta.core.cancels import _chat_cancels_lock
from accuretta.core.chats import _current_chat_id
from accuretta.core.paths import DATA
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.request_headers import _bounded_int


# ---- engagement request governor + ledger -----------------------------------
# A program's rate limit or a client's request budget is a HARD constraint, not
# a hint: exceeding it can end an engagement. Every outbound HTTP request the
# suite makes through its shared fetch paths is admitted here, counted per
# engagement, and appended to an on-disk ledger that doubles as proof of
# restraint ("50 requests this hour"). In-memory counters reset on restart; the
# ledger is the durable record. Raw sockets, the browser, the login spray and
# the generic fetch tools are admitted the same way. Sandbox scanners
# (nmap/sqlmap) send from inside the guest, so they cannot be counted one by
# one; their pacing flags are derived from the engagement limit instead.
_RT_GOV_LOCK = threading.Lock()


_RT_GOV_LEDGER_LOCK = threading.Lock()


_RT_GOV_STATE: dict[str, dict] = {}


_RT_GOV_LEDGER_MAX_BYTES = 8 * 1024 * 1024


def _rt_gov_state(key: str) -> dict:
    state = _RT_GOV_STATE.get(key)
    if state is None:
        state = {"window_start": time.time(), "window_count": 0, "total": 0, "blocked": 0}
        _RT_GOV_STATE[key] = state
        while len(_RT_GOV_STATE) > 128:
            _RT_GOV_STATE.pop(next(iter(_RT_GOV_STATE)), None)
    if time.time() - state["window_start"] >= 60:
        state["window_start"] = time.time()
        state["window_count"] = 0
    return state


def _rt_gov_limits(mission: dict | None) -> tuple[int, int]:
    m = mission or {}
    return (_bounded_int(m.get("rate_limit_rpm"), 0, 6000),
            _bounded_int(m.get("request_cap"), 0, 1000000))


def _rt_gov_preflight(estimated: int, mission: dict | None = None) -> str | None:
    """Refuse BEFORE any traffic when a fan-out would exceed the engagement
    budget. `estimated` is how many requests the call is about to send."""
    mission = mission if mission is not None else _rt_authorized_mission(_rt_context_chat())
    key = str((mission or {}).get("authorization_id") or "")
    if not key:
        return None
    rpm, cap = _rt_gov_limits(mission)
    interval = _rt_gov_interval(mission)
    if not cap and not interval:
        return None
    estimate = max(1, int(estimated or 1))
    with _RT_GOV_LOCK:
        state = _rt_gov_state(key)
        total_used = state["total"]
    if cap and total_used + estimate > cap:
        return (f"refused: this engagement's total request cap is {cap} and "
                f"{total_used} requests have already been sent; this call would send "
                f"about {estimate}. Raise the engagement cap or narrow the call.")
    # Requests are paced, so a batch larger than a minute's allowance is fine;
    # it just takes longer. Refuse only one that would tie the run up.
    minutes = estimate * interval * _RT_GOV_JITTER[1] / 60.0
    if minutes > _RT_GOV_PREFLIGHT_MAX_MIN:
        return (f"refused: at this engagement's pace ({rpm or round(60 / interval)}/minute) "
                f"this call's {estimate} requests would take about {int(minutes)} minutes. "
                f"Narrow it to the most likely candidates.")
    return None


def _rt_gov_ledger_append(key: str, host: str, method: str, url: str,
                          kind: str = "request") -> None:
    try:
        folder = DATA / "redteam"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "request_ledger.jsonl"
        row = {
            "t": round(time.time(), 2), "mission": key, "host": str(host)[:120],
            "method": str(method or "GET")[:12], "url": str(url)[:500],
        }
        if kind != "request":
            row["kind"] = kind
        line = json.dumps(row, ensure_ascii=False)
        with _RT_GOV_LEDGER_LOCK:
            try:
                if path.exists() and path.stat().st_size > _RT_GOV_LEDGER_MAX_BYTES:
                    rotated = folder / "request_ledger.1.jsonl"
                    if rotated.exists():
                        rotated.unlink()
                    path.rename(rotated)
            except OSError:
                pass
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass


# Pacing. A per-minute limit alone lets a tool or a page load send the whole
# minute's allowance in one second, which is exactly what bot protection flags
# ("that was too fast") and what a program means by hammering. So requests are
# spaced like a person working by hand: each one takes the next slot, at least
# 60/rpm seconds after the previous one plus a random extra, and the sending
# thread waits for its slot. A bug bounty run, or any run with automated tools
# off, is paced at about one request a second even without a set limit.
_RT_GOV_JITTER = (1.0, 1.5)        # never faster than the limit, sometimes slower


_RT_GOV_SOFT_INTERVAL_S = 1.0


_RT_GOV_MAX_WAIT_S = 120.0         # beyond this a backlog is refused, not queued


_RT_GOV_PREFLIGHT_MAX_MIN = 10.0   # a single batch may take this long at the pace


def _rt_gov_interval(mission: dict | None) -> float:
    """Seconds between paced requests for this engagement (0 = unpaced)."""
    m = mission or {}
    rpm, _cap = _rt_gov_limits(m)
    if rpm:
        return 60.0 / rpm
    if m.get("engagement") == "bug_bounty" or m.get("automated_allowed") is False:
        return _RT_GOV_SOFT_INTERVAL_S
    return 0.0


def _rt_gov_sleep_until(when: float) -> bool:
    """Sleep until `when`; False if the user pressed Stop meanwhile."""
    chat_id = _current_chat_id.get()
    with _chat_cancels_lock:
        entry = _chat_cancels.get(chat_id) if chat_id else None
    cancel = entry.get("cancel") if entry else None
    while True:
        left = when - time.time()
        if left <= 0:
            return True
        if cancel is not None:
            if cancel.wait(min(left, 0.5)):
                return False
        else:
            time.sleep(min(left, 0.5))


def _rt_gov_wait_turn(mission: dict | None) -> None:
    """Wait until the next paced slot is free without taking it (the browser
    waits here before a navigation; the page's own request then takes it)."""
    key = str((mission or {}).get("authorization_id") or "")
    if not key or not _rt_gov_interval(mission):
        return
    with _RT_GOV_LOCK:
        when = float(_rt_gov_state(key).get("next_at") or 0.0)
    if 0 < when - time.time() <= _RT_GOV_MAX_WAIT_S:
        _rt_gov_sleep_until(when)


def _rt_gov_admit(host: str, method: str, url: str = "", mission: dict | None = None,
                  kind: str = "request", wait: bool = True) -> str | None:
    """Count one outbound request against the engagement budget, ledger it,
    and return a refusal when the request is over budget.

    wait=True paces the request: the caller sleeps until its slot. The browser's
    request hook passes wait=False because it cannot stall the page; it takes
    the slot and is refused only past the per-minute limit. kind="asset" is a
    page file the browser loads in scope: ledgered, not counted."""
    mission = mission if mission is not None else _rt_authorized_mission(_rt_context_chat())
    key = str((mission or {}).get("authorization_id") or "")
    if not key:
        return None
    if kind == "asset":
        _rt_gov_ledger_append(key, host, method, url, kind="asset")
        return None
    rpm, cap = _rt_gov_limits(mission)
    interval = _rt_gov_interval(mission)
    if not rpm and not cap and not interval:
        # No explicit budget: still ledger every engagement request as the
        # audit trail a program or client may ask for.
        _rt_gov_ledger_append(key, host, method, url)
        return None
    slot = 0.0
    with _RT_GOV_LOCK:
        state = _rt_gov_state(key)
        if cap and state["total"] >= cap:
            state["blocked"] += 1
            return (f"refused: this engagement's request cap ({cap}) is reached; "
                    f"no further requests will be sent for it.")
        now = time.time()
        if interval and wait:
            slot = max(now, float(state.get("next_at") or 0.0))
            if slot - now > _RT_GOV_MAX_WAIT_S:
                state["blocked"] += 1
                return (f"refused: {int(slot - now)}s of requests are already queued for this "
                        f"engagement's pace. Send fewer requests at once.")
            state["next_at"] = slot + interval * random.uniform(*_RT_GOV_JITTER)
        else:
            if rpm and state["window_count"] >= rpm:
                state["blocked"] += 1
                left = max(1, 60 - int(now - state["window_start"]))
                return (f"refused: the engagement rate limit ({rpm}/minute) is reached for "
                        f"this window. Retry in about {left}s or raise the limit.")
            if interval:
                state["next_at"] = max(now, float(state.get("next_at") or 0.0)) + interval
        state["window_count"] += 1
        state["total"] += 1
    if slot and not _rt_gov_sleep_until(slot):
        return "refused: the run was stopped while this request waited for its turn."
    _rt_gov_ledger_append(key, host, method, url)
    return None


def _rt_gov_ledger_count(key: str) -> int:
    """Ledger lines recorded for this engagement, across rotations."""
    total = 0
    try:
        folder = DATA / "redteam"
        for name in ("request_ledger.jsonl", "request_ledger.1.jsonl"):
            path = folder / name
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                        if row.get("mission") == key and row.get("kind") != "asset":
                            total += 1
                    except Exception:
                        continue
    except Exception:
        return total
    return total
