"""Desktop tools the agent can actually see and act with.

In a computer-use run (2026-11-19) the agent answered 1 of 9 puzzles in 20 min:
- `screenshot` returned base64 text the result cap cut to 4,000 characters;
- `describe_screen` captured the whole main monitor, mostly Accuretta's own
  maximised window, shrunk to 1600 px, so page text was unreadable;
- there was no drag and no "open this URL", so it wrote its own mouse scripts,
  and typed a new URL into a stale tab.
These tests pin the replacements.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import bridge
from _patching import bridge_patch


class FakeWindow:
    def __init__(self, hwnd, title, box=(100, 50, 800, 600), active=False):
        self._hWnd = hwnd
        self.title = title
        self.left, self.top, self.width, self.height = box
        self.isActive = active
        self.isMinimized = False
        self.activated = 0

    def activate(self):
        self.activated += 1
        self.isActive = True

    def restore(self):
        self.isMinimized = False


class CoordinateTests(unittest.TestCase):
    def test_vision_positions_become_screen_coordinates(self):
        frame = {"x": 1330, "y": 108, "scale": 0.5, "width": 800, "height": 600}
        text = bridge._screen_coords_in_text("The Done button is at @(400, 300); logo @( 10 ,20 ).", frame)
        self.assertIn("screen (2130, 708)", text)
        self.assertIn("screen (1350, 148)", text)
        self.assertNotIn("@(", text)

    def test_text_without_positions_is_unchanged(self):
        frame = {"x": 0, "y": 0, "scale": 1.0}
        self.assertEqual(bridge._screen_coords_in_text("no positions here", frame), "no positions here")


class CaptureTargetTests(unittest.TestCase):
    def setUp(self):
        self.acc = FakeWindow(1, "Accuretta", active=True)
        self.edge = FakeWindow(2, "Desk - Microsoft Edge")
        self.pgw = MagicMock()
        self.pgw.getAllWindows.return_value = [self.acc, self.edge]
        self.pgw.getActiveWindow.return_value = self.acc
        self.stack = [bridge_patch("_HAVE_PGW", True),
                      bridge_patch("_pgw", self.pgw, create=True)]
        for p in self.stack:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.stack])
        bridge._remember_desktop_target(None)
        self.addCleanup(bridge._remember_desktop_target, None)

    def test_never_targets_accuretta_itself(self):
        self.assertIsNone(bridge._capture_target_window())

    def test_uses_the_window_the_agent_works_in(self):
        bridge._remember_desktop_target(self.edge)
        self.assertIs(bridge._capture_target_window(), self.edge)

    def test_a_closed_working_window_is_not_used(self):
        bridge._remember_desktop_target(FakeWindow(99, "gone"))
        self.pgw.getActiveWindow.return_value = self.edge
        self.assertIs(bridge._capture_target_window(), self.edge)

    def test_named_window_by_title_or_id(self):
        with bridge_patch("_find_window", lambda s: self.edge if "edge" in s.lower() else None):
            self.assertIs(bridge._capture_target_window("Edge"), self.edge)
        self.assertIs(bridge._capture_target_window("hwnd:2"), self.edge)


@unittest.skipUnless(bridge._HAVE_PIL, "Pillow isn't installed")
class ScreenToolTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.img = Image.new("RGB", (800, 600), "white")
        self.frame = {"x": 1330, "y": 108, "scale": 1.0, "width": 800, "height": 600,
                      "window": "Desk - Microsoft Edge"}
        for p in (bridge_patch("_desktop_preflight", lambda *a: None),
                  bridge_patch("_grab_view", lambda args: (self.img, dict(self.frame))),
                  bridge_patch("_vision_observation_available", lambda: True)):
            p.start()
            self.addCleanup(p.stop)

    def test_screenshot_saves_a_file_and_never_returns_base64(self):
        with tempfile.TemporaryDirectory() as tmp, \
                bridge_patch("_SCREEN_SHOT_DIR", Path(tmp)):
            result = bridge.tool_screenshot({})
            self.assertTrue(result["ok"])
            self.assertTrue(os.path.isfile(result["path"]))
            self.assertNotIn("image_b64", json.dumps(result))
            self.assertEqual(result["frame"]["x"], 1330)

    def test_describe_screen_asks_for_positions_and_converts_them(self):
        seen = {}

        def describe(b64, hint="", question=False):
            seen["hint"], seen["question"] = hint, question
            return "The Done button is at @(100, 50)."
        with bridge_patch("describe_image", describe), \
                bridge_patch("_content_looks_blank", lambda img: False):
            result = bridge.tool_describe_screen({"hint": "where is Done?"})
        self.assertIn("where is Done?", seen["hint"])
        self.assertIn("800x600", seen["hint"])
        self.assertTrue(seen["question"])           # answered, not a whole-page description
        self.assertIn("screen (1430, 158)", result["description"])
        with bridge_patch("describe_image", describe), \
                bridge_patch("_content_looks_blank", lambda img: False):
            bridge.tool_describe_screen({})
        self.assertFalse(seen["question"])

    def test_vision_prompt_answers_a_question_instead_of_describing(self):
        sent = {}

        def post(path, payload, base=None, timeout=0):
            sent["text"] = payload["messages"][0]["content"][1]["text"]
            return {"choices": [{"message": {"content": "37"}}]}
        with bridge_patch("llama_post", post):
            bridge.describe_image("aGk=", hint="What number is shown?", question=True)
            self.assertTrue(sent["text"].startswith("Answer the question"))
            self.assertIn("Question: What number is shown?", sent["text"])
            bridge.describe_image("aGk=", hint="the user's photo")
            self.assertTrue(sent["text"].startswith("Describe this image."))

    def test_view_image_reads_its_own_screenshots_and_refuses_the_data_folder(self):
        with tempfile.TemporaryDirectory() as tmp, \
                bridge_patch("_SCREEN_SHOT_DIR", Path(tmp)), \
                bridge_patch("describe_image", lambda b64, hint="", question=False: "a white box"):
            shot = bridge.tool_screenshot({})["path"]
            self.assertEqual(bridge.tool_view_image({"path": shot, "question": "what?"})["description"],
                             "a white box")
        data_file = bridge.DATA / "view_image_probe.png"
        data_file.parent.mkdir(parents=True, exist_ok=True)
        self.img.save(data_file)
        self.addCleanup(lambda: data_file.unlink(missing_ok=True))
        self.assertIn("error", bridge.tool_view_image({"path": str(data_file)}))


class DragAndOpenTests(unittest.TestCase):
    def test_drag_presses_moves_and_releases(self):
        gui = MagicMock()
        with bridge_patch("pyautogui", gui, create=True), \
                bridge_patch("_gate_action", lambda *a, **kw: None), \
                bridge_patch("_reassert_desktop_target", lambda: None), \
                bridge_patch("_capture_desktop_target", lambda: None), \
                bridge_patch("_desktop_post_state", lambda: {}):
            result = bridge.tool_desktop_drag({"x1": 10, "y1": 20, "x2": 300, "y2": 400})
        self.assertTrue(result["ok"])
        names = [c[0] for c in gui.method_calls]
        self.assertEqual(names[0], "moveTo")
        self.assertLess(names.index("mouseDown"), names.index("mouseUp"))
        self.assertEqual(gui.moveTo.call_args_list[-1].args[:2], (300, 400))

    def test_drag_and_open_url_are_desktop_input_kinds(self):
        self.assertIn("desktop.drag", bridge._DESKTOP_INPUT_KINDS)
        self.assertIn("desktop.open_url", bridge._DESKTOP_INPUT_KINDS)
        for name in ("desktop_drag", "desktop_open_url", "view_image"):
            self.assertIn(name, bridge._DESKTOP_TOOL_NAMES)
            self.assertIn(name, bridge.TOOLS)

    def test_open_url_only_takes_web_addresses(self):
        self.assertIn("error", bridge.tool_desktop_open_url({"url": "file:///C:/x.png"}))
        self.assertIn("error", bridge.tool_desktop_open_url({"url": "javascript:alert(1)"}))

    def test_open_url_opens_a_new_window_and_makes_it_the_working_window(self):
        acc = FakeWindow(1, "Accuretta", active=True)
        new = FakeWindow(7, "Desk - Personal - Microsoft Edge")
        pgw = MagicMock()
        calls = {"n": 0}

        def all_windows():
            calls["n"] += 1
            return [acc] if calls["n"] == 1 else [acc, new]
        pgw.getAllWindows.side_effect = all_windows
        pgw.getActiveWindow.return_value = acc
        launched = []
        bridge._remember_desktop_target(None)
        self.addCleanup(bridge._remember_desktop_target, None)
        with bridge_patch("_pgw", pgw, create=True), \
                bridge_patch("_HAVE_PGW", True), \
                bridge_patch("_desktop_preflight", lambda *a: None), \
                bridge_patch("_gate_action", lambda *a, **kw: None), \
                bridge_patch("_default_browser_exe", lambda: r"C:\Edge\msedge.exe"), \
                patch.object(bridge.subprocess, "Popen", lambda argv, **kw: launched.append(argv)):
            result = bridge.tool_desktop_open_url({"url": "https://example.test/desk/1", "wait_s": 2})
        self.assertEqual(launched, [[r"C:\Edge\msedge.exe", "--new-window", "https://example.test/desk/1"]])
        self.assertEqual(result["window"], new.title)
        self.assertEqual(new.activated, 1)
        with bridge._desktop_target_lock:
            self.assertIs(bridge.STATE.desktop_target_window, new)


if __name__ == "__main__":
    unittest.main()
