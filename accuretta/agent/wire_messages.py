# SPDX-License-Identifier: AGPL-3.0-only
"""Messages as llama-server gets them: reasoning kept or dropped per the chat template,
the reasoning field split out, and the verification capability of the template.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.agent.context_estimate import _TOOL_RESULT_KEEP_RECENT
from accuretta.agent.visible_text import _PRIOR_THINK_RE
from accuretta.agent.visible_text import _prior_thinking_mode
from accuretta.agent.visible_text import _thinking_wire_view
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_process import _llama_props_data
from pathlib import Path


def _preserve_prior_thinking(text: str) -> str:
    if not text or "<think>" not in text.lower():
        return text
    def _rewrite(m: "re.Match[str]") -> str:
        body = (m.group(1) or "").strip()
        if not body:
            return ""
        # plain text the chat template can't recognize as reasoning, but the
        # model can still read. compact-ish to keep ctx in check.
        return f"[scratchpad-from-earlier-turn]\n{body}\n[/scratchpad-from-earlier-turn]"
    return _PRIOR_THINK_RE.sub(_rewrite, text)


_REASONING_FIELD_CACHE: dict[tuple, bool] = {}


def _template_reads_reasoning_content(model_path: str) -> bool:
    """True when the model's chat template renders `message.reasoning_content`
    (Qwen3.5+, DeepSeek and most current reasoning templates). Those templates
    never look for <think> inside `content`: they print their own think block
    from the field, so reasoning sent inline came out doubled, an empty
    <think></think> followed by the real one."""
    if not model_path:
        return False
    try:
        st = Path(model_path).stat()
        key = (model_path, int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        key = (model_path, 0, 0)
    if key in _REASONING_FIELD_CACHE:
        return _REASONING_FIELD_CACHE[key]
    texts: list[str] = []
    try:
        keys = (read_gguf_metadata(model_path).get("keys") or {})
        texts += [str(v) for k, v in keys.items()
                  if "chat_template" in str(k).lower() and isinstance(v, str)]
    except Exception:
        pass
    if not texts:
        # Only ask the server about the model it is actually running.
        try:
            if _llama.loaded_model() == model_path:
                tpl = _llama_props_data().get("chat_template")
                if isinstance(tpl, str):
                    texts.append(tpl)
        except Exception:
            pass
    found = "reasoning_content" in "\n".join(texts)
    _REASONING_FIELD_CACHE[key] = found
    return found


def _split_reasoning_field(content: str) -> tuple[str, str] | None:
    """Split a leading <think>R</think>A into (R, A), or None."""
    if not isinstance(content, str):
        return None
    head = content.lstrip()
    if not head.lower().startswith("<think>"):
        return None
    close = head.lower().find("</think>")
    if close < 0:
        return None
    return head[len("<think>"):close].strip(), head[close + len("</think>"):].strip()


def _sanitize_messages_for_openai(msgs: list[dict],
                                  stub_old_tools: bool = True) -> list[dict]:
    """Sanitize messages for llama-server's OpenAI endpoint: strip local-only
    fields (`t`, `_stats`), coerce tool messages to
    `{role:'tool', tool_call_id, content}`, and give assistant tool_calls a
    string `arguments`. With `stub_old_tools`, tool messages older than the last
    _TOOL_RESULT_KEEP_RECENT become compact stubs (stored chat JSON is
    untouched); rolling-summary mode passes false so evidence stays live until a
    fold. The stub path is the fallback when compaction is off."""
    thinking_mode = _prior_thinking_mode()
    # Reasoning goes out in its own field when the template reads one, so the
    # model sees each earlier turn exactly as it generated it, and the prompt
    # extends the cached sequence token for token. Native mode (Nanbeige) keeps
    # its verbatim inline form, which its template expects.
    try:
        reasoning_field = (thinking_mode != "native"
                           and _template_reads_reasoning_content(
                               str(get_settings().get("model_path") or "")))
    except Exception:
        reasoning_field = False
    out = []
    # the LAST assistant message is "current" reasoning being authored — we
    # only rewrite think blocks for messages strictly older than the latest.
    if thinking_mode == "strip":
        msgs = _thinking_wire_view(msgs)
    last_assistant_idx = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            last_assistant_idx = i
    # tool_call_id → tool name from assistant tool_calls, so stubbed results
    # can still name the tool that produced them even when the tool message
    # itself carries no `name` field.
    tc_names: dict = {}
    for m in msgs:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn_name = (tc.get("function") or {}).get("name")
            if tc.get("id") and fn_name:
                tc_names[tc["id"]] = fn_name
    # Tool messages at or past this index keep their full content; anything
    # older gets stubbed. Position-from-end only → deterministic per turn.
    stub_cutoff = len(msgs) - _TOOL_RESULT_KEEP_RECENT
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        if role == "system" and out:
            # Strict chat templates (Hermes-3 lineage) hard-error on any non-first system
            # message, so re-role those as `user`, which every template accepts.
            role = "user"
        clean: dict = {"role": role}
        content = m.get("content", "")
        if isinstance(content, list):
            clean["content"] = content
        else:
            text_content = content or ""
            if (thinking_mode == "scratchpad" and role == "assistant"
                    and i < last_assistant_idx and isinstance(text_content, str)):
                text_content = _preserve_prior_thinking(text_content)
            split = (_split_reasoning_field(text_content)
                     if reasoning_field and role == "assistant" else None)
            if split is not None:
                clean["reasoning_content"], text_content = split
            clean["content"] = text_content
        if role == "tool":
            clean["tool_call_id"] = m.get("tool_call_id") or m.get("name") or "tool"
            if m.get("name"):
                clean["name"] = m["name"]
            if stub_old_tools and i < stub_cutoff and isinstance(clean["content"], str):
                tool_name = m.get("name") or tc_names.get(m.get("tool_call_id") or "")
                if tool_name:
                    clean["content"] = (f"[output elided — {tool_name} result from earlier in the "
                                        f"session; call the tool again if you need it]")
                else:
                    clean["content"] = ("[output elided — tool result from earlier in the "
                                        "session; call the tool again if you need it]")
        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tcs.append({
                    "id": tc.get("id") or f"call_{len(tcs)}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args or "{}"},
                })
            if tcs:
                clean["tool_calls"] = tcs
        out.append(clean)
    return out


_VERIFY_CAP_CACHE: dict[tuple, bool] = {}


def _template_supports_verification(model_path: str, props: dict | None = None) -> bool:
    """True when the chat template takes Sage-A1.5's `verification` kwarg.

    The template raises on verification=true without a non-empty system
    message, so callers only send it when the first message is a system one."""
    model_path = str(model_path or "").strip()
    if not model_path:
        return False
    try:
        st = Path(model_path).stat()
        key = (model_path, int(st.st_mtime_ns), int(st.st_size))
    except Exception:
        key = (model_path, 0, 0)
    if key in _VERIFY_CAP_CACHE:
        return _VERIFY_CAP_CACHE[key]
    texts: list[str] = []
    try:
        keys = (read_gguf_metadata(model_path).get("keys") or {})
        texts += [str(v) for k, v in keys.items()
                  if "chat_template" in str(k).lower() and isinstance(v, str)]
    except Exception:
        pass
    props = props if isinstance(props, dict) else _llama_props_data()
    tpl = props.get("chat_template") if isinstance(props, dict) else None
    if isinstance(tpl, str):
        texts.append(tpl)
    signal = "\n".join(texts)
    found = bool(re.search(r"verification\s+is\s+defined", signal))
    _VERIFY_CAP_CACHE[key] = found
    return found


def _verification_wanted(settings: dict, effort: str, model_path: str) -> bool:
    mode = str(settings.get("verification_mode") or "auto").strip().lower()
    if mode == "off" or not _template_supports_verification(model_path):
        return False
    return mode == "on" or effort == "high"
