# SPDX-License-Identifier: AGPL-3.0-only
"""The tool registry: tool name -> spec (description, parameters, the function
that runs it).

One shared dict, never rebound. bridge.py fills it with the tool table, and MCP
servers and skills add to it while running. Code that looks a tool up reads it
from here, so it no longer depends on where the table is written; that dependency
used to tie 84 definitions in bridge.py into one cycle.
"""
from __future__ import annotations

TOOLS: dict[str, dict] = {}
