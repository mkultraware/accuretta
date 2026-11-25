# SPDX-License-Identifier: AGPL-3.0-only
"""The workbench strip: GPU, tokens today, running MCP clients and the workspace's git
state, cached briefly.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import time
from accuretta.agent.skills import _skills_catalog
from accuretta.core.chats import get_chats
from accuretta.core.mcp_paths import _MCP_CONFIG_FILE
from accuretta.core.safe_fs import safe_is_dir
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.runtime.gpu_detect import detect_vram_gb
from accuretta.runtime.llama_http import llama_ready
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from pathlib import Path


_WORKBENCH_CACHE: dict = {}


def _workbench_cached(key: str, ttl: float, fn):
    hit = _WORKBENCH_CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        value = fn()
    except Exception:
        value = hit[1] if hit else None
    _WORKBENCH_CACHE[key] = (now, value)
    return value


def _tokens_today() -> int:
    start = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    total = 0
    for chat in (get_chats().get("chats") or {}).values():
        if not isinstance(chat, dict) or float(chat.get("updated") or 0) < start:
            continue
        for m in chat.get("messages") or []:
            if m.get("role") == "assistant" and float(m.get("t") or 0) >= start:
                total += int(m.get("tokens") or 0)
    return total


def _workbench_status() -> dict:
    """Small, cached numbers for the status bar, sidebar meters and the
    capability chips. Every field degrades to 0 / False instead of erroring."""
    def mcp():
        configured = 0
        try:
            cfg = json.loads(_MCP_CONFIG_FILE.read_text(encoding="utf-8"))
            configured = sum(1 for v in (cfg.get("mcpServers") or {}).values()
                             if isinstance(v, dict) and not v.get("disabled"))
        except Exception:
            pass
        return {"running": len(_ACTIVE_MCP_CLIENTS), "configured": configured}

    def vram():
        v = detect_vram_gb()
        total = float(v.get("total_gb") or 0)
        free = float(v.get("gb") or 0)
        out = {"total_gb": total, "used_gb": round(max(0.0, total - free), 1) if total else 0,
               "name": v.get("name") or ""}
        if total and isinstance(v.get("temp_c"), int):
            out["temp_c"] = v["temp_c"]
        return out

    skills = _workbench_cached("skills", 30, lambda: len(_skills_catalog())) or 0
    return {
        "mcp": _workbench_cached("mcp", 15, mcp) or {"running": 0, "configured": 0},
        "skills": {"count": skills},
        "vram": _workbench_cached("vram", 60, vram) or {"total_gb": 0, "used_gb": 0, "name": ""},
        "tokens_today": _workbench_cached("tokens_today", 20, _tokens_today) or 0,
        "llama_up": bool(_workbench_cached("llama_up", 5, lambda: llama_ready(timeout=0.6))),
    }


def _workspace_git_status(raw: str) -> dict:
    """Read-only git summary for a configured workspace folder: branch,
    per-file porcelain codes (absolute, case-folded paths) and +/- totals."""
    empty = {"repo": False, "branch": "", "files": {}, "added": 0, "deleted": 0}
    if not raw:
        return empty
    try:
        target = Path(normalize_path(raw)).resolve()
    except Exception:
        return empty
    inside = False
    for folder in get_workspace().get("folders", []):
        try:
            root = Path(folder).resolve()
        except Exception:
            continue
        if str(target).lower() == str(root).lower() or str(target).lower().startswith(str(root).lower() + os.sep):
            inside = True
            break
    if not inside or not safe_is_dir(target) or not shutil.which("git"):
        return empty

    def git(*args):
        r = subprocess.run(["git", "-C", str(target), *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=4,
                           env=_child_env(keep=_ENV_TOOLS),
                           creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        return r.stdout if r.returncode == 0 else None

    def compute():
        top = git("rev-parse", "--show-toplevel")
        if not top:
            return empty
        top = top.strip()
        branch = (git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        files = {}
        for entry in (git("status", "--porcelain=v1", "-z", "--untracked-files=normal") or "").split("\0"):
            if len(entry) < 4:
                continue
            code, rel = entry[:2], entry[3:]
            files[os.path.normcase(os.path.normpath(os.path.join(top, rel)))] = code.strip() or "M"
        added = deleted = 0
        for line in (git("diff", "--numstat", "HEAD") or "").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                added += int(parts[0])
                deleted += int(parts[1])
        return {"repo": True, "branch": "" if branch == "HEAD" else branch, "files": files,
                "added": added, "deleted": deleted}

    return _workbench_cached("git:" + str(target).lower(), 15, compute) or empty


_ACTIVE_MCP_CLIENTS = []
