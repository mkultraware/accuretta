# SPDX-License-Identifier: AGPL-3.0-only
"""Folding old turns into a summary: the anchors worth keeping (paths, URLs,
ids), the summary request and its sections, tool facts, and the continuity note.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.tools.remote_helpers import _text_on_wire
from typing import Any


_TASK_ACK_RE = re.compile(
    r"^(?:ok(?:ay)?|yes|yep|yeah|sure|thanks?|thank you|great|nice|cool|"
    r"go ahead|continue|proceed|do it|sounds good|alright|all right)[.!\s]*$",
    re.IGNORECASE,
)


def _task_anchor_text(content: Any) -> str:
    """Compact, deterministic user text suitable for the recency tail."""
    if isinstance(content, list):
        content = " ".join(
            str(p.get("text") or "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        )
    text = str(content or "")
    text = re.sub(r"\[referenced-files\][\s\S]*?\[/referenced-files\]", "", text,
                  flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


_CONTINUITY_VERIFY_PATHS = 5     # unverified files listed by path (the count covers the rest)


def _render_continuity_state(state: dict, ctx_limit: int | None = None,
                             wire_text: str = "", objective_shown: bool = False) -> str:
    """The STRUCTURED CONTINUITY section of the task-state note.

    `wire_text` is the user-message text already in this request: a request
    copy that is still there word for word is not repeated (one airbench note
    carried the task three times, ~1,000 tokens of its ~3,600). It is kept
    whenever the request has been folded out of the prompt. `objective_shown`
    drops the goal lines the OBJECTIVE section of the same note carries."""
    if not isinstance(state, dict):
        return ""
    original_chars, recent_chars, recent_count, anchor_chars = _compaction_render_limits(ctx_limit)
    wire_norm = re.sub(r"\s+", " ", str(wire_text or ""))
    lines = []
    if state.get("goal") and not objective_shown:
        lines.append("goal: " + str(state["goal"]))
    if (state.get("current_task") and state.get("current_task") != state.get("goal")
            and not objective_shown):
        lines.append("current task: " + str(state["current_task"]))
    original = str(state.get("verbatim_original_user") or "").strip()
    if original and _text_on_wire(original, wire_norm):
        original = ""
    if original:
        original = _compaction_excerpt(original, original_chars)
        chunks = [original[i:i + 620] for i in range(0, len(original), 620)]
        for index, chunk in enumerate(chunks):
            label = ("original user request (sensitive values redacted)"
                     if index == 0 else "original user request (continued)")
            lines.append(label + ": " + chunk)
    recent_messages = list(state.get("recent_user_messages") or [])[-recent_count:]
    for message in recent_messages:
        text = _compaction_excerpt(str(message or "").strip(), recent_chars)
        if not text or _text_on_wire(text, wire_norm):
            continue
        chunks = [text[i:i + 620] for i in range(0, len(text), 620)]
        for index, chunk in enumerate(chunks):
            label = "recent user instruction (verbatim)" if index == 0 else "recent user instruction (continued)"
            lines.append(label + ": " + chunk)
    for anchor in _render_anchor_subset(state.get("exact_anchors") or [], anchor_chars):
        if str(anchor).strip():
            lines.append("exact source anchor: " + str(anchor))
    for item in state.get("constraints") or []:
        lines.append("constraint: " + str(item))
    for item in state.get("completed") or []:
        lines.append("completed (tool-backed): " + str(item))
    for item in state.get("failures") or []:
        lines.append("failed/unresolved: " + str(item))
    for item in state.get("next_steps") or []:
        if isinstance(item, dict):
            lines.append(f"next ({item.get('status', 'pending')}): {item.get('step', '')}")
    for item in state.get("findings") or []:
        if isinstance(item, dict):
            lines.append(f"finding ({item.get('status', 'candidate')}/{item.get('severity', 'info')}): "
                         f"{item.get('id', '')} {item.get('title', '')}")
    if state.get("verification_required_count"):
        lines.append(f"Unverified files: {state['verification_required_count']}. A syntax check alone does not verify behaviour.")
    if state.get("verification_required_count"):
        lines.append("Use verification_guidance to choose a proportional check. For bugs, reproduce before editing and rerun afterward; distinguish existing failures from regressions.")
    for item in (state.get("verification_required") or [])[-_CONTINUITY_VERIFY_PATHS:]:
        if isinstance(item, dict) and item.get("path"):
            lines.append(f"verification required: {item.get('path')} ({item.get('status', 'unchecked')}; changed via {item.get('via', 'edit')})")
    return "\n".join(line[:700] for line in lines if line.strip())


_SUMMARY_REQUEST_CHARS = 120000  # hard ceiling; live contexts use a smaller proportional source slice


_SUMMARY_MAX_OUTPUT_TOKENS = 4096


_COMPACTION_ORIGINAL_USER_CHARS = 1600


_COMPACTION_RECENT_USER_CHARS = 900


_COMPACTION_RECENT_USER_MESSAGES = 2


_COMPACTION_ANCHOR_MAX = 48


_COMPACTION_ANCHOR_CHARS = 2400


_COMPACTION_SENSITIVE_KEY_RE = re.compile(
    r"(?:password|passwd|pwd|secret|token|api[_-]?key|credential|authorization|"
    r"bearer|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)


_COMPACTION_KEY_VALUE_RE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9_.-]{2,48})\s*[:=]\s*([^\s,;]{1,180})"
)


_COMPACTION_WINDOWS_PATH_RE = re.compile(r"(?<!\w)[A-Za-z]:[\\/][^\s<>\"'|]{1,300}")


_COMPACTION_POSIX_PATH_RE = re.compile(
    r"(?<![\w:])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+"
)


_COMPACTION_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


_COMPACTION_SHA_RE = re.compile(r"\b(?=[0-9a-fA-F]{7,40}\b)(?=[0-9a-fA-F]*[a-fA-F])(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,40}\b")


_COMPACTION_ISSUE_RE = re.compile(r"\b(?:PR|issue)\s*#?\d+\b", re.IGNORECASE)


def _compaction_message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, list):
        content = "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "").strip()


def _compaction_excerpt(text: str, limit: int) -> str:
    text = re.sub(
        r"\[referenced-files\][\s\S]*?\[/referenced-files\]",
        "",
        str(text or ""),
        flags=re.IGNORECASE,
    ).strip()
    text = _COMPACTION_URL_RE.sub(
        lambda match: _safe_compaction_url(match.group(0)) or "[redacted URL]",
        text,
    )
    text = _COMPACTION_KEY_VALUE_RE.sub(
        lambda match: (
            f"{match.group(1)}=[redacted sensitive value]"
            if _COMPACTION_SENSITIVE_KEY_RE.search(match.group(1))
            else match.group(0)
        ),
        text,
    )
    if len(text) <= limit:
        return text
    marker = "\n...[exact middle omitted for context budget]...\n"
    half = max(1, (limit - len(marker)) // 2)
    return text[:half] + marker + text[-half:]


def _safe_compaction_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value.rstrip(".,;:)]}"))
        host = parsed.hostname or ""
        if not host:
            return ""
        port = f":{parsed.port}" if parsed.port else ""
        return urllib.parse.urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))[:320]
    except Exception:
        return ""


# Anchors are replayed into the prompt as harness-owned facts, so a false
# positive crowds out real identifiers. The key[:=]value heuristic matches
# every `opacity:.5` or `class="wrap` in code-heavy sessions, so the value has
# to look worth copying verbatim.
_ANCHOR_VALUE_JUNK_CHARS = "{}()[]<>\"'`;=,|$"


def _looks_like_anchor_value(value: str) -> bool:
    """True when a `key[:=]value` slice is a real identifier/path, not source code."""
    v = str(value or "").strip()
    if not 3 <= len(v) <= 180:
        return False
    if "\\" in v or any(ch in v for ch in _ANCHOR_VALUE_JUNK_CHARS):
        return False
    if "/*" in v or "*/" in v:
        return False
    # CSS lengths, opacities, counts: a number with an optional unit.
    if re.fullmatch(r"[+-]?\d*\.?\d+(px|em|rem|%|s|ms|vh|vw|fr|deg|pt|ch|ex)?", v):
        return False
    # CSS colours (`#111113`) and font-shorthand suffixes (`wght@400`) are
    # everywhere in a web session and mean nothing as an anchor.
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}\W*", v) or re.search(r"@\d", v):
        return False
    # Keep only shapes that carry copy-worthy signal: a path/host/scheme, a
    # fragment/ref, a dotted filename, or an ENV_STYLE_KEY with an underscore.
    return bool(re.search(r"[/:.#]", v) or re.fullmatch(r"[A-Z][A-Z0-9]*_[A-Z0-9_]+", v))


def _hex_is_color_or_uuid(text: str, match) -> bool:
    """True when a hex-looking token is really a CSS colour or a UUID slice.

    `fill:#0b0b0c` and `%23ff4d1c` are hex runs that satisfy the SHA pattern,
    and each UUID contributes two extra 8-char "hashes". Feeding those back as
    exact source anchors is pure noise.
    """
    start, end = match.span()
    before = text[max(0, start - 3):start].lower()
    # `#0b0b0c` (CSS) and `%230b0b0c` (the same colour percent-encoded).
    if before.endswith("#") or before.endswith("%23") or before.endswith("%"):
        return True
    after = text[end:end + 5].lower()
    if after.startswith("-") and re.match(r"^-[0-9a-f]{4}", after):
        return True  # 8-4-4-4-12 UUID: this run is the first group
    # UUID tail: `dee5e84b-77be-4d09-b660-0aef80901941` ends in a 12-hex run.
    return bool(re.match(r"[0-9a-f]{6}$", before)
                and text[max(0, start - 1):start] == "-")


def _extract_compaction_anchors(messages: list[dict]) -> list[str]:
    """Collect exact high-signal identifiers without asking the model to copy them."""
    found: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = str(value or "").strip().rstrip(".,;:)]}")[:320]
        if not value or value in seen:
            return
        seen.add(value)
        found.append(value)

    for message in messages:
        text = _compaction_message_text(message)
        if message.get("tool_calls"):
            text += "\n" + json.dumps(message.get("tool_calls"), ensure_ascii=False)
        url_matches = list(_COMPACTION_URL_RE.finditer(text))
        for match in _COMPACTION_KEY_VALUE_RE.finditer(text):
            key, value = match.group(1), match.group(2)
            if (key.lower() in {"http", "https"}
                    or _COMPACTION_SENSITIVE_KEY_RE.search(key)
                    or not _looks_like_anchor_value(value)):
                continue
            add(match.group(0))
        for match in _COMPACTION_WINDOWS_PATH_RE.finditer(text):
            add(match.group(0))
        for match in _COMPACTION_POSIX_PATH_RE.finditer(text):
            if not any(url.start() <= match.start() < url.end() for url in url_matches):
                add(match.group(0))
        for match in url_matches:
            add(_safe_compaction_url(match.group(0)))
        for pattern in (_COMPACTION_SHA_RE, _COMPACTION_ISSUE_RE):
            for match in pattern.finditer(text):
                if pattern is _COMPACTION_SHA_RE and _hex_is_color_or_uuid(text, match):
                    continue
                add(match.group(0))
    return found


def _anchor_is_worth_keeping(value: str) -> bool:
    """Revalidate a stored anchor against the current extraction rules.

    Anchors are carried forward on every fold, so junk accepted under older,
    looser rules would replay forever; rejecting it here lets a session clean
    itself up on its next fold. Single backslashes (real Windows paths) stay;
    escaped newlines, doubled backslashes and markup punctuation do not."""
    v = str(value or "").strip()
    if not v or v.startswith("..."):
        return False
    if any(ch in v for ch in "{}()<>\"'`|"):
        return False
    if any(token in v for token in ("\\n", "\\r", "\\t", "\\\\", "/*", "*/")):
        return False
    match = _COMPACTION_KEY_VALUE_RE.fullmatch(v)
    if match:
        return _looks_like_anchor_value(match.group(2))
    return True


def _bounded_compaction_anchors(existing: list, added: list) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for raw in list(existing or []) + list(added or []):
        value = str(raw or "").strip()[:320]
        if (not value or value in seen
                or not _anchor_is_worth_keeping(value)
                or _COMPACTION_SENSITIVE_KEY_RE.search(value.split("=", 1)[0])):
            continue
        seen.add(value)
        merged.append(value)
    if len(merged) > _COMPACTION_ANCHOR_MAX:
        merged = merged[:12] + merged[-(_COMPACTION_ANCHOR_MAX - 12):]
    while sum(len(item) + 3 for item in merged) > _COMPACTION_ANCHOR_CHARS and len(merged) > 1:
        # Retain the oldest original anchors and the newest working anchors.
        merged.pop(12 if len(merged) > 12 else 0)
    return merged


def _build_compaction_memory(chat: dict, folded_messages: list[dict]) -> dict:
    """Prepare exact evidence for a fold without mutating persisted chat state."""
    prior = chat.get("compaction_memory")
    prior = dict(prior) if isinstance(prior, dict) else {}
    original = str(prior.get("original_user") or "").strip()
    if not original:
        for message in chat.get("messages", []):
            if (message.get("role") == "user" and not message.get("invisible")
                    and not message.get("_synthetic")):
                original = _compaction_excerpt(
                    _compaction_message_text(message),
                    _COMPACTION_ORIGINAL_USER_CHARS,
                )
                if original:
                    break

    recent = [
        str(item).strip()
        for item in (prior.get("recent_user_messages") or [])
        if str(item).strip()
    ]
    for message in folded_messages:
        if (message.get("role") != "user" or message.get("invisible")
                or message.get("_synthetic")):
            continue
        text = _compaction_excerpt(
            _compaction_message_text(message),
            _COMPACTION_RECENT_USER_CHARS,
        )
        if text and not _TASK_ACK_RE.fullmatch(_task_anchor_text(text)):
            recent.append(text)
    deduped_recent: list[str] = []
    for text in recent:
        if text == original or text in deduped_recent:
            continue
        deduped_recent.append(text)
    recent = deduped_recent[-_COMPACTION_RECENT_USER_MESSAGES:]

    anchors = _bounded_compaction_anchors(
        prior.get("exact_anchors") or [],
        _extract_compaction_anchors(folded_messages),
    )
    return {
        "version": 1,
        "original_user": original,
        "recent_user_messages": recent,
        "exact_anchors": anchors,
        "updated": int(time.time()),
    }


def _compaction_render_limits(ctx_limit: int | None) -> tuple[int, int, int, int]:
    if ctx_limit and ctx_limit <= 8192:
        return 600, 400, 1, 800
    if ctx_limit and ctx_limit <= 16384:
        return 900, 600, 1, 1200
    if ctx_limit and ctx_limit <= 32768:
        return 1200, 700, 2, 1800
    return 1600, 900, 2, 2400


def _render_anchor_subset(anchors: list, char_budget: int) -> list[str]:
    values = [str(item).strip()[:320] for item in anchors if str(item).strip()]
    if not values:
        return []
    selected = values[:8] + [item for item in values[-24:] if item not in values[:8]]
    while sum(len(item) + 3 for item in selected) > char_budget and len(selected) > 1:
        selected.pop(8 if len(selected) > 8 else 0)
    return selected


def _render_compaction_memory(memory: dict, ctx_limit: int | None = None) -> str:
    if not isinstance(memory, dict):
        return ""
    original_chars, recent_chars, recent_count, anchor_chars = _compaction_render_limits(ctx_limit)
    sections: list[str] = []
    original = str(memory.get("original_user") or "").strip()
    if original:
        sections.append(
            "ORIGINAL USER INTENT (SENSITIVE VALUES REDACTED):\n"
            + _compaction_excerpt(original, original_chars)
        )
    recent = [str(item).strip() for item in (memory.get("recent_user_messages") or [])
              if str(item).strip()][-recent_count:]
    if recent:
        sections.append(
            "RECENT REAL USER INSTRUCTIONS (SENSITIVE VALUES REDACTED):\n"
            + "\n".join(f"- {_compaction_excerpt(item, recent_chars)}" for item in recent)
        )
    anchors = _render_anchor_subset(memory.get("exact_anchors") or [], anchor_chars)
    if anchors:
        sections.append(
            "EXACT SOURCE ANCHORS (DO NOT PARAPHRASE):\n"
            + "\n".join(f"- {item}" for item in anchors)
        )
    return "\n\n".join(sections)


def _summary_request_chars(ctx_limit: int | None) -> int:
    if not ctx_limit:
        return _SUMMARY_REQUEST_CHARS
    output_tokens = _summary_output_budget(ctx_limit)
    fit_cap = max(8000, (int(ctx_limit) - output_tokens - 2048) * 2)
    lean_target = max(12000, min(_SUMMARY_REQUEST_CHARS, int(ctx_limit * 0.75)))
    return min(fit_cap, lean_target)


_SUMMARY_KEEP_COMPLETED = 14     # progress bullets kept (latest)


_SUMMARY_KEEP_FILES = 12         # file paths kept (latest touched)


_SUMMARY_KEEP_CHECKS = 10        # command/test outcomes kept (latest)


_SUMMARY_SECTION_MAX_CHARS = 1600


_SUMMARY_BULLET_MAX_CHARS = 280


_SUMMARY_STALE_NEXT = " (carried from an earlier fold; check the latest messages before acting on it)"


_SUMMARY_REQUIRED_HEADINGS = (
    "## ACTIVE TASK:", "## COMPLETED:", "## ROADBLOCKS:",
    "## DECISIONS AND CONSTRAINTS:", "## FILES IN USE:",
    "## VERIFICATION:", "## NEXT ACTION:",
)


def _summary_output_budget(ctx_limit: int | None) -> int:
    """Leave small contexts usable while giving long-session summaries room."""
    if not ctx_limit:
        return _SUMMARY_MAX_OUTPUT_TOKENS
    return max(1536, min(_SUMMARY_MAX_OUTPUT_TOKENS, int(ctx_limit) // 8))


def _summary_request_prefix(old_summary: str, ctx_limit: int | None,
                            compaction_memory: dict | None) -> str:
    """PRIOR STATE + harness evidence block that leads every fold request.

    Only the sections the model rewrites go back to it, plus the latest few
    progress items so it does not repeat them. The lists the bridge keeps
    (progress history, files, checks) are never handed back for copying."""
    request_chars = _summary_request_chars(ctx_limit)
    prior_cap = max(2000, int(request_chars * 0.40))
    blocks: list[str] = []
    if old_summary:
        prior = _split_summary_sections(old_summary)
        if prior:
            state = "\n".join(
                f"{heading}\n" + _summary_cap_section(prior.get(heading) or "- None recorded.")
                for heading in ("## ACTIVE TASK:", "## ROADBLOCKS:",
                                "## DECISIONS AND CONSTRAINTS:", "## NEXT ACTION:"))
        else:
            # A summary from before the structured format: hand it over whole.
            state = old_summary
        if len(state) > prior_cap:
            head = prior_cap // 2
            tail = prior_cap - head - 64
            state = state[:head] + "\n...[middle of the prior state elided]...\n" + state[-tail:]
        blocks.append("PRIOR STATE:\n" + state)
        recorded = _summary_bullets(prior.get("## COMPLETED:", ""))[-6:] if prior else []
        if recorded:
            blocks.append("ALREADY RECORDED PROGRESS (latest; do not repeat):\n"
                          + "\n".join(f"- {item}" for item in recorded))
    evidence = _render_compaction_memory(compaction_memory or {}, ctx_limit=ctx_limit)
    if evidence:
        blocks.append(evidence)
    return "\n\n".join(blocks) + ("\n\n" if blocks else "")


_SUMMARY_WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "create_file", "append_file", "apply_patch",
    "remote_write_file", "notebook_edit",
})


_SUMMARY_READ_TOOLS = frozenset({"read_file", "remote_read_file"})


_SUMMARY_FILE_ARG_KEYS = ("path", "file_path", "filepath", "target_file")


_SUMMARY_CMD_ARG_KEYS = ("command", "code", "cmd", "script")


_SUMMARY_NOISE_PREFIXES = (
    "powershell_exit_code", "failure_guidance", "[stderr]", "[stdout]", "stdout:",
    "stderr:", "traceback (most recent call last)", "verification_next_step",
)


def _summary_norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _summary_bullets(body: str) -> list[str]:
    """A section body as a list of items. Each line is an item; an indented
    line continues the one above; placeholders like '- None recorded.' drop."""
    items: list[str] = []
    for line in str(body or "").splitlines():
        if not line.strip():
            continue
        text = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip()
        if not text or _SUMMARY_EMPTY_SECTION_RE.match(text.strip(" -*\t")):
            continue
        if line[:1] in (" ", "\t") and items and not re.match(r"^\s*(?:[-*•]|\d+[.)])\s", line):
            items[-1] += " " + text
        else:
            items.append(text)
    return items


def _merge_summary_bullets(prior: list[str], added: list[str], keep: int,
                           key=None) -> list[str]:
    """Prior items then added ones, deduplicated (a repeated item moves to the
    end, as the newest), cut to the latest `keep`, each item bounded."""
    keyfn = key or _summary_norm
    merged: dict = {}
    for item in list(prior or []) + list(added or []):
        item = str(item or "").strip()
        if not item:
            continue
        if len(item) > _SUMMARY_BULLET_MAX_CHARS:
            item = item[:_SUMMARY_BULLET_MAX_CHARS - 1].rstrip() + "…"
        k = keyfn(item)
        merged.pop(k, None)
        merged[k] = item
    return list(merged.values())[-keep:] if keep > 0 else []


def _summary_file_key(item: str) -> str:
    """Files dedupe by path: '<path> (written)' and '<path> (read)' are one."""
    return _summary_norm(str(item).rsplit(" (", 1)[0])


def _summary_cap_section(body: str) -> str:
    body = str(body or "").strip()
    if len(body) <= _SUMMARY_SECTION_MAX_CHARS:
        return body
    return body[:_SUMMARY_SECTION_MAX_CHARS - 1].rstrip() + "…"


def _summary_result_failed(text: str) -> bool:
    t = str(text or "").lstrip()
    m = re.match(r"\[exit (-?\d+)", t)
    if m:
        return m.group(1) != "0"
    if t.startswith("{"):
        try:
            obj = json.loads(t)
        except ValueError:
            return False
        if isinstance(obj, dict):
            return bool(obj.get("error")) or obj.get("ok") is False
    return False


def _summary_outcome(text: str) -> str:
    """'exit 0: <first output line>' or 'exit 1: KeyError: ...' for a result."""
    t = str(text or "").strip()
    if not t:
        return "no result recorded"
    head = ""
    lines = t.splitlines()
    m = re.match(r"\[exit (-?\d+)", t)
    if m:
        head = f"exit {m.group(1)}"
        lines = lines[1:]
    elif t.startswith("{"):
        try:
            obj = json.loads(t)
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            if obj.get("error"):
                return "error: " + _summary_clip(str(obj.get("error")), 160)
            if "ok" in obj:
                head = "ok" if obj.get("ok") else "failed"
            elif "exit_code" in obj:
                head = f"exit {obj.get('exit_code')}"
            lines = []
    detail = ""
    errors = [ln.strip() for ln in lines
              if re.match(r"^\s*[A-Za-z_][\w.]*(Error|Exception)\b", ln)]
    if errors:
        detail = errors[-1]
    else:
        for ln in lines:
            s = ln.strip()
            if s and not s.lower().startswith(_SUMMARY_NOISE_PREFIXES):
                detail = s
                break
    detail = _summary_clip(detail, 160)
    return (f"{head}: {detail}" if head and detail else head or detail or "done")


def _summary_cmd_digest(value) -> str:
    """The first line of a command or script that says what it does."""
    lines = [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]
    pick = ""
    for ln in lines:
        if not re.match(r"^(import |from \S+ import |#|//|sys\.path)", ln):
            pick = ln
            break
    pick = pick or (lines[0] if lines else "")
    more = " …" if len(lines) > 1 else ""
    return _summary_clip(pick, 120) + more


def _summary_clip(text: str, limit: int) -> str:
    """Redacted like every fold excerpt, but cut on one line (the excerpt's
    multi-line middle marker would split a bullet)."""
    text = re.sub(r"\s+", " ", _compaction_excerpt(str(text or ""), 10 ** 6)).strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _summary_tool_facts(slice_msgs: list[dict]) -> tuple[list[str], list[str]]:
    """Files touched and command/test outcomes in a folded slice, read from the
    tool calls and their results rather than from the model's prose."""
    results = {m.get("tool_call_id"): m for m in slice_msgs
               if m.get("role") == "tool" and m.get("tool_call_id")}
    files: list[str] = []
    checks: list[str] = []
    for m in slice_msgs:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            res = results.get(tc.get("id"))
            res_text = _compaction_message_text(res) if res else ""
            failed = _summary_result_failed(res_text)
            path = next((str(args[k]) for k in _SUMMARY_FILE_ARG_KEYS if args.get(k)), "")
            if path and (name in _SUMMARY_WRITE_TOOLS or name in _SUMMARY_READ_TOOLS):
                verb = "written" if name in _SUMMARY_WRITE_TOOLS else "read"
                if not (failed and verb == "read"):
                    files.append(f"{path[:240]} ({verb}{', failed' if failed else ''})")
            cmd = next((args[k] for k in _SUMMARY_CMD_ARG_KEYS if args.get(k)), None)
            if cmd is not None or name == "run_tests":
                digest = _summary_cmd_digest(cmd) if cmd is not None else ""
                checks.append(f"{name}: {digest} -> {_summary_outcome(res_text)}"
                              if digest else f"{name} -> {_summary_outcome(res_text)}")
    return files, checks


