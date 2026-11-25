# SPDX-License-Identifier: AGPL-3.0-only
"""Commands the agent is running per chat, and killing the current one when the user
presses Stop.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import subprocess
import threading
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.core.cancels import _chat_cancels
from accuretta.core.cancels import _chat_cancels_lock


# The shell command currently running per chat, so the UI's kill button can
# terminate just that process while the turn continues (the tool returns a
# killed result). Tools run sequentially, so one live command per chat.
_running_cmds_lock = threading.Lock()


_running_cmds: dict[str, subprocess.Popen] = {}


_running_cmd_targets: dict[str, str] = {}


# Procs terminated via the kill button. Windows TerminateProcess sets exit code
# 1 (indistinguishable from a real exit 1 by returncode), so we mark the kill
# explicitly and read it back in _run_powershell.
_killed_procs: set = set()


def _kill_current_command(chat_id: str) -> bool:
    """Kill the shell command currently running for this chat, if any."""
    with _running_cmds_lock:
        proc = _running_cmds.get(chat_id or "")
        target = _running_cmd_targets.get(chat_id or "", "host")
        if proc is not None:
            _killed_procs.add(proc)
    if proc is None:
        return False
    try:
        if _terminate_process_tree(proc, target):
            return True
        raise RuntimeError("process tree did not stop")
    except Exception:
        with _running_cmds_lock:
            _killed_procs.discard(proc)
        return False


def cancel_chat(chat_id: str) -> bool:
    """Flip the cancel flag and force-close the active llama-server response
    for this chat. Returns True if something was cancelled."""
    with _chat_cancels_lock:
        entry = _chat_cancels.get(chat_id)
        if not entry:
            return False
        entry["cancel"].set()
        resp = entry.get("resp")
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
        try:
            # reach through urllib to the raw socket and hard-shut it so
            # llama-server notices within one token's worth of time.
            fp = getattr(resp, "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
            if sock:
                import socket as _sock
                try:
                    sock.shutdown(_sock.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass
    # A tool can still be running when the model reply is cut (an nmap scan in
    # the WSL guest ran out its full 600s timeout after Stop). Kill it too. A
    # sandbox kill restarts the guest and can take ~30s, so keep it off the
    # HTTP thread that answers Stop.
    threading.Thread(target=_kill_current_command, args=(chat_id,),
                     name="stop-kill", daemon=True).start()
    return True
