# SPDX-License-Identifier: AGPL-3.0-only
"""Work out which process a stop command means: PID files and variables set by the same command.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _stop_pid_file_values(tok: str, code: str, lits: list[str], cwd: str | None) -> list[int] | None:
    """Process ids read from a file the command names (`$pids = @(Get-Content
    app.pid); foreach ($p in $pids) { Stop-Process -Id $p }`): the ids in that
    file, or None when the variable isn't filled that way."""
    m = re.match(r"\$(\w+)", tok or "")
    if not m:
        return None
    var = m.group(1)
    loop = re.search(rf"foreach\s*\(\s*\${re.escape(var)}\s+in\s+\$(\w+)\s*\)", code, re.IGNORECASE)
    if loop:
        var = loop.group(1)
    src = re.search(rf"\${re.escape(var)}\s*=\s*@?\(?\s*(?:get-content|gc|cat|type)\s+(?:-path\s+)?(\"\d+\"|[^\s;|)]+)",
                    code, re.IGNORECASE)
    if not src:
        return None
    raw = src.group(1)
    path = lits[int(raw[1:-1])] if re.fullmatch(r'"\d+"', raw) else raw
    if not re.match(r"^[a-zA-Z]:[\\/]|^\\\\", path):
        if not cwd:
            return None
        path = os.path.join(cwd, path)
    try:
        if os.path.getsize(path) > 4096:
            return None
        values = re.findall(r"\d+", Path(path).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []                              # no file: nothing to stop
    return [int(v) for v in values[:32]]


def _stop_var_ok(token: str, code: str, ports: list[int]) -> bool:
    """A variable naming the process: fine when the same command started it
    (`$p = Start-Process ... -PassThru`), or it is the owner of a port the
    command looks up (`$_.OwningProcess` after Get-NetTCPConnection)."""
    m = re.match(r"\$\(?\s*(\$?\w+)", token)
    var = (m.group(1) if m else token).lstrip("$").lower()
    if var in ("_", "psitem"):
        port = re.search(r"get-nettcpconnection\b[^|;\n]*-localport\s+(\d+)", code, re.IGNORECASE)
        if port and "owningprocess" in token.lower():
            ports.append(int(port.group(1)))
            return True
        return False
    return bool(re.search(rf"\${re.escape(var)}\s*=\s*start-process\b[^\n;]*-passthru", code, re.IGNORECASE))
