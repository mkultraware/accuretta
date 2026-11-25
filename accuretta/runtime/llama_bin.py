# SPDX-License-Identifier: AGPL-3.0-only
"""Finding the llama-server binary, the llama-fit-params tool next to it, and whether
that build knows a model's architecture.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import shutil
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from accuretta.core.safe_fs import safe_exists
from accuretta.core.safe_fs import safe_is_dir
from accuretta.core.safe_fs import safe_rglob_files
from accuretta.core.settings import get_settings
from pathlib import Path


def _fit_params_bin() -> str:
    """llama-fit-params beside the configured llama-server, or ''."""
    server = find_llama_bin() or ""
    if not server:
        return ""
    p = Path(server)
    for name in ("llama-fit-params.exe", "llama-fit-params"):
        cand = p.with_name(name)
        if cand.is_file():
            return str(cand)
    return ""


_LLAMA_ARCH_TABLE: dict = {}       # {"key": (path, mtime, size), "data": bytes}


_ARCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")


def llama_knows_arch(arch: str, bin_path: str = "") -> bool | None:
    """Whether the configured llama.cpp build can load models of `arch`
    (the GGUF's general.architecture). llama.cpp keeps the name of every
    architecture it supports as a C string in its library (llama.dll,
    libllama.so / .dylib, or a statically linked llama-server), so a name
    missing there means "unknown model architecture" at load: the build is
    too old for this model. None when that can't be told (no build found,
    or no file carrying the name table). A build that knows the name can
    still have bugs with it; this only catches the missing-support case."""
    arch = str(arch or "").strip()
    if not _ARCH_NAME_RE.fullmatch(arch):
        return None
    server = bin_path or find_llama_bin() or ""
    if not server:
        return None
    folder = Path(server).parent
    candidates = [folder / "llama.dll", folder / "libllama.so", folder / "libllama.dylib"]
    try:
        candidates += sorted(folder.glob("libllama.so.*")) + sorted(folder.glob("libllama.*.dylib"))
    except Exception:
        pass
    candidates.append(Path(server))
    for lib in candidates:
        try:
            st = lib.stat()
        except Exception:
            continue
        key = (str(lib), st.st_mtime, st.st_size)
        if _LLAMA_ARCH_TABLE.get("key") != key:
            try:
                data = lib.read_bytes() if st.st_size <= 512 * 1024 * 1024 else b""
            except Exception:
                continue
            _LLAMA_ARCH_TABLE.update({"key": key, "data": data})
        data = _LLAMA_ARCH_TABLE.get("data") or b""
        # "llama" is in every build's table; without it this file isn't the one.
        if b"\x00llama\x00" not in data:
            continue
        return (b"\x00" + arch.encode("utf-8") + b"\x00") in data
    return None


def find_llama_bin() -> str:
    """Locate llama-server.exe — settings override > env > PATH > known dirs > recursive scan."""
    s = get_settings()
    explicit = (s.get("llama_bin") or "").strip()
    if explicit and safe_exists(explicit):
        return explicit
    env = (os.environ.get("ACCURETTA_LLAMA_BIN") or "").strip()
    if env and safe_exists(env):
        return env
    found = shutil.which("llama-server.exe") or shutil.which("llama-server")
    if found:
        return found
    
    home = Path.home()
    search_roots = [
        ROOT,
        USER_DIR,
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        Path("C:/llama.cpp"),
    ]
    
    # Scan other drive letters for common top-level llama folders
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if safe_exists(drive):
                for candidate_folder in ["llama.cpp", "llama", "bin"]:
                    cand = drive / candidate_folder
                    if safe_exists(cand) and safe_is_dir(cand):
                        search_roots.append(cand)
                        
    seen_roots = set()
    valid_roots = []
    for r in search_roots:
        try:
            r_abs = str(r.resolve())
            if r_abs not in seen_roots and safe_exists(r) and safe_is_dir(r):
                seen_roots.add(r_abs)
                valid_roots.append(r)
        except Exception:
            continue
            
    pattern = "llama-server.exe" if os.name == "nt" else "llama-server"
    for root in valid_roots:
        depth = 3
        try:
            matches = safe_rglob_files(root, pattern, max_depth=depth)
            if matches:
                # Prioritize Release builds
                for m in matches:
                    if "release" in str(m).lower():
                        return str(m.resolve())
                return str(matches[0].resolve())
        except Exception:
            continue
            
    candidates = [
        home / ".unsloth/llama.cpp/build/bin/Release/llama-server.exe",
        home / ".unsloth/llama.cpp/build/bin/llama-server.exe",
        home / ".docker/bin/inference/llama-server.exe",
        home / "llama.cpp/build/bin/Release/llama-server.exe",
        home / "llama.cpp/llama-server.exe",
        Path("C:/llama.cpp/build/bin/Release/llama-server.exe"),
        Path("C:/llama.cpp/llama-server.exe"),
    ]
    for c in candidates:
        if safe_exists(c):
            return str(c)
            
    return ""
