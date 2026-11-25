# SPDX-License-Identifier: AGPL-3.0-only
"""Writes the guard refused, kept for a short while so a confirmed retry can reuse the
content instead of the model sending it again.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import time
from accuretta.core.chats import _get_current_chat
from accuretta.core.workspace import normalize_path


# Content of a write the anti-clobber guard blocked, kept so a confirmed retry
# with force=true does not have to send the whole file again. One per chat and
# path; it expires after 30 minutes and goes with any successful write there.
_blocked_writes: dict[tuple[str, str], tuple[float, str]] = {}


_BLOCKED_WRITE_TTL_S = 1800


def _blocked_write_key(path: str) -> tuple[str, str]:
    return _get_current_chat() or "", os.path.normcase(normalize_path(path or ""))


def _blocked_write(path: str) -> str | None:
    key = _blocked_write_key(path)
    entry = _blocked_writes.get(key)
    if entry and time.time() - entry[0] > _BLOCKED_WRITE_TTL_S:
        _blocked_writes.pop(key, None)
        entry = None
    return entry[1] if entry else None


_FILE_IN_PARTS = ("write the file in parts: write_file with the first part (about 150 lines), "
                  "then write_file mode='append' for each next part")
