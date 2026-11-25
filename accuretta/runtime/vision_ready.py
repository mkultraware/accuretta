# SPDX-License-Identifier: AGPL-3.0-only
"""Whether the agent can look at images right now: the main model's own vision, or a
separate vision server that is up.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.endpoints import LLAMA
from accuretta.core.state import STATE
from accuretta.runtime.llama_http import llama_ready
from accuretta.runtime.llama_process import _llama


def _vision_observation_available() -> bool:
    try:
        if _llama.is_vision_capable():
            return True
    except Exception:
        pass
    return _separate_vision_server_ready(timeout=0.5)


def _separate_vision_server_ready(timeout: float = 0.8) -> bool:
    """True only when the configured side vision server answers right now.

    A different URL is configuration, not availability; treating it as ready
    made image POSTs wait in describe_image() for up to three minutes on a dead
    side-server address."""
    if not STATE.vision_llama or STATE.vision_llama == LLAMA:
        return False
    try:
        return llama_ready(timeout=timeout, base_url=STATE.vision_llama)
    except Exception:
        return False
