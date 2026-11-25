# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub helpers: repository page links, activity levels, the repo a push goes to.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import math
import re


def _parse_push_repo_key(url: str) -> tuple[str | None, str | None]:
    """(key, display) from a git remote URL, e.g. owner/repo. (None, None) when
    the URL has no owner/name shape (local paths, single-segment names)."""
    u = (url or "").strip()
    if not u:
        return None, None
    if "@" in u and "://" not in u and ":" in u:
        u = u.split(":", 1)[1]  # scp-style: git@github.com:owner/repo.git
    elif "://" in u:
        u = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", u)
        u = re.sub(r"^[^@/]+@", "", u)
    else:
        return None, None  # bare local path, no host — not a pushable repo
    u = u.strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    parts = [p for p in u.split("/") if p]
    if len(parts) < 2 or not parts[-2] or not parts[-1]:
        return None, None
    display = f"{parts[-2]}/{parts[-1]}"
    return display, display  # callers casefold the key


def _github_repo_page(connection: object) -> tuple[list[dict], dict]:
    """Normalize a GitHub repository connection for the browser."""
    conn = connection if isinstance(connection, dict) else {}
    repos = []
    for node in conn.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        repos.append({
            "name": node.get("name", ""),
            "owner": ((node.get("owner") or {}) or {}).get("login", ""),
            "stars": int(node.get("stargazerCount") or 0),
            "pushed_at": node.get("pushedAt", ""),
            "default_branch": ((node.get("defaultBranchRef") or {}) or {}).get("name", "main"),
        })
    raw_page = conn.get("pageInfo") if isinstance(conn.get("pageInfo"), dict) else {}
    page_info = {
        "has_next": bool(raw_page.get("hasNextPage")),
        "cursor": str(raw_page.get("endCursor") or ""),
    }
    return repos, page_info


def _gh_quartile_levels(counts: list[int]) -> list[int]:
    """Map contribution counts to the 0-4 GitHub grid levels.

    GitHub's graph buckets non-zero days by quartiles of the non-zero
    distribution (the schema exposes no `level` field on
    ContributionCalendarDay, so we reproduce the grid server-side).
    """
    nonzero = sorted(c for c in counts if c > 0)
    if not nonzero:
        return [0] * len(counts)

    def quartile(idx: int) -> float:
        pos = (len(nonzero) - 1) * idx / 3.0
        lo = math.floor(pos)
        hi = math.ceil(pos)
        if lo == hi:
            return float(nonzero[lo])
        return nonzero[lo] * (hi - pos) + nonzero[hi] * (pos - lo)

    q1, q2, q3 = quartile(1), quartile(2), quartile(3)
    out = []
    for c in counts:
        if c <= 0:
            out.append(0)
        elif c > q3:
            out.append(4)
        elif c > q2:
            out.append(3)
        elif c > q1:
            out.append(2)
        else:
            out.append(1)
    return out
