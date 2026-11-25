# SPDX-License-Identifier: AGPL-3.0-only
"""Finding a working yt-dlp runner, probed once and cached.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env


_YTDLP_CMD: list[str] | None = None


def _resolve_ytdlp_cmd() -> list[str]:
    """yt-dlp runner prefix (list), or [] when unavailable. Probed once, cached."""
    global _YTDLP_CMD
    if _YTDLP_CMD is not None:
        return _YTDLP_CMD
    direct = shutil.which("yt-dlp")
    if direct:
        _YTDLP_CMD = [direct]
        return _YTDLP_CMD
    for base in ([shutil.which("python")] if shutil.which("python") else []) + \
                ([shutil.which("py"), "-3"] if shutil.which("py") else []):
        try:
            probe = subprocess.run(
                [*base, "-m", "yt_dlp", "--version"],
                capture_output=True, text=True, timeout=30, env=_child_env(keep=_ENV_TOOLS),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if probe.returncode == 0:
                _YTDLP_CMD = [*base, "-m", "yt_dlp"]
                return _YTDLP_CMD
        except Exception:
            continue
    _YTDLP_CMD = []
    return _YTDLP_CMD
