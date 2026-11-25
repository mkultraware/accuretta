# SPDX-License-Identifier: AGPL-3.0-only
"""Processes the agent started itself: the registry, the kill-on-close job, the ports
they serve, and the llama-server port.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import threading
from accuretta.core.endpoints import LLAMA
from accuretta.core.endpoints import PORT
from accuretta.core.jobs import _KillOnCloseJob
from accuretta.core.processes import _local_listeners
from accuretta.core.processes import _proc_create_time
from accuretta.core.processes import _process_parent_map


_AGENT_PROCS: dict[int, list] = {}          # pid -> [created (epoch s), ended (epoch s) or None]


_AGENT_PROCS_LOCK = threading.Lock()


def _started_by_agent(pid: int, parents: dict[int, int], agents: dict[int, list],
                      created=_proc_create_time) -> bool:
    """Did one of the agent's own tools start the running process `pid`
    (itself, or anywhere up its chain of parents)?"""
    if _agent_job.contains(pid):
        return True
    me = os.getpid()
    child, child_ct = int(pid), created(pid)
    for _ in range(16):
        if child_ct is None or child == me:
            return False
        entry = agents.get(child)
        if entry and abs(entry[0] - child_ct) < 1.0:
            return True
        parent = parents.get(child)
        if not parent or parent == child:
            return False
        parent_ct = created(parent)
        entry = agents.get(parent)
        if (entry and child_ct + 1.0 >= entry[0]
                and (entry[1] is None or child_ct <= entry[1] + 1.0)
                # the id may since belong to a newer process; the child must predate it
                and (parent_ct is None or abs(parent_ct - entry[0]) < 1.0 or child_ct < parent_ct)):
            return True
        if parent_ct is None or parent_ct > child_ct + 1.0:
            return False                       # parent gone, or its id reused after the child began
        child, child_ct = parent, parent_ct
    return False


def _agent_served_ports() -> set[int]:
    """Local ports whose server one of the agent's own tools started.
    Accuretta's own port and the model server's port never count."""
    with _AGENT_PROCS_LOCK:
        agents = {pid: list(v) for pid, v in _AGENT_PROCS.items()}
    if not agents:
        return set()
    listeners = _local_listeners()
    if not listeners:
        return set()
    parents = _process_parent_map()
    excluded = {int(PORT), _parse_llama_port()}
    found: set[int] = set()
    checked: dict[int, bool] = {}
    for port, pid in listeners:
        if port in excluded or port in found:
            continue
        if pid not in checked:
            checked[pid] = _started_by_agent(pid, parents, agents)
        if checked[pid]:
            found.add(port)
    return found


def _parse_llama_port() -> int:
    m = re.search(r":(\d+)", LLAMA)
    return int(m.group(1)) if m else 8080


# Every process the agent's own tools start (run_powershell, shell sessions,
# open_program, run_tests). No limits: it only groups them, so whatever they
# start belongs to it too, even after the shell in between has exited, and a
# reused process id can't fake membership. BREAKAWAY_OK keeps programs that
# ask to leave a job working exactly as before.
_agent_job = _KillOnCloseJob(limit_flags=_KillOnCloseJob._BREAKAWAY_OK, quiet=True)
