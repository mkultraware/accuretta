# SPDX-License-Identifier: AGPL-3.0-only
"""Mission logs: the action audit file, which logs a mission keeps, and purging
the rest when a mission ends.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.redteam.governor import _RT_GOV_LEDGER_LOCK
from accuretta.redteam.identities import _rt_identities_forget
from accuretta.redteam.mission import _rt_authorized_mission
from pathlib import Path


ACTION_AUDIT_FILE = DATA / "action_audit.jsonl"


# ---- engagement logs the user chose not to keep ----------------------------
# Unticking "Keep log?" in the questionnaire sets mission.keep_log False. The
# evidence still gets written during the run (review_finding and the report
# need it), and every file is remembered here so it can be deleted when the
# engagement ends: closed, replaced by a new one, or found ended at startup.
_RT_UNKEPT_LOGS_FILE = DATA / "redteam" / "unkept_logs.json"


_RT_UNKEPT_LOCK = threading.Lock()


def _rt_keeps_log(mission: dict | None) -> bool:
    return not isinstance(mission, dict) or mission.get("keep_log") is not False


def _rt_filter_jsonl(path: Path, field: str, key: str) -> None:
    """Rewrite a JSONL log without the lines whose `field` equals `key`."""
    if not path.exists():
        return
    kept, dropped = [], 0
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                if json.loads(line).get(field) == key:
                    dropped += 1
                    continue
            except Exception:
                pass
            kept.append(line)
    if dropped:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(kept), encoding="utf-8")
        os.replace(tmp, path)


def _rt_log_purge(key: str) -> None:
    """Delete everything an unkept engagement logged: evidence files, receipts,
    and its lines in the request ledger and action audit."""
    if not key:
        return
    with _RT_UNKEPT_LOCK:
        reg = load_json(_RT_UNKEPT_LOGS_FILE, {})
        reg = reg if isinstance(reg, dict) else {}
        ev_root = (DATA / "recon_evidence").resolve()
        for p in reg.pop(key, []):
            try:
                target = Path(p).resolve()
                if ev_root not in target.parents:
                    continue
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
            except Exception:
                continue
        try:
            with _RT_GOV_LEDGER_LOCK:
                for name in ("request_ledger.jsonl", "request_ledger.1.jsonl"):
                    _rt_filter_jsonl(DATA / "redteam" / name, "mission", key)
            with _ACTION_AUDIT_LOCK:
                _rt_filter_jsonl(ACTION_AUDIT_FILE, "authorization_id", key)
        except Exception:
            pass
        save_json(_RT_UNKEPT_LOGS_FILE, reg)


def _rt_log_mission_ended(mission: dict | None) -> None:
    """Clean up after a finished engagement: always delete its stored test-account
    sign-ins, and purge its logs when the user chose not to keep them."""
    _rt_identities_forget(mission)
    if _rt_keeps_log(mission) or not mission.get("authorization_id"):
        return
    threading.Thread(target=_rt_log_purge, args=(str(mission["authorization_id"]),),
                     daemon=True).start()


def _rt_log_sweep() -> int:
    """At startup, purge unkept engagements that are no longer active (closed,
    replaced, or their chat deleted while the app was running)."""
    reg = load_json(_RT_UNKEPT_LOGS_FILE, {})
    if not isinstance(reg, dict) or not reg:
        return 0
    active = set()
    for chat in (get_chats().get("chats") or {}).values():
        m = chat.get("mission") if isinstance(chat, dict) else None
        if _rt_authorized_mission(chat) and isinstance(m, dict):
            active.add(str(m.get("authorization_id") or ""))
    ended = [k for k in reg if k not in active]
    for key in ended:
        _rt_log_purge(key)
    return len(ended)


# Durable, content-minimised provenance for actions that can change files,
# processes, applications, repositories, or external targets.  Arguments and
# outputs are represented by hashes; commands, typed text, prompt content, and
# tool output are never copied into the audit log.
_ACTION_AUDIT_LOCK = threading.Lock()
