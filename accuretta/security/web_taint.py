# SPDX-License-Identifier: AGPL-3.0-only
"""Web taint: remembering recently read page text for a while, so an action that comes
from page text can be told apart from the agent's own.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import threading
import time


# ---- web-taint tracking ----------------------------------------------------
# ClickFix for LLMs: page content (web_fetch, browser snapshots) can carry hidden
# instructions steering the agent into an install or shell command. The guard
# remembers WHAT untrusted text was read, and forces an approval prompt (past
# soft-mode auto-approve, /goal and session remembers) only when an action comes
# from that text: a command or typed text copied from a page, a download-and-run,
# a command running a file whose content came from a page, or the Run box.
#
# It used to force a prompt for every command, write and desktop action for
# three minutes after any page was read. That stalled long /goal runs after a
# web search, and the agent's own commands (run the tests, edit its file) are
# not the attack. Owner's decision, 2026-11-20. Destructive actions still ask
# under the normal Soft and /goal rules.
_WEB_TAINT_WINDOW_S = 1800.0


_WEB_TEXT_KEEP_CHARS = 600_000


_web_taint_at: float = 0.0


_web_texts: list[tuple[float, str]] = []          # (when, normalized text) read from the web


_web_taint_lock = threading.Lock()


def _norm_web(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _mark_web_taint(text: str = "") -> None:
    """Record that untrusted web content entered the context, and what it said."""
    global _web_taint_at
    now = time.time()
    norm = _norm_web(text)[:200_000]
    with _web_taint_lock:
        _web_taint_at = now
        if norm:
            _web_texts.append((now, norm))
        cutoff = now - _WEB_TAINT_WINDOW_S
        _web_texts[:] = [(t, s) for t, s in _web_texts if t >= cutoff]
        while sum(len(s) for _, s in _web_texts) > _WEB_TEXT_KEEP_CHARS and len(_web_texts) > 1:
            _web_texts.pop(0)


def _recent_web_taint() -> bool:
    with _web_taint_lock:
        return (time.time() - _web_taint_at) < _WEB_TAINT_WINDOW_S
