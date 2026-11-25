# SPDX-License-Identifier: AGPL-3.0-only
"""Soft approval's reading of a shell command: what it deletes, writes, kills, opens or
runs, which of its targets are private or outside the workspace, and the script it would
execute.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from accuretta.core.paths import DATA
from accuretta.core.workspace import _workspace_roots
from accuretta.core.sandbox_distro import SANDBOX_DISTRO
from accuretta.security.danger import _secret_named
from accuretta.security.data_paths import _key_files
from accuretta.security.data_paths import _path_within
from accuretta.security.data_paths import is_data_path
from accuretta.security.data_paths import is_key_file
from accuretta.security.git_rules import _git_concern
from accuretta.security.shell_paths import _soft_expand_delete_targets
from accuretta.security.shell_paths import _soft_resolve
from accuretta.security.shell_text import _shell_code_view
from accuretta.security.shell_vars import _soft_assignments
from accuretta.security.shell_vars import _soft_expand
from accuretta.security.soft_paths import _SOFT_REAL_LINUX
from accuretta.security.soft_paths import _soft_depth_under
from accuretta.security.soft_paths import _soft_target_ok
from accuretta.security.soft_paths import _soft_temp_roots
from accuretta.security.soft_patterns import _SOFT_ALWAYS_ASK_RES
from accuretta.security.soft_patterns import _SOFT_ARGUMENT_RES
from accuretta.security.soft_patterns import _SOFT_DATA_REASON
from accuretta.security.soft_patterns import _SOFT_DOWNLOAD_RE
from accuretta.security.soft_patterns import _SOFT_KEYS_REASON
from accuretta.security.soft_patterns import _SOFT_SH_HOSTS
from accuretta.security.soft_patterns import _soft_code_run
from accuretta.security.soft_patterns import _soft_command_needs_approval_raw
from accuretta.security.soft_patterns import _soft_data_text_reason
from accuretta.security.soft_patterns import _soft_open_concern
from accuretta.security.soft_patterns import _soft_param_values
from accuretta.security.soft_patterns import _soft_pattern_label
from accuretta.security.soft_patterns import _soft_protected
from accuretta.security.soft_patterns import _soft_scan_code
from accuretta.security.stop_concerns import _process_stop_concerns


# Evaluators whose text can't be followed: iex, Invoke-Command, wmic, ...
# (PowerShell -Command, cmd /c "...", wsl and bash -c are read like any other
# command: their text is checked with the same rules.)
_SOFT_EVALUATOR_RE = re.compile(
    r"\b(?:iex|invoke-expression|invoke-command|wmic)\b|scriptblock\]::create|invokecommand\.",
    re.IGNORECASE)


_SOFT_OPAQUE_HEADS = {"iex", "invoke-expression", "invoke-command", "wmic"}


# Wrappers whose text is checked as its own command: cmd /c "...", wsl ...,
# and on the host bash -c "..." (WSL's or Git's bash).
_SOFT_CMD_SWITCH_RE = re.compile(r"/[a-z](?::\S*)?$", re.IGNORECASE)


# HTTP DELETE against an API removes remote data; the method is often quoted.
_SOFT_HTTP_DELETE_RE = re.compile(r"(?:-method|-x|--request)\s*[=\s]\s*[\"']?delete\b", re.IGNORECASE)


_SOFT_DELETE_VERBS = {"remove-item", "ri", "rm", "del", "erase", "rmdir", "rd"}


_SOFT_WRITE_VERBS = {"set-content", "sc", "add-content", "ac", "out-file"}


_SOFT_PLACE_VERBS = {"copy-item", "cp", "copy", "cpi", "move-item", "mv", "move", "mi",
                     "rename-item", "ren", "rni", "new-item", "ni", "tee-object", "tee",
                     "expand-archive"}


_SOFT_OPEN_VERBS = {"start-process", "start", "saps", "invoke-item", "ii"}


_SOFT_CD_VERBS = {"cd", "set-location", "sl", "chdir", "pushd", "push-location"}


_SOFT_PREFIX_WORDS = {"then", "do", "else", "elif", "sudo", "nohup", "time", "exec",
                      "xargs", "env", "command", "builtin", "!"}


_SOFT_VALUE_PARAMS = {"-erroraction", "-ea", "-warningaction", "-wa", "-credential",
                      "-stream", "-encoding", "-itemtype", "-type", "-value", "-name",
                      "-newname", "-width", "-inputobject", "-windowstyle", "-workingdirectory"}


_SOFT_NULL_TARGETS = {"$null", "nul", "/dev/null", "null"}


def _soft_split_commands(code: str, lits: list[str]) -> list[tuple[str, list[str], str]]:
    """(command word, argument tokens, head as written) for every simple
    command in the code view, in order. Splits on ; | & newlines and
    braces/parens, and skips leading keywords and wrappers (then, sudo,
    xargs, ...)."""
    commands = []
    for chunk in re.split(r"\|\||&&|[;|&\n{}()]", code):
        tokens = chunk.split()
        while tokens and tokens[0].lower() in _SOFT_PREFIX_WORDS:
            tokens = tokens[1:]
        if tokens:
            head = tokens[0]
            quoted = re.fullmatch(r'"(\d+)"', head)
            if quoted:                       # & "Remove-Item" path, & "C:\tools\x.exe"
                head = lits[int(quoted.group(1))]
            name = head.lower().replace("/", "\\").rsplit("\\", 1)[-1].removesuffix(".exe")
            commands.append((name, tokens[1:], tokens[0]))
    return commands


_SOFT_DATA_SEARCH_REASON = "searches through Accuretta's data folder (settings, tokens, chats)"


_SOFT_KEYS_SEARCH_REASON = "searches through Accuretta's MCP key file (bridge_mcp_config.json)"


# Commands that only list names: a wildcard that happens to match the key
# file or the data folder is harmless there unless the pipeline reads contents.
_SOFT_LISTERS = {"get-childitem", "gci", "ls", "dir", "get-item", "gi", "test-path",
                 "resolve-path", "rvpa", "split-path", "join-path"}


# Commands that read file contents, and the ones that recurse with no flag.
_SOFT_CONTENT_READERS = {
    "select-string", "sls", "findstr", "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "get-content", "gc", "cat", "type", "copy-item", "cp", "copy", "cpi", "xcopy",
    "robocopy", "compress-archive", "tar", "7z", "zip"}


_SOFT_RECURSIVE_BY_DEFAULT = {"rg", "ag", "ack", "tar", "compress-archive", "7z"}


_SOFT_RECURSIVE_FLAGS = {"-recurse", "-r", "/s", "/e", "/mir", "--recursive", "-a"}


def _soft_glob_reason(pattern: str, listing: bool) -> str:
    """A wildcard path that can match the key file or reach into DATA."""
    sep = "\\" if os.name == "nt" else "/"
    pat = os.path.normcase(pattern)
    if not listing and any(fnmatch.fnmatchcase(k, pat) for k in _key_files()):
        return _SOFT_KEYS_REASON
    parts = pat.split(sep)
    data_parts = os.path.normcase(str(DATA)).split(sep)
    deeper = len(parts) > len(data_parts)
    if ((deeper or (len(parts) == len(data_parts) and not listing))
            and all(fnmatch.fnmatchcase(d, p) for d, p in zip(data_parts, parts))):
        return _SOFT_DATA_REASON
    return ""


def _soft_private_reason(res: tuple[str, str] | None, listing: bool = False) -> str:
    """Why a resolved argument touches the data folder or the key file."""
    if not res or res[0] != "host":
        return ""
    path = res[1]
    if "*" in path or "?" in path:
        return _soft_glob_reason(path, listing)
    if is_key_file(path):
        return _SOFT_KEYS_REASON
    return _SOFT_DATA_REASON if is_data_path(path) else ""


def _soft_holds_private(res: tuple[str, str] | None) -> str:
    """A folder somewhere above the data folder or the key file (the app
    folder, home, C:\\): searching its contents searches them too."""
    if not res or res[0] != "host":
        return ""
    path = os.path.normcase(res[1])
    if _path_within(os.path.normcase(str(DATA)), path) and not _path_within(path, str(DATA)):
        return _SOFT_DATA_SEARCH_REASON
    if any(k != path and _path_within(k, path) for k in _key_files()):
        return _SOFT_KEYS_SEARCH_REASON
    return ""


def _soft_data_concerns(text: str, code: str, lits: list[str], commands, cwd: str | None,
                        posix: bool, preset: dict[str, str] | None) -> list[str]:
    """Why a command touches the data folder or the key file; empty when it
    doesn't."""
    hit = _soft_data_text_reason(text)
    if hit:
        return [hit]
    variables = {**(preset or {}), **_soft_assignments(code, lits, posix)}
    readers = any(name in _SOFT_CONTENT_READERS for name, _, _ in commands)
    cur = cwd
    for name, args, head in commands:
        if name in _SOFT_CD_VERBS:
            dest = next((a for a in args if not a.startswith("-")), None)
            res = _soft_resolve(dest, lits, variables, cur, posix) if dest else None
            hit = _soft_private_reason(res)
            if hit:
                return [hit]
            cur = res[1] if res else None
            continue
        if name in ("popd", "pop-location"):
            cur = None
            continue
        resolved = [_soft_resolve(a, lits, variables, cur, posix)
                    for a in args if a and not a.startswith("-")]
        listing = name in _SOFT_LISTERS and not readers
        hit = next((x for x in (_soft_private_reason(r, listing) for r in resolved) if x), "")
        if hit:
            return [hit]
        flags = {a.lower() for a in args if a.startswith(("-", "/"))}
        recursive = (name in _SOFT_RECURSIVE_BY_DEFAULT or bool(flags & _SOFT_RECURSIVE_FLAGS)
                     or (name in ("grep", "egrep", "fgrep", "cp", "zip")
                         and any(re.fullmatch(r"-[a-z]*r[a-z]*", f) for f in flags)))
        if not (recursive and readers):
            continue
        folders = [r for r in resolved if r and r[0] == "host" and os.path.isdir(r[1])]
        if not folders and cur and not cur.startswith("/"):
            folders = [("host", os.path.normcase(os.path.normpath(cur)))]
        hit = next((x for x in map(_soft_holds_private, folders) if x), "")
        if hit:
            return [hit]
    return []


