# SPDX-License-Identifier: AGPL-3.0-only
"""Path checks for soft approval: temp folders, how deep a path sits under a root, and
whether a delete or write target is a safe one.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import contextvars
import os
import tempfile
from accuretta.core.workspace import _root_is_project
from accuretta.core.workspace import _workspace_roots
from pathlib import Path


_SOFT_REAL_LINUX: contextvars.ContextVar[bool] = contextvars.ContextVar("_soft_real_linux", default=False)


def _soft_temp_roots() -> list[str]:
    roots = {tempfile.gettempdir(), os.environ.get("TEMP") or "", os.environ.get("TMP") or ""}
    return [os.path.normcase(os.path.normpath(r)) for r in roots if r]


def _soft_depth_under(path: str, root: str) -> int:
    """Path components below root, or -1 when path is not inside root."""
    rel = os.path.relpath(path, root) if os.path.splitdrive(path)[0] == os.path.splitdrive(root)[0] else ".."
    if rel == ".":
        return 0
    if rel.startswith(".."):
        return -1
    return len(Path(rel).parts)


def _soft_target_ok(resolved: tuple[str, str] | None, *, delete: bool) -> bool:
    """Writes: anywhere in the workspace or temp. Deletions: inside a project
    folder (two levels below a workspace root), a single top-level file, or
    inside temp. A wildcard is
    only allowed in the last component; a match-all `*` counts as the folder."""
    if resolved is None:
        return False
    zone, path = resolved
    sep = "/" if zone == "guest" else "\\"
    parts = path.split(sep)
    if any(ch in "".join(parts[:-1]) for ch in "*?["):
        return False
    wildcard = any(ch in parts[-1] for ch in "*?[")
    base = sep.join(parts[:-1]) if wildcard else path
    # `x\*.png` is narrower than its folder; a bare `*` is the whole folder.
    extra = 1 if wildcard and parts[-1] not in ("*", "*.*") else 0
    if zone == "guest":
        if _SOFT_REAL_LINUX.get():
            # The user's own WSL distro (not Accuretta's sandbox): only /tmp is scratch.
            return base.startswith("/tmp/") and (len([p for p in base.split("/") if p]) + extra) >= 2
        return (len([p for p in base.split("/") if p]) + extra) >= 2
    for root in _soft_temp_roots():
        depth = _soft_depth_under(base, root)
        if depth >= 0 and depth + extra >= 1:
            return True
    for root in _workspace_roots():
        depth = _soft_depth_under(base, root)
        if depth < 0:
            continue
        if depth + extra >= (2 if delete else 1):
            return True
        if delete and depth == 1 and not wildcard:
            # One existing file at the top of the workspace is a small loss; a
            # path that isn't there loses nothing; a folder directly inside a
            # workspace folder that is itself a project is part of that project.
            # A top-level folder of a folder of projects is a whole project.
            if os.path.isfile(path) or not os.path.exists(path) or _root_is_project(root):
                return True
        if delete and depth + extra == 1 and wildcard and _root_is_project(root):
            return True
    return False
