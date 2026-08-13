import contextvars
import threading
import unittest
import os
import subprocess
import tempfile
import time
import urllib.error
import io
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import bridge


class _FakeStream:
    def __init__(self, events):
        self.events = [
            ("data: " + (event if isinstance(event, str) else __import__("json").dumps(event)) + "\n").encode()
            for event in events
        ]

    def __iter__(self):
        return iter(self.events)

    def close(self):
        pass


def _msg(role, content, **extra):
    return {"role": role, "content": content, **extra}


class CompactionGuardTests(unittest.TestCase):
    def test_failed_turn_is_included_in_passive_model_telemetry(self):
        settings = {"model": "failing.gguf", "num_ctx": 8192, "max_tool_rounds": 2,
                    "summarize_history": True, "thinking_budget": 0,
                    "enable_thinking": False, "passive_model_telemetry": True}
        observations = []
        with patch.object(bridge, "get_settings", return_value=settings), \
                patch.object(bridge, "get_chats", return_value={"chats": {"fail": {}}, "order": ["fail"]}), \
                patch.object(bridge, "_llama_props_ctx", return_value=8192), \
                patch.object(bridge, "_conversation_token_scale", return_value=1.0), \
                patch.object(bridge, "llama_post_stream", side_effect=RuntimeError("backend unavailable")), \
                patch.object(bridge, "_record_model_observation",
                             side_effect=lambda model, **kw: observations.append((model, kw))), \
                patch.object(bridge, "_turn_journal_begin", return_value="turn-fail"), \
                patch.object(bridge._llama, "is_vision_capable", return_value=False):
            result = bridge.run_chat_turn(
                "fail", [_msg("system", "system"), _msg("user", "work")],
                use_tools=False, emit=lambda _e: None, native_tools=True)
        self.assertIsNone(result)
        self.assertEqual(len(observations), 1)
        self.assertFalse(observations[0][1]["completed"])

    def test_orchestrator_simulates_fragmented_tool_round_then_final(self):
        first = _FakeStream([
            {"choices": [{"delta": {"tool_calls": [{
                "index": 0, "id": "c1", "function": {
                    "name": "read_file", "arguments": '{"path":"C:/demo.txt"}'}}]},
                "finish_reason": "tool_calls"}]},
            {"timings": {"predicted_n": 8, "predicted_ms": 100, "prompt_n": 100}},
        ])
        second = _FakeStream([
            {"choices": [{"delta": {"content": "work complete"}, "finish_reason": "stop"}]},
            {"timings": {"predicted_n": 3, "predicted_ms": 50, "prompt_n": 120}},
        ])
        streams = iter([first, second])
        settings = {
            "model": "fake.gguf", "num_ctx": 8192, "max_tool_rounds": 8,
            "summarize_history": True, "thinking_budget": 256,
            "enable_thinking": False, "red_team_enabled": False,
            "rt_mission_state": True, "passive_model_telemetry": False,
        }
        chat = {"id": "sim", "messages": [_msg("user", "read the file")],
                "task_anchor": {"original": "read the file", "current": "read the file"}}
        events = []
        with patch.object(bridge, "get_settings", return_value=settings), \
                patch.object(bridge, "get_chats", return_value={"chats": {"sim": chat}, "order": ["sim"]}), \
                patch.object(bridge, "_llama_props_ctx", return_value=8192), \
                patch.object(bridge, "_conversation_token_scale", return_value=1.0), \
                patch.object(bridge, "_tools_spec_overhead_tokens", return_value=0), \
                patch.object(bridge, "llama_post_stream", side_effect=lambda *_a, **_k: next(streams)), \
                patch.object(bridge, "invoke_tool", return_value={"content": "file contents"}), \
                patch.object(bridge, "_record_model_observation"), \
                patch.object(bridge, "_turn_journal_checkpoint"), \
                patch.object(bridge, "_turn_journal_clear"), \
                patch.object(bridge, "_savings_add", return_value={"tok_in": 220, "tok_out": 11,
                    "turns": 1, "since": 1}), \
                patch.object(bridge._llama, "is_vision_capable", return_value=False):
            final = bridge.run_chat_turn(
                "sim", [_msg("system", "system"), _msg("user", "read the file")],
                use_tools=True, emit=events.append, native_tools=True)
        self.assertEqual(final["content"], "work complete")
        roles = [m.get("role") for m in final["_appended_intermediate"]]
        self.assertIn("tool", roles)
        self.assertTrue(any(e.get("type") == "tool_result" for e in events))

    def test_orchestrator_retries_a_context_overflow(self):
        stream = _FakeStream([
            {"choices": [{"delta": {"content": "recovered"}, "finish_reason": "stop"}]},
            {"timings": {"predicted_n": 2, "predicted_ms": 50, "prompt_n": 100}},
        ])
        attempts = {"n": 0}

        def open_stream(*_args, **_kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise urllib.error.HTTPError(
                    "http://local", 400, "context exceeds n_ctx", {},
                    io.BytesIO(b'{"error":"prompt exceeds available context"}'))
            return stream

        settings = {"model": "fake.gguf", "num_ctx": 8192, "max_tool_rounds": 4,
                    "summarize_history": True, "thinking_budget": 0,
                    "enable_thinking": False, "passive_model_telemetry": False}
        chat = {"id": "overflow", "messages": [_msg("user", "continue")]}
        events = []
        with patch.object(bridge, "get_settings", return_value=settings), \
                patch.object(bridge, "get_chats", return_value={"chats": {"overflow": chat}, "order": ["overflow"]}), \
                patch.object(bridge, "_llama_props_ctx", return_value=8192), \
                patch.object(bridge, "_conversation_token_scale", return_value=1.0), \
                patch.object(bridge, "llama_post_stream", side_effect=open_stream), \
                patch.object(bridge, "_record_model_observation"), \
                patch.object(bridge, "_savings_add", return_value={"tok_in": 100, "tok_out": 2,
                    "turns": 1, "since": 1}), \
                patch.object(bridge._llama, "is_vision_capable", return_value=False):
            final = bridge.run_chat_turn(
                "overflow", [_msg("system", "system"), _msg("user", "continue")],
                use_tools=False, emit=events.append, native_tools=True)
        self.assertEqual(attempts["n"], 2)
        self.assertEqual(final["content"], "recovered")
        self.assertTrue(any("context window" in e.get("note", "") for e in events))

    def test_chat_snapshots_merge_independent_concurrent_updates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            chats_file = root / "chats.json"
            chats_file.write_text(
                '{"chats":{"a":{"id":"a","messages":[]},"b":{"id":"b","messages":[]}},'
                '"order":["a","b"]}', encoding="utf-8")
            with patch.object(bridge, "CHATS_FILE", chats_file), \
                    patch.object(bridge, "DATA", root):
                first = bridge.get_chats()
                second = bridge.get_chats()
                first["chats"]["a"]["messages"].append(_msg("user", "from a"))
                second["chats"]["b"]["messages"].append(_msg("user", "from b"))
                bridge.save_json(chats_file, first)
                bridge.save_json(chats_file, second)
                # Saving the first snapshot again must not revert b to its stale baseline.
                first["chats"]["a"]["title"] = "renamed"
                bridge.save_json(chats_file, first)
                merged = bridge.get_chats()
            self.assertEqual(merged["chats"]["a"]["messages"][0]["content"], "from a")
            self.assertEqual(merged["chats"]["b"]["messages"][0]["content"], "from b")

    def test_turn_journal_recovers_completed_tool_work_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            journal = root / "turn_journal"
            journal.mkdir()
            chats_file = root / "chats.json"
            chats_file.write_text(
                '{"chats":{"j":{"id":"j","messages":[{"role":"user","content":"work"}]}},'
                '"order":["j"]}', encoding="utf-8")
            entries = [
                _msg("assistant", "", tool_calls=[{"id": "c1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"}}]),
                _msg("tool", "verified output", tool_call_id="c1", name="read_file"),
            ]
            with patch.object(bridge, "DATA", root), \
                    patch.object(bridge, "TURN_JOURNAL_DIR", journal), \
                    patch.object(bridge, "CHATS_FILE", chats_file):
                bridge._turn_journal_checkpoint("j", "turn-1", entries, activity=[])
                recovered = bridge._recover_turn_journals(now=time.time())
                again = bridge._recover_turn_journals(now=time.time())
                chat = bridge.get_chats()["chats"]["j"]
            self.assertEqual(recovered["recovered"], 1)
            self.assertEqual(again["recovered"], 0)
            self.assertEqual(sum(1 for m in chat["messages"] if m.get("_turn_id") == "turn-1"), 3)

    def test_verification_debt_requires_harness_observed_check(self):
        debt, changed = bridge._update_verification_debt(
            [], "write_file", {"path": "src/demo.py"}, {"ok": True, "path": "src/demo.py"})
        self.assertTrue(changed)
        self.assertEqual(debt[0]["path"], "src/demo.py")
        still_due, changed = bridge._update_verification_debt(
            debt, "check_syntax", {"path": "src/demo.py"}, {"error": "SyntaxError"})
        self.assertFalse(changed)
        self.assertEqual(len(still_due), 1)
        clear, changed = bridge._update_verification_debt(
            debt, "check_syntax", {"path": "src/demo.py"}, {"result": "Syntax OK"})
        self.assertTrue(changed)
        self.assertEqual(clear, [])

    def test_action_audit_hashes_but_does_not_store_command_or_typed_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            audit = root / "actions.jsonl"
            with patch.object(bridge, "DATA", root), \
                    patch.object(bridge, "ACTION_AUDIT_FILE", audit), \
                    patch.object(bridge, "get_chats", return_value={"chats": {}, "order": []}):
                bridge._record_action_audit(
                    "run_powershell", {"command": "deploy --token very-secret"},
                    {"ok": True, "stdout": "private output"})
            raw = audit.read_text(encoding="utf-8")
        self.assertNotIn("very-secret", raw)
        self.assertNotIn("private output", raw)
        self.assertIn("args_sha256", raw)

    def test_network_baseline_detects_drift(self):
        first = {"tcp_connections": [{"process": "app", "RemoteAddress": "1.1.1.1",
                                      "RemotePort": 443, "State": "Established"}],
                 "udp_listeners": [], "recent_dns": [], "process_details": [{"process": "app"}]}
        second = {"tcp_connections": [{"process": "app", "RemoteAddress": "2.2.2.2",
                                       "RemotePort": 443, "State": "Established"}],
                  "udp_listeners": [], "recent_dns": [], "process_details": [{"process": "app"}]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.object(bridge, "_BLUE_BASELINE_DIR", root):
                bridge._network_baseline_apply(first, {"save_baseline": "known-good"})
                bridge._network_baseline_apply(second, {"compare_baseline": "known-good"})
        self.assertTrue(second["comparison"]["changed"])
        self.assertEqual(len(second["comparison"]["added_tcp"]), 1)

    def test_retention_never_drops_unsummarized_one_shot_work(self):
        chat = {
            "id": "one-shot",
            "messages": [_msg("user", "audit the repository")]
            + [_msg("assistant", f"working-{i}") for i in range(bridge.CHAT_HISTORY_MAX + 4)],
            "summary_through": 0,
        }
        before = list(chat["messages"])
        with patch.object(bridge, "_log_compact"):
            bridge._enforce_chat_retention(chat)
        self.assertEqual(chat["messages"], before)
        self.assertGreater(len(chat["messages"]), bridge.CHAT_HISTORY_MAX)

    def test_retention_removes_only_already_summarized_history(self):
        messages = [_msg("user", "original task")]
        messages += [_msg("assistant", f"m-{i}") for i in range(bridge.CHAT_HISTORY_MAX + 9)]
        chat = {"id": "folded", "messages": messages, "summary_through": 600}
        unsummarized_tail = list(messages[600:])
        bridge._enforce_chat_retention(chat)
        self.assertEqual(len(chat["messages"]), bridge.CHAT_HISTORY_MAX)
        self.assertIs(chat["messages"][0], messages[0])
        self.assertEqual(chat["messages"][-len(unsummarized_tail):], unsummarized_tail)
        self.assertEqual(chat["summary_through"], 590)

    def test_task_anchor_ignores_acknowledgements_and_tracks_real_switches(self):
        chat = {}
        self.assertTrue(bridge._update_task_anchor(chat, "Audit the whole repository and fix the harness."))
        first = dict(chat["task_anchor"])
        self.assertFalse(bridge._update_task_anchor(chat, "go ahead"))
        self.assertEqual(chat["task_anchor"]["current"], first["current"])
        self.assertTrue(bridge._update_task_anchor(chat, "Now verify the mobile layout as well."))
        self.assertEqual(chat["task_anchor"]["original"], first["original"])
        self.assertIn("mobile layout", chat["task_anchor"]["current"])

    def test_activity_ledger_records_outcome_without_command_body(self):
        rec = bridge._tool_activity_record(
            "run_powershell",
            {"command": "deploy --token super-secret"},
            {"error": "tests failed on line 12"},
        )
        self.assertEqual(rec["status"], "error")
        self.assertEqual(rec["target"], "")
        self.assertNotIn("super-secret", bridge._render_activity_tail([rec]))
        self.assertIn("tests failed", bridge._render_activity_tail([rec]))

    def test_one_shot_trim_keeps_original_ask_and_valid_recent_boundary(self):
        messages = [_msg("system", "system"), _msg("user", "do a long autonomous audit")]
        for i in range(30):
            cid = f"call-{i}"
            messages.append(_msg("assistant", "", tool_calls=[{
                "id": cid, "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }]))
            messages.append(_msg("tool", "x" * 700, tool_call_id=cid, name="read_file"))
        trimmed = bridge.truncate_messages(messages, max_tokens=2400, reserve=400)
        self.assertEqual(trimmed[0]["role"], "system")
        self.assertEqual(trimmed[1]["role"], "user")
        self.assertNotEqual(trimmed[2]["role"], "tool")
        self.assertLess(len(trimmed), len(messages))

    def test_summary_splice_treats_windows_backslashes_literally(self):
        system = (
            "base\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; old) ===\n"
            "old summary\n\nTOOL CALLING\nrest"
        )
        summary = r"path C:\work\repo\new and regex \d+\s"
        out = bridge._splice_rolling_summary(system, summary)
        self.assertIn(summary, out)
        self.assertNotIn("old summary", out)

    def test_summary_validator_rejects_missing_state_sections(self):
        self.assertFalse(bridge._validate_summary_output("a short generic summary")[0])
        valid = "\n".join(
            heading + "\nstate" for heading in bridge._SUMMARY_REQUIRED_HEADINGS)
        self.assertTrue(bridge._validate_summary_output(valid)[0])

    def test_structured_continuity_uses_harness_state(self):
        chat = {
            "task_anchor": {"original": "audit repo", "current": "verify fix"},
            "pins": ["do not rewrite bridge.py"],
            "plan": [{"title": "Run tests", "status": "active"}],
            "activity": [{"tool": "write_file", "status": "ok", "target": "C:/work/fix.py"}],
        }
        state = bridge._build_continuity_state(chat)
        self.assertEqual(state["goal"], "audit repo")
        self.assertIn("do not rewrite bridge.py", state["constraints"])
        self.assertEqual(state["next_steps"][0]["step"], "Run tests")

    def test_same_chat_fold_requests_are_single_flight(self):
        chat = {"id": "race", "messages": []}
        entered = threading.Event()
        release = threading.Event()
        outcomes = []

        def slow_fold(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            return True

        with patch.object(bridge, "_maybe_roll_summary_unlocked", side_effect=slow_fold), \
                patch.object(bridge, "_record_fold_event"):
            t = threading.Thread(
                target=lambda: outcomes.append(bridge._maybe_roll_summary(chat, 32768)),
                daemon=True,
            )
            t.start()
            self.assertTrue(entered.wait(1))
            outcomes.append(bridge._maybe_roll_summary(chat, 32768))
            release.set()
            t.join(2)
        self.assertCountEqual(outcomes, [True, False])

    def test_same_chat_cannot_start_two_turns(self):
        bridge._unregister_cancel("duplicate-turn")
        first = bridge._register_cancel("duplicate-turn")
        try:
            self.assertIsNotNone(first)
            self.assertIsNone(bridge._register_cancel("duplicate-turn"))
        finally:
            bridge._unregister_cancel("duplicate-turn")

    def test_evidence_retention_preserves_recent_proof_across_restart(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            evidence = root / "recon_evidence"
            evidence.mkdir()
            recent = evidence / "recent.json"
            old = evidence / "old.json"
            recent.write_text("recent", encoding="utf-8")
            old.write_text("old", encoding="utf-8")
            now = 2_000_000_000.0
            os.utime(recent, (now, now))
            os.utime(old, (now - 40 * 86400, now - 40 * 86400))
            with patch.object(bridge, "DATA", root):
                result = bridge._prune_red_team_evidence(
                    {"rt_evidence_retention_days": 30}, now=now)
            self.assertTrue(recent.exists())
            self.assertFalse(old.exists())
            self.assertEqual(result["removed"], 1)

    def test_project_map_gives_small_model_a_repo_overview(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "src").mkdir()
            (root / "tests").mkdir()
            (root / "README.md").write_text("demo", encoding="utf-8")
            (root / "package.json").write_text(
                '{"scripts":{"test":"node --test","lint":"eslint ."}}', encoding="utf-8")
            (root / "src" / "index.js").write_text("export const x = 1", encoding="utf-8")
            (root / "tests" / "index.test.js").write_text("", encoding="utf-8")
            with patch.object(bridge, "is_in_workspace", return_value=True), \
                    patch.object(bridge, "is_ignored", return_value=False):
                result = bridge.tool_project_map({"path": str(root)})
            self.assertEqual(result["files_scanned"], 4)
            self.assertIn("package.json", result["important_paths"])
            self.assertIn("tests/index.test.js", result["test_paths"])
            self.assertIn("npm run test", result["likely_verification_commands"])

    def test_run_tests_obeys_approval_policy(self):
        with patch.object(bridge, "is_in_workspace", return_value=True), \
                patch.object(bridge, "request_approval",
                             return_value={"decision": "deny", "status": "decided"}) as approval:
            result = bridge.tool_run_tests({"command": "example-test-command", "cwd": str(Path.cwd())})
        self.assertIn("user denied", result["error"])
        approval.assert_called_once()

    def test_run_tests_normalizes_timeout_byte_output(self):
        timeout = subprocess.TimeoutExpired(
            cmd=["example-test-command"], timeout=3,
            output=b"partial stdout", stderr=b"partial stderr",
        )
        with patch.object(bridge, "is_in_workspace", return_value=True), \
                patch.object(bridge, "request_approval",
                             return_value={"decision": "approve", "status": "decided"}), \
                patch.object(bridge.subprocess, "run", side_effect=timeout):
            result = bridge.tool_run_tests({
                "command": "example-test-command", "cwd": str(Path.cwd()), "timeout": 3,
            })
        self.assertIn("partial stdout", result["output_tail"])
        self.assertIn("partial stderr", result["output_tail"])

    def test_dependency_audit_never_guesses_semver_ranges(self):
        with tempfile.TemporaryDirectory() as td:
            manifest = Path(td) / "package.json"
            manifest.write_text(
                '{"dependencies":{"exact":"1.2.3","range":"^4.5.0","latest":"latest"}}',
                encoding="utf-8",
            )
            ecosystem, packages, unresolved = bridge._parse_dependency_manifest(manifest)
        self.assertEqual(ecosystem, "npm")
        self.assertEqual(packages, [{"name": "exact", "version": "1.2.3"}])
        self.assertIn("range: ^4.5.0", unresolved)
        self.assertIn("latest: latest", unresolved)

    def test_dependency_audit_prefers_exact_lockfile_versions(self):
        with tempfile.TemporaryDirectory() as td:
            lock = Path(td) / "package-lock.json"
            lock.write_text(
                '{"lockfileVersion":3,"packages":{"":{"name":"demo"},'
                '"node_modules/pkg":{"version":"2.3.4"}}}', encoding="utf-8")
            ecosystem, packages, unresolved = bridge._parse_dependency_manifest(lock)
        self.assertEqual(ecosystem, "npm")
        self.assertEqual(packages, [{"name": "pkg", "version": "2.3.4"}])
        self.assertEqual(unresolved, [])

    def test_dependency_audit_reads_pnpm_lock_and_does_not_call_go_mod_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            pnpm = Path(td) / "pnpm-lock.yaml"
            pnpm.write_text("lockfileVersion: '9.0'\npackages:\n  'left-pad@1.3.0': {}\n", encoding="utf-8")
            ecosystem, packages, unresolved = bridge._parse_dependency_manifest(pnpm)
            self.assertEqual(ecosystem, "npm")
            self.assertIn({"name": "left-pad", "version": "1.3.0"}, packages)
            go = Path(td) / "go.mod"
            go.write_text("module demo\nrequire example.com/pkg v1.2.3\n", encoding="utf-8")
            ecosystem, packages, unresolved = bridge._parse_dependency_manifest(go)
            self.assertEqual(ecosystem, "Go")
            self.assertEqual(packages, [])
            self.assertIn("go.mod declaration", unresolved[0])

    def test_capability_report_validates_registered_schemas(self):
        with patch.object(bridge, "_resolve_jadx_bin", return_value=None), \
                patch.object(bridge, "_sandbox_ready_cached", return_value=False):
            result = bridge.tool_capability_report({})
        self.assertGreater(result["registered_tools"], 100)
        self.assertEqual(result["schema_issues"], [])

    def test_capability_report_can_show_tool_contracts_and_hides_hard_missing_deps(self):
        settings = {"red_team_enabled": False, "desktop_enabled": True,
                    "analysis_tools_enabled": True}
        with patch.object(bridge, "get_settings", return_value=settings), \
                patch.object(bridge, "get_chats", return_value={"chats": {}, "order": []}), \
                patch.object(bridge, "_llama_props_ctx", return_value=32768), \
                patch.object(bridge, "_resolve_jadx_bin", return_value=None), \
                patch.object(bridge, "_sandbox_ready_cached", return_value=False), \
                patch.object(bridge, "_HAVE_CAPSTONE", False):
            excluded = bridge._excluded_tools(True, "")
            result = bridge.tool_capability_report({"detail": "visible"})
        self.assertIn("disasm_at", excluded)
        self.assertIn("decompile_apk", result["unavailable_tools"])
        self.assertTrue(result["tool_contracts"])
        self.assertIn("approval", result["tool_contracts"][0])

    def test_findings_require_observed_evidence_for_validated_state(self):
        chats = {"chats": {"finding-chat": {"id": "finding-chat", "messages": []}},
                 "order": ["finding-chat"]}
        token = bridge._current_chat_id.set("finding-chat")
        try:
            with patch.object(bridge, "get_chats", return_value=chats), \
                    patch.object(bridge, "save_json"):
                candidate = bridge.tool_record_finding({
                    "title": "Possible issue", "status": "validated",
                    "source_tool": "scan_secrets",
                })
                with bridge._chat_live_activity_lock:
                    bridge._chat_live_activity["finding-chat"] = [
                        {"tool": "scan_secrets", "status": "ok"}]
                validated = bridge.tool_record_finding({
                    "finding_id": candidate["finding"]["id"],
                    "title": "Possible issue", "status": "validated",
                    "source_tool": "scan_secrets", "evidence": "tool matched a key",
                })
            self.assertEqual(candidate["finding"]["status"], "candidate")
            self.assertTrue(candidate["downgraded"])
            self.assertEqual(validated["finding"]["status"], "validated")
            self.assertTrue(validated["finding"]["source_observed"])
        finally:
            with bridge._chat_live_activity_lock:
                bridge._chat_live_activity.pop("finding-chat", None)
            bridge._current_chat_id.reset(token)

    def test_passive_model_profile_contains_no_prompt_content(self):
        with tempfile.TemporaryDirectory() as td:
            profile_file = Path(td) / "profiles.json"
            with patch.object(bridge, "MODEL_RUNTIME_FILE", profile_file), \
                    patch.object(bridge, "get_settings", return_value={
                        "passive_model_telemetry": True, "num_ctx": 8192}), \
                    patch.object(bridge, "_llama_props_ctx", return_value=8192):
                bridge._record_model_observation(
                    "demo.gguf", prompt_tokens=100, output_tokens=25, rounds=2,
                    tool_calls=1, tool_errors=0, elapsed_s=5.0)
                data = bridge._model_runtime_profiles()
            raw = profile_file.read_text(encoding="utf-8")
        self.assertNotIn("prompt", raw.lower().replace("prompt_tokens", ""))
        self.assertEqual(data["models"]["demo.gguf"]["observed"]["output_tok_s"], 5.0)

    def test_public_model_health_is_sanitized_and_advisory(self):
        with tempfile.TemporaryDirectory() as td:
            profile_file = Path(td) / "profiles.json"
            settings = {
                "passive_model_telemetry": True,
                "model": "health-demo",
                "model_path": r"C:\private\models\health-demo.gguf",
                "num_ctx": 8192,
            }
            with patch.object(bridge, "MODEL_RUNTIME_FILE", profile_file), \
                    patch.object(bridge, "get_settings", return_value=settings), \
                    patch.object(bridge, "_llama_props_ctx", return_value=8192):
                for _ in range(8):
                    bridge._record_model_observation(
                        "health-demo", prompt_tokens=100, output_tokens=20,
                        peak_prompt_tokens=7000, rounds=2, tool_calls=1,
                        tool_errors=0, elapsed_s=4.0, completed=True)
                health = bridge._public_model_health()
        raw = __import__("json").dumps(health)
        self.assertEqual(health["model"], "health-demo.gguf")
        self.assertEqual(health["turns"], 8)
        self.assertNotIn("C:\\private", raw)
        self.assertNotIn("prompt_tokens", raw)
        self.assertIn("privacy", health)

    def test_small_context_uses_lean_tool_core(self):
        settings = {
            "red_team_enabled": False,
            "desktop_enabled": False,
            "analysis_tools_enabled": False,
        }
        with patch.object(bridge, "_llama_props_ctx", return_value=8192), \
                patch.object(bridge, "get_settings", return_value=settings), \
                patch.object(bridge, "get_chats", return_value={"chats": {}, "order": []}), \
                patch.object(bridge, "_sandbox_ready_cached", return_value=False):
            visible = bridge._visible_tool_names(True, "small-chat")
        self.assertIn("project_map", visible)
        self.assertIn("read_file", visible)
        self.assertNotIn("web_fetch", visible)
        self.assertNotIn("session_start", visible)
        self.assertLessEqual(len(visible), len(bridge._SMALL_CTX_CORE_TOOL_NAMES))


class ReasoningEffortTests(unittest.TestCase):
    def setUp(self):
        bridge._REASONING_CAP_CACHE.clear()

    def _cap(self, template="", name="plain.gguf", override="auto", observed=False):
        settings = {"model_path": name, "reasoning_capability_override": override}
        metadata = {"keys": {"tokenizer.chat_template": template}}
        with patch.object(bridge, "read_gguf_metadata", return_value=metadata), \
                patch.object(bridge, "_model_reasoning_was_observed", return_value=observed):
            return bridge._reasoning_capability(name, settings, props={})

    def test_native_effort_is_detected_from_template_not_model_name(self):
        cap = self._cap("{% set effort = reasoning_effort | default('medium') %}")
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["mode"], "native_effort")
        self.assertEqual(cap["source"], "chat_template")
        self.assertEqual(cap["native_parameter"], "reasoning_effort")

    def test_muse_reasoning_strength_is_detected_as_native_effort(self):
        cap = self._cap(
            "{% set rs = reasoning_strength if reasoning_strength is defined else 'high' %}",
            name="Muse-Glimmer-30B.gguf")
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["mode"], "native_effort")
        self.assertEqual(cap["native_parameter"], "reasoning_strength")

    def test_budget_reasoning_is_detected_from_template(self):
        cap = self._cap("{% if enable_thinking %}{{ thinking_budget }}{% endif %}")
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["mode"], "budget")

    def test_plain_model_does_not_get_a_placebo_control(self):
        cap = self._cap("{{ messages }}", name="ordinary-instruct.gguf")
        self.assertFalse(cap["supported"])
        self.assertEqual(cap["mode"], "none")

    def test_generic_server_default_does_not_create_a_placebo_control(self):
        settings = {"model_path": "ordinary-instruct.gguf",
                    "reasoning_capability_override": "auto"}
        props = {"default_generation_settings": {"reasoning_effort": "medium"}}
        with patch.object(bridge, "read_gguf_metadata", return_value={"keys": {}}), \
                patch.object(bridge, "_model_reasoning_was_observed", return_value=False):
            cap = bridge._reasoning_capability(
                "ordinary-instruct.gguf", settings, props=props)
        self.assertFalse(cap["supported"])

    def test_live_props_chat_template_can_enable_native_effort(self):
        settings = {"model_path": "future-model.gguf",
                    "reasoning_capability_override": "auto"}
        props = {"chat_template": "{% set level = reasoning_effort %}"}
        with patch.object(bridge, "read_gguf_metadata", return_value={"keys": {}}), \
                patch.object(bridge, "_model_reasoning_was_observed", return_value=False):
            cap = bridge._reasoning_capability("future-model.gguf", settings, props=props)
        self.assertEqual(cap["mode"], "native_effort")

    def test_observed_reasoning_unlocks_unknown_family(self):
        cap = self._cap("{{ messages }}", name="future-model.gguf", observed=True)
        self.assertTrue(cap["supported"])
        self.assertEqual(cap["source"], "observed_output")

    def test_manual_override_is_available_for_new_templates(self):
        cap = self._cap("", override="native_effort")
        self.assertEqual(cap["mode"], "native_effort")
        self.assertEqual(cap["source"], "manual")

    def test_budget_presets_are_ordered_and_native_level_is_forwarded(self):
        low = bridge._resolve_reasoning_request(
            "low", {"supported": True, "mode": "budget"}, {}, 32768)
        medium = bridge._resolve_reasoning_request(
            "medium", {"supported": True, "mode": "budget"}, {}, 32768)
        high = bridge._resolve_reasoning_request(
            "high", {"supported": True, "mode": "native_effort"}, {}, 32768)
        self.assertLess(low["budget"], medium["budget"])
        self.assertLess(medium["budget"], high["budget"])
        self.assertIsNone(low["native_effort"])
        self.assertEqual(high["native_effort"], "high")

    def test_auto_preserves_existing_settings(self):
        cfg = bridge._resolve_reasoning_request(
            "auto", {"supported": True, "mode": "budget"},
            {"enable_thinking": True, "thinking_budget": 3072}, 32768)
        self.assertEqual(cfg["budget"], 3072)
        self.assertTrue(cfg["enabled"])
        self.assertIsNone(cfg["native_effort"])
        self.assertEqual(bridge._reasoning_payload_overrides(
            cfg, {"mode": "budget"}), {})

    def test_explicit_levels_use_llama_cpp_top_level_controls(self):
        budget_cfg = {"effort": "low", "budget": 768, "native_effort": None}
        native_cfg = {"effort": "high", "budget": 4096,
                      "native_effort": "high"}
        self.assertEqual(
            bridge._reasoning_payload_overrides(budget_cfg, {"mode": "budget"}),
            {"thinking_budget_tokens": 768})
        self.assertEqual(
            bridge._reasoning_payload_overrides(native_cfg, {"mode": "native_effort"}),
            {"reasoning_effort": "high"})
        self.assertEqual(
            bridge._reasoning_payload_overrides(
                native_cfg,
                {"mode": "native_effort", "native_parameter": "reasoning_strength"}),
            {})


class RedTeamEnforcementTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "red_team_enabled": True,
            "rt_mission_state": True,
            "desktop_enabled": False,
            "analysis_tools_enabled": False,
            "rt_force_exploit": False,
        }

    def _chat(self, authorized=True, phase="recon", status="active"):
        return {
            "id": "rt-chat",
            "mission": {
                "target": "example.com",
                "scope": "in: example.com | out: admin.example.com",
                "objective": "authorized test",
                "facts": [],
                "phase": phase,
                "status": status,
                "authorized": authorized,
            },
        }

    def _patch_state(self, chat):
        return patch.multiple(
            bridge,
            get_settings=lambda: dict(self.settings),
            get_chats=lambda: {"chats": {"rt-chat": chat}, "order": ["rt-chat"]},
        )

    @contextmanager
    def _active_chat(self):
        token = bridge._current_chat_id.set("rt-chat")
        try:
            yield
        finally:
            bridge._current_chat_id.reset(token)

    @contextmanager
    def _live_rt_chat(self, chat):
        token = bridge._current_rt_chat.set(chat)
        try:
            yield
        finally:
            bridge._current_rt_chat.reset(token)

    def test_tools_refuse_settings_only_without_per_chat_authorization(self):
        chat = self._chat(authorized=False)
        with self._patch_state(chat), self._active_chat():
            reason = bridge._rt_scope_block("recon_dns", {"domain": "example.com"})
        self.assertIn("no active user-authorized", reason)

    def test_allowlist_accepts_target_and_rejects_outside_or_explicit_out(self):
        chat = self._chat()
        with self._patch_state(chat), self._active_chat():
            self.assertIsNone(bridge._rt_scope_block("recon_dns", {"domain": "example.com"}))
            self.assertIsNone(bridge._rt_scope_block("web_fetch", {"url": "https://api.example.com/v1"}))
            self.assertIn("OUT OF SCOPE", bridge._rt_scope_block(
                "web_fetch", {"url": "https://admin.example.com/"}))
            self.assertIn("not in the user-authorized scope", bridge._rt_scope_block(
                "web_fetch", {"url": "https://example.net/"}))

    def test_redirect_handler_refuses_cross_scope_destination(self):
        chat = self._chat()
        handler = bridge._ScopeCheckedRedirect()
        with self._patch_state(chat), self._active_chat():
            with self.assertRaises(urllib.error.URLError):
                handler.redirect_request(
                    None, None, 302, "Found", {}, "https://example.net/landing")

    def test_hostname_alias_cannot_reach_accuretta_self_port(self):
        chat = self._chat()
        with self._patch_state(chat), self._active_chat(), \
                patch.object(bridge.socket, "getaddrinfo",
                             return_value=[(2, 1, 6, "", ("127.0.0.1", 0))]):
            reason = bridge._rt_scope_block(
                "recon_http_fingerprint", {"url": f"http://example.com:{bridge.PORT}/"})
        self.assertIn("OWN bridge", reason)

    def test_exploit_tools_require_validated_phase(self):
        chat = self._chat(phase="recon")
        with self._patch_state(chat), self._active_chat():
            reason = bridge._rt_scope_block("http_request", {"url": "https://example.com/"})
        self.assertIn("exploit tools are locked", reason)

    def test_live_turn_phase_unlocks_next_worker_before_disk_commit(self):
        persisted = self._chat(phase="recon")
        live = self._chat(phase="recon")
        with self._patch_state(persisted), self._active_chat(), self._live_rt_chat(live):
            worker_context = contextvars.copy_context()
            self.assertTrue(bridge._rt_mission_set_phase(live, "exploit"))
            reason = worker_context.run(
                bridge._rt_scope_block, "http_request", {"url": "https://example.com/"})
        self.assertIsNone(reason)
        self.assertEqual(persisted["mission"]["phase"], "recon")

    def test_harness_observed_findings_can_promote_the_phase(self):
        self.assertTrue(bridge._rt_result_confirms_finding(
            "validate_finding", {"likely_real": True}))
        self.assertTrue(bridge._rt_result_confirms_finding(
            "recon_injection_probe", {"findings": [{"severity": "high", "type": "reflected XSS"}]}))
        self.assertFalse(bridge._rt_result_confirms_finding(
            "recon_injection_probe", {"findings": [{"severity": "info", "type": "none"}]}))
        self.assertTrue(bridge._rt_result_confirms_finding(
            "record_finding", {"finding": {"status": "validated", "source_observed": True}}))

    def test_generic_network_commands_cannot_bypass_scope(self):
        chat = self._chat(phase="exploit")
        with self._patch_state(chat), self._active_chat():
            self.assertIsNone(bridge._rt_command_scope_block(
                "curl https://api.example.com/status", chat))
            self.assertIn("not in the user-authorized scope", bridge._rt_command_scope_block(
                "curl https://example.net/status", chat))
            self.assertIn("destination is not a literal host", bridge._rt_command_scope_block(
                "curl $TARGET/status", chat))

    def test_mcp_or_desktop_cannot_bypass_active_engagement_scope(self):
        chat = self._chat(phase="exploit")
        fake_name = "mcp_test_browser"
        old = bridge.TOOLS.get(fake_name)
        bridge.TOOLS[fake_name] = {
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
            "fn": lambda _args: {"ok": True},
        }
        try:
            with self._patch_state(chat), self._active_chat():
                result = bridge.invoke_tool(fake_name, {})
            self.assertTrue(result.get("scope_blocked"))
        finally:
            if old is None:
                bridge.TOOLS.pop(fake_name, None)
            else:
                bridge.TOOLS[fake_name] = old

    def test_closed_mission_requires_fresh_gate(self):
        chat = self._chat(status="closed")
        with self._patch_state(chat), self._active_chat():
            reason = bridge._rt_scope_block("recon_dns", {"domain": "example.com"})
        self.assertIn("no active user-authorized", reason)

    def test_schema_visibility_follows_authorization_and_phase(self):
        unauthorized = self._chat(authorized=False)
        with self._patch_state(unauthorized):
            hidden = bridge._visible_tool_names(False, "rt-chat")
        self.assertNotIn("recon_dns", hidden)

        authorized = self._chat()
        with self._patch_state(authorized):
            recon = bridge._visible_tool_names(False, "rt-chat")
            exploit = bridge._visible_tool_names(True, "rt-chat")
        self.assertIn("recon_dns", recon)
        self.assertNotIn("http_request", recon)
        self.assertIn("http_request", exploit)

    def test_panel_requires_explicit_authorization_and_creates_fresh_record(self):
        chat = {"id": "rt-chat", "mission": {"target": "old.example", "facts": ["stale"]}}
        self.assertFalse(bridge._rt_mission_apply_panel(chat, {
            "target": "example.com", "authorized": False,
        }))
        self.assertEqual(chat["mission"]["target"], "old.example")
        self.assertTrue(bridge._rt_mission_apply_panel(chat, {
            "target": "example.com",
            "scope": "in: example.com",
            "objective": "test it",
            "constraints": "non-destructive",
            "authorized": True,
        }))
        self.assertTrue(chat["mission"]["authorized"])
        self.assertEqual(chat["mission"]["status"], "active")
        self.assertEqual(chat["mission"]["facts"], ["constraints: non-destructive"])
        self.assertNotIn("stale", chat["mission"]["facts"])

    def test_report_hashes_evidence_and_closes_mission(self):
        chat = self._chat(phase="recon")
        live = self._chat(phase="exploit")
        chats = {"chats": {"rt-chat": chat}, "order": ["rt-chat"]}
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ev = root / "recon_evidence"
            ev.mkdir()
            (ev / "proof.json").write_text("proof", encoding="utf-8")
            saved = []
            with patch.object(bridge, "DATA", root), \
                    patch.object(bridge, "get_settings", return_value=self.settings), \
                    patch.object(bridge, "get_chats", return_value=chats), \
                    patch.object(bridge, "save_json", side_effect=lambda _p, value: saved.append(value)), \
                    self._active_chat(), self._live_rt_chat(live):
                result = bridge.tool_rt_generate_report({"findings": "confirmed test"})
            report = Path(result["report"]).read_text(encoding="utf-8")
        self.assertIn("sha256", report)
        self.assertIn("model-authored", report)
        self.assertEqual(chat["mission"]["status"], "closed")
        self.assertEqual(chat["mission"]["phase"], "exploit")
        self.assertEqual(live["mission"]["status"], "closed")
        self.assertTrue(saved)


if __name__ == "__main__":
    unittest.main()
