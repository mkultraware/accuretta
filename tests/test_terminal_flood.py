"""A flood of command output no longer freezes the chat (2026-11-22).

An agent's search ran over a folder with images in it and streamed thousands
of lines of undecodable bytes (shown as "?" in diamonds). app.js added each
line to the chat's terminal on its own, read the pane's height after every
line and never trimmed the console, so each line cost more than the last and
the window froze for seconds. Now the bridge shows a run of binary lines as
one note, and the page writes queued lines once per frame and keeps only the
newest part of the console.
"""
import re
import unittest

import bridge
from _source import frontend_source

APP = frontend_source()


def _garbage(n=200):
    return "".join("\ufffd" if j % 3 else chr(33 + j % 90) for j in range(n))


class LooksBinaryTests(unittest.TestCase):
    def test_undecodable_and_control_bytes(self):
        self.assertTrue(bridge._looks_binary(_garbage()))
        self.assertTrue(bridge._looks_binary("icon.png:3:" + "\x00\x01\x02\x03\x04\x05\x06\x07\x08" * 3))

    def test_ordinary_text_stays(self):
        for line in ("", "short", "PS C:\\> Get-ChildItem -Recurse | Select-String foo",
                     "Sökväg: C:\\Användare\\åäö\tklar", "\x1b[32mPASS\x1b[0m tests/test_x.py::test_y",
                     "one \ufffd in an otherwise normal line of text that is long enough"):
            self.assertFalse(bridge._looks_binary(line), line)


class StreamCollapseTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        bridge._chat_emitters["flood-chat"] = self.events.append
        bridge._STREAM_BINARY_RUNS.clear()

    def tearDown(self):
        bridge._chat_emitters.pop("flood-chat", None)
        bridge._STREAM_BINARY_RUNS.clear()

    def emit(self, text, call="c1"):
        bridge._emit_tool_stream("run_powershell", text, chat_id="flood-chat", target="host", call_id=call)

    def test_a_run_of_binary_lines_is_one_note(self):
        self.emit("searching...")
        for _ in range(500):
            self.emit(_garbage())
        self.emit("done: 3 matches")
        self.emit(_garbage())
        texts = [e["text"] for e in self.events]
        self.assertEqual(texts, ["searching...", "[binary data hidden]", "done: 3 matches", "[binary data hidden]"])

    def test_runs_are_per_tool_call(self):
        self.emit(_garbage(), call="a")
        self.emit(_garbage(), call="b")
        self.emit(_garbage(), call="a")
        self.assertEqual([(e["call_id"], e["text"]) for e in self.events],
                         [("a", "[binary data hidden]"), ("b", "[binary data hidden]")])


class TerminalBatchingTests(unittest.TestCase):
    def body(self, name):
        m = re.search(r"function " + name + r"\([^)]*\) \{(.*?)\r?\n  \}\r?\n", APP, re.S)
        self.assertIsNotNone(m, name)
        return m.group(1)

    def test_lines_are_queued_not_written_one_by_one(self):
        body = self.body("appendTerminalText")
        self.assertNotIn("insertAdjacentHTML", body)
        self.assertNotIn("scrollHeight", body)
        self.assertIn("requestAnimationFrame(flushTerminalText)", body)
        self.assertIn("setTimeout(flushTerminalText", body)   # hidden windows stall animation frames

    def test_one_write_per_flush_and_the_console_is_trimmed(self):
        body = self.body("flushTerminalText")
        self.assertEqual(body.count("insertAdjacentHTML"), 2)   # (before the cursor, or at the end)
        self.assertIn("trimTerminal(", body)
        self.assertRegex(APP, r"const TERM_KEEP_CHARS = \d+;")

    def test_a_burst_bigger_than_the_console_drops_its_oldest_lines_first(self):
        body = self.body("appendTerminalText")
        self.assertIn("_termQueue.shift()", body)
        self.assertIn("trimTerminal(box, cursor, dropped > 0)", self.body("flushTerminalText"))
        self.assertIn("earlier output not shown", self.body("trimTerminal"))


if __name__ == "__main__":
    unittest.main()
