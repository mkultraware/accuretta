import contextvars
import copy
import json
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from conversation_tools import SteeringInbox, search_conversations


class SteeringTests(unittest.TestCase):
    def setUp(self):
        self.inbox = SteeringInbox()
        self.inbox.begin("chat")

    def test_fifo_and_duplicate_delivery(self):
        update = {"id": "first", "text": "Use the sidebar"}
        self.assertEqual(self.inbox.submit("chat", update), "pending")
        self.assertEqual(self.inbox.submit("chat", update), "pending")
        self.inbox.submit("chat", {"id": "second", "text": "Keep the colors"})
        self.assertEqual([item["id"] for item in self.inbox.take("chat")], ["first", "second"])
        self.inbox.applied("chat", "first")
        self.assertEqual(self.inbox.submit("chat", update), "applied")
        self.assertFalse(self.inbox.pending("chat"))

    def test_final_boundary_either_receives_update_or_rejects_it(self):
        self.inbox.submit("chat", {"id": "first", "text": "Wait"})
        self.assertTrue(self.inbox.take("chat", close_if_empty=True))
        self.assertEqual(self.inbox.submit("chat", {"id": "second", "text": "New detail"}), "pending")
        self.inbox.take("chat")
        self.assertEqual(self.inbox.take("chat", close_if_empty=True), [])
        self.assertEqual(self.inbox.submit("chat", {"id": "late", "text": "Too late"}), "inactive")

    def test_stopped_task_returns_undelivered_updates(self):
        self.inbox.submit("chat", {"id": "first", "text": "Wait"})
        self.assertEqual(len(self.inbox.finish("chat")), 1)
        self.assertEqual(self.inbox.status("chat", "first"), "deferred")
        self.assertEqual(self.inbox.status("other-chat", "first"), "unknown")

    def test_inflight_tool_completes_but_following_tool_is_skipped(self):
        entered, release = threading.Event(), threading.Event()
        executed = []
        def invoke(name, args):
            executed.append(name)
            entered.set()
            release.wait(2)
            return {"ok": True}
        with bridge_patch("_steering", self.inbox), bridge_patch("invoke_tool", side_effect=invoke):
            token = bridge._current_chat_id.set("chat")
            try:
                context = contextvars.copy_context()
                worker = threading.Thread(target=lambda: context.run(bridge._invoke_tool_with_context, "read_file", {}, "first"))
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    self.inbox.submit("chat", {"id": "update", "text": "Use another file"})
                    skipped = bridge._invoke_tool_with_context("write_file", {}, "second")
                    self.assertTrue(skipped["not_executed"])
                    self.assertEqual(executed, ["read_file"])
                finally:
                    release.set()
                    worker.join(3)
            finally:
                bridge._current_chat_id.reset(token)

    def test_real_agent_loop_restarts_generation_with_latest_user_update(self):
        requests, events = [], []
        inbox = SteeringInbox()
        class Response:
            def __init__(self, first):
                self.first = first
            def __iter__(self):
                if self.first:
                    inbox.submit("steering-loop-test", {"id": "update", "text": "Use the sidebar instead", "images": []})
                content = "Outdated answer" if self.first else "Updated sidebar answer"
                yield ("data: " + json.dumps({"choices": [{"delta": {"content": content}, "finish_reason": None}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'
            def close(self):
                pass
        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            return Response(len(requests) == 1)
        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False}
        mocks = {"_steering": inbox, "get_settings": lambda: settings,
                 "llama_post_stream": stream, "_llama_props_ctx": lambda: 32768,
                 "_conversation_token_scale": lambda *a, **kw: 1,
                 "_record_model_observation": lambda *a, **kw: None,
                 "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
                 "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
                 "_turn_journal_begin": lambda *a: "test-turn",
                 "_turn_journal_checkpoint": lambda *a, **kw: None,
                 "get_chats": lambda: {"chats": {}, "order": []}}
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            result = bridge.run_chat_turn("steering-loop-test", [{"role": "user", "content": "Fix the header"}], False, events.append)
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["content"], "Updated sidebar answer")
        self.assertTrue(any(message.get("content") == "Use the sidebar instead" for message in requests[1]["messages"]))
        self.assertFalse(any(event.get("content") == "Outdated answer" for event in events))
        self.assertEqual(inbox.status("steering-loop-test", "update"), "applied")
        self.assertTrue(any(message.get("_steering_id") == "update" for message in result["_appended_intermediate"]))


class ConversationSearchTests(unittest.TestCase):
    def setUp(self):
        self.chats = {"chats": {"a": {"title": "Website work", "updated": 2, "messages": [
            {"role": "system", "content": "Private instruction CORS"},
            {"role": "user", "content": "Fix the menu"},
            {"role": "assistant", "content": "Inspecting sidebar.css", "_internal": True},
            {"role": "tool", "content": '{"error":"CORS request failed"}'},
            {"role": "assistant", "content": "The request header is fixed."},
        ]}, "b": {"title": "Older work", "updated": 1, "messages": [
            {"role": "user", "content": [{"type": "text", "text": "CORS error"}, {"type": "image_url", "image_url": {"url": "private-image"}}]},
        ]}}}

    def test_tool_error_matches_and_points_to_visible_reply(self):
        results = search_conversations(self.chats, "cors")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["chat_id"], "a")
        self.assertEqual(results[0]["message_index"], 3)
        self.assertEqual(results[0]["visible_index"], 1)
        self.assertTrue(results[0]["internal"])

    def test_filenames_and_multiple_terms(self):
        self.assertEqual(search_conversations(self.chats, "sidebar.css")[0]["message_index"], 2)
        self.assertEqual(len(search_conversations(self.chats, "cors failed")), 1)

    def test_empty_queries_images_and_result_limits(self):
        self.assertEqual(search_conversations(self.chats, ""), [])
        self.assertEqual(search_conversations(self.chats, "private-image"), [])
        self.assertEqual(len(search_conversations(self.chats, "cors", limit=1)), 1)


if __name__ == "__main__":
    unittest.main()


class CompletionRecoveryTests(unittest.TestCase):
    def run_responses(self, responses, notebook=None):
        requests, events = [], []
        class Response:
            def __init__(self, content, reason):
                self.content, self.reason = content, reason
            def __iter__(self):
                yield ('data: ' + json.dumps({'choices': [{'delta': {'content': self.content}, 'finish_reason': None}]})).encode()
                yield ('data: ' + json.dumps({'choices': [{'delta': {}, 'finish_reason': self.reason}]})).encode()
                yield b'data: [DONE]'
            def close(self):
                pass
        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            return Response(*responses[len(requests) - 1])
        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False}
        mocks = {"_steering": SteeringInbox(), "get_settings": lambda: settings,
                 "llama_post_stream": stream, "_llama_props_ctx": lambda: 32768,
                 "_conversation_token_scale": lambda *a, **kw: 1,
                 "_record_model_observation": lambda *a, **kw: None,
                 "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
                 "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
                 "_turn_journal_begin": lambda *a: "test-turn",
                 "_turn_journal_checkpoint": lambda *a, **kw: None,
                 "get_chats": lambda: {"chats": {}, "order": []}}
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            if notebook is not None:
                stack.enter_context(bridge_patch("_research_context", return_value=("steering-loop-test", "research-id")))
                stack.enter_context(patch.object(bridge._research_store, "get", return_value=notebook))
            result = bridge.run_chat_turn("steering-loop-test", [{"role": "user", "content": "Research the topic"}], False, events.append)
        return result, events, requests

    def test_empty_output_recovers_after_token_limit_without_failure_event(self):
        result, events, requests = self.run_responses([('', 'length'), ('Finished successfully.', 'stop')])
        self.assertEqual(len(requests), 2)
        self.assertEqual(result['content'], 'Finished successfully.')
        self.assertFalse(result.get('_failed'))
        self.assertFalse(any(e['type'] == 'error' for e in events))
        self.assertFalse(any('did not produce a final response' in str(r) for r in requests))

    def test_empty_output_at_token_limit_does_not_continue_after_giving_up(self):
        # v1.0.2: once the empty-reply retries are spent the turn ends with one
        # error; the token-limit continuation must not ask a fourth time.
        result, events, requests = self.run_responses([('', 'length')] * 3 + [('Too late.', 'stop')])
        self.assertEqual(len(requests), 3)
        self.assertTrue(result['_failed'])
        self.assertEqual(sum(e['type'] == 'error' for e in events), 1)

    def test_exhausted_recovery_reports_one_terminal_failure(self):
        result, events, requests = self.run_responses([('', 'stop')] * 3)
        self.assertEqual(len(requests), 3)
        self.assertTrue(result['_failed'])
        self.assertEqual(sum(e['type'] == 'error' for e in events), 1)

    def test_published_research_finishes_without_requiring_model_epilogue(self):
        result, events, requests = self.run_responses([('', 'length')],
            {'status': 'complete', 'presentation': {'title': 'Saved presentation'}})
        self.assertEqual(len(requests), 1)
        self.assertIn('presentation is ready', result['content'])
        self.assertFalse(result.get('_failed'))
        self.assertFalse(any(e['type'] == 'error' for e in events))
