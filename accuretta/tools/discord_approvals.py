# SPDX-License-Identifier: AGPL-3.0-only
"""Approvals over Discord: telling the owner when an approval waits too long, and taking
the answer from a DM.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
from accuretta.core.events import subscribe
from accuretta.core.events import unsubscribe
from accuretta.core.settings import get_settings
from accuretta.security.approval_list import list_approvals
from presence_notify import PresenceNotify


# Presence + external-notification routing. Approval DMs and notable security
# alerts route through this (suppressed while the user looks active, escalated
# otherwise); the web UI feeds it via /api/ui/presence heartbeats.
_presence_notify = PresenceNotify()


_discord_state: dict = {"client": None, "loop": None, "running": False}


_discord_approvals: dict = {}          # discord message id -> approval id


_discord_approvals_lock = threading.Lock()


# Shorter than the shortest approval timeout (300 s) by a margin that leaves
# time to react from a phone.
_APPROVAL_DM_AFTER_S = 120.0


def _discord_approval_listener(client) -> None:
    """Subscribe to the event bus and mirror every approval:new to the owner's
    DM as a ✅/❌ reaction prompt. A reaction resolves the same approval the
    browser would — first responder wins."""
    import asyncio
    owner_id = str(get_settings().get("discord_owner_id") or "").strip()
    q, _snap = subscribe()

    async def post_approval(entry):
        try:
            owner = await client.fetch_user(int(owner_id))
            ch = owner.dm_channel or await owner.create_dm()
            title = entry.get("title", "approval")
            cmd = str(entry.get("command", ""))[:1500]
            msg = await ch.send(f"approve: **{title}**\n```\n{cmd}\n```\nreact ✅ approve · ❌ deny")
            with _discord_approvals_lock:
                _discord_approvals[msg.id] = entry.get("id")
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            return True
        except Exception as e:
            print(f"[discord] approval post failed: {e}", flush=True)
            # Only a card that never posted is a failure the escalator should
            # retry; a posted card with a missing reaction still works.
            with _discord_approvals_lock:
                return entry.get("id") in _discord_approvals.values()

    while _discord_state.get("running"):
        try:
            evt = q.get(timeout=1.0)
        except Exception:
            continue
        if isinstance(evt, dict) and evt.get("type") == "approval:new":
            loop = _discord_state.get("loop")
            entry = evt.get("approval") or {}
            if loop and entry:
                approval_id = str(entry.get("id") or "")

                def _send_approval_dm(e=entry) -> bool:
                    try:
                        return bool(asyncio.run_coroutine_threadsafe(post_approval(e), loop).result(timeout=30))
                    except Exception as err:
                        print(f"[discord] approval post failed: {err}", flush=True)
                        return False

                # Suppress-then-escalate: held while the user looks active, delivered as soon
                # as they look absent, or after two minutes still unanswered. Approvals expire
                # (and deny) after 5-10 minutes, so the DM has to land with time left to react;
                # answering in-app first cancels it. The pending approval is durable bridge
                # state, so a held DM can never lose a gate.
                _presence_notify.post_external(
                    key=f"approval:{approval_id}",
                    kind="approval",
                    title=str(entry.get("title") or "approval"),
                    body=str(entry.get("command") or "")[:1500],
                    send=_send_approval_dm,
                    resolved=lambda aid=approval_id: aid and all(
                        str(a.get("id")) != aid for a in list_approvals()
                    ),
                    escalate_after_s=_APPROVAL_DM_AFTER_S,
                )
        elif isinstance(evt, dict) and evt.get("type") == "approval:decided":
            _presence_notify.resolve(f"approval:{evt.get('id')}")
    unsubscribe(q)
