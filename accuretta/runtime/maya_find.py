# SPDX-License-Identifier: AGPL-3.0-only
"""Finding a built maya-ik2 engine, and whether a model is one only Maya runs right.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import sys
from accuretta.core.paths import DATA
from accuretta.core.paths import USER_DIR
from accuretta.runtime.gguf import read_gguf_metadata
from pathlib import Path


# ---- maya engine -------------------------------------------------------------
# Project Maya's maya-ik2 (github.com/alesha-pro/maya-inkling-mini) is an engine
# written for Inkling 2: every expert resident in VRAM, a token as one CUDA graph,
# and Thinking Machines' DSpark drafter. With the same model on an RTX 4090 it
# wrote 150 tok/s (290 editing, with the drafter) and read 4,000 tok/s, where
# llama.cpp wrote 56 and read 400 (2026-11-21). Its server speaks llama-server's
# HTTP API (/health, /props, /slots, /tokenize, /v1/chat/completions with
# `timings`), so only the launch differs. Nothing to configure: a built checkout
# is found in the home folder or under data/engines (ACCURETTA_MAYA_DIR names
# another place, as ACCURETTA_LLAMA_BIN does for llama.cpp).
_MAYA_DIR_NAMES = ("maya-inkling-mini", "project-maya", "maya")


_MAYA_ARCHES = ("inkling2",)


_MAYA_META_CACHE: dict = {}


# The ready-made engine "Install Maya" sets up (tools/maya-package/build.ps1 makes
# it): bin/maya/<version>/, the version in use named by bin/maya/active.json. It
# runs on Accuretta's own Python, with the few libraries Maya needs beyond it
# shipped inside (pylib/).
MAYA_PACKAGE_DIR = USER_DIR / "bin" / "maya"


MAYA_MANIFEST = "accuretta-maya.json"


_MAYA_MANIFEST_CACHE: dict = {}


def maya_package_manifest(root: Path) -> dict:
    """accuretta-maya.json of one installed package, {} when it has none."""
    path = Path(root) / MAYA_MANIFEST
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime, st.st_size)
    hit = _MAYA_MANIFEST_CACHE.get(key)
    if hit is None:
        try:
            hit = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            hit = {}
        hit = hit if isinstance(hit, dict) else {}
        if len(_MAYA_MANIFEST_CACHE) > 16:
            _MAYA_MANIFEST_CACHE.clear()
        _MAYA_MANIFEST_CACHE[key] = hit
    return hit


def active_maya_package() -> Path | None:
    """The installed package in use, or None."""
    try:
        version = str(json.loads((MAYA_PACKAGE_DIR / "active.json").read_text(encoding="utf-8")).get("version") or "")
    except (OSError, ValueError, AttributeError):
        return None
    if not version or "/" in version or "\\" in version or version.startswith("."):
        return None
    root = MAYA_PACKAGE_DIR / version
    return root if (root / MAYA_MANIFEST).is_file() else None


def _own_python() -> str:
    """Accuretta's own Python, as a console program (pythonw has no output to log)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").is_file():
        return str(exe.with_name("python.exe"))
    return str(exe)


def _package_python_path(root: Path, manifest: dict) -> list[str]:
    """The package's Python library folders for the Python Accuretta runs on."""
    v = sys.version_info
    out = []
    for rel in (manifest.get("python_libs") or "", str(manifest.get("python_libs_per_version") or "").format(v[0], v[1])):
        if rel and (root / rel).is_dir():
            out.append(str(root / rel))
    return out


# Windows embeddable Python (shipped by the Accuretta installer) has a
# pythonXY._pth file, which causes Python to ignore PYTHONPATH and omit the
# script's folder from sys.path. Packaged Maya scripts launch through this
# bootstrap so the package's pylib, script, and root folders land at the front
# of sys.path on both standard and embeddable Python installations.
_MAYA_PY_RUNNER = (
    "import os, pathlib, runpy, sys; "
    "extra = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]; "
    "script = pathlib.Path(sys.argv[1]).resolve(); "
    "roots = [str(script.parent)] + ([str(script.parents[1])] if len(script.parents) > 1 else []); "
    "sys.path[:0] = roots + extra; "
    "sys.argv = sys.argv[1:]; "
    "runpy.run_path(str(script), run_name='__main__')"
)


