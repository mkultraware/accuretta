# SPDX-License-Identifier: AGPL-3.0-only
"""Following the project a chat works in: the project folder and its git metadata, the
prompt line about it, and noticing when tool calls move into another project.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shlex
from accuretta.core.chat_events import _emit_chat_event
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.ignore_rules import _workspace_root_for
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from pathlib import Path
from typing import Any


from accuretta.core.command_cwd import (  # noqa: F401  re-export: moved 2026-11-23
    _default_command_cwd,
    _project_workspace_metadata,
)


from accuretta.core.github_names import (  # noqa: F401  re-export: moved 2026-11-23
    _GITHUB_OWNER_RE,
    _GITHUB_REPO_RE,
    _GITHUB_BRANCH_RE,
    _GITHUB_BRANCH_BAD,
    _github_valid_branch_name,
    _github_worktree_metadata,
)


def _project_workspace_prompt(chat: dict | None) -> str:
    """Render a new project's private workspace context for the model."""
    project = _project_workspace_metadata((chat or {}).get("project_workspace"))
    if not project:
        return ""
    lines = [
        "active project for this chat:",
        f"- project: {project['name']}",
        f"- local path: {project['path']}",
    ]
    if project["branch"]:
        lines.append(f"- expected branch: {project['branch']}")
    if project["owner"] or (Path(project["path"]) / ".git").exists():
        lines.append(f"- GitHub publication: {'published' if project['published'] else 'local only'}")
    if project["brief"]:
        lines.append(f"- initial brief: {project['brief']}")
    lines.append(
        "Treat this as the user's current project. Inspect this path first and keep "
        "project files inside it unless the user explicitly says otherwise."
    )
    return "\n".join(lines)


# The composer's folder chip follows the agent: when a chat with no chosen
# project starts changing files in a folder (writing, editing or deleting
# files, git commands that change a repo, a clone), that folder becomes the
# chat's project, marked auto. Reading never moves it. A folder the user
# picked is never replaced; an auto one moves when the agent moves on.
_PROJECT_FOLLOW_FILE_TOOLS = {"write_file", "edit_file", "delete_file"}


_PROJECT_FOLLOW_GIT_TOOLS = {"git_add", "git_branch", "git_checkout", "git_commit", "git_init",
                             "git_pull", "git_push", "git_reset", "git_restore", "git_fetch"}


_PROJECT_FOLLOW_SHELL_TOOLS = {"run_powershell", "run_python", "run_tests"}


_GIT_WRITE_VERBS = {"add", "commit", "push", "pull", "checkout", "switch", "merge", "rebase",
                    "reset", "restore", "init", "clone", "tag", "fetch", "stash", "am", "cherry-pick"}


# git clone options that take a value, so the value isn't read as the URL or folder
_GIT_CLONE_VALUE_OPTS = {"-b", "--branch", "--depth", "-o", "--origin", "-c", "--config",
                         "--reference", "--separate-git-dir", "-j", "--jobs", "--filter",
                         "--shallow-since", "--shallow-exclude", "-u", "--upload-pack", "--template"}


def _project_follow_folder(path: str) -> str | None:
    """The project folder a path belongs to: its git repo's top folder, or else
    the folder right under the workspace root. None outside every workspace
    root, or for a file sitting directly in a root."""
    if not path:
        return None
    full = normalize_path(path)
    root = _workspace_root_for(full)
    if not root:
        return None
    here = Path(full)
    if not here.is_dir():
        here = here.parent
    root_path = Path(root)
    probe = here
    while True:
        if (probe / ".git").exists():
            return normalize_path(str(probe))
        if os.path.normcase(str(probe)) == os.path.normcase(str(root_path)) or probe.parent == probe:
            break
        probe = probe.parent
    try:
        rel = here.relative_to(root_path)
    except ValueError:
        rel = Path(os.path.relpath(str(here), str(root_path)))
    if not rel.parts or rel.parts[0] in (".", ".."):
        return None
    folder = root_path / rel.parts[0]
    return normalize_path(str(folder)) if folder.is_dir() else None


