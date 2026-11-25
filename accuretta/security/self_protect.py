# SPDX-License-Identifier: AGPL-3.0-only
"""Commands that would kill Accuretta or destroy its own files.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import sys
from accuretta.core.endpoints import PORT
from accuretta.core.paths import ROOT
from accuretta.security.shell_text import _command_words
from pathlib import Path


_BRIDGE_PID = os.getpid()


# The running server's own code is bridge.py plus the accuretta package next
# to it (most of the server moved there). Both are taken from the app folder,
# not from __file__, so moving this code into a module can't change what it
# protects.
_BRIDGE_FILE_ABS = ""
_SERVER_PACKAGE_ABS = ""
try:
    _BRIDGE_FILE_ABS = str((ROOT / "bridge.py").resolve()).lower()
    _SERVER_PACKAGE_ABS = str((ROOT / "accuretta").resolve()).lower()
except Exception:
    _BRIDGE_FILE_ABS = _SERVER_PACKAGE_ABS = ""


def is_bridge_self_path(path: str) -> bool:
    """True if `path` is the running server's own code: bridge.py, or the
    accuretta package folder or anything in it."""
    if not path or not _BRIDGE_FILE_ABS:
        return False
    try:
        p = str(Path(path).resolve()).lower()
    except Exception:
        return False
    if p == _BRIDGE_FILE_ABS:
        return True
    pkg = _SERVER_PACKAGE_ABS
    return bool(pkg) and (p == pkg or p.startswith(pkg + os.sep))


# Patterns matched against the raw PowerShell command string.
_PROT_BRIDGE_FILE_RE = re.compile(r"\bbridge\.py\b", re.IGNORECASE)


_PROT_DESTROY_FILE_RE = re.compile(
    r"\b(remove-item|del|erase|rd|rmdir|move-item|out-file|set-content|add-content|"
    r"clear-content|new-item\s+-force)\b",
    re.IGNORECASE,
)


_PROT_KILL_VERB_RE = re.compile(
    r"\b(stop-process|taskkill|tskill|kill)\b",
    re.IGNORECASE,
)


_PROT_PORT_RE = re.compile(r"(?<!\d)8787(?!\d)")


def bridge_self_threat(cmd: str) -> str | None:
    """Return a refusal reason if `cmd` would kill or corrupt the bridge.
    Pattern-only check — runs before approval so even an approved Stop-Process
    against our PID gets refused. Returns None if the command is safe.
    Reads the command words (quoted data such as a commit message is not a
    command) and also refuses killing every process of Accuretta's own
    program by name (`Get-Process python | Stop-Process`, `taskkill /IM
    python.exe`), which takes Accuretta down with it."""
    if not cmd:
        return None
    raw_cmd = cmd
    cmd = _command_words(cmd) or cmd
    for pattern in _SELF_KILL_RES:
        if pattern.search(cmd):
            return ("command stops every process of Accuretta's own program by name, "
                    "which would stop Accuretta too; target the exact PID instead")
    if not cmd.strip():
        cmd = raw_cmd

    # Direct PID reference + a kill verb.
    if str(_BRIDGE_PID) in cmd and _PROT_KILL_VERB_RE.search(cmd):
        # Avoid false positives: the PID has to appear as a standalone token,
        # not as a substring of some unrelated number.
        if re.search(rf"(?<!\d){_BRIDGE_PID}(?!\d)", cmd):
            return f"command targets the bridge's own process (pid {_BRIDGE_PID})"

    # bridge.py + a destructive file verb.
    if _PROT_BRIDGE_FILE_RE.search(cmd) and _PROT_DESTROY_FILE_RE.search(cmd):
        return "command would overwrite or delete bridge.py"

    # Port 8787 + a kill verb. `Get-NetTCPConnection -LocalPort 8787` on its
    # own is fine (read-only); only refuse when paired with a process killer.
    if _PROT_PORT_RE.search(cmd) and _PROT_KILL_VERB_RE.search(cmd):
        return f"command would kill the bridge's listener (port {PORT})"

    return None


def _bridge_image_names_re() -> str:
    stem = Path(sys.executable).stem.lower()
    return r"(?:python[\w.]*|py)" if stem.startswith("python") or stem == "py" else re.escape(stem)


_SELF_KILL_RES = [re.compile(p, re.IGNORECASE) for p in (
    rf"\b(?:stop-process|spps|kill)\b[^\n;|]*-name\s+[\"']?(?:{_bridge_image_names_re()})?\*?(?:\.exe)?[\"']?(?=[\s,;|)]|$)",
    rf"\b(?:get-process|gps)\b[^\n;|]*?(?:-name\s+)?[\"']?(?:{_bridge_image_names_re()})\*?[\"']?(?=[\s,;|)]|$)"
    rf"[^\n;|]*\|\s*(?:stop-process|spps|kill)\b",
    rf"\btaskkill(?:\.exe)?\b[^\n;|&]*\s/im\s+[\"']?(?:{_bridge_image_names_re()})\*?(?:\.exe)?[\"']?(?=[\s;|&]|$)",
    rf"\b(?:pkill|killall)\b[^\n;|&]*\b(?:{_bridge_image_names_re()})\b",
    rf"\bwmic\b[^\n;|&]*name\s*=\s*[\"']?(?:{_bridge_image_names_re()})\.exe[\"']?[^\n;|&]*\b(?:delete|terminate)\b",
)]
