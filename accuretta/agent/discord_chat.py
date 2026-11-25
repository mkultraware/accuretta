# SPDX-License-Identifier: AGPL-3.0-only
"""Chatting through Discord: the bot connection, one Discord message as a chat turn,
splitting long answers and stripping thinking from them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import sys
import threading
import time
import traceback
from accuretta.agent.chat_index import _enforce_chat_retention
from accuretta.agent.chat_loop import run_chat_turn
from accuretta.agent.context_estimate import _is_gemma_model
from accuretta.agent.history_fold import _maybe_roll_summary
from accuretta.agent.history_fold import _summary_trigger_limit
from accuretta.agent.native_tools import _native_tools_broken
from accuretta.agent.native_tools import _text_tools_prompt
from accuretta.agent.rolling_summary import _log_compact
from accuretta.agent.system_prompt import build_system_prompt
from accuretta.agent.tool_bundles import _auto_unlock_bundles
from accuretta.agent.tool_bundles import _unlock_bundle
from accuretta.agent.turn_journal import _ACTIVITY_MAX
from accuretta.agent.turn_journal import _refresh_continuity_state
from accuretta.agent.turn_journal import _turn_journal_clear
from accuretta.agent.turn_records import _last_dropped_by_chat
from accuretta.agent.turn_records import _update_task_anchor
from accuretta.agent.user_anchor import _ensure_user_anchor
from accuretta.agent.verification import _set_current_chat
from accuretta.core.chats import get_chats
from accuretta.core.paths import CHATS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.core.visible_reply import _close_think_marker
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission_flow import _rt_mission_seed
from accuretta.runtime.llama_http import llama_ping
from accuretta.runtime.llama_process import _last_prompt_tokens_by_chat
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.model_observation import _client_context_by_chat
from accuretta.security.approvals import decide_approval
from accuretta.security.desktop_intent import _user_asked_desktop_interaction
from accuretta.security.user_words import _note_user_words
from accuretta.tools.discord_approvals import _discord_approval_listener
from accuretta.tools.discord_approvals import _discord_approvals
from accuretta.tools.discord_approvals import _discord_approvals_lock
from accuretta.tools.discord_approvals import _discord_state


# ============================================================
# DISCORD REMOTE BRIDGE (optional)
# DM accuretta from your phone, with native push notifications, and approve
# risky actions by reacting. Connects OUTBOUND to Discord (no port forwarding,
# no inbound exposure — your PC stays closed). Locked to ONE owner Discord user
# id; everyone else is ignored. Requires: pip install discord.py
# ============================================================
DISCORD_CHAT_ID = "discord"


def _discord_split(text: str, limit: int = 1900) -> list[str]:
    """Split a reply into Discord's 2000-char message limit (with headroom)."""
    text = (text or "").strip()
    if not text:
        return ["(empty reply)"]
    out = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out


