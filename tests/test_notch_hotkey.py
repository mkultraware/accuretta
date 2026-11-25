"""The hotkey must bring the island back on screen, every time.

The host shows the overlay window itself, but the island's slide-out is a
CSS class on the page: hiding sends {type: "leave"}, which leaves `is-leaving`
on the island (translated above the top screen edge at opacity 0). Only
{type: "enter"} clears it. The reveal path used to raise the window without
sending "enter", so from the second press onwards the island came back
invisible and untappable - the hotkey looked dead.
"""
import unittest

import notch_host


class _FakeUser32:
    def __init__(self):
        self.shown = []

    def ShowWindow(self, hwnd, cmd):
        self.shown.append(cmd)
        return True


    def test_reveal_logs_the_hotkey_reason(self):
        self._hide_first()
        self.posted.clear()
        self.host._leaving = False
        self.host.visible = False
        self.host._toggle_island("hotkey")
        self.assertTrue(any("hotkey" in line for line in self.logs),
                        "the reveal must be traceable in the host log")


class QuietAfterHotkeyTests(unittest.TestCase):
    """Tucked away with Ctrl+Alt+N, only an approval brings the island back,
    and it leaves again once that approval is handled."""

    def setUp(self):
        self.host = notch_host.NotchHost()
        self.posted = []
        self.host._to_page = self.posted.append
        self._real_user32, self._real_log = notch_host.user32, notch_host.log
        notch_host.user32 = _FakeUser32()
        notch_host.log = lambda *_: None
        self.host._apply_region = lambda: None
        self.addCleanup(self._restore)
        self.host.visible = True
        self.host._toggle_island("hotkey")                # tuck away
        self.host._hide_at = 0.0                          # the collapse ran its course
        self.host._leaving = False
        self.host.visible = False

    def _restore(self):
        notch_host.user32, notch_host.log = self._real_user32, self._real_log

    def _page(self, msg):
        class Args:
            WebMessageAsJson = __import__("json").dumps(msg)
        self.host._on_web_message(None, Args())

    def test_only_an_approval_pops_it_up(self):
        self.assertTrue(self.host.quiet)
        self._page({"type": "notify", "kind": "finished"})
        self.assertFalse(self.host.visible)
        self._page({"type": "notify", "kind": "gate"})
        self.assertTrue(self.host.visible)

    def test_it_leaves_once_the_approval_is_handled(self):
        self._page({"type": "notify", "kind": "gate"})
        self.host.island_state = "gate"
        self.host._auto_hide_check()
        self.posted.clear()
        self.host.island_state = "work"
        self.host._auto_hide_check()
        self.assertIn({"type": "collapse"}, self.posted)

    def test_it_hides_even_while_a_command_keeps_it_busy(self):
        # After the approval the command runs; its progress keeps the island in
        # "work", so it never reaches idle. It used to linger there.
        self._page({"type": "notify", "kind": "gate"})
        self.host.island_state = "gate"
        self.host._auto_hide_check()
        self.host.island_state = "work"
        self.host._auto_hide_check()
        self.assertTrue(self.host._hide_at)
        self.host._hide_at -= self.host.COLLAPSE_HIDE_S        # the spring has landed
        self.host._tick_collapse()
        self.assertTrue(self.host.visible, "a short grace first")
        self.host._hide_at -= self.host.COLLAPSE_FORCE_S + 0.01
        self.host._tick_collapse()
        self.assertFalse(self.host.visible)
        self.assertTrue(self.host.quiet, "still tucked away for the next approval")

    def test_a_new_approval_keeps_it_up(self):
        self._page({"type": "notify", "kind": "gate"})
        self.host.island_state = "gate"
        self.host._auto_hide_check()
        self.host.island_state = "work"
        self.host._auto_hide_check()
        self.host.island_state = "gate"                         # the next one arrived
        self.host._hide_at -= self.host.COLLAPSE_HIDE_S + self.host.COLLAPSE_FORCE_S + 0.01
        self.host._tick_collapse()
        self.assertTrue(self.host.visible)

    def test_hotkey_reveal_ends_quiet_mode(self):
        self.host._toggle_island("hotkey")
        self.assertFalse(self.host.quiet)


class ToggleIslandWasBrokenTests(unittest.TestCase):
    """Pins the bug itself: the old reveal raised the window without `enter`."""

    def test_old_reveal_path_never_sent_enter(self):
        import inspect
        src = inspect.getsource(notch_host.NotchHost._toggle_island)
        self.assertNotIn("ShowWindow", src,
                         "_toggle_island must delegate the reveal to show_island "
                         "so the page is told to come back on screen")
        self.assertIn("show_island", src)


class ToggleIslandTests(unittest.TestCase):
    def setUp(self):
        self.host = notch_host.NotchHost()
        self.posted = []
        self.logs = []
        self.host._to_page = self.posted.append
        self.host.log = self.logs.append
        self._real_user32 = notch_host.user32
        self.fake = _FakeUser32()
        self._real_log = notch_host.log
        notch_host.user32 = self.fake
        notch_host.log = self.logs.append
        self.addCleanup(self._restore)

    def _restore(self):
        notch_host.user32 = self._real_user32
        notch_host.log = self._real_log

    def _hide_first(self):
        """Put the host in the state it reaches after any auto-hide."""
        self.host.visible = True
        self.host.hide_island("test")
        self.assertIn({"type": "leave"}, self.posted)

    def test_hotkey_after_a_hide_asks_the_page_to_come_back(self):
        self._hide_first()
        self.posted.clear()
        self.host._leaving = False          # the slide-out finished
        self.host.visible = False

        self.host._toggle_island("hotkey")

        self.assertTrue(self.host.visible, "the window must be shown again")
        self.assertIn({"type": "enter"}, self.posted,
                      "the reveal must send `enter`, otherwise the island stays "
                      "parked above the top edge at opacity 0")
        self.assertIn({"type": "refresh-rect"}, self.posted,
                      "a hidden page sends no animation frames, so the host must "
                      "ask for the island rect again")
        self.assertTrue(self.fake.shown, "ShowWindow must be called")

    def test_hotkey_still_toggles_off_when_visible(self):
        self.host.visible = True
        self.posted.clear()
        self.host._toggle_island("hotkey")
        self.assertIn({"type": "collapse"}, self.posted)
        self.assertNotIn({"type": "enter"}, self.posted)

    def test_reveal_marks_the_island_sticky_so_a_timer_cannot_retract_it(self):
        self._hide_first()
        self.posted.clear()
        self.host._leaving = False
        self.host.visible = False
        self.host._toggle_island("hotkey")
        self.assertTrue(self.host.sticky,
                        "an owner-raised island must stay up until they dismiss it")


if __name__ == "__main__":
    unittest.main()