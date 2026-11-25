"""Discord approval DMs: when they should and should not reach the phone.

The owner may run Accuretta from the PC itself or from another machine over a
private connection. Approvals expire (and deny) after 5-10 minutes, so a held
DM is only useful if it lands with time left to react.

  - switched from the Accuretta tab to an IDE a few minutes ago, host idle:
    still around -> hold (answering in-app cancels the DM);
  - an approval left unanswered while "around": one DM after two minutes,
    well inside the approval's lifetime;
  - no Accuretta tab focused anywhere for ten minutes and an idle host:
    away -> DM straight away;
  - a desktop window left open (unfocused) on an empty desk is not presence.
"""

import threading
import unittest

from presence_notify import PresenceNotify

APPROVAL_DM_AFTER_S = 120.0   # mirrors bridge._APPROVAL_DM_AFTER_S
SHORTEST_APPROVAL_TIMEOUT_S = 300.0


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
    def __init__(self):
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return True


def _service(clock: FakeClock, idle: float | None) -> PresenceNotify:
    return PresenceNotify(
        idle_seconds=(lambda: idle) if idle is not None else (lambda: None),
        now=clock,
        run_escalator=False,
    )


def _post(svc: PresenceNotify, sender: Sender, resolved=None) -> str:
    return svc.post_external(key="approval:a1", kind="approval", title="t", body="b",
                             send=sender, resolved=resolved,
                             escalate_after_s=APPROVAL_DM_AFTER_S)


class RemoteWorkTests(unittest.TestCase):
    def test_switched_to_ide_recently_holds_the_dm(self):
        clock = FakeClock()
        svc = _service(clock, idle=900.0)          # nobody at the host
        svc.note_web("mac-tab", focused=True)
        clock.advance(20.0)
        svc.note_web("mac-tab", focused=False)     # blur: over to the IDE
        clock.advance(180.0)
        sender = Sender()
        self.assertEqual(_post(svc, sender), "held")
        self.assertEqual(sender.calls, 0)

    def test_answered_in_app_never_reaches_the_phone(self):
        clock = FakeClock()
        svc = _service(clock, idle=2.0)
        sender = Sender()
        state = {"done": False}
        self.assertEqual(_post(svc, sender, resolved=lambda: state["done"]), "held")
        clock.advance(40.0)
        state["done"] = True                       # approved on screen
        svc.tick()
        clock.advance(200.0)
        svc.tick()
        self.assertEqual(sender.calls, 0)

    def test_unanswered_approval_pings_once_with_time_left(self):
        clock = FakeClock()
        svc = _service(clock, idle=2.0)            # at the desk the whole time
        sender = Sender()
        self.assertEqual(_post(svc, sender), "held")
        clock.advance(APPROVAL_DM_AFTER_S - 15.0)
        svc.tick()
        self.assertEqual(sender.calls, 0)
        clock.advance(15.0)
        svc.tick()
        self.assertEqual(sender.calls, 1)
        clock.advance(60.0)
        svc.tick()
        self.assertEqual(sender.calls, 1)
        self.assertLess(APPROVAL_DM_AFTER_S + 60.0, SHORTEST_APPROVAL_TIMEOUT_S)

    def test_away_from_every_device_sends_straight_away(self):
        clock = FakeClock()
        svc = _service(clock, idle=900.0)
        svc.note_web("mac-tab", focused=True)
        svc.note_web("mac-tab", focused=False)
        clock.advance(601.0)
        sender = Sender()
        self.assertEqual(_post(svc, sender), "sent")
        self.assertEqual(sender.calls, 1)

    def test_unfocused_window_on_an_empty_desk_is_not_presence(self):
        clock = FakeClock()
        svc = _service(clock, idle=3600.0)
        for _ in range(10):                        # keepalives from an open window
            svc.note_web("desktop-window", focused=False)
            clock.advance(30.0)
        sender = Sender()
        self.assertEqual(_post(svc, sender), "sent")


if __name__ == "__main__":
    unittest.main()
