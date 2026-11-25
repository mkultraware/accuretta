# SPDX-License-Identifier: AGPL-3.0-only
"""Checks for GitHub owner, repository and branch names, and reading a chat's saved
worktree details.

Moved out of accuretta/agent/project_follow.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

import re
from accuretta.core.ignore_rules import _workspace_root_for
from accuretta.core.workspace import normalize_path
from pathlib import Path


_GITHUB_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


_GITHUB_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,256}$")


_GITHUB_BRANCH_BAD = re.compile(r"(^|/)\.\.|@{|^/|/$|^-|(^|/)\.(git|lock)(/|$)|\.\.$")


def _github_valid_branch_name(name: str) -> str | None:
    name = (name or "").strip()
    if not _GITHUB_BRANCH_RE.fullmatch(name):
        return "invalid branch name — use letters, digits, dots, underscores, slashes and hyphens (max 256 chars)"
    if _GITHUB_BRANCH_BAD.search(name):
        return "invalid branch name — no `..`, no `@{`, no leading/trailing slash or dash, no `.git`/`.lock` segments"
    return None


def _github_worktree_metadata(raw: object) -> dict | None:
    """Validate the private context attached to a GitHub branch session."""
    if not isinstance(raw, dict):
        return None
    owner = str(raw.get("owner") or "").strip()
    repo = str(raw.get("repo") or "").strip()
    branch = str(raw.get("branch") or "").strip()
    path = normalize_path(str(raw.get("path") or ""))
    if (not _GITHUB_OWNER_RE.fullmatch(owner)
            or not _GITHUB_REPO_RE.fullmatch(repo)
            or _github_valid_branch_name(branch)
            or not path
            or not _workspace_root_for(path)
            or not (Path(path) / ".git").exists()):
        return None
    return {"owner": owner, "repo": repo, "branch": branch, "path": path}
