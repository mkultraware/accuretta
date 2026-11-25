# SPDX-License-Identifier: AGPL-3.0-only
"""The error log: where caught errors are recorded for the Usage page and bug reports.

Moved out of accuretta/agent/usage_stats.py unchanged (it came from bridge.py);
both re-export every name here.
"""
from __future__ import annotations

from accuretta.core.paths import DATA
from error_log import ErrorStore


ERRORS_FILE = DATA / "errors.jsonl"


_error_store = ErrorStore(ERRORS_FILE)