def _soft_positional(args: list[str], path_params: tuple[str, ...]) -> tuple[list[str], list[str], set[str]]:
    """Split PowerShell-style arguments into (path-param values, positionals,
    other flags seen). Values of known value-taking parameters are skipped."""
    named, positional, flags = [], [], set()
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low.startswith("-") and len(low) > 1 and not re.match(r"^-\d", low):
            key = low.split(":", 1)[0]
            flags.add(key)
            if key in path_params:
                if ":" in a:
                    named.append(a.split(":", 1)[1])
                elif i + 1 < len(args):
                    named.append(args[i + 1])
                    i += 1
            elif key in _SOFT_VALUE_PARAMS or key in ("-filter", "-include", "-exclude",
                                                      "-argumentlist", "-args", "-verb",
                                                      "-destination", "-destinationpath"):
                i += 1
        elif a == "--":
            positional.extend(args[i + 1:])
            break
        else:
            positional.extend(x for x in a.split(",") if x)
        i += 1
    return named, positional, flags


# Start-Process may run these in the background. Running them in the
# foreground already passes, so this is the same check (their code is read).
_SOFT_DEV_TOOLS = {"python", "python3", "pythonw", "py", "node", "deno", "bun", "tsx", "ts-node",
                   "npm", "npx", "pnpm", "yarn", "uv", "uvicorn", "java", "dotnet", "go", "cargo"}


