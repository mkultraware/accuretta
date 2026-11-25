# SPDX-License-Identifier: AGPL-3.0-only
"""Feeding the Security overview: recent agent actions, commands and processes, the
admin check, the scheduled scan and its alerts.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from accuretta.core.events import broadcast_event
from accuretta.core.paths import DATA
from accuretta.core.settings import get_settings
from accuretta.core.tool_stream import _read_cmd_history
from accuretta.redteam.mission_logs import ACTION_AUDIT_FILE
from accuretta.redteam.mission_logs import _ACTION_AUDIT_LOCK
from accuretta.tools.discord_approvals import _discord_state
from accuretta.tools.discord_approvals import _presence_notify
from accuretta.tools.host_forensics import tool_network_snapshot
from accuretta.tools.host_forensics import tool_parse_event_logs
from accuretta.tools.host_forensics import tool_persistence_hunt
from accuretta.tools.powershell import _run_powershell
from accuretta.tools.security_model import _security_model_busy
from accuretta.tools.security_model import _security_model_summary
from security_overview import SecurityOverviewService


def _security_collect_actions() -> dict:
    entries: list[dict] = []
    if not ACTION_AUDIT_FILE.exists():
        return {"entries": entries}
    try:
        with _ACTION_AUDIT_LOCK:
            with open(ACTION_AUDIT_FILE, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-200:]
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    entries.append({
                        "t": int(row.get("t") or 0),
                        "tool": str(row.get("tool") or "")[:100],
                        "status": str(row.get("status") or "")[:40],
                        "target": str(row.get("target") or "")[:240],
                        "args_sha256": str(row.get("args_sha256") or "")[:64],
                        "result_sha256": str(row.get("result_sha256") or "")[:64],
                    })
            except Exception:
                continue
    except Exception as exc:
        return {"entries": entries, "error": str(exc)[:240]}
    return {"entries": entries}


# Read-only running-process probe. One generic PowerShell pass over
# Get-CimInstance Win32_Process; filtering against the watchlist happens in
# Python (security_overview._watchlist_alerts), so user-supplied watch names
# are never interpolated into a command line. Also powers the process picker.
def _security_collect_processes() -> dict:
    if os.name != "nt":
        return {"processes": [], "count": 0}
    res = _run_powershell(
        "Get-CimInstance -ClassName Win32_Process -ErrorAction Stop | "
        "Select-Object ProcessId, Name, ExecutablePath, CommandLine | "
        "ConvertTo-Json -Compress -Depth 2",
        timeout=25,
        max_stdout=2_000_000,
    )
    raw = (res.get("stdout") or "").lstrip("\ufeff").strip()
    if not res.get("ok") or not raw:
        return {"processes": [], "count": 0, "error": ((res.get("stderr") or raw or "process probe failed").strip()[:300])}
    try:
        data = json.loads(raw, strict=False)
    except Exception:
        data = None
    rows = data if isinstance(data, list) else ([data] if isinstance(data, dict) else [])
    out = []
    for row in rows[:400]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            pid = 0
        out.append({
            "pid": pid,
            "name": name[:120],
            "path": str(row.get("ExecutablePath") or "")[:260],
            "cmdline": str(row.get("CommandLine") or "")[:300],
        })
    return {"processes": out, "count": len(out)}


# Recent agent commands (cmd history, 24h window) for the L-scored
# "command looked malicious / prompt injection" review layer.
def _security_collect_commands() -> dict:
    cutoff_ms = (int(time.time()) - 24 * 3600) * 1000
    entries: list[dict] = []
    try:
        for entry in _read_cmd_history(limit=150):
            try:
                ts = int(entry.get("ts") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts and ts < cutoff_ms:
                continue
            entries.append({
                "ts": ts,
                "tool": str(entry.get("tool") or "")[:60],
                "target": str(entry.get("target") or "")[:40],
                "ok": bool(entry.get("ok", True)),
                "command": str(entry.get("command") or "")[:420],
            })
    except Exception as exc:
        return {"entries": entries, "error": str(exc)[:240]}
    return {"entries": entries}


def _is_admin() -> bool:
    """True when this bridge runs elevated (start.bat --admin / admin shell)."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _security_notify_alerts(payload: dict) -> None:
    """Notifier hook for SecurityOverviewService: Discord routing only.

    The service itself emits `security:attention` on the SSE bus (popup is
    rendered by the web UI from the overview's `attention` field); this hook
    adds the external leg — suppressed while the user looks active, delivered
    the moment they look absent, escalated after 30 minutes, dropped silently
    once acknowledged in-app. Nothing is ever silently lost while pending.
    """
    loop = _discord_state.get("loop")
    client = _discord_state.get("client")
    if not _discord_state.get("running") or not loop or not client:
        return
    s = get_settings()
    owner_id = str(s.get("discord_owner_id") or "").strip()
    if not s.get("discord_enabled") or not owner_id or s.get("security_discord_notify") is False:
        return
    import asyncio

    async def post(alert):
        owner = await client.fetch_user(int(owner_id))
        ch = owner.dm_channel or await owner.create_dm()
        confidence = alert.get("confidence")
        conf = f" (confidence {int(confidence)}%)" if isinstance(confidence, (int, float)) else ""
        await ch.send(
            f"🛡 security: **{alert.get('title') or 'local alert'}**{conf}\n"
            f"{str(alert.get('detail') or '')[:1400]}\n"
            f"open accuretta → Security Overview to investigate"
        )

    for alert in (payload.get("alerts") or [])[:4]:
        alert_id = str(alert.get("id") or "")
        if not alert_id:
            continue

        def _send(a=alert) -> bool:
            try:
                asyncio.run_coroutine_threadsafe(post(a), loop).result(timeout=30)
                return True
            except Exception as err:
                print(f"[discord] security post failed: {err}", flush=True)
                return False

        _presence_notify.post_external(
            key=f"security:{alert_id}",
            kind="security",
            title=str(alert.get("title") or "security alert"),
            body=str(alert.get("detail") or "")[:1200],
            send=_send,
            resolved=lambda aid=alert_id: not _security_overview.attention_open(aid),
            escalate_after_s=1800.0,
        )


