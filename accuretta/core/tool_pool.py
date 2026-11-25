# SPDX-License-Identifier: AGPL-3.0-only
"""The thread pool long-running tools run on, so the web server stays responsive, and
the table of async tool jobs.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor


# thread pool for long-running tools so the HTTP worker stays responsive
_tool_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-")


# async tool jobs (for POST /api/tools/call returning job-id)
_tool_jobs: dict[str, dict] = {}


_tool_jobs_lock = threading.Lock()
