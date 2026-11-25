# SPDX-License-Identifier: AGPL-3.0-only
"""The scan of this PC behind ACCURETTA.md: folders, drives, program roots and model
folders, cached for a while.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


# Cache system context scans for 5 minutes so we don't re-scan dirs every turn.
_SYSTEM_CONTEXT_CACHE: tuple[float, dict] | None = None


_SYSTEM_CONTEXT_TTL = 300


def _scan_system_context() -> dict:
    """Return a dict of facts about the machine, cheap to compute."""
    global _SYSTEM_CONTEXT_CACHE
    if _SYSTEM_CONTEXT_CACHE is not None:
        ts, cached = _SYSTEM_CONTEXT_CACHE
        if time.time() - ts < _SYSTEM_CONTEXT_TTL:
            return cached
    facts: dict = {}
    # OS / platform
    try:
        import platform as _plat
        facts["os"] = f"{_plat.system()} {_plat.release()} (build {_plat.version()})"
        facts["machine"] = _plat.machine()
        facts["hostname"] = _plat.node()
    except Exception:
        pass
    # User
    try:
        facts["user"] = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        facts["userprofile"] = os.environ.get("USERPROFILE") or str(Path.home())
    except Exception:
        pass

    home = Path(facts.get("userprofile") or str(Path.home()))

    # Known folders — only include if they exist
    candidates = [
        ("Desktop",     home / "Desktop"),
        ("Documents",   home / "Documents"),
        ("Downloads",   home / "Downloads"),
        ("Pictures",    home / "Pictures"),
        ("Screenshots", home / "Pictures" / "Screenshots"),
        ("Videos",      home / "Videos"),
        ("Music",       home / "Music"),
        ("OneDrive",    home / "OneDrive"),
        ("OneDrive Desktop",   home / "OneDrive" / "Desktop"),
        ("OneDrive Documents", home / "OneDrive" / "Documents"),
        ("OneDrive Pictures",  home / "OneDrive" / "Pictures"),
        ("AppData Local",   home / "AppData" / "Local"),
        ("AppData Roaming", home / "AppData" / "Roaming"),
    ]
    known: list[dict] = []
    for label, p in candidates:
        try:
            if p.exists() and p.is_dir():
                # top-level child count, bounded
                try:
                    n = 0
                    for _ in p.iterdir():
                        n += 1
                        if n >= 5000: break
                except Exception:
                    n = None
                known.append({"label": label, "path": str(p), "items": n})
        except Exception:
            pass
    facts["folders"] = known

    # System drives
    drives: list[str] = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if Path(f"{letter}:\\").exists():
                drives.append(f"{letter}:\\")
    facts["drives"] = drives

    # Program roots (read-only existence check)
    program_roots = []
    for p in [r"C:\Program Files", r"C:\Program Files (x86)", str(home / "AppData" / "Local" / "Programs")]:
        if Path(p).exists():
            program_roots.append(p)
    facts["program_roots"] = program_roots

    # GGUF model directories — check common llama.cpp / unsloth / lm-studio spots
    gguf_dirs = []
    candidates = [
        os.environ.get("LLAMA_MODELS"),
        str(home / "models"),
        str(home / ".cache" / "llama.cpp"),
        str(home / ".cache" / "unsloth"),
        str(home / ".cache" / "huggingface" / "hub"),
        str(home / ".lmstudio" / "models"),
        r"C:\llama.cpp\models",
    ]
    for c in candidates:
        if c and Path(c).exists():
            gguf_dirs.append(c)
    if gguf_dirs:
        facts["gguf_model_dirs"] = gguf_dirs

    facts["scanned_at"] = int(time.time())
    _SYSTEM_CONTEXT_CACHE = (time.time(), facts)
    return facts
