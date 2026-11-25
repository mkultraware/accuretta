# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's file tools: list a folder, write, edit and delete files, with diff
previews, staleness checks, text encodings and the rewrite-loop guard.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import difflib
import os
import shutil
import time
from accuretta.agent.edit_helpers import _closest_region
from accuretta.agent.edit_helpers import _new_file_eol
from accuretta.agent.loose_match import _loose_line_match
from accuretta.agent.tool_history import _tool_history_lock
from accuretta.core.chats import _get_current_chat
from accuretta.core.ignore_rules import is_ignored
from accuretta.core.undo import _undo_snapshot
from accuretta.core.undo import _undo_store
from accuretta.core.visible_reply import _visible_code_source
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import _critical_path_gate
from accuretta.security.approvals import request_approval
from accuretta.security.blocked_writes import _blocked_write
from accuretta.security.blocked_writes import _blocked_write_key
from accuretta.security.blocked_writes import _blocked_writes
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import is_blocked_path
from accuretta.security.path_guards import is_in_workspace
from accuretta.security.self_protect import is_bridge_self_path
from accuretta.tools.code_edit import _note_web_derived_file
from accuretta.tools.code_edit import _run_linter
from accuretta.tools.code_edit import _with_code_check
from accuretta.tools.file_read import _chat_file_reads
from accuretta.tools.file_read import _chat_file_reads_lock
from accuretta.tools.file_read import _file_md5
from accuretta.tools.file_read import _missing_file_error
from accuretta.tools.file_read import _record_file_read
from accuretta.tools.host_shell import _checkpoint_shell_deletes
from accuretta.tools.host_shell import _snapshot_shell_deletes
from accuretta.tools.ps_bom import _UTF8_BOM
from pathlib import Path


def _diff_preview_rows(old: str, new: str, max_rows: int = 400,
                       max_chars: int = 160) -> tuple[list, int, int]:
    """+N/−M preview for approval gates, as [op, text] row pairs.

    The notch's gate card scrolls through these. Capped so the broadcast
    payload stays bounded: at most max_rows rows are attached, but adds/dels
    count every changed line. Pure difflib — no external diff process."""
    old_lines = str(old or "").splitlines()
    new_lines = str(new or "").splitlines()
    adds = dels = 0
    rows: list = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        op = line[:1]
        if op not in ("+", "-"):
            continue                      # '@@' hunk headers and equal lines
        text = line[1:].rstrip("\r")
        if op == "+":
            adds += 1
        else:
            dels += 1
        if len(rows) < max_rows:
            shown = text[:max_chars] + "…" if len(text) > max_chars else text
            rows.append([op, shown])
    return rows, adds, dels


