# SPDX-License-Identifier: AGPL-3.0-only
"""Firmware and binary blobs: identifying embedded files by magic bytes, carving them
out, binwalk-style scans and string dumps.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.security.approvals import request_approval
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _fw_check_path
from pathlib import Path


# Magic-byte signature table for tool_file_inspect. Only formats we expect
# to see in consumer firmware. Order matters — longer signatures first.
_FW_MAGIC_TABLE = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip (empty)"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"hsqs", "squashfs (le)"),
    (b"sqsh", "squashfs (be)"),
    (b"\x85\x19\x03\x20", "jffs2 (le)"),
    (b"\x19\x85\x20\x03", "jffs2 (be)"),
    (b"\x45\x3d\xcd\x28", "cramfs (le)"),
    (b"\x28\xcd\x3d\x45", "cramfs (be)"),
    (b"HDR0", "trx (router header)"),
    (b"\x7fELF", "ELF"),
    (b"MZ", "PE/DOS executable"),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat"),
    (b"070701", "cpio (newc ascii)"),
    (b"070707", "cpio (binary)"),
]


def _fw_identify_magic(data: bytes) -> str | None:
    for sig, name in _FW_MAGIC_TABLE:
        if data.startswith(sig):
            return name
    # ustar lives at offset 257 in a tar header
    if len(data) >= 263 and data[257:262] == b"ustar":
        return "tar"
    return None


# Signatures scanned for at any offset in tool_binwalk_scan. Kept short and
# unambiguous to minimize false positives. This isn't binwalk's full database
# — just the formats that actually show up in consumer router firmware.
_FW_SCAN_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"BZh9", "bzip2 (level 9)"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"PK\x03\x04", "zip (local file)"),
    (b"PK\x05\x06", "zip (end of central dir)"),
    (b"hsqs", "squashfs (le)"),
    (b"sqsh", "squashfs (be)"),
    (b"\x85\x19\x03\x20", "jffs2 (le)"),
    (b"\x19\x85\x20\x03", "jffs2 (be)"),
    (b"\x45\x3d\xcd\x28", "cramfs (le)"),
    (b"\x28\xcd\x3d\x45", "cramfs (be)"),
    (b"\x7fELF", "ELF executable"),
    (b"HDR0", "trx router header"),
    (b"-rom1fs-", "romfs"),
    (b"070701", "cpio (newc ascii)"),
    (b"070707", "cpio (binary)"),
    (b"UBI#", "UBI image"),
    (b"!<arch>", "ar archive"),
]


def tool_binwalk_scan(args: dict) -> dict:
    """Scan a binary for known magic-byte signatures at any offset.
    Pure-Python — covers gzip, bzip2, xz, 7z, zip, squashfs, jffs2,
    cramfs, ELF, TRX, romfs, cpio, UBI, ar. Returns sorted offset table."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    max_results = max(1, min(int(args.get("max_results") or 200), 1000))
    max_bytes = max(1024, min(int(args.get("max_bytes") or 64 * 1024 * 1024),
                              256 * 1024 * 1024))
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        truncated = size > len(data)
        results: list[dict] = []
        for sig, name in _FW_SCAN_SIGNATURES:
            start = 0
            while True:
                idx = data.find(sig, start)
                if idx < 0:
                    break
                results.append({
                    "offset": idx,
                    "offset_hex": f"0x{idx:x}",
                    "description": name,
                })
                if len(results) >= max_results:
                    break
                start = idx + 1
            if len(results) >= max_results:
                break
        results.sort(key=lambda r: r["offset"])
        return {
            "path": path,
            "size": size,
            "scanned_bytes": len(data),
            "truncated": truncated,
            "count": len(results),
            "matches": results,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_strings_dump(args: dict) -> dict:
    """Extract printable ASCII string runs from a binary file. Optional
    regex filter. Caps reads at max_bytes so large firmwares stay sane."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    min_len = max(4, int(args.get("min_length") or 8))
    pattern = args.get("pattern")
    max_results = max(1, min(int(args.get("max_results") or 500), 5000))
    max_bytes = max(1024, min(int(args.get("max_bytes") or 16 * 1024 * 1024),
                              64 * 1024 * 1024))
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error as e:
        return {"error": f"bad pattern: {e}"}
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        run_re = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
        found: list[dict] = []
        for m in run_re.finditer(data):
            s = m.group(0).decode("ascii", errors="replace")
            if rx and not rx.search(s):
                continue
            found.append({"offset": m.start(), "string": s})
            if len(found) >= max_results:
                break
        return {
            "path": path,
            "scanned_bytes": len(data),
            "truncated": len(data) >= max_bytes,
            "match_count": len(found),
            "matches": found,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_carve_file(args: dict) -> dict:
    """Carve a byte range [offset, offset+length] out of a source file into
    a new file in the same directory. Use when binwalk_scan surfaces an
    embedded gzip/squashfs/etc inside a custom container (e.g. the 0xd00dfe
    TP-Link/ASUS .pkgtb wrapper) — carve the range, THEN run extract_archive
    or extract_squashfs on the carved file. length=0 carves to EOF."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    try:
        size = os.path.getsize(src)
    except OSError as e:
        return {"error": str(e)}
    offset = max(0, int(args.get("offset") or 0))
    if offset >= size:
        return {"error": f"offset 0x{offset:x} >= file size 0x{size:x}"}
    length_raw = int(args.get("length") or 0)
    if length_raw <= 0:
        length = size - offset
    else:
        length = min(length_raw, size - offset)
    # cap absurd carves so runaway tool calls can't fill the disk
    max_carve = max(1024, min(int(args.get("max_bytes") or 512 * 1024 * 1024),
                              2 * 1024 * 1024 * 1024))
    if length > max_carve:
        return {"error": f"carve length {length} exceeds max_bytes {max_carve}; pass max_bytes explicitly to override."}

    dest_name = (args.get("dest_name") or f"{src.stem}_at_0x{offset:x}.bin").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single filename (no slashes)"}
    dest = src.parent / dest_name
    if is_data_path(str(dest), write=True):
        return {"error": DATA_PATH_ERROR}
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    # carving creates a new file, so gate behind approval like other writes
    approval = request_approval(
        title="Carve file",
        command=f'carve: "{src.name}" [0x{offset:x}..+0x{length:x}] -> "{dest.name}"',
        details={"kind": "delete" if dest.exists() else "carve_file",
                 "source": str(src), "dest": str(dest),
                 "overwrite": bool(dest.exists()),
                 "offset": offset, "length": length},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied carve ({approval.get('status')})"}

    try:
        chunk = 1024 * 1024
        written = 0
        head_hex = ""
        with open(src, "rb") as f, open(dest, "wb") as o:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                if written == 0:
                    head_hex = buf[:16].hex()
                o.write(buf)
                written += len(buf)
                remaining -= len(buf)
        # identify carved magic so the model knows what tool to chain next
        try:
            with open(dest, "rb") as f:
                head = f.read(512)
            magic = _fw_identify_magic(head) or "unknown"
        except Exception:
            magic = "unknown"
        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "offset": offset,
            "offset_hex": f"0x{offset:x}",
            "length": written,
            "head_hex": head_hex,
            "magic": magic,
        }
    except Exception as e:
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return {"error": str(e)}
