# SPDX-License-Identifier: AGPL-3.0-only
"""Which red-team mission the current chat runs under, and whether it is
authorized or passive.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_rt_chat
from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats


def _rt_url_host(u: str) -> str:
    """Best-effort host[:port] from a url or bare host. '' on failure."""
    s = (u or "").strip()
    if not s:
        return ""
    try:
        if "://" not in s:
            s = "//" + s
        netloc = urllib.parse.urlsplit(s).netloc or ""
        return netloc or (u or "").strip()[:80]
    except Exception:
        return (u or "").strip()[:80]


def _rt_authorized_mission(chat: dict | None) -> dict | None:
    m = _rt_mission_get(chat) if isinstance(chat, dict) else None
    if not m or m.get("authorized") is not True:
        return None
    if str(m.get("status") or "active").lower() != "active":
        return None
    return m


def _rt_passive_mission(chat: dict | None) -> bool:
    mission = _rt_authorized_mission(chat)
    return bool(mission and mission.get("engagement") == "passive_osint")


def _rt_context_chat() -> dict | None:
    """Return the live turn state when present, otherwise the persisted chat."""
    live = _current_rt_chat.get()
    if isinstance(live, dict):
        return live
    try:
        chat_id = _current_chat_id.get() or _get_current_chat() or ""
        return ((get_chats().get("chats", {}) or {}).get(chat_id)
                if chat_id else None)
    except Exception:
        return None


def _rt_mission_get(chat: dict) -> dict | None:
    m = chat.get("mission")
    return m if isinstance(m, dict) and any(
        m.get(k) for k in ("target", "scope", "objective", "facts")) else None
