"""Reasoning cleanup before compaction.

Keeping reasoning for the whole task (so the prompt cache is reused) fills the
context faster. When a long task's prompt reaches the compaction trigger, the
app first drops the reasoning of all but the last few rounds and keeps every
tool result, answer and tool call word for word. It compacts only when that
would free too little.
"""
import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import bridge
from _patching import bridge_patch

OFF = {"preserve_prior_thinking": False}


def call(i):
    return [{"id": f"c{i}", "function": {"name": "list_directory", "arguments": "{}"}}]


class CutPointTests(unittest.TestCase):
    def task(self, n=5):
        msgs = [{"role": "user", "content": "Map the API"}]
        for i in range(1, n + 1):
            msgs += [{"role": "assistant", "content": f"<think>plan {i}</think>Step {i}.", "tool_calls": call(i)},
                     {"role": "tool", "tool_call_id": f"c{i}", "content": f"result {i}"}]
        return msgs

    def test_anchor_is_the_third_newest_reply(self):
        msgs = self.task(5)
        anchor = bridge._reasoning_cleanup_anchor(msgs, OFF)
        self.assertIs(anchor, msgs[5])                      # replies at 1,3,5,7,9 -> third newest is 5
        self.assertIsNone(bridge._reasoning_cleanup_anchor(self.task(3), OFF))
        self.assertIsNone(bridge._reasoning_cleanup_anchor(msgs, {"preserve_prior_thinking": True}))

    def test_cut_strips_reasoning_before_it_and_keeps_everything_else(self):
        msgs = self.task(5)
        before = copy.deepcopy(msgs)
        view = bridge._thinking_wire_view(msgs, OFF, keep_reasoning_from=msgs[5])
        replies = [m["content"] for m in view if m["role"] == "assistant"]
        self.assertEqual(replies, ["Step 1.", "Step 2.", "<think>plan 3</think>Step 3.",
                                   "<think>plan 4</think>Step 4.", "<think>plan 5</think>Step 5."])
        self.assertEqual([m["content"] for m in view if m["role"] == "tool"],
                         [f"result {i}" for i in range(1, 6)])
        self.assertEqual([m.get("tool_calls") for m in view], [m.get("tool_calls") for m in msgs])
        self.assertEqual(msgs, before, "stored conversation must not change")

    def test_cut_matches_by_identity_even_when_marker_rounds_are_copied(self):
        msgs = self.task(4)
        # Legacy marker form: the view normalizes (copies) this message.
        msgs[3] = dict(msgs[3], content="<think>plan 2\n responseStep 2.")
        view = bridge._thinking_wire_view(msgs, OFF, keep_reasoning_from=msgs[3])
        self.assertEqual(view[1]["content"], "Step 1.")
        self.assertEqual(view[3]["content"], "<think>plan 2</think>Step 2.")

    def test_unknown_cut_point_changes_nothing(self):
        msgs = self.task(4)
        plain = bridge._thinking_wire_view(msgs, OFF)
        stranger = {"role": "assistant", "content": "not in the list"}
        self.assertEqual(bridge._thinking_wire_view(msgs, OFF, keep_reasoning_from=stranger), plain)


CTX = 65536
# ~4.5K tokens of reasoning per round: after 7 rounds, dropping rounds 1-4
# frees ~18K, over the cleanup bar (25% of 64K = ~16.4K); one round alone does not.
BIG = 13500
FIRST_FULL = 7


