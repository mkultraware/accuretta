# SPDX-License-Identifier: AGPL-3.0-only
"""The .accurettaignore rules: which workspace files the agent leaves alone, and
resolving a path to its workspace folder.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import fnmatch
import os
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from pathlib import Path


IGNORE_FILE_NAME = ".accurettaignore"


_IGNORE_CACHE: dict[str, tuple[float, list[tuple[bool, str]]]] = {}


def _read_ignore_rules(ws_root: str) -> list[tuple[bool, str]]:
    ip = Path(ws_root) / IGNORE_FILE_NAME
    try:
        mtime = ip.stat().st_mtime if ip.exists() else 0.0
    except Exception:
        mtime = 0.0
    cached = _IGNORE_CACHE.get(ws_root)
    if cached and cached[0] == mtime:
        return cached[1]
    rules: list[tuple[bool, str]] = []
    if ip.exists():
        try:
            for raw in ip.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                if neg:
                    line = line[1:].strip()
                if not line:
                    continue
                # strip trailing slashes; a trailing `/` in gitignore-speak means
                # "directory only" but we match by glob regardless
                line = line.rstrip("/")
                rules.append((neg, line))
        except Exception:
            rules = []
    _IGNORE_CACHE[ws_root] = (mtime, rules)
    return rules


def _workspace_root_for(path: str) -> str | None:
    n = normalize_path(path).lower()
    for folder in get_workspace().get("folders", []):
        f = normalize_path(folder)
        fl = f.lower()
        if n == fl or n.startswith(fl + os.sep):
            return f
    return None


def resolve_workspace_file(root_raw: str, rel_raw: str) -> tuple["Path | None", "dict | None"]:
    """Resolve `<root>/<rel>` and verify the result is strictly inside the
    workspace root. Returns (resolved_path, None) on success, (None, error_dict)
    on rejection. Hardening:
      - root must exactly match a configured workspace folder (no parent dirs)
      - rel must be relative (no absolute paths, no drive letters)
      - .. is allowed in the input string but the resolved path MUST stay
        inside the resolved root (defends against `a/../../../etc`)
      - symlinks that escape the root are rejected (resolve() follows them,
        so the boundary check catches them)
      - .accurettaignore rules apply (consistency with the read tools)
      - file must exist and be a regular file (no dirs, no devices)
    """
    if not root_raw or not rel_raw:
        return None, {"error": "root and path required"}
    # 1. root must be one of the configured workspace folders, exactly
    configured = [normalize_path(f) for f in get_workspace().get("folders", [])]
    root_norm = normalize_path(root_raw)
    if root_norm not in configured:
        return None, {"error": "root not in workspace"}
    # 2. reject obviously-absolute or drive-rooted relatives
    rel = rel_raw.replace("\\", "/").lstrip("/")
    if not rel:
        return None, {"error": "empty path"}
    if os.path.isabs(rel) or (len(rel) >= 2 and rel[1] == ":"):
        return None, {"error": "absolute path not allowed"}
    # 3. resolve and re-check containment
    try:
        root_resolved = Path(root_norm).resolve(strict=True)
    except Exception:
        return None, {"error": "workspace root unreadable"}
    try:
        target_resolved = (root_resolved / rel).resolve(strict=True)
    except FileNotFoundError:
        return None, {"error": "file not found"}
    except Exception as e:
        return None, {"error": f"resolve failed: {e}"}
    try:
        # commonpath() raises ValueError on different drives; that's a reject too
        common = Path(os.path.commonpath([str(root_resolved), str(target_resolved)]))
    except ValueError:
        return None, {"error": "path escapes workspace"}
    if common != root_resolved:
        return None, {"error": "path escapes workspace"}
    # 4. must be a real file
    if not target_resolved.is_file():
        return None, {"error": "not a file"}
    # 5. honour .accurettaignore (same rules the read tools use)
    if is_ignored(str(target_resolved)):
        return None, {"error": "file is ignored by .accurettaignore"}
    return target_resolved, None


def is_ignored(path: str) -> bool:
    """True if `path` matches a rule in the enclosing workspace's .accurettaignore."""
    root = _workspace_root_for(path)
    if not root:
        return False
    rules = _read_ignore_rules(root)
    if not rules:
        return False
    rel = os.path.relpath(normalize_path(path), root).replace("\\", "/")
    if rel == "." or rel.startswith(".."):
        return False
    base = os.path.basename(rel)
    ignored = False
    for neg, pat in rules:
        # match against basename for patterns without a slash; match full rel path
        # for patterns that contain a slash (so `build/*.map` works).
        hit = False
        if "/" in pat:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*") or any(
                fnmatch.fnmatch(rel, pat + "/" + "*") for _ in [0]
            ):
                hit = True
        else:
            # bare pattern: match any basename along the path
            parts = rel.split("/")
            if any(fnmatch.fnmatch(p, pat) for p in parts) or fnmatch.fnmatch(base, pat):
                hit = True
        if hit:
            ignored = not neg
    return ignored
