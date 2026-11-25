# SPDX-License-Identifier: AGPL-3.0-only
"""What the user actually sees of an assistant reply (thinking markers stripped), and
the code blocks in it that the file tools may write as a source.

Moved out of accuretta/agent/visible_text.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats


_CHAT_CODE_FENCE_RE = re.compile(r"```([^\r\n`]*)\r?\n([\s\S]*?)```", re.MULTILINE)


def _visible_assistant_text(text: str) -> str:
    """Mirror the UI reasoning boundary for exports and completion checks."""
    # Legacy marker-form reasoning is closed first, so an answer line such as
    # `ResponseEntity<T> ok()` is never mistaken for the reasoning boundary.
    raw = _close_think_marker(str(text or ""))
    close_re = re.compile(
        r"</(?:think|thinking|reasoning)>|<\|/thinking\|>|"
        r"\[/(?:thought|thinking|reasoning|scratchpad)\]",
        re.IGNORECASE,
    )
    closes = list(close_re.finditer(raw))
    if closes:
        visible = raw[closes[-1].end():]
    else:
        open_re = re.compile(
            r"<(?:think|thinking|reasoning)>|<\|thinking\|>|"
            r"\[(?:thought|thinking|reasoning|scratchpad)\]",
            re.IGNORECASE,
        )
        opener = open_re.search(raw)
        if not opener:
            visible = raw
        else:
            visible = raw[:opener.start()]
            thinking = raw[opener.start():]
            implicit = re.search(
                r"</?tool_call>|<\|tool_call>|<call:[a-zA-Z0-9_\-]+>|"
                r"\[TOOL_CALLS\]|```(?:tool_call|tool_code)",
                thinking, re.IGNORECASE,
            )
            if implicit and implicit.start() > 0:
                visible += thinking[implicit.start():]
            else:
                response = re.search(r"\n\s*response(?:[ \t]*\r?\n)?", thinking,
                                     re.IGNORECASE)
                if response and response.start() > 0:
                    visible += thinking[response.end():]
    return re.sub(
        r"</?(?:think|thinking|reasoning)>|<\|/?thinking\|>|"
        r"\[/?(?:thought|thinking|reasoning|scratchpad)\]",
        "", visible, flags=re.IGNORECASE,
    )


# Assistant messages of the turn in progress, published by run_chat_turn before
# it runs a round's tools, so write_file(source='visible_code_block') can copy a
# fence from the current turn, including the reply that makes the call.
_live_turn_messages: dict[str, list[dict]] = {}


def _recent_visible_code_blocks(chat_id: str, language: str = "") -> list[dict]:
    wanted = str(language or "").strip().lower()
    chat = (get_chats().get("chats", {}) or {}).get(chat_id) or {}
    saved = [(i, m) for i, m in enumerate(chat.get("messages") or [])
             if not m.get("_internal")]
    live = [(len(chat.get("messages") or []) + i, m)
            for i, m in enumerate(_live_turn_messages.get(chat_id) or [])]
    blocks: list[dict] = []
    for message_index, message in saved + live:
        if (message.get("role") != "assistant"
                or not isinstance(message.get("content"), str)):
            continue
        visible = _visible_assistant_text(message.get("content") or "")
        for match in _CHAT_CODE_FENCE_RE.finditer(visible):
            info = (match.group(1) or "").strip()
            lang = (info.split(None, 1)[0] if info else "text").lower()
            if lang in {"tool_code", "tool_call"}:
                continue
            if wanted and lang != wanted:
                continue
            blocks.append({
                "content": match.group(2), "language": lang,
                "info": info, "message_index": message_index,
                "live": message_index >= len(chat.get("messages") or []),
            })
    return blocks


# Fence languages and file extensions that name the same kind of code. A block is
# refused only when both sides are known here and differ.
_CODE_FAMILIES = {
    "html": ("html", "htm", "xhtml"),
    "css": ("css",),
    "javascript": ("js", "javascript", "mjs", "cjs", "jsx", "ts", "typescript", "tsx"),
    "python": ("py", "python", "python3", "pyw"),
    "powershell": ("ps1", "psm1", "powershell", "pwsh"),
    "shell": ("sh", "bash", "zsh"),
    "batch": ("bat", "cmd", "batch"),
    "json": ("json", "jsonc"),
    "markdown": ("md", "markdown"),
    "java": ("java",),
    "csharp": ("cs", "csharp"),
    "go": ("go", "golang"),
    "rust": ("rs", "rust"),
    "ruby": ("rb", "ruby"),
    "sql": ("sql",),
    "yaml": ("yaml", "yml"),
    "c/c++": ("c", "h", "cpp", "cc", "cxx", "hpp", "c++"),
}


_CODE_FAMILY_OF = {name: family for family, names in _CODE_FAMILIES.items() for name in names}


def _code_only_in_thinking(chat_id: str) -> bool:
    """The reply that makes the call has a code fence in its thinking and none in view."""
    live = _live_turn_messages.get(chat_id) or []
    content = str(live[-1].get("content") or "") if live else ""
    thinking, _ = _split_leading_think(_close_think_marker(content))
    if "```" not in thinking:
        return False
    for match in _CHAT_CODE_FENCE_RE.finditer(_visible_assistant_text(content)):
        info = (match.group(1) or "").strip()
        if (info.split(None, 1)[0].lower() if info else "text") not in {"tool_code", "tool_call"}:
            return False
    return True


def _visible_code_source(args: dict) -> tuple[dict, str]:
    """Resolve a prior visible code fence selected by a write tool call."""
    if str(args.get("source") or "").strip().lower() != "visible_code_block":
        return {}, "source must be 'visible_code_block'. No file was written."
    try:
        index = max(0, min(int(args.get("index") or 0), 50))
    except (ValueError, TypeError) as exc:
        return {}, str(exc)
    chat_id = _get_current_chat() or ""
    blocks = _recent_visible_code_blocks(chat_id, str(args.get("language") or ""))
    write_it = "Pass the file text in content instead."
    if index >= len(blocks):
        return {}, (
            "No matching visible code block exists in this chat. "
            f"Found {len(blocks)} matching blocks. {write_it}"
        )
    block = blocks[-1 - index]
    name = os.path.basename(str(args.get("path") or ""))
    want = _CODE_FAMILY_OF.get(os.path.splitext(name)[1].lstrip(".").lower())
    have = _CODE_FAMILY_OF.get(block["language"])
    if want and have and want != have:
        return {}, (f"refused: the newest visible code block is {block['language']}, but {name} "
                    f"needs {want} code. No file was written. {write_it}")
    if not block["live"] and _code_only_in_thinking(chat_id):
        return {}, ("refused: this reply has its code only in your thinking, which is not visible "
                    "and is not kept. The newest visible code block is from an earlier message, so "
                    f"no file was written. {write_it}")
    return block, ""


# Chats saved before the stream loop closed reasoning with </think> hold
# "<think>R\n responseA" (a bare " response" boundary), and a model that copied
# that marker into its reasoning lands as "<think>R\n responseA</think>".
# _PRIOR_THINK_RE needs a real closer, so this legacy form is normalized first;
# otherwise the model sees and copies the marker.
_THINK_MARKER_RE = re.compile(
    r"\n[ \t]*response(?=[A-Za-z0-9<`])(?![sS](?:\b|$))|\n[ \t]*response[ \t]*$")


def _close_think_marker(text: str) -> str:
    """Rewrite "<think>R\\n responseA[</think>]" as "<think>R</think>A"."""
    if not text:
        return text
    low = text.lower()
    start = low.find("<think>")
    if start < 0:
        return text
    body_at = start + len("<think>")
    close = low.find("</think>", body_at)
    # A closer with an answer after it is already a proper block (and is what
    # this returns, so a second pass is a no-op). Only the trailing closer the
    # stream loop appends after reasoning-channel text is rewritten.
    if close >= 0 and text[close + len("</think>"):].strip():
        return text
    region = text[body_at:close] if close >= 0 else text[body_at:]
    marks = list(_THINK_MARKER_RE.finditer(region))
    if not marks:
        return text
    m = marks[-1]
    after = text[close + len("</think>"):] if close >= 0 else ""
    return (text[:body_at] + region[:m.start()] + "</think>"
            + region[m.end():] + after)


# Models without a reasoning channel open their content with an inline <think>
# block; an unclosed one is all reasoning.
_LEADING_THINK_RE = re.compile(r"^\s*<think>(?:[\s\S]*?</think>|[\s\S]*$)", re.IGNORECASE)


def _split_leading_think(text: str) -> tuple[str, str]:
    """Separate an inline <think> block that opens the content from the answer."""
    match = _LEADING_THINK_RE.match(text or "")
    if not match:
        return "", text or ""
    return match.group(0), text[match.end():]
