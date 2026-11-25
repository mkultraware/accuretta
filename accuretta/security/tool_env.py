# SPDX-License-Identifier: AGPL-3.0-only
"""The environment for programs the agent's tools run: the scrubbed child environment
with the tool groups it needs.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env


def _tool_child_env(**extra) -> dict:
    """Environment for programs the model runs (shell, tests, sessions).

    Python on Windows writes a pipe in the ANSI code page, so a script that
    printed a check mark or an arrow crashed with UnicodeEncodeError, and it
    reads and writes files in that code page too. UTF-8 mode (what run_python
    already uses with -X utf8, and Python's own default from 3.15) makes the
    same script behave the same whichever tool runs it. A value the user set
    themselves wins."""
    env = _child_env(keep=_ENV_TOOLS)
    for name, value in {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", **extra}.items():
        env.setdefault(name, value)
    return env
