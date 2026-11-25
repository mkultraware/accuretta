# SPDX-License-Identifier: AGPL-3.0-only
"""The pending approvals, listed for the UI and other surfaces.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.security.approvals import _approvals
from accuretta.security.approvals import _approvals_lock


def list_approvals() -> list[dict]:
    with _approvals_lock:
        return [dict(v) for v in _approvals.values() if v.get("status") == "pending"]
