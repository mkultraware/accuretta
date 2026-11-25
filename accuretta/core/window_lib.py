# SPDX-License-Identifier: AGPL-3.0-only
"""pygetwindow, when it is installed: desktop window lookup for approvals and the
desktop tools.

Moved out of bridge.py unchanged; bridge.py re-exports both names. Tests patch
them with bridge_patch, which reaches every module holding them.
"""
from __future__ import annotations

try:
    import pygetwindow as _pgw  # type: ignore
    _HAVE_PGW = True
except Exception:
    _pgw = None  # type: ignore
    _HAVE_PGW = False
