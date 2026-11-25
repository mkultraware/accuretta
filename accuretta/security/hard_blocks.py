# SPDX-License-Identifier: AGPL-3.0-only
"""Commands that are refused outright: wiping drives or the profile, and download-and-run (ClickFix).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
from accuretta.security.shell_text import _command_words


def _catastrophic_cmd(cmd: str) -> str | None:
    """Refuse (not merely prompt) commands that would wipe the machine or the
    user's data: whole-drive/profile recursive deletes, disk format/wipe, fork
    bombs, raw-disk overwrites. Deliberately narrow, so deleting a build folder
    still goes through normal approval. Returns a short reason, else None.
    Reads the command words only (_command_words): a commit message or an
    echo that mentions `format c:` is not a command."""
    if not cmd:
        return None
    c = " ".join(_command_words(cmd).lower().split())   # collapse whitespace so patterns stay simple
    standalone = [
        (r"\bformat\s+[a-z]:", "formats a drive"),
        (r"\bdiskpart\b", "runs diskpart (can wipe partitions)"),
        (r"\b(clear-disk|format-volume|remove-partition|initialize-disk)\b", "destroys a disk or partition"),
        (r"\bcipher\s+/w", "wipes free space (cipher /w)"),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "is a fork bomb"),
        (r"\bdd\b.*\bof=/dev/(sd|nvme|hd|disk)", "raw-writes a disk device (dd)"),
        (r"\bmkfs\.", "formats a filesystem (mkfs)"),
        (r">\s*/dev/(sd|nvme|hd)[a-z0-9]", "overwrites a raw disk device"),
    ]
    for pat, why in standalone:
        if re.search(pat, c):
            return why
    recurse_delete = bool(
        re.search(r"remove-item\b.*\brecurse\b.*\bforce\b", c)
        or re.search(r"remove-item\b.*\bforce\b.*\brecurse\b", c)
        or re.search(r"\brm\s+-\w*r\w*f\b|\brm\s+-\w*f\w*r\b", c)
        or re.search(r"\b(rd|rmdir)\s+/s\b", c)
        or re.search(r"\bdel\s+/s\b", c)
    )
    if recurse_delete:
        targets = [
            r"\s[a-z]:\\?(?:$|\s|['\"|&;])",            # a bare drive root, e.g. C:\
            r"[a-z]:\\windows\b",                       # C:\Windows
            r"[a-z]:\\users\b(?![\\a-z0-9])",           # the WHOLE C:\Users
            r"\$env:systemroot\b|\$env:windir\b",
            r"\$env:userprofile(?![\\a-z0-9])|\$home(?![\\/\w])",
            r"\brm\s+-\w+\s+/\s*(?:$|\*)",              # rm -rf /  or  rm -rf /*
            r"\brm\s+-\w+\s+~\s*(?:$|/\*)",             # rm -rf ~  or  rm -rf ~/*
            r"/mnt/[a-z](?![/\w])",                     # a Windows drive mounted in WSL, e.g. /mnt/c
            r"/mnt/[a-z]/\*",                           # /mnt/c/*  — everything on the drive
            r"/mnt/[a-z]/users(?![/\w])",               # the whole Users tree via WSL
        ]
        for pat in targets:
            if re.search(pat, c):
                return "would recursively force-delete a drive root, Windows, or your whole profile"
    return None


# ---- ClickFix hard-block ----------------------------------------------------
# `iwr <url> | iex`, `curl <url> | sh`, `irm <url>; iex $x`: one command that
# downloads and executes remote content. No legitimate agent task pipes a
# download straight into an interpreter, so these are refused outright (never
# queued for approval), like the catastrophic tier. A plain `... | iex` is
# refused too: iex is PowerShell's "execute this string" verb and has no
# read-only meaning. Returns a short reason if refused, else None.
_CLICKFIX_DL_EXEC = re.compile(
    r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b"
    r"[^\r\n]{0,400}\|\s*(?:iex\b|invoke-expression\b|sh\b|bash\b|pwsh\b|powershell\b)",
    re.IGNORECASE,
)


_CLICKFIX_FETCH_THEN_EXEC = re.compile(
    r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|curl|wget)\b"
    r"[^\r\n]{0,400}\b(?:iex|invoke-expression)\b",
    re.IGNORECASE,
)


_CLICKFIX_PIPE_EXEC = re.compile(
    r"\|\s*(?:iex\b|invoke-expression\b)",
    re.IGNORECASE,
)


def _clickfix_cmd(cmd: str) -> str | None:
    if not cmd:
        return None
    c = " ".join(_command_words(cmd).lower().split())
    if _CLICKFIX_DL_EXEC.search(c):
        return "downloads and executes a remote script in one pipeline"
    if _CLICKFIX_FETCH_THEN_EXEC.search(c):
        return "fetches remote content and runs it through iex"
    if _CLICKFIX_PIPE_EXEC.search(c):
        return "pipes command output straight into iex (an execute-anything verb)"
    return None
