# SPDX-License-Identifier: AGPL-3.0-only
"""Native tool calling and its fallback: noticing a server whose native tool calls are
broken, the text-call prompts used instead, warming the tool grammar, and turn-boundary
runaway stops.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import time
from accuretta.agent.tool_schemas import tools_for_llama
from accuretta.core.settings import get_settings
from accuretta.runtime.llama_post import llama_post
from accuretta.runtime.llama_process import _llama


# ---- llama.cpp native tool-call failure memory ---------------------------
# llama.cpp's native parser drops the <parameter> blocks of the Qwen3.5/3.6
# XML dialect for bulky multi-arg tools (write_file worst): the call keeps its
# name but arrives with `arguments:{}`. We then switch to text-tools mode and
# parse the XML ourselves. Remember per model id that this happened, so later
# turns start in text-tools mode instead of rediscovering it. The mark
# expires: text-tools mode is a real downgrade for capable models, so one bad
# parse must not pin the process to it.
_NATIVE_TOOLS_BROKEN_TTL_S = 1800


# Tools whose bulky argument (a file body or an edit list) is what the native
# parser drops. Their loss switches only the current turn to text mode: a
# plain read or search has no reason to start the next turn downgraded.
_BULKY_ARG_TOOLS = frozenset({"write_file", "edit_file", "remote_write_file", "save_skill"})


_native_tools_broken_at: dict[str, dict[str, float]] = {}   # model id -> tool -> time


def _mark_native_tools_broken(tool_names) -> None:
    """Record which tools lost their arguments in llama.cpp's native parser."""
    now = time.time()
    s = get_settings()
    for _mid in (s.get("model"), s.get("model_path"), _llama.loaded_model()):
        if _mid:
            marks = _native_tools_broken_at.setdefault(str(_mid), {})
            for name in tool_names:
                marks[str(name)] = now


def _native_tools_broken() -> bool:
    """True when a tool outside _BULKY_ARG_TOOLS recently lost its arguments
    for the active model, so the next turn starts in text-tools mode."""
    now = time.time()
    s = get_settings()
    for _mid in (s.get("model"), s.get("model_path"), _llama.loaded_model()):
        if not _mid:
            continue
        for name, marked in (_native_tools_broken_at.get(str(_mid)) or {}).items():
            if name not in _BULKY_ARG_TOOLS and now - marked < _NATIVE_TOOLS_BROKEN_TTL_S:
                return True
    return False


# Text-tools mode call format. Gemma's trained dialect is a python call in a
# ```tool_code``` fence. Other models (Qwen, Hermes lineage) are trained on the
# <tool_call><function=...> XML form, which carries raw values without escaping.
# The "TOOL CALLING" heading also ends the rolling-summary section for
# _SUMMARY_SECTION_RE.
_GEMMA_TOOL_CALL_PROMPT = (
    "\n\nTOOL CALLING — IMPORTANT for this model:\n"
    "to call a tool, output EXACTLY one python call inside a ```tool_code``` fence, then STOP:\n"
    "```tool_code\n"
    "read_file(path=\"C:/notes.txt\")\n"
    "```\n"
    "```tool_code\n"
    "edit_file(path=\"C:/notes.txt\", "
    "edits=[{\"old_text\": \"old line\", \"new_text\": \"new line\"}])\n"
    "```\n"
    "- list/object arguments (edits, files, tasks…) are ordinary JSON inside that same call:\n"
    "  one call per fence, never split across fences, and never discuss this format in the\n"
    "  visible answer. There is no separate rule for arrays — the same shape always works.\n"
    "- use the tool names listed above; keyword args with real quotes. when calls are\n"
    "  independent (several reads, a grep plus a status check), emit all their fences in\n"
    "  one reply: they run together. dependent calls and same-file writes stay one per reply.\n"
    "- for ordinary calls, do NOT wrap it in JSON, <tool_call> tags, or prose.\n"
    "- if content already exists in a prior visible code fence, call write_file or "
    "remote_write_file with source='visible_code_block'; NEVER repeat the full block.\n"
    "- for a genuinely new large write_file/edit_file body that is not already visible, use "
    "the <tool_call><function=NAME><parameter=KEY>raw value</parameter></function></tool_call> "
    "form instead of a Python string so quotes and newlines remain lossless.\n"
    "- after you emit the fence, wait for the tool result — never write \"done\"/\"saved\" "
    "or narrate a result before the tool actually returns one.\n"
    "- when a tool result comes back and the task has more steps, IMMEDIATELY emit the "
    "next tool_code call. Do not stop to summarize, do not ask permission, do not say "
    "what you will do — do it. Only write prose once the ENTIRE task is finished."
)


