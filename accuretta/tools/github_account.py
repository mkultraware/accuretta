# SPDX-License-Identifier: AGPL-3.0-only
"""The connected GitHub account: the stored credential, API headers, GraphQL
calls, and the activity and repository lists.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import base64 as _b64
import re
import subprocess
import sys
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.tools.github import _gh_quartile_levels
from accuretta.tools.github import _github_repo_page
from accuretta.core.persistence import save_json
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.core.workspace import get_workspace
from accuretta.security.child_env import _child_env
from pathlib import Path


GITHUB_CRED_FILE = DATA / "github_cred.json"


GITHUB_GRAPHQL_API = "https://api.github.com/graphql"


def _github_cred() -> dict:
    cred = load_json(GITHUB_CRED_FILE, {})
    return cred if isinstance(cred, dict) else {}


def _github_headers(token: str | None = None) -> dict:
    t = token if token is not None else (_github_cred().get("pat") or "")
    return {
        "Authorization": f"Bearer {t}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Accuretta",
    }


def _github_graphql(query: str, token: str | None = None, timeout: int = 30,
                    variables: dict | None = None) -> tuple[int, dict]:
    """Raw GitHub GraphQL call. Returns (status, parsed_json)."""
    request_body = {"query": query}
    if variables is not None:
        request_body["variables"] = variables
    payload = json.dumps(request_body).encode("utf-8")
    req = urllib.request.Request(
        GITHUB_GRAPHQL_API, data=payload, method="POST", headers=_github_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"message": raw[:500]}
    except Exception as e:
        return 0, {"message": str(e)}


_GH_ACTIVITY_QUERY = """
query AccurettaActivity {
  viewer {
    login
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
    repositories(first: 3, orderBy: {field: PUSHED_AT, direction: DESC},
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      nodes {
        name
        owner { login }
        stargazerCount
        pushedAt
        defaultBranchRef { name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


_GH_REPOS_QUERY = """
query AccurettaRepositories($first: Int!, $after: String) {
  viewer {
    repositories(first: $first, after: $after,
                 orderBy: {field: PUSHED_AT, direction: DESC},
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      nodes {
        name
        owner { login }
        stargazerCount
        pushedAt
        defaultBranchRef { name }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _github_repositories(first: int = 12, after: str = "") -> dict:
    """Fetch one token-authorized page of recently pushed repositories."""
    cred = _github_cred()
    pat = cred.get("pat") or ""
    if not pat:
        return {"connected": False, "error": "not connected"}
    limit = max(1, min(int(first or 12), 50))
    status, data = _github_graphql(
        _GH_REPOS_QUERY,
        token=pat,
        variables={"first": limit, "after": after or None},
    )
    if status != 200:
        return {"connected": False,
                "error": str(data.get("message") or data.get("errors") or f"HTTP {status}")}
    errors = data.get("errors")
    if errors:
        first_error = errors[0] if isinstance(errors, list) and errors else errors
        return {"connected": False, "error": str(
            first_error.get("message") if isinstance(first_error, dict) else first_error)}
    viewer = (data.get("data") or {}).get("viewer") or {}
    repos, page_info = _github_repo_page(viewer.get("repositories"))
    return {"connected": True, "repos": repos, "page_info": page_info}


def _github_activity() -> dict:
    """Contribution calendar plus the first page of recently pushed repos."""
    cred = _github_cred()
    pat = cred.get("pat") or ""
    if not pat:
        return {"connected": False, "error": "not connected"}
    status, data = _github_graphql(_GH_ACTIVITY_QUERY, token=pat)
    if status != 200:
        return {"connected": False,
                "error": str(data.get("message") or data.get("errors") or f"HTTP {status}")}
    errors = data.get("errors")
    if errors:
        first = errors[0] if isinstance(errors, list) and errors else errors
        return {"connected": False, "error": str(first.get("message") if isinstance(first, dict) else first)}
    viewer = (data.get("data") or {}).get("viewer") or {}
    cal = ((viewer.get("contributionsCollection") or {}).get("contributionCalendar")) or {}
    weeks = []
    raw_days: list[dict] = []
    for w in cal.get("weeks", []) or []:
        if not isinstance(w, dict):
            continue
        days = []
        for d in w.get("contributionDays", []) or []:
            if not isinstance(d, dict):
                continue
            count = int(d.get("contributionCount") or 0)
            raw_days.append({"date": d.get("date", ""), "count": count})
            days.append({"date": d.get("date", ""), "count": count})
        weeks.append(days)
    # ContributionCalendarDay has no `level` field in the GraphQL schema, so
    # derive the grid intensity the same way GitHub's own graph does.
    flat = [d["count"] for d in raw_days]
    levels = _gh_quartile_levels(flat)
    li = 0
    for w in weeks:
        for d in w:
            d["level"] = levels[li]
            li += 1
    repos, page_info = _github_repo_page(viewer.get("repositories"))
    return {
        "connected": True,
        "login": viewer.get("login") or cred.get("login", ""),
        "avatar_url": cred.get("avatar_url", ""),
        "total_contributions": int(cal.get("totalContributions") or 0),
        "weeks": weeks,
        "repos": repos,
        "repos_page": page_info,
    }


def _github_forget_saved_keys() -> list[str]:
    """Take out the GitHub key that older versions saved into the repos the
    branch flow cloned (see _github_git_login). Only the exact header they
    wrote (Basic x-access-token:...) in a workspace repo's own config.
    Returns the repos cleaned."""
    cleaned: list[str] = []
    for folder in get_workspace().get("folders", []) or []:
        try:
            children = [c for c in Path(str(folder)).iterdir() if c.is_dir()]
        except Exception:
            continue
        for child in children:
            cfg = child / ".git" / "config"
            try:
                if not cfg.is_file() or "extraheader" not in cfg.read_text("utf-8", "replace").lower():
                    continue
                got = subprocess.run(["git", "-C", str(child), "config", "--local", "--get-all",
                                      "http.extraheader"], capture_output=True, text=True, timeout=15,
                                     env=_child_env(), creationflags=_NO_WINDOW)
                for value in got.stdout.splitlines():
                    m = re.fullmatch(r"\s*Authorization:\s*Basic\s+([A-Za-z0-9+/=]+)\s*", value, re.I)
                    if not m:
                        continue
                    try:
                        user = _b64.b64decode(m.group(1)).decode("utf-8", "replace").partition(":")[0]
                    except Exception:
                        continue
                    if user != "x-access-token":
                        continue
                    gone = subprocess.run(["git", "-C", str(child), "config", "--local", "--unset-all",
                                           "http.extraheader", "^" + re.escape(value) + "$"],
                                          capture_output=True, text=True, timeout=15,
                                          env=_child_env(), creationflags=_NO_WINDOW)
                    if gone.returncode == 0 and str(child) not in cleaned:
                        cleaned.append(str(child))
            except Exception:
                continue
    for repo in cleaned:
        print(f"  github: removed a saved GitHub key from {repo}'s git settings", file=sys.stderr)
    return cleaned


def _github_save_cred(pat: str, login: str = "", avatar_url: str = "") -> None:
    save_json(GITHUB_CRED_FILE, {"pat": pat, "login": login, "avatar_url": avatar_url})
