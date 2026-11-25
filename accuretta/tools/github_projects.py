# SPDX-License-Identifier: AGPL-3.0-only
"""GitHub projects from the app: the REST calls, creating a local project and its empty
GitHub repo, and the branch flow that checks a new remote branch out in the workspace.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace import normalize_path
from accuretta.tools.git_runner import _run_git
from accuretta.tools.github_account import _github_cred
from accuretta.tools.github_account import _github_headers
from accuretta.core.github_names import _GITHUB_REPO_RE
from accuretta.core.github_names import _github_worktree_metadata
from pathlib import Path


GITHUB_REST_API = "https://api.github.com"


def _github_rest(method: str, path: str, body: dict | None = None,
                 token: str | None = None, timeout: int = 25) -> tuple[int, dict]:
    """Raw GitHub REST call. Returns (status, parsed_json)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        GITHUB_REST_API + path, data=data, method=method, headers=_github_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except Exception:
                return resp.status, {"message": raw[:500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw) if raw else {}
        except Exception:
            return e.code, {"message": raw[:500]}
    except Exception as e:
        return 0, {"message": str(e)}


def _github_rest_h(method: str, path: str, body: dict | None = None,
                   token: str | None = None, timeout: int = 25) -> tuple[int, dict, object]:
    """Like _github_rest but also returns the response headers object.

    GitHub reports the permission level a token actually carries on the
    requested resource via the `x-accepted-github-permissions` header, which
    is what makes 403s explainable without guessing.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        GITHUB_REST_API + path, data=data, method=method, headers=_github_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, (json.loads(raw) if raw else {}), resp.headers
            except Exception:
                return resp.status, {"message": raw[:500]}, resp.headers
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw else {}), e.headers
        except Exception:
            return e.code, {"message": raw[:500]}, e.headers
    except Exception as e:
        return 0, {"message": str(e)}, {}


def _github_worktree_prompt(chat: dict | None) -> str:
    """Render a branch session's private repo context for the model."""
    worktree = _github_worktree_metadata((chat or {}).get("github_worktree"))
    if not worktree:
        return ""
    return (
        "active GitHub worktree for this chat:\n"
        f"- repository: {worktree['owner']}/{worktree['repo']}\n"
        f"- expected branch: {worktree['branch']}\n"
        f"- local path: {worktree['path']}\n"
        "Treat this as the user's current project. For code work, inspect this path first "
        "and keep changes inside it unless the user explicitly names another project. "
        "Before branch-sensitive work, verify the branch with git_status."
    )


def _project_workspace_root(requested: str = "") -> str | None:
    """Resolve a selected workspace root without accepting arbitrary subfolders."""
    roots = [normalize_path(str(folder)) for folder in get_workspace().get("folders", []) or []
             if str(folder or "").strip()]
    if not roots:
        return None
    if not requested:
        return roots[0]
    wanted = normalize_path(requested)
    return next((root for root in roots if os.path.normcase(root) == os.path.normcase(wanted)), None)


def _github_create_project(name: str, brief: str = "", workspace_root: str = "",
                           publish: bool = False, private: bool = True) -> dict:
    """Create a safe local git project and optionally publish an empty GitHub repo."""
    name = (name or "").strip()
    if not _GITHUB_REPO_RE.fullmatch(name) or name in {".", ".."} or name.endswith("."):
        return {"error": "invalid project name; use letters, digits, dots, underscores and hyphens (max 100 characters)"}
    if os.name == "nt" and name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
        "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
        "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }:
        return {"error": "that project name is reserved by Windows"}
    root = _project_workspace_root(workspace_root)
    if not root:
        return {"error": "choose one of the configured workspace folders"}
    root_path = Path(root)
    destination = root_path / name
    try:
        resolved_destination = destination.resolve()
        if os.path.normcase(str(resolved_destination.parent)) != os.path.normcase(str(root_path.resolve())):
            return {"error": "project path escapes the selected workspace folder"}
        if destination.exists():
            return {"error": f"a folder named `{name}` already exists in that workspace"}
        destination.mkdir(parents=False, exist_ok=False)
    except Exception as exc:
        return {"error": f"could not create the project folder: {exc}"}

    initialized = _run_git(str(destination), ["init", "-b", "main"], timeout=60)
    if not initialized.get("ok"):
        initialized = _run_git(str(destination), ["init"], timeout=60)
        if initialized.get("ok"):
            initialized = _run_git(
                str(destination), ["symbolic-ref", "HEAD", "refs/heads/main"], timeout=30)
    if not initialized.get("ok"):
        try:
            shutil.rmtree(destination)
        except Exception:
            pass
        detail = str(initialized.get("stderr") or initialized.get("error") or "unknown error")[:500]
        return {"error": f"git could not initialize the project: {detail}"}

    result = {
        "ok": True,
        "name": name,
        "path": str(destination.resolve()),
        "branch": "main",
        "published": False,
        "remote_ready": False,
    }
    if not publish:
        return result

    pat = _github_cred().get("pat") or ""
    if not pat:
        result["publish_error"] = "GitHub is not connected. The local project was still created."
        return result
    status, remote, _headers = _github_rest_h("POST", "/user/repos", {
        "name": name,
        "description": (brief or "").strip()[:350],
        "private": bool(private),
        "auto_init": False,
    }, token=pat)
    if status != 201:
        message = str(remote.get("message") or remote.get("errors") or f"HTTP {status}")
        if status == 403:
            message = ("GitHub refused repository creation. Give the fine-grained token "
                       "Administration: Read and write, then retry publishing. The local project was still created.")
        else:
            message = f"GitHub repository creation failed: {message}. The local project was still created."
        result["publish_error"] = message
        return result

    owner = str(((remote.get("owner") or {}) or {}).get("login") or _github_cred().get("login") or "")
    clone_url = str(remote.get("clone_url") or "")
    result.update({
        "published": True,
        "owner": owner,
        "repo": str(remote.get("name") or name),
        "html_url": str(remote.get("html_url") or ""),
    })
    if not clone_url:
        result["publish_error"] = "GitHub created the repository but returned no clone URL. Add the remote manually."
        return result
    remote_result = _run_git(str(destination), ["remote", "add", "origin", clone_url], timeout=30)
    if not remote_result.get("ok"):
        detail = str(remote_result.get("stderr") or remote_result.get("error") or "unknown error")[:300]
        result["publish_error"] = f"GitHub created the repository, but wiring the local remote failed: {detail}"
        return result
    result["remote_ready"] = True
    return result


def _github_find_workspace_repo(repo: str) -> str | None:
    """Locate a repo directory (with .git) inside the configured workspace folders."""
    wanted = repo.lower()
    for folder in get_workspace().get("folders", []) or []:
        try:
            for child in Path(folder).iterdir():
                if (child.is_dir() and child.name.lower() == wanted
                        and (child / ".git").exists()):
                    return str(child.resolve())
        except Exception:
            continue
    return None


def _github_branch_flow(owner: str, repo: str, branch: str) -> dict:
    """Create the remote branch, then clone/fetch and check it out in the workspace."""
    cred = _github_cred()
    pat = cred.get("pat") or ""
    if not pat:
        return {"error": "GitHub is not connected — add a personal access token in Settings → GitHub integration."}
    # 1. repo metadata -> default branch name (also capture the permission
    #    level GitHub reports the token actually has on this repo)
    status, data, headers = _github_rest_h("GET", f"/repos/{owner}/{repo}")
    if status != 200:
        msg = str(data.get("message") or data.get("errors") or f"HTTP {status}")
        return {"error": f"could not read repo {owner}/{repo}: {msg}"}
    default_branch = str(data.get("default_branch") or "main").strip()
    accepted_perms = (headers.get("x-accepted-github-permissions") or "") if headers else ""
    # 2. SHA of the default branch tip
    status, ref = _github_rest(
        "GET", f"/repos/{owner}/{repo}/git/ref/heads/{urllib.parse.quote(default_branch, safe='/')}")
    if status != 200:
        return {"error": f"could not read the default branch ref: "
                         f"{str(ref.get('message') or f'HTTP {status}')}"}
    sha = str(((ref.get("object") or {}) or {}).get("sha") or "")
    if not sha:
        return {"error": "the default branch ref returned no sha"}
    # 3. create the new ref (422 "already exists" is treated as success)
    status, created = _github_rest("POST", f"/repos/{owner}/{repo}/git/refs",
                                   {"ref": f"refs/heads/{branch}", "sha": sha})
    if status not in (200, 201):
        msg = str(created.get("message") or f"HTTP {status}")
        if "already exists" in msg.lower():
            pass
        elif status == 403:
            low = msg.lower()
            if "authoriz" in low and "resource owner" in low:
                return {"error": (f"GitHub hasn't authorized this token for {owner}/{repo} — "
                                  f"if the repo belongs to an organization, approve the "
                                  f"fine-grained PAT under its settings, then retry.")}
            have = accepted_perms or "unknown"
            return {"error": (f"GitHub refused branch creation — the personal access token "
                              f"carries `{have}` on {owner}/{repo}, but creating a branch "
                              f"needs `Contents: Read and write` (Metadata: Read is "
                              f"automatic). Edit the token's Repository permissions in "
                              f"GitHub and retry — no new token required.")}
        else:
            return {"error": f"could not create branch `{branch}`: {msg}"}
    # 4. local checkout — repos are only ever cloned into the Accuretta workspace
    repo_dir = _github_find_workspace_repo(repo)
    cloned = False
    if not repo_dir:
        folders = [f for f in get_workspace().get("folders", []) or [] if str(f or "").strip()]
        if not folders:
            return {"error": "no workspace folder configured — cannot clone the repo locally"}
        dest = Path(folders[0]) / repo
        if dest.exists() and any(dest.iterdir()):
            return {"error": f"{owner}/{repo} isn't cloned in the workspace, but a folder "
                             f"named `{repo}` already exists there and isn't a git repo"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        clone_url = f"https://github.com/{owner}/{repo}.git"
        # The key reaches git through _git_env, never `clone -c`, which would
        # save it into the clone's own config.
        result = _run_git(str(dest.parent), ["clone", "--", clone_url, str(dest)], timeout=900)
        if not result.get("ok"):
            return {"error": f"clone into the workspace failed: "
                             f"{str(result.get('stderr') or result.get('stdout') or result.get('error') or 'unknown')[:500]}"}
        repo_dir = str(dest.resolve())
        cloned = True
    # 5. fetch (brings in the new branch) and switch to it
    fetch = _run_git(repo_dir, ["fetch", "origin", "--prune"], timeout=300)
    if not fetch.get("ok"):
        return {"error": f"git fetch failed: "
                         f"{str(fetch.get('stderr') or fetch.get('error') or 'unknown')[:500]}"}
    switch = _run_git(repo_dir, ["switch", "-c", branch, f"origin/{branch}"], timeout=120)
    if not switch.get("ok"):
        switch = _run_git(repo_dir, ["switch", "-c", branch], timeout=120)
    if not switch.get("ok"):
        return {"error": f"git switch failed: "
                         f"{str(switch.get('stderr') or switch.get('error') or 'unknown')[:500]}"}
    return {"ok": True, "owner": owner, "repo": repo, "branch": branch,
            "default_branch": default_branch, "sha": sha,
            "repo_dir": repo_dir, "cloned": cloned}