def tool_list_directory(args: dict) -> dict:
    path = normalize_path(args.get("path") or str(Path.home()))
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if is_data_path(path):
        return {"error": DATA_PATH_ERROR}
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}", "expected_type": "directory",
                "actual_type": "file" if os.path.isfile(path) else "unknown"}
    out = []
    skipped = 0
    try:
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if is_ignored(full):
                skipped += 1
                continue
            try:
                st = os.stat(full)
                out.append({
                    "name": entry,
                    "path": full,
                    "dir": os.path.isdir(full),
                    "size": st.st_size if os.path.isfile(full) else None,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
    except PermissionError as e:
        return {"error": f"permission denied: {e}"}
    resp = {"path": path, "entries": out[:200]}
    if skipped:
        resp["ignored"] = skipped
    if len(out) > 200:
        resp["truncated"] = True
    return resp


def _file_staleness(path: str) -> str:
    """'' = the model's view is current. Otherwise a short reason it may be stale."""
    cid = _get_current_chat()
    if not cid or not os.path.isfile(path):
        return ""
    npath = normalize_path(path)
    with _chat_file_reads_lock:
        seen = _chat_file_reads.get(cid, {}).get(npath)
    if seen is None:
        return "never read this session"
    cur = _file_md5(path)
    if cur and cur != seen:
        return "changed since you last read it"
    return ""


def _decode_text_file(raw: bytes) -> tuple[str, str, bool]:
    """(text with LF line endings, the file's line ending, whether it has a BOM)."""
    bom = raw.startswith(_UTF8_BOM)
    text = raw.decode("utf-8-sig")
    eol = "\r\n" if text.count("\r\n") * 2 > text.count("\n") else "\n"
    return text.replace("\r\n", "\n"), eol, bom


def _encode_text_file(text: str, eol: str, bom: bool) -> bytes:
    text = text.replace("\r\n", "\n")
    if eol != "\n":
        text = text.replace("\n", eol)
    return (_UTF8_BOM if bom else b"") + text.encode("utf-8")


def tool_write_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    content = args.get("content")
    # Append adds a chunk at the end of the file, so a large file can be built
    # in parts that each fit comfortably in one reply.
    append = str(args.get("mode") or "overwrite").strip().lower() == "append"
    source_meta: dict = {}
    if not path:
        return {"error": "missing path"}
    if is_data_path(path, write=True):
        return {"error": DATA_PATH_ERROR}
    if args.get("source"):
        if isinstance(content, str) and content.strip():
            return {"error": "refused: provide either content or source, not both. No file was written."}
        block, source_error = _visible_code_source(args)
        if source_error:
            return {"error": source_error}
        content = block["content"]
        source_meta = {
            "source": "visible_chat_code_block",
            "language": block["language"],
            "message_index": block["message_index"],
        }
    elif args.get("force") and not (isinstance(content, str) and content.strip()):
        content = _blocked_write(path)
        if content is not None:
            source_meta = {"source": "blocked_write"}
    if not isinstance(content, str) or not content.strip():
        return {"error": "refused: content or source is empty or missing. No file was written."}
    if is_bridge_self_path(path):
        return {"error": "refused: that file is the running Accuretta server's own code (bridge.py or the accuretta/ folder) — edit it from your real IDE, not from a chat tool call. Restart Accuretta afterward."}
    _cerr = _critical_path_gate(path, "write_file", "PROTECTED PATH - write under Windows/System32")
    if _cerr:
        return _cerr
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    # Anti-clobber: refuse to overwrite an existing file the model hasn't read this
    # session (or that changed since). An empty file is exempt: it is usually the
    # leftover of an earlier failed write (arguments stripped by llama.cpp's
    # parser), there is nothing in it to protect, and blocking would deadlock the
    # agent in a "exists -> never read -> blocked" loop.
    _existing_len = 0
    try:
        _existing_len = os.path.getsize(path) if os.path.isfile(path) else 0
    except OSError:
        pass
    if os.path.isfile(path) and not bool(args.get("force")):
        stale = ("" if _existing_len == 0 else _file_staleness(path))
        if stale:
            _blocked_writes[_blocked_write_key(path)] = (time.time(), content)
            while len(_blocked_writes) > 8:
                _blocked_writes.pop(next(iter(_blocked_writes)))
            return {"error": (f"write blocked: this file exists and was {stale}. Call read_file on it "
                              f"first so you overwrite the CURRENT contents, not a stale assumption — "
                          f"then write_file again. If you truly mean to replace the whole file, pass "
                              f"force=true. To write the content you just sent, call write_file with "
                              f"this path and force=true and leave out content."),
                    "path": path, "reason": stale}
    # Rewrite-loop breaker: refuse the Nth full rewrite of the same file in one
    # turn. The model can't verify a fresh version is better and spirals into
    # "let me rewrite from scratch"; this forces targeted edits instead.
    _npath = normalize_path(path)
    _prior = _write_count(_npath)
    if (not append and os.path.isfile(path) and _prior >= REWRITE_LOOP_LIMIT
            and not bool(args.get("force_rewrite"))):
        return {"error": (f"rewrite loop blocked: you have already rewritten this file {_prior} time(s) this "
                          f"turn and it IS saved. STOP rewriting from scratch — you cannot verify a fresh "
                          f"version is better and you are spiralling. Instead: if a SPECIFIC thing is wrong, "
                              f"use edit_file for a targeted change, or check the saved result against the request. "
                              f"The rewrite limit does not establish correctness. Only pass force_rewrite=true with a concrete reason."),
                    "path": path, "rewrites_this_turn": _prior, "loop_breaker": True, "not_executed": True}
    # Diff preview rows for the approval card (the notch renders them). Read-only
    # peek; the write itself happens only after the decision.
    _old_for_diff = ""
    try:
        if os.path.isfile(path):
            _old_for_diff = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        _old_for_diff = ""
    _diff_rows, _adds, _dels = _diff_preview_rows(
        _old_for_diff, _old_for_diff + content if append else content)
    _note_web_derived_file(path, content)
    _wf_details = {"kind": "write_file", "path": path, "bytes": len(content.encode('utf-8'))}
    if append:
        _wf_details["mode"] = "append"
    if _diff_rows:
        _wf_details["diff"] = _diff_rows
        _wf_details["diff_counts"] = {"adds": _adds, "dels": _dels}
    _verb = "Add-Content" if append else "Set-Content"
    approval = request_approval(
        title="Append to file" if append else "Write file",
        command=f'{_verb} -Path "{path}" -Value <{len(content)} chars>',
        details=_wf_details,
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied write ({approval.get('status')})"}
    _undo_snapshot(path)
    try:
        old_text, eol, bom = "", _new_file_eol(path), False
        if os.path.isfile(path):
            try:
                old_text, eol, bom = _decode_text_file(Path(path).read_bytes())
            except (OSError, UnicodeDecodeError):
                if append:
                    return {"error": f"cannot append: {path} is not readable UTF-8 text. No file was written."}
        content = content.replace("\r\n", "\n")
        new_text = old_text + content if append else content
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(_encode_text_file(new_text, eol, bom))
        _blocked_writes.pop(_blocked_write_key(path), None)
        _undo_store.checkpoint(path)
        _record_file_read(path)  # our hash now matches what we just wrote
        if not append:
            _record_write(_npath)    # count this rewrite for the loop-breaker

        import difflib
        diff = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            n=0
        ))
        added = 0
        deleted = 0
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                deleted += 1
                
        lint_err = _run_linter(path)
        result = {"ok": True, "path": path, "bytes": len(content.encode("utf-8")), "added": added, "deleted": deleted}
        if append:
            result["mode"] = "append"
            result["total_bytes"] = len(new_text.encode("utf-8"))
        _with_code_check(result, lint_err)
        result.update(source_meta)
        if not append and _write_count(_npath) >= 2:
            result["warning"] = ("you have now rewritten this file from scratch. If it still isn't right, make a "
                                 "TARGETED edit_file fix next — do NOT rewrite the whole file again, you'll spiral.")
        return result
    except Exception as e:
        return {"error": str(e)}


def tool_edit_file(args: dict) -> dict:
    """Apply surgical search-and-replace edits to an existing file.
    Each edit finds old_text and replaces it with new_text.
    old_text must be unique or an exact single match — ambiguous matches are rejected.
    """
    path = normalize_path(args.get("path") or "")
    edits = args.get("edits") or []
    if not path:
        return {"error": "missing path"}
    if not isinstance(edits, list) or not edits:
        return {"error": "edits must be a non-empty list of {old_text, new_text}"}
    if is_bridge_self_path(path):
        return {"error": "refused: that file is the running Accuretta server's own code (bridge.py or the accuretta/ folder) — edit it from your real IDE, not from a chat tool call. Restart Accuretta afterward."}
    if is_data_path(path, write=True):
        return {"error": DATA_PATH_ERROR}
    _cerr = _critical_path_gate(path, "edit_file", "PROTECTED PATH - edit under Windows/System32")
    if _cerr:
        return _cerr
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    if not os.path.isfile(path):
        return _missing_file_error(path)

    try:
        text, eol, bom = _decode_text_file(Path(path).read_bytes())
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"read failed: {e}"}

    applied = []
    errors = []
    modified = text

    for i, edit in enumerate(edits):
        if not isinstance(edit, dict):
            errors.append(f"edit {i}: each edit must be an object with old_text and new_text")
            continue
        old = str(edit.get("old_text", edit.get("old_string", "")) or "").replace("\r\n", "\n")
        new = str(edit.get("new_text", edit.get("new_string", "")) or "").replace("\r\n", "\n")
        if not old:
            errors.append(f"edit {i}: old_text is empty")
            continue

        count = modified.count(old)
        if count == 0:
            # try stripping common surrounding whitespace for a fuzzy match
            old_stripped = old.strip()
            if old_stripped and old_stripped != old:
                count = modified.count(old_stripped)
                if count == 1:
                    # The file keeps its own whitespace around the match, so the
                    # replacement drops the model's: "\t" + "    x" mixed tabs and spaces.
                    modified = modified.replace(old_stripped, new.strip(), 1)
                    applied.append({"edit": i, "match": "fuzzy", "old": old[:60], "new": new[:60]})
                    continue
                elif count > 1:
                    errors.append(f"edit {i}: fuzzy match '{old[:40]}' appears {count} times — ambiguous")
                    continue
            # Whole lines that differ only in whitespace (indentation depth,
            # tabs vs spaces, trailing spaces): local models misquote these a lot.
            loose, _why = _loose_line_match(modified, old, new)
            if loose is not None:
                modified = loose
                applied.append({"edit": i, "match": "whitespace", "old": old[:60], "new": new[:60]})
                continue
            hint = _closest_region(modified, old)
            errors.append(f"edit {i}: '{old[:40]}' not found in file"
                          + (f". Closest lines in the file now:\n{hint}" if hint else ""))
            continue
        elif count > 1:
            errors.append(f"edit {i}: '{old[:40]}' appears {count} times — must be unique")
            continue
        else:
            modified = modified.replace(old, new, 1)
            applied.append({"edit": i, "match": "exact", "old": old[:60], "new": new[:60]})

    if errors:
        # All or nothing: say plainly that nothing was written. "applied: N"
        # read as if N edits had landed, and the model built on edits that never were.
        return {
            "error": ("nothing was written (edits are all-or-nothing): "
                      + "; ".join(errors)
                      + (f". {len(applied)} other edit(s) matched; send them again together "
                         "with the corrected ones." if applied else "")),
            "applied": 0,
            "matched": len(applied),
            "failed": len(errors),
            "path": path,
        }

    # approval: show a diff-like summary
    diff_lines = []
    for a in applied:
        diff_lines.append(f"- {a['old'][:50]}")
        diff_lines.append(f"+ {a['new'][:50]}")
    preview = "\n".join(diff_lines) if diff_lines else "(no preview)"
    _note_web_derived_file(path, "\n".join(
        str(e.get("new_text", e.get("new_string", "")) or "") for e in edits if isinstance(e, dict)))
    _ef_details = {"kind": "edit_file", "path": path, "edits": len(applied), "preview": preview}
    # Full line diff for the approval card (the notch scrolls through it).
    _diff_rows, _adds, _dels = _diff_preview_rows(text, modified)
    if _diff_rows:
        _ef_details["diff"] = _diff_rows
        _ef_details["diff_counts"] = {"adds": _adds, "dels": _dels}
    approval = request_approval(
        title="Edit file",
        command=f'edit {len(applied)} location(s) in "{path}"',
        details=_ef_details,
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied edit ({approval.get('status')})"}
    _undo_snapshot(path)

    try:
        import difflib
        diff = list(difflib.unified_diff(
            text.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            n=0
        ))
        added = 0
        deleted = 0
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                deleted += 1

        Path(path).write_bytes(_encode_text_file(modified, eol, bom))
        _undo_store.checkpoint(path)
        _record_file_read(path)  # keep our hash current after our own edit

        lint_err = _run_linter(path)
        return _with_code_check({
            "ok": True,
            "path": path,
            "edits_applied": len(applied),
            "bytes": len(modified.encode("utf-8")),
            "added": added,
            "deleted": deleted,
        }, lint_err)
    except Exception as e:
        return {"error": str(e)}


def tool_delete_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": f"not found: {path}"}
    if is_bridge_self_path(path):
        return {"error": "refused: that is the running Accuretta server's own code (bridge.py or the accuretta/ folder) — won't delete it."}
    if is_data_path(path, write=True):
        return {"error": DATA_PATH_ERROR}
    _cerr = _critical_path_gate(path, "delete", "PROTECTED PATH - delete under Windows/System32")
    if _cerr:
        return _cerr
    if not is_in_workspace(path):
        return {"error": "path outside workspace"}
    approval = request_approval(
        title="Delete",
        command=f'Remove-Item -Path "{path}" -Recurse -Force',
        details={"kind": "delete", "path": path, "dir": os.path.isdir(path)},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied delete ({approval.get('status')})"}
    # A folder's text files go into the undo journal too, not only a single file.
    snapped = _snapshot_shell_deletes([path]) if os.path.isdir(path) else []
    _undo_snapshot(path)
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        _undo_store.checkpoint(path)
        _checkpoint_shell_deletes(snapped)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}


_tool_write_history: dict[str, dict[str, int]] = {}    # chat_id -> path -> count


# Rewrite-loop breaker: count full write_file rewrites per path per turn so a
# model stuck on "let me rewrite from scratch" is forced into targeted edits.
# Resets every turn with the tool-call history above.
REWRITE_LOOP_LIMIT = 2  # original write + one rewrite; the next is blocked


def _write_count(npath: str) -> int:
    with _tool_history_lock:
        return _tool_write_history.get(_get_current_chat(), {}).get(npath, 0)


def _record_write(npath: str) -> None:
    cid = _get_current_chat()
    with _tool_history_lock:
        w = _tool_write_history.setdefault(cid, {})
        w[npath] = w.get(npath, 0) + 1