def _git_command_folders(command: str, cwd: str) -> list[str]:
    """Folders a shell command changes through git: a clone's target, the
    folder named by git -C, or the command's cwd for other changing verbs."""
    found: list[str] = []
    for segment in re.split(r"[;&|\n]+", command or ""):
        try:
            tokens = shlex.split(segment, posix=False)
        except ValueError:
            tokens = segment.split()
        tokens = [t.strip("\"'") for t in tokens]
        lowered = [t.lower() for t in tokens]
        if "git" not in lowered and "git.exe" not in lowered:
            continue
        i = lowered.index("git") if "git" in lowered else lowered.index("git.exe")
        rest = tokens[i + 1:]
        where = cwd
        while len(rest) >= 2 and rest[0] == "-C":
            where = rest[1] if os.path.isabs(rest[1]) else os.path.join(where, rest[1])
            rest = rest[2:]
        if not rest or rest[0].lower() not in _GIT_WRITE_VERBS:
            continue
        if rest[0].lower() == "clone":
            positional, skip = [], False
            for tok in rest[1:]:
                if skip:
                    skip = False
                    continue
                if tok.startswith("-"):
                    skip = tok in _GIT_CLONE_VALUE_OPTS
                    continue
                positional.append(tok)
            if not positional:
                continue
            dest = positional[1] if len(positional) > 1 else re.sub(r"\.git$", "", positional[0].rstrip("/\\").replace("\\", "/").split("/")[-1].split(":")[-1])
            found.append(dest if os.path.isabs(dest) else os.path.join(where, dest))
        else:
            found.append(where)
    return found


def _project_follow_target(canon: str, args: dict) -> str | None:
    """Where a successful tool call changed things, as a project folder."""
    args = args if isinstance(args, dict) else {}
    cwd = _default_command_cwd()

    def full(p):
        p = str(p or "").strip()
        return p if not p or os.path.isabs(p) else os.path.join(cwd, p)

    candidates: list[str] = []
    if canon in _PROJECT_FOLLOW_FILE_TOOLS:
        candidates.append(full(args.get("path")))
    elif canon in _PROJECT_FOLLOW_GIT_TOOLS:
        candidates.append(full(args.get("path")) or cwd)
    elif canon == "git_clone":
        dest = args.get("dest")
        if not dest:
            url = str(args.get("url") or "").rstrip("/")
            dest = re.sub(r"\.git$", "", url.split("/")[-1].split(":")[-1]) if url else ""
        candidates.append(full(dest))
    elif canon in _PROJECT_FOLLOW_SHELL_TOOLS:
        command = str(args.get("command") or args.get("code") or "")
        candidates.extend(_git_command_folders(command, full(args.get("cwd")) or cwd))
    for path in reversed(candidates):
        folder = _project_follow_folder(path)
        if folder:
            return folder
    return None


def _project_follow(canon: str, args: dict, result: Any) -> None:
    """Point the chat's project at the folder the agent just changed things in,
    unless the user chose one, and tell the open composer."""
    if not isinstance(result, dict) or result.get("error") or result.get("not_executed"):
        return
    if canon not in (_PROJECT_FOLLOW_FILE_TOOLS | _PROJECT_FOLLOW_GIT_TOOLS
                     | _PROJECT_FOLLOW_SHELL_TOOLS | {"git_clone"}):
        return
    chat_id = _current_chat_id.get() or _get_current_chat()
    if not chat_id:
        return
    try:
        folder = _project_follow_target(canon, args)
        if not folder:
            return
        chats = get_chats()
        chat = chats.get("chats", {}).get(chat_id)
        if not isinstance(chat, dict) or chat.get("github_worktree"):
            return
        current = chat.get("project_workspace")
        if isinstance(current, dict) and current.get("path"):
            if not current.get("auto"):
                return
            if os.path.normcase(normalize_path(current["path"])) == os.path.normcase(folder):
                return
        project = _project_workspace_metadata({"path": folder, "name": Path(folder).name, "auto": True})
        if not project:
            return
        chat["project_workspace"] = project
        save_json(CHATS_FILE, chats)
        _emit_chat_event({"type": "project_follow", "chat_id": chat_id, "project_workspace": project})
    except Exception:
        pass