def _assemble_summary(prior: dict, model: dict | None,
                      files: list[str], checks: list[str]) -> str:
    """Join the model's sections with the bridge-kept lists into the stored
    seven-section note. A section the model did not write keeps its prior
    text; with no model output at all, the next action is marked as carried."""
    model = model or {}
    none = "- None recorded."

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or none

    def own(heading: str) -> str:
        if heading in model:
            return model[heading].strip() or none
        return (prior.get(heading) or "").strip() or none

    progress = model.get("## NEW PROGRESS:") or model.get("## COMPLETED:") or ""
    completed = _merge_summary_bullets(_summary_bullets(prior.get("## COMPLETED:", "")),
                                       _summary_bullets(progress), _SUMMARY_KEEP_COMPLETED)
    kept_files = _merge_summary_bullets(_summary_bullets(prior.get("## FILES IN USE:", "")),
                                        files, _SUMMARY_KEEP_FILES, key=_summary_file_key)
    kept_checks = _merge_summary_bullets(_summary_bullets(prior.get("## VERIFICATION:", "")),
                                         checks, _SUMMARY_KEEP_CHECKS)
    next_action = (model.get("## NEXT ACTION:") or "").strip()
    if not next_action:
        default = "- Continue the active task from the latest messages."
        carried = (prior.get("## NEXT ACTION:") or "").strip().replace(_SUMMARY_STALE_NEXT, "")
        next_action = (carried + _SUMMARY_STALE_NEXT) if carried and carried != default else default
    parts = (
        ("## ACTIVE TASK:", own("## ACTIVE TASK:")),
        ("## COMPLETED:", bullets(completed)),
        ("## ROADBLOCKS:", own("## ROADBLOCKS:")),
        ("## DECISIONS AND CONSTRAINTS:", own("## DECISIONS AND CONSTRAINTS:")),
        ("## FILES IN USE:", bullets(kept_files)),
        ("## VERIFICATION:", bullets(kept_checks)),
        ("## NEXT ACTION:", next_action),
    )
    return "\n".join(f"{heading}\n{_summary_cap_section(body)}" for heading, body in parts)