_security_overview = SecurityOverviewService(
    DATA / "security_overview.db",
    collectors={
        "network": lambda: tool_network_snapshot(
            {"compare_baseline": "security-overview", "save_baseline": "security-overview"},
            approved=True,
        ),
        "system": lambda: tool_parse_event_logs(
            {"log": "system", "hours": 24, "limit": 180}, approved=True,
        ),
        "application": lambda: tool_parse_event_logs(
            {"log": "application", "hours": 24, "limit": 120}, approved=True,
        ),
        "security": lambda: tool_parse_event_logs(
            {"log": "security", "hours": 24, "limit": 200}, approved=True,
        ),
        "persistence": lambda: tool_persistence_hunt({}, approved=True),
        "processes": _security_collect_processes,
        "commands": _security_collect_commands,
        "actions": _security_collect_actions,
    },
    summarizer=_security_model_summary,
    model_busy=_security_model_busy,
    emit=broadcast_event,
    notifier=_security_notify_alerts,
    env_info=lambda: {
        "elevated": _is_admin(),
        "platform": "windows",
    },
)


def _security_apply_schedule(s: dict) -> None:
    """Push scheduled-scan settings into the overview service."""
    try:
        interval = int(s.get("security_interval_minutes") or 0)
    except (TypeError, ValueError):
        interval = 0
    if s.get("security_scheduled") and interval > 0:
        _security_overview.set_schedule(max(5, interval) * 60)
    else:
        _security_overview.set_schedule(0)
    _security_overview.set_notify_threshold(str(s.get("security_notify_threshold") or "medium"))


def _security_boot_scan() -> None:
    """Establish a baseline overview shortly after boot, before the first
    scheduled pass — so scheduled alerts compare against a real snapshot."""
    time.sleep(90)
    try:
        if not _security_overview.get_overview().get("generated_at"):
            _security_overview.refresh()
    except Exception:
        pass