_XML_TOOL_CALL_PROMPT = (
    "\n\nTOOL CALLING (text mode) for this model:\n"
    "to call a tool, write one block in this exact form, then STOP and wait for the result:\n"
    "<tool_call>\n<function=read_file>\n<parameter=path>\nC:/notes.txt\n</parameter>\n"
    "</function>\n</tool_call>\n"
    "- put raw values between the parameter tags. do not escape quotes, backslashes or newlines.\n"
    "- list and object arguments are JSON inside their parameter, for example:\n"
    "<parameter=edits>\n[{\"old_text\": \"old line\", \"new_text\": \"new line\"}]\n</parameter>\n"
    "- use only the tool names listed above. independent calls (several reads, a search plus a "
    "status check) may go in one reply as separate blocks: they run together. dependent calls "
    "and writes to the same file go one per reply.\n"
    "- if the content already exists in a visible code fence, call write_file or "
    "remote_write_file with source='visible_code_block' instead of repeating it.\n"
    "- never write \"done\" or \"saved\" before the tool result comes back.\n"
    "- when a result comes back and the task has more steps, make the next call right away. "
    "write prose only when the whole task is finished."
)


def _text_tools_prompt(gemma: bool) -> str:
    """Call-format rules for text-tools mode, in the model family's own dialect."""
    return _GEMMA_TOOL_CALL_PROMPT if gemma else _XML_TOOL_CALL_PROMPT


def _warmup_tool_grammar() -> None:
    """Compile llama-server's tool-call grammar before the user's first real turn.

    llama.cpp builds it lazily on the first request carrying `tools`, and that
    cold generation tends to emit the call as plain content, so the first turn
    after a load printed its tool call as text. A throwaway tools-bearing
    request at load time pays that cost up front. Best-effort and silent on
    failure (e.g. a template that rejects tools)."""
    try:
        s = get_settings()
        payload: dict = {
            "model": s.get("model") or "local",
            "messages": [{"role": "user", "content": "ready"}],
            "tools": tools_for_llama(),
            "tool_choice": "auto",
            "max_tokens": 8,
            "stream": False,
        }
        et = s.get("enable_thinking")
        if et is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": bool(et)}
        llama_post("/v1/chat/completions", payload, timeout=120)
        print("[llama] tool-grammar warmup complete", flush=True)
    except Exception as e:
        print(f"[llama] tool-grammar warmup skipped: {e}", flush=True)


# Chat turn-boundary markers. Some models (Qwen3.6 Q4 in particular) emit
# these as literal text instead of the stop token, so llama-server's own stop
# never fires and the model re-answers forever; the stream loop cuts there.
_TURN_BOUNDARY_STOPS = ["<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|eot_id|>", "<|end_of_text|>"]


_TURN_BOUNDARY_RE = re.compile(
    r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|eot_id\|>|<\|end_of_text\|>"
)


# llama-server applies stop strings to tool-call arguments too, so sending them
# on every request made a file that contains one of these markers (a chat
# template, or this repository) impossible to write. They go out only for a
# model that has actually run past its turn; the stream loop still cuts the
# first runaway itself.
_turn_boundary_seen: dict[str, float] = {}   # model id -> time a runaway was cut


def _turn_boundary_stops() -> list[str]:
    s = get_settings()
    ids = (s.get("model"), s.get("model_path"), _llama.loaded_model())
    return list(_TURN_BOUNDARY_STOPS) if any(str(m) in _turn_boundary_seen for m in ids if m) else []


def _mark_turn_boundary_runaway() -> None:
    s = get_settings()
    for m in (s.get("model"), s.get("model_path"), _llama.loaded_model()):
        if m:
            _turn_boundary_seen[str(m)] = time.time()


def _attach_session_state(messages: list[dict], body: str, role: str) -> list[dict]:
    """Add the volatile session-state block to a per-round wire copy.

    Mid-turn (last message is a tool result) the block rides inside that result.
    A separate trailing user/system message after a tool result reads as a new
    user turn, and with the previous round's reasoning still in context
    (Qwen3.5-family templates preserve it) the model replays that round verbatim
    (2/2 copies on a real 91K-token loop vs 0/2 when folded in). At turn start
    there is no prior round to replay, so it stays a separate message. Never
    mutates `messages`."""
    last = messages[-1] if messages else None
    if (isinstance(last, dict) and last.get("role") == "tool"
            and isinstance(last.get("content"), str)):
        return messages[:-1] + [dict(last, content=f"{last['content']}\n\n{body}")]
    return messages + [{"role": role, "content": body}]
