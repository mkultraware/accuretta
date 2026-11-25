# SPDX-License-Identifier: AGPL-3.0-only
"""Spot Accuretta's private data folder in a command or in code (settings, tokens, chats).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.paths import DATA


_DATA_READABLE_SUBDIRS = ("reports", "recon_evidence")


def _soft_data_text_hit(text: str) -> bool:
    """The data folder's path, or its environment variable, appears in the
    text: in a quoted argument, inline code, an escaped Python string."""
    if "accuretta_data_dir" in (text or "").lower():
        return True
    data = str(DATA)
    if os.name == "nt":
        hay = re.sub(r"\\+", r"\\", (text or "").replace("/", "\\")).lower()
        needle, sep = data.lower(), "\\"
    else:
        hay = re.sub(r"/+", "/", text or "")
        needle, sep = data, "/"
    start = 0
    while (i := hay.find(needle, start)) >= 0:
        start = i + 1
        rest = hay[i + len(needle):]
        if rest and (rest[0].isalnum() or rest[0] in "_-"):
            continue                        # data2, data_old: another folder
        tail = re.match(r"[^\s\"'`;|&<>,()]*", rest).group(0)
        readable = any(
            re.match(re.escape(sep + sub) + r"(?![\w-])", tail, re.IGNORECASE)
            for sub in _DATA_READABLE_SUBDIRS)
        if readable and ".." not in tail:
            continue
        return True
    return False
