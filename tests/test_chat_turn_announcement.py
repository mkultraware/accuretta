"""A chat turn must always announce its answer on the wire.

run_chat_turn returns a reply from several paths that never emit the `final`
event themselves: a cancel seen at the top of a round, a cancel that lands
mid-stream, and the catch-all that salvages a turn after an unexpected
exception. The reply is still persisted, so without an announcement the client
received `chat_start` ... `chat_end` and nothing in between - the surface sat on
its work pill and then collapsed to the idle dot with no answer and no error.

These tests pin both halves of the contract: a turn that already announced its
`final` must not announce it twice, and a turn that returned a reply silently
must have it announced for it.
"""
import json
import socket
import threading
import time
import unittest
import urllib.request
from queue import Empty

import bridge
from _patching import bridge_patch

_LOOPBACK = ".".join(("127", "0", "0", "1"))


def _sse_events(raw: str):
    out = []
    for chunk in raw.split("\n\n"):
        line = next((l for l in chunk.split("\n") if l.startswith("data: ")), None)
        if not line:
            continue
        try:
            out.append(json.loads(line[6:]))
        except ValueError:
            continue
    return out


class SilentTurnIsAnnouncedTests(unittest.TestCase):
    """Drives the real POST /api/chat handler with run_chat_turn stubbed."""

    @classmethod
    def setUpClass(cls):
        bridge.Handler.log_message = lambda self, fmt, *a: None
        cls.httpd = bridge.AccurettaHTTPServer((_LOOPBACK, 0), bridge.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.chats = {"chats": {}, "order": []}
        stubs = {
            "get_chats": lambda: self.chats,
            "save_json": lambda *a, **k: None,
            "get_settings": lambda: {
                "model": "stub", "model_path": "", "summarize_history": False,
                "desktop_enabled": False, "red_team_enabled": False,
                "rt_mission_state": False, "max_tool_rounds": 4,
            },
            "broadcast_event": lambda evt: None,
        }
        # bridge_patch reaches the code moved out of bridge.py too.
        for name, stub in stubs.items():
            patcher = bridge_patch(name, stub)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _turn(self, stub):
        patcher = bridge_patch("run_chat_turn", stub)
        patcher.start()
        self.addCleanup(patcher.stop)
        body = json.dumps({"message": "hello?", "mode": "agent"}).encode()
        req = urllib.request.Request(
            f"http://{_LOOPBACK}:{self.port}/api/chat", data=body,
            headers={"Content-Type": "application/json",
                     "X-Accuretta-Token": bridge._REQUEST_TOKEN})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return _sse_events(resp.read().decode("utf-8"))

    def test_silent_return_is_announced_as_final(self):
        """A reply returned without a `final` event reaches the client anyway."""
        def silent_return(chat_id, messages, use_tools, emit, **kw):
            # exactly what the cancel / exception paths do: return a message and
            # emit nothing more.
            return {"role": "assistant", "content": "here, what do you need?",
                    "_turn_id": "", "_build": "", "_appended_intermediate": [],
                    "_stats": {"eval_count": 4}}

        events = self._turn(silent_return)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1,
                         "the salvaged reply must be announced exactly once")
        self.assertIn("what do you need", finals[0]["message"]["content"])
        self.assertEqual(len([e for e in events if e.get("type") == "chat_end"]), 1)

    def test_already_announced_final_is_not_repeated(self):
        """The normal path emits `final` itself; the client must see one, not two."""
        def announces_itself(chat_id, messages, use_tools, emit, **kw):
            emit({"type": "final", "message": {"role": "assistant", "content": "hello!"}})
            return {"role": "assistant", "content": "hello!", "_turn_id": "",
                    "_build": "", "_appended_intermediate": [], "_stats": {}}

        events = self._turn(announces_itself)
        finals = [e for e in events if e.get("type") == "final"]
        self.assertEqual(len(finals), 1, "final must never be duplicated")

    def test_announced_payload_carries_no_tool_working_memory(self):
        """The announcement is what a surface renders, not the persisted blob."""
        def silent_return(chat_id, messages, use_tools, emit, **kw):
            return {"role": "assistant", "content": "answer", "_turn_id": "t1",
                    "_build": "b", "_stats": {"eval_count": 2},
                    "_appended_intermediate": [{"role": "tool", "content": "x" * 500}]}

        events = self._turn(silent_return)
        msg = [e for e in events if e.get("type") == "final"][0]["message"]
        self.assertNotIn("_appended_intermediate", msg)
        self.assertNotIn("_turn_id", msg)
        self.assertEqual(msg["content"], "answer")

    def test_no_reply_at_all_still_ends_the_turn(self):
        """A turn that produced nothing announces nothing but still terminates."""
        def nothing(chat_id, messages, use_tools, emit, **kw):
            return None

        events = self._turn(nothing)
        self.assertEqual([e for e in events if e.get("type") == "final"], [])
        self.assertEqual(len([e for e in events if e.get("type") == "chat_end"]), 1)


