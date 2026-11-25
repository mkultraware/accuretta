"""A new approval goes down the running turn's own stream as well as the shared
event bus (a remote browser got the shared copy minutes late), and the quit
buttons send JSON the bridge accepts (a raw object never shut it down)."""
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from _source import frontend_source

ROOT = Path(__file__).resolve().parent.parent
ENTRY = {"id": "abc123", "chat_id": "chat-1", "title": "Run a command"}


class AnnounceApproval(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.bus = []
        patcher = bridge_patch("broadcast_event", side_effect=self.bus.append)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(bridge._chat_emitters.pop, "chat-1", None)

    def test_turn_stream_and_bus(self):
        bridge._chat_emitters["chat-1"] = self.sent.append
        bridge._announce_approval(ENTRY)
        self.assertEqual(self.sent, [{"type": "approval_request", "approval": ENTRY}])
        self.assertEqual(self.bus, [{"type": "approval:new", "approval": ENTRY}])

    def test_gone_client_still_broadcasts(self):
        def broken(_evt):
            raise BrokenPipeError
        bridge._chat_emitters["chat-1"] = broken
        bridge._announce_approval(ENTRY)
        self.assertEqual(self.bus, [{"type": "approval:new", "approval": ENTRY}])

    def test_no_turn_broadcasts_only(self):
        bridge._announce_approval(ENTRY)
        self.assertEqual(self.bus, [{"type": "approval:new", "approval": ENTRY}])


class QuitButtons(unittest.TestCase):
    def test_shutdown_bodies_are_json_strings(self):
        js = frontend_source()
        calls = re.findall(r'api\("/api/shutdown",\s*\{[^\n]*', js)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertIn("body: JSON.stringify(", call)


if __name__ == "__main__":
    unittest.main()
