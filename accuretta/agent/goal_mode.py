# SPDX-License-Identifier: AGPL-3.0-only
"""The /goal command: parsing it, the time limit and clock, progress windows, the
summary, and the latch that keeps a goal running across rounds.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import time
from accuretta.core.chats import get_chats
from accuretta.core.events import broadcast_event
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.security.approvals import _goal_mode_chats
from accuretta.security.approvals import _goal_mode_lock


def _set_goal_mode(chat_id: str, on: bool) -> None:
    if not chat_id:
        return
    with _goal_mode_lock:
        if on:
            _goal_mode_chats.add(chat_id)
        else:
            _goal_mode_chats.discard(chat_id)


_GOAL_OFF_RE = re.compile(r"^\s*/goal\s+(off|stop|end|done|cancel)\b\s*$", re.IGNORECASE)


_GOAL_COMMAND_RE = re.compile(r"^\s*/goal\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)


def _parse_goal_command(text: str) -> tuple[str, str | None]:
    """Parse a leading /goal slash command. Returns (remaining_task, action):
      - "/goal off|stop|end|done|cancel"  -> ("", "off")
      - "/goal on" | "/goal start" | "/goal" -> ("", "on")   (latch, no task)
      - "/goal <task>"                    -> (task, "on")    (latch + run task)
      - no /goal                          -> (text, None)    (unchanged)
    """
    t = text or ""
    if _GOAL_OFF_RE.match(t):
        return "", "off"
    m = _GOAL_COMMAND_RE.match(t)
    if not m:
        return t, None
    rest = m.group(1).strip()
    if rest.lower() in ("on", "start"):
        rest = ""
    return rest, "on"


# A /goal turn that reaches max_tool_rounds gets another stretch of the same
# size while the last stretch made progress, with no fixed number of
# stretches: an overnight decompile must not stop at an arbitrary round count
# (1,200 rounds is ~4 h on one PC and ~20 h on another). A run ends when the
# work is done, a stretch makes no progress, a loop guard fires or the user
# presses Stop.
# Tools that only look around. A stretch whose successes are all of these has
# not moved the task forward.
_GOAL_LOOK_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "grep_files", "glob_files", "search_files",
    "find_files", "capability_report", "update_plan", "pin_note", "list_tools",
    "tool_help", "verification_guidance",
})


def _goal_window_progressed(outcomes: list[dict]) -> bool:
    """True when a stretch of tool outcomes shows real progress: most calls
    succeeded, few were loop-breaker refusals, and at least one success did
    something beyond looking around (wrote, ran, fetched, tested)."""
    if not outcomes:
        return False
    refused = sum(1 for o in outcomes if o.get("refused"))
    ok = [o for o in outcomes if o.get("ok") and not o.get("refused")]
    if refused * 4 >= len(outcomes) or len(ok) * 2 < len(outcomes):
        return False
    return any(str(o.get("name") or "") not in _GOAL_LOOK_ONLY_TOOLS for o in ok)


def _goal_open_steps(plan: list | None) -> list[str]:
    """Titles of the plan steps not yet done (update_plan: pending/active/done)."""
    return [str(p.get("title") or "").strip() for p in (plan or [])
            if isinstance(p, dict) and str(p.get("title") or "").strip()
            and str(p.get("status") or "pending").strip().lower() != "done"]


_GOAL_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "sixty": 60,
    "ninety": 90,
}


_GOAL_NUM = r"(\d+(?:[.,]\d+)?|" + "|".join(sorted(_GOAL_NUM_WORDS, key=len, reverse=True)) + r")"


_GOAL_UNIT = r"(hours?|hrs?|h|minutes?|mins?)\b"


_GOAL_LIMIT_RE = re.compile(
    r"(?:you have|you've got|you get|within|in under|no more than|at most|"
    r"time limit(?: of| is)?|time budget(?: of| is)?|deadline(?: of| is)?|budget(?: of| is)?)"
    r"\s+(?:about|around|roughly|approximately|~|only|just)?\s*"
    + _GOAL_NUM + r"\s*" + _GOAL_UNIT
    + r"(?:\s*(?:and)?\s*" + _GOAL_NUM + r"\s*" + _GOAL_UNIT + r")?"
    + r"|" + _GOAL_NUM + r"\s*" + _GOAL_UNIT + r"\s+from now",
    re.IGNORECASE,
)


def _goal_time_limit(text: str) -> int:
    """Seconds of a time limit the /goal task states for itself ("you have
    about 3 hours from now", "within 90 minutes"), or 0 when it states none.
    Only reported to the model: nothing stops when it runs out."""
    m = _GOAL_LIMIT_RE.search(str(text or "")[:20000])
    if not m:
        return 0
    groups = [g for g in m.groups()]
    total = 0.0
    for i in range(0, len(groups), 2):
        num, unit = groups[i], groups[i + 1] if i + 1 < len(groups) else None
        if not num or not unit:
            continue
        num = num.lower().replace(",", ".")
        value = float(_GOAL_NUM_WORDS.get(num, 0) or (num if num.replace(".", "", 1).isdigit() else 0))
        total += value * (3600 if unit.lower().startswith("h") else 60)
    return int(total) if 60 <= total <= 14 * 86400 else 0


def _goal_clock_text(elapsed: float, limit: int) -> str:
    """The GOAL CLOCK line: elapsed time, and the time left worked out for the
    model when the task stated a limit (a 27B read "1 h 56 min" of a 3-hour
    limit as 4 minutes left and gave up with an hour to go)."""
    used = _goal_elapsed_text(elapsed)
    if not limit:
        return f"=== GOAL CLOCK === the /goal task has been running for {used}. The task states no time limit."
    left = limit - elapsed
    if left >= 60:
        return (f"=== GOAL CLOCK === {used} used of the {_goal_elapsed_text(limit)} the task allows: "
                f"about {_goal_elapsed_text(left)} left. Use that time; plan the remaining work to fit it.")
    return (f"=== GOAL CLOCK === {used} used; the task's stated limit of {_goal_elapsed_text(limit)} "
            f"has passed. Submit what you have and finish up.")


def _goal_elapsed_text(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def _goal_summary(task: str) -> str:
    """The goal as the composer shows it: one line, at most 500 characters."""
    return re.sub(r"\s+", " ", task or "").strip()[:500]


def _apply_goal_latch(chat_id: str, on: bool, goal: str | None = None) -> bool:
    """Set the hands-free latch for a chat, mirror it onto the persisted chat
    dict (so it survives a UI reload and is rehydrated next turn), and broadcast
    the goal_mode state to every surface. A task from "/goal <task>" is kept as
    chat["goal_text"] while the latch is on (the composer shows it); turning
    the latch off clears it. Returns the new active state."""
    _set_goal_mode(chat_id, on)
    text = (goal or "") if on else ""
    try:
        chats = get_chats()
        chat = chats.get("chats", {}).get(chat_id)
        if chat is not None:
            changed = bool(chat.get("goal_mode")) != bool(on)
            chat["goal_mode"] = bool(on)
            if on and goal and chat.get("goal_text") != goal:
                chat["goal_text"] = goal
                chat["goal_started"] = int(time.time())
                changed = True
            elif not on and ("goal_text" in chat or "goal_started" in chat):
                chat.pop("goal_text", None)
                chat.pop("goal_started", None)
                chat.pop("goal_limit_s", None)
                changed = True
            if on and not chat.get("goal_started"):
                chat["goal_started"] = int(time.time())
                changed = True
            if on:
                text = str(chat.get("goal_text") or "")
            if changed:
                save_json(CHATS_FILE, chats)
    except Exception:
        pass
    broadcast_event({"type": "goal_mode", "chat_id": chat_id, "active": bool(on), "goal": text})
    return bool(on)
