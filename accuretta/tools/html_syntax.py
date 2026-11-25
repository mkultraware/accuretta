# SPDX-License-Identifier: AGPL-3.0-only
"""A tolerant HTML structure check: unclosed and mismatched tags, allowing void elements
and optional closing tags.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


# HTML void elements never take a closing tag — they must not be tracked on
# the nesting stack or we'd report every <br>/<img> as "unclosed".
_HTML_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
))


# Elements whose closing tag is OPTIONAL per the HTML spec — browsers auto-close
# them, so a missing </p> or </li> is valid, not a bug. We never flag these as
# unclosed, otherwise the model "fixes" markup that was already correct.
_HTML_OPTIONAL_CLOSE = frozenset((
    "html", "head", "body", "p", "li", "dt", "dd", "option", "optgroup",
    "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "caption",
    "rp", "rt", "menuitem",
))


def _check_html_syntax(file_path: str) -> dict:
    """Structural sanity check for HTML using only the stdlib HTMLParser.
    HTML is lenient by design, so we deliberately report ONLY high-confidence
    breakage — unclosed <script>/<style>, explicit close tags with no matching
    open, and tags left open at EOF — while ignoring void and optional-close
    elements. This avoids false positives that would push the model into
    "fixing" valid markup."""
    from html.parser import HTMLParser

    problems: list[str] = []
    stack: list[tuple[str, int]] = []  # (tagname, lineno)

    class _Checker(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag in _HTML_VOID_TAGS:
                return
            stack.append((tag, self.getpos()[0]))

        def handle_startendtag(self, tag, attrs):
            pass  # self-closing (<br/>) — nothing to track

        def handle_endtag(self, tag):
            if tag in _HTML_VOID_TAGS:
                return
            # Find the nearest matching open tag. Tags left dangling between it
            # and the close are implicitly closed by the browser; that's fine
            # for optional-close elements (an open <li> when </ul> arrives) but
            # a dangling non-optional element (e.g. an open <div> when </body>
            # arrives) is a real missing-close-tag bug, so we flag those.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    for dtag, dline in stack[i + 1:]:
                        if dtag not in _HTML_OPTIONAL_CLOSE:
                            problems.append(f"line {dline}: unclosed <{dtag}> — missing </{dtag}> (implicitly closed by </{tag}>)")
                    del stack[i:]
                    return
            # No matching open tag anywhere — a real stray close tag, unless
            # it's an optional-close element the parser can safely ignore.
            if tag not in _HTML_OPTIONAL_CLOSE:
                problems.append(f"line {self.getpos()[0]}: stray </{tag}> with no matching open tag")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        return {"error": f"read failed: {e}"}

    parser = _Checker(convert_charrefs=True)
    try:
        parser.feed(src)
        parser.close()
    except Exception as e:
        # HTMLParser rarely raises, but malformed declarations can — surface it.
        return {"error": f"HTML parse error: {e}"}

    # Anything left open that REQUIRES closing is a genuine structural error.
    # <script>/<style> are called out by name because an unclosed one swallows
    # the rest of the document and is the most common real HTML break.
    for tag, lineno in stack:
        if tag in _HTML_OPTIONAL_CLOSE:
            continue
        if tag in ("script", "style"):
            problems.append(f"line {lineno}: unclosed <{tag}> — missing </{tag}> (this hides everything after it)")
        else:
            problems.append(f"line {lineno}: unclosed <{tag}> — missing </{tag}>")

    if problems:
        return {"error": "HTML structural errors:\n" + "\n".join(problems)}
    return {"result": "Syntax OK (HTML is lenient — structural check only: tag nesting, "
                      "unclosed script/style. Browser-tolerated quirks are not flagged.)"}