def _discord_strip_think(text: str) -> str:
    """Discord-only display filter: return the visible answer, hiding reasoning.

    Used only on the Discord reply path (_run_discord_turn) and mirrors the web
    UI's splitThinking boundary (app.js). Chats saved by older builds separate
    reasoning with a bare " response" marker and no closing </think>; that form
    is closed first, so an answer line such as `ResponseEntity<T>` is never
    taken for the boundary."""
    buf = _close_think_marker(text or "")
    if not buf:
        return ""
    # 1. Strip closed reasoning blocks, preserving surrounding prose.
    cleaned = buf
    cleaned = re.sub(r"<\s*think\s*>[\s\S]*?<\s*/\s*think\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"<\s*(?:thinking|reasoning)\s*>[\s\S]*?<\s*/\s*(?:thinking|reasoning)\s*>",
        "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"<\|\s*thinking\s*\|>[\s\S]*?<\|\s*/\s*thinking\s*\|>",
        "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(
        r"\[(?:thought|thinking|reasoning|scratchpad)\][\s\S]*?\[/(?:thought|thinking|reasoning|scratchpad)\]",
        "", cleaned, flags=re.IGNORECASE)
    if cleaned != buf:
        cleaned = re.sub(
            r"</?(?:think|thinking|reasoning)>|<\|\/?thinking\|>|\[/?(?:thought|thinking|reasoning|scratchpad)\]",
            "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()
    # 2. Bare closer with no opener, e.g. "planning</think>Answer".
    _closes = list(re.finditer(
        r"<\s*/\s*(?:think|thinking|reasoning)\s*>|<\|\s*/\s*thinking\s*\|>"
        r"|\[/\s*(?:thought|thinking|reasoning|scratchpad)\s*\]",
        buf, flags=re.IGNORECASE))
    if _closes:
        tail = buf[_closes[-1].end():]
        tail = re.sub(
            r"</?(?:think|thinking|reasoning)>|<\|\/?thinking\|>|\[/?(?:thought|thinking|reasoning|scratchpad)\]",
            "", tail, flags=re.IGNORECASE)
        return tail.strip()
    # 3. Unclosed opener (in-flight thinking, no answer yet) -> prefix only.
    _open = re.search(
        r"<\s*(?:think|thinking|reasoning)\s*>|<\|\s*thinking\s*\|>"
        r"|\[(?:thought|thinking|reasoning|scratchpad)\]",
        buf, flags=re.IGNORECASE)
    if _open:
        return buf[:_open.start()].strip()
    return buf.strip()


_DISCORD_FRIEND_PROMPT = (
    "you are accuretta, a local AI running on the owner's machine, chatting casually in a discord "
    "server with the owner's friends. keep replies short and friendly. you have NO tools: you cannot "
    "read, write, or delete files, run commands, scan anything, or touch the machine in any way. if "
    "someone asks you to do work like that, tell them only the owner can ask you to do real work."
)


def _run_discord_turn(user_text: str, chat_id: str, use_tools: bool) -> str:
    """Run one Discord turn. The owner (use_tools=True) gets the full agent with
    tools; everyone else gets a locked chat-only turn that cannot touch the
    machine. Tool approvals fire on the event bus and mirror to Discord. Returns
    the visible reply. Never raises."""
    try:
        if not llama_ping(timeout=2.0):
            return "im offline right now"
        chats = get_chats()
        chat = chats["chats"].setdefault(chat_id, {
            "id": chat_id, "title": chat_id, "messages": [],
            "created": int(time.time()),
        })
        chat.setdefault("messages", [])
        chat["messages"].append({"role": "user", "content": user_text})
        STATE.last_user_text = user_text
        _note_user_words(chat_id, user_text)
        discord_ctx = dict(_client_context_by_chat.get(chat_id) or {})
        discord_ctx.setdefault("execution_target", "host")
        discord_ctx["_user_text"] = user_text
        _client_context_by_chat[chat_id] = discord_ctx
        _update_task_anchor(chat, user_text)
        _enforce_chat_retention(chat)
        chat["updated"] = int(time.time())
        save_json(CHATS_FILE, chats)
        if (use_tools and get_settings().get("desktop_enabled")
                and _user_asked_desktop_interaction(user_text)):
            _unlock_bundle(chat_id, "desktop")
        if use_tools:
            try:
                _auto_unlock_bundles(chat_id, user_text)
            except Exception as _bundle_error:
                print(f"[tools] bundle auto-load failed: {_bundle_error}", file=sys.stderr)

        replay_from = 0
        # Gemma over discord gets the same text-tools routing as the web UI:
        # llama-server can't parse its dialect, so keep tools in the prompt but
        # off the wire and let extract_tool_calls handle the tool_code shape.
        _gm = any(_is_gemma_model(m) for m in
                  (get_settings().get("model"), get_settings().get("model_path"), _llama.loaded_model()))
        _text_tools = _gm or _native_tools_broken()

        if use_tools:
            # Owner path: same long-session memory as the web UI (token-aware rolling
            # summary + pins, replaying only the un-folded tail).
            ctx_limit = _llama_props_ctx() or int(get_settings().get("num_ctx") or 32768)
            if get_settings().get("summarize_history", True):
                try:
                    _drop_force = _last_dropped_by_chat.get(chat_id, False)
                    if _drop_force:
                        # Same gate as the web pre-turn: a mid-turn fold usually recovered the
                        # overflow already, so don't force-fold the next message at low fill.
                        _fill_now = _last_prompt_tokens_by_chat.get(chat_id, 0)
                        if _fill_now > 0 and _fill_now < _summary_trigger_limit(ctx_limit) * 0.7:
                            _drop_force = False
                    _log_compact(chat_id, f"pre_turn discord: fill={_last_prompt_tokens_by_chat.get(chat_id, 0)} "
                                          f"trigger={_summary_trigger_limit(ctx_limit)} ctx={ctx_limit} "
                                          f"drop_flag={_last_dropped_by_chat.get(chat_id, False)} force={_drop_force}")
                    if _maybe_roll_summary(chat, ctx_limit,
                                           force=_drop_force,
                                           reason_hint="pre_turn"):
                        save_json(CHATS_FILE, chats)
                except Exception:
                    traceback.print_exc()
            if get_settings().get("red_team_enabled") and get_settings().get("rt_mission_state", True):
                try:
                    if _rt_authorized_mission(chat) and _rt_mission_seed(chat, user_text):
                        save_json(CHATS_FILE, chats)
                except Exception:
                    traceback.print_exc()
            _set_current_chat(chat_id)
            system_prompt = build_system_prompt(include_tools=True, chat_mode="agent",
                                                include_tool_list=_text_tools)
            # Mission block + pins are not spliced here; run_chat_turn appends them after
            # the history (see its volatile-tail block). Only the rolling summary and the
            # static pin_note nudge stay in the system prompt.
            _roll = chat.get("rolling_summary", "")
            if _roll:
                system_prompt += ("\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; "
                                  "treat as established facts — do not redo work or re-ask) ===\n" + _roll)
            system_prompt += ("\n\nWhen you fix a bug, settle a design decision, or the user states a constraint, "
                              "call pin_note to lock it in for this session so you never undo it or repeat the mistake.")
            if _text_tools:
                system_prompt += _text_tools_prompt(_gm)
            replay_from = int(chat.get("summary_through", 0) or 0)
        else:
            system_prompt = _DISCORD_FRIEND_PROMPT
        msgs = [{"role": "system", "content": system_prompt}]
        for m in chat["messages"][replay_from:]:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            out = {"role": role, "content": m.get("content", "") or ""}
            if role == "assistant" and m.get("tool_calls"):
                out["tool_calls"] = m["tool_calls"]
            if role == "tool":
                if m.get("tool_call_id"):
                    out["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    out["name"] = m["name"]
            msgs.append(out)
        msgs = _ensure_user_anchor(msgs, chat["messages"], replay_from)

        final = run_chat_turn(chat_id, msgs, use_tools=use_tools, emit=lambda e: None,
                              native_tools=not _text_tools)
        if not final:
            return "(no reply — stopped or empty)"

        # persist intermediate tool rounds + the final assistant message so the
        # next message has continuity.
        chats = get_chats()
        chat = chats["chats"].get(chat_id)
        if chat is not None:
            _completed_turn_id = str(final.pop("_turn_id", "") or "")
            _mu = final.get("_mission_updates")
            if isinstance(_mu, dict):
                chat["mission"] = _mu
            _au = final.get("_activity_updates")
            if isinstance(_au, list):
                chat["activity"] = [a for a in _au if isinstance(a, dict)][-_ACTIVITY_MAX:]
            _vu = final.get("_verification_updates")
            if isinstance(_vu, list):
                chat["verification_debt"] = [x for x in _vu if isinstance(x, dict)]
            _cu = final.get("_continuity_updates")
            if isinstance(_cu, dict):
                chat["continuity"] = _cu
            else:
                _refresh_continuity_state(chat)
            for im in (final.get("_appended_intermediate") or []):
                if _completed_turn_id and isinstance(im, dict):
                    im = {**im, "_turn_id": _completed_turn_id}
                chat["messages"].append(im)
            amsg = {"role": "assistant", "content": final.get("content", "")}
            if _completed_turn_id:
                amsg["_turn_id"] = _completed_turn_id
            if final.get("tool_calls"):
                amsg["tool_calls"] = final["tool_calls"]
            chat["messages"].append(amsg)
            chat["updated"] = int(time.time())
            save_json(CHATS_FILE, chats)
            _turn_journal_clear(chat_id, _completed_turn_id)
        return _discord_strip_think(final.get("content", "")) or "(done)"
    except Exception as e:
        traceback.print_exc()
        return f"error: {e}"


def _discord_start() -> None:
    """Run the Discord bot (blocking — call in a daemon thread). Outbound only."""
    s = get_settings()
    token = (s.get("discord_bot_token") or "").strip()
    owner_id = str(s.get("discord_owner_id") or "").strip()
    if not token or not owner_id:
        print("[discord] enabled but bot token / owner id missing — not starting.", flush=True)
        return
    try:
        import discord
    except Exception:
        print("[discord] discord.py not installed. run: pip install discord.py", flush=True)
        return

    intents = discord.Intents.default()
    intents.message_content = True  # requires Message Content Intent in the dev portal
    intents.reactions = True        # ✅/❌ approval taps (in default already; explicit to be sure)
    client = discord.Client(intents=intents)
    _discord_state["client"] = client
    _discord_state["running"] = True
    _discord_state["approval_listener_started"] = False   # reset per bot start

    @client.event
    async def on_ready():
        _discord_state["loop"] = client.loop
        print(f"[discord] connected as {client.user}. obeying owner id {owner_id}.", flush=True)
        # on_ready fires on every reconnect/resume; start the approval listener once
        # per bot run, or every approval gets DM'd once per reconnect.
        if not _discord_state.get("approval_listener_started"):
            _discord_state["approval_listener_started"] = True
            threading.Thread(target=_discord_approval_listener, args=(client,), daemon=True).start()

    @client.event
    async def on_message(message):
        try:
            if client.user and message.author.id == client.user.id:
                return
            is_owner = str(message.author.id) == owner_id
            is_dm = message.guild is None
            mentioned = bool(client.user) and client.user in getattr(message, "mentions", [])
            # DMs are always handled; in a server channel only respond when the
            # bot is actually @mentioned (so it ignores normal chatter).
            if not is_dm and not mentioned:
                return
            print(f"[discord] incoming author={message.author.id} owner={owner_id!r} "
                  f"is_owner={is_owner} is_dm={is_dm} mentioned={mentioned} "
                  f"content={(message.content or '')[:40]!r}", flush=True)
            text = (message.content or "").strip()
            if client.user:
                text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
            if not text:
                return
            # owner => full agent with tools; everyone else => locked chat-only,
            # in their own per-user chat so friends never touch the owner's
            # context (or the machine).
            use_tools = is_owner
            chat_id = DISCORD_CHAT_ID if is_owner else f"discord-{message.author.id}"
            async with message.channel.typing():
                reply = await client.loop.run_in_executor(
                    None, _run_discord_turn, text, chat_id, use_tools)
            for chunk in _discord_split(reply):
                await message.channel.send(chunk)
        except Exception as e:
            print(f"[discord] on_message error: {e}", flush=True)

    @client.event
    async def on_raw_reaction_add(payload):
        # raw event fires for EVERY reaction regardless of message cache —
        # on_reaction_add silently missed approval taps (uncached DM messages).
        try:
            if client.user and payload.user_id == client.user.id:
                return  # our own ✅/❌ seeding on the card
            emoji = str(payload.emoji)
            # accept the exact card emojis plus common near-misses a phone
            # keyboard offers (✔️ ☑️ ❎ ✖️) — variation selectors don't matter
            is_yes = any(mark in emoji for mark in ("✅", "✔", "☑"))
            is_no = any(mark in emoji for mark in ("❌", "❎", "✖"))
            if str(payload.user_id) != owner_id:
                # Log ignored reactions so a reaction from a non-owner account isn't a silent
                # dead end.
                print(f"[discord] reaction ignored: user {payload.user_id} is not "
                      f"the configured owner {owner_id} (emoji={emoji!r})", flush=True)
                return
            if not (is_yes or is_no):
                print(f"[discord] reaction ignored: {emoji!r} is not an approve/deny "
                      f"emoji — tap ✅ or ❌ on the approval card", flush=True)
                return
            with _discord_approvals_lock:
                aid = _discord_approvals.get(payload.message_id)
            # message_author_id is on the raw payload (discord.py 2.4+); lets us
            # tell "stale approval prompt of ours" from "some other message".
            own_msg = bool(client.user) and getattr(payload, "message_author_id", None) == client.user.id
            try:
                print(f"[discord] reaction user={payload.user_id} msg={payload.message_id} "
                      f"emoji={emoji!r} matched_approval={aid} own_msg={own_msg}", flush=True)
            except Exception:
                pass  # a hostile console must never kill the decision below

            async def _reply(text):
                try:
                    owner = await client.fetch_user(int(owner_id))
                    ch = owner.dm_channel or await owner.create_dm()
                    await ch.send(text)
                except Exception as _e:
                    print(f"[discord] reaction reply failed: {_e}", flush=True)

            if not aid:
                # No live approval for this message: only reply if it is clearly one of our
                # own stale prompts, so a restart or timeout isn't a silent dead end.
                if own_msg:
                    await _reply("that approval is no longer active — the bridge restarted or it timed out. "
                                 "re-run the action to get a fresh prompt.")
                return
            ok = decide_approval(aid, "approve" if is_yes else "deny")
            with _discord_approvals_lock:
                _discord_approvals.pop(payload.message_id, None)
            await _reply(("✅ approved — continuing." if is_yes else "❌ denied.") if ok
                         else "that approval already resolved or expired.")
        except Exception as e:
            print(f"[discord] reaction error: {e}", flush=True)

    try:
        client.run(token, log_handler=None)
    except Exception as e:
        print(f"[discord] bot stopped: {e}", flush=True)
    finally:
        _discord_state["running"] = False
