# SPDX-License-Identifier: AGPL-3.0-only
"""Remote Mac helpers: public machine info, allowed folders, and text sent over the wire.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import posixpath
import re
from typing import Any


def _public_remote_machine(row: dict) -> dict:
    return {
        "id": row.get("id", ""),
        "label": row.get("label", "Mac"),
        "hostname": row.get("hostname", ""),
        "dns_name": row.get("dns_name", ""),
        "ip": row.get("ip", ""),
        "os": row.get("os", "macOS"),
        "username": row.get("username", ""),
        "allowed_roots": list(row.get("allowed_roots") or []),
        "home": row.get("home", ""),
        "status": row.get("status", "prepared"),
        "last_verified": int(row.get("last_verified") or 0),
        "enabled": bool(row.get("enabled", True)),
    }


def _normalize_pairing_roots(value: Any) -> list[str]:
    raw = value if isinstance(value, list) else str(value or "").splitlines()
    roots: list[str] = []
    for item in raw:
        root = str(item or "").strip().replace("\\", "/")
        if not root or "\x00" in root or "\n" in root or "\r" in root:
            continue
        if not (root == "~" or root.startswith("~/") or root.startswith("/")):
            continue
        root = posixpath.normpath(root)
        if root not in roots:
            roots.append(root)
        if len(roots) >= 12:
            break
    return roots or ["~/Desktop", "~/Documents", "~/Downloads"]


def _resolved_remote_roots(row: dict) -> list[str]:
    verified = [posixpath.normpath(str(x)) for x in (row.get("resolved_roots") or [])
                if str(x).startswith("/")]
    if verified:
        return list(dict.fromkeys(verified))
    home = posixpath.normpath(str(row.get("home") or ""))
    roots = []
    for raw in row.get("allowed_roots") or []:
        value = str(raw or "")
        if value == "~":
            value = home
        elif value.startswith("~/"):
            value = posixpath.join(home, value[2:])
        value = posixpath.normpath(value)
        if value.startswith("/") and value not in roots:
            roots.append(value)
    return roots


def _text_on_wire(text: str, wire_norm: str) -> bool:
    """True when a stored user request is still in the prompt word for word.
    Compares the opening of the stored copy, up to the first redaction or
    elision marker (the stored copy is redacted, the wire is not). Too short a
    prefix to be sure counts as absent, so the copy is kept."""
    if not wire_norm:
        return False
    head = str(text or "")
    for marker in ("[redacted", "...[", "…"):
        cut = head.find(marker)
        if cut >= 0:
            # Redaction rewrites "key: value" as "key=[redacted ...]", so the
            # key itself differs from the wire too: stop before that word.
            head = re.sub(r"\S*$", "", head[:cut])
    head = re.sub(r"\s+", " ", head).strip()[:200]
    return len(head) >= 40 and head in wire_norm
