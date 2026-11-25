"""Core tool behaviour found in the 2026-11-19 audit of 24,682 recorded calls.

- edit_file failed 18% of the time; a common cause is a quoted block that differs
  from the file only in whitespace (indentation depth, tabs vs spaces).
- A batch with one bad edit wrote nothing but reported "applied: N".
- run_tests failed 70% of the time; "no tests collected" (wrong folder) was
  reported as "Tests failed", sending the model after a bug that wasn't there.
"""
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import bridge
from _patching import bridge_patch


class EditFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in {
            "is_in_workspace": lambda p: True, "is_ignored": lambda p: False,
            "is_data_path": lambda *a, **kw: False, "is_bridge_self_path": lambda p: False,
            "_critical_path_gate": lambda *a, **kw: None,
            "request_approval": lambda **kw: {"decision": "approve"},
            "_undo_snapshot": lambda p: None, "_undo_store": MagicMock(),
            "_run_linter": lambda p: None, "_record_file_read": lambda p: None,
        }.items():
            stack.enter_context(bridge_patch(name, value))

    def write(self, name, text, crlf=False):
        p = Path(self.tmp.name) / name
        p.write_bytes(text.replace("\n", "\r\n" if crlf else "\n").encode())
        return str(p)

    def read(self, path):
        return Path(path).read_bytes().decode()

    def test_block_quoted_at_the_wrong_depth_lands_at_the_files_depth(self):
        path = self.write("a.py", "class A:\n    def f(self):\n        x = 1\n        return x\n")
        result = bridge.tool_edit_file({"path": path, "edits": [{
            "old_text": "def f(self):\n    x = 1\n    return x",
            "new_text": "def f(self):\n    x = 2\n    return x * 3"}]})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.read(path),
                         "class A:\n    def f(self):\n        x = 2\n        return x * 3\n")

    def test_spaces_quoted_against_a_tab_file_keep_the_tabs(self):
        path = self.write("b.go", "func f() {\n\tif x {\n\t\treturn 1\n\t}\n}\n")
        result = bridge.tool_edit_file({"path": path, "edits": [{
            "old_text": "    if x {\n        return 1\n    }",
            "new_text": "    if x {\n        return 2\n    }"}]})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.read(path), "func f() {\n\tif x {\n\t\treturn 2\n\t}\n}\n")

    def test_crlf_files_stay_crlf(self):
        path = self.write("c.py", "def g():\n    return 1\n", crlf=True)
        result = bridge.tool_edit_file({"path": path, "edits": [{
            "old_text": "def g():\n  return 1", "new_text": "def g():\n  return 5"}]})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.read(path), "def g():\r\n    return 5\r\n")

    def test_new_nesting_follows_the_files_indent_width(self):
        path = self.write("g.py", "def h():\n    if a:\n        b()\n    return 0\n")
        result = bridge.tool_edit_file({"path": path, "edits": [{
            "old_text": "if a:\n  b()",
            "new_text": "if a:\n  b()\n  if c:\n    d()"}]})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(self.read(path),
                         "def h():\n    if a:\n        b()\n        if c:\n            d()\n    return 0\n")

    def test_ambiguous_loose_match_is_refused(self):
        path = self.write("d.py", "if a:\n    go()\nif a:\n        go()\n")
        result = bridge.tool_edit_file({"path": path, "edits": [{
            "old_text": "if a:\n  go()", "new_text": "if b:\n  go()"}]})
        self.assertIn("error", result)
        self.assertEqual(self.read(path), "if a:\n    go()\nif a:\n        go()\n")

    def test_one_bad_edit_writes_nothing_and_says_so(self):
        path = self.write("e.py", "a = 1\nb = 2\n")
        result = bridge.tool_edit_file({"path": path, "edits": [
            {"old_text": "a = 1", "new_text": "a = 10"},
            {"old_text": "c = 3", "new_text": "c = 30"}]})
        self.assertIn("nothing was written", result["error"])
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(self.read(path), "a = 1\nb = 2\n")

    def test_exact_matches_still_work_as_before(self):
        path = self.write("f.py", "value = 1\n")
        result = bridge.tool_edit_file({"path": path, "edits": [
            {"old_text": "value = 1", "new_text": "value = 2"}]})
        self.assertTrue(result.get("ok"))
        self.assertEqual(self.read(path), "value = 2\n")


