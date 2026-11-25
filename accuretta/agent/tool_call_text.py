# SPDX-License-Identifier: AGPL-3.0-only
"""Reading tool calls out of model text: repairing JavaScript-style arguments into JSON,
and telling whether a streaming block is still an open tool call.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re


_TOOL_CODE_OPENER_RE = re.compile(r"```tool_code\b[^\r\n]*\r?\n?", re.IGNORECASE)


_TOOL_CODE_CLOSER_RE = re.compile(r"```")


# Live-stream suppression: in text-tools mode the model's raw <tool_call> XML
# streams as content and would show up in the bubble as gibberish. On an
# opener, suppress everything until its matching closer (</tool_call> for
# <tool_call>, ``` for a ```tool_call fence) or end of stream. Tolerates the
# mangled dialect seen in the wild: `function="NAME">`,
# `<parametername="path">`, a missing leading `<`.
_TOOL_STREAM_PAIRS = (
    (re.compile(r"(?:<|&lt;|\\<)tool_call\b", re.IGNORECASE),
     re.compile(r"(?:</|&lt;/|\\</)tool_call\s*(?:>|&gt;)", re.IGNORECASE)),
    (re.compile(r"```tool_call", re.IGNORECASE),
     re.compile(r"```")),
    (re.compile(r"<call:", re.IGNORECASE),
     re.compile(r"</call(?::[a-zA-Z0-9_\-]+)?>", re.IGNORECASE)),
    (re.compile(r"\[TOOL_CALLS\]\[", re.IGNORECASE),
     re.compile(r"\}\s*\]")),
    # Legacy JSON-form tool calls were wrapped in <tool:tool>...</tool:tool>;
    # close on the wrapper so the whole JSON blob (including "content") is hidden.
    (re.compile(r"<tool:tool\b"),
     re.compile(r"</tool:tool>", re.IGNORECASE)),
    # Gemma/Qwen text-tools fallback. This is a protocol envelope, not code
    # intended for the user, so never stream its potentially huge arguments
    # into the answer bubble.
    (_TOOL_CODE_OPENER_RE, _TOOL_CODE_CLOSER_RE),
)


_TOOL_STREAM_OPEN_PREFIXES = tuple(s.lower() for s in (
    "<tool_call", "&lt;tool_call", "\\<tool_call", "```tool_call",
    "```tool_code", "<call:", "[tool_calls][", "<tool:tool",
))


def _tool_stream_pending_suffix(text: str) -> str:
    """Potential opener fragment at the end of one streamed token chunk."""
    lowered = text.lower()
    limit = min(len(lowered), max(map(len, _TOOL_STREAM_OPEN_PREFIXES)))
    for size in range(limit, 0, -1):
        suffix = lowered[-size:]
        if any(prefix.startswith(suffix) for prefix in _TOOL_STREAM_OPEN_PREFIXES):
            return text[-size:]
    return ""


def _classify_tool_stream(text: str, in_block) -> tuple[str, object]:
    """Hide actual tool frames while keeping Markdown examples visible."""
    out: list[str] = []
    state = dict(in_block) if isinstance(in_block, dict) else {}
    if in_block and not isinstance(in_block, dict):
        state["closer"] = in_block
    text = state.pop("pending", "") + text

    def consume(value: str, visible: bool = True) -> None:
        if visible:
            out.append(value)
        prefix = state.get("line_prefix", "") + value
        state["line_prefix"] = prefix.rsplit("\n", 1)[-1][-256:]

    while text:
        if state.get("example_fence"):
            end = text.find("\n")
            if end < 0:
                state["pending"] = text
                break
            line, text = text[:end + 1], text[end + 1:]
            delimiter = state["example_fence"]
            if re.fullmatch(r"[ \t]*" + re.escape(delimiter[0]) + "{"
                            + str(len(delimiter)) + r",}[ \t]*\r?\n", line):
                state.pop("example_fence")
            consume(line)
            continue
        if state.get("example_ticks"):
            match = re.search(r"`+", text)
            if match is None:
                consume(text)
                text = ""
                break
            if match.end() == len(text):
                consume(text[:match.start()])
                state["pending"] = text[match.start():]
                break
            consume(text[:match.end()])
            text = text[match.end():]
            if len(match.group()) == state["example_ticks"]:
                state.pop("example_ticks")
            continue
        if state.get("closer"):
            match = state["closer"].search(text)
            if match is None:
                state["pending"] = text[-64:]
                consume(text[:-64], visible=False)
                break
            consume(text[:match.end()], visible=False)
            text = text[match.end():]
            state.pop("closer")
            continue
        best = None
        for opener, closer in _TOOL_STREAM_PAIRS:
            match = opener.search(text)
            if match and (best is None or match.start() < best[0].start()):
                best = (match, closer)
        marker = re.search(r"`+|~{1,}", text)
        if marker and (best is None or marker.start() <= best[0].start()):
            consume(text[:marker.start()])
            text = text[marker.start():]
            delimiter = marker.group()
            if len(text) == len(delimiter):
                state["pending"] = text
                break
            fenced = len(delimiter) >= 3 and not state.get("line_prefix", "").strip()
            if fenced:
                end = text.find("\n")
                if end < 0:
                    state["pending"] = text
                    break
                label = text[len(delimiter):end].strip().lower()
                if delimiter[0] == "`" and label in ("tool_call", "tool_code"):
                    state["closer"] = _TOOL_CODE_CLOSER_RE
                    consume(text[:end + 1], visible=False)
                else:
                    state["example_fence"] = delimiter
                    consume(text[:end + 1])
                text = text[end + 1:]
            else:
                if delimiter[0] == "`":
                    state["example_ticks"] = len(delimiter)
                consume(delimiter)
                text = text[len(delimiter):]
            continue
        if best:
            match, closer = best
            consume(text[:match.start()])
            consume(text[match.start():match.end()], visible=False)
            text = text[match.end():]
            state["closer"] = closer
            continue
        pending = _tool_stream_pending_suffix(text)
        if pending:
            consume(text[:-len(pending)])
            state["pending"] = pending
        else:
            consume(text)
        break
    return "".join(out), state


def _js_to_json(s: str) -> str:
    """Best-effort: turn a JavaScript-style object literal into valid JSON.
    Handles two failure modes seen on local fine-tunes:
      - unquoted identifier keys:  {path: "..."}      → {"path": "..."}
      - invalid backslash escapes: "C:\\Users\\..."   → "C:\\\\Users\\\\..."
        (Windows paths emitted as raw \\ in JSON strings)
    Conservative: only touches obvious problems, leaves valid JSON alone."""
    # quote unquoted identifier keys appearing after { or ,
    s = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
    # double any backslash that isn't part of a valid JSON escape sequence.
    # Valid: \" \\ \/ \b \f \n \r \t \uXXXX. Anything else gets escaped.
    s = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
    return s


def repair_tool_args(raw) -> dict:
    """Coerce a tool-call arguments blob to a dict. Accepts dict, JSON string,
    or broken JSON (trailing prose, missing close brace, code fences,
    JavaScript-style unquoted keys, raw Windows backslashes). Returns {}
    on total failure — the agentic loop then feeds that back as an error
    and the model retries with a clean call."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    # Some Gemma fine-tunes wrap string values in <|"|> (special-token vocabulary
    # bleeding into content); normalize it, and the single-quote variant, before
    # parsing.
    s = s.replace('<|"|>', '"').replace("<|'|>", "'")
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?\s*```\s*$", "", s)
    # Unquoted string values after a colon, e.g. Gemma's
    # `"query":Casemiro Manchester United news today}`. Only triggers when the
    # value starts with a letter (never a JSON primitive opener), quoting the run
    # up to the next structural char.
    def _quote_unquoted_values(text: str) -> str:
        # Key + ":" + an unquoted run up to the next , } or ]. The run may hold spaces
        # and punctuation (bare values often look like sentences); internal " and \
        # are escaped.
        pattern = re.compile(
            r'("[A-Za-z_][\w-]*"\s*:\s*)'        # key + colon
            r'([A-Za-z][^,\}\]\n\r]*?)'           # alphabetic-start value
            r'(\s*[,\}\]])'                        # trailing structural
        )
        def repl(m):
            key_part = m.group(1)
            value = m.group(2).strip()
            trailing = m.group(3)
            # Skip if value is a JSON primitive that just happens to be
            # alphabetic (true / false / null).
            if value.lower() in ("true", "false", "null"):
                return m.group(0)
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'{key_part}"{escaped}"{trailing}'
        # Run twice — first pass may leave nested issues that the second
        # catches once the outer structure is well-formed.
        out = pattern.sub(repl, text)
        out = pattern.sub(repl, out)
        return out
    s = _quote_unquoted_values(s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    # Escape literal newlines/tabs inside JSON string values — Gemma and
    # other local models emit real \n characters inside "content":"..." when
    # the value contains code.  json.loads rejects unescaped control chars.
    def _esc_ctrl_in_strings(t: str) -> str:
        out = []
        in_s = False
        esc2 = False
        for ch in t:
            if in_s:
                if esc2:
                    esc2 = False
                    out.append(ch)
                elif ch == '\\':
                    esc2 = True
                    out.append(ch)
                elif ch == '"':
                    in_s = False
                    out.append(ch)
                elif ch == '\n':
                    out.append('\\n')
                elif ch == '\r':
                    out.append('\\r')
                elif ch == '\t':
                    out.append('\\t')
                else:
                    out.append(ch)
            else:
                if ch == '"':
                    in_s = True
                out.append(ch)
        return "".join(out)
    s_esc = _esc_ctrl_in_strings(s)
    if s_esc != s:
        try:
            v = json.loads(s_esc)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
        s = s_esc  # use the escaped version for subsequent attempts
    # try JS-style → JSON repair (unquoted keys + Windows-path backslashes)
    try:
        v = json.loads(_js_to_json(s))
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    # slice to outermost braces — catches leading/trailing prose
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        sliced = s[start:end + 1]
        try:
            v = json.loads(sliced)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
        try:
            v = json.loads(_js_to_json(sliced))
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
    # balance missing closing braces
    if start >= 0:
        tail = s[start:]
        opens = tail.count("{")
        closes = tail.count("}")
        if opens > closes:
            patched = tail + ("}" * (opens - closes))
            try:
                v = json.loads(patched)
                return v if isinstance(v, dict) else {"value": v}
            except Exception:
                pass
            try:
                v = json.loads(_js_to_json(patched))
                return v if isinstance(v, dict) else {"value": v}
            except Exception:
                pass
    return {}
