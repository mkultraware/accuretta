# SPDX-License-Identifier: AGPL-3.0-only
"""Folders for the UI: opening one in the file manager, listing subfolders, and checking
a path is in the workspace.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import subprocess
import sys
from accuretta.core.workspace import normalize_path
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.path_guards import is_blocked_path
from pathlib import Path


_SUBFOLDER_LIST_CAP = 1000


def _list_subfolders(raw: str) -> dict:
    """Folders inside `raw`, or the starting points (home folder, then each
    drive on Windows or / elsewhere) when `raw` is empty. Names only, never
    file contents."""
    raw = str(raw or "").strip()
    if not raw:
        roots: list[dict] = []
        home = Path.home()
        if home.is_dir():
            roots.append({"name": home.name or str(home), "path": str(home), "kind": "home"})
        if os.name == "nt":
            import string
            for letter in string.ascii_uppercase:
                drive = Path(f"{letter}:\\")
                try:
                    if drive.exists():
                        roots.append({"name": f"{letter}:", "path": str(drive), "kind": "drive"})
                except OSError:
                    continue
        else:
            roots.append({"name": "/", "path": "/", "kind": "drive"})
        return {"path": "", "entries": roots}
    try:
        target = Path(normalize_path(raw)).resolve()
    except Exception:
        return {"path": raw, "error": "bad path", "entries": []}
    if not target.is_dir():
        return {"path": str(target), "error": "not a folder", "entries": []}
    entries: list[dict] = []
    try:
        for child in sorted(target.iterdir(), key=lambda c: c.name.lower()):
            try:
                if child.is_dir():
                    entries.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
            if len(entries) >= _SUBFOLDER_LIST_CAP:
                break
    except PermissionError:
        return {"path": str(target), "error": "permission denied", "entries": []}
    except OSError as e:
        return {"path": str(target), "error": str(e), "entries": []}
    return {"path": str(target), "entries": entries}


def _path_in_workspace(target: Path, folders: list) -> bool:
    """True when `target` (already resolved) is a workspace folder or inside one.

    Compares whole path components: a plain string-prefix check let C:\\foo
    also admit C:\\foobar."""
    for f in folders or []:
        try:
            root = Path(f).resolve()
        except Exception:
            continue
        if target == root or root in target.parents:
            return True
    return False


def _open_folder_in_file_manager(raw_path: str) -> dict:
    """Open a folder in the OS file manager (Explorer/Finder/xdg-open).

    The only guard is a protected system path (Windows/System32, registry hives,
    ...); the agent can't write there anyway, so this never blocks a folder it
    actually saved to."""
    p = str(raw_path or "").strip()
    if not p:
        return {"error": "no path"}
    try:
        real = Path(os.path.realpath(p))
    except Exception:
        return {"error": "bad path"}
    if not real.is_dir():
        return {"error": "not a folder"}
    if is_blocked_path(str(real)):
        return {"error": "cannot open a protected system folder"}
    try:
        if os.name == "nt":
            os.startfile(str(real))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(real)], env=_child_env(keep=_ENV_TOOLS))
        else:
            subprocess.Popen(["xdg-open", str(real)], env=_child_env(keep=_ENV_TOOLS))
    except Exception as e:
        return {"error": f"could not open: {e}"}
    return {"ok": True, "path": str(real)}
