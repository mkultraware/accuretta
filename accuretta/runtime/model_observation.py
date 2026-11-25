# SPDX-License-Identifier: AGPL-3.0-only
"""Recording what each turn showed about the loaded model (speed, tool use, errors) for
the model advisor.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import json
import time
from accuretta.core.data_files import MODEL_DATASET_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.runtime.llama_process import _llama_props_ctx
from accuretta.runtime.model_advisor import MODEL_RUNTIME_FILE
from accuretta.runtime.model_advisor import _MODEL_RUNTIME_LOCK
from accuretta.runtime.model_advisor import _model_config_signature
from accuretta.runtime.model_advisor import _model_profile_key
from accuretta.runtime.model_advisor import _model_runtime_config
from accuretta.runtime.model_advisor import _sanitize_interventions


_MODEL_ADVISOR_OBSERVATION_CAP = 80


def _record_model_observation(model: str, *, prompt_tokens: int = 0,
                              output_tokens: int = 0, rounds: int = 0,
                              tool_calls: int = 0, tool_errors: int = 0,
                              peak_prompt_tokens: int = 0,
                              elapsed_s: float = 0.0, native_fallback: bool = False,
                              completed: bool = True, chat_id: str = "",
                              prompt_ms: float = 0.0, eval_ms: float = 0.0,
                              draft_n: int = 0, draft_accepted: int = 0,
                              compactions: int = 0,
                              compaction_failures: int = 0,
                              tool_outcomes: list | None = None,
                              interventions: dict | None = None,
                              end_reason: str = "") -> None:
    """Local-only, content-free observations from normal use.

    This replaces a mandatory per-model benchmark. It costs no extra inference,
    stores no prompts/results, and can support later recommendations once enough
    real turns have accumulated.  Each turn is also appended to the long-term
    comparison dataset (MODEL_DATASET_FILE, JSONL): per-turn counters plus
    content-free per-tool-call outcomes (tool name, ok flag, exit code, refusal)
    so models can be compared across weeks of real usage.
    """
    try:
        settings = get_settings()
        if not settings.get("passive_model_telemetry", True):
            return
    except Exception:
        return
    key = _model_profile_key(model)
    config = _model_runtime_config(settings)
    config_key = _model_config_signature(config)
    intervention_counts = _sanitize_interventions(interventions)
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    # A missing chat id contributes no session attribution instead of collapsing
    # a whole day into one fake "session".
    session_key = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:12] if chat_id else ""
    with _MODEL_RUNTIME_LOCK:
        data = load_json(MODEL_RUNTIME_FILE, {"version": 2, "models": {}})
        if not isinstance(data, dict):
            data = {"version": 2, "models": {}}
        data["version"] = max(2, int(data.get("version", 1) or 1))
        models = data.setdefault("models", {})
        row = models.setdefault(key, {
            "turns": 0, "completed_turns": 0, "prompt_tokens": 0,
            "output_tokens": 0, "rounds": 0, "tool_calls": 0,
            "tool_errors": 0, "elapsed_s": 0.0, "native_fallback_turns": 0,
            "peak_context_tokens": 0, "peak_context_sum": 0,
        })
        row["turns"] = int(row.get("turns", 0)) + 1
        row["completed_turns"] = int(row.get("completed_turns", 0)) + int(bool(completed))
        row["prompt_tokens"] = int(row.get("prompt_tokens", 0)) + max(0, int(prompt_tokens or 0))
        row["output_tokens"] = int(row.get("output_tokens", 0)) + max(0, int(output_tokens or 0))
        row["rounds"] = int(row.get("rounds", 0)) + max(0, int(rounds or 0))
        row["tool_calls"] = int(row.get("tool_calls", 0)) + max(0, int(tool_calls or 0))
        row["tool_errors"] = int(row.get("tool_errors", 0)) + max(0, int(tool_errors or 0))
        _peak = max(0, int(peak_prompt_tokens or 0))
        row["peak_context_tokens"] = max(int(row.get("peak_context_tokens", 0) or 0), _peak)
        row["peak_context_sum"] = int(row.get("peak_context_sum", 0) or 0) + _peak
        row["elapsed_s"] = round(float(row.get("elapsed_s", 0.0)) + max(0.0, float(elapsed_s or 0)), 3)
        row["prefill_ms"] = round(float(row.get("prefill_ms", 0.0)) + max(0.0, float(prompt_ms or 0)), 3)
        row["eval_ms"] = round(float(row.get("eval_ms", 0.0)) + max(0.0, float(eval_ms or 0)), 3)
        row["compactions"] = int(row.get("compactions", 0) or 0) + max(0, int(compactions or 0))
        row["compaction_failures"] = int(row.get("compaction_failures", 0) or 0) + max(0, int(compaction_failures or 0))
        row_interventions = row.setdefault("interventions", {})
        for intervention, count in intervention_counts.items():
            row_interventions[intervention] = int(row_interventions.get(intervention, 0) or 0) + count
        row["native_fallback_turns"] = int(row.get("native_fallback_turns", 0)) + int(bool(native_fallback))
        row["last_seen"] = now
        row["last_context"] = int(_llama_props_ctx() or settings.get("num_ctx") or 0)
        row["active_config"] = config_key

        configs = row.setdefault("configs", {})
        cfg = configs.setdefault(config_key, {
            "settings": config, "first_seen": now, "turns": 0,
            "completed_turns": 0, "prompt_tokens": 0, "output_tokens": 0,
            "rounds": 0, "tool_calls": 0, "tool_errors": 0,
            "elapsed_s": 0.0, "prefill_ms": 0.0, "eval_ms": 0.0,
            "native_fallback_turns": 0, "peak_context_tokens": 0,
            "peak_context_sum": 0, "compactions": 0,
            "compaction_failures": 0, "sessions": [], "days": [],
            "clients": {}, "observations": [],
        })
        cfg["settings"] = config
        cfg["turns"] = int(cfg.get("turns", 0) or 0) + 1
        cfg["completed_turns"] = int(cfg.get("completed_turns", 0) or 0) + int(bool(completed))
        cfg["prompt_tokens"] = int(cfg.get("prompt_tokens", 0) or 0) + max(0, int(prompt_tokens or 0))
        cfg["output_tokens"] = int(cfg.get("output_tokens", 0) or 0) + max(0, int(output_tokens or 0))
        cfg["rounds"] = int(cfg.get("rounds", 0) or 0) + max(0, int(rounds or 0))
        cfg["tool_calls"] = int(cfg.get("tool_calls", 0) or 0) + max(0, int(tool_calls or 0))
        cfg["tool_errors"] = int(cfg.get("tool_errors", 0) or 0) + max(0, int(tool_errors or 0))
        cfg["elapsed_s"] = round(float(cfg.get("elapsed_s", 0.0)) + max(0.0, float(elapsed_s or 0)), 3)
        cfg["prefill_ms"] = round(float(cfg.get("prefill_ms", 0.0)) + max(0.0, float(prompt_ms or 0)), 3)
        cfg["eval_ms"] = round(float(cfg.get("eval_ms", 0.0)) + max(0.0, float(eval_ms or 0)), 3)
        cfg["native_fallback_turns"] = int(cfg.get("native_fallback_turns", 0) or 0) + int(bool(native_fallback))
        cfg["peak_context_tokens"] = max(int(cfg.get("peak_context_tokens", 0) or 0), _peak)
        cfg["peak_context_sum"] = int(cfg.get("peak_context_sum", 0) or 0) + _peak
        cfg["compactions"] = int(cfg.get("compactions", 0) or 0) + max(0, int(compactions or 0))
        cfg["compaction_failures"] = int(cfg.get("compaction_failures", 0) or 0) + max(0, int(compaction_failures or 0))
        cfg_interventions = cfg.setdefault("interventions", {})
        for intervention, count in intervention_counts.items():
            cfg_interventions[intervention] = int(cfg_interventions.get(intervention, 0) or 0) + count
        cfg["last_seen"] = now
        cfg["last_context"] = row["last_context"]
        sessions = [str(x)[:16] for x in (cfg.get("sessions") or []) if x]
        if session_key and session_key not in sessions:
            sessions.append(session_key)
        cfg["sessions"] = sessions[-24:]
        days = [str(x)[:10] for x in (cfg.get("days") or []) if x]
        if day not in days:
            days.append(day)
        cfg["days"] = days[-30:]
        client = _client_context_by_chat.get(chat_id, {}) if chat_id else {}
        client_key = f"{client.get('os') or 'unknown'}:{'remote' if client.get('remote') else 'local'}"
        clients = cfg.setdefault("clients", {})
        clients[client_key] = int(clients.get(client_key, 0) or 0) + 1
        obs = cfg.setdefault("observations", [])
        obs.append({
            "ts": now, "completed": bool(completed),
            "prompt_tokens": max(0, int(prompt_tokens or 0)),
            "output_tokens": max(0, int(output_tokens or 0)),
            "peak_context_tokens": _peak, "rounds": max(0, int(rounds or 0)),
            "tool_calls": max(0, int(tool_calls or 0)),
            "tool_errors": max(0, int(tool_errors or 0)),
            "elapsed_ms": round(max(0.0, float(elapsed_s or 0)) * 1000.0, 2),
            "prefill_ms": round(max(0.0, float(prompt_ms or 0)), 2),
            "eval_ms": round(max(0.0, float(eval_ms or 0)), 2),
            "draft_n": max(0, int(draft_n or 0)),
            "draft_accepted": max(0, int(draft_accepted or 0)),
            "native_fallback": bool(native_fallback),
            "compactions": max(0, int(compactions or 0)),
            "compaction_failures": max(0, int(compaction_failures or 0)),
            "interventions": intervention_counts,
        })
        cfg["observations"] = obs[-_MODEL_ADVISOR_OBSERVATION_CAP:]
        _append_model_dataset({
            "v": 1,
            "ts": now,
            "day": day,
            "model": key,
            "config": config_key,
            "session": session_key,
            "client": client_key,
            "completed": bool(completed),
            "rounds": max(0, int(rounds or 0)),
            "prompt_tokens": max(0, int(prompt_tokens or 0)),
            "output_tokens": max(0, int(output_tokens or 0)),
            "elapsed_ms": round(max(0.0, float(elapsed_s or 0)) * 1000.0, 2),
            "prefill_ms": round(max(0.0, float(prompt_ms or 0)), 2),
            "eval_ms": round(max(0.0, float(eval_ms or 0)), 2),
            "peak_context_tokens": _peak,
            "native_fallback": bool(native_fallback),
            "compactions": max(0, int(compactions or 0)),
            "compaction_failures": max(0, int(compaction_failures or 0)),
            "tool_calls": max(0, int(tool_calls or 0)),
            "tool_errors": max(0, int(tool_errors or 0)),
            "interventions": intervention_counts,
            "end_reason": end_reason if end_reason in _TURN_END_REASONS else "",
            "tools": _sanitize_tool_outcomes(tool_outcomes),
        })
        save_json(MODEL_RUNTIME_FILE, data)


def _sanitize_tool_outcomes(tool_outcomes) -> list:
    """Reduce raw tool results to content-free {name, ok, exit_code, refused}.

    Tool arguments and result bodies (file contents, paths, error text) are
    never copied — only the call's identity and its binary/numeric outcome.
    """
    out = []
    try:
        source = tool_outcomes if isinstance(tool_outcomes, list) else []
        for item in source[-_MODEL_ADVISOR_OBSERVATION_CAP:]:
            if not isinstance(item, dict):
                continue
            entry = {
                "name": str(item.get("name") or "unknown")[:80],
                "ok": bool(item.get("ok", True)),
                "exit_code": int(item.get("exit_code", 0) or 0),
                "refused": bool(item.get("refused", False)),
            }
            kind = str(item.get("error_kind") or "")
            if kind in _TOOL_ERROR_KINDS:
                entry["error_kind"] = kind
            out.append(entry)
    except Exception:
        return []
    return out


# Content-free reasons a tool call failed, for the usage dataset: a fixed label,
# never the error text (which can hold paths, hosts or file contents). Without
# them an audit of 24,682 calls could see that write_file failed 20% of the time
# but not why.
_TOOL_ERROR_PATTERNS = (
    ("denied", r"user denied|denied action|not approved|approval (?:timed out|expired)"),
    ("rewrite_loop", r"rewrite loop|loop guard|loop_breaker|refused repeated|identical call"),
    ("blocked_path", r"path blocked|protected path|data folder|outside (?:the )?workspace|\.accurettaignore"),
    ("refused", r"^refused|not allowed|disabled|allowlist|not in scope|out of scope"),
    ("edit_mismatch", r"not found in file|no match|old_string|closest lines"),
    ("not_found", r"not a file|no such file|not found|does not exist|cannot find|unknown (?:tool|bundle)"),
    ("bad_arguments", r"required|must be|invalid|missing|expected|unknown key|malformed"),
    ("timeout", r"timed out|timeout"),
    ("http_4xx", r"\bhttp (?:error )?4\d\d\b|\b4\d\d (?:client error|forbidden|not found|unauthorized)"),
    ("http_5xx", r"\bhttp (?:error )?5\d\d\b|\b5\d\d server error"),
    ("network", r"connection|unreachable|name resolution|getaddrinfo|ssl|certificate"),
    ("vision", r"vision|mmproj|projector"),
    ("syntax", r"syntaxerror|syntax error|parse error|e999"),
)


_TOOL_ERROR_KINDS = {kind for kind, _ in _TOOL_ERROR_PATTERNS} | {"nonzero_exit", "other"}


# Content-free reason a turn ended, for the usage dataset ("" on older rows).
# 110 of 1,694 recorded turns never completed and nothing said why.
_TURN_END_REASONS = {"ok", "round_limit", "thinking_stall", "no_answer", "error", "stopped", "failed"}


def _append_model_dataset(record: dict) -> None:
    """Append one content-free turn record to the long-term comparison dataset.

    JSONL on purpose: append-only, crash-safe, and trivially tail/aggregate-able
    later.  The file lives under ./data so it is gitignored with everything else
    user-local.  Never allowed to raise — a broken disk must not kill a turn.
    """
    try:
        MODEL_DATASET_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MODEL_DATASET_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception:
        pass


_client_context_by_chat: dict[str, dict] = {}
