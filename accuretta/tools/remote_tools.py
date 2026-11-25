# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's remote-machine tools: a read-only shell, listing, reading, copying both
ways, and writing files in approved, hash-checked stages.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import hashlib
import os
import posixpath
import re
import shlex
import threading
import time
import uuid
from accuretta.core.chats import _get_current_chat
from accuretta.core.settings import get_settings
from accuretta.core.visible_reply import _visible_code_source
from accuretta.core.workspace import normalize_path
from accuretta.runtime.model_observation import _client_context_by_chat
from accuretta.security.approvals import request_approval
from accuretta.security.data_paths import is_data_path
from accuretta.security.hard_blocks import _catastrophic_cmd
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import is_in_workspace
from accuretta.tools.remote_helpers import _resolved_remote_roots
from accuretta.tools.remote_machines import REMOTE_DIR
from accuretta.tools.remote_machines import _remote_machine
from accuretta.tools.remote_pairing import _protect_remote_private_key
from accuretta.tools.remote_ssh import _run_remote_process
from pathlib import Path


REMOTE_STAGING_DIR = REMOTE_DIR / "staging"


_remote_file_reads: dict[tuple[str, str, str], str] = {}


_remote_file_reads_lock = threading.Lock()


_remote_staged_writes: dict[str, dict] = {}


_remote_staged_writes_lock = threading.Lock()


def _normalize_remote_path(row: dict, raw: str) -> str:
    value = str(raw or "").strip().replace("\\", "/")
    home = posixpath.normpath(str(row.get("home") or ""))
    if value == "~":
        value = home
    elif value.startswith("~/"):
        value = posixpath.join(home, value[2:])
    value = posixpath.normpath(value)
    if not value.startswith("/") or not home:
        raise ValueError("Use an absolute Mac path or a path beginning with ~/.")
    roots = _resolved_remote_roots(row)
    if not any(value == root or value.startswith(root.rstrip("/") + "/") for root in roots):
        raise ValueError("That path is outside this Mac pairing's allowed folders.")
    return value


def _confirm_remote_path(row: dict, path: str, *, directory: bool = False) -> tuple[str, str]:
    """Resolve parent symlinks on the Mac and re-check the verified root fence."""
    quoted = shlex.quote(path)
    if directory:
        command = f"test -d {quoted} && cd -P -- {quoted} && pwd -P"
    else:
        command = (
            f"test ! -L {quoted} || exit 74; parent=$(dirname -- {quoted}); "
            f"base=$(basename -- {quoted}); real_parent=$(cd -P -- \"$parent\" 2>/dev/null && pwd -P) || exit 75; "
            "printf '%s/%s\\n' \"$real_parent\" \"$base\""
        )
    result = _run_remote_process(row, command, timeout=25, max_stdout=4096, stream_name="")
    if not result.get("ok"):
        if result.get("exit") == 74:
            return "", "Refused a symbolic-link file. Choose the real file inside an allowed folder."
        return "", result.get("stderr") or result.get("error") or "Could not verify the remote path."
    lines = (result.get("stdout_bytes") or b"").decode("utf-8", "replace").strip().splitlines()
    if not lines or not lines[-1].startswith("/"):
        return "", "The Mac returned an invalid resolved path."
    resolved = posixpath.normpath(lines[-1])
    roots = _resolved_remote_roots(row)
    if not any(resolved == root or resolved.startswith(root.rstrip("/") + "/") for root in roots):
        return "", "The resolved Mac path escapes this pairing's allowed folders."
    return resolved, ""


def _selected_remote_machine(args: dict) -> dict | None:
    machine_id = str(args.get("machine") or "").strip()
    if not machine_id:
        ctx = _client_context_by_chat.get(_get_current_chat() or "", {})
        machine_id = str(ctx.get("execution_target") or "")
    row = _remote_machine(machine_id)
    if not row or row.get("status") != "ready":
        return None
    return row


_REMOTE_READ_ONLY_COMMANDS = {
    "pwd", "whoami", "id", "uname", "sw_vers", "date", "uptime", "hostname",
    "which", "df", "ps", "pgrep", "groups",
}


