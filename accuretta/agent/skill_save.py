# SPDX-License-Identifier: AGPL-3.0-only
"""The save_skill tool: turning what worked into a reusable skill file.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from accuretta.agent.skills import SKILLS_DIR
from accuretta.agent.skills import SKILL_BODY_CAP_CHARS
from accuretta.agent.skills import _FRONTMATTER_RE
from accuretta.agent.skills import _SKILLS_CACHE
from accuretta.agent.skills import _SKILL_NAME_RE
from accuretta.agent.skills import _parse_skill_frontmatter
from accuretta.agent.skills import _scan_skills
from accuretta.agent.token_budget import _approx_tokens
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.events import broadcast_event
from accuretta.core.undo import _undo_snapshot
from accuretta.core.undo import _undo_store
from accuretta.core.workspace import normalize_path
from accuretta.runtime.llama_http import _llama_tokenize
from accuretta.security.approvals import request_approval
from accuretta.security.path_guards import is_in_workspace
from pathlib import Path


def _skill_slug(value: str) -> str:
    """Turn a user-facing title into a safe skill name and filename stem."""
    raw = re.sub(r"(?i)\.(?:md|markdown|mdown|mkd)$", "", str(value or "").strip())
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw)
    slug = re.sub(r"[-_.]{2,}", "-", slug).strip("-._").lower()
    return slug[:80].rstrip("-._")


def _skill_description(value: str, body: str, name: str) -> str:
    description = " ".join(str(value or "").split())
    if not description:
        for raw in body.splitlines():
            line = re.sub(r"^[#>*+\-\d.()\s]+", "", raw).strip()
            if line:
                description = f"Use when the user asks to follow this procedure: {line}"
                break
    return (description or f"Use when the user asks to follow the {name} procedure.")[:220]


def _skill_token_budget(body: str) -> tuple[int, str]:
    """Count the loaded body with llama.cpp's tokenizer, with the normal fallback."""
    exact = _llama_tokenize(body)
    if exact is not None:
        return max(1, int(exact)), "model_tokenizer"
    return max(1, int(math.ceil(_approx_tokens(body)))), "conservative_estimate"


def tool_save_skill(args: dict) -> dict:
    """Create an Accuretta skill from pasted Markdown or a workspace .md file."""
    supplied = args.get("content")
    raw_source_path = str(args.get("source_path") or "").strip()
    source_path = normalize_path(raw_source_path) if raw_source_path else ""
    if supplied is not None and source_path:
        return {"error": "provide either content or source_path, not both"}
    if supplied is None and not source_path:
        return {"error": "provide pasted Markdown in content or a .md source_path"}

    source_name = ""
    if source_path:
        source = Path(source_path)
        if source.suffix.lower() not in {".md", ".markdown", ".mdown", ".mkd"}:
            return {"error": "source_path must be a Markdown file"}
        if not is_in_workspace(source_path):
            return {"error": "source_path is outside the configured workspace"}
        if not source.is_file():
            return {"error": f"Markdown source not found: {source_path}"}
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"error": f"could not read Markdown source: {exc}"}
        source_name = source.stem
    elif isinstance(supplied, str):
        text = supplied
    else:
        return {"error": "content must be Markdown text"}

    text = text.lstrip("\ufeff")
    match = _FRONTMATTER_RE.match(text)
    frontmatter = _parse_skill_frontmatter(text)
    body = (text[match.end():] if match else text).strip()
    if not body:
        return {"error": "refused: the skill body is empty"}
    if len(body) > SKILL_BODY_CAP_CHARS:
        return {"error": (f"skill body is {len(body)} chars, over the "
                          f"{SKILL_BODY_CAP_CHARS}-char load cap; shorten it first")}

    requested_name = str(args.get("name") or frontmatter.get("name") or source_name).strip()
    name = _skill_slug(requested_name)
    if not name or not _SKILL_NAME_RE.fullmatch(name):
        return {"error": "a skill name is required; use letters, numbers, dots, underscores, or hyphens"}
    description = _skill_description(
        str(args.get("description") or frontmatter.get("description") or ""), body, name)
    budget, budget_source = _skill_token_budget(body)

    destination = SKILLS_DIR / f"{name}.md"
    overwrite = bool(args.get("overwrite"))
    existed = destination.exists()
    if existed and not overwrite:
        return {"error": (f"skill '{name}' already exists; pass overwrite=true only if the user "
                          "asked to replace it"),
                "path": str(destination)}

    rendered = ("---\n"
                f"name: {json.dumps(name, ensure_ascii=False)}\n"
                f"description: {json.dumps(description, ensure_ascii=False)}\n"
                f"budget: {budget}\n"
                "---\n\n"
                f"{body}\n")
    chat_id = _get_current_chat() or ""
    call_id = _current_tool_call_id.get()
    approval = request_approval(
        title="Save skill",
        command=f'Save Accuretta skill "{name}" to "{destination}"',
        details={"kind": "save_skill", "path": str(destination),
                 "bytes": len(rendered.encode("utf-8")), "budget": budget},
    )
    if approval.get("decision") != "approve":
        try:
            broadcast_event({"type": "skill:save_finished", "chat_id": chat_id,
                             "call_id": call_id, "skill": name, "outcome": "cancelled"})
        except Exception:
            pass
        return {"error": f"user denied skill save ({approval.get('status')})"}

    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        broadcast_event({"type": "skill:saving", "chat_id": chat_id,
                         "call_id": call_id, "skill": name})
    except Exception:
        pass
    try:
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        _undo_snapshot(str(destination))
        temp_path.write_text(rendered, encoding="utf-8")
        os.replace(temp_path, destination)
        _undo_store.checkpoint(str(destination))
        _SKILLS_CACHE.clear()
        saved_entry = _scan_skills().get(name)
        if not saved_entry or saved_entry.get("budget") != budget:
            raise ValueError("saved file did not pass the skill catalog validation")
        broadcast_event({"type": "skills:update", "skill": name})
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            broadcast_event({"type": "skill:save_finished", "chat_id": chat_id,
                             "call_id": call_id, "skill": name, "outcome": "failed"})
        except Exception:
            pass
        return {"error": f"could not save skill: {exc}"}

    try:
        broadcast_event({"type": "skill:save_finished", "chat_id": chat_id,
                         "call_id": call_id, "skill": name,
                         "outcome": "saved", "budget": budget})
    except Exception:
        pass

    return {
        "ok": True,
        "skill": name,
        "description": description,
        "budget": budget,
        "budget_source": budget_source,
        "body_chars": len(body),
        "path": str(destination),
        "overwritten": existed,
        "note": f"saved '{name}' and refreshed the skill catalog",
    }
