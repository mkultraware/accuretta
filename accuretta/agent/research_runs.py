# SPDX-License-Identifier: AGPL-3.0-only
"""Deep-research runs per chat, and the research context a chat's turn carries.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.chats import _get_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from deep_research import ResearchStore


_research_runs: dict[str, str] = {}


# ---- lazy tool bundles -------------------------------------------------------
# The full active suite is ~100 schemas, and llama-server's Jinja template
# inlines the whole tools array into the system message (6-10K tokens per round),
# most of it unused on any given turn. So: a small always-on core plus
# demand-loaded bundles. The model calls `list_more_tools` (always in core) to
# see and load what it needs; a loaded bundle stays for the rest of that chat.
# Hiding only saves prompt tokens: invoke_tool still runs any TOOLS name.
def _research_context(chat_id: str = "") -> tuple[str, str]:
    chat_id = chat_id or _get_current_chat()
    if not chat_id:
        return "", ""
    if chat_id in _research_runs:
        return chat_id, _research_runs[chat_id]
    chat = (get_chats().get("chats") or {}).get(chat_id) or {}
    return chat_id, str(chat.get("research_id") or "") if chat.get("last_mode") == "research" else ""


_research_store = ResearchStore(DATA / "research")
