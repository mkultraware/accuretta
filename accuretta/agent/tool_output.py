# SPDX-License-Identifier: AGPL-3.0-only
"""Fitting a tool result into the model's context: HTTP summaries, plain-text
views, elision, and the full output saved to a file the model can read back.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from accuretta.agent.truncation import _elide_text
from accuretta.agent.truncation import _truncation_envelope
from accuretta.redteam.recon_map import _http_body_kind
from agent_reliability import digest
from pathlib import Path
from typing import Any


from accuretta.redteam.http_summary import (  # noqa: F401  re-export: moved 2026-11-23
    _HTTP_SUMMARY_MIN_BODY,
    _HTTP_SUMMARY_TEXT_CHARS,
    _HTTP_SUMMARY_HEADER_CHARS,
    _HTTP_SUMMARY_HEADER_COUNT,
    _HTTP_SUMMARY_COOKIE_CHARS,
    _HTTP_SUMMARY_COOKIE_COUNT,
    _HTTP_NOTABLE_HEADERS,
    _clip_text,
    _clip_headers,
    _html_visible_text,
    _http_body_preview,
    _http_summary,
    _http_model_view,
)


# Fields known to carry the bulk of a tool's output. They are elided in place
# (head + marker + tail) before serializing, so the JSON stays valid and the
# trailing diagnostics (errors are usually at the bottom of stdout) survive.
_BULK_TOOL_FIELDS = ("stdout", "stderr", "content", "text", "output", "html", "body")


_PLAIN_RESULT_SHOWN = frozenset({
    "ok", "exit", "exit_code", "stdout", "stderr", "executed", "execution_target", "cwd",
    "content", "path", "offset", "lines_returned", "total_lines", "size", "truncated",
    "next_offset", "hint", "matches", "files", "pattern", "root", "match_count", "files_scanned",
    "files_skipped_binary", "files_skipped_size",
})


def _plain_tool_text(name: str, result: dict) -> str | None:
    """Plain-text view of a successful file read, search or shell result.

    As JSON, every quote, backslash and newline in code or command output is
    escaped (about 14% more characters in real sessions), and the model has to
    undo that before it copies text into edit_file's old_text. Other fields of
    the result follow the header as "key: value" lines."""
    if result.get("error"):
        return None
    if name == "read_file" and isinstance(result.get("content"), str) and "lines_returned" in result:
        first = int(result.get("offset") or 0) + 1
        last = first + int(result.get("lines_returned") or 0) - 1
        head = f"[{result.get('path')} | lines {first}-{last} of {result.get('total_lines')}"
        if result.get("truncated"):
            head += f" | more: read_file offset={result.get('next_offset')}"
        body = result["content"]
    elif name == "grep_files" and isinstance(result.get("matches"), list):
        head = (f"[grep {result.get('pattern')!r} in {result.get('root')} | "
                f"{result.get('match_count', len(result['matches']))} matches, "
                f"{result.get('files_scanned', '?')} files scanned"
                + (" | truncated" if result.get("truncated") else ""))
        lines: list[str] = []
        if isinstance(result.get("files"), list):
            lines = [f"{f.get('path')} ({f.get('count')})" for f in result["files"] if isinstance(f, dict)]
        current = None
        for m in result["matches"]:
            if not isinstance(m, dict):
                continue
            if m.get("path") != current:
                current = m.get("path")
                lines.append(str(current))
            line_no = int(m.get("line") or 0)
            before = m.get("before") or []
            lines += [f"  {line_no - len(before) + k}- {text}" for k, text in enumerate(before)]
            lines.append(f"  {line_no}: {m.get('text')}")
            lines += [f"  {line_no + 1 + k}- {text}" for k, text in enumerate(m.get("after") or [])]
        body = "\n".join(lines) if lines else "(no matches)"
    elif name in ("run_powershell", "run_python") and isinstance(result.get("stdout"), str):
        head = f"[exit {result.get('exit_code')}" + (f" | cwd {result['cwd']}" if result.get("cwd") else "")
        body = result["stdout"]
        if result.get("stderr"):
            body += ("" if not body or body.endswith("\n") else "\n") + "[stderr]\n" + result["stderr"]
    else:
        return None
    extras = []
    for key, value in result.items():
        if key in _PLAIN_RESULT_SHOWN or value in (None, "", [], {}):
            continue
        shown = value if isinstance(value, (str, int, float, bool)) else json.dumps(
            value, ensure_ascii=False, default=str)
        extras.append(f"{key}: {str(shown)[:600]}")
    return head + "]\n" + "".join(e + "\n" for e in extras) + body


from accuretta.tools.saved_output import (  # noqa: F401  re-export: moved 2026-11-23
    _TOOL_OUTPUT_DIR,
    _TOOL_OUTPUT_KEEP,
    _save_full_output,
    _is_saved_tool_output,
)


def compress_tool_result(name: str, result: Any, cap: int) -> str:
    """Serialize a tool result as JSON for the model, applying line-aware
    head/tail elision to known bulk fields BEFORE serializing. Errors are
    never elided (they're always small and always critical). Iteratively
    tightens the per-field budget if JSON escaping overhead pushes the
    result over cap; falls back to a minimal truncation envelope rather
    than producing malformed JSON."""
    # Non-dict results (lists, scalars). For lists we elide the JSON-string
    # form; for scalars we just serialize. Both stay valid JSON because we
    # only ever return either the full serialization or the safe envelope.
    if not isinstance(result, dict):
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) <= cap:
            return s
        # Try to keep a head sample of the list as a separate JSON; otherwise
        # fall through to the envelope.
        if isinstance(result, list) and len(result) > 4:
            for keep in (200, 100, 50, 20, 10, 5):
                if keep >= len(result):
                    continue
                sample = result[:keep]
                sampled = {
                    "_truncated": True,
                    "_note": f"showing first {keep} of {len(result)} items",
                    "items": sample,
                }
                ss = json.dumps(sampled, ensure_ascii=False, default=str)
                if len(ss) <= cap:
                    return ss
        return _truncation_envelope(result, cap, len(s))

    # Errors: never compress, always pass through verbatim. They're critical
    # and almost always small. If a tool somehow returns an error PLUS a
    # huge stdout (rare), strip the bulk fields rather than chopping the
    # error message itself.
    if isinstance(result.get("error"), str) and len(result["error"]) < cap // 2:
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) <= cap:
            return s
        # A failed command's output is usually why it failed (a timeout's last
        # lines, a crash's traceback): elide it to fit instead of dropping it.
        bulk = [k for k in _BULK_TOOL_FIELDS if isinstance(result.get(k), str) and result[k]]
        if bulk:
            trimmed = dict(result)
            if not (result.get("evidence_receipt") or result.get("research_source_id")):
                saved = _save_full_output(name, s)
                if saved:
                    trimmed["_full_output"] = (f"{saved} ({len(s):,} chars; read_file it "
                                               "with offset to see what was elided)")
            rest = json.dumps({k: v for k, v in trimmed.items() if k not in bulk},
                              ensure_ascii=False, default=str)
            per_field = (cap - len(rest) - 64 * len(bulk)) // len(bulk)
            for _ in range(4):
                if per_field < 300:
                    break
                for k in bulk:
                    trimmed[k] = _elide_text(result[k], per_field)
                ss = json.dumps(trimmed, ensure_ascii=False, default=str)
                if len(ss) <= cap:
                    return ss
                per_field = per_field * 3 // 4
        slim = {k: v for k, v in result.items() if k not in _BULK_TOOL_FIELDS}
        slim["_note"] = "bulk fields stripped — error preserved verbatim"
        ss = json.dumps(slim, ensure_ascii=False, default=str)
        if len(ss) <= cap:
            return ss
        return _truncation_envelope(result, cap, len(s))

    plain = _plain_tool_text(name, result)
    if plain is not None:
        if len(plain) > cap and name != "read_file":   # read_file pages itself
            saved = _save_full_output(name, plain)
            if saved:
                note = (f"[output cut to fit: all {len(plain):,} chars are saved in {saved}; "
                        "read_file it with offset to see the cut part]\n")
                return note + _elide_text(plain, max(512, cap - len(note)))
        return _elide_text(plain, cap)

    # HTTP results are the biggest single source of context bloat: one page can
    # carry a megabyte of boilerplate. Once the full response is in an evidence
    # receipt, show status/metadata/headers plus a body preview instead of raw
    # HTML/JSON. The receipt reader pages through the untouched body later.
    if name == "http_request":
        result = _http_model_view(result)

    s_full = json.dumps(result, ensure_ascii=False, default=str)
    if len(s_full) <= cap:
        return s_full

    # In-place compression of bulk fields. The budget is split across the fields
    # present so {stdout: 100KB, stderr: 50KB} shrink proportionally, and is
    # tightened iteratively because JSON escaping inflates the serialized size by
    # a few percent.
    bulk_present = [
        k for k in _BULK_TOOL_FIELDS
        if isinstance(result.get(k), str) and len(result[k]) > 200
    ]
    if bulk_present:
        scratch = dict(result)
        # Evidence and research results have their own reader; the rest get the
        # untouched result in a file, so the elided middle is never just gone.
        if not (result.get("evidence_receipt") or result.get("research_source_id")):
            saved = _save_full_output(name, s_full)
            if saved:
                scratch["_full_output"] = (f"{saved} ({len(s_full):,} chars; read_file it "
                                           "with offset to see what was elided)")
                result = {**result, "_full_output": scratch["_full_output"]}
        for k in bulk_present:
            scratch[k] = ""
        overhead = len(json.dumps(scratch, ensure_ascii=False, default=str))
        # Start with the full available budget per field, then halve until
        # the serialized result fits or the per-field budget would be too
        # small to be useful.
        base_budget = max(256, (cap - overhead) // len(bulk_present))
        for attempt in range(6):
            per_field = max(256, base_budget >> attempt)
            compressed = dict(result)
            for k in bulk_present:
                compressed[k] = _elide_text(result[k], per_field)
            s = json.dumps(compressed, ensure_ascii=False, default=str)
            if len(s) <= cap:
                return s
            if per_field <= 256:
                break  # can't tighten further usefully

    # Large list-valued fields (tool catalogs, folder listings, match sets):
    # sample the head of each so the model still sees real entries and a count and
    # knows to re-call with narrower args. The blind envelope below hides
    # everything, which makes models re-emit the same call in a loop.
    big_list_fields = sorted(
        k for k, v in result.items()
        if isinstance(v, list) and len(v) > 5 and k not in _BULK_TOOL_FIELDS)
    if big_list_fields:
        for sample_n in (200, 100, 50, 20, 10, 5):
            sampled = dict(result)
            for k in big_list_fields:
                items = result[k]
                if len(items) > sample_n:
                    sampled[k] = items[:sample_n]
            s = json.dumps(sampled, ensure_ascii=False, default=str)
            if len(s) <= cap:
                sampled["_truncated"] = True
                bodies = "; ".join(
                    f"{k}: showing first {sample_n} of {len(result[k])} items"
                    for k in big_list_fields if len(result[k]) > sample_n)
                sampled["_note"] = (bodies +
                                    " — re-call the tool with narrower/filtered args "
                                    "to see the rest")
                return json.dumps(sampled, ensure_ascii=False, default=str)

    # Final fallback: minimal envelope. Always valid JSON.
    return _truncation_envelope(result, cap, len(s_full))
