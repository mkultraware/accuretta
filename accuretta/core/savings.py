# SPDX-License-Identifier: AGPL-3.0-only
"""The lifetime token counters behind the savings card: loading, reading and adding,
kept in savings.json.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
import time
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json


SAVINGS_FILE = DATA / "savings.json"


# ---- lifetime savings counters --------------------------------------------
# The "saved vs cloud" totals are persisted here, next to chats.json, and read
# over /api/savings: localStorage is per-browser and wiped by the desktop
# webview's private mode. Tokens only; cost is priced client-side from the
# selected provider, so switching provider re-prices the whole history.
_SAVINGS_LOCK = threading.Lock()


_savings_cache: dict | None = None


_SAVINGS_DEFAULT = {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0}


def _savings_ensure_locked() -> None:
    """Load savings.json into the cache once. Caller must hold _SAVINGS_LOCK."""
    global _savings_cache
    if _savings_cache is not None:
        return
    data = load_json(SAVINGS_FILE, None)
    if not isinstance(data, dict):
        data = dict(_SAVINGS_DEFAULT)
    else:
        for k, v in _SAVINGS_DEFAULT.items():
            data.setdefault(k, v)
    if not data.get("since"):
        data["since"] = int(time.time())   # first-ever run: stamp the start date
    _savings_cache = data


def _savings_get() -> dict:
    with _SAVINGS_LOCK:
        _savings_ensure_locked()
        return dict(_savings_cache)


def _savings_add(tok_in: int, tok_out: int, *, count_turn: bool = True) -> dict:
    """Add tokens to the lifetime counters and persist. Returns a copy of the
    new totals. Never raises. Side calls (sub-agents, compaction, image
    descriptions) pass count_turn=False: their tokens count, but they are not
    model turns the owner took."""
    try:
        ti = max(0, int(tok_in or 0))
        to = max(0, int(tok_out or 0))
    except (TypeError, ValueError):
        ti = to = 0
    with _SAVINGS_LOCK:
        _savings_ensure_locked()
        _savings_cache["tok_in"] += ti
        _savings_cache["tok_out"] += to
        if count_turn and (ti or to):
            _savings_cache["turns"] += 1
        snap = dict(_savings_cache)
    try:
        save_json(SAVINGS_FILE, snap)   # its own lock; keep it out of _SAVINGS_LOCK
    except Exception:
        pass
    return snap
