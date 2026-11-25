# SPDX-License-Identifier: AGPL-3.0-only
"""Running Windows binaries quietly: the no-console flag, and checking that a system
binary is the trusted one in System32.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess


def _trusted_windows_binary(*relative_parts: str) -> str:
    if os.name != "nt":
        return ""
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    for system_dir in ("Sysnative", "System32"):
        candidate = os.path.abspath(os.path.join(windows_root, system_dir, *relative_parts))
        if os.path.isfile(candidate):
            return candidate
    return ""


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