_SOFT_SCRIPT_MAX_BYTES = 1_000_000


def _soft_local_path(resolved: tuple[str, str]) -> str:
    """Where this side can open a resolved path (sandbox paths through
    \\\\wsl.localhost)."""
    zone, path = resolved
    if zone == "guest":
        return "\\\\wsl.localhost\\" + SANDBOX_DISTRO + path.replace("/", "\\")
    return path


def _soft_read_script(resolved: tuple[str, str] | None) -> str | None:
    """Text of a script about to run, or None when it can't be read (missing,
    unresolved, too large). A folder runs its entry file (index.js, the
    package.json main, __main__.py)."""
    if resolved is None:
        return None
    path = _soft_local_path(resolved)
    if os.path.isdir(path):
        main = None
        try:
            with open(os.path.join(path, "package.json"), encoding="utf-8") as f:
                main = json.load(f).get("main")
        except (OSError, ValueError, AttributeError):
            pass
        for name in filter(None, (main, "index.js", "index.mjs", "__main__.py")):
            if os.path.isfile(os.path.join(path, name)):
                path = os.path.join(path, name)
                break
    try:
        if os.path.getsize(path) > _SOFT_SCRIPT_MAX_BYTES:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _soft_code_concerns(run: tuple[str, str, str | None], lits: list[str], variables: dict[str, str],
                        cwd: str | None, posix: bool, kind: str, stdin_code: str | None,
                        depth: int, command_text: str) -> list[str]:
    """Concerns for the code one command runs (see _soft_code_run)."""
    lang, how, token = run
    if depth >= 3:
        return ["runs code nested too deep to check"]
    if how == "opaque":
        return ["runs an encoded PowerShell command that can't be read"]
    def literal(tok: str | None) -> str | None:
        if tok is None:
            return None
        return re.sub(r'"(\d+)"', lambda m: lits[int(m.group(1))], tok)
    if how == "file":
        # The script resolves against the real process folder (the app folder
        # on the host when nothing cd'd), unlike deletions, which need a cd.
        res = _soft_resolve(token, lits, variables, cwd or (None if posix else os.getcwd()),
                            posix) if token else None
        shown = (lits[int(token[1:-1])] if token and re.fullmatch(r'"\d+"', token) else token) or "?"
        label = f"script {os.path.basename(shown.replace('/', os.sep))}"
        code = _soft_read_script(res)
        if code is None and res is not None and not os.path.exists(_soft_local_path(res)):
            # Not there yet: either the same command writes it (its text is
            # in the command, so read that), downloads it, or it fails.
            if _SOFT_DOWNLOAD_RE.search(command_text):
                return [f"runs a script the same command downloads: {shown}"]
            base = os.path.basename(res[1].replace("/", "\\").rsplit("\\", 1)[-1]).lower()
            if command_text.lower().count(base) < 2:
                return []
            code, label = "\n".join(lits), f"{label} (written by this command)"
        if code is None:
            return [f"runs a script that can't be read: {shown}"]
        sep = "/" if res[0] == "guest" else "\\"
        script_dir = res[1].rsplit(sep, 1)[0] or sep
        self_dir_ok = _soft_target_ok((res[0], script_dir), delete=False)
    else:
        code = stdin_code if how == "stdin" else literal(token)
        if code is None:
            return [f"runs {'Python' if lang == 'python' else 'code'} from input that can't be read"]
        label = {"python": "the Python code", "js": "the JavaScript code",
                 "ps": "powershell -Command", "sh": "bash -c"}[lang]
        script_dir, self_dir_ok = None, False
    if lang in ("ps", "sh"):
        inner_kind = "sandbox" if lang == "sh" else ("session" if kind == "session" else "powershell")
        preset = {"psscriptroot": script_dir} if lang == "ps" and script_dir else {}
        inner = _soft_command_concerns(code, inner_kind, cwd, _depth=depth + 1, _vars=preset)
        if how == "file":
            # Build and helper scripts cd around and write to computed paths;
            # inside a script only writes known to land outside still ask.
            inner = [c for c in inner if not c.startswith("writes somewhere that can't be checked")]
        return [f"{label}: {c}" for c in inner]
    base_zone = "guest" if posix and cwd and cwd.startswith("/") else "host"
    base_ok = bool(cwd) and _soft_target_ok((base_zone, cwd), delete=False)
    # A script file of a workspace project (not run_python's temp script).
    project_file = (how == "file" and bool(script_dir) and not posix
                    and any(_soft_depth_under(os.path.normcase(script_dir), r) >= 0 for r in _workspace_roots()))
    return _soft_scan_code(code, lang, label, base_ok=base_ok, self_dir_ok=self_dir_ok, posix=posix,
                           project_file=project_file)