# ---- summary fidelity gate --------------------------------------------------
# After a fold writes the note, re-inject anything it silently dropped versus
# the prior note: a sticky section that went empty, or a load-bearing exact
# anchor that vanished. Add-only, so it cannot make a note worse.
# COMPLETED / ACTIVE TASK / VERIFICATION / NEXT ACTION are not sticky: items
# there legitimately fall away as work finishes.
_SUMMARY_STICKY_HEADINGS = (
    "## ROADBLOCKS:",
    "## DECISIONS AND CONSTRAINTS:",
    "## FILES IN USE:",
)


_SUMMARY_EMPTY_SECTION_RE = re.compile(r"^none(?:\s+recorded)?\.?$", re.IGNORECASE)


_FIDELITY_MAX_REINJECT_CHARS = 1200


def _split_summary_sections(text: str, headings: tuple = None) -> dict:
    """Map each required heading to its section body. A heading absent from the
    text maps to nothing. The note reaching the gate is already schema-valid
    (every heading present and ordered), but we stay robust to a missing one."""
    text = str(text or "")
    found = sorted(
        (text.find(h), h) for h in (headings or _SUMMARY_REQUIRED_HEADINGS) if text.find(h) >= 0
    )
    out: dict = {}
    for idx, (pos, heading) in enumerate(found):
        start = pos + len(heading)
        end = found[idx + 1][0] if idx + 1 < len(found) else len(text)
        out[heading] = text[start:end].strip()
    return out


