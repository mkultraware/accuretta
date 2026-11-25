# SPDX-License-Identifier: AGPL-3.0-only
"""Finding tool calls in a model's text reply, in every format models use (fenced JSON,
Qwen, Mistral, Gemma, XML tags, Python calls), and normalising their arguments.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.agent.system_prompt import _active_tools
from accuretta.agent.tool_call_text import _TOOL_STREAM_PAIRS
from accuretta.agent.tool_call_text import _js_to_json
from accuretta.agent.tool_call_text import repair_tool_args
from accuretta.agent.tool_parsing import _find_balanced_json_objects
from accuretta.core.chats import _get_current_chat
from accuretta.core.tool_names import _resolve_tool_name
from accuretta.core.tool_registry import TOOLS


def _normalize_tool_argument_shapes(name: str, arguments):
    """Repair schema-known container shapes without guessing at user data.

    The XML fallback parses each ``<parameter>`` independently. Its legacy JSON
    repair helper must return a dict, so a valid array becomes
    ``{"value": [...]}``. Only unwrap that exact one-key shape when the target
    tool schema explicitly declares the parameter as an array.
    """
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return arguments
    tool = TOOLS.get(_resolve_tool_name(name)) or {}
    properties = ((tool.get("parameters") or {}).get("properties") or {})
    repaired = arguments
    for key, schema in properties.items():
        if not isinstance(schema, dict) or schema.get("type") != "array":
            continue
        wrapped = arguments.get(key)
        if (isinstance(wrapped, dict) and set(wrapped) == {"value"}
                and isinstance(wrapped.get("value"), list)):
            if repaired is arguments:
                repaired = dict(arguments)
            repaired[key] = wrapped["value"]
        elif isinstance(wrapped, str) and wrapped.lstrip().startswith("["):
            try:
                parsed = json.loads(wrapped)
            except ValueError:
                continue
            if isinstance(parsed, list):
                if repaired is arguments:
                    repaired = dict(arguments)
                repaired[key] = parsed
    # Small models often send one edit flat, sometimes with Claude Code's
    # old_string/new_string names.
    if _resolve_tool_name(name) == "edit_file" and not repaired.get("edits"):
        old = repaired.get("old_text", repaired.get("old_string"))
        new = repaired.get("new_text", repaired.get("new_string"))
        if isinstance(old, str) and isinstance(new, str):
            repaired = {k: v for k, v in repaired.items()
                        if k not in ("old_text", "new_text", "old_string", "new_string")}
            repaired["edits"] = [{"old_text": old, "new_text": new}]
    return repaired


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)


TOOL_CALL_FENCE_RE = re.compile(r"```tool_call\s*\n([\s\S]*?)\n```", re.IGNORECASE)


_EXAMPLE_MARKER_RE = re.compile(r"`+|^[ \t]*~{3,}", re.MULTILINE)


def _tool_text_view(text: str) -> str:
    """Blank Markdown examples, preserving offsets and actual tool payloads."""
    parts = []
    pos = 0
    payloads = []
    for pattern in (TOOL_CALL_GEMMA_RE, TOOL_CALL_QWEN_RE,
                    TOOL_CALL_PYTAG_RE, TOOL_CALL_SELFCLOSING_RE):
        payloads.extend((match.start(), match.end()) for match in pattern.finditer(text))
    for start, end in _find_balanced_json_objects(text):
        try:
            value = json.loads(text[start:end])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict) and (value.get("name") or value.get("function")) \
                and any(key in value for key in ("arguments", "parameters", "parameter")):
            payloads.append((start, end))
    for match in TOOL_CALL_PYFUNC_RE.finditer(text):
        if _parse_python_tool_call(match.group().strip().strip("[]").strip()):
            payloads.append((match.start(), match.end()))
    payloads.sort()
    payload_index = 0
    while pos < len(text):
        marker = _EXAMPLE_MARKER_RE.search(text, pos)
        tool = None
        for opener, closer in _TOOL_STREAM_PAIRS:
            match = opener.search(text, pos)
            if match and (tool is None or match.start() < tool[0].start()):
                tool = (match, closer)
        while payload_index < len(payloads) and payloads[payload_index][0] < pos:
            payload_index += 1
        if payload_index < len(payloads):
            start, end = payloads[payload_index]
            if (marker is None or start < marker.start()) and (tool is None or start < tool[0].start()):
                parts.append(text[pos:end])
                pos = end
                continue
        if tool and (marker is None or tool[0].start() <= marker.start()):
            end = tool[1].search(text, tool[0].end())
            stop = end.end() if end else len(text)
            parts.append(text[pos:stop])
            pos = stop
            continue
        if marker is None:
            parts.append(text[pos:])
            break
        delimiter = marker.group().strip()
        line_start = text.rfind("\n", 0, marker.start()) + 1
        fenced = len(delimiter) >= 3 and not text[line_start:marker.start()].strip()
        if fenced:
            close = re.compile(r"^[ \t]*" + re.escape(delimiter[0])
                               + "{" + str(len(delimiter)) + r",}[ \t]*$", re.MULTILINE)
            end = close.search(text, marker.end())
        else:
            close = re.compile(r"(?<!`)" + re.escape(delimiter) + r"(?!`)")
            end = close.search(text, marker.end())
        stop = end.end() if end else len(text)
        parts.append(text[pos:marker.start()])
        parts.append(re.sub(r"[^\r\n]", " ", text[marker.start():stop]))
        pos = stop
    return "".join(parts)


# Extra dialects from non-OpenAI fine-tunes. Tier-2 fallback only — native
# tool_calls on the streamed delta still wins. Patterns are anchored on
# dialect-specific markers so they cannot collide with each other.
#   <call:NAME>{args}</call:NAME>      seen on BugTraceAI and similar tunes
#   <|python_tag|>{...}<|eom_id|>      llama-3.1 / 3.2 native format
#   [TOOL_CALLS][{...}, ...]           mistral native format
TOOL_CALL_NAMED_RE = re.compile(
    # Accept either </call> or </call:NAME> for the closer. BugTraceAI and
    # other gemma-derived tunes drop the name in the close tag.
    r"<call:([a-zA-Z0-9_\-]+)>\s*(\{[\s\S]*?\})\s*</call(?::\1)?>",
    re.IGNORECASE)


TOOL_CALL_PYTAG_RE = re.compile(
    r"<\|python_tag\|>\s*(\{[\s\S]*?\})\s*(?:<\|eom_id\|>|<\|eot_id\|>|$)",
    re.IGNORECASE)


TOOL_CALL_MISTRAL_RE = re.compile(
    r"\[TOOL_CALLS\]\s*(\[[\s\S]*?\])", re.IGNORECASE)


# XML-tag dialect: Hermes-3 / GLM-4 / some Qwen3 finetunes emit
#   <tool_call><function=NAME><parameter=KEY>VAL</parameter>...</function></tool_call>
# Tolerant: closing </function> and/or </tool_call> may be missing if the
# model truncates. We anchor on <tool_call> + <function=NAME> and then walk
# parameters until we hit a closer or the next tool_call/end-of-string.
TOOL_CALL_XMLTAG_RE = re.compile(
    r"<tool_call>\s*<?\s*(?:function|call)\s*=\s*\"?\s*([a-zA-Z0-9_\-\.]+)\s*\"?\s*>"
    r"([\s\S]*?)"
    r"(?:</function>\s*</tool_call>|</tool_call>|(?=<tool_call>)|$)",
    re.IGNORECASE)


TOOL_PARAM_XMLTAG_RE = re.compile(
    r"<\s*parameter(?:\s*name)?\s*=\s*\"?\s*([a-zA-Z0-9_\-\.]+)\s*\"?\s*>"
    r"([\s\S]*?)(?:</parameter>|</function>|</tool_call>|<tool_call>|$)",
    re.IGNORECASE)


# Gemma 4 native tool-call dialect. Format:
#   <|tool_call>call:NAME{key:value,key:<|"|>string<|"|>,key:42}<tool_call|>
# where <|"|> is Gemma's special-token string delimiter (not a JSON quote).
# Asymmetric opener / closer is intentional — that's how Google spec'd it.
# Closer is forgiving so a truncated emit (model cut off before <tool_call|>)
# still parses up to end-of-string or the next opener.
# Spec: https://ai.google.dev/gemma/docs/capabilities/text/function-calling-gemma4
TOOL_CALL_GEMMA_RE = re.compile(
    r"<\|tool_call>\s*call\s*:\s*([\w.\-]+)\s*\{([\s\S]*?)\}\s*"
    r"(?:<tool_call\|>|(?=<\|tool_call>)|$)",
    re.IGNORECASE)


# Qwen native tool-call dialect. Format:
#   <|function_calls_begin|><|function_call|>{"name":"...","arguments":"..."}<|function_calls_end|>
TOOL_CALL_QWEN_RE = re.compile(
    r"<\|function_call\|>\s*(\{[\s\S]*?\})\s*(?:<\|function_calls_end\|>|(?=<\|function_call\|>)|$)",
    re.IGNORECASE)


# Self-closing XML tag dialect (often output by models like Gemma when prompted
# with certain schemas, or Llama 3 when hallucinating XML):
#   <tool_call name="..." arguments='...'/>
TOOL_CALL_SELFCLOSING_RE = re.compile(
    r"<tool_call\s+([^>]+)/>",
    re.IGNORECASE)


# Gemma / CodeGemma python-style dialect: per Google's function-calling spec
# the model emits a python call inside a ```tool_code``` fence (sometimes
# wrapped in print(...)), e.g. ```tool_code\nweb_search(query="news")\n```.
# The token-based <|tool_call> form above covers only some tunes; real
# Gemma-2/3 GGUFs emit this fenced shape.
TOOL_CALL_TOOLCODE_RE = re.compile(r"```(?:tool_code|python)\s*\n?([\s\S]*?)```",
                                   re.IGNORECASE)


# A bare/bracketed single python call: name(...) or [name(...)]. It must be on
# its own line: an unanchored match turns prose parentheticals ("without using
# update_plan (this is one-step)") into phantom tool calls that derail the
# turn. A prose false negative is harmless (the fenced form and XML dialects
# still catch real calls); a false positive breaks the turn. Non-greedy to the
# first ")", so a ")" inside a string arg is only caught by the fenced form.
TOOL_CALL_PYFUNC_RE = re.compile(
    r"(?m)^[ \t]*\[?\s*(?:print\s*\(\s*)?([a-zA-Z_]\w*)\s*\(([^()]*)\)\s*\)?\s*\]?[ \t]*(?:\r?\n|$)")


def _parse_python_tool_call(expr: str) -> dict | None:
    """Parse ONE python-style call — name(k=v, ...) or name("positional") — into
    {name, arguments}. This is Gemma's documented function-call shape. Uses ast
    so nested quotes / lists / dicts parse correctly, unwraps a print(...) that
    Gemma sometimes adds, and maps positional args onto the tool's declared
    parameter order. Returns None unless the call names a REAL active tool — so
    ordinary python the model prints in prose/code is never mistaken for a call."""
    import ast
    src = (expr or "").strip()
    if not src:
        return None
    try:
        node = ast.parse(src, mode="eval").body
    except Exception:
        return None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "print" and len(node.args) == 1 and not node.keywords):
        node = node.args[0]  # unwrap print(call(...))
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        return None
    tools = _active_tools(True, _get_current_chat())
    if name not in tools:
        return None

    def _lit(v):
        try:
            return ast.literal_eval(v)
        except Exception:
            return _FAIL

    args: dict = {}
    for kw in node.keywords:
        if kw.arg:
            val = _lit(kw.value)
            if val is _FAIL:
                return None  # non-literal expression — prose, not a call
            args[kw.arg] = val
    if node.args:
        props = list(tools[name]["parameters"].get("properties", {}).keys())
        for i, a in enumerate(node.args):
            val = _lit(a)
            if val is _FAIL:
                return None  # non-literal expression — prose, not a call
            if i < len(props):
                args[props[i]] = val
    return {"name": name, "arguments": args}


# Sentinel: ast.literal_eval failure (BinOp, Name, Call — any non-literal
# expression). Real tool calls always pass literal values; a "call" with an
# expression argument is almost certainly prose like "run_grep (in src)".
_FAIL = object()


def extract_tool_calls(text: str, python_calls: bool = True) -> list[dict]:
    """Parse tool calls from free text. Tries multiple dialects so models
    that weren't fine-tuned on the OpenAI/llama-server schema can still
    drive the agent loop. All paths normalize to {name, arguments}.
    Native tool_calls on the streamed delta still take precedence; this
    only runs when that field came back empty.

    python_calls=False disables the bare python-call prose scan (case 7b).
    Native-tools models emit calls as structured deltas, never as bare
    `name(...)` lines — scanning their prose for "update_plan (...)" only
    manufactures phantom calls out of narration. Dedicated tool fences and
    unquoted protocol blocks remain enabled; ordinary Markdown examples are
    excluded from every dialect."""
    text = _tool_text_view(text)
    calls: list[dict] = []
    seen = set()

    def _add(parsed) -> None:
        if not isinstance(parsed, dict):
            return
        name = parsed.get("name") or parsed.get("tool")
        if not name:
            return
        if isinstance(parsed.get("arguments"), dict):
            normalized = _normalize_tool_argument_shapes(name, parsed["arguments"])
            if normalized is not parsed["arguments"]:
                parsed = dict(parsed)
                parsed["arguments"] = normalized
        # de-dup identical consecutive calls (some models double-emit)
        key = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        if key in seen:
            return
        seen.add(key)
        calls.append(parsed)

    # 1. <tool_call>{...}</tool_call> and ```tool_call fences  (hermes/qwen)
    for m in list(TOOL_CALL_RE.finditer(text)) + list(TOOL_CALL_FENCE_RE.finditer(text)):
        _add(repair_tool_args(m.group(1)))

    # 2. <call:NAME>{args}</call:NAME>  (BugTraceAI dialect and similar)
    for m in TOOL_CALL_NAMED_RE.finditer(text):
        args = repair_tool_args(m.group(2))
        _add({"name": m.group(1), "arguments": args})

    # 3. <|python_tag|>{...}<|eom_id|>  (llama-3.1 / 3.2 native)
    for m in TOOL_CALL_PYTAG_RE.finditer(text):
        parsed = repair_tool_args(m.group(1))
        # llama uses "parameters" instead of "arguments" — normalize
        if isinstance(parsed, dict) and "parameters" in parsed and "arguments" not in parsed:
            parsed["arguments"] = parsed.pop("parameters")
        _add(parsed)

    # 4. [TOOL_CALLS][{...}, ...]  (mistral native)
    for m in TOOL_CALL_MISTRAL_RE.finditer(text):
        try:
            arr = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    _add(item)

    # 5. Qwen native tool-call dialect
    for m in TOOL_CALL_QWEN_RE.finditer(text):
        parsed = repair_tool_args(m.group(1))
        # Qwen's JSON structure has {"name": "...", "arguments": "{...}"} where arguments is often a stringified JSON.
        if isinstance(parsed, dict) and "arguments" in parsed and isinstance(parsed["arguments"], str):
            parsed["arguments"] = repair_tool_args(parsed["arguments"])
        _add(parsed)

    # 5. <tool_call><function=NAME><parameter=K>V</parameter>...</function></tool_call>
    #    (Hermes-3 / GLM-4 / some Qwen3 finetunes — XML-tag dialect)
    for m in TOOL_CALL_XMLTAG_RE.finditer(text):
        name = m.group(1)
        body = m.group(2) or ""
        args: dict = {}
        for pm in TOOL_PARAM_XMLTAG_RE.finditer(body):
            k = pm.group(1)
            v = (pm.group(2) or "").strip()
            # try to coerce numeric / bool / json-ish values, fall back to str
            if v.lower() in ("true", "false"):
                args[k] = (v.lower() == "true")
            else:
                try:
                    args[k] = int(v)
                except Exception:
                    try:
                        args[k] = float(v)
                    except Exception:
                        if (v.startswith("{") and v.endswith("}")) or \
                           (v.startswith("[") and v.endswith("]")):
                            parsed_v = repair_tool_args(v)
                            args[k] = parsed_v if parsed_v is not None else v
                        else:
                            args[k] = v
        _add({"name": name, "arguments": args})

    # 6. Gemma 4 native dialect: <|tool_call>call:NAME{k:v,k:<|"|>str<|"|>}<tool_call|>
    # The body is Gemma's own key:value syntax with <|"|> as the string delimiter:
    # substitute <|"|> -> ", wrap in {}, and run it through _js_to_json (which
    # quotes bare identifier keys).
    for m in TOOL_CALL_GEMMA_RE.finditer(text):
        name = m.group(1)
        body = (m.group(2) or "").strip()
        # Map Gemma's string-delimiter token to a real " character.
        body = body.replace('<|"|>', '"').replace("<|'|>", "'")
        args: dict = {}
        # Try as a JSON object first (after wrapping in braces). _js_to_json
        # handles the unquoted-key case that Gemma's body always has.
        candidate = "{" + body + "}"
        try:
            args = json.loads(candidate)
        except Exception:
            try:
                args = json.loads(_js_to_json(candidate))
            except Exception:
                # Last resort: hand the body to repair_tool_args wrapped in
                # braces so its full repair chain (unquoted values etc.) gets
                # a swing at it.
                args = repair_tool_args(candidate) or {}
        if not isinstance(args, dict):
            args = {}
        _add({"name": name, "arguments": args})

    # 7. Self-closing XML tag dialect: <tool_call name="..." arguments='...'/>
    for m in TOOL_CALL_SELFCLOSING_RE.finditer(text):
        attrs = m.group(1)
        name_m = re.search(r"name=(['\"])(.*?)\1", attrs, re.IGNORECASE)
        args_m = re.search(r"arguments=(['\"])(.*?)\1", attrs, re.IGNORECASE)
        if name_m and args_m:
            name = name_m.group(2)
            # Some models emit html-encoded JSON in the attribute
            args_str = args_m.group(2).replace('&quot;', '"').replace('&apos;', "'").replace('&#39;', "'")
            _add({"name": name, "arguments": repair_tool_args(args_str)})

    # 7b. Gemma / CodeGemma python-style calls: a ```tool_code``` fence, or a
    # bare/bracketed name(...) line. _parse_python_tool_call accepts only real
    # active tools with literal arguments. The bare-line scan is skipped for
    # native-tools models, where such a line is prose by definition.
    _py_candidates: list[str] = [m.group(1) for m in TOOL_CALL_TOOLCODE_RE.finditer(text)]
    if python_calls:
        # scan for bare calls in the text with the fenced regions blanked out
        # so we don't double-count what the fence already captured.
        _defenced = TOOL_CALL_TOOLCODE_RE.sub(" ", text)
        _py_candidates += [m.group(0) for m in TOOL_CALL_PYFUNC_RE.finditer(_defenced)]
    for _cand in _py_candidates:
        parsed = _parse_python_tool_call(_cand.strip().strip("[]").strip())
        if parsed:
            _add(parsed)

    # 8. Bare JSON tool call, last-chance fallback: raw {"name":..., "arguments":...}
    # (or Qwen's {"function":..., "parameter":...}) without any <tool_call> tags.
    # Only runs when the parsers above found nothing.
    if not calls and ('"name"' in text or "'name'" in text or '"function"' in text or "'function'" in text):
        spans = _find_balanced_json_objects(text)
        for s, e in spans:
            chunk = text[s:e]
            if '"name"' not in chunk and "'name'" not in chunk and '"function"' not in chunk and "'function'" not in chunk:
                continue
            parsed = repair_tool_args(chunk)
            if not isinstance(parsed, dict):
                continue
            nm = parsed.get("name") or parsed.get("tool") or parsed.get("function")
            if not nm:
                continue
            if "arguments" not in parsed and "parameters" not in parsed and "parameter" not in parsed:
                continue
            if "parameters" in parsed and "arguments" not in parsed:
                parsed["arguments"] = parsed.pop("parameters")
            elif "parameter" in parsed and "arguments" not in parsed:
                parsed["arguments"] = parsed.pop("parameter")
            _add(parsed)

        # 8b. Truncated bare JSON: the model ran out of tokens mid-object, so there
        # is no balanced span. Start at the first '{' before a '"name"' key and let
        # repair_tool_args close braces and fix literal newlines.
        if not calls:
            # Find candidate start positions: every { that begins what
            # looks like a tool-call JSON object.
            _name_hint = re.compile(r'\{\s*"(?:name|function)"', re.IGNORECASE)
            for m in _name_hint.finditer(text):
                tail = text[m.start():]
                # Escape literal newlines inside JSON string values so
                # json.loads has a chance.  We do this by finding runs
                # inside "..." and replacing \n → \\n.
                def _escape_literal_newlines(s: str) -> str:
                    out = []
                    in_str = False
                    esc = False
                    for ch in s:
                        if in_str:
                            if esc:
                                esc = False
                                out.append(ch)
                            elif ch == '\\':
                                esc = True
                                out.append(ch)
                            elif ch == '"':
                                in_str = False
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
                                in_str = True
                            out.append(ch)
                    return "".join(out)

                escaped = _escape_literal_newlines(tail)
                parsed = repair_tool_args(escaped)
                if not isinstance(parsed, dict):
                    continue
                nm = parsed.get("name") or parsed.get("tool") or parsed.get("function")
                if not nm:
                    continue
                if "arguments" not in parsed and "parameters" not in parsed and "parameter" not in parsed:
                    continue
                if "parameters" in parsed and "arguments" not in parsed:
                    parsed["arguments"] = parsed.pop("parameters")
                elif "parameter" in parsed and "arguments" not in parsed:
                    parsed["arguments"] = parsed.pop("parameter")
                _add(parsed)
                break  # one tool call is enough for the truncated case

    return calls
