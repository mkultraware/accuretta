# SPDX-License-Identifier: AGPL-3.0-only
"""Values Accuretta changes while it runs and that several areas read.

A plain module-level name that bridge.py rebinds with `global` is copied by every
module that imports it, and that copy goes stale at the first rebind. These live
as attributes on one object instead, so every reader sees the current value:
read and write `STATE.last_user_text`, never a local copy.
"""
from __future__ import annotations

import os

from accuretta.core.endpoints import LLAMA


def _initial_vision_llama() -> str:
    # Optional separate vision-capable llama-server. If unset, we assume the main
    # llama-server is vision-capable (started with --mmproj). If it isn't, image
    # messages just fail; the caller should handle that.
    url = (os.environ.get("VISION_LLAMA_HOST") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url or LLAMA


class _State:
    def __init__(self) -> None:
        # The vision server's address. main() points it at a fallback vision
        # server when it starts one, and back at LLAMA when that goes away.
        self.vision_llama: str = _initial_vision_llama()
        # The user's own latest message. Playwright's interactive verbs, desktop
        # input and named-site checks run freely only when these words asked for
        # them; page text never counts.
        self.last_user_text: str = ""
        # The last real prompt token count from llama-server, updated after each
        # turn (the per-chat numbers live in _last_prompt_tokens_by_chat).
        self.last_prompt_tokens: int = 0
        # The last intended raw-input target (a pygetwindow Window), re-asserted
        # right before desktop input fires. Guarded by _desktop_target_lock.
        self.desktop_target_window = None


STATE = _State()
