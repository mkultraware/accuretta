# SPDX-License-Identifier: AGPL-3.0-only
"""Read a shell command line: quoted text, here-strings and the words that run.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re


def _shell_subexpressions(raw: str, posix: bool) -> list[str]:
    """Bodies of $(...) (balanced) and, in POSIX, `...` inside a string."""
    subs, i = [], 0
    while True:
        i = raw.find("$(", i)
        if i == -1:
            break
        depth, j = 1, i + 2
        while j < len(raw) and depth:
            depth += {"(": 1, ")": -1}.get(raw[j], 0)
            j += 1
        subs.append(raw[i + 2:j - 1] if depth == 0 else raw[i + 2:])
        i = j
    if posix:
        subs += re.findall(r"`([^`]*)`", raw)
    return subs


class _HereString(str):
    """A here-string or heredoc body: text a command reads, not a command."""


def _shell_code_view(cmd: str, posix: bool) -> tuple[str, list[str]]:
    """Return the command with every quoted string, here-string, heredoc body
    and comment replaced by a numbered placeholder `"N"` (comments by a
    space), plus the literal values by number. Subexpressions inside an
    expanding literal (`$(...)`, POSIX backticks) still run, so their bodies
    follow the placeholder as code."""
    out: list[str] = []
    lits: list[str] = []

    def literal(raw: str, value: str, expands: bool) -> None:
        out.append(f'"{len(lits)}"')
        lits.append(value)
        if expands:
            # Subexpressions inside an expanding string run: keep them as code.
            for sub in _shell_subexpressions(raw, posix):
                out.append(f" ( {sub} ) ")

    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        word_start = i == 0 or cmd[i - 1] in " \t\r\n;|&(){}"
        if not posix and c == "@" and cmd.startswith(('@"', "@'"), i):
            m = re.compile(r"@([\"'])\r?\n(.*?)\r?\n\1@", re.S).match(cmd, i)
            if m:
                literal(m.group(0), _HereString(m.group(2)), m.group(1) == '"')
                i = m.end()
                continue
        if posix and cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            m = re.compile(r"<<-?\s*(['\"]?)(\w+)\1").match(cmd, i)
            line_end = cmd.find("\n", i)
            if m and line_end != -1:
                term = re.compile(r"^\t*" + re.escape(m.group(2)) + r"[ \t]*\r?$", re.M).search(cmd, line_end + 1)
                if term:
                    body = cmd[line_end + 1:term.start()]
                    out.append(cmd[i:line_end + 1])
                    literal(body, body, not m.group(1))
                    i = term.end()
                    continue
        if not posix and cmd.startswith("<#", i):
            end = cmd.find("#>", i + 2)
            if end == -1:
                out.append(cmd[i:])
                break
            out.append(" ")
            i = end + 2
            continue
        if c == "#" and word_start:
            end = cmd.find("\n", i)
            out.append(" ")
            i = n if end == -1 else end
            continue
        if c in "'\"":
            j = i + 1
            while j < n:
                ch = cmd[j]
                if c == '"' and ((posix and ch == "\\") or (not posix and ch == "`")):
                    j += 2
                    continue
                if ch == c:
                    if not posix and j + 1 < n and cmd[j + 1] == c:   # '' / "" escape
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:                       # unterminated: keep the rest as code
                out.append(cmd[i:])
                break
            literal(cmd[i:j + 1], cmd[i + 1:j], c == '"')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out), lits


# Text the hard blocks must not read as commands: a multi-line here-string, a
# commit message, a value written to a file, a search pattern, what echo prints.
_BLOCK_DATA_PARAMS = {"-m", "--message", "-message", "-value", "-inputobject", "-pattern",
                      "-body", "--body", "-simplematch", "-object"}


_BLOCK_PRINT_HEADS = {"write-output", "write-host", "echo", "write", "write-verbose",
                      "write-warning", "write-information", "write-debug", "out-host", "printf"}


_BLOCK_EXEC_BEFORE_RE = re.compile(
    r"(?:\b(?:iex|invoke-expression)|\s-(?:command|c|e|enc|ec|encodedcommand)|\s/[ckr]|--eval|"
    r"scriptblock\]::create\(|\b(?:sh|bash|zsh|dash|ksh|wsl)(?:\.exe)?(?:\s+-\S+)*)\s*\(?\s*$",
    re.IGNORECASE)


_BLOCK_EXEC_AFTER_RE = re.compile(
    r"\s*\|\s*&?\s*(?:iex|invoke-expression|powershell|pwsh|cmd|sh|bash|zsh|python[\d.]*|py|node)(?:\.exe)?\b",
    re.IGNORECASE)


def _command_words(cmd: str) -> str:
    """The command as it runs, for the hard blocks. Quoted arguments are put
    back, but text that is only data stays out unless it is handed to an
    interpreter (iex, -Command, cmd /c, bash -c, `| sh`). A commit message
    that mentioned `| iex` was refused as ClickFix with no way to approve it."""
    text = cmd or ""
    if not text.strip():
        return ""
    try:
        code, lits = _shell_code_view(text, False)
    except Exception:
        return text
    out, pos = [], 0
    for m in re.finditer(r'"(\d+)"', code):
        out.append(code[pos:m.start()])
        pos = m.end()
        try:
            value = lits[int(m.group(1))]
        except (IndexError, ValueError):
            out.append(m.group(0))
            continue
        before = code[max(0, m.start() - 60):m.start()]
        after = code[m.end():m.end() + 60]
        if not (_BLOCK_EXEC_BEFORE_RE.search(before) or _BLOCK_EXEC_AFTER_RE.match(after)):
            prev = before.split()
            seg = re.split(r"\|\||&&|[;|&\n{}()]", before)[-1].split()
            if (isinstance(value, _HereString) or "\n" in value
                    or (prev and prev[-1].lower() in _BLOCK_DATA_PARAMS)
                    or (seg and seg[0].lower() in _BLOCK_PRINT_HEADS)):
                out.append('""')
                continue
        out.append('"' + value + '"')
    out.append(code[pos:])
    return "".join(out)
