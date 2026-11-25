# SPDX-License-Identifier: AGPL-3.0-only
"""Which git commands ask for approval: pushing, throwing work away, rewriting history, risky settings.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re


_GIT_REMOTE_MUTATING_VERBS = {
    "add", "remove", "rm", "rename", "set-head", "set-branches",
    "set-url", "prune", "update",
}


# -- git ----------------------------------------------------------------------------
# Local git work runs: it stays on this PC and the reflog keeps it. What asks:
# pushing, throwing away uncommitted work, rewriting or deleting history, and
# settings that run programs or change where code goes.
_GIT_QUIET_VERBS = {
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "grep", "blame",
    "add", "commit", "checkout", "switch", "branch", "stash", "tag", "merge", "rebase",
    "cherry-pick", "revert", "restore", "mv", "rm", "init", "clone", "fetch", "pull", "am",
    "apply", "notes", "worktree", "submodule", "sparse-checkout", "bisect", "format-patch",
    "archive", "bundle", "gc", "config", "remote", "reflog", "describe", "rev-list",
    "shortlog", "cat-file", "for-each-ref", "name-rev", "merge-base", "count-objects", "fsck",
    "verify-commit", "verify-tag", "ls-remote", "help", "version", "check-ignore",
    "check-attr", "whatchanged", "var", "lfs", "range-diff", "show-ref", "show-branch",
    "symbolic-ref", "hash-object", "write-tree", "mktree", "update-index", "difftool",
    "credential-manager", "maintenance", "prune", "repack", "column", "interpret-trailers",
}


_GIT_RESET_DISCARD = {"--hard", "--merge", "--keep"}


_GIT_EXEC_CONFIG_RE = re.compile(
    r"^(?:core\.(?:hookspath|sshcommand|fsmonitor|pager|editor|askpass|gitproxy)|alias\..+|credential\..*|"
    r"url\..+|include(?:if)?\..*|filter\..+|diff\..+\.textconv|gpg\.program|sequence\.editor|"
    r"protocol\..*allow)$", re.IGNORECASE)


def _git_concern(argv: list[str]) -> str | None:
    """Why a git command line should ask, or None. `argv` starts after `git`."""
    args = [str(a) for a in (argv or [])]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-c",) and i + 1 < len(args):
            key = args[i + 1].split("=", 1)[0]
            if _GIT_EXEC_CONFIG_RE.match(key):
                return f"runs Git with a setting that starts programs ({key})"
            i += 2
            continue
        i += 2 if args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return None
    verb, rest = args[i].lower(), args[i + 1:]
    low = [a.lower() for a in rest]
    flags = {a for a in low if a.startswith("-")}
    short = "".join(a[1:] for a in low if re.fullmatch(r"-[a-z]+", a))
    if verb == "push":
        if (flags & {"--force", "--force-with-lease", "--mirror", "--delete", "--prune", "-d"}
                or "f" in short or any(a.startswith((":", "+")) for a in rest if not a.startswith("-"))):
            return "rewrites or deletes Git history on the remote (git push --force/--delete)"
        return "pushes to a remote (git push)"
    if verb == "reset":
        return "throws away uncommitted changes (git reset --hard)" if flags & _GIT_RESET_DISCARD else None
    if verb == "credential":
        return "reads or changes your saved Git login (git credential)"
    if verb not in _GIT_QUIET_VERBS:
        return f"runs an unfamiliar Git command (git {verb})"
    if verb == "clean" and ("f" in short or "--force" in flags) and not ({"-n", "--dry-run"} & flags or "n" in short):
        return "deletes untracked files (git clean -f)"
    if verb == "checkout" and ("--" in rest or "." in rest or flags & {"-f", "--force", "-p", "--patch"}):
        return "throws away uncommitted changes (git checkout -- …)"
    if verb == "restore" and ("--staged" not in flags or "--worktree" in flags):
        return "throws away uncommitted changes (git restore)"
    if verb == "switch" and flags & {"--discard-changes", "-f", "--force"}:
        return "throws away uncommitted changes (git switch --discard-changes)"
    if verb == "stash" and low[:1] and low[0] in ("drop", "clear"):
        return "deletes stashed changes (git stash drop/clear)"
    if verb == "branch" and ("D" in "".join(a[1:] for a in rest if re.fullmatch(r"-[A-Za-z]+", a))
                             or ({"--delete", "-d"} & flags and {"--force", "-f"} & flags)
                             or ("f" in short and len([a for a in rest if not a.startswith("-")]) >= 1)):
        return "deletes or moves a branch that may not be merged (git branch -D/-f)"
    if verb == "gc" and any(a.startswith("--prune") for a in low):
        return "makes deleted commits unrecoverable (git gc --prune)"
    if verb == "reflog" and low[:1] and low[0] in ("expire", "delete"):
        return "makes deleted commits unrecoverable (git reflog expire)"
    if verb == "prune":
        return "makes deleted commits unrecoverable (git prune)"
    if verb == "remote" and low[:1] and low[0] in _GIT_REMOTE_MUTATING_VERBS:
        return f"changes where Git pushes and pulls (git remote {low[0]})"
    if verb == "config":
        reading = flags & {"--get", "--get-all", "--get-regexp", "--list", "-l", "--get-urlmatch",
                           "--show-origin", "--show-scope", "--name-only", "-z", "--null"}
        positional = [a for a in rest if not a.startswith("-")]
        setting = (not reading and len(positional) >= 2) or flags & {"--unset", "--unset-all", "--add",
                                                                       "--replace-all", "--remove-section",
                                                                       "--rename-section", "--edit", "-e"}
        if setting:
            key = positional[0] if positional else ""
            if _GIT_EXEC_CONFIG_RE.match(key):
                return f"changes a Git setting that starts programs ({key})"
            if flags & {"--global", "--system"}:
                return "changes your global Git settings (git config --global)"
    return None
