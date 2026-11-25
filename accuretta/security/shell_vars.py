# SPDX-License-Identifier: AGPL-3.0-only
"""Follow shell variables in a command line: single literal assignments and their expansion.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _soft_assignments(code: str, lits: list[str], posix: bool) -> dict[str, str]:
    """Variables assigned a single literal (`$dir = "C:\\x"`, `d=/tmp/x`).
    A variable assigned more than once, or from anything else, is unknown."""
    seen: dict[str, str | None] = {}
    if posix:
        pattern = r"(?:^|[\s;&|])([A-Za-z_]\w*)=(\"(\d+)\"|[^\s;&|\"]*)"
    else:
        pattern = r"\$([A-Za-z_]\w*)\s*=\s*(\"(\d+)\"|[^\s;|]*)"
    for m in re.finditer(pattern, code):
        name = m.group(1).lower()
        value = lits[int(m.group(3))] if m.group(3) is not None else m.group(2)
        jp = None if posix else re.match(
            r"join-path\s+(?:-path\s+)?(\"\d+\"|\$\w+)\s+(?:-childpath\s+)?(\"\d+\"|[^\s;|)]+)",
            code[m.start(2):], re.IGNORECASE)
        if jp:
            # $snap = Join-Path $pub "new-snap.png"
            part = lambda t: lits[int(t[1:-1])] if t.startswith('"') else t  # noqa: E731
            value = part(jp.group(1)).rstrip("\\/") + "\\" + part(jp.group(2)).lstrip("\\/")
        seen[name] = None if name in seen else value
    return {k: v for k, v in seen.items() if v is not None}


def _soft_expand(text: str, variables: dict[str, str], posix: bool, depth: int = 0) -> str | None:
    """Expand $var / ${var} / $env:NAME / %NAME% / ~ in a path. None when a
    variable is unknown, so the caller treats the path as unresolved."""
    unknown = False

    def var(m: re.Match) -> str:
        nonlocal unknown
        env, name = m.group(1), m.group(2)
        if env:
            value = os.environ.get(name)
        elif name.upper() == "HOME":
            value = "/root" if posix else str(Path.home())
        elif posix and name == "TMPDIR":
            value = "/tmp"
        else:
            value = variables.get(name.lower())
            if value is not None:
                value = _soft_expand(value, variables, posix, depth + 1) if depth < 4 else None
        if value is None:
            unknown = True
            return ""
        return value

    def cmd_env(m: re.Match) -> str:
        nonlocal unknown
        value = os.environ.get(m.group(1))
        if value is None:
            unknown = True
            return ""
        return value

    out = re.sub(r"\$\{?(env:)?([A-Za-z_]\w*)\}?", var, text, flags=re.IGNORECASE)
    out = re.sub(r"%([A-Za-z_]\w*)%", cmd_env, out)
    if out.startswith("~"):
        out = ("/root" if posix else str(Path.home())) + out[1:]
    return None if unknown else out
