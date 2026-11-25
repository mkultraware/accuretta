# SPDX-License-Identifier: AGPL-3.0-only
"""Files PowerShell writes with a UTF-8 BOM: finding a command's output files so the BOM
can be removed.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re


# Text files keep their own line endings and BOM. Path.write_text() on Windows
# writes every "\n" as "\r\n", so one edit_file call on an LF file used to flip
# every line ending: git showed the whole file as changed and LF-enforcing
# formatters failed. Edits work on LF text; the original format is restored on
# write.
_UTF8_BOM = b"\xef\xbb\xbf"


# Windows PowerShell 5.1 writes `>`, `>>` and Out-File as UTF-16, which no
# Python, Node or JSON reader opens as text (`python gen.py > big.log` then
# reading big.log failed with "can't decode byte 0xff"). The prelude makes them
# UTF-8; 5.1's UTF-8 starts with a byte-order mark, which breaks json.loads and
# hides in a CSV's first column, so the files this command creates are cleaned
# of it: before each later command in the same run (a lookup hook; the built-in
# Out-File stays in charge of writing) and once more after the run.
_PS_REDIRECT_TARGET_RE = re.compile(
    r"(?<![\w-])[1-6*]?>>?(?!&)\s*(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s;|&(){}'\"]+)")


_PS_OUTFILE_RE = re.compile(r"\bOut-File\b([^;|\r\n]*)", re.IGNORECASE)


_PS_TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|\S+")


def _ps_write_targets(cmd: str) -> list[str]:
    """Files a PowerShell command writes with >, >> or Out-File without naming
    an encoding, as written (unresolved)."""
    found = [m.group(1) for m in _PS_REDIRECT_TARGET_RE.finditer(cmd or "")]
    for m in _PS_OUTFILE_RE.finditer(cmd or ""):
        tokens = _PS_TOKEN_RE.findall(m.group(1))
        if any(t.lower().startswith("-e") for t in tokens):
            continue                      # an explicit -Encoding is the model's choice
        i = 0
        while i < len(tokens):
            tok, low = tokens[i], tokens[i].lower()
            if low.startswith(("-f", "-l", "-psp", "-pa")):
                if i + 1 < len(tokens):
                    found.append(tokens[i + 1])
                break
            if low.startswith(("-w", "-i")):
                i += 2                    # -Width / -InputObject take a value
                continue
            if not low.startswith("-"):
                found.append(tok)         # the first positional argument is the file
                break
            i += 1
    return found


def _ps_bom_candidates(cmd: str, cwd: str) -> list[str]:
    """Absolute paths of this command's write targets that may lose a new BOM:
    files that do not exist yet or exist without one."""
    out: list[str] = []
    for raw in _ps_write_targets(cmd):
        path = raw.strip().strip("'\"").strip()
        low = path.lower()
        if (not path or "$" in path or "`" in path or low in ("nul", "con", "null")
                or low.startswith(("\\\\.\\", "-"))):
            continue
        try:
            path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
            if os.path.isdir(path):
                continue
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    if fh.read(3) == _UTF8_BOM:
                        continue          # it already had one: not ours to remove
        except (OSError, ValueError):
            continue
        if path not in out:
            out.append(path)
        if len(out) >= 8:
            break
    return out
