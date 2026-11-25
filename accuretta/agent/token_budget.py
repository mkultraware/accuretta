# SPDX-License-Identifier: AGPL-3.0-only
"""Rough token counts without a tokenizer, trimming messages to a budget, and
how often the task-state note is refreshed.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json


from accuretta.core.tokens import (  # noqa: F401  re-export: moved 2026-11-23
    CHARS_PER_TOKEN,
    _approx_tokens,
    _count_msg_tokens,
)


def truncate_messages(msgs: list[dict], max_tokens: int, reserve: int = 256,
                      token_scale: float = 1.0) -> list[dict]:
    """Conveyor belt: drop the oldest middle messages while anchoring the system
    prompt and the first user message (the original ask). The most recent
    messages are always kept.

    Layout: [system] + [first_user] + ...dropped... + [recent...]
    `reserve` leaves room for the next reply + reasoning. `token_scale` converts
    the per-message char estimate into llama-server tokens (see
    _conversation_token_scale); 1.0 keeps the plain heuristic."""
    if not msgs:
        return msgs
    budget = max_tokens - reserve
    # Scaled count: the trimmer and llama-server's slot must agree on size.
    _cm = lambda m: _count_msg_tokens(m) * token_scale

    # Anchor 1: system prompt at index 0 (if present).
    system = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
    rest = msgs[len(system):]

    # Anchor 2: only the first user message itself, and only when it really is
    # the first message in `rest`. Absorbing the following assistant/tool messages
    # turned a one-shot autonomous run (no second user message) into one
    # indivisible anchor that the 60% fallback below then discarded wholesale.
    # After a rolling-summary fold the replay starts mid tool run, and anchoring
    # on a later user message would drop everything between the boundary and it;
    # the original ask survives a fold through compaction_memory instead.
    # Tool-call/result pairing is protected separately at the tail boundary.
    anchor: list[dict] = []
    anchor_end = 0
    if rest and rest[0].get("role") == "user":
        anchor = [rest[0]]
        anchor_end = 1

    middle_and_tail = rest[anchor_end:]

    # If the anchors alone bust the budget (a long first message in a tiny ctx),
    # drop the anchor and keep tail-only behavior.
    anchor_cost = sum(_cm(m) for m in system) + sum(_cm(m) for m in anchor)
    if anchor_cost > budget * 0.6:
        anchor = []
        anchor_cost = sum(_cm(m) for m in system)

    # Walk recent messages from the end backwards, keeping until budget hits.
    keep: list[dict] = []
    total = anchor_cost
    for m in reversed(middle_and_tail):
        t = _cm(m)
        if total + t > budget and keep:
            break
        keep.insert(0, m)
        total += t

    if not keep and not anchor:
        # Emergency: keep only the very last message.
        keep = middle_and_tail[-1:] or rest[-1:]

    # Pair-protect the boundary: a `tool` message at the front of `keep` is
    # an orphan if its parent assistant (with the matching tool_call_id) was
    # dropped. llama-server rejects orphan tool messages, so drop them here.
    while keep and keep[0].get("role") == "tool":
        keep.pop(0)
    if len(system) + len(anchor) + len(keep) < len(msgs):
        try:
            print(f"TRIMDBG n={len(msgs)} keep={len(system) + len(anchor) + len(keep)} "
                  f"budget={budget} anchor_cost={anchor_cost} "
                  f"costs=[{','.join(str(int(_cm(m))) for m in msgs[:12])}]", flush=True)
        except Exception:
            print(f"TRIMDBG n={len(msgs)} keep={len(system) + len(anchor) + len(keep)} "
                  f"budget={budget} anchor_cost={anchor_cost} (cost dump failed)", flush=True)

    # If the anchor is the same object as the first kept message (very short
    # convo), don't duplicate it.
    if anchor and keep and anchor[0] is keep[0]:
        return system + keep
    return system + anchor + keep


# Sections of the task-state note that change on almost every round. A change
# there alone does not trigger a new note; it rides along with the periodic
# refresh. Every _STATE_NOTE_REFRESH_EVERY rounds the note is added again even
# if unchanged, so it stays near the end where attention lands. Adding costs a
# little context but never breaks the cache; only rewriting earlier text does.
_STATE_NOTE_SLOW_PREFIXES = ("=== RECENT TOOL LEDGER", "=== STRUCTURED CONTINUITY", "=== GOAL CLOCK")


_STATE_NOTE_REFRESH_EVERY = 4


# An unchanged note's refresh is a short reminder (see _StateNotes.apply);
# the whole note goes out again at least this often.
_STATE_NOTE_FULL_EVERY = 16


# Old notes stay where they were sent. Past this many tokens of them, they are
# all dropped once and the note starts fresh (one cache re-read, bounded size).
_STATE_NOTE_KEEP_TOKENS = 6000


class _StateNotes:
    """Turn-local record of where each task-state note was attached.

    The note used to be rebuilt every round and attached to the newest tool
    result only, so the previous result lost its copy every round. That rewrote
    earlier prompt text each round, and hybrid models (Qwen3.5/3.6/3.8, whose
    recurrent state cannot rewind) then re-read from their last context
    checkpoint, often 8K+ tokens back. Now a note stays on the message it was
    attached to, and a new one is added only when its text changes, so within a
    task the prompt only grows. Placement follows _attach_session_state: inside
    a tool result, or as its own message when the last one is not a tool result.
    Records hold the anchor message itself, so an identity match cannot hit a
    different message that reused a freed object's id."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.fast: str | None = None
        self.sent_round = 0
        self.full_round = 0

    def kept_tokens(self) -> int:
        """Prompt the kept notes add on top of the trimmed messages."""
        return sum(_approx_tokens(p["body"]) + 8 for p in self.placed)

    def apply(self, wire: list[dict], header: str, sections: list[str], role: str,
              round_no: int, allow_new: bool = True,
              brief: list[str] | None = None) -> list[dict]:
        """`brief`, when given, is what an unchanged note's periodic refresh
        carries while a full note is still in the prompt: a short reminder
        instead of the whole note. A full note is sent when the text changed,
        when none survives (trimmed, folded, or the pile-up reset) and every
        _STATE_NOTE_FULL_EVERY rounds, so the slow sections stay current.

        Full refreshes made the notes outgrow _STATE_NOTE_KEEP_TOKENS every
        ~14 rounds on a long task (two ~3,600-token notes), and each reset
        made a hybrid model re-read most of the prompt: ~19 of 118 minutes of
        one run went to those re-reads."""
        ids = {id(m) for m in wire}
        # Notes whose message was trimmed or folded away are gone with it.
        self.placed = [p for p in self.placed if id(p["anchor"]) in ids
                       and any(m is p["anchor"] for m in wire)]
        fast = "\n\n".join(s for s in sections if not s.startswith(_STATE_NOTE_SLOW_PREFIXES))
        if self.kept_tokens() > _STATE_NOTE_KEEP_TOKENS:
            self.placed = []
        if allow_new and sections and wire:
            changed = not self.placed or fast != self.fast
            stale = changed or round_no - self.sent_round >= _STATE_NOTE_REFRESH_EVERY
            if stale:
                full = (changed or not brief
                        or not any(p.get("full", True) for p in self.placed)
                        or round_no - self.full_round >= _STATE_NOTE_FULL_EVERY)
                anchor = wire[-1]
                kind = ("inside" if anchor.get("role") == "tool"
                        and isinstance(anchor.get("content"), str) else "after")
                body = header + "\n" + "\n\n".join(sections if full else brief)
                # Same anchor as the newest note (no new message since): replace it.
                self.placed = [p for p in self.placed if p["anchor"] is not anchor]
                self.placed.append({"anchor": anchor, "body": body, "kind": kind,
                                    "role": role, "full": full})
                self.fast, self.sent_round = fast, round_no
                if full:
                    self.full_round = round_no
        if not self.placed:
            return wire
        out: list[dict] = []
        for m in wire:
            rec = next((p for p in self.placed if p["anchor"] is m), None)
            if rec is None:
                out.append(m)
            elif rec["kind"] == "inside":
                out.append(dict(m, content=f"{m['content']}\n\n{rec['body']}"))
            else:
                out.append(m)
                out.append({"role": rec["role"], "content": rec["body"]})
        return out
