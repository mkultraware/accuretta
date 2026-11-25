# SPDX-License-Identifier: AGPL-3.0-only
"""The web guard: page text never gives orders. Remembers what pages said and which
files came from them, and flags an action that copies a command from a page, downloads
and runs something, or runs a file a page wrote.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import time
from accuretta.security.web_taint import _WEB_TAINT_WINDOW_S
from accuretta.security.web_taint import _norm_web
from accuretta.security.web_taint import _recent_web_taint
from accuretta.security.web_taint import _web_taint_lock
from accuretta.security.web_taint import _web_texts


_SOFT_COMMAND_KINDS = {"powershell", "sandbox", "session", "git", "test"}


_web_derived_paths: dict[str, float] = {}         # files written with page content


def _recent_web_texts() -> list[str]:
    cutoff = time.time() - _WEB_TAINT_WINDOW_S
    with _web_taint_lock:
        return [s for t, s in _web_texts if t >= cutoff]


def _web_copied(text: str, min_len: int = 6) -> bool:
    """True when `text` (a command, typed text, file content) repeats text
    recently read from the web: the whole of a short one, or any 32-character
    stretch of a long one."""
    t = _norm_web(text)
    if len(t) < min_len:
        return False
    pages = _recent_web_texts()
    if not pages:
        return False
    if len(t) <= 40:
        return any(t in page for page in pages)
    step = max(8, (len(t) - 32) // 200)       # at most ~200 probes for a big file
    return any(t[i:i + 32] in page for i in range(0, len(t) - 31, step) for page in pages)


# A command that fetches something and runs it, the core of every ClickFix lure.
_DOWNLOAD_RUN_RE = re.compile(
    r"(?:\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget|start-bitstransfer|bitsadmin)\b"
    r"|downloadstring|downloadfile|certutil[^\n]*-urlcache)"
    r"[^\n]*(?:\|\s*(?:iex|invoke-expression|sh|bash|zsh|python3?|powershell|pwsh|cmd)\b"
    r"|;\s*(?:start-process|iex|invoke-expression|&)"
    r"|\.(?:exe|msi|ps1|bat|cmd|vbs|js|hta|scr)\b)"
    r"|\bmshta(?:\.exe)?\s|\bregsvr32\b[^\n]*http|\brundll32\b[^\n]*http",
    re.IGNORECASE)


def _web_guard_reason(kind: str | None, command: str, details: dict | None = None) -> str | None:
    """Why this action must ask because of web content, or None."""
    details = details or {}
    if not _recent_web_taint():
        return None
    cmd = str(command or "")
    if kind in _SOFT_COMMAND_KINDS:
        if _DOWNLOAD_RUN_RE.search(cmd):
            return "downloads something and runs it, after reading a web page"
        if _web_copied(cmd):
            return "this command comes from a web page"
        low = os.path.normcase(cmd)
        with _web_taint_lock:
            derived = [p for p, t in _web_derived_paths.items()
                       if time.time() - t < _WEB_TAINT_WINDOW_S]
        if any(os.path.basename(p) in low for p in derived):
            return "runs a file whose content came from a web page"
        return None
    if _is_desktop_approval_kind(kind):
        if kind == "desktop.keys" and re.search(r"\bwin(?:dows)?\s*\+\s*[rx]\b", cmd, re.IGNORECASE):
            return "opens the Windows Run box, after reading a web page"
        text = {
            "desktop.type": str(details.get("text") or cmd.removeprefix("type: ")),
            "desktop.set_value": cmd.rsplit(" to ", 1)[-1],
            "desktop.launch": str(details.get("target") or cmd),
        }.get(kind, "")
        if text and _web_copied(text):
            return "this text comes from a web page"
    return None


def _is_desktop_approval_kind(kind: str) -> bool:
    return str(kind or "").startswith(("desktop_", "desktop."))
