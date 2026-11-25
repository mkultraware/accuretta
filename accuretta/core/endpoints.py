# SPDX-License-Identifier: AGPL-3.0-only
"""Where things listen: the llama-server URL from the environment, and Accuretta's own
port.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os


def _resolve_llama_url() -> str:
    """Resolve llama-server URL. Default: http://127.0.0.1:8080.
    Env LLAMA_HOST accepts 'host:port', bare host, or full URL."""
    raw = (os.environ.get("LLAMA_HOST") or os.environ.get("LLAMA_URL") or "").strip()
    if not raw:
        return "http://127.0.0.1:8080"
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    try:
        scheme, rest = raw.split("://", 1)
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "8080"
    except Exception:
        return "http://127.0.0.1:8080"
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port}"


LLAMA = _resolve_llama_url()


PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))
