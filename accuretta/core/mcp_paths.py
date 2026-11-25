# SPDX-License-Identifier: AGPL-3.0-only
"""Where the MCP server config lives (it holds API keys; the file tools refuse it).

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.paths import USER_DIR


# ---- MCP server manager (the MCP window) -------------------------------------
# bridge_mcp_config.json stays the single source of truth (gitignored, same
# "mcpServers" shape other MCP hosts use), so hand edits keep working. The
# window edits it through these helpers and starts/stops servers live: tools
# are read from TOOLS on every turn, so no bridge restart is needed.
_MCP_CONFIG_FILE = USER_DIR / "bridge_mcp_config.json"
