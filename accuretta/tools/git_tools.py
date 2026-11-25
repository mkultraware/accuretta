# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's git tools: status, log, diff, branch, show, remote, add, commit, push,
pull, fetch, checkout, restore, reset, init and clone, with the approval and self-
protection checks in front of the ones that change things.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shlex
import time
from accuretta.core.data_files import PUSHED_REPOS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.workspace import normalize_path
from accuretta.security.approvals import _critical_path_gate
from accuretta.security.approvals import request_approval
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.security.path_guards import is_blocked_path
from accuretta.security.path_guards import is_in_workspace
from accuretta.security.self_protect import _BRIDGE_FILE_ABS
from accuretta.security.self_protect import _PROT_BRIDGE_FILE_RE
from accuretta.security.self_protect import is_bridge_self_path
from accuretta.tools.git_runner import _run_git
from accuretta.tools.github import _parse_push_repo_key
from pathlib import Path


def _resolve_git_cwd(path: str) -> tuple[str | None, dict | None]:
    """Validate `path` is inside the workspace and resolves to a directory.
    Returns (abs_dir, None) on success or (None, {"error": ...})."""
    if not path:
        return None, {"error": "path is required (directory inside the workspace)"}
    norm = normalize_path(path)
    p = Path(norm)
    if not p.exists():
        return None, {"error": f"path does not exist: {norm}"}
    if not p.is_dir():
        norm = str(p.parent)
    if is_blocked_path(norm):
        return None, {"error": "path is blocked"}
    if not is_in_workspace(norm):
        return None, {"error": "path is outside the workspace — add it via Workspace settings first"}
    return norm, None


def _git_approve(title: str, cwd: str, argv: list[str], extra: dict | None = None) -> dict | None:
    """Block on user approval. Returns None if approved, or a refusal dict."""
    # Self-protection: `git checkout -- bridge.py`, `git restore bridge.py`,
    # `git reset --hard` etc. in the accuretta repo would wipe in-flight edits.
    # Hard refuse, bypassing approval like the PowerShell path.
    head = argv[0] if argv else ""
    destructive = head in {"checkout", "restore", "reset", "clean", "rm"}
    if destructive:
        joined = " ".join(argv)
        if _PROT_BRIDGE_FILE_RE.search(joined):
            return {"error": "refused: would discard local changes to bridge.py. Stash or commit your edits first if you actually want this."}
        if _is_accuretta_repo(cwd) and any(
                a and not a.startswith("-") and is_bridge_self_path(os.path.join(cwd, a)) for a in argv[1:]):
            return {"error": "refused: would discard local changes to Accuretta's own code (the accuretta/ folder). Stash or commit your edits first if you actually want this."}
        if head == "reset" and "--hard" in argv and _is_accuretta_repo(cwd):
            return {"error": "refused: `git reset --hard` inside the Accuretta repo would wipe uncommitted bridge edits. Stash or commit first."}
        if head == "clean" and any(a in {"-f", "-fd", "-fdx", "-fx"} for a in argv) and _is_accuretta_repo(cwd):
            return {"error": "refused: `git clean -f*` inside the Accuretta repo would delete untracked bridge files."}
    command = "git " + " ".join(shlex.quote(a) for a in argv)
    details = {"kind": "git", "cwd": cwd, "argv": argv}
    if extra:
        details.update(extra)
    approval = request_approval(title=title, command=command, details=details)
    if approval.get("decision") != "approve":
        return {"error": f"user denied git ({approval.get('status')})"}
    return None


def _is_accuretta_repo(cwd: str) -> bool:
    """True if `cwd` is inside the directory that contains the running bridge.py."""
    if not _BRIDGE_FILE_ABS:
        return False
    try:
        bridge_dir = str(Path(_BRIDGE_FILE_ABS).parent).lower()
        c = str(Path(cwd).resolve()).lower()
        return c == bridge_dir or c.startswith(bridge_dir + os.sep)
    except Exception:
        return False


