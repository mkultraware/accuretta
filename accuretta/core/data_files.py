# SPDX-License-Identifier: AGPL-3.0-only
"""Data files the app appends to: the content-free model usage dataset, fold events, and the repos the user pushed to.

Moved out of accuretta/agent/usage_stats.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

from accuretta.core.paths import DATA


MODEL_DATASET_FILE = DATA / "model_usage_dataset.jsonl"   # append-only, content-free turn/tool log (gitignored via data/)


PUSHED_REPOS_FILE = DATA / "pushed_repos.json"   # repos the user pushed to from the app (remote URL -> owner/name)


FOLD_EVENTS_FILE = DATA / "fold_events.jsonl"
