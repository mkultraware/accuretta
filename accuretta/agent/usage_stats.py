# SPDX-License-Identifier: AGPL-3.0-only
"""The usage statistics page: the model dataset, error log, fold events and pushed
repos, summed for the Usage view.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import re
import time
from accuretta.core.chats import get_chats
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.safe_fs import safe_is_dir
from accuretta.core.savings import _savings_get
from accuretta.core.settings import get_settings
from accuretta.runtime.model_advisor import _HARNESS_INTERVENTION_KEYS
from accuretta.runtime.model_advisor import _model_observed
from accuretta.runtime.model_advisor import _model_runtime_profiles
from accuretta.runtime.model_advisor import _sanitize_interventions
from accuretta.runtime.model_config import _models_config
from accuretta.runtime.model_files import _is_mmproj_name
from accuretta.runtime.model_files import scan_gguf_dir
from error_log import ErrorStore
from pathlib import Path


from accuretta.core.data_files import (  # noqa: F401  re-export: moved 2026-11-23
    MODEL_DATASET_FILE,
    PUSHED_REPOS_FILE,
    FOLD_EVENTS_FILE,
)


from accuretta.core.errors import (  # noqa: F401  re-export: moved 2026-11-23
    ERRORS_FILE,
    _error_store,
)


def _usage_stats() -> dict:
    """Return content-free aggregates for the local Usage stats page."""
    settings = get_settings()
    telemetry_enabled = bool(settings.get("passive_model_telemetry", True))
    profiles = _model_runtime_profiles().get("models") or {}
    models_dir = str(settings.get("models_dir") or "").strip()
    installed_models: set[str] = set()
    known_paths = set(_models_config())
    known_paths.add(str(settings.get("model_path") or ""))
    if models_dir and safe_exists(models_dir) and safe_is_dir(models_dir):
        for item in scan_gguf_dir(models_dir):
            known_paths.add(str(item.get("path") or ""))
    for model_path in known_paths:
        path = Path(model_path)
        if (model_path and path.suffix.lower() == ".gguf" and safe_exists(path)
                and not safe_is_dir(path) and not _is_mmproj_name(path.name)):
            installed_models.add(path.name.casefold())
            installed_models.add(path.stem.casefold())
    # --- model scoring (two passes) -------------------------------------
    # Score = 0.40 * tool_success + 0.30 * completion + 0.15 * speed
    #         + 0.15 * evidence.
    # Rates are Bayesian-smoothed toward the volume-weighted fleet mean. The
    # evidence term (turn count, diminishing returns) is what keeps a lucky
    # 5-turn 100% run from outranking a 200-turn 95% regular; smoothing alone
    # can't, since the regular sets the fleet mean. Raw rates are still reported.
    _raw_models = []
    for name, row in profiles.items():
        if not isinstance(row, dict):
            continue
        if str(name).casefold() not in installed_models:
            continue
        turns = max(0, int(row.get("turns", 0) or 0))
        if turns < 1:
            continue
        observed = row.get("observed") or _model_observed(row)
        calls = max(0, int(row.get("tool_calls", 0) or 0))
        completion = float(observed.get("completion_rate", 0) or 0)
        tool_success = (1.0 - float(observed.get("tool_error_rate", 0) or 0)) if calls else None
        _raw_models.append({
            "model": str(name)[:180],
            "row": row,
            "observed": observed,
            "turns": turns,
            "completed_turns": max(0, int(row.get("completed_turns", 0) or 0)),
            "completion": min(1.0, max(0.0, completion)),
            "calls": calls,
            "tool_success": None if tool_success is None else min(1.0, max(0.0, tool_success)),
            "speed": max(0.0, float(observed.get("output_tok_s", 0) or 0)),
        })
    _fleet_turns = sum(m["turns"] for m in _raw_models)
    _prior_completion = (sum(m["completion"] * m["turns"] for m in _raw_models) / _fleet_turns
                         if _fleet_turns else 0.8)
    _rated_calls = [(m["tool_success"], m["calls"]) for m in _raw_models if m["tool_success"] is not None]
    _prior_tool = (sum(s * c for s, c in _rated_calls) / sum(c for _, c in _rated_calls)
                   if _rated_calls and sum(c for _, c in _rated_calls) else 0.8)
    _prior_speed = (sum(m["speed"] * m["turns"] for m in _raw_models) / _fleet_turns
                    if _fleet_turns else 0.0)
    _TOOL_PRIOR_CALLS = 50.0
    _COMP_PRIOR_TURNS = 10.0
    _SPEED_PRIOR_TURNS = 5.0
    for m in _raw_models:
        m["smooth_completion"] = ((m["completion"] * m["turns"] + _prior_completion * _COMP_PRIOR_TURNS)
                                  / (m["turns"] + _COMP_PRIOR_TURNS))
        if m["tool_success"] is None:
            m["smooth_tool"] = _prior_tool
        else:
            m["smooth_tool"] = ((m["tool_success"] * m["calls"] + _prior_tool * _TOOL_PRIOR_CALLS)
                                / (m["calls"] + _TOOL_PRIOR_CALLS))
        m["smooth_speed"] = ((m["speed"] * m["turns"] + _prior_speed * _SPEED_PRIOR_TURNS)
                             / (m["turns"] + _SPEED_PRIOR_TURNS))
    _speeds = [m["smooth_speed"] for m in _raw_models]
    _smin, _smax = (min(_speeds), max(_speeds)) if _speeds else (0.0, 0.0)
    _sspread = _smax - _smin
    models = []
    for m in _raw_models:
        turns = m["turns"]
        speed_norm = 1.0 if _sspread <= 0 else (m["smooth_speed"] - _smin) / _sspread
        evidence = turns / (turns + 10.0)  # 5->0.33, 25->0.71, 200->0.95
        reliability = (0.40 * m["smooth_tool"] + 0.30 * m["smooth_completion"]
                       + 0.15 * speed_norm + 0.15 * evidence)
        models.append({
            "model": m["model"],
            "turns": turns,
            "completed_turns": m["completed_turns"],
            "completion_rate": round(m["completion"], 3),
            "tool_calls": m["calls"],
            "tool_success": round(m["tool_success"], 3) if m["tool_success"] is not None else None,
            "output_tok_s": float(m["observed"].get("output_tok_s", 0) or 0),
            "avg_elapsed_ms": round(max(0.0, float(m["row"].get("elapsed_s", 0) or 0)) * 1000 / turns, 1),
            "compactions": max(0, int(m["observed"].get("compactions", 0) or 0)),
            "reliability": round(min(1.0, max(0.0, reliability)), 3),
            "confidence": "high" if turns >= 25 else "medium" if turns >= 8 else "warming_up",
            "ranked": turns >= 5,
        })
    models.sort(key=lambda item: (item["ranked"], item["reliability"], item["turns"]), reverse=True)

    chat_data = get_chats()
    chat_titles = {}
    repo_keys = set()
    repo_names = set()
    repo_sessions = 0
    for chat_id, chat in (chat_data.get("chats") or {}).items():
        if not isinstance(chat, dict):
            continue
        session = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:12]
        chat_titles[session] = str(chat.get("title") or "Untitled task")[:180]
        worktree = chat.get("github_worktree")
        if not isinstance(worktree, dict):
            continue
        owner = str(worktree.get("owner") or "").strip()
        repo = str(worktree.get("repo") or "").strip()
        if not repo:
            continue
        key = f"{owner}/{repo}" if owner else repo
        repo_keys.add(key.casefold())
        repo_names.add(key)
        repo_sessions += 1
    # Repos pushed to from the app count too — not just branch sessions
    # started through the GitHub tab. Deduped against worktree repos.
    try:
        _pushed = load_json(PUSHED_REPOS_FILE, {})
        _pushed_repos = _pushed.get("repos") if isinstance(_pushed, dict) else None
        if isinstance(_pushed_repos, dict):
            for _k, _e in _pushed_repos.items():
                _name = _e.get("name") if isinstance(_e, dict) else None
                _disp = str(_name or _k or "").strip()[:180]
                if not _disp:
                    continue
                repo_keys.add(_disp.casefold())
                repo_names.add(_disp)
    except Exception:
        pass
    try:
        base_unique = max(0, int(settings.get("repos_baseline_unique", 0) or 0))
    except (TypeError, ValueError):
        base_unique = 0
    try:
        base_sessions = max(0, int(settings.get("repos_baseline_sessions", 0) or 0))
    except (TypeError, ValueError):
        base_sessions = 0

    daily = {}
    longest = None
    dataset_turns = 0
    dataset_input = 0
    dataset_output = 0
    dataset_elapsed = 0.0
    interventions = {key: 0 for key in _HARNESS_INTERVENTION_KEYS}
    try:
        with open(MODEL_DATASET_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue
                day = str(item.get("day") or "")[:10]
                prompt = max(0, int(item.get("prompt_tokens", 0) or 0))
                output = max(0, int(item.get("output_tokens", 0) or 0))
                elapsed = max(0.0, float(item.get("elapsed_ms", 0) or 0))
                dataset_turns += 1
                dataset_input += prompt
                dataset_output += output
                dataset_elapsed += elapsed
                for key, count in _sanitize_interventions(item.get("interventions")).items():
                    interventions[key] += count
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                    bucket = daily.setdefault(day, {"day": day, "turns": 0, "tokens": 0, "elapsed_ms": 0.0})
                    bucket["turns"] += 1
                    bucket["tokens"] += prompt + output
                    bucket["elapsed_ms"] += elapsed
                item_model = str(item.get("model") or "Unknown model")[:180]
                model_is_installed = item_model.casefold() in installed_models
                if model_is_installed and (longest is None or elapsed > longest["elapsed_ms"]):
                    session = str(item.get("session") or "")[:16]
                    longest = {
                        "elapsed_ms": round(elapsed, 1),
                        "model": item_model,
                        "day": day,
                        "title": chat_titles.get(session) or "Chat no longer available",
                        "chat_available": session in chat_titles,
                    }
    except (FileNotFoundError, OSError):
        pass

    today = datetime.date.today()
    chart_days = []
    for offset in range(29, -1, -1):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        bucket = daily.get(day) or {"day": day, "turns": 0, "tokens": 0, "elapsed_ms": 0.0}
        chart_days.append({**bucket, "elapsed_ms": round(float(bucket["elapsed_ms"]), 1)})

    activity_days = []
    for offset in range(364, -1, -1):
        day = (today - datetime.timedelta(days=offset)).isoformat()
        bucket = daily.get(day) or {"day": day, "turns": 0, "tokens": 0, "elapsed_ms": 0.0}
        activity_days.append({**bucket, "elapsed_ms": round(float(bucket["elapsed_ms"]), 1)})

    fold = {"attempts": 0, "successful": 0, "failed": 0, "skipped": 0,
            "folded_messages": 0, "folded_tokens": 0}
    benign = {"tail_too_small", "no_messages", "already_compacted", "below_threshold", "busy"}
    try:
        # utf-8-sig: this append-only file has been seen with a BOM, which makes the
        # first json.loads fail and (errors being swallowed) under-counts every session.
        with open(FOLD_EVENTS_FILE, "r", encoding="utf-8-sig") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue
                fold["attempts"] += 1
                if item.get("ok"):
                    fold["successful"] += 1
                    fold["folded_messages"] += max(0, int(item.get("folded", 0) or 0))
                    fold["folded_tokens"] += max(0, int(item.get("folded_tokens", 0) or 0))
                elif str(item.get("outcome") or "") in benign:
                    fold["skipped"] += 1
                else:
                    fold["failed"] += 1
    except (FileNotFoundError, OSError):
        pass

    savings = _savings_get()
    return {
        "generated_at": int(time.time()),
        "telemetry_enabled": telemetry_enabled,
        "privacy": "Local aggregate counters only. Prompts, replies, paths and tool results are not included.",
        "models": models,
        "daily": chart_days,
        "activity": {"days": activity_days, "timezone": "host local time"},
        "totals": {
            "turns": max(dataset_turns, max(0, int(savings.get("turns", 0) or 0))),
            "input_tokens": max(dataset_input, max(0, int(savings.get("tok_in", 0) or 0))),
            "output_tokens": max(dataset_output, max(0, int(savings.get("tok_out", 0) or 0))),
            "elapsed_ms": round(dataset_elapsed, 1),
            "since": max(0, int(savings.get("since", 0) or 0)),
        },
        "savings": savings,
        "compression": fold,
        "interventions": {
            **interventions,
            "total": sum(interventions.values()),
        },
        "repos": {
            "unique": len(repo_keys) + base_unique,
            "sessions": repo_sessions + base_sessions,
            "names": sorted(repo_names, key=str.casefold)[:12],
        },
        "longest": longest,
        "errors": _error_store.summary(),
    }