def tool_git_status(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    res = _run_git(cwd, ["status", "--porcelain=v1", "--branch", "--untracked-files=normal"], timeout=30)
    # Also fetch the current branch name verbatim — porcelain v1 ## line is
    # cryptic when the branch has no upstream (## main vs ## HEAD (no branch)).
    head = _run_git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    if isinstance(res, dict) and res.get("ok"):
        res["branch"] = (head.get("stdout") or "").strip() if isinstance(head, dict) else ""
    return res


def tool_git_log(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    n = max(1, min(int(args.get("max_count", 20) or 20), 200))
    fmt = "%h%x09%an%x09%ad%x09%s"
    argv = ["log", f"-n{n}", "--date=short", f"--pretty=format:{fmt}"]
    ref = (args.get("ref") or "").strip()
    if ref:
        argv.append(ref)
    file_filter = (args.get("file") or "").strip()
    if file_filter:
        argv.extend(["--", file_filter])
    return _run_git(cwd, argv, timeout=30)


def tool_git_diff(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["diff", "--no-color"]
    if args.get("staged"):
        argv.append("--cached")
    if args.get("stat"):
        argv.append("--stat")
    ref = (args.get("ref") or "").strip()
    if ref:
        argv.append(ref)
    file_filter = (args.get("file") or "").strip()
    if file_filter:
        argv.extend(["--", file_filter])
    return _run_git(cwd, argv, timeout=60, max_stdout=400_000)


def tool_git_branch(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["branch", "--no-color", "-vv"]
    if args.get("all"):
        argv.append("-a")
    if args.get("remote"):
        argv.append("-r")
    return _run_git(cwd, argv, timeout=20)


def tool_git_show(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    ref = (args.get("ref") or "HEAD").strip()
    argv = ["show", "--no-color", ref]
    if args.get("stat"):
        argv.append("--stat")
    return _run_git(cwd, argv, timeout=30, max_stdout=300_000)


def tool_git_remote(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    return _run_git(cwd, ["remote", "-v"], timeout=15)


def tool_git_add(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not paths or not isinstance(paths, list):
        if args.get("all"):
            paths = ["-A"]
        else:
            return {"error": "specify `paths` (list of files) or `all: true`"}
    # Refuse the lazy `-A` from a nested cwd unless the caller explicitly opts in.
    argv = ["add", "--", *paths] if paths != ["-A"] else ["add", "-A"]
    refusal = _git_approve(f"git add ({len(paths)} target{'s' if len(paths)!=1 else ''})", cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=60)


def tool_git_commit(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    message = (args.get("message") or "").strip()
    if not message:
        return {"error": "commit message is required"}
    argv = ["commit", "-m", message]
    if args.get("amend"):
        argv.append("--amend")
        if args.get("no_edit"):
            argv.append("--no-edit")
    if args.get("allow_empty"):
        argv.append("--allow-empty")
    if args.get("all"):
        argv.append("-a")
    title = "git commit" + (" --amend" if args.get("amend") else "")
    refusal = _git_approve(title, cwd, argv, extra={"message_preview": message[:200]})
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=60)


def _record_repo_push(cwd: str, remote: str = "") -> None:
    """Best-effort: after a successful in-app `git push`, register the repo
    (owner/name from the remote URL) for Usage stats. Never raises — a stats
    miss is always preferable to breaking the push result."""
    try:
        name = (remote or "").strip() or "origin"
        res = _run_git(cwd, ["remote", "get-url", name], timeout=10)
        if not isinstance(res, dict) or not res.get("ok"):
            return
        key, display = _parse_push_repo_key(str(res.get("stdout") or ""))
        if not key:
            return
        try:
            reg = load_json(PUSHED_REPOS_FILE, {})
        except Exception:
            reg = {}
        if not isinstance(reg, dict):
            reg = {}
        repos = reg.get("repos")
        if not isinstance(repos, dict):
            repos = {}
            reg["repos"] = repos
        now = int(time.time())
        entry = repos.get(key.casefold())
        if not isinstance(entry, dict):
            entry = {"name": display, "pushes": 0, "first_push": now}
        entry["name"] = display
        entry["pushes"] = max(0, int(entry.get("pushes", 0) or 0)) + 1
        entry["last_push"] = now
        if not entry.get("first_push"):
            entry["first_push"] = now
        repos[key.casefold()] = entry
        save_json(PUSHED_REPOS_FILE, reg)
    except Exception:
        pass


def tool_git_push(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["push"]
    if args.get("set_upstream"):
        argv.append("-u")
    if args.get("force_with_lease"):
        argv.append("--force-with-lease")
    if args.get("tags"):
        argv.append("--tags")
    remote = (args.get("remote") or "").strip()
    branch = (args.get("branch") or "").strip()
    if remote:
        argv.append(remote)
        if branch:
            argv.append(branch)
    elif branch:
        # branch without remote is invalid; default remote to origin
        argv.extend(["origin", branch])
    refusal = _git_approve("git push", cwd, argv)
    if refusal:
        return refusal
    # Push can be slow on a fresh clone with large history; 5 min cap.
    res = _run_git(cwd, argv, timeout=300)
    if isinstance(res, dict) and res.get("ok"):
        _record_repo_push(cwd, remote)
    return res


def tool_git_pull(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["pull"]
    if args.get("rebase"):
        argv.append("--rebase")
    if args.get("ff_only"):
        argv.append("--ff-only")
    remote = (args.get("remote") or "").strip()
    branch = (args.get("branch") or "").strip()
    if remote:
        argv.append(remote)
        if branch:
            argv.append(branch)
    refusal = _git_approve("git pull", cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=300)


def tool_git_fetch(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["fetch"]
    if args.get("all"):
        argv.append("--all")
    if args.get("prune"):
        argv.append("--prune")
    if args.get("tags"):
        argv.append("--tags")
    remote = (args.get("remote") or "").strip()
    if remote:
        argv.append(remote)
    refusal = _git_approve("git fetch", cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=180)


def tool_git_checkout(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    target = (args.get("target") or "").strip()
    if not target:
        return {"error": "`target` is required (branch, ref, or '--' file)"}
    argv = ["checkout"]
    if args.get("create"):
        argv.append("-b")
    argv.append(target)
    files = args.get("files")
    if isinstance(files, str):
        files = [files]
    if files:
        argv.append("--")
        argv.extend(files)
    refusal = _git_approve("git checkout", cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=60)


def tool_git_restore(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    files = args.get("files")
    if isinstance(files, str):
        files = [files]
    if not files:
        return {"error": "`files` is required (list of paths to restore)"}
    argv = ["restore"]
    if args.get("staged"):
        argv.append("--staged")
    if args.get("worktree", True) and not args.get("staged"):
        # default behavior of `git restore` is worktree; nothing to add
        pass
    if args.get("source"):
        argv.extend(["--source", str(args["source"])])
    argv.append("--")
    argv.extend(files)
    refusal = _git_approve("git restore", cwd, argv, extra={"files": files})
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=30)


def tool_git_reset(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    mode = (args.get("mode") or "mixed").strip().lower()
    if mode not in {"soft", "mixed", "hard", "keep", "merge"}:
        return {"error": "mode must be one of: soft, mixed, hard, keep, merge"}
    argv = ["reset", f"--{mode}"]
    target = (args.get("target") or "").strip()
    if target:
        argv.append(target)
    title = f"git reset --{mode}"
    refusal = _git_approve(title, cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=60)


def tool_git_init(args: dict) -> dict:
    cwd, err = _resolve_git_cwd(args.get("path") or "")
    if err:
        return err
    argv = ["init"]
    initial_branch = (args.get("initial_branch") or "").strip()
    if initial_branch:
        argv.extend(["-b", initial_branch])
    refusal = _git_approve("git init", cwd, argv)
    if refusal:
        return refusal
    return _run_git(cwd, argv, timeout=20)


def tool_git_clone(args: dict) -> dict:
    """Clone into a directory inside the workspace. `dest` must be a workspace path."""
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "`url` is required"}
    dest = (args.get("dest") or "").strip()
    if not dest:
        return {"error": "`dest` is required (directory inside workspace; will be created)"}
    dest_norm = normalize_path(dest)
    parent = str(Path(dest_norm).parent)
    if is_data_path(dest_norm, write=True):
        return {"error": DATA_PATH_ERROR}
    if not is_in_workspace(parent):
        return {"error": "dest is outside the workspace"}
    _cerr = _critical_path_gate(dest_norm, "git", "PROTECTED PATH - git clone destination under Windows/System32")
    if _cerr:
        return _cerr
    if os.path.exists(dest_norm) and os.listdir(dest_norm):
        return {"error": f"dest already exists and is non-empty: {dest_norm}"}
    Path(parent).mkdir(parents=True, exist_ok=True)
    argv = ["clone", url, dest_norm]
    depth = args.get("depth")
    if depth and int(depth) > 0:
        argv.extend(["--depth", str(int(depth))])
    branch = (args.get("branch") or "").strip()
    if branch:
        argv.extend(["--branch", branch])
    refusal = _git_approve("git clone", parent, argv, extra={"url": url, "dest": dest_norm})
    if refusal:
        return refusal
    # Clone runs in the parent (cwd doesn't matter much for clone with explicit dest).
    return _run_git(parent, argv, timeout=900)
