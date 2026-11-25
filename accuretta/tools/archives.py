# SPDX-License-Identifier: AGPL-3.0-only
"""Looking inside files and archives: file_inspect, extracting zip/tar/7z safely, and
extracting squashfs images.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
from accuretta.core.optional_imports import ELFFile
from accuretta.core.optional_imports import SquashFsImage
from accuretta.core.optional_imports import _HAVE_ELFTOOLS
from accuretta.core.optional_imports import _HAVE_SQUASHFS
from accuretta.security.approvals import request_approval
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _fw_check_path
from accuretta.tools.firmware import _fw_identify_magic
from pathlib import Path


def tool_file_inspect(args: dict) -> dict:
    """Identify a file by magic bytes. For ELF binaries, also report
    architecture, type, entry point, interpreter, and strip status."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(512)
        magic = _fw_identify_magic(head)
        info: dict = {
            "path": path,
            "size": size,
            "magic": magic or "unknown",
            "header_hex": head[:32].hex(),
        }
        if magic == "ELF" and _HAVE_ELFTOOLS:
            try:
                with open(path, "rb") as f:
                    elf = ELFFile(f)
                    interp_section = elf.get_section_by_name(".interp")
                    info["elf"] = {
                        "class": elf.header["e_ident"]["EI_CLASS"],
                        "data": elf.header["e_ident"]["EI_DATA"],
                        "machine": elf.header["e_machine"],
                        "type": elf.header["e_type"],
                        "entry": hex(elf.header["e_entry"]),
                        "stripped": elf.get_section_by_name(".symtab") is None,
                        "interpreter": (
                            interp_section.data().decode(errors="replace").rstrip("\x00")
                            if interp_section else None
                        ),
                    }
            except Exception as e:
                info["elf_error"] = str(e)
        return info
    except Exception as e:
        return {"error": str(e)}


def _safe_member(name: str) -> str | None:
    """Archive member name as a relative POSIX path, or None if it escapes."""
    n = (name or "").replace("\\", "/").lstrip("/")
    if not n or ".." in n.split("/"):
        return None
    return n


