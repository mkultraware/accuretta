"""Long unattended runs: the /goal round checkpoint and folds that always commit.

A 3-hour /goal run stopped three times at the 120-round cap, each time
dropping the call the model had just written, and 11 of its 20 folds
committed nothing: the summary had grown into the summarizer's output cap, so
the model handed the prior note back unchanged. These tests pin the fixes:

- under /goal the round cap is a checkpoint: the same turn carries on while
  the last stretch made progress, and stops as before when it did not;
- the turn tells the model when its round budget is about to run out, and how
  long the goal has been running, inside the task-state note;
- the fold note's growing lists are kept by the bridge, bounded, and a model
  that fails or copies its input still lets the fold move forward.
"""
import copy
import json
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import bridge
from _patching import bridge_patch

CTX = 65536


class GoalRoundCheckpointTests(unittest.TestCase):
    """Drives the real run_chat_turn loop with a scripted model."""

    def run_turn(self, *, goal: bool, tool: str, result: dict, calls: int,
                 max_rounds: int = 4, chat: dict | None = None):
        chat_id = "goal-rounds"
        requests, notices, executed = [], [], []

        class Response:
            def __init__(self, i):
                self.i = i

            def __iter__(self):
                if self.i <= calls:
                    delta = {"content": f"Step {self.i}.",
                             "tool_calls": [{"index": 0, "id": f"c{self.i}", "function": {
                                 "name": tool,
                                 "arguments": json.dumps({"code": f"print({self.i})"})}}]}
                else:
                    delta = {"content": "All steps finished."}
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
                yield b'data: [DONE]'

            def close(self):
                pass

        def stream(path, payload):
            requests.append(copy.deepcopy(payload))
            return Response(len(requests))

        def invoke(name, args, *a, **kw):
            executed.append(name)
            return dict(result)

        settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": CTX,
                    "summarize_history": False, "red_team_enabled": False,
                    "max_tool_rounds": max_rounds}
        chats = {"chats": {chat_id: chat} if chat else {}, "order": []}
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
            "get_chats": lambda: chats,
            "_mid_turn_fold": lambda *a, **kw: None,
            "_maybe_roll_summary": lambda *a, **kw: False,
            "invoke_tool": invoke,
        }
        with ExitStack() as stack:
            for name, value in mocks.items():
                stack.enter_context(bridge_patch(name, value))
            stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {chat_id: 0}))
            stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
            bridge._set_goal_mode(chat_id, goal)
            try:
                final = bridge.run_chat_turn(
                    chat_id, [{"role": "user", "content": "Run every step."}], True,
                    lambda e: notices.append(e) if e.get("type") == "notice" else None)
            finally:
                bridge._set_goal_mode(chat_id, False)
        return final, requests, notices, executed

    RUN_OK = {"ok": True, "exit_code": 0, "stdout": "1\n"}

    def test_goal_turn_carries_on_past_the_cap_while_making_progress(self):
        final, requests, notices, executed = self.run_turn(
            goal=True, tool="run_python", result=self.RUN_OK, calls=10)
        self.assertEqual(len(executed), 10, "every call the model wrote must run")
        self.assertIn("All steps finished", final["content"])
        renewals = [n for n in notices if "/goal:" in str(n.get("note"))]
        self.assertEqual(len(renewals), 2)       # at round 4 and at round 8

    def test_without_goal_the_cap_still_ends_the_turn(self):
        final, requests, notices, executed = self.run_turn(
            goal=False, tool="run_python", result=self.RUN_OK, calls=10)
        self.assertEqual(len(executed), 4)
        self.assertIn("tool-round limit", final["content"])

    def test_goal_stretch_of_only_looking_around_is_not_renewed(self):
        final, requests, notices, executed = self.run_turn(
            goal=True, tool="read_file", result={"ok": True, "content": "x"}, calls=10)
        self.assertEqual(len(executed), 4)
        self.assertIn("tool-round limit", final["content"])

    def test_goal_stretch_of_failures_is_not_renewed(self):
        final, requests, notices, executed = self.run_turn(
            goal=True, tool="run_python",
            result={"ok": False, "exit_code": 1, "error": "Traceback: boom"}, calls=10)
        self.assertEqual(len(executed), 4)

    def test_round_budget_warning_rides_in_the_state_note(self):
        final, requests, notices, executed = self.run_turn(
            goal=False, tool="run_python", result=self.RUN_OK, calls=10)
        early = json.dumps(requests[1]["messages"])
        late = json.dumps(requests[2]["messages"])        # 2 rounds left
        self.assertNotIn("ROUND BUDGET", early)
        self.assertIn("2 tool rounds left", late)
        # It sits inside the newest tool result, never as its own message.
        last = requests[2]["messages"][-1]
        self.assertEqual(last["role"], "tool")
        self.assertIn("ROUND BUDGET", last["content"])
        self.assertIn("No tool rounds are left", json.dumps(requests[4]["messages"]))

    def test_goal_turn_that_will_renew_gets_no_budget_warning(self):
        final, requests, notices, executed = self.run_turn(
            goal=True, tool="run_python", result=self.RUN_OK, calls=10)
        self.assertNotIn("ROUND BUDGET", json.dumps(requests))

    def test_goal_clock_is_in_the_state_note_not_the_system_prompt(self):
        chat = {"id": "goal-rounds", "messages": [], "goal_mode": True,
                "goal_started": int(time.time()) - 95 * 60}
        final, requests, notices, executed = self.run_turn(
            goal=True, tool="run_python", result=self.RUN_OK, calls=2, chat=chat)
        system = requests[0]["messages"][0]["content"]
        self.assertNotIn("GOAL CLOCK", system)
        self.assertIn("running for 1 h 35 min", json.dumps(requests[1]["messages"]))
        # Same system prompt on every request, so the cached prefix survives.
        self.assertEqual(len({r["messages"][0]["content"] for r in requests}), 1)