def _soft_delete_reason(res: tuple[str, str] | None, shown: str) -> str:
    """Why a deletion target asks, in words that name what would be lost."""
    if res is None:
        return f"deletes something that can't be checked: {shown}"
    if res[0] == "guest":
        return f"deletes a top-level folder in the sandbox: {shown}"
    path = res[1].rstrip("\\*")
    for root in _workspace_roots():
        depth = _soft_depth_under(path, root)
        if depth == 0:
            return f"deletes your whole workspace folder: {shown}"
        if depth == 1:
            if os.path.isdir(path) or not os.path.splitext(path)[1]:
                return f"deletes a whole project folder: {shown}"
            return f"deletes a file at the top of your workspace that isn't there yet: {shown}"
        if depth >= 2:
            return f"deletes something that can't be checked: {shown}"
    if any(_soft_depth_under(path, root) == 0 for root in _soft_temp_roots()):
        return f"deletes the whole temp folder: {shown}"
    return f"deletes outside your workspace: {shown}"


def _soft_command_concerns(cmd: str, kind: str = "powershell", cwd: str | None = None, *,
                           code_out: list | None = None, _depth: int = 0,
                           _vars: dict[str, str] | None = None,
                           run_cwd: str | None = None,
                           targets_out: list | None = None) -> list[str]:
    """Why soft mode should ask before running this command line; empty when
    it can run. See the section comment above for the rules. Concerns that
    come from code the command runs are also added to `code_out` (/goal gates
    on those, since its own check only reads the command line). `run_cwd` is
    the folder the command really runs in: relative paths resolve against it
    (the shell starts there), and a `cd` in the command moves from there.
    `targets_out` collects the files and folders it is allowed to delete, so
    the caller can put them in the undo journal first."""
    text = cmd or ""
    if not text.strip():
        return []
    posix = kind in ("sandbox", "wsl")
    if kind == "wsl":
        token = _SOFT_REAL_LINUX.set(True)
        try:
            return _soft_command_concerns(text, "sandbox", cwd, code_out=code_out, _depth=_depth,
                                          _vars=_vars, targets_out=targets_out)
        finally:
            _SOFT_REAL_LINUX.reset(token)
    if not posix and not cwd and run_cwd and not str(run_cwd).startswith("/"):
        cwd = run_cwd
    code, lits = _shell_code_view(text, posix)
    if not posix:
        # Remove-Item (Join-Path $dir "x.png"): the joined path is one argument.
        def _join(m: re.Match) -> str:
            part = lambda t: lits[int(t[1:-1])] if t.startswith('"') else t  # noqa: E731
            lits.append(part(m.group(1)).rstrip("\\/") + "\\" + part(m.group(2)).lstrip("\\/"))
            return f'"{len(lits) - 1}"'
        code = re.sub(r"\(\s*join-path\s+(?:-path\s+)?(\"\d+\"|\$\w+)\s+(?:-childpath\s+)?(\"\d+\"|[^\s;|)]+)\s*\)",
                      _join, code, flags=re.IGNORECASE)
    commands = _soft_split_commands(code, lits)
    data = _soft_data_concerns(text, code, lits, commands, cwd or run_cwd, posix, _vars)
    if data and code_out is not None:
        code_out.extend(data)                # /goal stops for these too
    if (_SOFT_EVALUATOR_RE.search(code)
            or any(name in _SOFT_OPAQUE_HEADS for name, args, _ in commands)):
        # Merges and null-device redirects never write anything.
        raw = re.sub(r"[\d*]?>&\d|[\d*]?>\s*(?:\$null|nul|/dev/null)(?![\w/])", " ", text, flags=re.IGNORECASE)
        if _soft_command_needs_approval_raw(raw):
            reasons = ["passes text to another shell (iex, Invoke-Command, ...), so it can't be checked"]
            if code_out is not None:
                code_out.extend(reasons)
            return data + reasons
        return data
    concerns: list[str] = list(data)
    secret_readers = _SOFT_CONTENT_READERS | {"move-item", "mv", "move", "mi", "get-filehash"}
    if (any(name in secret_readers for name, _, _ in commands)
            and _secret_named(re.sub(r'"(\d+)"', lambda m: '"' + lits[int(m.group(1))] + '"', code))):
        concerns.append("reads a private key or saved password file")
        if code_out is not None:
            code_out.append("reads a private key or saved password file")
    # Literals put back (separators blanked) for the argument-sensitive patterns.
    inlined = re.sub(r'"(\d+)"',
                     lambda m: '"' + re.sub(r"[\r\n;|&]", " ", lits[int(m.group(1))]) + '"', code)
    for pattern in _SOFT_ALWAYS_ASK_RES:
        if pattern.search(inlined if pattern in _SOFT_ARGUMENT_RES else code):
            concerns.append(_soft_pattern_label(pattern))
    if _SOFT_HTTP_DELETE_RE.search(text):
        concerns.append("sends an HTTP DELETE request")

    variables = {**(_vars or {}), **_soft_assignments(code, lits, posix)}
    resolve = lambda tok: _soft_resolve(tok, lits, variables, cur_cwd, posix)  # noqa: E731

    def show(tok: str) -> str:
        m = re.fullmatch(r'"(\d+)",?', tok)
        return lits[int(m.group(1))] if m else tok

    # Code fed on stdin: heredoc bodies (in order) and literals or variables
    # piped straight into an interpreter.
    heredocs = [lits[int(m.group(2))] for m in re.finditer(r"<<-?\s*(['\"]?)\w+\1[^\n]*\n\"(\d+)\"", code)]
    piped = []
    for m in re.finditer(r"(?:\"(\d+)\"|\$(\w+))\s*\|\s*(?:&\s*)?(?:python[\d.]*w?|py|node|deno|bun)(?:\.exe)?\b",
                         code, re.IGNORECASE):
        piped.append(lits[int(m.group(1))] if m.group(1) else variables.get(m.group(2).lower()))

    def code_concerns(run, run_cwd, args) -> list[str]:
        stdin_code = None
        if run[1] == "stdin":
            fed = any(a.startswith("<<") and not a.startswith("<<<") for a in args)
            if fed and heredocs_left:
                stdin_code = heredocs_left.pop(0)
            elif piped:
                stdin_code = piped.pop(0)
            elif not args:
                return []                    # a bare REPL start (session input)
        found = _soft_code_concerns(run, lits, variables, run_cwd, posix, kind, stdin_code, _depth, text)
        if code_out is not None:
            code_out.extend(found)
        return found

    # Redirects: drop stream merges and null-device targets, check the rest.
    cur_cwd = cwd
    redirect_code = re.sub(r"[\d*]?>&\d", " ", code)
    for chunk_cwd, chunk in _soft_cwd_chunks(redirect_code, lits, variables, cwd, posix):
        cur_cwd = chunk_cwd
        for m in re.finditer(r"(?:&|[\d*])?>>?\s*([^\s;|&)]+)", chunk):
            target = m.group(1)
            literal_target = lits[int(target[1:-1])] if re.fullmatch(r'"\d+"', target) else target
            if literal_target.lower() in _SOFT_NULL_TARGETS:
                continue
            res = resolve(target)
            if not _soft_target_ok(res, delete=False):
                concerns.append(f"writes somewhere that can't be checked: {literal_target}" if res is None
                                else f"writes outside your workspace: {literal_target}")

    cur_cwd = cwd
    heredocs_left = list(heredocs)
    for name, args, head in commands:
        if name in _SOFT_CD_VERBS:
            dest = next((a for a in args if not a.startswith("-")), None)
            res = _soft_resolve(dest, lits, variables, cur_cwd, posix) if dest else None
            cur_cwd = res[1] if res else None
            continue
        if name in ("popd", "pop-location"):
            cur_cwd = None
            continue
        run = _soft_code_run(name, head, args, posix)
        if (run and not posix and re.fullmatch(r'"\d+"', head)
                and not re.search(r"(?:&|(?:^|[\s;(])\.)\s*" + re.escape(head), code)):
            run = None                       # a bare string is output, not a call: $c.IndexOf("x.js")
        if run:
            concerns += code_concerns(run, cur_cwd, args)
            continue
        if any(a.startswith("<<") and not a.startswith("<<<") for a in args) and heredocs_left:
            heredocs_left.pop(0)             # cat > file <<EOF: the body is data
        if _depth < 3 and not posix and name == "cmd" and any(a.lower() in ("/c", "/k", "/r") for a in args):
            # cmd /c "...": check the text it runs, with cmd's /switches dropped.
            at = next(i for i, a in enumerate(args) if a.lower() in ("/c", "/k", "/r"))
            payload = args[at + 1:]
            # One quoted string is the whole command; otherwise keep quoted paths whole.
            inner = show(payload[0]) if len(payload) == 1 else " ".join(
                f'"{show(a)}"' if " " in show(a) else show(a) for a in payload)
            found = _soft_command_concerns(inner, "cmd", cur_cwd, code_out=code_out, _depth=_depth + 1,
                                           targets_out=targets_out)
            concerns += [f"cmd /c: {c}" for c in found]
            continue
        if _depth < 3 and not posix and (name == "wsl" or (name in _SOFT_SH_HOSTS and any(
                re.fullmatch(r"-\w*c", a) for a in args))):
            # wsl ... / bash -c "...": the user's own Linux, read with Linux rules.
            rest = list(args)
            if name == "wsl":
                while rest and rest[0].startswith("-"):
                    flag = rest.pop(0).lower()
                    if flag in ("-d", "--distribution", "-u", "--user", "--cd") and rest:
                        rest.pop(0)
            else:
                rest = [name] + rest
            if rest and rest[0].lower() in _SOFT_SH_HOSTS and len(rest) >= 3 and re.fullmatch(r"-\w*c", rest[1]):
                inner = show(rest[2])
            else:
                inner = " ".join(show(a) if " " not in show(a) else f'"{show(a)}"' for a in rest)
            found = _soft_command_concerns(inner, "wsl", None, code_out=code_out, _depth=_depth + 1)
            concerns += [f"wsl: {c}" for c in found]
            continue
        if name in _SOFT_KILL_VERBS:
            concerns += _process_stop_concerns(name, args, lits, code, posix, cur_cwd)
            continue
        if name == "sc" and not posix and (head.lower().endswith(".exe") or (args and show(args[0]).lower() in (
                "query", "queryex", "qc", "qdescription", "qfailure", "qtriggerinfo", "sdshow", "getdisplayname",
                "getkeyname", "enumdepend", "qprivs", "qsidtype", "showsid", "start", "stop", "pause",
                "continue", "config", "create", "delete", "failure", "description", "sdset", "control",
                "interrogate", "privs", "sidtype", "triggerinfo"))):
            continue                         # sc.exe (services), not the Set-Content alias
        if name == "git":
            argv = [show(a) for a in args]
            concern = _git_concern(argv)
            if concern:
                concerns.append(concern)
            continue
        if name in _SOFT_DELETE_VERBS:
            if posix:
                targets = [a for a in args if not a.startswith("-")]
                contents = False
            else:
                named, positional, flags = _soft_positional(args, ("-path", "-literalpath"))
                targets = named + positional
                if kind == "cmd":
                    targets = [t for t in targets if not _SOFT_CMD_SWITCH_RE.fullmatch(t)]
                contents = bool(flags & {"-filter", "-include", "-exclude"})
            targets = _soft_expand_delete_targets(targets, code, lits, posix, cur_cwd, contents)
            if not targets:
                concerns.append(f"runs {name} on files that aren't known in advance")
            for t in targets:
                res = resolve(t)
                if res and contents:
                    sep = "/" if res[0] == "guest" else "\\"
                    res = (res[0], res[1].rstrip(sep) + sep + "*")
                if res is None and not posix:
                    res = _soft_partial_target(t, lits, variables, cur_cwd)
                if not _soft_target_ok(res, delete=True):
                    concerns.append(_soft_delete_reason(res, show(t)))
                    continue
                if res and res[0] == "host":
                    big = _big_delete_concern(res[1], show(t))
                    if big:
                        concerns.append(big)
                    elif targets_out is not None:
                        targets_out.append(res[1])
            continue
        if name in _SOFT_WRITE_VERBS and not posix:
            named, positional, _ = _soft_positional(args, ("-path", "-literalpath", "-filepath"))
            targets = named or positional[:1]
            if not targets:
                concerns.append(f"runs {name} on files that aren't known in advance")
            for t in targets:
                res = resolve(t)
                if not _soft_target_ok(res, delete=False):
                    concerns.append(f"writes somewhere that can't be checked: {show(t)}" if res is None
                                    else f"writes outside your workspace: {show(t)}")
            continue
        if name in _SOFT_PLACE_VERBS:
            copying = name in ("copy-item", "cp", "copy", "cpi", "expand-archive", "tee-object", "tee")
            if posix:
                paths = [a for a in args if not a.startswith("-")]
                checked = paths[-1:] if copying else paths
            else:
                named, positional, _ = _soft_positional(args, ("-path", "-literalpath", "-filepath"))
                dest = _soft_param_values(args, ("-destination", "-destinationpath"))
                if copying and name not in ("tee-object", "tee"):
                    checked = dest or positional[1:2]
                else:
                    checked = named + positional + dest
            for t in checked:
                if _soft_protected(resolve(t)):
                    concerns.append(f"puts files in a protected folder ({name}): {show(t)}")
            continue
        if name in _SOFT_OPEN_VERBS and not posix:
            named, positional, flags = _soft_positional(args, ("-filepath", "-path", "-literalpath"))
            target = (named or positional or [None])[0]
            # Start-Process $py: follow a variable set to one literal path.
            show_open = lambda tok: (_soft_expand(show(tok), variables, posix) or show(tok))  # noqa: E731
            tool = show_open(target).lower().replace("/", "\\").rsplit("\\", 1)[-1].removesuffix(".exe") if target else ""
            if tool in _SOFT_DEV_TOOLS and not flags & {"-verb", "-credential"}:
                # A dev server or script in the background: check it as if run directly.
                pieces, i = [], 0
                while i < len(args):
                    if args[i].lower() in ("-argumentlist", "-args"):
                        j = i + 1
                        while j < len(args) and not re.match(r"-[a-z]", args[j], re.IGNORECASE):
                            pieces += [p for p in args[j].split(",") if p]
                            j += 1
                        break
                    i += 1
                values = [show(p) for p in pieces]
                argline = values[0] if len(values) == 1 else " ".join(
                    f'"{v}"' if " " in v and not v.startswith('"') else v for v in values)
                workdir = _soft_param_values(args, ("-workingdirectory",))
                wd_res = resolve(workdir[0]) if workdir else None
                run_cwd = wd_res[1] if wd_res else (None if workdir else cur_cwd)
                inner = _soft_command_concerns(f"{tool} {argline}", kind, run_cwd, code_out=code_out,
                                               _depth=_depth + 1)
                concerns += [f"background {tool}: {c}" for c in inner]
                continue
            concern = _soft_open_concern(name, args, target, flags, resolve, show_open, text)
            if concern:
                concerns.append(concern)
            continue
    return list(dict.fromkeys(concerns))      # dedupe, keep order


