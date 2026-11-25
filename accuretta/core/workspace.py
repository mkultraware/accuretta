# SPDX-License-Identifier: AGPL-3.0-only
"""The workspace: which folders the agent may work in, path normalizing, and what
counts as a project folder.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from pathlib import Path


WORKSPACE_FILE = DATA / "workspace.json"


def get_workspace() -> dict:
    ws = load_json(WORKSPACE_FILE, {"folders": []})
    if not isinstance(ws, dict):
        ws = {"folders": []}
    ws.setdefault("folders", [])
    
    # If no workspace folder is configured (first run or none selected),
    # automatically create and select "Accuretta Workspace" in the Documents folder.
    folders = [f for f in ws["folders"] if isinstance(f, str) and f.strip()]
    if not folders:
        try:
            doc_dir = Path(os.path.expanduser("~/Documents")).resolve()
            if not doc_dir.exists():
                doc_dir = Path.home() / "Documents"
            
            workspace_dir = doc_dir / "Accuretta Workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            
            default_folder = normalize_path(str(workspace_dir))
            ws["folders"] = [default_folder]
            save_json(WORKSPACE_FILE, ws)
        except Exception as e:
            print(f"Error creating default workspace directory: {e}")
            
    return ws


def normalize_path(p: str) -> str:
    p = os.path.expandvars(os.path.expanduser(p or ""))
    try:
        return str(Path(p).resolve())
    except Exception:
        return p


# -- deletes ------------------------------------------------------------------------
_PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "requirements.txt", "setup.py",
                    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "CMakeLists.txt", "Makefile",
                    ".gitignore", "composer.json", "Gemfile", "index.html", "main.py", "app.py")


def _root_is_project(root: str) -> bool:
    """A workspace folder that is itself a project (not a folder of projects):
    a folder directly inside it is part of the project, not a whole project."""
    try:
        if any(os.path.exists(os.path.join(root, m)) for m in _PROJECT_MARKERS):
            return True
        return any(name.endswith((".sln", ".csproj")) for name in os.listdir(root)[:200])
    except OSError:
        return False


def _workspace_roots() -> set[str]:
    roots: set[str] = set()
    for raw in get_workspace().get("folders") or []:
        try:
            path = Path(raw).resolve()
            if path.is_dir():
                roots.add(os.path.normcase(str(path)))
        except Exception:
            continue
    return roots
