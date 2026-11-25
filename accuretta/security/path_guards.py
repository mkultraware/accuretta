# SPDX-License-Identifier: AGPL-3.0-only
"""Where the file tools may go: inside the workspace (or anywhere when desktop-wide
access is on), never into system paths, Accuretta's data folder or key files.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.paths import DATA
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.security.data_paths import _KEY_FILE_NAMES
from accuretta.security.data_paths import is_data_path


BLOCKED_PATH_PATTERNS = [
    re.compile(r"^[a-zA-Z]:\\Windows(\\|$)", re.IGNORECASE),
    re.compile(r"\\System32\\", re.IGNORECASE),
    re.compile(r"^[a-zA-Z]:\\Windows\\System32", re.IGNORECASE),
]


def is_blocked_path(p: str) -> bool:
    n = normalize_path(p)
    for pat in BLOCKED_PATH_PATTERNS:
        if pat.search(n):
            return True
    return False


# Accuretta's own data folder holds settings (with tokens), credentials,
# chats and memories. File tools refuse it whatever the workspace says, even
# with no workspace folders (open access). The agent may still read, never
# change, the red-team reports and the evidence files they cite.
# The MCP config sits in the app folder, not in DATA, but holds the MCP
# servers' API keys, so it is refused the same way (read and write alike).
DATA_PATH_ERROR = "path blocked (Accuretta's private data: settings, keys and chats)"


def _is_data_dir(path: str) -> bool:
    """Cheap test for directory walks: is this directory DATA itself? Walks
    prune it, so nothing below is visited. A symlink is resolved first."""
    if os.path.islink(path):
        return is_data_path(path, write=True)
    return os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(DATA))


def _data_file_in_walk(path: str) -> bool:
    """For files found by a walk: the MCP key file, or a symlink (the only way
    into DATA once the DATA directory itself is pruned)."""
    if os.path.basename(path).lower() in _KEY_FILE_NAMES:
        return is_data_path(path)
    return os.path.islink(path) and is_data_path(path)


def _desktop_wide_enabled(settings: dict | None = None) -> bool:
    """Desktop automation toggle is the opt-in for system-wide tool use.

    Kept separate from get_settings() (which reads settings.json on every call)
    so the common inside-workspace path of is_in_workspace() never pays for a
    disk read; the toggle is only consulted once a path is already known to be
    outside the configured workspace folders.
    """
    try:
        s = settings if settings is not None else load_json(SETTINGS_FILE, {})
        return bool(s.get("desktop_enabled")) if isinstance(s, dict) else False
    except Exception:
        return False


def is_in_workspace(p: str) -> bool:
    """True if path is inside any workspace folder. Empty workspace = open access.

    With desktop automation on, workspace tools may also operate system-wide
    (the user's opt-in). The hard blocks still apply downstream:
    is_blocked_path (Windows/System32, registry), _catastrophic_cmd,
    bridge_self_threat, approval gates and the red-team scope blocks.

    Accuretta's data folder is never inside the workspace (see is_data_path),
    so any tool gated on this refuses it too."""
    if is_data_path(p):
        return False
    ws = get_workspace().get("folders", [])
    if not ws:
        return True
    n = normalize_path(p).lower()
    for folder in ws:
        f = normalize_path(folder).lower()
        if n == f or n.startswith(f + os.sep):
            return True
    if _desktop_wide_enabled():
        return not is_blocked_path(p)
    return False


def _fw_check_path(path: str, must_exist: bool = True) -> tuple[str, dict | None]:
    """Resolve + validate a workspace path. Returns (resolved, error_or_None)."""
    p = normalize_path(path or "")
    if not p:
        return "", {"error": "missing path"}
    if must_exist and not os.path.exists(p):
        return p, {"error": f"not found: {p}"}
    if is_blocked_path(p):
        return p, {"error": "path blocked (Windows/System32)"}
    if is_data_path(p):
        return p, {"error": DATA_PATH_ERROR}
    if not is_in_workspace(p):
        return p, {"error": "path outside workspace. Add folder in Workspace panel."}
    return p, None
