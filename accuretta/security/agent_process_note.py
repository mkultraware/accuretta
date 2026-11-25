# SPDX-License-Identifier: AGPL-3.0-only
"""Recording a process the agent started, so later stop and kill checks know it is the
agent's own.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import subprocess  # only for the type hint
import time
from accuretta.core.processes import _proc_create_time
from accuretta.security.agent_processes import _AGENT_PROCS
from accuretta.security.agent_processes import _AGENT_PROCS_LOCK
from accuretta.security.agent_processes import _agent_job


_AGENT_PROCS_MAX = 2000


def _note_agent_process(proc: "subprocess.Popen") -> None:
    """Remember a process the agent's own tools just started."""
    try:
        _agent_job.assign(proc)
        created = _proc_create_time(proc.pid) or time.time()
        with _AGENT_PROCS_LOCK:
            _AGENT_PROCS[int(proc.pid)] = [created, None]
            if len(_AGENT_PROCS) > _AGENT_PROCS_MAX:
                ended = sorted((v[1], k) for k, v in _AGENT_PROCS.items() if v[1] is not None)
                for _, old in ended[:len(_AGENT_PROCS) - _AGENT_PROCS_MAX]:
                    _AGENT_PROCS.pop(old, None)
    except Exception:
        pass


def _note_agent_process_end(pid: int) -> None:
    with _AGENT_PROCS_LOCK:
        entry = _AGENT_PROCS.get(int(pid))
        if entry is not None and entry[1] is None:
            entry[1] = time.time()
