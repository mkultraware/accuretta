# SPDX-License-Identifier: AGPL-3.0-only
"""The operation kinds the user chose 'Always allow (this session)' for: kept in memory
until Accuretta restarts, never for destructive kinds.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


# Kinds the user chose "Always allow (this session)" for — auto-approved until
# the bridge restarts. Never persisted to disk; destructive kinds never enter it
# (the UI only offers "always allow" on safe operations).
_approval_session_allow: set[str] = set()
