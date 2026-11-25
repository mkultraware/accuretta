# SPDX-License-Identifier: AGPL-3.0-only
"""Finding the HTML page in a reply, for the preview pane and saved versions.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.agent.write_recovery import _extract_write_file_content
from accuretta.agent.write_recovery import _maybe_unescape_json_html


# Any fenced code block with an optional language hint; the first one whose
# contents look like HTML wins, since some models emit bare ``` fences.
HTML_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\s*\n([\s\S]*?)```", re.MULTILINE)


def extract_html(text: str) -> str | None:
    """Extract a complete HTML document from a model response. In order:
      1. a fenced block tagged ```html
      2. any fenced block whose contents start with <!DOCTYPE / <html
      3. the whole response if it starts with <!DOCTYPE / <html
      4. a write_file tool_call wrapping the HTML (Qwen3 / DeepSeek distills do
         this even in IDE mode): use `arguments.content`
      5. the substring from <!DOCTYPE / <html to </html>
    Lenient on purpose: a false positive previews the wrong block, a false
    negative shows no preview at all. Finally undoes accidental JSON-string
    escaping."""
    if not text:
        return None
    # 1 + 2: walk fenced blocks, prefer html-tagged, then any html-looking content.
    html_tagged = None
    html_untagged = None
    for m in HTML_BLOCK_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        body = m.group(2).strip()
        bl = body.lower()
        if lang in ("html", "htm", "xhtml") and "<" in body and ">" in body:
            html_tagged = body
            break  # explicit html tag wins immediately
        if bl.startswith("<!doctype") or bl.startswith("<html"):
            if html_untagged is None:
                html_untagged = body
    if html_tagged:
        return _maybe_unescape_json_html(html_tagged)
    if html_untagged:
        return _maybe_unescape_json_html(html_untagged)
    # 3: bare HTML response, no fence
    stripped = text.strip()
    sl = stripped.lower()
    if sl.startswith("<!doctype") or sl.startswith("<html"):
        return _maybe_unescape_json_html(stripped)
    # 4: write_file tool_call in IDE mode, e.g.
    #     <tool_call>{"name":"write_file","arguments":{"path":"...",
    #                 "content":"<!DOCTYPE html>\\n..."}}</tool_call>
    # (sometimes unclosed). Pull the content arg out with a linear scan: a regex
    # like `(?:\\.|[^"\\])*` inside `[\s\S]*?` backtracks catastrophically on
    # 30KB+ bodies. The final unescape pass restores \\n / \\".
    if 'write_file' in text and '"content"' in text:
        body = _extract_write_file_content(text)
        if body and "<" in body and ">" in body:
            bl = body.lower()
            if "<!doctype" in bl or "<html" in bl:
                return _maybe_unescape_json_html(body)
    # 5: HTML embedded in prose — find first doctype/<html and last </html>
    lower = text.lower()
    starts = [i for i in (lower.find("<!doctype"), lower.find("<html")) if i >= 0]
    if starts:
        start = min(starts)
        end = lower.rfind("</html>")
        if end > start:
            return _maybe_unescape_json_html(text[start:end + len("</html>")].strip())
    return None
