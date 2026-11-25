"""After a fold the wire conversation must still carry a user message:
Qwen-family templates raise "No user query found in messages" (HTTP 500)
when everything after the system prompt is assistant calls and tool results,
which is what a long one-shot agent turn looks like once its request is
folded into the summary. Mid-turn steering updates must survive the fold."""
import unittest
from unittest.mock import patch

import bridge
from _source import app_source
from _patching import bridge_patch


def _tool_round(i):
    return [
        {"role": "assistant", "content": "", "_internal": True,
         "tool_calls": [{"id": f"c{i}", "type": "function",
                         "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "content": f"result {i}", "tool_call_id": f"c{i}", "name": "read_file"},
    ]


class EnsureUserAnchorTests(unittest.TestCase):
    def test_restores_the_folded_request(self):
        stored = [{"role": "user", "content": "Audit the wallet gateway and write AUDIT.md"}]
        for i in range(3):
            stored += _tool_round(i)
        wire = [{"role": "system", "content": "sys"}] + [
            bridge._replay_wire_msg(m, False) for m in stored[3:]]
        out = bridge._ensure_user_anchor(wire, stored, 3)
        self.assertEqual(out[0]["role"], "system")
        self.assertEqual(out[1]["role"], "user")
        self.assertIn("Audit the wallet gateway and write AUDIT.md", out[1]["content"])
        self.assertEqual(out[2:], wire[1:])

    def test_leaves_a_conversation_with_a_user_message_alone(self):
        wire = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        self.assertIs(bridge._ensure_user_anchor(wire, [], 0), wire)

    def test_fallback_and_cap(self):
        wire = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "x"}]
        self.assertIn("Continue the task", bridge._ensure_user_anchor(wire, [], 0)[1]["content"])
        huge = [{"role": "user", "content": "A" * 50_000}]
        anchored = bridge._ensure_user_anchor(wire, huge, 1)[1]["content"]
        self.assertLess(len(anchored), bridge._USER_ANCHOR_MAX_CHARS + 300)

    def test_every_history_rebuild_goes_through_it(self):
        src = app_source()
        self.assertEqual(src.count("_ensure_user_anchor(msgs, chat[\"messages\"], _replay_from)"), 1)
        self.assertEqual(src.count("_ensure_user_anchor(msgs, chat[\"messages\"], replay_from)"), 1)
        self.assertEqual(src.count("rebuilt = _ensure_user_anchor(rebuilt, chat[\"messages\"], new_through)"), 1)


class MidTurnFoldTests(unittest.TestCase):
    def test_fold_keeps_steering_and_a_user_message(self):
        request = {"role": "user", "content": "Build the PoC, then write AUDIT.md"}
        chat = {"id": "c1", "messages": [request], "summary_through": 0, "rolling_summary": ""}
        chats = {"chats": {"c1": chat}, "order": ["c1"]}
        conversation = [{"role": "system", "content": "sys"}, bridge._replay_wire_msg(request, False)]
        start_len = len(conversation)
        for i in range(6):
            conversation += _tool_round(i)
        steer = bridge._replay_wire_msg({"role": "user", "content": "please stay local"}, False)
        steer.update(_steering_id="s1", _steering_text="please stay local", _steering_images=[])
        conversation.append(steer)
        conversation += _tool_round(6)

        def fold(c, ctx_limit, force=False, reason_hint="auto"):
            # fold the request and the first rounds, like a real mid-turn fold
            c["summary_through"] = 5
            c["rolling_summary"] = "summary"
            return True

        with bridge_patch("get_chats", return_value=chats), \
             bridge_patch("save_json"), \
             bridge_patch("_maybe_roll_summary", side_effect=fold):
            out = bridge._mid_turn_fold("c1", conversation, start_len, 32768, False)
        persisted = [m for m in chat["messages"] if m.get("_steering_id") == "s1"]
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["content"], "please stay local")
        roles = [m["role"] for m in out["conversation"]]
        self.assertIn("user", roles[1:])
        contents = [m.get("content") for m in out["conversation"] if m["role"] == "user"]
        self.assertIn("please stay local", contents)


if __name__ == "__main__":
    unittest.main()
