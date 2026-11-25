import time
import threading
import unittest
from queue import Empty, Full

import bridge
from accuretta.core import events   # owns and rebinds _event_log_id


class SlowSubscriberTests(unittest.TestCase):
    """A slow/backgrounded SSE client must never be silently unsubscribed.

    The old behavior removed a subscriber whose bounded queue filled, while
    leaving the HTTP stream open with pings — EventSource never saw an error,
    never reconnected, and every push-only feature (approvals, compaction) went
    dark until a reload.
    """

    def test_full_queue_drops_oldest_and_keeps_subscriber(self):
        q, _ = bridge.subscribe()
        try:
            while True:
                try:
                    q.put_nowait({"type": "filler"})
                except Full:
                    break
            bridge.broadcast_event({"type": "marker", "n": 7})
            self.assertIn(q, bridge._subscribers)
            drained = []
            while True:
                try:
                    drained.append(q.get_nowait())
                except Empty:
                    break
            self.assertEqual(len(drained), q.maxsize)
            self.assertEqual(drained[-1]["type"], "marker")
            self.assertEqual(drained[-1]["n"], 7)
        finally:
            bridge.unsubscribe(q)
            self.assertNotIn(q, bridge._subscribers)


class StreamChatterTests(unittest.TestCase):
    """Token chatter is forwarded live to subscribers but never logged.

    The notch overlay mirrors a turn that is running on another surface, so
    deltas now reach the SSE bus — but as best-effort, unlogged chatter: one
    token per ring-buffer slot would flush approvals out of the 256-event
    resume log in seconds. Under backpressure a token is dropped rather than
    evicting a durable event.
    """

    def test_delta_and_thinking_delta_are_forwarded_but_never_logged(self):
        q, _ = bridge.subscribe()
        try:
            while True:
                try:
                    q.get_nowait()
                except Empty:
                    break
            log_before = len(bridge._event_log)
            id_before = events._event_log_id
            bridge.broadcast_event({"type": "delta", "content": "a"})
            bridge.broadcast_event({"type": "thinking_delta", "content": "b"})
            first = q.get_nowait()
            second = q.get_nowait()
            self.assertEqual(first["type"], "delta")
            self.assertEqual(second["type"], "thinking_delta")
            self.assertNotIn("_id", first)      # transient: never resumeable
            self.assertEqual(len(bridge._event_log), log_before)
            self.assertEqual(events._event_log_id, id_before)

            # A durable event still flows and is logged.
            bridge.broadcast_event({"type": "summary_folded", "chat_id": "c1", "folded": 3})
            self.assertEqual(len(bridge._event_log), log_before + 1)
            self.assertEqual(events._event_log_id, id_before + 1)
        finally:
            bridge.unsubscribe(q)

    def test_chatter_overflow_never_evicts_a_durable_event(self):
        # Token chatter hitting a full queue must be dropped, never evict:
        # an approval sitting behind a flood has to survive it. (Durable
        # events keep the old drop-oldest-keeps-subscriber behaviour.)
        q, _ = bridge.subscribe()
        try:
            marker = {"type": "approval:new", "approval": {"id": "z"}}
            q.put_nowait(marker)
            for _ in range(q.maxsize - 1):
                q.put_nowait({"type": "filler"})
            before = list(q.queue)
            bridge.broadcast_event({"type": "delta", "content": "a"})
            bridge.broadcast_event({"type": "thinking_delta", "content": "b"})
            self.assertIn(q, bridge._subscribers)
            self.assertEqual(before, list(q.queue))
        finally:
            bridge.unsubscribe(q)


class InflightFoldTests(unittest.TestCase):
    """In-flight folds are state: re-emitted so a reconnect redraws the row."""

    def test_list_inflight_folds_is_sorted_and_cleaned_up(self):
        with bridge._summary_fold_lock:
            bridge._summary_folds_inflight.discard("zf-test")
            bridge._summary_folds_inflight.discard("af-test")
            bridge._summary_folds_inflight.add("zf-test")
            bridge._summary_folds_inflight.add("af-test")
        try:
            listed = bridge.list_inflight_folds()
            self.assertIn("af-test", listed)
            self.assertIn("zf-test", listed)
            self.assertEqual(listed, sorted(listed))
        finally:
            with bridge._summary_fold_lock:
                bridge._summary_folds_inflight.discard("zf-test")
                bridge._summary_folds_inflight.discard("af-test")
            self.assertNotIn("zf-test", bridge.list_inflight_folds())
            self.assertNotIn("af-test", bridge.list_inflight_folds())


class SSEReemitTests(unittest.TestCase):
    """Over the real handler, a connect re-emits an in-flight fold."""

    def test_inflight_fold_is_reemitted_on_connect(self):
        class FakeWFile:
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                self.buf.extend(data)

            def flush(self):
                text = self.buf.decode("utf-8", "ignore")
                if "summary_folding" in text and "test:unblock" in text:
                    raise RuntimeError("stop after re-emit")

        class DummyHandler:
            headers = {}

            def send_response(self, *args, **kwargs):
                pass

            def send_header(self, *args, **kwargs):
                pass

            def _set_cors(self):
                pass

            def end_headers(self):
                pass

        handler = DummyHandler()
        handler.wfile = FakeWFile()
        handler._sse_send = lambda obj, evt_id=None: bridge.Handler._sse_send(handler, obj, evt_id)
        with bridge._summary_fold_lock:
            bridge._summary_folds_inflight.add("reemit-test-chat")
        try:
            worker = threading.Thread(
                target=bridge.Handler._serve_sse, args=(handler,), daemon=True)
            worker.start()
            deadline = time.time() + 2.0
            while not bridge._subscribers and time.time() < deadline:
                time.sleep(0.01)
            # Unblock the handler's queue.get; its third write raises and exits.
            bridge.broadcast_event({"type": "test:unblock"})
            worker.join(timeout=2.0)
        finally:
            with bridge._summary_fold_lock:
                bridge._summary_folds_inflight.discard("reemit-test-chat")
        payload = bytes(handler.wfile.buf).decode("utf-8")
        self.assertIn("summary_folding", payload)
        self.assertIn("reemit-test-chat", payload)


if __name__ == "__main__":
    unittest.main()