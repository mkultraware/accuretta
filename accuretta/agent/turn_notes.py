# SPDX-License-Identifier: AGPL-3.0-only
"""Short notes the loop adds to a turn: continue after a steer, answer at the round
limit, stop after compaction, and the Maya loop stop.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import sys
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.auto_tune import _maya_auto_tune
from accuretta.runtime.auto_tune import _tune_budget_gb
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.model_config import _save_model_config


_COMPACTION_STOP_MESSAGE = (
    "Accuretta stopped this response because it could not produce a safe session summary. "
    "Your saved chat and completed actions are intact. Use Compact to try again now, "
    "wait three minutes and retry, or continue in a new session."
)


# Sent when a turn runs out of tool rounds with a call still pending; the next
# request has tool_choice "none" (run_chat_turn's final_answer_round).
_ROUND_LIMIT_ANSWER_NOTE = (
    "[automatic note] This turn has used all {n} of its tool rounds, so your last tool call "
    "did not run and no more can run in this turn. Don't call tools. Write your answer now "
    "from what you found: what you established, what is still open, and the next step you "
    "would take.")


# Added to the model's copy of an update that cut its reply short. The saved
# update keeps only the user's words.
_STEER_CONTINUE_NOTE = (
    "[automatic note] Your reply above was cut off when this message arrived. Take the "
    "message into account and carry on from where your reply stopped, without repeating "
    "what you already wrote, unless the message asks for something else.")


def _with_steer_continue_note(wire: dict) -> dict:
    """Append _STEER_CONTINUE_NOTE to a wire user message (text or parts)."""
    content = wire.get("content")
    if isinstance(content, list):
        wire["content"] = list(content) + [{"type": "text", "text": _STEER_CONTINUE_NOTE}]
    else:
        wire["content"] = f"{content or ''}\n\n{_STEER_CONTINUE_NOTE}"
    return wire


# What maya's server writes as the answer when its loop guard stops a reply
# (serve/server.py LOOP_STOP_NOTE); run_chat_turn re-asks without thinking.
_MAYA_LOOP_STOP_NOTE = "(The answer was stopped: the model kept repeating itself.)"


def _maya_helper_followup(body: dict) -> bool:
    """After a settings save that switched the helper (Speculative decoding,
    or the decoder's Unload) for a model maya runs: size the context for the
    new choice right away, as auto-tune sizes context around llama.cpp's
    drafts. Without it the reload kept the old context and left the helper
    out. A context saved in the same save is the user's and stays."""
    if "num_ctx" in body or not ({"spec_strategy", "spec_draft_mode"} & set(body)):
        return False
    s = get_settings()
    mp = str(s.get("model_path") or "")
    if not mp or not _maya_engine_for(mp):
        return False
    try:
        sug = _maya_auto_tune(mp, _tune_budget_gb())
    except Exception:
        return False
    ctx = int(sug.get("num_ctx") or 0)
    if ctx <= 0 or ctx == int(s.get("num_ctx") or 0):
        return False
    s["num_ctx"] = ctx
    save_json(SETTINGS_FILE, s)
    _save_model_config(mp, {"num_ctx": ctx})
    print(f"[maya] helper switched: context {ctx:,}", file=sys.stderr)
    return True