def run_scripted(script, *, goal: bool, plan=None, invoke_results=None, cancel_after=None,
                 max_rounds: int = 40, stream_errors=None, extra_mocks=None):
    """Drive run_chat_turn with one scripted reply per model request.

    Each step is {"tool": name} (a run_python-style call), {"text": "..."} (a
    plain reply) or {"think": "..."} (a reply with only thinking). Past the
    script the model answers "All steps finished." `invoke_results` maps the
    1-based tool call number to its result (default: a successful run)."""
    chat_id = "goal-scripted"
    requests, notices = [], []
    calls = {"n": 0}
    cancel = {"ev": None}

    class Response:
        def __init__(self, i):
            self.i = i

        def __iter__(self):
            step = script[self.i - 1] if self.i <= len(script) else {"text": "All steps finished."}
            if "tool" in step:
                delta = {"content": f"Step {self.i}.", "tool_calls": [{"index": 0, "id": f"s{self.i}",
                         "function": {"name": step["tool"],
                                      "arguments": json.dumps({"code": f"print({self.i})"})}}]}
            elif "think" in step:
                delta = {"content": f"<think>{step['think']}</think>"}
            else:
                delta = {"content": step["text"]}
            yield ("data: " + json.dumps({"choices": [{"delta": delta}]})).encode()
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield b'data: [DONE]'

        def close(self):
            pass

    errors = dict(stream_errors or {})
    attempts = {"n": 0}

    def stream(path, payload):
        attempts["n"] += 1
        if attempts["n"] in errors:
            raise errors[attempts["n"]]
        requests.append(copy.deepcopy(payload))
        if cancel_after and len(requests) >= cancel_after and cancel["ev"] is not None:
            cancel["ev"].set()
        return Response(len(requests))

    def invoke(name, args, *a, **kw):
        calls["n"] += 1
        return dict((invoke_results or {}).get(calls["n"], {"ok": True, "exit_code": 0, "stdout": "1"}))

    real_register = bridge._register_cancel

    def register(cid):
        cancel["ev"] = real_register(cid)
        return cancel["ev"]

    chat = {"id": chat_id, "messages": [], "goal_mode": goal,
            "plan": plan if plan is not None else []}
    chats = {"chats": {chat_id: chat}, "order": []}
    settings = {**bridge.DEFAULT_SETTINGS, "model": "test-model", "num_ctx": CTX,
                "summarize_history": False, "red_team_enabled": False,
                "max_tool_rounds": max_rounds}
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
        "get_chats": lambda: chats,
        "_mid_turn_fold": lambda *a, **kw: None,
        "_maybe_roll_summary": lambda *a, **kw: False,
        "invoke_tool": invoke,
        "_register_cancel": register,
        **(extra_mocks or {}),
    }
    with ExitStack() as stack:
        for name, value in mocks.items():
            stack.enter_context(bridge_patch(name, value))
        stack.enter_context(patch.dict(bridge._last_prompt_tokens_by_chat, {chat_id: 0}))
        stack.enter_context(patch.object(bridge._llama, "loaded_model", return_value="test-model"))
        bridge._set_goal_mode(chat_id, goal)
        try:
            final = bridge.run_chat_turn(
                chat_id, [{"role": "user", "content": "Do the plan."}], True,
                lambda e: notices.append(e) if e.get("type") == "notice" else None)
        finally:
            bridge._set_goal_mode(chat_id, False)
    plan_notices = [n for n in notices if "plan still has" in str(n.get("note"))]
    return final, requests, notices, plan_notices


