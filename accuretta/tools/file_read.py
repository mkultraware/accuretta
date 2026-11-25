# SPDX-License-Identifier: AGPL-3.0-only
"""Reading files: read_file with paging and the record of what each chat read,
read_skeleton, and read_bytes.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ast
import difflib
import os
import re
import threading
from accuretta.tools.saved_output import _is_saved_tool_output
from accuretta.core.chats import _get_current_chat
from accuretta.core.ignore_rules import is_ignored
from accuretta.core.workspace import normalize_path
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import _fw_check_path
from accuretta.security.path_guards import is_blocked_path
from accuretta.security.path_guards import is_in_workspace
from accuretta.tools.pdf_text import _extract_pdf_text
from pathlib import Path


# ---- file read-state tracking (anti-stale / anti-clobber) -----------------
# Record each file's on-disk hash when the model reads it, per chat, and refuse
# a full overwrite of a file it never read (or that changed since): the main
# cause of "it undid its own fix" on long tasks.
_chat_file_reads: dict[str, dict[str, str]] = {}


_chat_file_reads_lock = threading.Lock()


def _file_md5(path: str) -> str:
    import hashlib
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except Exception:
        return ""


def _record_file_read(path: str) -> None:
    cid = _get_current_chat()
    if not cid:
        return
    h = _file_md5(path)
    if h:
        with _chat_file_reads_lock:
            _chat_file_reads.setdefault(cid, {})[normalize_path(path)] = h


def _missing_file_error(path: str) -> dict:
    """"not a file", plus what is there instead, so a misremembered name costs
    one call instead of a directory hunt. Names are only listed inside the
    workspace."""
    out: dict = {"error": f"not a file: {path}"}
    if not path:
        return out
    try:
        if os.path.isdir(path):
            out["error"] = f"not a file: {path} is a folder. Use list_directory to see what is in it."
            return out
        parent = os.path.dirname(path)
        while parent and not os.path.isdir(parent):
            up = os.path.dirname(parent)
            if up == parent:
                return out
            parent = up
        if (not parent or not is_in_workspace(parent) or is_data_path(parent)
                or is_blocked_path(parent)):
            return out
        names = sorted(os.listdir(parent))[:5000]
        want = os.path.relpath(path, parent).split(os.sep)[0]
        stem = os.path.splitext(want)[0].lower()
        close = difflib.get_close_matches(want, names, n=5, cutoff=0.6)
        close += [n for n in names
                  if os.path.splitext(n)[0].lower() == stem and n not in close]
        if close:
            out["did_you_mean"] = [os.path.join(parent, n) for n in close[:5]]
        if parent != os.path.dirname(path):
            out["error"] += (f" (the folder {os.path.dirname(path)} does not exist; "
                             f"the nearest one that does is {parent})")
    except (OSError, ValueError):
        pass
    return out


def tool_read_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.isfile(path):
        return _missing_file_error(path)
    if not _is_saved_tool_output(path):      # a cut tool result, saved by compress_tool_result
        if is_blocked_path(path):
            return {"error": "path blocked (Windows/System32)"}
        if is_data_path(path):
            return {"error": DATA_PATH_ERROR}
        if not is_in_workspace(path):
            return {"error": "path outside workspace. Add folder in Workspace panel."}
        if is_ignored(path):
            return {"error": f"path ignored by .accurettaignore: {path}"}
    # PDFs are binary containers; read as UTF-8 they are garbage. Route them
    # through the extractor, which returns page-numbered text or a clear note for
    # scanned PDFs.
    if path.lower().endswith(".pdf"):
        return _extract_pdf_text(path)
    # Line-based pagination via `offset` (0-indexed starting line). A truncated
    # response says explicitly that repeating the same call returns the same chunk,
    # because models otherwise loop on read_file hoping for fresh bytes.
    offset = max(0, int(args.get("offset") or 0))
    limit = max(0, int(args.get("limit") or 0))
    # 20KB chunk: enough for a function or a chunk of HTML, yet small enough that
    # the serialized result stays under the read_file result cap (32000) so
    # compress_tool_result never truncates it a second time. Keep the two in sync.
    BYTE_CAP = 20 * 1024
    try:
        _raw_head = b""
        with open(path, "rb") as _fh:
            _raw_head = _fh.read(2)
        try:
            # UTF-16 (Windows PowerShell output, .reg exports, many Windows logs)
            # used to come back as latin-1 with a NUL between every letter.
            full_text = (Path(path).read_bytes().decode("utf-16")
                         if _raw_head in (b"\xff\xfe", b"\xfe\xff")
                         else Path(path).read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError:
            full_text = Path(path).read_bytes().decode("latin-1", errors="replace")
        total_size = len(full_text.encode("utf-8", errors="replace"))
        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)
        _record_file_read(path)  # the model has now seen this file's current state
        if offset >= total_lines and total_lines > 0:
            return {
                "path": path,
                "content": "",
                "offset": offset,
                "total_lines": total_lines,
                "size": total_size,
                "note": (
                    f"offset {offset} is past EOF — file has {total_lines} lines "
                    f"(0-indexed: 0..{total_lines - 1}). Nothing to read here."
                ),
            }
        # Walk lines from offset until BYTE_CAP, always including at least one line
        # so a giant single-line minified file still returns something.
        chunk_parts: list[str] = []
        byte_count = 0
        end = offset
        for i in range(offset, total_lines):
            line_bytes = len(lines[i].encode("utf-8", errors="replace"))
            if (byte_count + line_bytes > BYTE_CAP or (limit and i - offset >= limit)) and chunk_parts:
                break
            chunk_parts.append(lines[i])
            byte_count += line_bytes
            end = i + 1
        chunk = "".join(chunk_parts)
        truncated = end < total_lines
        out: dict = {
            "path": path,
            "content": chunk,
            "offset": offset,
            "lines_returned": end - offset,
            "total_lines": total_lines,
            "size": total_size,
            "truncated": truncated,
        }
        if truncated:
            out["next_offset"] = end
            out["hint"] = (
                f"This is the COMPLETE, untruncated chunk for lines {offset}-{end - 1} "
                f"of {total_lines}. To read the next chunk, call read_file with "
                f"offset={end}. To jump straight to the part you need instead of "
                f"paging, call grep_files (search for a string) or read_skeleton "
                f"(code structure/signatures). Retrying with the same args returns "
                f"the same bytes — the file is fixed on disk."
            )
        return out
    except Exception as e:
        return {"error": str(e)}


def tool_read_bytes(args: dict) -> dict:
    """Read raw bytes at an offset. Returns hex + printable ASCII view.
    Use to inspect a header or an offset surfaced by binwalk_scan."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    offset = max(0, int(args.get("offset") or 0))
    length = max(1, min(int(args.get("length") or 256), 4096))
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        ascii_view = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        return {
            "path": path,
            "offset": offset,
            "length": len(data),
            "hex": data.hex(),
            "ascii": ascii_view,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_read_skeleton(args: dict) -> dict:
    file_path = normalize_path(args.get("path", ""))
    if is_data_path(file_path):
        return {"error": DATA_PATH_ERROR}
    if not is_in_workspace(file_path):
        return {"error": "Path is outside workspace bounds."}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    
    ext = os.path.splitext(file_path)[1]
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Read error: {e}"}

    if ext == '.py':
        try:
            tree = ast.parse(content)
            skeleton = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node)
                    doc_preview = f" # {doc.splitlines()[0]}" if doc else ""
                    skeleton.append(f"def {node.name}(...):{doc_preview}")
                elif isinstance(node, ast.ClassDef):
                    doc = ast.get_docstring(node)
                    doc_preview = f" # {doc.splitlines()[0]}" if doc else ""
                    skeleton.append(f"class {node.name}:{doc_preview}")
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            cdoc = ast.get_docstring(child)
                            cdoc_prev = f" # {cdoc.splitlines()[0]}" if cdoc else ""
                            skeleton.append(f"    def {child.name}(...):{cdoc_prev}")
            return {"result": "\n".join(skeleton)}
        except SyntaxError as e:
            return {"error": f"AST parse failed due to SyntaxError: {e}"}
            
    elif ext in ('.js', '.ts', '.jsx', '.tsx'):
        skeleton = []
        class_re = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)')
        func_re = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)')
        arrow_re = re.compile(r'^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=]+)\s*=>')
        
        for line in content.splitlines():
            if m := class_re.search(line):
                skeleton.append(f"class {m.group(1)}")
            elif m := func_re.search(line):
                skeleton.append(f"function {m.group(1)}()")
            elif m := arrow_re.search(line):
                skeleton.append(f"const {m.group(1)} = () => ...")
                
        return {"result": "\n".join(skeleton)}
    else:
        return {"error": f"Unsupported file extension: {ext}"}