def _soft_partial_target(token: str, lits: list[str], variables: dict[str, str],
                         cwd: str | None) -> tuple[str, str] | None:
    """A deletion target whose folder is known but whose last part is a
    variable (`$snap = Join-Path $pub $name`): counts like a narrowed wildcard
    in that folder, so it passes only where `folder\\*.x` would."""
    m = re.fullmatch(r'"(\d+)"', token)
    text = lits[int(m.group(1))] if m else token
    for _ in range(3):                       # follow $a -> "$b\x" -> ...
        whole = re.fullmatch(r"\$(\w+)", text.strip())
        if not whole or whole.group(1).lower() not in variables:
            break
        text = variables[whole.group(1).lower()]
    parts = re.split(r"[\\/]", text.strip())
    if len(parts) < 2 or not re.fullmatch(r"\$\w+", parts[-1]) or any(".." in p for p in parts):
        return None
    folder = _soft_resolve('"0"', ["\\".join(parts[:-1])], variables, cwd, False)
    if not folder or folder[0] != "host":
        return None
    return "host", folder[1].rstrip("\\") + "\\*.var"


def _soft_cwd_chunks(code: str, lits: list[str], variables: dict[str, str],
                     cwd: str | None, posix: bool):
    """Yield (cwd in effect, chunk) for each command chunk, following literal
    cd / Set-Location / Push-Location so relative redirect targets resolve."""
    cur = cwd
    for chunk in re.split(r"\|\||&&|[;\n]", code):
        tokens = chunk.split()
        while tokens and tokens[0].lower() in _SOFT_PREFIX_WORDS:
            tokens = tokens[1:]
        if tokens and tokens[0].lower() in _SOFT_CD_VERBS:
            dest = next((a for a in tokens[1:] if not a.startswith("-")), None)
            res = _soft_resolve(dest, lits, variables, cur, posix) if dest else None
            cur = res[1] if res else None
            continue
        if tokens and tokens[0].lower() in ("popd", "pop-location"):
            cur = None
            continue
        yield cur, chunk


