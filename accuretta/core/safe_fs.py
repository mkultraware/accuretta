# SPDX-License-Identifier: AGPL-3.0-only
"""File checks that never raise: exists, is-a-folder, and a depth-limited search.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path


def safe_exists(path: str | Path | None) -> bool:
    """Check if a path exists without raising OSError on malformed Windows inputs."""
    if not path:
        return False
    try:
        return Path(path).exists()
    except Exception:
        return False


def safe_is_dir(path: str | Path | None) -> bool:
    """Check if a path is a directory without raising OSError on malformed Windows inputs."""
    if not path:
        return False
    try:
        return Path(path).is_dir()
    except Exception:
        return False


def safe_rglob_files(root_dir: str | Path, pattern: str, max_depth: int = 3) -> list[Path]:
    """Safely recursively list files matching pattern under root_dir with strict depth limits and ignore-lists."""
    if not root_dir:
        return []
    try:
        root_path = Path(root_dir)
        if not safe_exists(root_path) or not safe_is_dir(root_path):
            return []
    except Exception:
        return []

    ignored_dirs = {
        ".git", "node_modules", "venv", ".venv", "__pycache__", 
        "appdata/local/temp", "temp", "tmp", "$recycle.bin", 
        "system volume information", "build", "dist"
    }
    
    results: list[Path] = []
    
    def _scan(dir_path: Path, current_depth: int):
        if current_depth > max_depth:
            return
        try:
            for child in dir_path.iterdir():
                try:
                    if child.is_dir():
                        if child.name.lower() not in ignored_dirs:
                            _scan(child, current_depth + 1)
                    elif child.is_file():
                        if fnmatch.fnmatch(child.name.lower(), pattern.lower()):
                            results.append(child)
                except Exception:
                    continue
        except Exception:
            pass
            
    _scan(root_path, 1)
    return results
