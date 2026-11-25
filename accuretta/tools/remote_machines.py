# SPDX-License-Identifier: AGPL-3.0-only
"""Paired remote machines: the saved list, looking one up, and the client hints a paired
device sends.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from typing import Any


REMOTE_DIR = DATA / "remote"


REMOTE_MACHINES_FILE = REMOTE_DIR / "machines.json"


_REMOTE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


_CLIENT_OS_VALUES = {"macOS", "Windows", "Linux", "iOS", "Android", "unknown"}


def _load_remote_machines() -> dict:
    data = load_json(REMOTE_MACHINES_FILE, {"machines": []})
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        return {"machines": []}
    clean = []
    for row in data["machines"]:
        if isinstance(row, dict) and _REMOTE_ID_RE.fullmatch(str(row.get("id") or "")):
            clean.append(dict(row))
    return {"machines": clean}


def _remote_machine(machine_id: str) -> dict | None:
    mid = str(machine_id or "").strip()
    if not _REMOTE_ID_RE.fullmatch(mid):
        return None
    for row in _load_remote_machines().get("machines", []):
        if row.get("id") == mid and row.get("enabled", True):
            return row
    return None


def _sanitize_client_hint(raw: Any) -> dict:
    src = raw if isinstance(raw, dict) else {}
    client_os = str(src.get("os") or "unknown")
    if client_os not in _CLIENT_OS_VALUES:
        client_os = "unknown"
    target = str(src.get("execution_target") or "host").strip()
    if target != "host" and _remote_machine(target) is None:
        target = "host"
    return {
        "os": client_os,
        "mobile": bool(src.get("mobile")),
        "secure_context": bool(src.get("secure_context")),
        "execution_target": target,
    }


def _handler_client_context(handler, raw_hint: Any = None) -> dict:
    hint = _sanitize_client_hint(raw_hint)
    peer_text = str((getattr(handler, "client_address", None) or ("",))[0] or "")
    try:
        peer = ipaddress.ip_address(peer_text)
    except ValueError:
        peer = None
    tailnet_peer = bool(peer and any(
        peer in ipaddress.ip_network(cidr)
        for cidr in ("100.64.0.0/10", "fd7a:115c:a1e0::/48")
    ))
    # Tailscale Serve proxies from localhost and adds identity headers. They are
    # used only for presentation, never authorization, so spoofing one gains
    # nothing.
    served = bool((handler.headers.get("Tailscale-User-Login") or "").strip())
    remote = bool(tailnet_peer or served)
    transport = "tailscale_serve" if served else ("tailscale_direct" if tailnet_peer else "local")
    target = hint["execution_target"]
    machine = _remote_machine(target) if target != "host" else None
    return {
        **hint,
        "remote": remote,
        "transport": transport,
        "peer_ip": peer_text if tailnet_peer else "",
        "target_label": machine.get("label", "Mac") if machine else "Inference PC",
        "inference_os": "Windows" if os.name == "nt" else ("macOS" if sys.platform == "darwin" else "Linux"),
    }


REMOTE_BACKUPS_DIR = REMOTE_DIR / "backups"
