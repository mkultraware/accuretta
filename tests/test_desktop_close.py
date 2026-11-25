"""Desktop window close: the window's X opens the in-app shutdown dialog
instead of quitting, and every close the app means (the dialog's buttons, the
bridge stopping), an unloaded or frozen page, and a Windows shutdown still go
straight through. accuretta_app.py is read as source, not imported, so its
environment setup and pywebview import stay out of the test process."""
import ast
import sys
import threading
import time
import unittest

import bridge
from _source import frontend_source

SOURCE = (bridge.ROOT / "accuretta_app.py").read_text(encoding="utf-8")


def load_close_logic():
    tree = ast.parse(SOURCE)
    wanted = {"DesktopWindowApi", "_session_ending", "_ask_before_closing"}
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in wanted]
    ns = {"threading": threading, "sys": sys}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "accuretta_app.py", "exec"), ns)
    return ns


class FakeWindow:
    def __init__(self, answer=True, delay=0.0, raises=False):
        self.answer, self.delay, self.raises = answer, delay, raises
        self.asked = []
        self.destroyed = threading.Event()

    def evaluate_js(self, code):
        self.asked.append(code)
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise RuntimeError("page gone")
        return self.answer

    def destroy(self):
        self.destroyed.set()


class AskBeforeClosingTests(unittest.TestCase):
    def setUp(self):
        self.ns = load_close_logic()
        self.ns["_session_ending"] = lambda: False
        self.api = self.ns["DesktopWindowApi"]()
        self.api._app_loaded = True

    def close(self, win):
        self.api._window = win
        return self.ns["_ask_before_closing"](win, self.api)

    def test_the_x_opens_the_dialog_and_keeps_the_window(self):
        win = FakeWindow(answer=True)
        self.assertFalse(self.close(win))
        self.assertFalse(win.destroyed.wait(0.5))
        self.assertIn("accurettaRequestQuit", win.asked[0])
        self.assertFalse(self.api._allow_close)

    def test_closes_the_app_means_go_straight_through(self):
        self.api._allow_close = True
        win = FakeWindow()
        self.assertTrue(self.close(win))
        self.assertEqual(win.asked, [])

    def test_before_the_app_loads_the_x_just_closes(self):
        self.api._app_loaded = False
        self.assertTrue(self.close(FakeWindow()))

    def test_a_windows_shutdown_is_never_held_up(self):
        self.ns["_session_ending"] = lambda: True
        self.assertTrue(self.close(FakeWindow()))

    def test_a_page_that_cannot_show_the_dialog_closes_anyway(self):
        for win in (FakeWindow(answer=False), FakeWindow(answer=None), FakeWindow(raises=True)):
            with self.subTest(answer=win.answer, raises=win.raises):
                self.api._allow_close = False
                self.assertFalse(self.close(win))
                self.assertTrue(win.destroyed.wait(1.0))
                self.assertTrue(self.api._allow_close)

    def test_a_frozen_page_closes_after_two_seconds(self):
        win = FakeWindow(answer=True, delay=5.0)
        self.assertFalse(self.close(win))
        self.assertTrue(win.destroyed.wait(3.0))

    def test_the_dialog_buttons_close_for_real(self):
        win = FakeWindow()
        self.api._window = win
        self.api.close_window()
        self.assertTrue(self.api._allow_close)
        self.assertTrue(win.destroyed.wait(1.0))


class WiringTests(unittest.TestCase):
    def test_window_hooks(self):
        self.assertIn("win.events.closing += lambda: _ask_before_closing(win, desktop_api)", SOURCE)
        boot = SOURCE[SOURCE.index("win.load_url(f\"http://127.0.0.1:{PORT}\")"):]
        self.assertIn("desktop_api._app_loaded = True", boot[:200])
        # the bridge stopping closes the window without asking again
        watch = SOURCE[SOURCE.index("def _watch_bridge("):SOURCE.index("_APP_ICON =")]
        self.assertEqual(watch.count("api._allow_close = True"), watch.count("win.destroy()"))

    def test_page_answers_and_hides_the_power_button(self):
        app = frontend_source()
        css = (bridge.ROOT / "workbench.css").read_text(encoding="utf-8")
        self.assertIn("window.accurettaRequestQuit = () => { openShutdown(); return true; };", app)
        self.assertIn('window.addEventListener("pywebviewready", markDesktopApp);', app)
        self.assertIn("body.in-desktop-app #btn-shutdown-side { display: none; }", css)


if __name__ == "__main__":
    unittest.main()