def _section_is_empty(body: str) -> bool:
    """True for a blank body or a placeholder like '- None recorded.'"""
    lines = [ln.strip(" -*\t") for ln in str(body or "").splitlines() if ln.strip(" -*\t")]
    if not lines:
        return True
    return all(_SUMMARY_EMPTY_SECTION_RE.match(ln) for ln in lines)


def _reinject_under_heading(summary: str, heading: str, block: str) -> str:
    """Insert `block` immediately after `heading` (staying inside that section).
    Add-only: whatever the model wrote below is left untouched."""
    pos = summary.find(heading)
    if pos < 0:  # schema-valid notes always have it; append defensively if not
        return summary.rstrip() + "\n\n" + heading + "\n" + block
    after = pos + len(heading)
    return summary[:after] + "\n" + block + summary[after:]


def _reconcile_summary_fidelity(prior_summary: str, new_summary: str,
                                candidate_memory: dict | None,
                                pins: list | None) -> tuple[str, list]:
    """Deterministic, add-only fidelity gate between writing the fold note and
    committing it. Returns (possibly-repaired note, list of repair tags). Never
    raises for the caller's sake is the caller's job; this stays pure."""
    new_summary = str(new_summary or "")
    if not new_summary.strip():
        return new_summary, []
    prior_summary = str(prior_summary or "")
    repairs: list = []
    new_sections = _split_summary_sections(new_summary)
    prior_sections = _split_summary_sections(prior_summary) if prior_summary else {}

    # 1. Section regression: restore (bounded) a sticky section the prior note
    #    carried and the new note emptied, so the loss doesn't compound on the
    #    next fold.
    for heading in _SUMMARY_STICKY_HEADINGS:
        prior_body = prior_sections.get(heading, "")
        if _section_is_empty(prior_body):
            continue
        if not _section_is_empty(new_sections.get(heading, "")):
            continue
        block = prior_body.strip()[:_FIDELITY_MAX_REINJECT_CHARS].rstrip()
        block += "\n(restored from prior summary — the fold had dropped this)"
        new_summary = _reinject_under_heading(new_summary, heading, block)
        repairs.append("section:" + heading.strip("# :").lower().replace(" ", "_"))

    # 2. Anchor presence: restore load-bearing exact anchors the prior note had
    #    and the new note dropped. Exact substring test only, so no false misses;
    #    we repair loss, never invent.
    anchors = []
    if isinstance(candidate_memory, dict):
        anchors = _render_anchor_subset(candidate_memory.get("exact_anchors") or [],
                                        _FIDELITY_MAX_REINJECT_CHARS)
    low_new = new_summary.lower()
    low_prior = prior_summary.lower()
    missing, used = [], 0
    for item in anchors:
        value = str(item).strip()
        if len(value) < 4 or value.lower() in low_new:
            continue
        if prior_summary and value.lower() not in low_prior:
            continue
        if used + len(value) + 3 > _FIDELITY_MAX_REINJECT_CHARS:
            break
        missing.append(value)
        used += len(value) + 3
        if len(missing) >= 12:
            break
    if missing:
        block = "\n".join(f"- {value} (restored exact anchor)" for value in missing)
        new_summary = _reinject_under_heading(new_summary, "## FILES IN USE:", block)
        repairs.append(f"anchors:{len(missing)}")
    return new_summary, repairs
