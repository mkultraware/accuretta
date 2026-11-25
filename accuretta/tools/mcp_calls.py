# SPDX-License-Identifier: AGPL-3.0-only
"""Calling MCP tools from the agent: interactive browser verbs only when the user asked,
page text compressed, and refusals explained.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
from accuretta.core.state import STATE
from accuretta.redteam.network_scope import _rt_scope_mcp_calls
from accuretta.redteam.osint import _shodan_refusal
from accuretta.security.approvals import request_approval
from accuretta.security.session_allow import _approval_session_allow
from accuretta.security.web_taint import _mark_web_taint


_BROWSER_INTENT_RE = re.compile(
    r"\b(click|fill|type|press|select|check|submit|login|log\s+in|sign\s+in|"
    r"signin|register|complete|captcha|verify|button|form|scroll|hover|upload|"
    r"download|input|toggle|agree|accept)\b",
    re.IGNORECASE,
)


def _user_asked_browser_interaction() -> bool:
    return bool(_BROWSER_INTENT_RE.search(STATE.last_user_text or ""))


# Playwright MCP verbs that MUTATE page state (as opposed to reads like
# snapshot/screenshot/navigate). Interactive verbs are the ClickFix surface.
_INTERACTIVE_MCP_RE = re.compile(
    r"\b(click|dblclick|fill|type|press|check|uncheck|select_option|upload_file|"
    r"drag_and_drop|hover|scroll_into_view|submit|keyboard|mouse|download|"
    r"browser_evaluate|evaluate)\b",
    re.IGNORECASE,
)


# MCP tools whose output is untrusted PAGE content (browser/server reads).
_MCP_BROWSER_TOOL_RE = re.compile(
    r"(playwright|browser|page)", re.IGNORECASE,
)


# ---- MCP result shaping ------------------------------------------------------
# Playwright-style aria snapshots are YAML-ish trees where 40-60% of lines are
# anonymous layout containers (`- generic [ref=e12]:`) with nothing
# actionable. They cost real tokens (a big snapshot is 20-60 KB) and actions
# target named/interactive elements anyway, so the noise lines are dropped.
_ARIA_NOISE_RE = re.compile(
    r"^\s*- generic(?: \[[^\]]*\])?:\s*$", re.IGNORECASE)


def _compress_aria_text(text: str) -> str:
    """Shrink playwright-mcp snapshot/page-state text. Only lines that are
    PURE structural noise are removed (anonymous `generic` containers whose
    attributes carry nothing but a ref/active marker — never a name, role
    info, or cursor=pointer). Indentation of children is left untouched; the
    model reads these trees loosely, and every actionable element keeps its
    ref. Non-snapshot text is returned unchanged."""
    if "- generic" not in text and "Page Snapshot" not in text:
        return text
    out_lines: list[str] = []
    dropped = 0
    for ln in text.split("\n"):
        if _ARIA_NOISE_RE.match(ln):
            # keep generics that are named or clickable: they carry a quoted
            # name or cursor=pointer inside the brackets
            if '"' in ln or "cursor=pointer" in ln or "pressed" in ln or "expanded" in ln:
                out_lines.append(ln)
            else:
                dropped += 1
        else:
            out_lines.append(ln)
    if dropped:
        out_lines.append(f"[{dropped} anonymous layout containers omitted — page structure unchanged]")
    return "\n".join(out_lines)


# Third-party MCP servers (osint-mcp-server and friends) pass a Shodan refusal
# on as a bare status ("Shodan search failed: 403 Forbidden"). The result
# gets a line saying what that status means.
_MCP_SHODAN_STATUS_RE = re.compile(
    r"\bshodan\b[^\n]{0,80}?\b(?:failed|error|denied|refused|forbidden|unauthorized)\b[^\n]{0,40}?\b(401|403|429)\b",
    re.IGNORECASE)


def _mcp_explain_refusal(text: str) -> str:
    m = _MCP_SHODAN_STATUS_RE.search(text or "")
    if not m:
        return text
    return f"{text}\n[Accuretta: {_shodan_refusal(int(m.group(1)))}]"


def _mcp_make_tool_callable(mcp_client, name, desc, kind):
    def wrapper(args: dict):
        # Mission-scoped URL filter. MCP connectors are third-party and opaque, so
        # with an active mission the URL-bearing arguments go through the same
        # allowlist gate as rt_browser before the call leaves the process. Outside
        # missions the approval flow below is the only gate.
        scope_reason = _rt_scope_mcp_calls(name, args)
        if scope_reason:
            return {"error": scope_reason}
        # ClickFix click gate: interactive browser verbs (click, fill, type, ...)
        # run freely only when the user's own message asked for page interaction or
        # the user session-remembered this tool. A page-driven "click the download
        # button" goes through an approval card even in soft mode.
        gated = False
        if (_INTERACTIVE_MCP_RE.search(name)
                and kind not in _approval_session_allow
                and not _user_asked_browser_interaction()):
            gated = True
            appr = request_approval(
                title=f"MCP: {name} — interactive action NOT requested by user",
                command=f"Execute {name} via {mcp_client.name}",
                details={"kind": kind, "force_prompt": True,
                         "clickfix_gate": True, "Arguments": args},
            )
            if appr.get("decision") != "approve":
                return {"error": "User denied action.", "reason": appr.get("reason", "")}
        # Every MCP tool goes through approval. `kind` is the stable prefixed tool
        # name, so "remember for this session" auto-approves this tool's repeats.
        if not gated:
            appr = request_approval(
                title=f"MCP: {name}",
                command=f"Execute {name} via {mcp_client.name}",
                details={"kind": kind, "Arguments": args}
            )
            if appr.get("decision") != "approve":
                return {"error": "User denied action.", "reason": appr.get("reason", "")}

        response = mcp_client.call_tool(name, args)
        if "error" in response:
            err = response["error"]
            if isinstance(err, dict):
                err = err.get("message") or json.dumps(err)[:500]
            return {"error": _mcp_explain_refusal(str(err))}

        content = response.get("result", {}).get("content", [])
        texts: list[str] = []
        n_images = 0
        for item in content:
            if item.get("type") == "text":
                texts.append(item.get("text", ""))
            elif item.get("type") == "image":
                n_images += 1
        out_text = "\n".join(t for t in texts if t)
        if n_images:
            # Screenshots can't be seen without a vision projector (mmproj), and loading
            # one just for browser automation shouldn't be required; steer the model to
            # the text snapshot instead.
            out_text += (f"\n[{n_images} image(s) omitted — no vision projector is loaded, "
                         f"so screenshots can't be seen. Use the page's text snapshot "
                         f"(browser_snapshot) to read structure; it needs no vision.]")
        # Strip anonymous layout-container noise from aria snapshots so a big page
        # doesn't flood the context.
        out_text = _compress_aria_text(out_text)
        out_text = _mcp_explain_refusal(out_text)
        # Untrusted page content entered the context: remember it so
        # actions copied from it force approval, and frame browser
        # output as [web_content] data.
        _mark_web_taint(out_text)
        if _MCP_BROWSER_TOOL_RE.search(name):
            out_text = f"[web_content tool={name}]\n{out_text}\n[/web_content]"
        # Return a dict, not a bare string: "text" is a known bulk field, so an
        # oversized result gets line-aware head/tail elision instead of the opaque
        # "too large" envelope.
        return {"text": out_text, "_mcp_tool": name}
    return wrapper
