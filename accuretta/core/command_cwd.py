# SPDX-License-Identifier: AGPL-3.0-only
"""The folder a host command runs in when the model names none, and the project
details saved on a project chat.

Moved out of accuretta/agent/project_follow.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.github_names import _GITHUB_OWNER_RE
from accuretta.core.github_names import _github_valid_branch_name
from accuretta.core.github_names import _github_worktree_metadata
from accuretta.core.ignore_rules import _workspace_root_for
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from pathlib import Path


def _default_command_cwd() -> str:
    """The folder a host command runs in when the model names none: the chat's
    branch worktree or project, else the first workspace folder."""
    try:
        chat = (get_chats().get("chats", {}) or {}).get(_get_current_chat()) or {}
        for meta in (_github_worktree_metadata(chat.get("github_worktree")),
                     _project_workspace_metadata(chat.get("project_workspace"))):
            if meta and os.path.isdir(meta["path"]):
                return normalize_path(meta["path"])
    except Exception:
        pass
    for folder in get_workspace().get("folders", []) or []:
        if isinstance(folder, str) and folder.strip() and os.path.isdir(folder):
            return normalize_path(folder)
    return os.getcwd()


def _project_workspace_metadata(raw: object) -> dict | None:
    """Validate the private context attached to a project chat.

    New-project sessions carry git metadata (branch, publication). A folder
    picked from the composer is any directory inside a configured workspace
    root, so only path membership and a matching label are required."""
    if not isinstance(raw, dict):
        return None
    path = normalize_path(str(raw.get("path") or ""))
    if not path or not _workspace_root_for(path) or not Path(path).is_dir():
        return None
    name = str(raw.get("name") or "").strip() or Path(path).name
    branch = str(raw.get("branch") or "").strip()
    owner = str(raw.get("owner") or "").strip()
    if (not name
            or len(name) > 120
            or re.search(r"[\r\n\x00]", name)
            or Path(path).name.lower() != name.lower()
            or (branch and _github_valid_branch_name(branch))
            or (owner and not _GITHUB_OWNER_RE.fullmatch(owner))):
        return None
    return {
        "name": name,
        "path": path,
        "branch": branch,
        "brief": str(raw.get("brief") or "").strip()[:4000],
        "published": bool(raw.get("published")),
        "owner": owner,
        "auto": bool(raw.get("auto")),
    }
