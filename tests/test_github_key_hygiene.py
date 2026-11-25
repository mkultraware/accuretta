"""The GitHub key and tokens stay out of repos, chats and logs (2026-11-22).

The branch flow cloned with `git clone -c http.extraHeader=...`, which saved
Accuretta's GitHub key into the clone's .git/config. When the key expired,
every push and fetch in that repo failed with "Invalid username or token"
while gh still worked. In the same session `git credential fill` printed the
owner's live gh token into the chat. Now the key reaches git only through
the environment of one command, as the last login after the user's own,
older saved keys are taken out, and GitHub tokens in tool output are hidden.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _patching import bridge_patch

GHO = "gho_" + "a1B2c3D4e5" * 4                 # 40 chars after the prefix, made up
PAT = "github_pat_" + "11ABCDEFG0" * 8 + "xy"   # fine-grained shape, made up


class MaskTests(unittest.TestCase):
    def test_tokens_are_hidden_but_named(self):
        text = f"username=me\npassword={GHO}\nother {PAT} end"
        out = bridge._mask_github_tokens(text)
        self.assertNotIn(GHO, out)
        self.assertNotIn(PAT, out)
        self.assertIn("password=gho_…(hidden)", out)
        self.assertIn("github_pat_…(hidden) end", out)

    def test_nested_results_and_plain_text(self):
        out = bridge._mask_github_tokens({"stdout": GHO, "list": [PAT, 3], "n": 1})
        self.assertEqual(out, {"stdout": "gho_…(hidden)", "list": ["github_pat_…(hidden)", 3], "n": 1})
        for plain in ("gho_short", "the ghost of git", "ghp_ is a prefix", ""):
            self.assertEqual(bridge._mask_github_tokens(plain), plain)

    def test_tool_results_are_masked_before_the_model_sees_them(self):
        tools = {"fake_print": {"description": "t", "parameters": {"type": "object", "properties": {}},
                                "fn": lambda args: {"ok": True, "stdout": f"password={GHO}"}}}
        with patch.dict(bridge.TOOLS, tools), \
                bridge_patch("_record_action_audit", lambda *a, **k: None):
            r = bridge.invoke_tool("fake_print", {})
        self.assertNotIn(GHO, json.dumps(r))
        self.assertIn("gho_…(hidden)", r["stdout"])

    def test_command_history_is_masked(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "cmd_history.jsonl"
            with bridge_patch("CMD_HISTORY_FILE", hist), bridge_patch("DATA", Path(tmp)):
                bridge._append_cmd_history({"command": "git credential fill", "stdout": f"password={GHO}"})
            self.assertNotIn(GHO, hist.read_text(encoding="utf-8"))

    def test_live_command_lines_are_masked(self):
        seen = []
        with patch.dict(bridge._chat_emitters, {"c1": seen.append}):
            bridge._emit_tool_stream("run_powershell", f"password={GHO}", "c1")
        self.assertNotIn(GHO, seen[0]["text"])


class GitLoginTests(unittest.TestCase):
    def test_no_key_no_change(self):
        with bridge_patch("_github_cred", lambda: {}):
            env = bridge._git_env()
        self.assertNotIn("GIT_CONFIG_COUNT", env)
        self.assertNotIn("ACCURETTA_GITHUB_TOKEN", env)

    def test_key_is_a_github_only_helper_in_the_environment(self):
        with bridge_patch("_github_cred", lambda: {"pat": PAT}):
            env = bridge._git_env()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.https://github.com.helper")
        self.assertNotIn(PAT, env["GIT_CONFIG_VALUE_0"])        # the key isn't in the config value
        self.assertEqual(env["ACCURETTA_GITHUB_TOKEN"], PAT)

    def test_helper_answers_only_when_nothing_else_does(self):
        home = tempfile.mkdtemp()
        with bridge_patch("_github_cred", lambda: {"pat": PAT}):
            env = bridge._git_env()
        env.update({"HOME": home, "USERPROFILE": home, "XDG_CONFIG_HOME": home, "GIT_CONFIG_NOSYSTEM": "1"})

        def fill(host, extra_env=None):
            r = subprocess.run(["git", "credential", "fill"], input=f"protocol=https\nhost={host}\n\n",
                               capture_output=True, text=True, timeout=30, env={**env, **(extra_env or {})})
            return r.stdout

        self.assertIn(f"password={PAT}", fill("github.com"))
        self.assertNotIn(PAT, fill("gitlab.example.com"))
        # the user's own helper wins
        own = os.path.join(home, ".gitconfig")
        Path(own).write_text("[credential \"https://github.com\"]\n\thelper = "
                             "\"!f() { echo username=me; echo password=mine; }; f\"\n", encoding="utf-8")
        self.assertIn("password=mine", fill("github.com"))

    def test_branch_flow_clone_saves_no_key(self):
        calls = []

        def run_git(cwd, argv, **kw):
            calls.append(argv)
            if argv[0] == "clone":
                Path(argv[-1], ".git").mkdir(parents=True)
            return {"ok": True}

        with tempfile.TemporaryDirectory() as ws, \
                bridge_patch("_github_cred", lambda: {"pat": PAT}), \
                bridge_patch("get_workspace", lambda: {"folders": [ws]}), \
                bridge_patch("_github_rest_h", lambda *a, **k: (200, {"default_branch": "main"}, {})), \
                bridge_patch("_github_rest", lambda m, *a, **k: (200, {"object": {"sha": "abc"}})
                             if m == "GET" else (201, {})), \
                bridge_patch("_run_git", run_git):
            r = bridge._github_branch_flow("me", "proj", "feature")
        self.assertTrue(r.get("ok"), r)
        clone = next(c for c in calls if c[0] == "clone")
        self.assertEqual(clone[:2], ["clone", "--"])
        self.assertNotIn(PAT, json.dumps(calls))
        self.assertFalse(any("extraHeader" in a for c in calls for a in c))


class ForgetSavedKeysTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)

    def test_only_the_header_older_versions_wrote_is_removed(self):
        import base64
        with tempfile.TemporaryDirectory() as ws:
            ours = Path(ws, "cloned")
            theirs = Path(ws, "own-header")
            for repo in (ours, theirs):
                subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
            saved = base64.b64encode(f"x-access-token:{PAT}".encode()).decode()
            self.git(ours, "config", "--local", "http.extraheader", f"Authorization: Basic {saved}")
            self.git(theirs, "config", "--local", "http.extraheader", "Authorization: Bearer something-else")
            with bridge_patch("get_workspace", lambda: {"folders": [ws]}):
                cleaned = bridge._github_forget_saved_keys()
            self.assertEqual(cleaned, [str(ours)])
            self.assertEqual(self.git(ours, "config", "--local", "--get-all", "http.extraheader").stdout, "")
            self.assertIn("Bearer something-else",
                          self.git(theirs, "config", "--local", "--get-all", "http.extraheader").stdout)
            with bridge_patch("get_workspace", lambda: {"folders": [ws]}):
                self.assertEqual(bridge._github_forget_saved_keys(), [])     # nothing left to do


if __name__ == "__main__":
    unittest.main()