def tool_extract_archive(args: dict) -> dict:
    """Auto-detect format and extract gzip/tar/zip/xz/bzip2/squashfs into
    a sandbox subdirectory next to the source. Destructive — gated by an
    approval card. Refuses path-traversal in archive members."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    dest_name = (args.get("dest_name") or f"{src.stem}_extracted").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    if is_data_path(str(dest), write=True):
        return {"error": DATA_PATH_ERROR}
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    approval = request_approval(
        title="Extract archive",
        command=f'extract: "{src.name}" -> "{dest.name}"',
        details={"kind": "delete" if dest.exists() else "extract_archive",
                 "source": str(src), "dest": str(dest),
                 "overwrite": bool(dest.exists())},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied extraction ({approval.get('status')})"}

    import gzip as _gz
    import tarfile as _tar
    import zipfile as _zip
    import lzma as _xz
    import bz2 as _bz

    try:
        with open(src, "rb") as f:
            head = f.read(512)
        magic = _fw_identify_magic(head) or ""

        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        files_written = 0

        if magic == "gzip":
            # gzip usually wraps a tar in firmware contexts
            with _gz.open(src, "rb") as gz:
                inner_head = gz.read(512)
            if len(inner_head) >= 263 and inner_head[257:262] == b"ustar":
                with _gz.open(src, "rb") as gz, _tar.open(fileobj=gz, mode="r:") as tar:
                    for m in tar.getmembers():
                        n = _safe_member(m.name)
                        if not n:
                            continue
                        m.name = n
                        tar.extract(m, dest)
                        files_written += 1
            else:
                out = dest / (src.stem + ".bin")
                with _gz.open(src, "rb") as gz, open(out, "wb") as o:
                    shutil.copyfileobj(gz, o)
                files_written = 1
        elif magic == "tar":
            with _tar.open(src, "r:") as tar:
                for m in tar.getmembers():
                    n = _safe_member(m.name)
                    if not n:
                        continue
                    m.name = n
                    tar.extract(m, dest)
                    files_written += 1
        elif magic.startswith("zip"):
            with _zip.ZipFile(src) as z:
                for info in z.infolist():
                    n = _safe_member(info.filename)
                    if not n:
                        continue
                    info.filename = n
                    z.extract(info, dest)
                    files_written += 1
        elif magic == "xz":
            out = dest / (src.stem + ".bin")
            with _xz.open(src, "rb") as xz, open(out, "wb") as o:
                shutil.copyfileobj(xz, o)
            files_written = 1
        elif magic == "bzip2":
            out = dest / (src.stem + ".bin")
            with _bz.open(src, "rb") as bz, open(out, "wb") as o:
                shutil.copyfileobj(bz, o)
            files_written = 1
        elif magic.startswith("squashfs"):
            if not _HAVE_SQUASHFS:
                return {"error": "squashfs detected but PySquashfsImage not installed."}
            with SquashFsImage.from_file(str(src)) as img:
                for entry in img:
                    if getattr(entry, "is_dir", False):
                        continue
                    if not getattr(entry, "is_file", True):
                        continue
                    rel = (getattr(entry, "path", "") or "").lstrip("/")
                    n = _safe_member(rel)
                    if not n:
                        continue
                    out = dest / n
                    out.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        data = entry.read_bytes()
                    except Exception:
                        # API drift fallback
                        with entry.open() as fp:  # type: ignore[attr-defined]
                            data = fp.read()
                    with open(out, "wb") as o:
                        o.write(data)
                    files_written += 1
        else:
            shutil.rmtree(dest, ignore_errors=True)
            return {"error": f"unsupported or unrecognized format (magic={magic or 'unknown'})"}

        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "files_written": files_written,
            "magic": magic,
        }
    except Exception as e:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        return {"error": str(e)}


def tool_extract_squashfs(args: dict) -> dict:
    """Extract a squashfs image into a sandbox subdirectory next to the source.
    Pure-Python via PySquashfsImage — no system squashfs-tools needed. Refuses
    path-traversal in archive members. Overwriting an existing destination
    keeps the deletion approval gate."""
    if not _HAVE_SQUASHFS:
        return {"error": "PySquashfsImage not installed. pip install PySquashfsImage"}
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    dest_name = (args.get("dest_name") or f"{src.stem}_rootfs").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    if is_data_path(str(dest), write=True):
        return {"error": DATA_PATH_ERROR}
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    # quick magic sanity check
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except Exception as e:
        return {"error": f"could not read source: {e}"}
    if not (head.startswith(b"hsqs") or head.startswith(b"sqsh")):
        return {"error": f"not a squashfs image (magic={head[:4].hex()}). Try binwalk_scan to find the offset and carve first."}

    approval = request_approval(
        title="Extract squashfs",
        command=f'extract squashfs: "{src.name}" -> "{dest.name}"',
        details={"kind": "delete" if dest.exists() else "extract_squashfs",
                 "source": str(src), "dest": str(dest),
                 "overwrite": bool(dest.exists())},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied extraction ({approval.get('status')})"}

    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        files_written = 0
        symlinks = 0
        skipped = 0
        total_bytes = 0

        with SquashFsImage.from_file(str(src)) as img:
            for entry in img:
                try:
                    rel = (getattr(entry, "path", "") or "").lstrip("/")
                    n = _safe_member(rel)
                    if not n:
                        skipped += 1
                        continue
                    out = dest / n
                    if getattr(entry, "is_dir", False):
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if getattr(entry, "is_symlink", False):
                        # write symlink target as a tiny text file - Windows
                        # can't always make real symlinks without admin
                        target = getattr(entry, "readlink", lambda: "")()
                        try:
                            with open(out, "w", encoding="utf-8") as o:
                                o.write(f"[symlink] -> {target}\n")
                            symlinks += 1
                        except Exception:
                            skipped += 1
                        continue
                    if not getattr(entry, "is_file", True):
                        skipped += 1
                        continue
                    try:
                        data = entry.read_bytes()
                    except Exception:
                        # API drift fallback
                        try:
                            with entry.open() as fp:  # type: ignore[attr-defined]
                                data = fp.read()
                        except Exception:
                            skipped += 1
                            continue
                    with open(out, "wb") as o:
                        o.write(data)
                    files_written += 1
                    total_bytes += len(data)
                except Exception:
                    skipped += 1
                    continue

        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "files_written": files_written,
            "symlinks": symlinks,
            "skipped": skipped,
            "total_bytes": total_bytes,
        }
    except Exception as e:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        return {"error": str(e)}