def _remote_command_refusal(command: str) -> str:
    cmd = str(command or "").strip()
    catastrophic = _catastrophic_cmd(cmd)
    if catastrophic:
        return f"this command {catastrophic}"
    lowered = " ".join(cmd.lower().split())
    rules = (
        (r"(^|[;&|]\s*)(sudo|doas|su)(\s|$)", "requests elevated or another user's privileges"),
        (r"\bdiskutil\s+(erase|partition|apfs\s+delete|secureerase)", "can erase or repartition a disk"),
        (r"\bcsrutil\b", "changes macOS System Integrity Protection"),
        (r"\bsecurity\s+authorizationdb\b", "changes macOS authorization policy"),
        (r"\blaunchctl\s+(bootstrap|bootout)\s+system\b", "changes system launch services"),
    )
    for pattern, reason in rules:
        if re.search(pattern, lowered):
            return reason
    return ""


def _remote_command_is_read_only(command: str) -> bool:
    cmd = str(command or "").strip()
    if not cmd or any(token in cmd for token in ("\n", "\r", ";", "&&", "||", "|", ">", "<", "`", "$(")):
        return False
    try:
        parts = shlex.split(cmd, posix=True)
    except ValueError:
        return False
    if not parts:
        return False
    first = parts[0]
    if first in _REMOTE_READ_ONLY_COMMANDS:
        return not any(arg in {"-delete", "--delete"} for arg in parts[1:])
    if first == "brew" and len(parts) > 1:
        return parts[1] in {"list", "config", "doctor"}
    if first in {"python", "python3", "node", "npm", "ruby", "clang", "swift", "xcodebuild"}:
        return len(parts) == 2 and parts[1] in {"--version", "-V", "-v"}
    return False


def _remote_text_result(result: dict) -> dict:
    raw = result.pop("stdout_bytes", b"") or b""
    result["stdout"] = raw.decode("utf-8", "replace")
    return result


