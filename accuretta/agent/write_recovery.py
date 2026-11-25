# SPDX-License-Identifier: AGPL-3.0-only
"""Recover file content from a broken write_file call (escaped JSON or HTML in the arguments).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _maybe_unescape_json_html(html: str) -> str:
    """Some local fine-tunes (especially Qwen3.6-Claude-distilled and other
    JSON-trained chats) emit HTML inside their ```html fence already escaped
    as a JSON string literal: real newlines become the two characters `\\n`,
    real quotes become `\\"`, and so on. The browser then renders those
    backslash-letter pairs as visible text in the preview iframe instead of
    treating them as line breaks — the page comes out as one giant unstyled
    blob with `\\n` peppered through it.
    Heuristic: lots of `\\n` sequences, very few real newlines → it's encoded.
    Decode by reversing the standard JSON string escapes (handle `\\\\` first
    via a sentinel so we don't double-process backslashes)."""
    if not html or "\\" not in html:
        return html
    real_newlines = html.count("\n")
    escaped_n = html.count("\\n")
    escaped_quote = html.count('\\"')
    # Need clear evidence of encoding: many escape sequences AND few real
    # newlines. A normal HTML file has dozens of real newlines, so even one
    # `\\n` next to fifty real ones is just incidental (e.g. inline JS).
    if escaped_n < 3 and escaped_quote < 3:
        return html
    if real_newlines >= max(5, escaped_n // 2):
        return html
    sentinel = "\x00BS\x00"
    decoded = (
        html
        .replace("\\\\", sentinel)
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\/", "/")
        .replace(sentinel, "\\")
    )
    return decoded


def _extract_write_file_content(text: str) -> str | None:
    """Linear scan for `"content":"<JSON-string>"` inside a write_file tool
    call. Returns the JSON-string body (still escape-encoded) or None.
    Tolerates a missing closing quote (truncated streams). No regex, so no
    catastrophic backtracking on multi-KB bodies."""
    if not text:
        return None
    n = len(text)
    # Find the first occurrence of `"content"` that follows a `write_file`
    # mention — guards against unrelated keys named "content" elsewhere.
    wf = text.find("write_file")
    if wf == -1:
        return None
    key = text.find('"content"', wf)
    if key == -1:
        return None
    # Skip whitespace, expect `:`, more whitespace, then opening `"`.
    j = key + len('"content"')
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n or text[j] != ":":
        return None
    j += 1
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n or text[j] != '"':
        return None
    j += 1
    body_start = j
    # Walk until unescaped `"` or end-of-text.
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2  # skip the escape and the escaped char
            continue
        if c == '"':
            return text[body_start:j]
        j += 1
    # Truncated — no closing quote found. Trim a trailing `"}}</tool_call>`
    # tail if present so the caller doesn't get JSON debris glued on.
    tail = text[body_start:]
    # Strip the most common truncation suffixes.
    for suffix in ('"}}</tool_call>', '"}}', '"}'):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail or None
