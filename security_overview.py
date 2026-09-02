"""Local, read-only security overview service.

The service owns persistence, deterministic triage, whitelisting, and bounded
investigations. Host collection and model inference are injected by bridge.py
so this module never imports the agent runtime or gains access to its tools.
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
    ) -> None:
        self.db_path = Path(db_path)
        self.collectors = dict(collectors)
        self.summarizer = summarizer
        self.model_busy = model_busy or (lambda: False)
        self.emit = emit or (lambda _event: None)
        self._state_lock = threading.RLock()
        self._scan_thread: threading.Thread | None = None
        self._scan_state = {"status": "idle", "stage": "", "started_at": None, "error": ""}
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

    def _scan_worker(self) -> None:
        sources: dict[str, dict] = {}
        try:
            labels = {
                "network": "Reading network state",
                "system": "Reading system events",
                "application": "Reading application events",
                "security": "Checking security events",
                "persistence": "Checking startup and persistence",
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

        actions = sources.get("actions", {})
        failures = [
            row for row in actions.get("entries") or []
            if str(row.get("status") or "").lower() in {"error", "failed", "timeout"}
        ]
        if len(failures) >= 3:
            alerts.append(self._alert(
                "accuretta", "action_failures", "low",
                f"{len(failures)} recent Accuretta actions did not complete",
                "Denied, blocked, and failed actions remain local and may indicate a configuration or scope problem.",
                "accuretta:action-failures", "Accuretta action history", failures[:10],
            ))

        unique: dict[str, dict] = {}
        for alert in alerts:
            current = unique.get(alert["id"])
            if not current or _SEVERITY_RANK[alert["severity"]] > _SEVERITY_RANK[current["severity"]]:
                unique[alert["id"]] = alert
        alerts = sorted(
            unique.values(),
            key=lambda item: (_SEVERITY_RANK[item["severity"]], item.get("first_seen", "")),
            reverse=True,
        )
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
                "action_records": len(actions.get("entries") or []),
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
        return {
            "generated_at": None,
            "window_hours": 24,
            "coverage": [],
            "timeline": [],
            "alerts": [],
            "whitelisted_activity": [],
            "whitelist": self._whitelist_rows(),
            "investigations": self._investigation_rows(12),
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
