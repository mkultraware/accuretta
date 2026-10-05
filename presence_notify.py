"""User presence and external-notification routing, shared by approval DMs
and future security alerts.

The bridge historically mirrored every event to the owner's phone even while
they sat in front of the web UI. This module answers two questions instead:

  1. Is the user active right now?  Two cheap signals are OR-ed so a single
     broken or stale signal cannot wrongly suppress notifications:
       - a fresh *focused* web-UI heartbeat (works for remote devices too —
         a phone actively browsing Accuretta counts as present);
       - low local input idle via Windows GetLastInputInfo (the OS maintains
         the timestamp whether or not a monitor is on; no polling involved).
     When neither signal can say anything, the verdict is ABSENT (fail-open:
     deliver externally immediately — that is today's behaviour).

  2. Where should an attention-needing event go, and when? "Suppress then
     escalate": while the user looks ACTIVE, external delivery (e.g. a
     Discord DM) is held. A held notification is never lost — the escalator
     delivers it the moment the user looks inactive or when the escalation
     deadline passes, and in-app resolution cancels it silently. The worst
     outcome is a late notification, never a silent one.

Compute cost is negligible: no WMI/process polling, one microsecond OS API
call consulted lazily when events fire, plus a slow background tick that
only inspects a handful of held records.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Callable


DEFAULT_WEB_ACTIVE_WINDOW_S = 90.0   # heartbeat fresher than this counts
DEFAULT_IDLE_ACTIVE_S = 150.0        # input idle below this counts as active
DEFAULT_ESCALATE_AFTER_S = 600.0     # held pings deliver after 10 minutes
DEFAULT_TICK_S = 15.0
RECENT_HANDLED_S = 90.0             # dedupe window after send/resolve
MAX_PENDING = 64
MAX_HANDLED_MEMORY = 128


def _windows_input_idle_seconds() -> float | None:
    """Seconds since the last local user input, or None when unavailable.

    Reads the LASTINPUTINFO timestamp the OS keeps for the current session.
    Deliberately not a WMI/process poll. GetTickCount wraps about every 49.7
    days; the mask arithmetic keeps a wrap-adjacent delta positive, which in
    the worst case misreads a fleetingly as "very idle" once per 49 days.
    """
    try:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        user32 = ctypes.windll.user32
        if not user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = user32.GetTickCount()
        delta = (int(tick) - int(info.dwTime)) & 0xFFFFFFFF
        return float(delta) / 1000.0
    except Exception:
        return None


class PresenceNotify:
    def __init__(
        self,
        *,
        idle_seconds: Callable[[], float | None] | None = None,
        now: Callable[[], float] = time.monotonic,
        run_escalator: bool = True,
        web_active_window_s: float = DEFAULT_WEB_ACTIVE_WINDOW_S,
        idle_active_s: float = DEFAULT_IDLE_ACTIVE_S,
        escalate_after_s: float = DEFAULT_ESCALATE_AFTER_S,
        tick_s: float = DEFAULT_TICK_S,
    ):
        self._idle_seconds = idle_seconds or _windows_input_idle_seconds
        self._now = now
        self._web_active_window_s = float(web_active_window_s)
        self._idle_active_s = float(idle_active_s)
        self._escalate_after_s = float(escalate_after_s)
        self._tick_s = float(tick_s)
        self._run_escalator = bool(run_escalator)
        self._lock = threading.Lock()
        self._web: dict[str, dict] = {}       # client_id -> {focused, seen}
        self._pending: dict[str, dict] = {}   # key -> held/sending record
        self._handled: dict[str, float] = {}  # key -> when it was handled
        self._ticker: threading.Thread | None = None

    # ---- web UI presence -------------------------------------------------

    def note_web(self, client_id: str, focused: bool) -> None:
        """Record a web-UI heartbeat. One id per browser tab; the focused
        flag is the tab's visibilityState && document.hasFocus() snapshot."""
        cid = str(client_id or "").strip()[:64]
        if not cid:
            return
        with self._lock:
            self._web[cid] = {"focused": bool(focused), "seen": self._now()}
            self._gc_web_locked()

    def drop_web(self, client_id: str) -> None:
        cid = str(client_id or "").strip()[:64]
        if not cid:
            return
        with self._lock:
            self._web.pop(cid, None)

    def _gc_web_locked(self) -> None:
        horizon = self._now() - max(self._web_active_window_s * 40.0, 3600.0)
        for cid in [cid for cid, rec in self._web.items() if rec["seen"] < horizon]:
            self._web.pop(cid, None)

    # ---- presence decision -----------------------------------------------

    def presence(self) -> dict:
        """{"active": bool, "reason": str, ...} snapshot for routing and UI."""
        now = self._now()
        with self._lock:
            fresh = [rec for rec in self._web.values() if now - rec["seen"] <= self._web_active_window_s]
        focused_fresh = any(rec["focused"] for rec in fresh)
        try:
            idle = self._idle_seconds()
        except Exception:
            idle = None
        if focused_fresh:
            active, reason = True, "web-focused"
        elif idle is not None and idle <= self._idle_active_s:
            active, reason = True, "input-recent"
        elif idle is not None:
            active, reason = False, "input-idle"
        elif fresh:
            active, reason = False, "web-not-focused"
        else:
            active, reason = False, "no-signal"
        return {
            "active": active,
            "reason": reason,
            "idle_seconds": idle,
            "web_clients": len(fresh),
            "web_focused_fresh": focused_fresh,
        }

    # ---- external notification routing -----------------------------------

    def post_external(
        self,
        *,
        key: str,
        kind: str,
        title: str,
        body: str,
        send: Callable[[], bool],
        resolved: Callable[[], bool] | None = None,
        escalate_after_s: float | None = None,
    ) -> str:
        """Route an event that may end up on the user's phone.

        send() must actually deliver the external ping and return True when
        delivery succeeded. resolved() should return True once the underlying
        item no longer needs attention (answered/decided/timeout elsewhere).

        Returns "sent" (delivered immediately because the user looks absent),
        "held" (suppressed while the user looks active; the escalator owns it
        now), or "duplicate"/"invalid".
        """
        key = str(key or "").strip()[:128]
        if not key or not callable(send):
            return "invalid"
        now = self._now()
        state = self.presence()

        with self._lock:
            if key in self._pending:
                return "duplicate"
            seen = self._handled.get(key)
            if seen is not None and now - seen <= RECENT_HANDLED_S:
                return "duplicate"

        if not state["active"]:
            try:
                delivered = bool(send())
            except Exception:
                delivered = False
            if delivered:
                self._handled[key] = now
                self._trim_handled_locked()
                return "sent"
            # Delivery failed: fall through to hold so the escalator retries
            # instead of silently dropping the notification.

        deadline = now + (self._escalate_after_s if escalate_after_s is None else max(1.0, float(escalate_after_s)))
        with self._lock:
            self._pending[key] = {
                "key": key,
                "kind": str(kind or "")[:32],
                "title": str(title or "")[:180],
                "body": str(body or "")[:1500],
                "created": now,
                "escalate_at": deadline,
                "state": "held",
                "send": send,
                "resolved": resolved,
            }
            self._trim_pending_locked()
            self._ensure_ticker_locked()
        return "held"

    def resolve(self, key: str) -> None:
        """Mark an event handled in-app; cancels any held external ping."""
        key = str(key or "").strip()
        if not key:
            return
        with self._lock:
            self._pending.pop(key, None)
            self._handled[key] = self._now()
            self._trim_handled_locked()

    def _trim_pending_locked(self) -> None:
        while len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda k: self._pending[k]["created"])
            self._pending.pop(oldest, None)

    def _trim_handled_locked(self) -> None:
        while len(self._handled) > MAX_HANDLED_MEMORY:
            oldest = min(self._handled, key=self._handled.get)  # type: ignore[arg-type]
            self._handled.pop(oldest, None)

    # ---- escalator ---------------------------------------------------------

    def _ensure_ticker_locked(self) -> None:
        if not self._run_escalator:
            return
        if self._ticker and self._ticker.is_alive():
            return
        self._ticker = threading.Thread(target=self._loop, name="presence-escalator", daemon=True)
        self._ticker.start()

    def _loop(self) -> None:
        while True:
            time.sleep(self._tick_s)
            try:
                self.tick()
            except Exception:
                pass

    def tick(self) -> None:
        """One escalator pass: drop resolved items, deliver when the user
        looks absent, deliver past the deadline otherwise."""
        now = self._now()
        # One presence decision per pass — state cannot change meaningfully
        # within a tick, and presence() must not run while the record lock is
        # held (it takes the same lock; threading.Lock is not reentrant).
        presence_active = self.presence()["active"]
        due: list[dict] = []
        with self._lock:
            for record in list(self._pending.values()):
                if record["state"] != "held":
                    continue
                resolved_now = False
                if callable(record["resolved"]):
                    try:
                        resolved_now = bool(record["resolved"]())
                    except Exception:
                        resolved_now = False
                if resolved_now:
                    self._pending.pop(record["key"], None)
                    self._handled[record["key"]] = now
                    self._trim_handled_locked()
                    continue
                if now >= record["escalate_at"] or not presence_active:
                    record["state"] = "sending"
                    due.append(record)
        for record in due:
            try:
                delivered = bool(record["send"]())
            except Exception:
                delivered = False
            with self._lock:
                if delivered:
                    self._pending.pop(record["key"], None)
                    self._handled[record["key"]] = self._now()
                    self._trim_handled_locked()
                else:
                    # keep at "held" so the next pass retries; the deadline
                    # still applies so repeated failures cannot stall forever
                    current = self._pending.get(record["key"])
                    if current and current["state"] == "sending":
                        current["state"] = "held"

    def status(self) -> dict:
        result = self.presence()  # outside the record lock (presence locks too)
        with self._lock:
            return {
                **result,
                "pending": [
                    {k: v for k, v in rec.items() if k not in ("send", "resolved")}
                    for rec in self._pending.values()
                ],
                "web_seen": {cid: rec["seen"] for cid, rec in sorted(self._web.items())},
            }