OPEN_PLAN = [{"title": "Fix the parser", "status": "done"},
             {"title": "Solve target2", "status": "active"},
             {"title": "Post the debrief", "status": "pending"}]


class GoalPlanCheckpointTests(unittest.TestCase):
    """A /goal turn whose reply would end it while the plan has open steps."""

    def test_reply_with_open_plan_continues_once_after_progress(self):
        script = [{"tool": "run_python"}, {"tool": "run_python"}, {"text": "Here is where I am."},
                  {"text": "I need your input to go on."}]
        final, requests, notices, plan_notices = run_scripted(script, goal=True, plan=OPEN_PLAN)
        self.assertEqual(len(plan_notices), 1)
        self.assertEqual(len(requests), 4)
        note = requests[3]["messages"][-1]
        self.assertIn("plan has open steps: Solve target2; Post the debrief", note["content"])
        # The nudge is an automatic note, not a new task: reasoning is kept.
        self.assertFalse(bridge._is_task_boundary(note))
        # No progress after the nudge, so the second reply ends the turn.
        self.assertIn("I need your input", final["content"])

    def test_without_goal_the_reply_ends_the_turn(self):
        script = [{"tool": "run_python"}, {"text": "Here is where I am."}]
        final, requests, notices, plan_notices = run_scripted(script, goal=False, plan=OPEN_PLAN)
        self.assertEqual(plan_notices, [])
        self.assertEqual(len(requests), 2)

    def test_finished_plan_or_no_plan_ends_the_turn(self):
        done = [dict(p, status="done") for p in OPEN_PLAN]
        for plan in (done, []):
            script = [{"tool": "run_python"}, {"text": "Here is where I am."}]
            final, requests, notices, plan_notices = run_scripted(script, goal=True, plan=plan)
            self.assertEqual(plan_notices, [])
            self.assertEqual(len(requests), 2)

    def test_reply_without_progress_since_turn_start_ends_the_turn(self):
        script = [{"tool": "read_file"}, {"text": "Here is where I am."}]
        final, requests, notices, plan_notices = run_scripted(script, goal=True, plan=OPEN_PLAN)
        self.assertEqual(plan_notices, [])
        self.assertEqual(len(requests), 2)

    def test_thinking_only_stop_is_a_checkpoint_once_then_stops(self):
        script = ([{"tool": "run_python"}]
                  + [{"think": "draft of the emulator " * 20}] * 3     # guard: 2 retries, then stop
                  + [{"think": "still drafting " * 20}] * 3)
        final, requests, notices, plan_notices = run_scripted(script, goal=True, plan=OPEN_PLAN)
        self.assertEqual(len(plan_notices), 1)
        self.assertEqual(len(requests), 7)
        after = requests[4]
        self.assertFalse(after["chat_template_kwargs"].get("enable_thinking", True),
                         "the round after the checkpoint runs without thinking")
        # Same placement as the thinking-only retry note (the state note follows it).
        self.assertIn("plan has open steps",
                      " ".join(str(m.get("content")) for m in after["messages"][-2:]))
        self.assertNotIn("draft of the emulator draft", json.dumps(after["messages"]),
                         "a draft written in thinking is never fed back")
        self.assertIn("kept planning in its thinking", final["content"])

    def test_thinking_only_guard_unchanged_on_normal_turns(self):
        script = [{"tool": "run_python"}] + [{"think": "planning " * 20}] * 3
        final, requests, notices, plan_notices = run_scripted(script, goal=False, plan=OPEN_PLAN)
        self.assertEqual(len(requests), 4)
        self.assertIn("kept planning in its thinking", final["content"])

    def test_loop_guard_still_ends_a_goal_turn(self):
        refused = {"loop_breaker": True, "error": "identical call refused", "not_executed": True}
        script = [{"tool": "run_python"}] * 6
        final, requests, notices, plan_notices = run_scripted(
            script, goal=True, plan=OPEN_PLAN, invoke_results={2: refused, 3: refused})
        self.assertTrue(any("loop guard" in str(n.get("note")) for n in notices))
        self.assertEqual(plan_notices, [])
        self.assertLessEqual(len(requests), 4)

    def test_stop_still_ends_a_goal_turn(self):
        script = [{"tool": "run_python"}, {"tool": "run_python"}, {"text": "Here is where I am."}]
        final, requests, notices, plan_notices = run_scripted(
            script, goal=True, plan=OPEN_PLAN, cancel_after=3)
        self.assertEqual(plan_notices, [])
        self.assertEqual(len(requests), 3)


