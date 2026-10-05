"""Local, read-only security overview service.

The service owns persistence, deterministic triage, whitelisting, bounded
investigations, the scheduled scan loop, the process watchlist, and the
notable-alert notification ledger. Host collection and model inference are
injected by bridge.py so this module never imports the agent runtime or gains
access to its tools.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable


_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_RISK_COPY = {
    "quiet": ("No immediate threat detected", "No unwhitelisted activity currently needs attention."),
    "review": ("A few items are worth reviewing", "Accuretta found low-confidence changes that deserve a quick look."),
    "elevated": ("Security activity needs review", "One or more evidence-backed changes may require investigation."),
    "critical": ("High-risk activity detected", "Accuretta found strongly suspicious or high-impact activity."),
}

# Deterministic pre-filter for agent-command review: candidates that match any
# of these reach the (injected) model for a confidence-scored verdict, which is
# the "deeper than regex" layer. The pre-filter exists only to bound model
# usage — its misses never hide anything from the LLM review of *later* scans,
# and every recorded command stays visible in source_counts.
_CMD_SUSPICION_PATTERNS = tuple(compiled for compiled in (
    re.compile(pattern, re.I) for pattern in (
        r"\b(?:invoke-expression|iex|invoke-command)\b",
        r"(?:^|\s)-enc(?:odedcommand)?\b",
        r"(?:^|\s)-(?:w|windowstyle)\s+(?:hidden|h)\b",
        r"\b(?:certutil|bitsadmin)\b.*\b(?:urlcache|transfer|download)\b",
        r"(?:invoke-webrequest|invoke-restmethod|\bcurl\b|\bwget\b)[^\n]{0,200}\|\s*(?:iex|invoke-expression)",
        r"\bfrombase64string\b",
        r"\brundll32\b[^\n]{0,120}\b(?:shell32|url\.dll|javascript|mshtml)\b",
        r"\breg(?:\.exe)?\s+add\b[^\n]{0,200}currentversion\\run",
        r"\bschtasks\b[^\n|]{0,200}\b/create\b",
        r"\bnetsh\b[^\n]{0,120}\bportproxy\b",
        r"discord(?:app)?\.com/api/webhooks",
        r"\bstart-process\b[^\n]{0,120}-verb\s+runas\b",
        r"\b(?:vssadmin|wbadmin|bcdedit)\b[^\n]{0,120}\b(?:delete|shadow|recovery)\b",
        r"\bset-mppreference\b[^\n]{0,160}\b(?:disable|exclusion)\b",
        r"\bmshta\b|\bcsc\.exe\b|\binstallutil\b",
        r"\bnet(?:.exe)?\s+(?:user|localgroup)\b[^\n]{0,80}\b/add\b",
        r"\bwevtutil\b[^\n]{0,80}\bcl\b",
        r"\btaskkill\b[^\n]{0,80}(?:/f|/im)",
        r"\bbcdedit\b[^\n]{0,80}bootstatuspolicy",
        r"\bmd5sum\b|\bsha256sum\b",
    )
))

# Lowest model confidence that becomes a user-visible alert (0-100).
_CMD_ALERT_MIN_CONFIDENCE = 65
_WATCH_WHEN_VALUES = {"running", "network", "new_listener", "unsigned", "outside_hours"}


def _now() -> int:
    return int(time.time())


def _bounded(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _fingerprint(*parts: Any) -> str:
    raw = "|".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def _normalize_entity(value: Any) -> str:
    text = str(value or "").strip().strip('"\'')
    if not text:
        return ""
    return re.sub(r"/+", r"\\", text).lower()[:500]


def _path_from_text(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"(?i)([a-z]:\\[^\r\n\"]+?\.(?:exe|dll|com|bat|cmd|ps1))(?=\s|$|\"|')", text)
    return match.group(1).strip() if match else ""


def _risky_path(value: Any) -> bool:
    text = str(value or "").lower()
    return any(part in text for part in (
        "\\temp\\", "\\downloads\\", "\\desktop\\", "$recycle.bin", "\\windows\\temp\\",
    ))


class SecurityOverviewService:
    """Thread-safe facade for manual security scans and saved investigations."""

    def __init__(
        self,
        db_path: Path,
        collectors: dict[str, Callable[[], dict]],
        summarizer: Callable[[str, dict], dict | None] | None = None,
        model_busy: Callable[[], bool] | None = None,
        emit: Callable[[dict], None] | None = None,
        notifier: Callable[[dict], None] | None = None,
        env_info: Callable[[], dict] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.collectors = dict(collectors)
        self.summarizer = summarizer
        self.model_busy = model_busy or (lambda: False)
        self.emit = emit or (lambda _event: None)
        self.notifier = notifier
        self.env_info = env_info or (lambda: {})
        self._notify_threshold = "medium"
        self._state_lock = threading.RLock()
        self._scan_thread: threading.Thread | None = None
        self._scan_state = {"status": "idle", "stage": "", "started_at": None, "error": ""}
        self._attention = {"updated": None, "alerts": []}
        self._schedule_seconds = 0
        self._next_scheduled: int | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_stop = threading.Event()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _db(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _init_db(self) -> None:
        with self._db() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS whitelist (
                    id TEXT PRIMARY KEY,
                    entity_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    behaviors_json TEXT NOT NULL DEFAULT '[]',
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigations (
                    id TEXT PRIMARY KEY,
                    created_at INTEGER NOT NULL,
                    alert_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_scans_created ON scans(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_investigations_created ON investigations(created_at DESC);
                CREATE TABLE IF NOT EXISTS alert_filters (
                    id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    match_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(mode, match_key)
                );
                CREATE TABLE IF NOT EXISTS process_watchlist (
                    id TEXT PRIMARY KEY,
                    process_key TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    conditions_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL
                );
                """
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(whitelist)").fetchall()}
            if "behaviors_json" not in columns:
                db.execute("ALTER TABLE whitelist ADD COLUMN behaviors_json TEXT NOT NULL DEFAULT '[]'")

    def _latest_raw(self) -> dict | None:
        with self._db() as db:
            row = db.execute(
                "SELECT payload_json FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload_json"])
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def _whitelist_rows(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, entity_key, label, reason, behaviors_json, created_at, last_seen "
                "FROM whitelist ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for row in rows:
            value = dict(row)
            try:
                value["behaviors"] = json.loads(value.pop("behaviors_json") or "[]")
            except Exception:
                value["behaviors"] = []
            out.append(value)
        return out

    def _investigation_rows(self, limit: int = 20) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT payload_json FROM investigations ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        out = []
        for row in rows:
            try:
                value = json.loads(row["payload_json"])
                if isinstance(value, dict):
                    out.append(value)
            except Exception:
                continue
        return out

    @staticmethod
    def _action_key(row: dict, occurrence: bool = False) -> str:
        fields = [row.get(key) for key in ("tool", "status", "target", "args_sha256", "result_sha256")]
        if occurrence:
            fields.append(row.get("t"))
        return _fingerprint(*fields)

    def _alert_filters(self) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM alert_filters ORDER BY created_at DESC, id"
            ).fetchall()]

    def filter_alert(self, alert_id: str, mode: str) -> dict:
        if mode not in {"dismiss", "ignore"}:
            return {"ok": False, "error": "Choose dismiss or ignore."}
        alert = next((item for item in self.get_overview().get("alerts", [])
                      if item.get("id") == alert_id), None)
        if not alert or alert.get("source") != "accuretta" or alert.get("kind") != "action_failures":
            return {"ok": False, "error": "Only recorded Accuretta tool errors can be dismissed here."}
        with self._db() as db:
            for row in alert.get("evidence", []):
                key = self._action_key(row, occurrence=mode == "dismiss")
                label = _bounded(f"{row.get('tool') or 'Tool'}: {row.get('status') or 'error'}"
                                 f" · {row.get('target') or 'local action'}", 350)
                db.execute("INSERT OR IGNORE INTO alert_filters VALUES (?, ?, ?, ?, ?)",
                           (f"filter-{mode}-{key}", mode, key, label, _now()))
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self.get_overview()}

    def remove_alert_filter(self, filter_id: str) -> dict:
        with self._db() as db:
            removed = db.execute("DELETE FROM alert_filters WHERE id = ?", (filter_id,)).rowcount
        if not removed:
            return {"ok": False, "error": "Filter not found."}
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self.get_overview()}

    def scan_state(self) -> dict:
        with self._state_lock:
            return dict(self._scan_state)

    def refresh(self) -> dict:
        with self._state_lock:
            if self._scan_thread and self._scan_thread.is_alive():
                return {"started": False, **self._scan_state}
            self._scan_state = {
                "status": "scanning",
                "stage": "Preparing local collectors",
                "started_at": _now(),
                "error": "",
            }
            self._scan_thread = threading.Thread(
                target=self._scan_worker,
                name="security-overview-scan",
                daemon=True,
            )
            self._scan_thread.start()
        self.emit({"type": "security:update", "status": "scanning"})
        return {"started": True, **self.scan_state()}

    def _set_stage(self, stage: str) -> None:
        with self._state_lock:
            self._scan_state["stage"] = stage
        self.emit({"type": "security:update", "status": "scanning", "stage": stage})

    # ---- scheduled scans ---------------------------------------------------

    def set_schedule(self, seconds: int) -> None:
        """Set the scheduled-scan cadence. 0 disables scheduled scans."""
        try:
            seconds = int(seconds)
        except (TypeError, ValueError):
            seconds = 0
        with self._state_lock:
            self._schedule_seconds = max(0, seconds)
            self._next_scheduled = _now() + self._schedule_seconds if self._schedule_seconds > 0 else None

    def schedule_seconds(self) -> int:
        with self._state_lock:
            return int(self._schedule_seconds)

    def start_scheduler(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_stop.clear()
        with self._state_lock:
            if self._schedule_seconds > 0 and self._next_scheduled is None:
                self._next_scheduled = _now() + self._schedule_seconds
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            name="security-overview-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()

    def stop_scheduler(self) -> None:
        self._scheduler_stop.set()

    def _scheduler_loop(self) -> None:
        while not self._scheduler_stop.wait(timeout=5.0):
            with self._state_lock:
                interval = int(self._schedule_seconds)
                due = self._next_scheduled
            if interval <= 0 or due is None:
                continue
            if _now() < due:
                continue
            with self._state_lock:
                if self._scan_thread and self._scan_thread.is_alive():
                    self._next_scheduled = _now() + interval
                    continue
                self._next_scheduled = _now() + interval
            try:
                self.refresh()
            except Exception:
                continue

    def _scan_worker(self) -> None:
        sources: dict[str, dict] = {}
        try:
            labels = {
                "network": "Reading network state",
                "system": "Reading system events",
                "application": "Reading application events",
                "security": "Checking security events",
                "persistence": "Checking startup and persistence",
                "processes": "Checking watched processes",
                "commands": "Reviewing recent agent commands",
                "actions": "Reading Accuretta action history",
            }
            for name, collector in self.collectors.items():
                self._set_stage(labels.get(name, f"Reading {name}"))
                started = time.monotonic()
                try:
                    value = collector()
                    if not isinstance(value, dict):
                        value = {"error": "collector returned an invalid response"}
                except Exception as exc:
                    value = {"error": _bounded(exc, 300)}
                value["duration_ms"] = int((time.monotonic() - started) * 1000)
                sources[name] = value

            self._set_stage("Correlating evidence")
            snapshot = self._build_snapshot(sources)

            if self.summarizer and not self.model_busy() and self._command_candidates(sources):
                self._set_stage("Scoring recent agent commands")
                command_alerts = self._score_commands(sources)
                if command_alerts:
                    snapshot["alerts_all"] = self._dedupe_alerts(
                        [*snapshot.get("alerts_all", []), *command_alerts])

            public = self._public_snapshot(snapshot)
            if self.summarizer and not self.model_busy():
                self._set_stage("Writing situation summary")
                summary = self.summarizer("overview", self._summary_digest(public))
                if isinstance(summary, dict):
                    snapshot["model_summary"] = self._clean_summary(summary)
                    snapshot["summary_hash"] = public["evidence_hash"]
            elif self.model_busy():
                snapshot["summary_deferred"] = True

            with self._db() as db:
                db.execute(
                    "INSERT INTO scans(created_at, payload_json) VALUES (?, ?)",
                    (snapshot["generated_at"], json.dumps(snapshot, ensure_ascii=False)),
                )
                db.execute(
                    "DELETE FROM scans WHERE id NOT IN (SELECT id FROM scans ORDER BY id DESC LIMIT 120)"
                )
            with self._state_lock:
                self._scan_state = {
                    "status": "idle",
                    "stage": "",
                    "started_at": None,
                    "error": "",
                }
            self.emit({"type": "security:update", "status": "ready"})
            # Recompute after the model summary landed so the attention payload
            # carries the LLM's final overview, not the fallback summary.
            public = self._public_snapshot(snapshot)
            self._notify_notable(public)
        except Exception as exc:
            with self._state_lock:
                self._scan_state = {
                    "status": "error",
                    "stage": "",
                    "started_at": None,
                    "error": _bounded(exc, 400),
                }
            self.emit({"type": "security:update", "status": "error"})

    def _alert(
        self,
        source: str,
        kind: str,
        severity: str,
        title: str,
        detail: str,
        entity_key: str,
        entity_label: str,
        evidence: list[dict] | None = None,
        first_seen: str = "",
    ) -> dict:
        evidence = list(evidence or [])[:12]
        stable = _fingerprint(source, kind, entity_key, title)
        normalized_entity = _normalize_entity(entity_key) or f"alert:{stable}"
        return {
            "id": f"sec-{stable}",
            "source": source,
            "kind": kind,
            "severity": severity if severity in _SEVERITY_RANK else "info",
            "title": _bounded(title, 140),
            "detail": _bounded(detail, 700),
            "entity_key": normalized_entity,
            "entity_label": _bounded(entity_label or title, 180),
            "whitelistable": normalized_entity.startswith(("path:", "process:", "persistence:")),
            "first_seen": _bounded(first_seen, 40),
            "evidence": evidence,
        }

    @staticmethod
    def _dedupe_alerts(alerts: list[dict]) -> list[dict]:
        unique: dict[str, dict] = {}
        for alert in alerts:
            current = unique.get(alert["id"])
            if not current or _SEVERITY_RANK[alert["severity"]] > _SEVERITY_RANK[current["severity"]]:
                unique[alert["id"]] = alert
        return sorted(
            unique.values(),
            key=lambda item: (_SEVERITY_RANK[item["severity"]], item.get("first_seen", "")),
            reverse=True,
        )

    # ---- process watchlist ---------------------------------------------------

    def _watchlist_rows(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id, process_key, label, conditions_json, created_at, last_seen, enabled "
                "FROM process_watchlist ORDER BY created_at DESC"
            ).fetchall()
        out = []
        for row in rows:
            value = dict(row)
            try:
                value["conditions"] = json.loads(value.pop("conditions_json") or "{}")
            except Exception:
                value["conditions"] = {}
            if not isinstance(value["conditions"], dict):
                value["conditions"] = {}
            value["enabled"] = bool(value.get("enabled", 1))
            out.append(value)
        return out

    def add_process_watch(
        self,
        process_key: str,
        label: str = "",
        conditions: dict | None = None,
        acknowledge_compute: bool = False,
    ) -> dict:
        """Watch one specific process (Task-Manager-style pick).

        process_key is "name:<exe name>" or "path:<full path>". conditions:
        {"when": ["running"|"network"|"new_listener"|"unsigned"|"outside_hours"],
         "hours": {"start": 0-23, "end": 0-23}, "severity": "low|medium|high"}
        """
        key = _bounded(process_key, 520).strip().lower()
        if not key or ":" not in key:
            return {"ok": False, "error": "Pick a process name or path to watch."}
        kind, _, target = key.partition(":")
        if kind not in {"name", "path"} or not target.strip():
            return {"ok": False, "error": "Watch keys must be name: or path: based."}
        if not acknowledge_compute:
            return {"ok": False, "error": "Confirm the performance disclaimer to enable background monitoring."}
        conds = conditions if isinstance(conditions, dict) else {}
        when = [w for w in (conds.get("when") or ["running"]) if w in _WATCH_WHEN_VALUES] or ["running"]
        hours = conds.get("hours") if isinstance(conds.get("hours"), dict) else None
        severity = str(conds.get("severity") or "medium").lower()
        clean = {"when": when, "severity": severity if severity in _SEVERITY_RANK and severity != "info" else "medium"}
        if "outside_hours" in when and hours:
            try:
                clean["hours"] = {"start": max(0, min(23, int(hours.get("start")))),
                                  "end": max(0, min(23, int(hours.get("end"))))}
            except (TypeError, ValueError):
                clean.pop("hours", None)
                when = [w for w in when if w != "outside_hours"] or ["running"]
                clean["when"] = when
        watch_id = f"watch-{_fingerprint(key)}"
        now = _now()
        with self._db() as db:
            db.execute(
                "INSERT INTO process_watchlist(id, process_key, label, conditions_json, created_at, last_seen, enabled) "
                "VALUES (?, ?, ?, ?, ?, ?, 1) "
                "ON CONFLICT(process_key) DO UPDATE SET label=excluded.label, "
                "conditions_json=excluded.conditions_json, last_seen=excluded.last_seen, enabled=1",
                (
                    watch_id,
                    key,
                    _bounded(label or target, 180),
                    json.dumps(clean, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "watch_id": watch_id, "overview": self.get_overview()}

    def remove_process_watch(self, watch_id: str) -> dict:
        with self._db() as db:
            removed = db.execute(
                "DELETE FROM process_watchlist WHERE id = ?", (str(watch_id),)
            ).rowcount
        if not removed:
            return {"ok": False, "error": "Watchlist entry not found."}
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self.get_overview()}

    @staticmethod
    def _hour_outside_window(hours: dict) -> bool:
        try:
            start = int(hours.get("start"))
            end = int(hours.get("end"))
        except (TypeError, ValueError, AttributeError):
            return False
        if not (0 <= start <= 23 and 0 <= end <= 23) or start == end:
            return False
        hour = time.localtime().tm_hour
        if start < end:                      # allowed window [start, end)
            return hour < start or hour >= end
        return hour < start and hour >= end  # wrap-around window, e.g. 23 -> 07

    def _watchlist_alerts(self, sources: dict) -> list[dict]:
        rows = [row for row in self._watchlist_rows() if row.get("enabled")]
        if not rows:
            return []
        processes = (sources.get("processes") or {}).get("processes") or []
        net_by_name: dict[str, dict] = {}
        for item in (sources.get("network") or {}).get("process_details") or []:
            if isinstance(item, dict) and item.get("process"):
                net_by_name[str(item.get("process")).lower()] = item
        added_udp = [str(sig) for sig in ((sources.get("network") or {}).get("comparison") or {}).get("added_udp") or []]
        alerts: list[dict] = []
        now_stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        for row in rows:
            key = str(row.get("process_key") or "")
            kind, _, target = key.partition(":")
            target = target.strip().lower()
            if not target:
                continue
            matches = []
            for process in processes:
                if not isinstance(process, dict):
                    continue
                name = str(process.get("name") or "").strip().lower()
                path = str(process.get("path") or "").strip().lower()
                if (kind == "name" and name == target) or (kind == "path" and path == target):
                    matches.append(process)
            if not matches:
                continue
            name = str(matches[0].get("name") or target)
            conds = row.get("conditions") or {}
            when = conds.get("when") or ["running"]
            triggered: list[str] = []
            evidence: list[dict] = []
            if "running" in when:
                triggered.append("it is running")
            for match in matches[:2]:
                evidence.append({
                    "pid": match.get("pid"),
                    "name": name,
                    "path": _bounded(match.get("path"), 260),
                    "cmdline": _bounded(match.get("cmdline"), 300),
                })
            net = net_by_name.get(name.lower())
            if "network" in when and net and int(net.get("connections") or 0) > 0:
                triggered.append(f"it has {int(net.get('connections') or 0)} active network connection(s)")
                evidence.append({
                    "name": name,
                    "connections": int(net.get("connections") or 0),
                    "signed": net.get("signed"),
                    "path": _bounded(net.get("path"), 260),
                })
            if "unsigned" in when and net and net.get("signed") is False:
                triggered.append("its binary has no valid signature")
            if "new_listener" in when and any(sig.split("|")[0].strip().lower() == name.lower() for sig in added_udp):
                triggered.append("it opened a new UDP listener since the previous scan")
            if "outside_hours" in when and conds.get("hours") and self._hour_outside_window(conds["hours"]):
                start = conds["hours"].get("start")
                end = conds["hours"].get("end")
                triggered.append(f"it is running outside the allowed {int(start):02d}:00–{int(end):02d}:00 window")
            if not triggered:
                continue
            severity = str(conds.get("severity") or "medium").lower()
            alerts.append(self._alert(
                "watchlist", "watched_process",
                severity if severity in _SEVERITY_RANK else "medium",
                f"Watched process: {name}",
                "Triggered because " + " and ".join(triggered) + ". You asked Accuretta to keep an eye on this process.",
                f"watch:{key}", name or target, evidence, now_stamp,
            ))
        return alerts

    # ---- LLM-scored agent command review --------------------------------------

    @staticmethod
    def _command_looks_suspicious(text: str) -> bool:
        value = str(text or "")
        return any(pattern.search(value) for pattern in _CMD_SUSPICION_PATTERNS)

    @staticmethod
    def _confidence_score(value: Any) -> int:
        """Map a model-provided confidence to 0-100."""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, min(100, int(float(value) if float(value) <= 100 else float(value) / 10)))
        word = str(value or "").strip().lower()
        if word in {"high", "certain", "very_likely"}:
            return 85
        if word in {"medium", "moderate", "likely"}:
            return 65
        if word in {"low", "unlikely"}:
            return 35
        return 0

    def _command_candidates(self, sources: dict) -> list[dict]:
        entries = (sources.get("commands") or {}).get("entries") or []
        return [entry for entry in entries if self._command_looks_suspicious(entry.get("command"))][:6]

    def _score_commands(self, sources: dict) -> list[dict]:
        """Ask the injected summarizer to verdict suspicious agent commands.

        Returns alerts with a numeric `confidence` plus an `injection` flag —
        the model review is the deeper-than-regex layer; the deterministic
        pre-filter only decides who gets reviewed.
        """
        if not self.summarizer or self.model_busy():
            return []
        candidates = self._command_candidates(sources)
        if not candidates:
            return []
        digest = [
            {
                "id": f"cmd-{index:02d}",
                "tool": str(candidate.get("tool") or "")[:60],
                "command": str(candidate.get("command") or "")[:420],
            }
            for index, candidate in enumerate(candidates)
        ]
        value = self.summarizer("command_review", {"window_hours": 24, "commands": digest})
        verdicts = value.get("verdicts") if isinstance(value, dict) else None
        if not isinstance(verdicts, list):
            return []
        alerts: list[dict] = []
        for index, verdict in enumerate(verdicts[:6]):
            if not isinstance(verdict, dict) or index >= len(candidates):
                continue
            score = self._confidence_score(verdict.get("confidence"))
            if score < _CMD_ALERT_MIN_CONFIDENCE:
                continue
            malicious = verdict.get("malicious") is True or str(
                verdict.get("verdict") or "").lower() in {"suspicious", "malicious", "critical"}
            injection = verdict.get("injection") is True
            if not (malicious or injection):
                continue
            reason = str(verdict.get("reason") or verdict.get("summary")
                         or "The local model judged this command worth review.")[:400]
            command_text = str(candidates[index].get("command") or "")[:360]
            alert = self._alert(
                "commands", "malicious_command",
                "high" if score >= 80 else "medium",
                f"Agent command flagged ({score}% confidence)",
                f"{reason} Command: {command_text}",
                f"command:{_fingerprint(command_text)}",
                f"agent command cmd-{index:02d}",
                [{
                    "id": digest[index]["id"],
                    "confidence": score,
                    "injection": injection,
                    "command": command_text[:280],
                }],
                time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            alert["confidence"] = score
            alert["injection"] = injection
            alerts.append(alert)
        return alerts

    # ---- notable-alert notifications -------------------------------------------

    @staticmethod
    def _attention_view(alert: dict) -> dict:
        return {
            "id": alert.get("id"),
            "severity": alert.get("severity"),
            "title": alert.get("title"),
            "detail": alert.get("detail"),
            "entity_label": alert.get("entity_label"),
            "confidence": alert.get("confidence"),
            "injection": alert.get("injection"),
            "source": alert.get("source"),
        }

    def attention_open(self, alert_id: str) -> bool:
        with self._state_lock:
            return any(item.get("id") == alert_id for item in (self._attention or {}).get("alerts", []))

    def acknowledge_attention(self) -> dict:
        with self._state_lock:
            had = bool((self._attention or {}).get("alerts"))
            self._attention = {"updated": _now(), "alerts": []}
        if had:
            self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self.get_overview()}

    def _notify_notable(self, public: dict) -> None:
        """Persist + route NEW alerts that cross the notification threshold.

        Deduped by alert id in the notifications ledger so re-scans never
        spam the user about the same finding twice.
        """
        threshold = _SEVERITY_RANK.get(str(self._notify_threshold or "medium").lower(), 2)
        notable = [
            item for item in public.get("alerts", [])
            if _SEVERITY_RANK.get(item.get("severity"), 0) >= threshold
        ]
        if not notable:
            return
        fresh: list[dict] = []
        with self._db() as db:
            for alert in notable:
                seen = db.execute(
                    "SELECT 1 FROM notifications WHERE alert_id = ?", (alert["id"],)
                ).fetchone()
                if not seen:
                    db.execute(
                        "INSERT OR IGNORE INTO notifications(alert_id, created_at) VALUES (?, ?)",
                        (alert["id"], _now()),
                    )
                    fresh.append(alert)
            db.execute("DELETE FROM notifications WHERE id NOT IN (SELECT id FROM notifications ORDER BY id DESC LIMIT 500)")
        if not fresh:
            return
        with self._state_lock:
            self._attention = {
                "updated": _now(),
                "alerts": [self._attention_view(alert) for alert in fresh[:6]],
            }
        self.emit({"type": "security:attention", "status": "attention"})
        if self.notifier:
            try:
                self.notifier({"alerts": fresh, "summary": public.get("summary") or {}, "risk": public.get("risk") or {}})
            except Exception:
                pass

    def set_notify_threshold(self, value: str) -> None:
        word = str(value or "").strip().lower()
        self._notify_threshold = word if word in _SEVERITY_RANK else "medium"

    def _build_snapshot(self, sources: dict[str, dict]) -> dict:
        alerts: list[dict] = []
        timeline: list[dict] = []
        coverage: list[dict] = []

        for name, data in sources.items():
            access = str(data.get("access") or "").lower()
            if data.get("error"):
                state, note = "error", _bounded(data.get("error"), 220)
            elif access in {"denied", "error"}:
                state = "limited"
                note = "Additional Windows access is required." if access == "denied" else "The source could not be read."
            else:
                state, note = "available", "Collector completed successfully."
            coverage.append({
                "source": name,
                "state": state,
                "note": note,
                "duration_ms": int(data.get("duration_ms") or 0),
            })

        event_labels = {
            4624: "Successful logon",
            4625: "Failed logon",
            4688: "Process created",
            4698: "Scheduled task created",
            7045: "Service installed",
            1074: "Shutdown or restart requested",
            6005: "Event log service started",
            6006: "Event log service stopped",
            1001: "Application crash",
        }
        events: list[dict] = []
        for log_name in ("system", "application", "security"):
            for event in sources.get(log_name, {}).get("events") or []:
                if not isinstance(event, dict):
                    continue
                event_id = int(event.get("event_id") or 0)
                row = {
                    "source": log_name,
                    "event_id": event_id,
                    "label": _bounded(event.get("label") or event_labels.get(event_id, "Windows event"), 100),
                    "time": _bounded(event.get("time"), 40),
                    "provider": _bounded(event.get("provider"), 120),
                    "details": _bounded(event.get("details"), 900),
                }
                events.append(row)
                if event_id in {4698, 7045, 1074, 1001}:
                    timeline.append({
                        "time": row["time"],
                        "source": log_name,
                        "label": event_labels.get(event_id, row["label"]),
                        "detail": row["details"][:240],
                    })

        failed = [event for event in events if event["event_id"] == 4625]
        if len(failed) >= 10:
            severity = "high" if len(failed) >= 50 else "medium"
            alerts.append(self._alert(
                "security", "failed_logons", severity,
                f"{len(failed)} failed logons in the scan window",
                "Repeated authentication failures can be a mistyped password, a stale service credential, or an attempted login.",
                "windows-event:4625", "Windows failed logons", failed[:8],
                failed[-1].get("time", ""),
            ))

        for event in [item for item in events if item["event_id"] in {4698, 7045}][:12]:
            event_id = event["event_id"]
            path = _path_from_text(event.get("details"))
            if not path or not _risky_path(path):
                continue
            kind = "scheduled_task" if event_id == 4698 else "service_install"
            title = "A scheduled task was created" if event_id == 4698 else "A Windows service was installed"
            alerts.append(self._alert(
                event["source"], kind, "medium", title,
                event.get("details") or "Windows recorded a persistence-capable system change.",
                f"path:{path}" if path else f"event:{event_id}:{_fingerprint(event.get('details'))}",
                path or title, [event], event.get("time", ""),
            ))

        persistence = sources.get("persistence", {})
        for item in (persistence.get("flagged") or [])[:30]:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category") or "persistence")
            value = str(item.get("value") or "")
            consumer_payload = str(item.get("consumer_payload") or "")
            path = _path_from_text(consumer_payload) or _path_from_text(value)
            label = str(item.get("name") or item.get("location") or path or category)
            score = int(item.get("evidence_score") or 0)
            if score < 2:
                continue
            severity = "high" if score >= 4 else "medium" if category in {"wmi_subscription", "service", "scheduled_task"} else "low"
            detail = consumer_payload or value or str(item.get("location") or "Persistence entry needs review.")
            alerts.append(self._alert(
                "persistence", category, severity,
                f"{label} appears in {category.replace('_', ' ')}",
                detail,
                f"path:{path}" if path else f"persistence:{category}:{label}",
                path or label, [item],
            ))

        network = sources.get("network", {})
        comparison = network.get("comparison") or {}
        added_processes = set(str(value).lower() for value in comparison.get("added_processes") or [])
        for process in (network.get("process_details") or [])[:40]:
            if not isinstance(process, dict) or process.get("signed") is not False:
                continue
            path = str(process.get("path") or "")
            lower_path = path.lower()
            risky_location = _risky_path(lower_path)
            newly_seen = str(process.get("process") or "").lower() in added_processes
            if not risky_location and not newly_seen:
                continue
            severity = "medium" if risky_location and newly_seen else "low"
            alerts.append(self._alert(
                "network", "unsigned_process", severity,
                f"Unsigned network process: {process.get('process') or 'unknown'}",
                f"The process has {int(process.get('connections') or 0)} active connection(s) and no valid signature was found.",
                f"path:{path}" if path else f"process:{process.get('process')}",
                path or str(process.get("process") or "Unknown process"), [process],
            ))

        for signature in (comparison.get("added_udp") or [])[:12]:
            parts = str(signature).split("|")
            process = parts[0] if parts else "Unknown process"
            alerts.append(self._alert(
                "network", "new_listener", "low",
                f"New UDP listener: {process}",
                str(signature), f"process:{process}", process,
                [{"signature": str(signature)}],
            ))

        # Agent tool failures (run_powershell "did not complete", etc.) are
        # agent-health telemetry, not system security — they used to be emitted
        # here as source="accuretta"/kind="action_failures" alerts and drowned
        # the overview (2026-09). They stay out by design; the collected action
        # records still feed source_counts, and the LLM-scored
        # "command looked malicious" alert kind reads recent agent commands.

        alerts.extend(self._watchlist_alerts(sources))

        alerts = self._dedupe_alerts(alerts)
        timeline = sorted(timeline, key=lambda item: item.get("time", ""), reverse=True)[:80]
        return {
            "generated_at": _now(),
            "window_hours": 24,
            "platform": "windows" if any(name in sources for name in ("system", "network")) else "unknown",
            "coverage": coverage,
            "alerts_all": alerts[:100],
            "timeline": timeline,
            "source_counts": {
                "windows_events": len(events),
                "tcp_connections": int(network.get("tcp_count") or 0),
                "udp_listeners": int(network.get("udp_count") or 0),
                "persistence_items": len(persistence.get("all_entries") or []),
                "running_processes": int((sources.get("processes") or {}).get("count") or 0),
                "agent_commands": len((sources.get("commands") or {}).get("entries") or []),
                "action_records": len((sources.get("actions") or {}).get("entries") or []),
            },
        }

    def _risk_for(self, alerts: list[dict]) -> dict:
        highest = max((_SEVERITY_RANK.get(item.get("severity"), 0) for item in alerts), default=0)
        if highest >= 4:
            state = "critical"
        elif highest >= 2:
            state = "elevated"
        elif highest >= 1:
            state = "review"
        else:
            state = "quiet"
        headline, detail = _RISK_COPY[state]
        return {"state": state, "headline": headline, "detail": detail, "highest_rank": highest}

    def _public_snapshot(self, raw: dict) -> dict:
        public = copy.deepcopy(raw)
        whitelist = self._whitelist_rows()
        allowed = {row["entity_key"]: set(row.get("behaviors") or []) for row in whitelist}
        all_alerts = public.pop("alerts_all", [])
        filters = self._alert_filters()
        dropped_kinds = {"action_failures"}  # agent-health, never system security
        all_alerts = [
            item for item in all_alerts
            if not (item.get("source") == "accuretta" and str(item.get("kind")) in dropped_kinds)
        ]
        public["alert_filters"] = filters
        def is_allowed(item: dict) -> bool:
            behaviors = allowed.get(item.get("entity_key"))
            return bool(behaviors and item.get("kind") in behaviors)
        alerts = [item for item in all_alerts if not is_allowed(item)]
        hidden = [item for item in all_alerts if is_allowed(item)]
        public["alerts"] = alerts
        public["whitelisted_activity"] = hidden
        public["whitelist"] = whitelist
        public["risk"] = self._risk_for(alerts)
        public["metrics"] = {
            "open_alerts": len(alerts),
            "hidden_alerts": len(hidden),
            "coverage_available": sum(1 for item in public.get("coverage", []) if item.get("state") == "available"),
            "coverage_total": len(public.get("coverage", [])),
            **public.get("source_counts", {}),
        }
        digest_source = [
            (item.get("id"), item.get("severity"), item.get("detail")) for item in alerts
        ] + [(item.get("source"), item.get("state")) for item in public.get("coverage", [])]
        public["evidence_hash"] = _fingerprint(json.dumps(digest_source, sort_keys=True))
        if public.get("summary_hash") != public["evidence_hash"]:
            public.pop("model_summary", None)
        public["summary"] = public.get("model_summary") or self._fallback_summary(public)
        public["summary"]["source"] = "model" if public.get("model_summary") else "rules"
        public["summary_deferred"] = bool(raw.get("summary_deferred"))
        public["scan"] = self.scan_state()
        public["investigations"] = self._investigation_rows(12)
        public["watchlist"] = self._watchlist_rows()
        with self._state_lock:
            public["attention"] = copy.deepcopy(self._attention)
        try:
            public["environment"] = dict(self.env_info() or {})
        except Exception:
            public["environment"] = {}
        return public

    def _fallback_summary(self, public: dict) -> dict:
        risk = public.get("risk") or self._risk_for(public.get("alerts") or [])
        limited = [item["source"] for item in public.get("coverage", []) if item.get("state") != "available"]
        notable = [
            {"alert_id": item["id"], "explanation": item["title"]}
            for item in (public.get("alerts") or [])[:3]
        ]
        coverage_note = (
            "Limited visibility: " + ", ".join(limited) + "."
            if limited else "All configured sources were available during the scan."
        )
        return {
            "headline": risk["headline"],
            "situation": risk["detail"],
            "confidence": "medium" if limited else "high",
            "coverage_note": coverage_note,
            "notable": notable,
            "recommended_checks": [],
        }

    def _summary_digest(self, public: dict) -> dict:
        return {
            "generated_at": public.get("generated_at"),
            "window_hours": public.get("window_hours"),
            "risk": public.get("risk"),
            "metrics": public.get("metrics"),
            "coverage": public.get("coverage"),
            "alerts": [
                {
                    "id": item.get("id"),
                    "severity": item.get("severity"),
                    "title": item.get("title"),
                    "detail": _bounded(item.get("detail"), 420),
                    "entity": item.get("entity_label"),
                }
                for item in (public.get("alerts") or [])[:15]
            ],
        }

    def _clean_summary(self, value: dict) -> dict:
        notable = []
        for item in value.get("notable") or []:
            if isinstance(item, dict):
                notable.append({
                    "alert_id": _bounded(item.get("alert_id"), 80),
                    "explanation": _bounded(item.get("explanation"), 500),
                })
        checks = [_bounded(item, 240) for item in (value.get("recommended_checks") or []) if _bounded(item, 240)]
        confidence = str(value.get("confidence") or "medium").lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        return {
            "headline": _bounded(value.get("headline"), 160),
            "situation": _bounded(value.get("situation"), 1200),
            "confidence": confidence,
            "coverage_note": _bounded(value.get("coverage_note"), 500),
            "notable": notable[:5],
            "recommended_checks": checks[:5],
        }

    def get_overview(self) -> dict:
        raw = self._latest_raw()
        if raw:
            return self._public_snapshot(raw)
        try:
            environment = dict(self.env_info() or {})
        except Exception:
            environment = {}
        with self._state_lock:
            attention = copy.deepcopy(self._attention)
        return {
            "generated_at": None,
            "window_hours": 24,
            "coverage": [],
            "timeline": [],
            "alerts": [],
            "whitelisted_activity": [],
            "whitelist": self._whitelist_rows(),
            "alert_filters": self._alert_filters(),
            "investigations": self._investigation_rows(12),
            "watchlist": self._watchlist_rows(),
            "attention": attention,
            "environment": environment,
            "metrics": {"open_alerts": 0, "hidden_alerts": 0, "coverage_available": 0, "coverage_total": 0},
            "risk": {"state": "quiet", "headline": "No scan has run yet", "detail": "Run a local scan to establish the current situation."},
            "summary": {
                "headline": "Security Overview is ready",
                "situation": "Run the first read-only scan to establish a local baseline.",
                "confidence": "low",
                "coverage_note": "No sources have been read yet.",
                "notable": [],
                "recommended_checks": [],
                "source": "rules",
            },
            "scan": self.scan_state(),
        }

    def summarize_now(self) -> dict:
        raw = self._latest_raw()
        if not raw:
            return {"ok": False, "error": "Run a security scan first."}
        if not self.summarizer:
            return {"ok": False, "error": "No local summarizer is available."}
        if self.model_busy():
            return {"ok": False, "busy": True, "error": "The local model is busy with a chat. Try again when it is idle."}
        public = self._public_snapshot(raw)
        summary = self.summarizer("overview", self._summary_digest(public))
        if not isinstance(summary, dict):
            return {"ok": False, "error": "The local model did not return a usable summary."}
        raw["model_summary"] = self._clean_summary(summary)
        raw["summary_hash"] = public["evidence_hash"]
        raw["summary_deferred"] = False
        with self._db() as db:
            db.execute(
                "INSERT INTO scans(created_at, payload_json) VALUES (?, ?)",
                (_now(), json.dumps(raw, ensure_ascii=False)),
            )
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self._public_snapshot(raw)}

    def add_whitelist(self, alert_id: str, reason: str = "") -> dict:
        raw = self._latest_raw()
        alert = next((item for item in (raw or {}).get("alerts_all", []) if item.get("id") == alert_id), None)
        if not alert:
            return {"ok": False, "error": "Alert not found in the latest scan."}
        if not alert.get("whitelistable"):
            return {"ok": False, "error": "This alert is a system condition, not a stable application identity."}
        entity_key = str(alert.get("entity_key") or "").strip()
        if not entity_key:
            return {"ok": False, "error": "This alert has no stable application identity."}
        whitelist_id = f"allow-{_fingerprint(entity_key)}"
        now = _now()
        with self._db() as db:
            existing = db.execute(
                "SELECT behaviors_json FROM whitelist WHERE entity_key = ?", (entity_key,)
            ).fetchone()
            try:
                behaviors = set(json.loads(existing["behaviors_json"] or "[]")) if existing else set()
            except Exception:
                behaviors = set()
            behaviors.add(str(alert.get("kind") or "unknown"))
            db.execute(
                "INSERT INTO whitelist(id, entity_key, label, reason, behaviors_json, created_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(entity_key) DO UPDATE SET label=excluded.label, reason=excluded.reason, "
                "behaviors_json=excluded.behaviors_json, last_seen=excluded.last_seen",
                (
                    whitelist_id,
                    entity_key,
                    _bounded(alert.get("entity_label") or alert.get("title"), 180),
                    _bounded(reason, 300),
                    json.dumps(sorted(behaviors)),
                    now,
                    now,
                ),
            )
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "whitelist_id": whitelist_id, "overview": self.get_overview()}

    def remove_whitelist(self, whitelist_id: str) -> dict:
        with self._db() as db:
            cursor = db.execute("DELETE FROM whitelist WHERE id = ?", (str(whitelist_id),))
        if not cursor.rowcount:
            return {"ok": False, "error": "Whitelist entry not found."}
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "overview": self.get_overview()}

    def investigation_context(self, alert_id: str) -> dict:
        """Return a small, server-resolved evidence snapshot for a new chat."""
        raw = self._latest_raw()
        alert = next(
            (item for item in (raw or {}).get("alerts_all", []) if item.get("id") == alert_id),
            None,
        )
        if not alert:
            return {"ok": False, "error": "Alert not found in the latest scan."}

        coverage_gaps = [
            {
                "source": _bounded(item.get("source"), 80),
                "state": _bounded(item.get("state"), 40),
                "note": _bounded(item.get("note"), 240),
            }
            for item in ((raw or {}).get("coverage") or [])
            if item.get("state") != "available"
        ][:4]
        evidence = []
        for item in (alert.get("evidence") or [])[:4]:
            if isinstance(item, dict):
                evidence.append({
                    _bounded(key, 64): _bounded(value, 280)
                    for key, value in list(item.items())[:8]
                })
            else:
                evidence.append(_bounded(item, 500))

        context = {
            "alert_id": _bounded(alert.get("id"), 120),
            "source": _bounded(alert.get("source"), 80),
            "kind": _bounded(alert.get("kind"), 100),
            "severity": _bounded(alert.get("severity"), 20),
            "title": _bounded(alert.get("title"), 220),
            "detail": _bounded(alert.get("detail"), 700),
            "entity": _bounded(alert.get("entity_label"), 240),
            "first_seen": alert.get("first_seen"),
            "evidence": evidence,
            "coverage_gaps": coverage_gaps,
            "scan_created_at": (raw or {}).get("generated_at"),
        }
        return {"ok": True, "context": context}

    def investigate(self, alert_id: str) -> dict:
        raw = self._latest_raw()
        alert = next((item for item in (raw or {}).get("alerts_all", []) if item.get("id") == alert_id), None)
        if not alert:
            return {"ok": False, "error": "Alert not found in the latest scan."}
        coverage = list((raw or {}).get("coverage") or [])
        unavailable = [item.get("source") for item in coverage if item.get("state") != "available"]
        report = {
            "id": f"case-{uuid.uuid4().hex[:10]}",
            "created_at": _now(),
            "alert_id": alert_id,
            "status": "complete",
            "assessment": {
                "verdict": "unresolved" if _SEVERITY_RANK.get(alert.get("severity"), 0) >= 2 else "worth_reviewing",
                "confidence": "low" if unavailable else "medium",
                "summary": alert.get("detail") or alert.get("title"),
            },
            "alert": copy.deepcopy(alert),
            "verified": [
                f"The alert came from the {alert.get('source', 'local')} collector.",
                f"Deterministic severity is {alert.get('severity', 'info')}.",
                f"{len(alert.get('evidence') or [])} bounded evidence record(s) support this alert.",
            ],
            "unknown": ([f"Unavailable source: {name}" for name in unavailable] or ["No additional source gaps were reported."]),
            "evidence": copy.deepcopy(alert.get("evidence") or []),
            "coverage": coverage,
            "model_assessment": None,
        }
        if self.summarizer and not self.model_busy():
            model_value = self.summarizer("investigation", {
                "alert": {
                    "id": alert.get("id"),
                    "severity": alert.get("severity"),
                    "title": alert.get("title"),
                    "detail": alert.get("detail"),
                    "entity": alert.get("entity_label"),
                    "evidence": alert.get("evidence"),
                },
                "coverage": coverage,
            })
            if isinstance(model_value, dict):
                report["model_assessment"] = {
                    "verdict": _bounded(model_value.get("verdict"), 60),
                    "confidence": _bounded(model_value.get("confidence"), 20),
                    "summary": _bounded(model_value.get("summary") or model_value.get("situation"), 1200),
                    "what_is_known": [_bounded(item, 300) for item in (model_value.get("what_is_known") or [])][:6],
                    "what_is_unknown": [_bounded(item, 300) for item in (model_value.get("what_is_unknown") or [])][:6],
                    "next_step": _bounded(model_value.get("next_step"), 400),
                }
        elif self.model_busy():
            report["model_deferred"] = True
        with self._db() as db:
            db.execute(
                "INSERT INTO investigations(id, created_at, alert_id, status, payload_json) VALUES (?, ?, ?, ?, ?)",
                (report["id"], report["created_at"], alert_id, report["status"], json.dumps(report, ensure_ascii=False)),
            )
        self.emit({"type": "security:update", "status": "ready"})
        return {"ok": True, "investigation": report}
