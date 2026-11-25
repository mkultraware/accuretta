# SPDX-License-Identifier: AGPL-3.0-only
"""Watching workspace folders for changes and telling the UI, debounced.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
from accuretta.core.events import broadcast_event


_WORKSPACE_WATCH_STOP = threading.Event()


_WORKSPACE_WATCH_LOCK = threading.Lock()


_WORKSPACE_WATCHERS: dict[str, object] = {}


_WORKSPACE_EVENT_TIMER: threading.Timer | None = None


def _broadcast_workspace_update_debounced(delay: float = 0.4) -> None:
    global _WORKSPACE_EVENT_TIMER

    def _fire() -> None:
        global _WORKSPACE_EVENT_TIMER
        with _WORKSPACE_WATCH_LOCK:
            _WORKSPACE_EVENT_TIMER = None
        if not _WORKSPACE_WATCH_STOP.is_set():
            broadcast_event({"type": "workspace:update"})

    with _WORKSPACE_WATCH_LOCK:
        if _WORKSPACE_EVENT_TIMER is not None:
            _WORKSPACE_EVENT_TIMER.cancel()
        _WORKSPACE_EVENT_TIMER = threading.Timer(delay, _fire)
        _WORKSPACE_EVENT_TIMER.daemon = True
        _WORKSPACE_EVENT_TIMER.start()


def _stop_workspace_watching() -> None:
    global _WORKSPACE_EVENT_TIMER
    _WORKSPACE_WATCH_STOP.set()
    with _WORKSPACE_WATCH_LOCK:
        for watcher in _WORKSPACE_WATCHERS.values():
            watcher.stop()
        _WORKSPACE_WATCHERS.clear()
        if _WORKSPACE_EVENT_TIMER is not None:
            _WORKSPACE_EVENT_TIMER.cancel()
            _WORKSPACE_EVENT_TIMER = None
