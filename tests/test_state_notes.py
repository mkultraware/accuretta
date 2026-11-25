"""The task-state note stays where it was sent.

It used to be rebuilt every round and attached to the newest tool result only,
so the previous result lost its copy every round. That rewrote earlier prompt
text each round, and hybrid models (Qwen3.5/3.6/3.8) then re-read from their
last context checkpoint, often 8K+ tokens back. Now notes stay put and a new
one is added only when the text changes, so within a task each request is an
exact extension of the previous one.
"""
import unittest

import bridge

HEADER = "[task state from the harness: follow it]"
VERIFY = "Verification: capture a reproducing check before editing."
PLAN = "=== ACTIVE PLAN ===\n- active: step one"
LEDGER = "=== RECENT TOOL LEDGER (harness-recorded outcomes) ===\n- ls ok"


def tool(i):
    return {"role": "tool", "tool_call_id": f"c{i}", "content": f"r{i}"}


def asst(i):
    return {"role": "assistant", "content": f"step {i}",
            "tool_calls": [{"id": f"c{i}", "function": {"name": "ls", "arguments": "{}"}}]}


class StateNotesTests(unittest.TestCase):
    def run_rounds(self, sections_per_round):
        notes = bridge._StateNotes()
        conv = [{"role": "system", "content": "sys"}, {"role": "user", "content": "do it"}]
        sent = []
        for r, sections in enumerate(sections_per_round):
            if r:
                conv += [asst(r), tool(r)]
            sent.append(notes.apply(list(conv), HEADER, sections, "user", r))
        return notes, sent

    def assert_append_only(self, sent):
        for earlier, later in zip(sent, sent[1:]):
            self.assertEqual(later[:len(earlier)], earlier)

    def test_unchanged_note_stays_put_and_is_refreshed_periodically(self):
        every = bridge._STATE_NOTE_REFRESH_EVERY
        _, sent = self.run_rounds([[VERIFY]] * (2 * every + 1))
        self.assert_append_only(sent)
        # Placed at round 0, then re-added every few rounds near the end.
        self.assertEqual(sum(HEADER in str(m.get("content")) for m in sent[-1]), 3)
        self.assertEqual(sum(HEADER in str(m.get("content")) for m in sent[every - 1]), 1)
        # At turn start it is its own message right after the user's ask.
        self.assertEqual(sent[-1][2], {"role": "user", "content": HEADER + "\n" + VERIFY})

    def test_changed_note_rides_the_newest_tool_result_and_old_ones_stay(self):
        plan2 = "=== ACTIVE PLAN ===\n- done: step one\n- active: step two"
        _, sent = self.run_rounds([[VERIFY, PLAN], [VERIFY, PLAN], [VERIFY, plan2], [VERIFY, plan2]])
        self.assert_append_only(sent)
        last = sent[-1]
        notes = [m for m in last if HEADER in str(m.get("content"))]
        self.assertEqual(len(notes), 2)
        self.assertIn("step two", notes[1]["content"])
        self.assertEqual(notes[1]["role"], "tool")          # inside a tool result, never its own message mid-task
        self.assertTrue(notes[1]["content"].startswith("r2\n\n" + HEADER))

    def test_ledger_alone_refreshes_every_few_rounds(self):
        rounds = [[VERIFY, f"{LEDGER} {i}"] for i in range(9)]
        _, sent = self.run_rounds(rounds)
        self.assert_append_only(sent)
        n_notes = sum(HEADER in str(m.get("content")) for m in sent[-1])
        # Round 0 places it; the ledger then changes every round but only rides
        # along with the refresh every _STATE_NOTE_REFRESH_EVERY rounds.
        self.assertEqual(n_notes, 1 + (8 // bridge._STATE_NOTE_REFRESH_EVERY))
        self.assertIn(f"{LEDGER} 8", sent[-1][-1]["content"])

    def test_trimmed_anchor_drops_its_note_and_a_fresh_one_is_placed(self):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "do it"}, asst(1), tool(1)]
        notes.apply(list(conv), HEADER, [VERIFY], "user", 1)
        # The trimmer dropped the message holding the note.
        conv2 = [{"role": "user", "content": "do it"}, asst(2), tool(2)]
        out = notes.apply(conv2, HEADER, [VERIFY], "user", 2)
        self.assertEqual(sum(HEADER in str(m.get("content")) for m in out), 1)
        self.assertIn(HEADER, out[-1]["content"])

    def test_pile_up_is_capped(self):
        big = "x " * 2000
        rounds = [[VERIFY, f"=== ACTIVE PLAN ===\n{big}{i}"] for i in range(12)]
        notes, sent = self.run_rounds(rounds)
        self.assertLessEqual(notes.kept_tokens(),
                             bridge._STATE_NOTE_KEEP_TOKENS + bridge._approx_tokens(big) + 200)

    def test_no_new_note_while_a_tool_call_is_carried(self):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "do it"}, asst(1), tool(1)]
        first = notes.apply(list(conv), HEADER, [VERIFY], "user", 1)
        carry = {"role": "assistant", "content": "<tool_call>partial"}
        out = notes.apply(conv + [carry], HEADER, [VERIFY, PLAN], "user", 2, allow_new=False)
        self.assertIs(out[-1], carry)                       # the unfinished call stays last
        self.assertEqual(out[:len(first)], first)           # the earlier note stays put

    def test_no_sections_sends_nothing(self):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "chat"}]
        self.assertEqual(notes.apply(list(conv), HEADER, [], "user", 0), conv)


if __name__ == "__main__":
    unittest.main()
