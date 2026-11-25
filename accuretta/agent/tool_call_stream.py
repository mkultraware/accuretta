# SPDX-License-Identifier: AGPL-3.0-only
"""Tool calls while they stream: unclosed calls, a call carried over a cut, markup
stripped from the visible text, progress shown before a call finishes, trailing calls
recovered, and parse failures logged.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import time
from accuretta.agent.tool_call_parse import _tool_text_view
from accuretta.agent.tool_call_text import _TOOL_CODE_CLOSER_RE
from accuretta.agent.tool_call_text import _TOOL_CODE_OPENER_RE
from accuretta.core.paths import DATA
from accuretta.core.settings import get_settings
from accuretta.core.tool_names import _resolve_tool_name


# Carrying a cut-off call across rounds is for file bodies, which really can
# be long. Any other call this long ran away (a red-team http_request reached
# 45,000 characters and was "continued" three times as a file write, then the
# turn ended on "the oversized file operation stopped").
_CARRY_OTHER_CALL_MAX_CHARS = 8000


def _cut_off_call_name(tail: str) -> str:
    """Tool name of an unfinished text tool call ("" when it can't be read)."""
    m = (re.search(r'"name"\s*:\s*"([A-Za-z0-9_.\-]+)"', tail or "")
         or re.search(r"<function=([A-Za-z0-9_.\-]+)", tail or ""))
    return m.group(1) if m else ""


def _is_file_write_call(name: str) -> bool:
    canon = _resolve_tool_name(name) if name else ""
    return bool(canon) and any(w in canon for w in ("write", "edit", "append", "patch", "create_file"))


# The tolerant XML-dialect parser will complete a call whose closing tags were
# cut off, which is usually fine. The case it must not swallow is a <tool_call>
# that never closes: the model hit max_tokens mid-file, and "ok"-ing that
# prefix as the whole file starts a cut-off-file spiral. An opener with no
# later </tool_call> anywhere marks the call as truncated.
_TOOL_CALL_OPENER_RE = re.compile(
    r"(?:<|&lt;|\\<)\s*[a-z]*tool_call(?:>|&gt;|\\>)?", re.IGNORECASE)


_TOOL_CALL_CLOSER_RE = re.compile(
    r"(?:<|&lt;|\\<)\s*/tool_call(?:>|&gt;|\\>)?", re.IGNORECASE)


def _unclosed_tool_code_tail(text: str) -> str:
    """Return an unfinished ``tool_code`` fence, including its opener."""
    if not text:
        return ""
    tail_start = -1
    view = _tool_text_view(text)
    for opener in _TOOL_CODE_OPENER_RE.finditer(view):
        if not _TOOL_CODE_CLOSER_RE.search(view, opener.end()):
            tail_start = opener.start()
    return text[tail_start:] if tail_start >= 0 else ""


def _has_unclosed_tool_call(text: str) -> bool:
    """True if the buffer ends inside a supported text-tool envelope.

    Covers XML ``<tool_call>`` blocks and Gemma-style ``tool_code`` fences; both
    are protocol frames even though llama-server delivers them as ordinary
    assistant content. Every opener must be closed before the next opener (or
    end of string); a segment without a closer means the call was cut off,
    usually at the token/thinking budget."""
    if not text:
        return False
    text = _tool_text_view(text)
    if _unclosed_tool_code_tail(text):
        return True
    openers = list(_TOOL_CALL_OPENER_RE.finditer(text))
    if not openers:
        return False
    boundaries = [o.end() for o in openers]
    boundaries.append(-1)  # sentinel: tail after the final opener is end-of-string
    for i, o in enumerate(openers):
        seg_end = boundaries[i + 1] if boundaries[i + 1] >= 0 else len(text)
        if not _TOOL_CALL_CLOSER_RE.search(text[o.end():seg_end]):
            return True
    return False


def _unclosed_tool_tail(text: str) -> str:
    """Return the last unfinished XML or ``tool_code`` protocol frame."""
    if not text:
        return ""
    code_tail = _unclosed_tool_code_tail(text)
    view = _tool_text_view(text)
    openers = list(_TOOL_CALL_OPENER_RE.finditer(view))
    if not openers:
        return code_tail
    # Walk forwards: an opener segment with no closer before the next opener/EOF
    # is a truncated call. Return from the LAST such opener to the end.
    tail_start = -1
    for i, o in enumerate(openers):
        seg_end = openers[i + 1].start() if i + 1 < len(openers) else len(text)
        if not _TOOL_CALL_CLOSER_RE.search(view[o.end():seg_end]):
            tail_start = o.start()
    xml_tail = text[tail_start:] if tail_start >= 0 else ""
    if code_tail and (not xml_tail or text.rfind(code_tail) > text.rfind(xml_tail)):
        return code_tail
    return xml_tail


def _merge_carried_tool_call(carried: str, continuation: str) -> str:
    """Join a truncated XML call with the model's next continuation.

    The normal path is llama.cpp assistant prefill: ``continuation`` contains
    only newly generated text and can be appended directly. Some templates
    nevertheless repeat part or all of the unfinished call. Remove exact
    overlap, accept a complete fresh call, and refuse to replace a longer
    partial call with an equal/shorter restart. The caller's progress guard then
    detects a model that keeps restarting instead of advancing.
    """
    if not carried:
        return continuation
    if not continuation:
        return carried

    carried_is_tool_code = bool(_TOOL_CODE_OPENER_RE.search(carried))
    continuation_is_tool_code = bool(_TOOL_CODE_OPENER_RE.search(continuation))
    if continuation_is_tool_code:
        # Some templates ignore assistant prefill and restart the complete
        # fenced call. A complete restart is authoritative; an equal/shorter
        # partial restart is left to the progress guard instead of being
        # appended into invalid Python.
        if not _has_unclosed_tool_call(continuation):
            return continuation
        if carried_is_tool_code:
            return continuation if len(continuation) > len(carried) else carried

    function_re = re.compile(
        r"<\s*(?:function|call)\s*=\s*[\"']?\s*[a-zA-Z0-9_.-]+",
        re.IGNORECASE)
    continuation_has_function = bool(function_re.search(continuation))
    if continuation_has_function and not _has_unclosed_tool_call(continuation):
        return continuation

    if continuation.startswith(carried):
        return continuation
    if carried.startswith(continuation):
        return carried
    if carried.endswith(continuation):
        return carried

    # Longest prefix of continuation that is also a suffix of carried. KMP keeps
    # this linear even for multi-megabyte generated files.
    prefix = [0] * len(continuation)
    matched = 0
    for i in range(1, len(continuation)):
        while matched and continuation[i] != continuation[matched]:
            matched = prefix[matched - 1]
        if continuation[i] == continuation[matched]:
            matched += 1
        prefix[i] = matched
    matched = 0
    for ch in carried:
        while matched and ch != continuation[matched]:
            matched = prefix[matched - 1]
        if ch == continuation[matched]:
            matched += 1
            if matched == len(continuation):
                matched = prefix[matched - 1]
    if matched >= 8:
        return carried + continuation[matched:]

    if carried_is_tool_code:
        return carried + continuation

    carried_has_function = bool(function_re.search(carried))
    if not carried_has_function:
        return continuation
    if continuation_has_function:
        return continuation if len(continuation) > len(carried) else carried

    chunk = continuation
    # The wrapper can be repeated more than once after a budget continuation.
    chunk = re.sub(r"^\s*(?:<\s*tool_call\s*>\s*)+", "", chunk,
                   flags=re.IGNORECASE)
    # The previous chunk is normally still inside content. Do not nest a
    # duplicate parameter tag, which would make the XML parser lose the tail.
    chunk = re.sub(r"^\s*<\s*parameter\s*=\s*content\s*>", "", chunk,
                   count=1, flags=re.IGNORECASE)
    return carried + chunk


def _strip_tool_markup(text: str, pattern: str) -> str:
    matches = list(re.finditer(pattern, _tool_text_view(text), re.IGNORECASE))
    for match in reversed(matches):
        text = text[:match.start()] + text[match.end():]
    return text


def _flush_tool_stream(state) -> str:
    if isinstance(state, dict) and not state.get("closer"):
        return state.get("pending", "")
    return ""


# Heuristic: model emitted tool-call syntax but no parser matched it.
# When this fires with zero parsed calls, the reply is almost certainly
# a hallucination from a chat-template mismatch.
TOOL_SYNTAX_HINT_RE = re.compile(
    r'<call:[a-zA-Z]|<\|python_tag\|>|\[TOOL_CALLS\]|<tool_call>|<\|tool_call>|```tool_call|```tool_code|<\|function_calls_begin\|>|\{"name"\s*:\s*"',
    re.IGNORECASE)


# Cyber-range / CTF breach marker. A FLAG{...} captured in a tool response is
# treated as confirmed access by run_chat_turn's breach detection.
_FLAG_RE = re.compile(r"FLAG\{[^}]+\}")


def _is_breach_capable_tool(name: str | None) -> bool:
    """Only tools that actually TOUCH a target can 'capture' a flag: network
    probes and the recon suite. A FLAG{...} that merely appears in a file read,
    grep hit, or local transform was already sitting on disk — reading your own
    pentest report should not re-trigger 'confirmed access'. Mirrors the
    frontend's isRedTeamTool gate (minus encode_decode, which is a local
    transform, not target access)."""
    n = name or ""
    return n.startswith("recon_") or n == "http_request"


def _looks_unfinished(text: str, finish_reason: str = None) -> bool:
    """True when a reply is truncated by token limit or trails off on a colon."""
    if finish_reason == "length":
        return True
    tail = (text or "").strip()
    if not tail:
        return False
    if tail.endswith(":"):
        return True
    return False


def _log_tool_parse_failure(chat_id: str, round_no: int, native: bool, finish_reason,
                            raw_answer: str, slots: dict) -> None:
    """Opt-in (debug_tool_parse_failures): keep the raw output of a round whose
    tool call lost its arguments, to see whether llama.cpp's parser or the model
    dropped them. The saved chat cannot show this: call markup is stripped."""
    try:
        settings = get_settings()
        if not settings.get("debug_tool_parse_failures"):
            return
        path = DATA / "tool_parse_failures.jsonl"
        if path.exists() and path.stat().st_size > 5_000_000:
            path.replace(path.with_name(path.name + ".1"))
        record = {
            "t": int(time.time()), "chat_id": chat_id, "round": round_no,
            "model": str(settings.get("model") or ""), "native": native,
            "finish_reason": finish_reason, "answer_tail": raw_answer[-4000:],
            "native_calls": [{"name": slot.get("name"),
                              "arguments": str(slot.get("arguments") or "")[:4000]}
                             for _, slot in sorted(slots.items())],
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# One or more tool-call blocks that end the text, each free of a nested opener,
# so a call merely mentioned earlier can never swallow the prose after it.
_TRAILING_CALL_RUN_RE = re.compile(
    r"(?:\s*(?:<tool_call>(?:(?!<tool_call>)[\s\S])*?(?:</tool_call>|$)"
    r"|```tool_code\b(?:(?!```)[\s\S])*(?:```|$)))+\s*$",
    re.IGNORECASE)


def _recover_trailing_calls(reasoning_text: str) -> tuple[str, str]:
    """Move tool-call blocks that end the reasoning into the answer.

    Some models emit their call before they close the reasoning block. Only a
    run of call blocks with nothing after it moves; a call mentioned earlier in
    the reasoning stays there and is never parsed."""
    body = re.sub(r"</think>\s*$", "", reasoning_text or "", flags=re.IGNORECASE)
    match = _TRAILING_CALL_RUN_RE.search(body)
    if not match:
        return reasoning_text, ""
    kept = body[:match.start()].rstrip()
    return (kept + "</think>" if kept else ""), body[match.start():].strip()


# A write that streams for a minute shows nothing until the call is complete, so
# the stream loop reports the target and size of a call while it arrives.
_PROGRESS_PATH_RES = (
    re.compile(r'"path"\s*:\s*"((?:[^"\\]|\\.)*)"'),
    re.compile(r"<parameter=path>\s*(.*?)\s*</parameter>", re.DOTALL),
    re.compile(r"\bpath\s*=\s*(?:\"([^\"\n]*)\"|'([^'\n]*)')"),
)


_TEXT_CALL_OPEN_RE = re.compile(r"<tool_call>|```tool_code", re.IGNORECASE)


_TEXT_CALL_NAME_RE = re.compile(r"<function=([\w.-]+)>|\"name\"\s*:\s*\"([\w.-]+)\"|^\s*([\w.]+)\s*\(")


def _text_call_progress(text: str) -> tuple[str, str] | None:
    """Name and arguments so far of a text-mode tool call that is still open."""
    opens = list(_TEXT_CALL_OPEN_RE.finditer(text))
    if not opens:
        return None
    tail = text[opens[-1].end():]
    closer = "</tool_call>" if opens[-1].group().startswith("<") else "```"
    if closer in tail.lower():
        return None
    match = _TEXT_CALL_NAME_RE.search(tail)
    name = next((g for g in match.groups() if g), "") if match else ""
    return (name, tail) if name else None


def _maybe_emit_call_progress(emit, sent: dict, key, name: str, raw: str) -> None:
    """Emit tool_progress for a call of at least 1 KB, then every 5 lines or 2 KB."""
    state = sent.setdefault(key, {"checked": 0, "lines": -1, "bytes": 0, "path": ""})
    if not name or len(raw) < 1024 or len(raw) - state["checked"] < 256:
        return
    state["checked"] = len(raw)
    path = ""
    for regex in _PROGRESS_PATH_RES:
        match = regex.search(raw)
        if match:
            path = next(g for g in match.groups() if g is not None)
            if regex is _PROGRESS_PATH_RES[0]:
                try:
                    path = json.loads('"' + path + '"')
                except ValueError:
                    pass
            break
    lines = raw.count("\n") + raw.count("\\n")
    if (state["lines"] >= 0 and lines < state["lines"] + 5 and path == state["path"]
            and len(raw) - state["bytes"] < 2048):
        return
    state.update(lines=lines, bytes=len(raw), path=path)
    emit({"type": "tool_progress", "name": _resolve_tool_name(name) or name, "path": path,
          "lines": lines, "bytes": len(raw)})