class ChatEndReachesTheBusTests(unittest.TestCase):
    """Stop aborts the page's own fetch while the turn still runs. A page or the
    notch mirroring that turn ends it only on chat_end from the shared bus, so
    chat_end must go out even though the requesting page can no longer be
    written to. It used to be lost: emit wrote to the page first and raised."""

    @classmethod
    def setUpClass(cls):
        bridge.Handler.log_message = lambda self, fmt, *a: None
        cls.httpd = bridge.AccurettaHTTPServer((_LOOPBACK, 0), bridge.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _start_and_leave(self, chat_id, turn, leave_after: bytes):
        """POST a chat turn, read until `leave_after` shows up, then drop the
        connection like a page that pressed Stop. Returns a bus subscription."""
        chats = {"chats": {}, "order": []}
        stubs = {
            "get_chats": lambda: chats,
            "save_json": lambda *a, **k: None,
            "get_settings": lambda: {
                "model": "stub", "model_path": "", "summarize_history": False,
                "desktop_enabled": False, "red_team_enabled": False,
                "rt_mission_state": False, "max_tool_rounds": 4,
            },
            "run_chat_turn": turn,
        }
        for name, stub in stubs.items():
            patcher = bridge_patch(name, stub)
            patcher.start()
            self.addCleanup(patcher.stop)
        q, _ = bridge.subscribe()
        self.addCleanup(bridge.unsubscribe, q)
        body = json.dumps({"message": "hello?", "mode": "agent", "chat_id": chat_id}).encode()
        head = (f"POST /api/chat HTTP/1.1\r\nHost: {_LOOPBACK}:{self.port}\r\n"
                "Content-Type: application/json\r\n"
                f"X-Accuretta-Token: {bridge._REQUEST_TOKEN}\r\nContent-Length: {len(body)}\r\n\r\n")
        sock = socket.create_connection((_LOOPBACK, self.port), timeout=5)
        sock.sendall(head.encode() + body)
        got = b""
        while leave_after not in got:
            chunk = sock.recv(65536)
            if not chunk:
                break
            got += chunk
        self.assertIn(leave_after, got)
        sock.close()
        return q

    def _bus_until_end(self, q, chat_id):
        seen = []
        deadline = time.time() + 15
        while time.time() < deadline and "chat_end" not in seen:
            try:
                evt = q.get(timeout=0.5)
            except Empty:
                continue
            if evt.get("chat_id") == chat_id:
                seen.append(evt.get("type"))
        return seen

    def test_page_gone_before_the_turn_runs(self):
        q = self._start_and_leave("gone-early", lambda *a, **k: None, b"chat_start")
        seen = self._bus_until_end(q, "gone-early")
        self.assertIn("chat_start", seen)
        self.assertEqual(seen.count("chat_end"), 1, f"bus saw {seen[-8:]}")

    def test_page_gone_mid_turn(self):
        page_gone = threading.Event()
        write_failed = threading.Event()

        def turn(chat_id, messages, use_tools, emit, **kw):
            emit({"type": "delta", "content": "first-chunk"})
            page_gone.wait(5)
            for _ in range(200):          # a write to the closed socket fails soon
                try:
                    emit({"type": "delta", "content": "x" * 200})
                except OSError:
                    write_failed.set()
                    raise
                time.sleep(0.01)
            return None

        q = self._start_and_leave("gone-mid", turn, b"first-chunk")
        page_gone.set()
        seen = self._bus_until_end(q, "gone-mid")
        self.assertTrue(write_failed.is_set(), f"no write failed; bus saw {seen[-8:]}")
        self.assertIn("delta", seen)
        self.assertEqual(seen.count("chat_end"), 1, f"bus saw {seen[-8:]}")


if __name__ == "__main__":
    unittest.main()