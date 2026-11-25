# SPDX-License-Identifier: AGPL-3.0-only
"""When a model only thinks: retries, the draft it left in its thinking, the recovery
note, and trimming old reasoning once a chat runs long.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.agent.visible_text import _prior_thinking_mode
from accuretta.security.blocked_writes import _FILE_IN_PARTS


# Rounds in a row that may end with only thinking before the turn stops.
_THINKING_ONLY_RETRIES = 2


_NUMBERED_LINE_RE = re.compile(r"^[ \t]*\d{1,4}[:.]?[ \t]+\S", re.MULTILINE)


def _thinking_body(text: str) -> str:
    return re.sub(r"</?think>", "", text or "", flags=re.IGNORECASE).strip()


def _thinking_draft(text: str) -> bool:
    """True when the thinking holds a code draft: a fence or a run of numbered lines."""
    return "```" in text or len(_NUMBERED_LINE_RE.findall(text)) >= 30


def _thinking_recovery_note(cause: str, reasoning: str, use_tools: bool) -> str:
    """The note that asks again after a round ended with only thinking.

    A draft is not fed back: the model would continue it from the middle, or
    write it out in the thinking again."""
    body = _thinking_body(reasoning)
    if _thinking_draft(body):
        step = ("If the task is a file, call write_file now with the whole file in content; "
                "for a long file, " + _FILE_IN_PARTS + ". Otherwise make the next tool call "
                "now, or write the answer if the task is done." if use_tools else
                "Write the complete answer now, directly in your reply.")
        return (f"[automatic note] {cause} Your thinking held a draft, and thinking is not kept, so "
                "the draft is gone. Do not draft again and do not count lines: a size such as "
                f"\"300 lines\" is approximate. {step}")
    # In a tool turn the work is usually not done: ordering a final answer here
    # made the model stop mid-task.
    step = ("Stop thinking now. If the task needs another action, make that tool call now; if the "
            "task is done, write the final answer." if use_tools else
            "Stop thinking now and write the COMPLETE final answer directly — full code/prose.")
    return (f"[automatic note] {cause} {step} No <think> block, no more planning. "
            "Continue from your planning so far:\n" + body[-2000:])


def _with_thinking_note(conversation: list[dict], start: int, note: str) -> list[dict]:
    """Swap this turn's earlier thinking note for a new one, so notes never stack."""
    kept = [m for m in conversation[start:]
            if not (isinstance(m, dict) and m.get("_thinking_note"))]
    return conversation[:start] + kept + [{"role": "user", "content": note, "_thinking_note": True}]


# Reasoning cleanup: when a long task's prompt reaches the compaction trigger,
# first drop the reasoning of all but the last few rounds and keep everything
# else (tool results, answers, tool calls) word for word. Reasoning is the least
# valuable context, and on a reasoning-heavy task it was ~70% of what the model
# wrote, so this frees real room without summarizing anything away. It costs
# one re-read of the prompt (the earlier text changes once), about what a
# compaction's rebuild costs, minus the summarizer call and its detail loss.
# Compaction still runs when a cleanup would not free enough. 25% so it only
# fires when it can actually head off a compaction (reasoning-heavy, light
# tool output). At 10%, a replay of a real 66-round task spent ~80 s
# re-reading to delay its one compaction by just 7 minutes.
_REASONING_CLEANUP_KEEP_ROUNDS = 3


_REASONING_CLEANUP_MIN_FRAC = 0.25   # of the context window


def _reasoning_cleanup_anchor(msgs: list[dict], settings: dict | None = None) -> dict | None:
    """The assistant message a reasoning cleanup would keep reasoning from (the
    _REASONING_CLEANUP_KEEP_ROUNDS-th newest), or None when there is nothing
    older to drop or reasoning is not stripped for this model."""
    if _prior_thinking_mode(settings) != "strip":
        return None
    asst = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant"]
    if len(asst) <= _REASONING_CLEANUP_KEEP_ROUNDS:
        return None
    return asst[-_REASONING_CLEANUP_KEEP_ROUNDS]