# -- processes --------------------------------------------------------------------
_SOFT_KILL_VERBS = {"stop-process", "spps", "kill", "taskkill", "tskill", "pkill", "killall"}


_REBUILDABLE_DIRS = {"build", "dist", "out", "target", "node_modules", "__pycache__", ".venv", "venv",
                     ".pytest_cache", ".mypy_cache", ".ruff_cache", ".next", ".nuxt", ".parcel-cache",
                     ".turbo", "coverage", "htmlcov", ".tox", "bin", "obj", ".gradle", ".cache"}


_BIG_DELETE_FILES = 400


_BIG_DELETE_BYTES = 300 * 1024 * 1024


def _big_delete_concern(path: str, shown: str) -> str | None:
    """A folder too big to snapshot for undo that isn't a build/cache folder."""
    if not os.path.isdir(path) or os.path.basename(path.rstrip("\\/")).lower() in _REBUILDABLE_DIRS:
        return None
    if any(_soft_depth_under(path, r) >= 0 for r in _soft_temp_roots()):
        return None
    files = size = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d.lower() not in _REBUILDABLE_DIRS]
            files += len(filenames)
            for fn in filenames[:200]:
                try:
                    size += os.path.getsize(os.path.join(dirpath, fn))
                except OSError:
                    pass
            if files > _BIG_DELETE_FILES or size > _BIG_DELETE_BYTES:
                return f"deletes a big folder ({files}+ files) that can't be undone: {shown}"
    except OSError:
        return None
    return None


_SOFT_PROMPT_KINDS = {"delete", "registry", "launch"}


def _soft_command_needs_approval(cmd: str, kind: str = "powershell", cwd: str | None = None) -> bool:
    """Keep Soft useful for normal project work without silently allowing
    commands that delete/overwrite data outside the project, launch or kill
    apps, or change core operating-system state."""
    return bool(_soft_command_concerns(cmd, kind, cwd))