def _maya_python_cmd(engine: dict, script: str, *args: str) -> list[str]:
    """Build the command to execute a Maya Python script.

    A developer checkout uses its own .venv directly; a ready-made package runs
    through _MAYA_PY_RUNNER so its bundled pylib is importable even when
    Accuretta runs on Windows embeddable Python (whose ._pth ignores PYTHONPATH).
    """
    if engine.get("pythonpath"):
        return [engine["python"], "-c", _MAYA_PY_RUNNER, script, *args]
    return [engine["python"], script, *args]


def find_maya_engine() -> dict:
    """A built maya-ik2: {root, server, engine, python, vision, tokenizer_tool,
    chat_template, kind, ...}, or {} when there is none. A checkout someone built
    (with its own .venv) comes first; then the package "Install Maya" set up."""
    roots: list[Path] = []
    env_dir = (os.environ.get("ACCURETTA_MAYA_DIR") or "").strip()
    if env_dir:
        roots.append(Path(env_dir))
    for base in (Path.home(), DATA / "engines"):
        roots += [base / name for name in _MAYA_DIR_NAMES]
    exe = "maya-ik2.exe" if os.name == "nt" else "maya-ik2"
    py = (".venv", "Scripts", "python.exe") if os.name == "nt" else (".venv", "bin", "python")
    vision_exe = "maya-vision.exe" if os.name == "nt" else "maya-vision"
    for root in roots:
        try:
            server, engine, python = root / "serve" / "server.py", root / "build" / exe, root.joinpath(*py)
            if not (server.is_file() and engine.is_file() and python.is_file()):
                continue
            vision = root / "build-vision" / "bin" / vision_exe
            return {"root": str(root), "server": str(server), "engine": str(engine), "python": str(python),
                    "vision": str(vision) if vision.is_file() else "",
                    "tokenizer_tool": str(root / "tools" / "strata_tokenizer.py"),
                    "chat_template": str(root / "tools" / "ik2" / "chat_template.jinja"),
                    "kind": "checkout"}
        except OSError:
            continue
    root = active_maya_package()
    if root is not None:
        manifest = maya_package_manifest(root)
        try:
            server = root / "serve" / "server.py"
            engine = root / str(manifest.get("engine") or f"build/{exe}")
            vision = root / str(manifest.get("vision") or f"build-vision/bin/{vision_exe}")
            if server.is_file() and engine.is_file():
                return {"root": str(root), "server": str(server), "engine": str(engine), "python": _own_python(),
                        "vision": str(vision) if vision.is_file() else "",
                        "tokenizer_tool": str(root / "tools" / "strata_tokenizer.py"),
                        "chat_template": str(root / "tools" / "ik2" / "chat_template.jinja"),
                        "pythonpath": os.pathsep.join(_package_python_path(root, manifest)),
                        "kind": "package", "version": str(manifest.get("version") or "")}
        except OSError:
            pass
    return {}


def _maya_gguf_meta(model_path: str) -> dict:
    """The model's header metadata, cached per file. The first MB holds every
    architecture key (the tokenizer arrays come after), so nothing more is read."""
    try:
        st = os.stat(model_path)
    except OSError:
        return {}
    key = (os.path.normcase(os.path.abspath(model_path)), st.st_size, int(st.st_mtime))
    hit = _MAYA_META_CACHE.get(key)
    if hit is None:
        hit = read_gguf_metadata(model_path, max_bytes=1024 * 1024, retry_bytes=0)
        if len(_MAYA_META_CACHE) > 64:
            _MAYA_META_CACHE.clear()
        _MAYA_META_CACHE[key] = hit
    return hit


def _maya_only_model(model_path: str) -> bool:
    """A model only maya runs right: Unsloth's looped Inkling 2 layout
    (architecture inkling2 with num_loops > 1). Mainline llama.cpp loads it but
    computes it wrong (Unsloth's own warning: gibberish). llama.cpp's looped
    layout (inkling2_loop) and the unlooped Inkling 2 are llama.cpp's."""
    if not model_path:
        return False
    meta = _maya_gguf_meta(model_path)
    arch = str(meta.get("architecture") or "")
    if arch not in _MAYA_ARCHES:
        return False
    loops = (meta.get("keys") or {}).get(f"{arch}.num_loops")
    return not isinstance(loops, bool) and isinstance(loops, (int, float)) and loops > 1


def _maya_engine_for(model_path: str) -> dict:
    """The maya engine when it is installed and this model is one only it
    runs right (see _maya_only_model); {} otherwise."""
    if not model_path:
        return {}
    eng = find_maya_engine()
    if not eng or not _maya_only_model(model_path):
        return {}
    return eng
