# SPDX-License-Identifier: AGPL-3.0-only
"""Paths the agent must treat with care: Accuretta's own data folder and files that hold
keys or credentials.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.email_paths import EMAIL_CONFIG_FILE
from accuretta.core.mcp_paths import _MCP_CONFIG_FILE
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from accuretta.core.workspace import normalize_path
from accuretta.security.data_guard import _DATA_READABLE_SUBDIRS
from pathlib import Path


_KEY_FILE_NAMES = ("bridge_mcp_config.json", "bridge_mcp_config.json.tmp")


def _path_within(path: str, folder: str) -> bool:
    p = os.path.normcase(path)
    f = os.path.normcase(folder).rstrip("\\/")
    return p == f or p.startswith(f + os.sep)


def _key_files() -> set[str]:
    """Normalized paths of the MCP config (and the temp file it is saved
    through), in the app folder and wherever _MCP_CONFIG_FILE points."""
    folders = {ROOT, USER_DIR}
    # Read by name, not via globals(): in bridge.py a globals() lookup found
    # these, but once this moved here it silently found nothing and a
    # redirected key file was no longer refused. Imported names are also what
    # bridge_patch and the test setup redirect.
    cfg = _MCP_CONFIG_FILE
    if cfg is not None:
        folders.add(Path(cfg).parent)
    files = {os.path.normcase(os.path.normpath(str(folder / name)))
             for folder in folders for name in _KEY_FILE_NAMES}
    # The email account file (mailbox password) is refused the same way; the
    # email scripts read it themselves and never print the password.
    email_cfg = EMAIL_CONFIG_FILE or USER_DIR / "email_plugin" / "email_config.json"
    files.update(os.path.normcase(os.path.normpath(str(email_cfg) + suffix)) for suffix in ("", ".tmp"))
    return files


def is_key_file(p: str) -> bool:
    n = normalize_path(p)
    return bool(n) and os.path.normcase(n) in _key_files()


def is_data_path(p: str, write: bool = False) -> bool:
    """True when a file tool must refuse p: inside DATA, or an MCP key file."""
    n = normalize_path(p)
    if not n:
        return False
    if os.path.normcase(n) in _key_files():
        return True
    if not _path_within(n, str(DATA)):
        return False
    if not write:
        for sub in _DATA_READABLE_SUBDIRS:
            if _path_within(n, str(DATA / sub)):
                return False
    return True
