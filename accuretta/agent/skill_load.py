# SPDX-License-Identifier: AGPL-3.0-only
"""The load_skill tool and the chat's active skill.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.agent.skills import SKILL_BODY_CAP_CHARS
from accuretta.agent.skills import _read_skill_body
from accuretta.agent.skills import _scan_skills
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.events import broadcast_event
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json


def _set_active_skill(chat_id: str, skill_name: str | None, budget: int = 0):
    """Persist the user-chosen active skill on the chat record (survives chat
    reloads; serialized like fold_mark/summary_through). Swapping replaces."""
    if not chat_id:
        return
    try:
        chats = get_chats()
        chat = (chats.get("chats") or {}).get(chat_id)
        if chat is None:
            return
        if skill_name:
            chat["active_skill"] = skill_name
            chat["active_skill_budget"] = max(0, int(budget or 0))
        else:
            chat.pop("active_skill", None)
            chat.pop("active_skill_budget", None)
        save_json(CHATS_FILE, chats)
    except Exception:
        pass


def tool_load_skill(args: dict) -> dict:
    """Load a user-curated skill (markdown procedure from skills/) into this
    chat. One active slot per chat — loading replaces the previous skill."""
    name = str(args.get("name") or "").strip()
    chat_id = str(args.get("chat_id") or "") or (_get_current_chat() or "")
    catalog = _scan_skills()
    if not name:
        _set_active_skill(chat_id, None)
        try:
            broadcast_event({"type": "skill:cleared", "chat_id": chat_id})
        except Exception:
            pass
        return {"skill": None, "active": False,
                "note": "active skill cleared"}
    entry = catalog.get(name)
    if entry is None:
        return {"error": f"unknown skill '{name}' — available: {sorted(catalog)}",
                "available": sorted(catalog)}
    body = _read_skill_body(entry)
    if body is None:
        return {"error": f"skill '{name}' could not be read from disk"}
    if len(body) > SKILL_BODY_CAP_CHARS:
        return {"error": f"skill '{name}' body is {len(body)} chars — over the "
                         f"{SKILL_BODY_CAP_CHARS}-char load cap; trim the file"}
    replaced = None
    if chat_id:
        try:
            prev = (get_chats().get("chats") or {}).get(chat_id, {}).get("active_skill")
            if prev and prev != name:
                replaced = prev
        except Exception:
            pass
    _set_active_skill(chat_id, name, budget=entry["budget"])
    try:
        ev = {"type": "skill:loaded", "chat_id": chat_id, "skill": name,
              "budget": entry["budget"], "body_chars": entry["body_chars"]}
        if replaced:
            ev["replaced"] = replaced
        broadcast_event(ev)
    except Exception:
        pass
    result: dict = {"skill": name, "active": True,
                    "description": entry["description"],
                    "budget": entry["budget"],
                    "body_chars": entry["body_chars"],
                    "content": body}
    if replaced:
        result["replaced"] = replaced
        result["note"] = f"loaded '{name}', replacing the previously active skill '{replaced}'"
    return result