class GoalNoCapTests(unittest.TestCase):
    def test_goal_turn_has_no_fixed_number_of_stretches(self):
        final, requests, notices, executed = GoalRoundCheckpointTests.run_turn(
            GoalRoundCheckpointTests(), goal=True, tool="run_python",
            result=GoalRoundCheckpointTests.RUN_OK, calls=48, max_rounds=4)
        self.assertEqual(len(executed), 48)
        self.assertEqual(len([n for n in notices if "/goal:" in str(n.get("note"))]), 11)
        self.assertIn("All steps finished", final["content"])
        self.assertFalse(hasattr(bridge, "_GOAL_MAX_RENEWALS"))


class GoalClockTests(unittest.TestCase):
    def test_limit_is_read_from_the_task_text(self):
        cases = {
            "All sections share one clock: you have about 3 hours from now for the whole checkup.": 10800,
            "You have about 75 minutes from now.": 4500,
            "finish within 90 minutes": 5400,
            "you have 1 hour and 30 minutes": 5400,
            "within two hours": 7200,
            "time limit is 45 min": 2700,
            "decompile this firmware overnight": 0,
            "I have 3 hours of meetings, so take your time": 0,
            "the log covers 5 min of traffic": 0,
        }
        for text, want in cases.items():
            self.assertEqual(bridge._goal_time_limit(text), want, text)

    def test_clock_works_out_the_time_left(self):
        line = bridge._goal_clock_text(116 * 60, 3 * 3600)
        self.assertIn("1 h 56 min used of the 3 h 00 min", line)
        self.assertIn("about 1 h 04 min left", line)
        self.assertIn("has passed", bridge._goal_clock_text(200 * 60, 3 * 3600))
        self.assertIn("states no time limit", bridge._goal_clock_text(30 * 60, 0))

    def test_limit_from_the_goal_reaches_the_note(self):
        chat = {"id": "goal-rounds", "messages": [], "goal_mode": True,
                "goal_started": int(time.time()) - 116 * 60, "goal_limit_s": 3 * 3600}
        final, requests, notices, executed = GoalRoundCheckpointTests.run_turn(
            GoalRoundCheckpointTests(), goal=True, tool="run_python",
            result=GoalRoundCheckpointTests.RUN_OK, calls=2, chat=chat)
        self.assertRegex(json.dumps(requests[1]["messages"]), r"about 1 h 0[34] min left")


ASK = ("You are taking the checkup: fetch the challenges with "
       "Authorization: Bearer abc123token and solve them one at a time, posting each answer.")


class ContinuityNoteTests(unittest.TestCase):
    def state(self):
        return {"goal": "the checkup", "current_task": "solve target2",
                "verbatim_original_user": bridge._compaction_excerpt(ASK, 1600),
                "recent_user_messages": [bridge._compaction_excerpt(ASK, 900)],
                "verification_required_count": 12,
                "verification_required": [{"path": f"C:\\w\\f{i}.py", "status": "unchecked",
                                           "via": "edit"} for i in range(12)]}

    def test_request_still_in_the_prompt_is_not_repeated(self):
        text = bridge._render_continuity_state(self.state(), 129024, wire_text=ASK)
        self.assertNotIn("original user request", text)
        self.assertNotIn("recent user instruction", text)

    def test_request_folded_out_of_the_prompt_is_kept(self):
        text = bridge._render_continuity_state(self.state(), 129024,
                                               wire_text="continue where you left off")
        self.assertIn("original user request", text)

    def test_goal_lines_go_when_the_objective_carries_them(self):
        shown = bridge._render_continuity_state(self.state(), 129024, objective_shown=True)
        hidden = bridge._render_continuity_state(self.state(), 129024)
        self.assertNotIn("goal: the checkup", shown)
        self.assertIn("goal: the checkup", hidden)

    def test_unverified_files_list_the_latest_few(self):
        text = bridge._render_continuity_state(self.state(), 129024)
        self.assertIn("Unverified files: 12", text)
        self.assertEqual(text.count("verification required:"), bridge._CONTINUITY_VERIFY_PATHS)
        self.assertIn("f11.py", text)


