# SPDX-License-Identifier: AGPL-3.0-only
"""Decompiling an Android APK with jadx.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from accuretta.core.settings import get_settings
from accuretta.security.approvals import request_approval
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _fw_check_path
from pathlib import Path


def _resolve_jadx_bin() -> str | None:
    """Find a jadx executable. Settings override > PATH > common install dirs."""
    s = get_settings()
    custom = (s.get("jadx_path") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    # PATH lookup
    for cand in ("jadx", "jadx.bat", "jadx.cmd"):
        p = shutil.which(cand)
        if p:
            return p
    # Common Windows install locations
    for guess in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "jadx" / "bin" / "jadx.bat",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "jadx" / "bin" / "jadx.bat",
        Path.home() / "jadx" / "bin" / "jadx.bat",
    ):
        try:
            if guess.is_file():
                return str(guess)
        except Exception:
            continue
    return None


def tool_decompile_apk(args: dict) -> dict:
    """Phase-2 APK decompile. Shells out to JADX and writes Java sources +
    resources into a sandbox subdirectory next to the APK. The model can
    then read/grep the output dir using existing tools. Destructive (writes
    files) — gated by approval. Java 11+ and JADX must be installed; set
    settings.jadx_path if jadx is not on PATH."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    if src.suffix.lower() not in {".apk", ".xapk", ".aab"}:
        return {"error": f"not an APK/AAB: {src.name}"}

    dest_name = (args.get("dest_name") or f"{src.stem}_jadx").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    if is_data_path(str(dest), write=True):
        return {"error": DATA_PATH_ERROR}
    overwrite = bool(args.get("overwrite"))
    if dest.exists() and not overwrite:
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    jadx = _resolve_jadx_bin()
    if not jadx:
        return {
            "error": (
                "jadx not found. Install from https://github.com/skylot/jadx/releases "
                "(needs Java 11+), then either add it to PATH or set settings.jadx_path "
                "to the full path of jadx.bat."
            )
        }

    timeout = max(30, min(int(args.get("timeout") or 600), 3600))
    deobf = bool(args.get("deobf", True))
    show_bad_code = bool(args.get("show_bad_code", True))
    no_res = bool(args.get("no_res"))
    no_src = bool(args.get("no_src"))
    classes = (args.get("classes") or "").strip()  # passed to --classes-only-glob if present

    cmd: list[str] = [jadx, "-d", str(dest)]
    if deobf:
        cmd.append("--deobf")
    if show_bad_code:
        cmd.append("--show-bad-code")
    if no_res:
        cmd.append("--no-res")
    if no_src:
        cmd.append("--no-src")
    if classes:
        # JADX 1.5+ supports --class-list to scope decompilation. Older versions
        # ignore unknown flags; the call still works, just decompiles everything.
        cmd += ["--include-classes", classes]
    cmd.append(str(src))

    approval = request_approval(
        title="Decompile APK with JADX",
        command=f'jadx -d "{dest.name}" "{src.name}"' + (f' (filter: {classes})' if classes else ""),
        details={
            "kind": "decompile_apk",
            "source": str(src),
            "dest": str(dest),
            "jadx": jadx,
            "timeout_s": timeout,
            "command": cmd,
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied decompile ({approval.get('status')})"}

    if dest.exists() and overwrite:
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            env=_child_env(keep=_ENV_TOOLS),
        )
    except subprocess.TimeoutExpired:
        return {"error": f"jadx timed out after {timeout}s. Try --no-res, narrower --classes filter, or a longer timeout."}
    except FileNotFoundError:
        return {"error": f"jadx binary not executable: {jadx}"}
    except Exception as e:
        return {"error": f"jadx invocation failed: {e}"}

    elapsed = round(time.time() - t0, 2)
    # Walk the output and produce a compact summary the model can navigate.
    java_files: list[str] = []
    res_files: list[str] = []
    other: list[str] = []
    total_bytes = 0
    for root, _dirs, files in os.walk(dest):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                total_bytes += os.path.getsize(p)
            except OSError:
                pass
            rel = os.path.relpath(p, dest).replace("\\", "/")
            ext = fn.rsplit(".", 1)[-1].lower()
            if ext == "java":
                java_files.append(rel)
            elif ext in {"xml", "png", "jpg", "jpeg", "webp", "json", "ttf", "otf", "html"}:
                res_files.append(rel)
            else:
                other.append(rel)

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "elapsed_s": elapsed,
        "jadx": jadx,
        "command": " ".join(f'"{c}"' if " " in c else c for c in cmd),
        "source": str(src),
        "dest": str(dest),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
        "output_summary": {
            "total_bytes": total_bytes,
            "java_count": len(java_files),
            "resource_count": len(res_files),
            "other_count": len(other),
            # cap the file lists so a 30k-class APK doesn't nuke the model context
            "java_sample": java_files[:200],
            "resources_sample": res_files[:100],
        },
        "next_steps": [
            f"grep_files path:'{dest}' pattern:'<your regex>' to hunt across decompiled sources",
            f"read_file path:'{dest}/<file>.java' for individual class inspection",
        ],
    }
