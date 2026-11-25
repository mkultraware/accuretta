"""Lifetime token counter (savings.json, the Usage page's "tokens processed").

It must count the FULL prompt of every request (llama-server's prompt_n skips
the cached prefix, which undercounted ~5x), turns that were stopped or failed,
and the side generations: sub-agents, compaction summaries, image descriptions.
"""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch


def _chunk(obj):
    return ("data: " + json.dumps(obj)).encode()


class _Stream:
    """A llama-server SSE stream: some content, then a final chunk with
    timings, or a dropped connection part-way."""
    def __init__(self, text, timings=None, die=False):
        self.text, self.timings, self.die = text, timings, die

    def __iter__(self):
        yield _chunk({"choices": [{"delta": {"content": self.text}, "finish_reason": None}]})
        if self.die:
            raise ConnectionError("llama-server went away")
        final = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        if self.timings:
            final["timings"] = self.timings
        yield _chunk(final)
        yield b"data: [DONE]"

    def close(self):
        pass


class ChatTurnCounterTests(unittest.TestCase):
    def run_turn(self, response):
        added = []
        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": 32768,
                    "summarize_enabled": False, "red_team_enabled": False}
        mocks = {"get_settings": lambda: settings,
                 "llama_post_stream": lambda path, payload: copy.deepcopy(response),
                 "_llama_props_ctx": lambda: 32768,
                 "_conversation_token_scale": lambda *a, **kw: 1,
                 "_record_model_observation": lambda *a, **kw: None,
                 "_savings_add": lambda ti, to, **kw: added.append((ti, to, kw)) or
                     {"tok_in": ti, "tok_out": to, "turns": 1, "since": 0},
                 "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
                 "_turn_journal_begin": lambda *a: "test-turn",
                 "_turn_journal_checkpoint": lambda *a, **kw: None,
                 "get_chats": lambda: {"chats": {}, "order": []}}
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            bridge.run_chat_turn("token-counter-test", [{"role": "user", "content": "Hi"}],
                                 False, lambda evt: None)
        return added

    def test_full_prompt_counts_the_cached_prefix(self):
        added = self.run_turn(_Stream("Hello there.", {"prompt_n": 120, "cache_n": 9000,
                                                       "predicted_n": 40}))
        self.assertEqual(len(added), 1, "a finished turn is counted exactly once")
        tok_in, tok_out, kw = added[0]
        self.assertEqual(tok_in, 9120)
        self.assertEqual(tok_out, 40)
        self.assertNotEqual(kw.get("count_turn"), False)

    def test_without_cache_n_the_payload_size_is_the_floor(self):
        added = self.run_turn(_Stream("Hello there.", {"prompt_n": 3, "predicted_n": 40}))
        self.assertEqual(len(added), 1)
        self.assertGreater(added[0][0], 3, "the incremental prompt_n alone undercounts")

    def test_a_turn_that_dies_mid_stream_is_still_counted(self):
        added = self.run_turn(_Stream("Partial answer that got cut " * 4, die=True))
        self.assertEqual(len(added), 1, "stopped / failed turns used to vanish from the counter")
        tok_in, tok_out, _ = added[0]
        self.assertGreater(tok_in, 0, "the prompt was prefilled before the stream died")
        self.assertGreater(tok_out, 0, "the partial output was generated")


class SideCallCounterTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        patcher = bridge_patch("SAVINGS_FILE", tmp / "savings.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        # The counter cache lives in accuretta.core.savings, which rebinds it.
        cache_patch = bridge_patch("_savings_cache", None)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)

    def test_usage_counts_tokens_but_not_a_turn(self):
        bridge._savings_note_completion({"usage": {"prompt_tokens": 5000, "completion_tokens": 300}})
        totals = bridge._savings_get()
        self.assertEqual((totals["tok_in"], totals["tok_out"], totals["turns"]), (5000, 300, 0))

    def test_timings_fallback_includes_the_cache(self):
        bridge._savings_note_completion({"timings": {"cache_n": 4000, "prompt_n": 25,
                                                     "predicted_n": 60}})
        totals = bridge._savings_get()
        self.assertEqual((totals["tok_in"], totals["tok_out"]), (4025, 60))

    def test_garbage_never_raises(self):
        for junk in (None, [], "x", {"usage": {"prompt_tokens": "lots"}}):
            bridge._savings_note_completion(junk)
        self.assertEqual(bridge._savings_get()["tok_in"], 0)

    def test_llama_post_counts_completions_only(self):
        class _Resp:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(self.body).encode()
        body = {"usage": {"prompt_tokens": 700, "completion_tokens": 20}, "choices": []}
        with patch.object(bridge.urllib.request, "urlopen", lambda req, timeout=0: _Resp(body)):
            bridge.llama_post("/v1/chat/completions", {}, base="http://127.0.0.1:1")
            bridge.llama_post("/tokenize", {}, base="http://127.0.0.1:1")
        self.assertEqual(bridge._savings_get()["tok_in"], 700)

    def test_sub_agents_are_counted(self):
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self):
                return json.dumps({"usage": {"prompt_tokens": 1200, "completion_tokens": 80},
                                   "choices": [{"message": {"content": "done"}}]}).encode()
        with patch.object(bridge.urllib.request, "urlopen", lambda req, timeout=0: _Resp()), \
                bridge_patch("_set_cancel_resp", lambda *a: None):
            bridge._subagent_complete("sub-test", [{"role": "user", "content": "go"}], [])
        totals = bridge._savings_get()
        self.assertEqual((totals["tok_in"], totals["tok_out"], totals["turns"]), (1200, 80, 0))


if __name__ == "__main__":
    unittest.main()