class BriefRefreshTests(unittest.TestCase):
    HEADER = "[task state]"
    FULL = ["=== ACTIVE PLAN ===\n- active: step two", "=== STRUCTURED CONTINUITY ===\n" + "x " * 1500]
    BRIEF = ["=== TASK STATE (unchanged; the full note above still applies) ===\n- active: step two"]

    def run_rounds(self, n, brief):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "do it"}]
        sent = []
        for r in range(n):
            if r:
                conv += [{"role": "assistant", "content": f"s{r}", "tool_calls": [
                             {"id": f"c{r}", "function": {"name": "ls", "arguments": "{}"}}]},
                         {"role": "tool", "tool_call_id": f"c{r}", "content": f"r{r}"}]
            sent.append(notes.apply(list(conv), self.HEADER, self.FULL, "user", r, brief=brief))
        return notes, sent

    def test_unchanged_refresh_is_brief_while_a_full_note_stays(self):
        every, full_every = bridge._STATE_NOTE_REFRESH_EVERY, bridge._STATE_NOTE_FULL_EVERY
        notes, sent = self.run_rounds(full_every + 1, self.BRIEF)
        for earlier, later in zip(sent, sent[1:]):
            self.assertEqual(later[:len(earlier)], earlier)       # the prompt only grows
        bodies = [m["content"] for m in sent[-1] if self.HEADER in str(m.get("content"))]
        full = [b for b in bodies if "STRUCTURED CONTINUITY" in b]
        brief = [b for b in bodies if "TASK STATE (unchanged" in b]
        self.assertEqual(len(full), 2)                            # round 0 and round FULL_EVERY
        self.assertEqual(len(brief), full_every // every - 1)
        self.assertLess(notes.kept_tokens(), bridge._STATE_NOTE_KEEP_TOKENS)

    def test_without_brief_the_refresh_is_the_whole_note_as_before(self):
        notes, sent = self.run_rounds(bridge._STATE_NOTE_REFRESH_EVERY + 1, None)
        bodies = [m["content"] for m in sent[-1] if self.HEADER in str(m.get("content"))]
        self.assertTrue(all("STRUCTURED CONTINUITY" in b for b in bodies))

    def test_changed_note_is_sent_whole(self):
        notes = bridge._StateNotes()
        conv = [{"role": "user", "content": "do it"}]
        notes.apply(list(conv), self.HEADER, self.FULL, "user", 0, brief=self.BRIEF)
        conv += [{"role": "assistant", "content": "s", "tool_calls": [
                     {"id": "c1", "function": {"name": "ls", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "r1"}]
        changed = ["=== ACTIVE PLAN ===\n- active: step three", self.FULL[1]]
        out = notes.apply(list(conv), self.HEADER, changed, "user", 1, brief=self.BRIEF)
        self.assertIn("STRUCTURED CONTINUITY", out[-1]["content"])
        self.assertIn("step three", out[-1]["content"])


class NoThinkPrefillTests(unittest.TestCase):
    """A round that must not think keeps the prompt prefix when it can."""

    PREFILL = "<think>\n\n</think>\n\n"

    def retry_request(self, prefill):
        script = [{"tool": "run_python"}, {"think": "planning " * 30}, {"text": "Done."}]
        with bridge_patch("_no_think_prefill", lambda kw: prefill):
            final, requests, notices, plan_notices = run_scripted(script, goal=False)
        return requests[1], requests[2]

    def test_template_that_needs_it_gets_the_prefill(self):
        normal, retry = self.retry_request(self.PREFILL)
        self.assertEqual(retry["messages"][-1], {"role": "assistant", "content": self.PREFILL})
        self.assertEqual(retry["continue_final_message"], "content")
        self.assertFalse(retry["add_generation_prompt"])
        # The template flags are the turn's own, so the system block is unchanged.
        self.assertEqual(retry.get("chat_template_kwargs", {}).get("enable_thinking"),
                         normal.get("chat_template_kwargs", {}).get("enable_thinking"))

    def test_other_templates_switch_thinking_off_as_before(self):
        normal, retry = self.retry_request("")
        self.assertIs(retry["chat_template_kwargs"]["enable_thinking"], False)
        self.assertNotIn("continue_final_message", retry)
        self.assertNotEqual(retry["messages"][-1].get("role"), "assistant")

    def detect(self, on, off):
        def post(path, payload, timeout=None):
            think = payload["chat_template_kwargs"]["enable_thinking"]
            return {"prompt": on if think else off}
        bridge._no_think_prefill_cache.clear()
        with bridge_patch("llama_post", post), \
                patch.object(bridge._llama, "is_running", return_value=True), \
                patch.object(bridge._llama, "loaded_model", return_value="m"):
            return bridge._no_think_prefill({"enable_thinking": True})

    def test_no_probe_without_a_server_this_bridge_runs(self):
        def post(*a, **kw):
            raise AssertionError("must not contact a llama-server it did not start")
        with bridge_patch("llama_post", post), \
                patch.object(bridge._llama, "is_running", return_value=False):
            self.assertEqual(bridge._no_think_prefill({"enable_thinking": True}), "")

    def test_detection(self):
        s, u = bridge._NO_THINK_PROBE_SYS, bridge._NO_THINK_PROBE_USER
        head = f"<|im_start|>system\nEffort line.\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n"
        plain = f"<|im_start|>system\n{s}<|im_end|>\n<|im_start|>user\n{u}<|im_end|>\n"
        # Qwen3.8-like: the switch rewrites the system block; no-think is an empty block.
        self.assertEqual(self.detect(head + "<|im_start|>assistant\n<think>\n",
                                     plain + "<|im_start|>assistant\n<think>\n\n</think>\n\n"),
                         self.PREFILL)
        # The switch only changes the reply start: toggling is already cheap.
        self.assertEqual(self.detect(plain + "<|im_start|>assistant\n<think>\n",
                                     plain + "<|im_start|>assistant\n<think>\n\n</think>\n\n"), "")
        # No think block in the no-think form: unknown dialect, keep the old way.
        self.assertEqual(self.detect(head + "<|start|>assistant",
                                     plain + "<|start|>assistant<|channel|>final"), "")
        bridge._no_think_prefill_cache.clear()


class ModelLoadingTests(unittest.TestCase):
    """A turn that arrives while llama-server is (re)loading the model waits
    for it; two short retries used to fail the turn after ~6 s."""

    @staticmethod
    def loading():
        import io
        import urllib.error
        body = b'{"error":{"message":"Loading model","type":"unavailable_error","code":503}}'
        return urllib.error.HTTPError("http://127.0.0.1:8080/v1/chat/completions", 503,
                                      "Service Unavailable", {}, io.BytesIO(body))

    def test_turn_waits_for_the_model_and_finishes(self):
        polls = {"n": 0}

        def ready(timeout=2.0, base_url=None):
            polls["n"] += 1
            return polls["n"] >= 2
        final, requests, notices, _ = run_scripted(
            [{"text": "Done."}], goal=False,
            stream_errors={1: self.loading(), 2: self.loading()},
            extra_mocks={"llama_ready": ready, "time": type("T", (), {
                "time": staticmethod(__import__("time").time),
                "sleep": staticmethod(lambda s: None),
                "monotonic": staticmethod(__import__("time").monotonic),
                "strftime": staticmethod(__import__("time").strftime)})})
        self.assertEqual(final["content"], "Done.")
        self.assertTrue(any("still loading" in str(n.get("note")) for n in notices))

    def test_other_503s_are_not_mistaken_for_loading(self):
        import io
        import urllib.error
        other = urllib.error.HTTPError("u", 503, "busy", {}, io.BytesIO(b"slot unavailable"))
        self.assertFalse(bridge._is_model_loading(other))
        self.assertTrue(bridge._is_model_loading(self.loading()))


class ToolErrorKindTests(unittest.TestCase):
    """The usage dataset records why a tool failed as a fixed label, never text."""

    def test_labels(self):
        cases = {
            "denied": {"error": "user denied action (timeout)"},
            "edit_mismatch": {"error": "edit 1: 'def x' not found in file. Closest lines in the file now"},
            "blocked_path": {"error": "path outside workspace. Add folder in Workspace panel."},
            "rewrite_loop": {"error": "rewrite loop blocked: you have already rewritten this file"},
            "timeout": {"error": "browser worker timed out"},
            "nonzero_exit": {"ok": False, "exit_code": 2, "stderr": ""},
            "other": {"ok": False},
        }
        for want, result in cases.items():
            self.assertEqual(bridge._tool_error_kind(result), want, result)
        self.assertEqual(bridge._tool_error_kind({"ok": True, "exit_code": 0}), "")

    def test_dataset_keeps_the_label_and_never_the_text(self):
        out = bridge._sanitize_tool_outcomes([
            {"name": "write_file", "ok": False, "error_kind": "blocked_path", "exit_code": 0,
             "error": r"C:\secret\path not allowed"},
            {"name": "x", "ok": False, "error_kind": r"C:\leak"}])
        self.assertEqual(out[0]["error_kind"], "blocked_path")
        self.assertNotIn("secret", json.dumps(out))
        self.assertNotIn("error_kind", out[1])


class GoalHelperTests(unittest.TestCase):
    def test_open_steps(self):
        self.assertEqual(bridge._goal_open_steps(OPEN_PLAN), ["Solve target2", "Post the debrief"])
        self.assertEqual(bridge._goal_open_steps(None), [])

    def test_progress_rules(self):
        ok = {"name": "run_python", "ok": True, "refused": False}
        read = {"name": "read_file", "ok": True, "refused": False}
        bad = {"name": "run_python", "ok": False, "refused": False}
        refused = {"name": "run_python", "ok": True, "refused": True}
        self.assertTrue(bridge._goal_window_progressed([ok, read, bad]))
        self.assertFalse(bridge._goal_window_progressed([]))
        self.assertFalse(bridge._goal_window_progressed([read] * 5))
        self.assertFalse(bridge._goal_window_progressed([ok, bad, bad]))
        self.assertFalse(bridge._goal_window_progressed([ok, ok, refused]))

    def test_elapsed_text(self):
        self.assertEqual(bridge._goal_elapsed_text(30), "0 min")
        self.assertEqual(bridge._goal_elapsed_text(61 * 60), "1 h 01 min")


def _tool_round(i, name, args, result):
    return [
        {"role": "assistant", "content": f"step {i}",
         "tool_calls": [{"id": f"t{i}", "type": "function",
                         "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": f"t{i}", "name": name, "content": result},
    ]


PRIOR = "\n".join([
    "## ACTIVE TASK:", "Decode the X16 CPU.",
    "## COMPLETED:", *[f"- solved part {i}" for i in range(40)],
    "## ROADBLOCKS:", "- opcode 0x59 unknown",
    "## DECISIONS AND CONSTRAINTS:", "- submit each answer right away",
    "## FILES IN USE:", *[f"- C:\\work\\f{i}.py (written)" for i in range(30)],
    "## VERIFICATION:", *[f"- run_python: check {i} -> exit 0" for i in range(30)],
    "## NEXT ACTION:", "- fit opcode 0x59",
])

SLICE = (
    _tool_round(1, "write_file", {"path": "C:\\work\\emu.py", "content": "x"},
                '{"ok": true, "path": "C:\\\\work\\\\emu.py"}')
    + _tool_round(2, "run_python", {"code": "import sys\nfrom emu import run\nrun('probe1')"},
                  "[exit 1 | cwd C:\\work]\npowershell_exit_code: 0\n[stderr]\n"
                  "Traceback (most recent call last):\n  File \"x\", line 1\nKeyError: 'R9'")
    + _tool_round(3, "run_python", {"code": "print('ok')"}, "[exit 0 | cwd C:\\work]\n0 mismatches")
)

MODEL_BLOCK = "\n".join([
    "## ACTIVE TASK:", "Decode the X16 CPU; emulator now matches probe1.",
    "## NEW PROGRESS:", "- 0x59 = sign-extend shift, verified 0/1836 mismatches",
    "## ROADBLOCKS:", "- None recorded.",
    "## DECISIONS AND CONSTRAINTS:", "- submit each answer right away",
    "## NEXT ACTION:", "- run the emulator on target1 and submit",
])


class FoldNoteTests(unittest.TestCase):

    def fold(self, replies, prior=PRIOR):
        sent = []

        def post(path, payload, timeout=None):
            sent.append(payload)
            text = replies[min(len(sent), len(replies)) - 1]
            return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}

        outcome = {}
        with bridge_patch("llama_post", post), \
                bridge_patch("broadcast_event", lambda *a, **kw: None), \
                bridge_patch("_log_compact", lambda *a, **kw: None):
            summary = bridge._update_rolling_summary(prior, SLICE, chat_id="c", notify=False,
                                                     ctx_limit=CTX, outcome=outcome)
        return summary, sent, outcome

    def test_model_writes_short_part_and_bridge_keeps_bounded_lists(self):
        summary, sent, outcome = self.fold([MODEL_BLOCK])
        ok, why = bridge._validate_summary_output(summary)
        self.assertTrue(ok, why)
        sections = bridge._split_summary_sections(summary)
        completed = bridge._summary_bullets(sections["## COMPLETED:"])
        self.assertEqual(len(completed), bridge._SUMMARY_KEEP_COMPLETED)
        self.assertIn("0x59 = sign-extend", completed[-1])
        files = bridge._summary_bullets(sections["## FILES IN USE:"])
        self.assertEqual(len(files), bridge._SUMMARY_KEEP_FILES)
        self.assertEqual(files[-1], "C:\\work\\emu.py (written)")
        checks = bridge._summary_bullets(sections["## VERIFICATION:"])
        self.assertEqual(len(checks), bridge._SUMMARY_KEEP_CHECKS)
        self.assertIn("exit 1: KeyError: 'R9'", checks[-2])
        self.assertIn("run_python: run('probe1') … ->", checks[-2])   # skips the imports
        self.assertIn("exit 0: 0 mismatches", checks[-1])
        self.assertIn("target1", sections["## NEXT ACTION:"])
        self.assertEqual(outcome["model_fallbacks"], 0)
        # The model never sees the growing lists, only the short sections.
        request = sent[0]["messages"][1]["content"]
        self.assertNotIn("check 3 ->", request)
        self.assertNotIn("f3.py", request)
        self.assertNotIn("solved part 3\n", request)

    def test_note_size_stays_bounded_over_many_folds(self):
        summary = PRIOR
        for _ in range(30):
            summary, _, _ = self.fold([MODEL_BLOCK.replace("verified", f"verified {_}")], summary)
        self.assertLess(len(summary), 9000)

    def test_copied_prior_is_rejected_and_the_fold_still_moves_forward(self):
        copied = "\n".join(["## ACTIVE TASK:", "Decode the X16 CPU.",
                            "## NEW PROGRESS:", "- None recorded.",
                            "## ROADBLOCKS:", "- opcode 0x59 unknown",
                            "## DECISIONS AND CONSTRAINTS:", "- submit each answer right away",
                            "## NEXT ACTION:", "- fit opcode 0x59"])
        summary, sent, outcome = self.fold([copied, copied])
        self.assertEqual(len(sent), 2, "one repair attempt, then the fallback")
        self.assertGreater(sent[1]["temperature"], 0, "the repair must not run greedy")
        self.assertEqual(outcome["model_fallbacks"], 1)
        sections = bridge._split_summary_sections(summary)
        self.assertIn("emu.py", sections["## FILES IN USE:"])
        self.assertIn("carried from an earlier fold", sections["## NEXT ACTION:"])
        self.assertNotEqual(summary, PRIOR)

    def test_legacy_seven_section_reply_is_accepted(self):
        legacy = PRIOR.replace("- fit opcode 0x59", "- run target1")
        summary, sent, outcome = self.fold([legacy])
        self.assertEqual(outcome["model_fallbacks"], 0)
        self.assertIn("run target1", summary)

    def test_identical_note_still_advances_the_fold_point(self):
        msgs = [{"role": "user", "content": "go"}] + SLICE * 4
        chat = {"id": "fold-commit", "messages": msgs, "summary_through": 0,
                "rolling_summary": PRIOR}
        with bridge_patch("_update_rolling_summary", lambda old, *a, **kw: old), \
                bridge_patch("_record_fold_event", lambda *a, **kw: None), \
                bridge_patch("broadcast_event", lambda *a, **kw: None), \
                bridge_patch("_log_compact", lambda *a, **kw: None):
            moved = bridge._maybe_roll_summary_unlocked(chat, CTX, force=True, reason_hint="mid_turn")
        self.assertTrue(moved)
        self.assertGreater(chat["summary_through"], 0)


if __name__ == "__main__":
    unittest.main()
