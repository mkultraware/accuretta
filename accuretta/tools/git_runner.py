# SPDX-License-Identifier: AGPL-3.0-only
"""Running git for the agent: its environment, the GitHub credential helper login, and
the streaming runner.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess
import threading
from accuretta.core.chats import _current_chat_id
from accuretta.core.tool_stream import _emit_tool_stream
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.tools.github_account import _github_cred


def _git_env() -> dict:
    env = _child_env(keep=_ENV_TOOLS)
    # Refuse to prompt for credentials / passphrases / host keys; we can't
    # answer them through a subprocess pipe and the worker would hang.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("GCM_INTERACTIVE", "Never")
    return _github_git_login(env)


# Accuretta's GitHub key (Settings → GitHub integration) as the last git login
# for github.com: git asks the user's own credential helpers (gh, Git
# Credential Manager) first and takes the key only when none of them answers.
# It travels in the environment of one git command and is never written to a
# repo. The branch flow used to clone with `git clone -c http.extraHeader=...`,
# which saved the key into the clone's .git/config; once the key expired,
# every push and fetch there failed while gh still worked (2026-11-22).
_GITHUB_GIT_HELPER = ('!f() { while read -r l && [ -n "$l" ]; do :; done; [ "$1" = get ] && '
                      'printf "username=x-access-token\\npassword=%s\\n" "$ACCURETTA_GITHUB_TOKEN"; }; f')


def _github_git_login(env: dict) -> dict:
    pat = str(_github_cred().get("pat") or "")
    if not pat:
        return env
    try:
        n = max(0, int(env.get("GIT_CONFIG_COUNT") or 0))
    except ValueError:
        n = 0
    env[f"GIT_CONFIG_KEY_{n}"] = "credential.https://github.com.helper"
    env[f"GIT_CONFIG_VALUE_{n}"] = _GITHUB_GIT_HELPER
    env["GIT_CONFIG_COUNT"] = str(n + 1)
    env["ACCURETTA_GITHUB_TOKEN"] = pat
    return env


def _run_git(cwd: str, argv: list[str], timeout: int = 60,
             max_stdout: int = 200_000, stream_label: str = "git") -> dict:
    """Run `git <argv...>` in cwd. Streams stdout/stderr like _run_powershell."""
    try:
        proc = subprocess.Popen(
            ["git", *argv],
            cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_git_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        return {"error": "git is not installed or not on PATH"}
    except Exception as e:
        return {"error": str(e)}

    out_lines: list[str] = []
    err_lines: list[str] = []

    stream_chat_id = _current_chat_id.get()

    def reader(pipe, sink, label):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                _emit_tool_stream(label, line.rstrip("\n\r"), stream_chat_id)
            pipe.close()
        except Exception:
            pass

    label = f"{stream_label} {argv[0]}" if argv else stream_label
    t_out = threading.Thread(target=reader, args=(proc.stdout, out_lines, label), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_lines, label), daemon=True)
    t_out.start(); t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": f"timeout after {timeout}s", "argv": argv}

    t_out.join(timeout=5); t_err.join(timeout=5)
    stdout = "".join(out_lines)[-max_stdout:]
    stderr = "".join(err_lines)[-8000:]
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "argv": ["git", *argv],
        "cwd": cwd,
    }
