# SPDX-License-Identifier: AGPL-3.0-only
"""The live event bus: subscribers, broadcasting, and the short log a reconnecting
client resumes from.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
from collections import deque
from queue import Full
from queue import Queue


EVENT_LOG_MAX = 256


_subscribers: list[Queue] = []


_subs_lock = threading.Lock()


_event_log: deque = deque(maxlen=EVENT_LOG_MAX)  # holds (id, evt) tuples


_event_log_id = 0  # monotonic counter, only incremented under _subs_lock


def subscribe() -> tuple[Queue, int]:
    """Returns (queue, snapshot_id): the highest event id that existed before this
    subscription. The SSE handler replays only up to it; anything newer arrives
    via the queue."""
    q: Queue = Queue(maxsize=1024)
    with _subs_lock:
        _subscribers.append(q)
        return q, _event_log_id


def unsubscribe(q: Queue) -> None:
    with _subs_lock:
        if q in _subscribers:
            _subscribers.remove(q)


# Event types forwarded live but excluded from the resume ring buffer (see
# broadcast_event). ctx_fill fires once per agentic round.
_TRANSIENT_EVENT_TYPES = frozenset({"ctx_fill", "summary_folding"})


# Token deltas normally travel over the chat POST response, but the notch
# mirrors turns running on another surface and only sees them here. They are
# best-effort: never logged (one token per ring slot would flush approvals out
# of the resume log in seconds) and dropped under backpressure rather than
# evicting a durable event.
_BEST_EFFORT_EVENT_TYPES = frozenset({"delta", "thinking_delta", "tool_progress"})


# Count of events discarded for a slow subscriber (queue full). Never removes
# the subscriber; surfaced only so a persistent flood is observable.
_dropped_event_count = 0


def broadcast_event(evt: dict) -> None:
    global _event_log_id, _dropped_event_count
    best_effort = evt.get("type") in _BEST_EFFORT_EVENT_TYPES
    with _subs_lock:
        logged = dict(evt)
        if not best_effort:
            # Per-round gauge noise goes to live subscribers but is not logged; a long
            # turn would otherwise flush resumable state out of the ring buffer.
            transient = evt.get("type") in _TRANSIENT_EVENT_TYPES
            if not transient:
                _event_log_id += 1
            evt_id = _event_log_id if not transient else None
            # Tag a copy so the caller's dict isn't mutated. The SSE handler writes
            # evt['_id'] into the wire `id:` field for Last-Event-ID resume.
            if evt_id is not None:
                logged["_id"] = evt_id
                _event_log.append((evt_id, logged))
        for q in _subscribers:
            try:
                q.put_nowait(logged)
            except Full:
                if best_effort:
                    # A token is expendable; a durable event is not. Drop the
                    # chatter, keep the queue untouched.
                    _dropped_event_count += 1
                    continue
                # A slow subscriber must not be silently unsubscribed while its HTTP stream
                # stays open (EventSource would never notice and reconnect). Drop its oldest
                # queued event instead.
                try:
                    q.get_nowait()
                    q.put_nowait(logged)
                    _dropped_event_count += 1
                except Exception:
                    pass
            except Exception:
                pass


def replay_events_since(since_id: int, up_to_id: int) -> list:
    """Return logged events with since_id < id <= up_to_id, in order.
    Snapshot under the lock so we don't iterate a deque mid-append."""
    with _subs_lock:
        return [(i, e) for (i, e) in _event_log if since_id < i <= up_to_id]