def tool_remote_shell(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected. Pair it in Settings, then choose it in the top bar."}
    command = str(args.get("command") or "").strip()
    if not command:
        return {"error": "command is required"}
    refused = _remote_command_refusal(command)
    if refused:
        return {
            "error": f"refused: {refused}. Run it yourself on the Mac if that is genuinely intended.",
            "refused_command": command,
        }
    if not _remote_command_is_read_only(command):
        approval = request_approval(
            title=f"Run on {row.get('label') or 'paired Mac'}",
            command=command,
            details={
                "kind": "remote_shell", "force_prompt": True,
                "machine": row.get("id"), "target": row.get("label"),
            },
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied remote command ({approval.get('status')})"}
    result = _run_remote_process(
        row, command, timeout=args.get("timeout"), max_stdout=240_000,
        stream_name="remote_shell",
    )
    return _remote_text_result(result)


def tool_remote_list_directory(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    try:
        path = _normalize_remote_path(row, args.get("path") or "~/Desktop")
    except ValueError as exc:
        return {"error": str(exc)}
    path, path_error = _confirm_remote_path(row, path, directory=True)
    if path_error:
        return {"error": path_error}
    command = f"LC_ALL=C ls -laO -- {shlex.quote(path)}"
    result = _remote_text_result(_run_remote_process(row, command, max_stdout=240_000, stream_name=""))
    result["path"] = path
    return result


def _remote_file_metadata(row: dict, path: str) -> dict:
    path, path_error = _confirm_remote_path(row, path)
    if path_error:
        return {"ok": False, "error": path_error}
    quoted = shlex.quote(path)
    command = (
        f"test -f {quoted} || exit 44; "
        f"size=$(stat -f '%z' -- {quoted}) || exit 45; "
        f"hash=$(shasum -a 256 -- {quoted} | awk '{{print $1}}') || exit 46; "
        "printf '%s %s\\n' \"$size\" \"$hash\""
    )
    result = _remote_text_result(_run_remote_process(row, command, timeout=30, max_stdout=4096, stream_name=""))
    if not result.get("ok"):
        return result
    match = re.search(r"^(\d+)\s+([0-9a-fA-F]{64})\s*$", result.get("stdout") or "", re.MULTILINE)
    if not match:
        return {"ok": False, "error": "The Mac returned invalid file metadata."}
    return {"ok": True, "path": path, "size": int(match.group(1)), "sha256": match.group(2).lower()}


def tool_remote_read_file(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    try:
        path = _normalize_remote_path(row, args.get("path") or "")
        offset = max(0, int(args.get("offset") or 0))
        length = max(1, min(int(args.get("length") or 65536), 262144))
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)}
    meta = _remote_file_metadata(row, path)
    if not meta.get("ok"):
        return {"error": meta.get("stderr") or meta.get("error") or "Remote file not found."}
    path = meta["path"]
    command = f"dd if={shlex.quote(path)} bs=1 skip={offset} count={length} 2>/dev/null"
    result = _run_remote_process(row, command, timeout=60, max_stdout=length, stream_name="")
    if not result.get("ok"):
        return {"error": result.get("stderr") or result.get("error") or "Remote read failed."}
    raw = result.get("stdout_bytes") or b""
    with _remote_file_reads_lock:
        _remote_file_reads[(_get_current_chat() or "", str(row.get("id")), path)] = meta["sha256"]
    try:
        content = raw.decode("utf-8")
        binary = False
    except UnicodeDecodeError:
        content = _b64.b64encode(raw).decode("ascii")
        binary = True
    return {
        "ok": True, "path": path, "offset": offset, "bytes": len(raw),
        "size": meta["size"], "sha256": meta["sha256"],
        "content": content, "binary_base64": binary,
        "truncated": offset + len(raw) < meta["size"],
    }


def _remote_write_bytes(row: dict, path: str, payload: bytes,
                        expected_sha256: str = "") -> dict:
    quoted = shlex.quote(path)
    expected = str(expected_sha256 or "").lower()
    guard = ""
    if expected:
        guard = (
            f"current=$(shasum -a 256 -- {quoted} 2>/dev/null | awk '{{print $1}}'); "
            f"test \"$current\" = {shlex.quote(expected)} || "
            "{ printf 'STALE_REMOTE_FILE\\n' >&2; exit 73; }; "
        )
    command = (
        "set -eu; "
        f"dest={quoted}; parent=$(dirname -- \"$dest\"); test -d \"$parent\"; "
        f"{guard}"
        "tmp=\"${dest}.accuretta.$$\"; trap 'rm -f -- \"$tmp\"' EXIT HUP INT TERM; "
        "cat > \"$tmp\"; "
        "if test -e \"$dest\"; then mode=$(stat -f '%Lp' -- \"$dest\"); chmod \"$mode\" \"$tmp\"; fi; "
        "mv -f -- \"$tmp\" \"$dest\"; trap - EXIT HUP INT TERM"
    )
    result = _run_remote_process(
        row, command, timeout=max(60, int(len(payload) / 250_000) + 30),
        stdin=payload, max_stdout=4096, stream_name="",
    )
    if result.get("exit") == 73 or "STALE_REMOTE_FILE" in str(result.get("stderr") or ""):
        return {"ok": False, "error": "The Mac file changed since it was read. Read it again before overwriting it.", "stale": True}
    return _remote_text_result(result)


def _remote_payload_limit() -> int:
    try:
        mb = max(1, min(int(get_settings().get("remote_file_max_mb") or 100), 1024))
    except Exception:
        mb = 100
    return mb * 1024 * 1024


def _remote_expected_hash(row: dict, path: str, explicit: str = "") -> str:
    expected = str(explicit or "").strip().lower()
    if expected:
        return expected
    with _remote_file_reads_lock:
        return _remote_file_reads.get(
            (_get_current_chat() or "", str(row.get("id")), path), "")


def _remote_write_approved_payload(row: dict, path: str, payload: bytes,
                                   expected_sha256: str = "",
                                   title: str = "Write file on paired Mac") -> dict:
    if len(payload) > _remote_payload_limit():
        return {"error": "File exceeds the configured remote transfer limit."}
    approval = request_approval(
        title=title,
        command=f"Write {len(payload)} bytes to {path}",
        details={
            "kind": "remote_file_write", "force_prompt": True,
            "machine": row.get("id"), "path": path,
            "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(),
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied remote file write ({approval.get('status')})"}
    result = _remote_write_bytes(row, path, payload, expected_sha256)
    if result.get("ok"):
        result.update({
            "path": path, "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        })
    return result


def _cleanup_remote_staging(max_age_s: int = 86400) -> None:
    cutoff = time.time() - max_age_s
    try:
        for path in REMOTE_STAGING_DIR.glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def tool_remote_file_begin(args: dict) -> dict:
    """Open one path-bound staged write. Approval is intentionally paid here once."""
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    try:
        path = _normalize_remote_path(row, args.get("path") or "")
    except ValueError as exc:
        return {"error": str(exc)}
    path, path_error = _confirm_remote_path(row, path)
    if path_error:
        return {"error": path_error}
    approval = request_approval(
        title=f"Prepare a large file on {row.get('label') or 'paired Mac'}",
        command=f"Assemble one file in chunks, then write atomically to {path}",
        details={
            "kind": "remote_file_write", "force_prompt": True,
            "machine": row.get("id"), "path": path,
            "max_bytes": _remote_payload_limit(),
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied remote file write ({approval.get('status')})"}
    REMOTE_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_remote_staging()
    upload_id = uuid.uuid4().hex
    staging_path = REMOTE_STAGING_DIR / f"{upload_id}.part"
    staging_path.touch(exist_ok=False)
    _protect_remote_private_key(staging_path)
    record = {
        "id": upload_id, "chat_id": _get_current_chat() or "",
        "machine_id": str(row.get("id") or ""), "path": path,
        "staging_path": staging_path, "bytes": 0, "chunks": 0,
        "created": time.time(),
        "expected_sha256": _remote_expected_hash(row, path),
    }
    with _remote_staged_writes_lock:
        _remote_staged_writes[upload_id] = record
    return {
        "ok": True, "upload_id": upload_id, "path": path,
        "chunk_chars_max": 6000, "bytes": 0, "chunks": 0,
        "note": "Append the file in order, then commit it. Do not repeat earlier chunks.",
    }


def _owned_remote_stage(upload_id: str) -> tuple[dict | None, str]:
    token = str(upload_id or "").strip()
    with _remote_staged_writes_lock:
        record = _remote_staged_writes.get(token)
    if not record:
        return None, "Unknown or expired staged-write id. Start again with remote_file_begin."
    if record.get("chat_id") != (_get_current_chat() or ""):
        return None, "This staged write belongs to another chat."
    if time.time() - float(record.get("created") or 0) > 86400:
        with _remote_staged_writes_lock:
            _remote_staged_writes.pop(token, None)
        try:
            Path(record["staging_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        return None, "This staged write expired. Start it again."
    return record, ""


def tool_remote_file_append(args: dict) -> dict:
    record, error = _owned_remote_stage(args.get("upload_id") or "")
    if error:
        return {"error": error}
    content = args.get("content")
    if not isinstance(content, str) or not content:
        return {"error": "Chunk content is empty. Re-emit only this missing chunk."}
    if len(content) > 8000:
        return {"error": "Chunk is too large. Split it into chunks of at most 6000 characters."}
    payload = content.encode("utf-8")
    if int(record.get("bytes") or 0) + len(payload) > _remote_payload_limit():
        return {"error": "Staged file exceeds the configured remote transfer limit."}
    staging_path = Path(record["staging_path"])
    with _remote_staged_writes_lock:
        with staging_path.open("ab") as handle:
            handle.write(payload)
        record["bytes"] = int(record.get("bytes") or 0) + len(payload)
        record["chunks"] = int(record.get("chunks") or 0) + 1
        total = int(record["bytes"])
        chunks = int(record["chunks"])
    return {
        "ok": True, "upload_id": record["id"], "path": record["path"],
        "bytes": total, "chunks": chunks, "chunk_bytes": len(payload),
    }


def tool_remote_file_commit(args: dict) -> dict:
    record, error = _owned_remote_stage(args.get("upload_id") or "")
    if error:
        return {"error": error}
    row = _remote_machine(str(record.get("machine_id") or ""))
    if not row or row.get("status") != "ready":
        return {"error": "The paired Mac is unavailable. The staged file is retained for a later retry."}
    path, path_error = _confirm_remote_path(row, str(record.get("path") or ""))
    if path_error:
        return {"error": path_error}
    if path != record.get("path"):
        return {"error": "The resolved destination changed after approval. Start the staged write again."}
    staging_path = Path(record["staging_path"])
    payload = staging_path.read_bytes()
    if not payload:
        return {"error": "Staged file is empty. Append content before committing."}
    actual_sha = hashlib.sha256(payload).hexdigest()
    wanted_sha = str(args.get("sha256") or "").strip().lower()
    if wanted_sha and wanted_sha != actual_sha:
        return {
            "error": "Staged content SHA-256 does not match the expected value.",
            "actual_sha256": actual_sha,
        }
    result = _remote_write_bytes(
        row, path, payload, str(record.get("expected_sha256") or ""))
    if not result.get("ok"):
        result["upload_id"] = record["id"]
        result["retained"] = True
        return result
    with _remote_staged_writes_lock:
        _remote_staged_writes.pop(record["id"], None)
    staging_path.unlink(missing_ok=True)
    result = _remote_text_result(result) if "stdout_bytes" in result else result
    result.update({
        "ok": True, "path": path, "bytes": len(payload),
        "chunks": int(record.get("chunks") or 0), "sha256": actual_sha,
    })
    return result


def tool_remote_write_file(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    try:
        path = _normalize_remote_path(row, args.get("path") or "")
    except ValueError as exc:
        return {"error": str(exc)}
    path, path_error = _confirm_remote_path(row, path)
    if path_error:
        return {"error": path_error}
    content = args.get("content")
    source_meta: dict = {}
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
    if not isinstance(content, str) or not content.strip():
        return {"error": "refused: content or source is empty or missing. No file was written."}
    payload = content.encode("utf-8")
    expected = _remote_expected_hash(row, path, args.get("expected_sha256") or "")
    result = _remote_write_approved_payload(
        row, path, payload, expected,
        title=f"Write file on {row.get('label') or 'paired Mac'}",
    )
    if result.get("ok"):
        result.update(source_meta)
    return result


def tool_remote_copy_to(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    source = normalize_path(args.get("source") or "")
    if is_data_path(source):
        return {"error": DATA_PATH_ERROR}
    if not is_in_workspace(source) or not os.path.isfile(source):
        return {"error": "Source must be an existing file inside the PC workspace."}
    try:
        destination = _normalize_remote_path(row, args.get("destination") or "")
    except ValueError as exc:
        return {"error": str(exc)}
    destination, path_error = _confirm_remote_path(row, destination)
    if path_error:
        return {"error": path_error}
    size = os.path.getsize(source)
    if size > _remote_payload_limit():
        return {"error": "File exceeds the configured remote transfer limit."}
    approval = request_approval(
        title=f"Copy file to {row.get('label') or 'paired Mac'}",
        command=f"{source} -> {destination} ({size} bytes)",
        details={"kind": "remote_file_copy", "force_prompt": True, "machine": row.get("id"), "path": destination},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied file transfer ({approval.get('status')})"}
    payload = Path(source).read_bytes()
    result = _remote_write_bytes(row, destination, payload)
    if result.get("ok"):
        result.update({"source": source, "destination": destination, "bytes": size})
    return result


def tool_remote_copy_from(args: dict) -> dict:
    row = _selected_remote_machine(args)
    if not row:
        return {"error": "No verified Mac is selected."}
    try:
        source = _normalize_remote_path(row, args.get("source") or "")
    except ValueError as exc:
        return {"error": str(exc)}
    destination = normalize_path(args.get("destination") or "")
    if is_data_path(destination, write=True):
        return {"error": DATA_PATH_ERROR}
    if not is_in_workspace(destination):
        return {"error": "Destination must be inside the PC workspace."}
    meta = _remote_file_metadata(row, source)
    if not meta.get("ok"):
        return {"error": meta.get("stderr") or meta.get("error") or "Remote file not found."}
    if meta["size"] > _remote_payload_limit():
        return {"error": "File exceeds the configured remote transfer limit."}
    source = meta["path"]
    approval = request_approval(
        title=f"Copy file from {row.get('label') or 'paired Mac'}",
        command=f"{source} -> {destination} ({meta['size']} bytes)",
        details={"kind": "remote_file_copy", "force_prompt": True, "machine": row.get("id"), "path": source},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied file transfer ({approval.get('status')})"}
    result = _run_remote_process(row, f"cat -- {shlex.quote(source)}", timeout=max(60, int(meta["size"] / 250_000) + 30),
                                 max_stdout=meta["size"] + 1, stream_name="")
    if not result.get("ok"):
        return {"error": result.get("stderr") or result.get("error") or "Remote transfer failed."}
    payload = result.get("stdout_bytes") or b""
    if len(payload) != meta["size"] or hashlib.sha256(payload).hexdigest() != meta["sha256"]:
        return {"error": "Transferred bytes failed the size or SHA-256 integrity check."}
    dest_path = Path(destination)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_name(dest_path.name + ".accuretta.part")
    tmp.write_bytes(payload)
    tmp.replace(dest_path)
    return {"ok": True, "source": source, "destination": destination, "bytes": len(payload), "sha256": meta["sha256"]}


def tool_remote_file_abort(args: dict) -> dict:
    record, error = _owned_remote_stage(args.get("upload_id") or "")
    if error:
        return {"error": error}
    with _remote_staged_writes_lock:
        _remote_staged_writes.pop(record["id"], None)
    Path(record["staging_path"]).unlink(missing_ok=True)
    return {"ok": True, "upload_id": record["id"], "aborted": True}
