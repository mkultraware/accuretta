import threading
import time
import unittest

from presence_notify import PresenceNotify


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.t = start
        self.lock = threading.Lock()

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.t += seconds

    def __call__(self) -> float:
        with self.lock:
            return self.t


class Sender:
    def __init__(self, deliver: bool = True, fail_times: int = 0):
        self.calls = 0
        self.deliver = deliver
        self.fail_times = int(fail_times)

    def __call__(self) -> bool:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        return self.deliver


def _policy(clock: FakeClock, idle: float | None, **kwargs) -> tuple[PresenceNotify, FakeClock]:
    svc = PresenceNotify(idle_seconds=(lambda: idle) if idle is not None else (lambda: None),
                         now=clock, run_escalator=False, **kwargs)
    return svc, clock


class PresenceTests(unittest.TestCase):
    def test_windows_idle_reader_returns_number_or_none(self):
        from presence_notify import _windows_input_idle_seconds
        value = _windows_input_idle_seconds()
        self.assertTrue(value is None or (isinstance(value, float) and value >= 0.0))

    def test_absent_user_gets_immediate_delivery(self):
        clock = FakeClock()
        svc, _ = _policy(clock, idle=900.0)
        sender = Sender()
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender), "sent")
        self.assertEqual(sender.calls, 1)
        self.assertEqual(svc.status()["pending"], [])

    def test_no_signal_fails_open_to_immediate_delivery(self):
        clock = FakeClock()
        svc, _ = _policy(clock, idle=None)  # no web clients, no idle source
        sender = Sender()
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender), "sent")
        self.assertEqual(sender.calls, 1)

    def test_active_user_holds_then_escalates_after_deadline(self):
        clock = FakeClock()
        svc, clock = _policy(clock, idle=2.0)
        sender = Sender()
        outcome = svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender, escalate_after_s=60.0)
        self.assertEqual(outcome, "held")
        self.assertEqual(sender.calls, 0)
        clock.advance(30.0)
        svc.tick()
        self.assertEqual(sender.calls, 0)  # before the deadline: still held
        clock.advance(31.0)
        svc.tick()
        self.assertEqual(sender.calls, 1)  # unanswered past the deadline: one ping
        self.assertEqual(svc.status()["pending"], [])
        clock.advance(30.0)
        svc.tick()
        self.assertEqual(sender.calls, 1)  # never twice

    def test_hold_releases_when_user_turns_absent(self):
        svc, clock = _policy(FakeClock(), idle=2.0)
        sender = Sender()
        svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender, escalate_after_s=3600.0)
        self.assertEqual(sender.calls, 0)
        clock.advance(10.0)
        svc._idle_seconds = lambda: 800.0  # user walked away
        svc.tick()
        self.assertEqual(sender.calls, 1)

    def test_web_focused_heartbeat_counts_as_active(self):
        clock = FakeClock()
        svc, _ = _policy(clock, idle=5000.0)  # host idle huge, but a device is focused
        svc.note_web("tab-1", focused=True)
        sender = Sender()
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender,
                                           escalate_after_s=3600.0), "held")
        self.assertEqual(sender.calls, 0)
        # heartbeat goes stale but the tab had focus recently -> still around
        clock.advance(120.0)
        svc.tick()
        self.assertEqual(sender.calls, 0)
        self.assertEqual(svc.presence()["reason"], "web-recent")
        # ten minutes without focus anywhere and an idle host -> absent -> delivers
        clock.advance(500.0)
        svc.tick()
        self.assertEqual(sender.calls, 1)

    def test_resolve_cancels_held_ping(self):
        svc, clock = _policy(FakeClock(), idle=2.0)
        sender = Sender()
        svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender, escalate_after_s=30.0)
        svc.resolve("k1")
        clock.advance(100.0)  # past dedupe window and past the old deadline
        svc.tick()
        self.assertEqual(sender.calls, 0)
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=Sender()), "held")

    def test_resolved_callback_drops_pending_on_tick(self):
        state = {"done": False}
        svc, clock = _policy(FakeClock(), idle=2.0)
        sender = Sender()
        svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender,
                          resolved=lambda: state["done"], escalate_after_s=3600.0)
        self.assertEqual(sender.calls, 0)
        state["done"] = True
        clock.advance(5.0)
        svc.tick()
        self.assertEqual(sender.calls, 0)
        self.assertEqual(svc.status()["pending"], [])

    def test_duplicate_keys_are_suppressed_then_expire(self):
        svc, clock = _policy(FakeClock(), idle=2.0)
        sender = Sender()
        self.assertEqual(svc.post_external(key="dup", kind="approval", title="t", body="b", send=sender,
                                           escalate_after_s=60.0), "held")
        sender2 = Sender()
        self.assertEqual(svc.post_external(key="dup", kind="approval", title="t", body="b", send=sender2), "duplicate")
        self.assertEqual(sender2.calls, 0)
        clock.advance(120.0)
        svc._idle_seconds = lambda: 900.0  # user away -> held ping delivers
        svc.tick()
        # only the first sender delivered
        self.assertEqual(sender.calls, 1)
        self.assertEqual(sender2.calls, 0)
        # dedupe still applies within the handled window
        self.assertEqual(svc.post_external(key="dup", kind="approval", title="t", body="b", send=Sender()), "duplicate")
        # ...and expires with it (re-routed as a fresh hold, not swallowed)
        clock.advance(100.0)
        svc._idle_seconds = lambda: 2.0  # user is back at the desk
        self.assertEqual(svc.post_external(key="dup", kind="approval", title="t", body="b", send=Sender()), "held")

    def test_failed_send_retries_via_escalator(self):
        svc, clock = _policy(FakeClock(), idle=900.0)
        sender = Sender(deliver=False)
        # absent user: the first delivery attempt happens inline and fails
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender), "held")
        clock.advance(20.0)
        svc.tick()  # retry fails
        sender.deliver = True
        clock.advance(20.0)
        svc.tick()  # retry succeeds
        self.assertEqual(sender.calls, 3)
        self.assertEqual(svc.status()["pending"], [])

    def test_never_focused_heartbeat_is_not_presence(self):
        # A tab (or desktop window) that is open but never had focus says
        # nothing about whether anyone is there.
        clock = FakeClock()
        svc, _ = _policy(clock, idle=None)  # no input source; tab open but not focused
        svc.note_web("tab-2", focused=False)
        sender = Sender()
        self.assertEqual(svc.post_external(key="k1", kind="approval", title="t", body="b", send=sender), "sent")
        self.assertEqual(svc.presence()["reason"], "web-not-focused")

    def test_blur_counts_as_the_last_focused_moment(self):
        # focused at t0, blur heartbeat at t0+300: the ten-minute window runs
        # from the blur, not from the last focused keepalive.
        clock = FakeClock()
        svc, _ = _policy(clock, idle=900.0)
        svc.note_web("tab-1", focused=True)
        clock.advance(300.0)
        svc.note_web("tab-1", focused=False)
        clock.advance(550.0)
        self.assertEqual(svc.presence()["reason"], "web-recent")
        clock.advance(60.0)
        self.assertEqual(svc.presence()["reason"], "input-idle")
        self.assertFalse(svc.presence()["active"])

    def test_status_shape(self):
        svc, _ = _policy(FakeClock(), idle=2.0)
        svc.note_web("tab-3", focused=True)
        svc.post_external(key="s1", kind="approval", title="T", body="B", send=Sender(), escalate_after_s=60.0)
        snap = svc.status()
        self.assertTrue(snap["active"])
        self.assertEqual(snap["reason"], "web-focused")
        self.assertEqual(len(snap["pending"]), 1)
        self.assertNotIn("send", snap["pending"][0])
        self.assertNotIn("resolved", snap["pending"][0])
        self.assertIn("tab-3", snap["web_seen"])

    def test_real_thread_escalator_delivers(self):
        # Active tab: the ping is held, then delivered by the real-thread
        # escalator at the (short) escalation deadline.
        svc = PresenceNotify(idle_seconds=lambda: None, run_escalator=True, tick_s=0.05)
        svc.note_web("tab", focused=True)
        sender = Sender()
        outcome = svc.post_external(key="k9", kind="approval", title="t", body="b", send=sender, escalate_after_s=1.0)
        self.assertEqual(outcome, "held")
        deadline = time.time() + 3.0
        while sender.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(sender.calls, 1)


if __name__ == "__main__":
    unittest.main()