class NoTestsRanTests(unittest.TestCase):
    def test_detects_runners_that_found_nothing(self):
        self.assertTrue(bridge._no_tests_ran(5, "===== no tests ran in 0.01s ====="))
        self.assertTrue(bridge._no_tests_ran(5, "\nRan 0 tests in 0.000s\n\nNO TESTS RAN\n"))
        self.assertTrue(bridge._no_tests_ran(1, "No tests found, exiting with code 1"))
        self.assertTrue(bridge._no_tests_ran(0, "collected 0 items"))

    def test_real_failures_and_passes_are_not_mistaken(self):
        self.assertFalse(bridge._no_tests_ran(1, "FAILED test_a.py::test_x - assert 1 == 2\n1 failed"))
        self.assertFalse(bridge._no_tests_ran(0, "Ran 12 tests in 0.3s\n\nOK"))


class ShellEncodingTests(unittest.TestCase):
    """Windows PowerShell 5.1 wrote `>` as UTF-16 and Python printed in the ANSI
    code page: `python gen.py > big.log` then reading big.log failed with
    "can't decode byte 0xff", and print('✓') crashed."""

    def test_write_targets_are_found(self):
        cmd = ("python gen.py > data/big.log; python a.py >> 'out file.txt'; "
               "node x 2>&1; 'x' | Out-File -FilePath res.txt; dir *> all.txt; "
               "$o | Out-File plain.txt -Width 200; 'u' | Out-File enc.txt -Encoding unicode")
        self.assertEqual(bridge._ps_write_targets(cmd),
                         ["data/big.log", "'out file.txt'", "all.txt", "res.txt", "plain.txt"])

    def test_candidates_skip_variables_devices_and_existing_boms(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "had_bom.txt").write_bytes(b"\xef\xbb\xbfx")
            Path(tmp, "plain.txt").write_bytes(b"x")
            got = bridge._ps_bom_candidates(
                "a > $out; b > $null; c > nul; d > had_bom.txt; e > plain.txt; f > new.txt", tmp)
            self.assertEqual(got, [os.path.join(tmp, "plain.txt"), os.path.join(tmp, "new.txt")])

    def test_strip_only_touches_fresh_bom_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp, "fresh.txt")
            fresh.write_bytes(b"\xef\xbb\xbfdata\r\n")
            bridge._strip_new_boms([str(fresh)], since=os.path.getmtime(fresh))
            self.assertEqual(fresh.read_bytes(), b"data\r\n")
            old = Path(tmp, "old.txt")
            old.write_bytes(b"\xef\xbb\xbfdata")
            os.utime(old, (1, 1))
            bridge._strip_new_boms([str(old)], since=time.time())
            self.assertEqual(old.read_bytes(), b"\xef\xbb\xbfdata")

    def test_tool_env_is_utf8_but_user_values_win(self):
        with patch.dict(os.environ, {"PYTHONIOENCODING": "cp437"}, clear=False):
            env = bridge._tool_child_env(PYTHONUNBUFFERED="1")
        self.assertEqual(env["PYTHONUTF8"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "cp437")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5.1 only")
    def test_live_redirect_is_clean_utf8_within_and_after_the_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = bridge._run_powershell(
                "python -c \"print('h\\u00e9llo \\u2713')\" > big.log; "
                "python -c \"import sys; print(open(sys.argv[1],'rb').read()[:3].hex())\" big.log; "
                "$v = 'a'; $v > e.txt; 'b' >> e.txt; python -c \"input()\"",
                timeout=60, cwd=tmp, wrap=True)
            self.assertEqual(Path(tmp, "big.log").read_bytes(), "h\u00e9llo \u2713\r\n".encode())
            self.assertIn("68c3a9", result["stdout"])          # no BOM even mid-command
            self.assertEqual(Path(tmp, "e.txt").read_bytes(), b"a\r\nb\r\n")
            self.assertIn("EOFError", result["stderr"])          # input() fails fast


class SessionTests(unittest.TestCase):
    def test_bare_repls_become_interactive(self):
        self.assertEqual(bridge._session_repl_command("python"), "python -i -u")
        self.assertEqual(bridge._session_repl_command("py -3"), "py -3 -i -u")
        self.assertEqual(bridge._session_repl_command('"C:\\Py\\python.exe"'), '"C:\\Py\\python.exe" -i -u')
        self.assertEqual(bridge._session_repl_command("node"), "node -i")
        self.assertEqual(bridge._session_repl_command("python app.py"), "python app.py")
        self.assertEqual(bridge._session_repl_command("ssh host"), "ssh host")

    @unittest.skipUnless(os.name == "nt", "uses cmd.exe")
    def test_prompt_without_newline_is_visible(self):
        s = bridge._session_mgr.create("cmd /c set /p x=Password: ", cwd=None)
        try:
            deadline = time.time() + 10
            while time.time() < deadline and "Password:" not in s.read_since(0)[0]:
                time.sleep(0.1)
            self.assertIn("Password:", s.read_since(0)[0])
        finally:
            s.stop()


class ReadFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in {
            "is_in_workspace": lambda p: True, "is_ignored": lambda p: False,
            "is_data_path": lambda *a, **kw: False, "is_blocked_path": lambda p: False,
            "_record_file_read": lambda p: None,
        }.items():
            stack.enter_context(bridge_patch(name, value))

    def test_utf16_files_read_as_text(self):
        path = Path(self.tmp.name, "ps.log")
        path.write_bytes("h\u00e9llo\r\nline 2\r\n".encode("utf-16"))
        result = bridge.tool_read_file({"path": str(path)})
        self.assertIn("h\u00e9llo", result["content"])
        self.assertNotIn("\x00", result["content"])

    def test_missing_file_suggests_close_names(self):
        Path(self.tmp.name, "receipts").mkdir()
        Path(self.tmp.name, "report.json").write_text("{}")
        result = bridge.tool_read_file({"path": str(Path(self.tmp.name, "receipt"))})
        self.assertIn(str(Path(self.tmp.name, "receipts")), result["did_you_mean"])
        result = bridge.tool_read_file({"path": str(Path(self.tmp.name, "report.txt"))})
        self.assertIn(str(Path(self.tmp.name, "report.json")), result["did_you_mean"])

    def test_folder_and_missing_parent_are_named(self):
        Path(self.tmp.name, "src").mkdir()
        self.assertIn("is a folder", bridge.tool_read_file({"path": str(Path(self.tmp.name, "src"))})["error"])
        err = bridge.tool_read_file({"path": str(Path(self.tmp.name, "nope", "deeper", "a.py"))})["error"]
        self.assertIn("nearest one that does is " + self.tmp.name, err)

    def test_web_view_image_hands_local_files_to_view_image(self):
        img = Path(self.tmp.name, "shot.png")
        img.write_bytes(b"png")
        seen = {}
        with bridge_patch("tool_view_image", lambda a: seen.update(a) or {"ok": True}):
            self.assertEqual(bridge.tool_web_view_image({"url": str(img), "hint": "the logo"}), {"ok": True})
            bridge.tool_web_view_image({"url": "file:///" + str(img).replace("\\", "/")})
        self.assertEqual(seen["path"].replace("\\", "/"), str(img).replace("\\", "/"))
        self.assertIn("view_image", bridge.tool_web_view_image({"url": "C:/nope.png"})["error"])


class CutOutputTests(unittest.TestCase):
    """Shell output over the 4K budget had its middle cut with no way back, and
    the runner had already kept only the last 16K without saying so."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = bridge_patch("_TOOL_OUTPUT_DIR", Path(self.tmp.name, "out"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cut_shell_output_is_saved_and_readable(self):
        stdout = "".join(f"row {i:05d}\n" for i in range(3000))
        text = bridge.compress_tool_result("run_python", {"ok": True, "exit_code": 0, "stdout": stdout}, 4000)
        self.assertLessEqual(len(text), 4000)
        saved = text.split("saved in ", 1)[1].split(";", 1)[0]
        self.assertIn("row 01500", Path(saved).read_text(encoding="utf-8"))
        with bridge_patch("is_in_workspace", lambda p: False), \
                bridge_patch("_record_file_read", lambda p: None):
            page = bridge.tool_read_file({"path": saved, "offset": 1500, "limit": 3})
        self.assertIn("row 01499", page["content"])

    def test_failed_command_keeps_its_output_tail(self):
        stdout = "".join(f"step {i}\n" for i in range(4000)) + "FINAL: listening on 8000\n"
        text = bridge.compress_tool_result(
            "run_powershell", {"ok": False, "exit_code": 1, "timed_out": True,
                               "error": "timeout after 120s", "stdout": stdout}, 4000)
        self.assertLessEqual(len(text), 4000)
        self.assertIn("listening on 8000", text)
        self.assertIn("_full_output", text)

    def test_head_tail_clip_says_what_it_dropped(self):
        out = bridge._clip_head_tail("a" * 50 + "b" * 50, 40)
        self.assertTrue(out.startswith("a" * 10) and out.endswith("b" * 30))
        self.assertIn("60 chars of output dropped", out)


class BundleIntentTests(unittest.TestCase):
    """A reverse-engineering run never found ghidra_analyze: it sat in a lazy
    bundle the model did not think to load."""

    def hits(self, text):
        return [b for b, rx in bridge._BUNDLE_INTENT_RES.items() if rx.search(text)]

    def test_plain_requests_pick_their_bundle(self):
        self.assertEqual(self.hits("Reverse engineer the CPU from these traces"), ["analysis"])
        self.assertEqual(self.hits("decompile firmware.bin and find the key"), ["analysis"])
        self.assertEqual(self.hits("look through capture.pcapng for the beacon"), ["analysis"])
        self.assertEqual(self.hits("analyze crackme.exe and get the password"), ["analysis"])
        self.assertEqual(self.hits("start the dev server and keep it running"), ["sessions"])
        self.assertEqual(self.hits("debug it with gdb"), ["sessions"])
        self.assertEqual(self.hits("commit this and open a pull request"), ["git"])

    def test_everyday_coding_words_do_not(self):
        for text in ("implement a binary search tree", "run setup.exe", "write a commit message helper",
                     "fix the server error in api.py", "make the parser faster", "add tests for the shell"):
            self.assertEqual(self.hits(text), [], text)

    def test_binary_wording_picks_analysis(self):
        for text in ("crack this crackme", "write a keygen for it", "the ELF binary in /tmp",
                     "keycheck is a Linux x86-64 program that validates a key"):
            self.assertEqual(self.hits(text), ["analysis"], text)

    def test_a_named_native_program_loads_analysis_whatever_the_wording(self):
        """Red-team bench 2026-11-20: 'keycheck is a Linux program that validates
        a product key' matched no word, so the model never saw disasm_at or
        ghidra_analyze and decoded the binary by hand (no valid key)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            prog = os.path.join(d, "keycheck")
            with open(prog, "wb") as fh:
                fh.write(b"\x7fELF\x02\x01\x01" + b"\x00" * 64)
            notes = os.path.join(d, "notes.txt")
            with open(notes, "w") as fh:
                fh.write("plain text")
            self.assertEqual(bridge._message_binary_paths(f"{prog} validates a key from stdin."), [prog])
            self.assertEqual(bridge._message_binary_paths(f"summarize {notes} please"), [])
            with patch.dict(bridge._unlocked_bundles_by_chat, {}, clear=True), \
                    bridge_patch("_excluded_tools", lambda *a, **k: set()), \
                    bridge_patch("broadcast_event", lambda e: None):
                self.assertEqual(bridge._auto_unlock_bundles(
                    "c2", f"{prog} is a program that checks a key. Give me a valid one."), ["analysis"])
                self.assertEqual(bridge._auto_unlock_bundles("c3", f"summarize {notes}"), [])

    def test_only_usable_bundles_load_once(self):
        loaded = []
        with patch.dict(bridge._unlocked_bundles_by_chat, {}, clear=True), \
                bridge_patch("_excluded_tools", lambda *a, **k: set(bridge._ANALYSIS_SUITE_TOOL_NAMES)), \
                bridge_patch("broadcast_event", lambda e: loaded.append(e["bundle"])):
            self.assertEqual(bridge._auto_unlock_bundles("c1", "decompile it, then commit this"), ["git"])
            self.assertEqual(bridge._auto_unlock_bundles("c1", "commit this again"), [])
        self.assertEqual(loaded, ["git"])


class ClickFeedbackTests(unittest.TestCase):
    """Hard deskbench 2026-11-20: a click a few pixels off hit a row's label,
    not its Claim button, and nothing said so."""

    def test_named_element_under_the_point_is_reported(self):
        ctrl = MagicMock(Name="Claim", ControlTypeName="ButtonControl")
        with bridge_patch("_HAVE_UIA", True), \
                bridge_patch("_uia", MagicMock(ControlFromPoint=lambda x, y: ctrl), create=True):
            self.assertEqual(bridge._element_at_point(5, 5), {"kind": "button", "name": "Claim"})

    def test_unnamed_container_says_nothing(self):
        ctrl = MagicMock(Name="", ControlTypeName="GroupControl")
        with bridge_patch("_HAVE_UIA", True), \
                bridge_patch("_uia", MagicMock(ControlFromPoint=lambda x, y: ctrl), create=True):
            self.assertIsNone(bridge._element_at_point(5, 5))


class DesktopLaunchHintTests(unittest.TestCase):
    """Launching an app asks exactly when starting it from the shell would. It
    used to refuse anything off the allowlist in every mode: a /goal run then
    started Notepad with Start-Process instead (2026-11-20)."""

    def launch(self, name, allowlisted):
        gates, popen = [], MagicMock()
        with bridge_patch("_app_matches_allowlist", lambda t: allowlisted), \
                bridge_patch("_gate_action", lambda **kw: gates.append(kw) or None), \
                patch.object(bridge.subprocess, "Popen", popen):
            result = bridge.tool_desktop_launch_app({"name": name})
        return result, gates, popen

    def test_off_the_allowlist_it_asks_instead_of_refusing(self):
        result, gates, popen = self.launch("notepad", allowlisted=False)
        self.assertTrue(result["ok"])
        self.assertEqual(gates[0]["details"], {"kind": "desktop.launch", "target": "notepad",
                                               "allowlisted": False})
        popen.assert_called_once()

    def test_browser_launch_points_to_open_url(self):
        self.assertEqual(self.launch("chrome", False)[0]["suggested_tool"], "desktop_open_url")
        self.assertNotIn("suggested_tool", self.launch("blender", False)[0])

    def approve(self, mode, allowlisted, goal=False):
        with bridge_patch("_approval_mode", lambda: mode), \
                bridge_patch("_recent_web_taint", lambda *a, **k: False), \
                bridge_patch("_chat_in_goal_mode", lambda cid: goal), \
                bridge_patch("broadcast_event", lambda e: None):
            return bridge.request_approval("Launch app", "notepad", {
                "kind": "desktop.launch", "target": "notepad", "allowlisted": allowlisted},
                timeout_s=0)

    def test_allowlisted_apps_open_without_asking_except_in_hard_mode(self):
        self.assertEqual(self.approve("soft", True)["status"], "auto-approved")
        self.assertEqual(self.approve("medium", True)["status"], "auto-approved")
        self.assertFalse(self.approve("hard", True).get("auto"))

    def test_other_apps_ask_but_goal_runs_them_like_the_shell(self):
        self.assertFalse(self.approve("soft", False).get("auto"))
        self.assertEqual(self.approve("soft", False, goal=True)["status"], "goal-auto-approved")
        cmd = bridge._soft_command_concerns("Start-Process notepad")   # the shell asks too
        self.assertTrue(cmd)


class ProjectorDeviceTests(unittest.TestCase):
    """The projector runs on the CPU by default (the owner's choice); a setup
    with VRAM to spare can keep it on the GPU. Spawn and tuner both ask
    _mmproj_on_cpu, so the VRAM reserve follows the choice."""

    def test_cpu_by_default_gpu_when_chosen(self):
        caps = {"no_mmproj_offload": True}
        with bridge_patch("get_settings", lambda: {"mmproj_device": "cpu"}):
            self.assertTrue(bridge._mmproj_on_cpu(caps))
        with bridge_patch("get_settings", lambda: {}):
            self.assertTrue(bridge._mmproj_on_cpu(caps))
        with bridge_patch("get_settings", lambda: {"mmproj_device": "gpu"}):
            self.assertFalse(bridge._mmproj_on_cpu(caps))
        with bridge_patch("get_settings", lambda: {"mmproj_device": "cpu"}):
            self.assertFalse(bridge._mmproj_on_cpu({}))     # an old build can't move it

    def test_stored_value_is_normalized(self):
        self.assertEqual(bridge.DEFAULT_SETTINGS["mmproj_device"], "cpu")
        with bridge_patch("load_json", lambda *a, **k: {"mmproj_device": "GPU"}):
            self.assertEqual(bridge.get_settings()["mmproj_device"], "gpu")
        with bridge_patch("load_json", lambda *a, **k: {"mmproj_device": "tpu"}):
            self.assertEqual(bridge.get_settings()["mmproj_device"], "cpu")


class ScreenSaverTests(unittest.TestCase):
    """An unattended deskbench run saw only the screen saver for ten minutes:
    Ribbons ignored the run's own clicks, and Windows' 'is a saver running'
    flag said no."""

    def test_preflight_wakes_the_screen_first(self):
        calls = []
        with bridge_patch("get_settings", lambda: {"desktop_enabled": True}), \
                bridge_patch("_wake_screen_saver", lambda: calls.append(1)):
            self.assertIsNone(bridge._desktop_preflight())
        self.assertEqual(calls, [1])

    def test_nothing_happens_without_a_saver(self):
        with bridge_patch("_screen_saver_pids", lambda: set()):
            self.assertFalse(bridge._wake_screen_saver())

    @unittest.skipUnless(os.name == "nt", "Windows process list")
    def test_process_scan_runs(self):
        self.assertIsInstance(bridge._screen_saver_pids(), set)

    @unittest.skipUnless(os.name == "nt", "Windows API")
    def test_a_failed_nudge_still_ends_the_saver(self):
        """Once the saver has run a while (display asleep), moving the mouse
        raised: the cursor belongs to the saver's desktop. That skipped closing
        it, and every screenshot failed with "screen grab failed"."""
        pids = iter([{4242}, {4242}, {4242}])
        with bridge_patch("_screen_saver_pids", lambda: next(pids, set())), \
                patch.object(bridge.time, "sleep", lambda s: None), \
                patch.object(bridge.ctypes, "windll") as windll:
            windll.user32.mouse_event.side_effect = OSError("cursor is on another desktop")
            windll.kernel32.OpenProcess.return_value = 99
            self.assertTrue(bridge._wake_screen_saver())
        windll.kernel32.TerminateProcess.assert_called_with(99, 0)

    @unittest.skipUnless(os.name == "nt", "Windows API")
    def test_nudge_is_real_input_not_a_cursor_jump(self):
        """Windows doesn't count SetCursorPos (pyautogui's moves) as input, so
        savers ignored it; mouse_event is what a real mouse sends."""
        pids = iter([{4242}, set()])
        with bridge_patch("_screen_saver_pids", lambda: next(pids, set())), \
                patch.object(bridge.time, "sleep", lambda s: None), \
                patch.object(bridge.ctypes, "windll") as windll:
            self.assertTrue(bridge._wake_screen_saver())
        moves = [c.args for c in windll.user32.mouse_event.call_args_list]
        self.assertEqual(moves, [(0x0001, 1, 0, 0, 0), (0x0001, -1, 0, 0, 0)])
        windll.kernel32.TerminateProcess.assert_not_called()


if __name__ == "__main__":
    unittest.main()
