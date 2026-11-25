# SPDX-License-Identifier: AGPL-3.0-only
"""Render the machine facts the model sees (ACCURETTA.md): OS, GPU, tools, folders.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


def _render_system_context_md(facts: dict) -> str:
    lines = [
        "# ACCURETTA",
        "",
        "Machine context auto-generated on first boot. Edit freely — the bridge reads this",
        "file directly on every chat turn. Delete it to trigger a re-scan.",
        "",
        "## System",
        f"- OS: {facts.get('os', '')}",
        f"- Hostname: {facts.get('hostname', '')}",
        f"- User: {facts.get('user', '')}",
        f"- Home: {facts.get('userprofile', '')}",
    ]
    if facts.get("drives"):
        lines.append(f"- Drives: {', '.join(facts['drives'])}")
    if facts.get("gguf_model_dirs"):
        lines.append("- GGUF model directories:")
        for d in facts["gguf_model_dirs"]:
            lines.append(f"  - {d}")
    if facts.get("program_roots"):
        lines.append("- Program roots:")
        for p in facts["program_roots"]:
            lines.append(f"  - {p}")

    if facts.get("folders"):
        lines.append("")
        lines.append("## Known folders")
        for f in facts["folders"]:
            n = f.get("items")
            suffix = f" (~{n} items)" if isinstance(n, int) else ""
            lines.append(f"- {f['label']}: `{f['path']}`{suffix}")

    lines += [
        "",
        "## Notes for the agent",
        "- Use these exact paths when the user says \"my desktop\", \"my screenshots\", etc.",
        "- If a requested folder isn't listed here, ask the user or list a parent first.",
        "- The user-configured workspace (in the sidebar) is separate — prefer it for reads/writes.",
        "",
    ]
    return "\n".join(lines)
