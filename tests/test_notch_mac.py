"""macOS notch host: the behaviour layer and the bridge's platform pick.

notch_host_mac.py keeps everything that decides when the island shows,
hides, folds or lets clicks through in IslandLogic, which has no AppKit
dependency, so it runs here on any platform with a fake UI layer. The
AppKit/WebKit layer itself needs a Mac.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

import notch_host_mac as mac


class FakeUI:
    def __init__(self):
        self.posted, self.logs, self.calls = [], [], []
        self.interactive = None
        self.raise_ok = True

    def show(self): self.calls.append("show")
    def hide(self): self.calls.append("hide")
    def post(self, msg): self.posted.append(msg)
    def set_interactive(self, on): self.interactive = on
    def raise_main(self): self.calls.append("raise"); return self.raise_ok
    def open_url(self, url): self.calls.append(("open", url))
    def log(self, text): self.logs.append(text)


def rect(state="idle", w=196, h=32):
    return {"type": "island-rect", "x": (720 - w) / 2, "y": 0, "w": w, "h": h, "state": state}


class IslandLogicTests(unittest.TestCase):
    def setUp(self):
        self.ui = FakeUI()
        self.logic = mac.IslandLogic(self.ui)

    def test_first_rect_reveals_once(self):
        self.logic.on_page_message(rect())
        self.assertTrue(self.logic.visible)
        self.assertIn({"type": "enter"}, self.ui.posted)
        self.ui.posted.clear()
        self.logic.on_page_message(rect())
        self.assertNotIn({"type": "enter"}, self.ui.posted)

    def test_hotkey_toggles_down_and_back_up(self):
        self.logic.toggle("hotkey", now=0)
        self.assertTrue(self.logic.visible and self.logic.sticky)
        self.assertIn({"type": "enter"}, self.ui.posted, "the page must be told to come back down")
        self.logic.on_page_message(rect())
        self.logic.toggle("hotkey", now=1)
        self.assertIn({"type": "collapse"}, self.ui.posted)
        # the page settles to the pill, then the island slides out and hides
        self.logic.tick(2, None, False, False, False)
        self.assertIn({"type": "leave"}, self.ui.posted)
        self.logic.tick(3, None, False, False, False)
        self.assertFalse(self.logic.visible)
        self.assertIn("hide", self.ui.calls)

    def test_clicks_pass_through_except_on_the_island(self):
        self.logic.on_page_message(rect())
        self.logic.tick(0, (360, 16), False, False, False)
        self.assertTrue(self.ui.interactive)
        self.assertIn({"type": "pointer", "inside": True}, self.ui.posted)
        self.logic.tick(0.1, (40, 300), False, False, False)
        self.assertFalse(self.ui.interactive)
        self.assertIn({"type": "pointer", "inside": False}, self.ui.posted)

    def test_hover_on_the_top_edge_reveals_after_a_dwell(self):
        self.logic.tick(0, None, False, True, False)
        self.assertFalse(self.logic.visible)
        self.logic.tick(0.2, None, False, True, False)
        self.assertTrue(self.logic.visible and self.logic.hover_reveal)

    def test_no_hover_reveal_over_a_full_screen_app(self):
        self.logic.tick(0, None, False, True, True)
        self.logic.tick(0.5, None, False, True, True)
        self.assertFalse(self.logic.visible)

    def test_hover_pill_tucks_away_soon_after_the_pointer_leaves(self):
        self.logic.revealed_once = True       # long past the startup reveal
        self.logic.tick(0, None, False, True, False)
        self.logic.tick(0.2, None, False, True, False)
        self.logic.on_page_message(rect())
        self.logic.tick(1, (10, 300), False, False, False)
        self.assertTrue(self.logic.visible)
        self.logic.tick(4, (10, 300), False, False, False)
        self.assertFalse(self.logic.visible)

    def test_open_cards_and_live_work_are_never_timed_out(self):
        for state in ("gate", "prompt", "home", "notice", "work", "alert", "drop"):
            ui = FakeUI()
            logic = mac.IslandLogic(ui)
            logic.on_page_message(rect(state, 640, 180))
            logic.tick(60, (10, 400), False, False, False)
            self.assertTrue(logic.visible, f"{state} was hidden on a timer")

    def test_click_outside_an_open_card_folds_it(self):
        self.logic.on_page_message(rect("prompt", 640, 200))
        self.logic.tick(0, (10, 400), True, False, False)
        self.assertIn({"type": "outside-click"}, self.ui.posted)

    def test_open_url_only_for_the_bridge_origin(self):
        self.logic.on_page_message({"type": "open-url", "url": "https://evil.example/x"})
        self.assertEqual(self.ui.calls, [])
        self.logic.on_page_message({"type": "open-url", "url": mac.BASE + "/?chat=abc"})
        self.assertIn("raise", self.ui.calls)
        self.ui.raise_ok = False
        self.ui.calls.clear()
        self.logic.on_page_message({"type": "open-url", "url": mac.BASE + "/"})
        self.assertIn(("open", mac.BASE + "/"), self.ui.calls, "no desktop window: fall back to the browser")


class QuietAfterHotkeyTests(unittest.TestCase):
    """Tucked away with the hotkey, only an approval brings the island back,
    and it leaves again once that approval is handled."""

    def setUp(self):
        self.ui = FakeUI()
        self.logic = mac.IslandLogic(self.ui)
        self.logic.revealed_once = True
        self.logic.toggle("hotkey", now=0)                 # down
        self.logic.on_page_message(rect())
        self.logic.toggle("hotkey", now=1)                 # and tucked away
        self.logic.tick(2, None, False, False, False)
        self.logic.tick(3, None, False, False, False)
        self.assertFalse(self.logic.visible)
        self.assertTrue(self.logic.quiet)

    def test_other_news_and_hover_stay_hidden(self):
        for kind in ("finished", "error", "model"):
            self.logic.on_page_message({"type": "notify", "kind": kind})
        self.logic.tick(4, None, False, True, False)
        self.logic.tick(5, None, False, True, False)
        self.assertFalse(self.logic.visible)

    def test_an_approval_pops_it_up_then_it_leaves_once_handled(self):
        self.logic.on_page_message({"type": "notify", "kind": "gate"})
        self.assertTrue(self.logic.visible)
        self.logic.on_page_message(rect("gate", 640, 180))
        self.logic.tick(4, (10, 400), False, False, False)
        self.assertTrue(self.logic.visible, "the card waits for the owner")
        self.ui.posted.clear()
        self.logic.on_page_message(rect("work"))          # approved; the agent runs on
        self.logic.tick(5, (10, 400), False, False, False)
        self.assertIn({"type": "collapse"}, self.ui.posted)
        self.logic.on_page_message(rect("idle"))
        self.logic.tick(6, (10, 400), False, False, False)
        self.logic.tick(7, (10, 400), False, False, False)
        self.assertFalse(self.logic.visible)
        self.assertTrue(self.logic.quiet, "still tucked away for the next one")

    def test_it_leaves_even_while_the_command_keeps_it_busy(self):
        self.logic.on_page_message({"type": "notify", "kind": "gate"})
        self.logic.on_page_message(rect("gate", 640, 180))
        self.logic.tick(4, (10, 400), False, False, False)
        self.logic.on_page_message(rect("work"))          # approved; never goes idle
        self.logic.tick(5, (10, 400), False, False, False)
        self.logic.tick(6, (10, 400), False, False, False)
        self.logic.tick(7, (10, 400), False, False, False)
        self.assertFalse(self.logic.visible)
        self.assertTrue(self.logic.quiet)

    def test_the_hotkey_brings_it_back_for_good(self):
        self.logic.toggle("hotkey", now=4)
        self.assertFalse(self.logic.quiet)
        self.assertTrue(self.logic.visible)


class ShimContractTests(unittest.TestCase):
    """notch-wire.js speaks WebView2's channel; the shim must provide it."""

    def test_shim_provides_the_webview2_surface(self):
        js = mac.SHIM_JS
        self.assertIn("window.chrome.webview", js)
        self.assertIn("postMessage", js)
        self.assertIn("addEventListener", js)
        self.assertIn("window.webkit.messageHandlers.notch.postMessage", js)
        self.assertIn("window.__notchHost", js)

    def test_wire_uses_exactly_that_surface(self):
        wire = (Path(mac.ROOT) / "web" / "js" / "notch-wire.js").read_text(encoding="utf-8")
        self.assertIn("window.chrome.webview.postMessage", wire)
        self.assertIn('window.chrome.webview.addEventListener("message"', wire)


class BridgePlatformTests(unittest.TestCase):
    def test_each_platform_gets_its_own_host(self):
        import bridge
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(bridge._notch_host_script().name, "notch_host.py")
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(bridge._notch_host_script().name, "notch_host_mac.py")
        with mock.patch.object(sys, "platform", "linux"):
            self.assertIsNone(bridge._notch_host_script())
            self.assertFalse(bridge._notch_host_available())


if __name__ == "__main__":
    unittest.main()
