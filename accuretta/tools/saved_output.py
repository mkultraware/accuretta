# SPDX-License-Identifier: AGPL-3.0-only
"""Full tool results too big for the model's context, saved to files the model can read back.

Moved out of accuretta/agent/tool_output.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path


# Where a result too big for the model's context is kept in full. The model
# reads it back with read_file (allowed here though it is outside the
# workspace). Named by content, so the same output is written once; the oldest
# files go first.
_TOOL_OUTPUT_DIR = Path(tempfile.gettempdir()) / "accuretta-output"


_TOOL_OUTPUT_KEEP = 200


def _save_full_output(name: str, text: str) -> str:
    try:
        data = text.encode("utf-8", errors="replace")[:8 * 1024 * 1024]
        _TOOL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^\w-]", "_", str(name))[:40] or "tool"
        path = _TOOL_OUTPUT_DIR / f"{slug}-{hashlib.sha1(data).hexdigest()[:12]}.txt"
        if not path.exists():
            path.write_bytes(data)
            older = sorted(_TOOL_OUTPUT_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime)
            for old in older[:-_TOOL_OUTPUT_KEEP]:
                old.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return ""


def _is_saved_tool_output(path: str) -> bool:
    root = os.path.normcase(os.path.abspath(str(_TOOL_OUTPUT_DIR)))
    return os.path.normcase(os.path.abspath(path)).startswith(root + os.sep)
