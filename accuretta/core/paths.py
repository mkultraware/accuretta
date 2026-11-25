# SPDX-License-Identifier: AGPL-3.0-only
"""Where Accuretta's code and the user's files live.

Moved out of bridge.py; bridge.py re-exports every name here. ROOT is the
folder that holds bridge.py, two levels above this file.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Where the user's own files live: data/, skills/, the MCP key file, the email
# account file and downloaded llama.cpp builds. A git checkout keeps them next
# to the code (USER_DIR == ROOT). The Windows installer's launcher sets
# ACCURETTA_HOME to a folder outside the app, so an upgrade that replaces the
# app files never touches them. Not a user setting.
USER_DIR = Path(os.environ.get("ACCURETTA_HOME") or ROOT).resolve()
DATA = Path(os.environ.get("ACCURETTA_DATA_DIR") or USER_DIR / "data").resolve()
CHATS_FILE = DATA / "chats.json"
SETTINGS_FILE = DATA / "settings.json"