class AgentLoopCleanupTests(unittest.TestCase):
    """A long task whose context is reported full after a few rounds: cleanup
    runs instead of compaction when it frees enough, and every tool result stays."""

    def run_turn(self, reasoning_chars, full_from=FIRST_FULL, n_rounds=9):
        requests, folds, notices = [], [], []
        chat_id = "reasoning-cleanup"
        thought = "x" * reasoning_chars

        class Response:
            def __init__(self, i):
                self.i = i

            def __iter__(self):
                if self.i < n_rounds:
                    delta = {"content": f"<think>round {self.i} {thought}</think>Step {self.i}.",
                             "tool_calls": [{"index": 0, "id": f"r{self.i}", "function": {
                                 "name": "list_directory",
                                 "arguments": json.dumps({"path": f"C:/d{self.i}"})}}]}
                else:
                    delta = {"content": "All folders mapped."}
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            # The server reports the prompt at the compaction trigger once the
            # task has run a few rounds (checked at the start of the next one).
            bridge._last_prompt_tokens_by_chat[chat_id] = 10 ** 6 if len(requests) >= full_from else 0
            return Response(len(requests))

        def fold(*a, **kw):
            folds.append(len(requests))
            return None

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": CTX,
                    "summarize_history": True, "red_team_enabled": False,
                    "preserve_prior_thinking": False}
        mocks = {
            "get_settings": lambda: settings, "llama_post_stream": stream,
            "_native_tools_broken": lambda: False, "_mark_native_tools_broken": lambda *a, **kw: None,
            "_rt_trailing_system_ok": lambda: True,
            "_llama_props_ctx": lambda: CTX, "_conversation_token_scale": lambda *a, **kw: 1,
            "_record_model_observation": lambda *a, **kw: None,
            "_savings_add": lambda *a, **kw: {"tok_in": 0, "tok_out": 0, "turns": 0, "since": 0},
            "_undo_begin_turn": lambda *a: None, "_undo_commit_turn": lambda: None,
            "_turn_journal_begin": lambda *a: "test-turn",
            "_turn_journal_checkpoint": lambda *a, **kw: None,
            "get_chats": lambda: {"chats": {}, "order": []},
            "_mid_turn_fold": fold,
            "_maybe_roll_summary": lambda *a, **kw: False,
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {chat_id: 0}))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            stack.enter_context(bridge_patch("invoke_tool", return_value={"ok": True, "entries": ["a"]}))
            bridge.run_chat_turn(chat_id, [{"role": "user", "content": "Map every folder."}], True,
                                 lambda e: notices.append(e) if e.get("type") == "notice" else None)
        return requests, folds, notices

    @staticmethod
    def cleanups(notices):
        return [n for n in notices if "dropped older reasoning" in str(n.get("note"))]

    def test_cleanup_runs_instead_of_compaction_and_keeps_tool_results(self):
        requests, folds, notices = self.run_turn(BIG)
        self.assertEqual(len(self.cleanups(notices)), 1)
        # The first full check comes before request 8 (7 sent): it cleans, not compacts.
        self.assertNotIn(FIRST_FULL, folds)
        after = requests[FIRST_FULL]["messages"]
        sent = json.dumps(after)
        # Rounds 1-4 lost their reasoning; the last 3 rounds kept theirs.
        for i in range(1, 5):
            self.assertNotIn(f"round {i} x", sent)
        for i in range(5, 8):
            self.assertIn(f"round {i} x", sent)
        # Every tool call and tool result is still there, word for word.
        self.assertEqual(sum(1 for m in after if m.get("tool_calls")), FIRST_FULL)
        self.assertEqual(sum(1 for m in after if m["role"] == "tool"), FIRST_FULL)
        # One more round frees too little (a single round), so compaction runs.
        self.assertIn(FIRST_FULL + 1, folds)

    def test_compaction_still_runs_when_cleanup_frees_too_little(self):
        # Tiny reasoning: dropping it would free almost nothing.
        requests, folds, notices = self.run_turn(40)
        self.assertFalse(self.cleanups(notices))
        self.assertIn(FIRST_FULL, folds)

    def test_one_round_of_reasoning_is_not_worth_a_cleanup(self):
        # Full from round 5: only round 1's reasoning (~4.5K) would go, under
        # the 25% bar, so it compacts until enough older reasoning piles up.
        requests, folds, notices = self.run_turn(BIG, full_from=4)
        self.assertEqual(folds[:3], [4, 5, 6])
        self.assertTrue(self.cleanups(notices))

    def test_bar_is_a_quarter_of_the_window(self):
        self.assertEqual(bridge._REASONING_CLEANUP_MIN_FRAC, 0.25)


if __name__ == "__main__":
    unittest.main()
