# SPDX-License-Identifier: AGPL-3.0-only
"""Soft approval's pattern checks: destructive and sensitive commands, protected paths,
code that deletes, writes, spawns or touches the registry, risky opens and script runs.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.core.paths import USER_DIR
from accuretta.core.settings import get_settings
from accuretta.core.workspace import _workspace_roots
from accuretta.security.danger import _secret_named
from accuretta.security.data_guard import _soft_data_text_hit
from accuretta.security.git_rules import _GIT_REMOTE_MUTATING_VERBS
from accuretta.security.shell_paths import _soft_resolve
from accuretta.security.soft_paths import _soft_depth_under
from accuretta.security.soft_paths import _soft_target_ok
from pathlib import Path


# git verbs that only read the repo: the only ones soft mode runs freely.
# Everything else (commit, push, checkout, reset, clean, ...) prompts. The git
# tools pass their argv in details, so request_approval can tell read from
# write without regex guesswork.
_GIT_READONLY_VERBS = {
    "status", "log", "diff", "show", "remote", "rev-parse",
    "ls-files", "ls-tree", "grep", "blame",
}


_SOFT_DESTRUCTIVE_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"\bremove-item\b",
    r"\bdel(?:ete)?\s+[/a-z0-9(\"\']",
    r"\berase\s+[/a-z0-9(\"\']",
    r"\brd\s+/s", r"\brmdir\s+/s", r"\brmdir\s+[a-z0-9~./]",
    r"\brm\s+-", r"\brm\s+[/~.]", r"\brm\s+[a-z0-9_]",
    r"\bunlink\s+", r"\bshred\s+",
    r"\breg\s+(?:add|delete|import|restore|copy)\b", r"\bregedit\b",
    r"\bset-itemproperty\b", r"\bnew-itemproperty\b",
    r"\bremove-itemproperty\b", r"\bclear-itemproperty\b",
    r"\bformat\s+[a-z]:", r"\bdisk" r"part\b", r"\bmkfs\b", r"\bwipefs\b", r"\bdd\s+[^|;&]*\bof=/dev/",
    r"\bgit\s+reset\s+--hard", r"\bgit\s+clean\s+-\w*f",
    r"\bgit\s+push\s+[^&|;]*(?:--force\w*|-f(?:\s|$))", r"\bgit\s+rm\b", r"\bgit\s+branch\s+-D\b",
)]


def _is_destructive_command(cmd: str) -> bool:
    """True when a shell/git command line deletes or overwrites data. Matches
    deletion VERBS with arguments (word-bounded), not substrings, so 'firm',
    'model', an 'rm' inside a path, or 'delete' in prose don't trip it. Soft
    approval mode: destructive -> still prompts, everything else runs."""
    if not cmd:
        return False
    return any(p.search(cmd) for p in _SOFT_DESTRUCTIVE_RES)


_SOFT_SENSITIVE_COMMAND_RES = [re.compile(p, re.IGNORECASE) for p in (
    r"\bstart-process\b", r"\binvoke-item\b", r"\bexplorer(?:\.exe)?\b",
    r"\btaskkill\b", r"\bstop-process\b", r"\bkill(?:all)?\b",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bsc(?:\.exe)?\s+(?:create|config|delete|start|stop)\b",
    r"\b(?:register|set|disable|enable|unregister)-scheduledtask\b",
    # netsh and bcdedit ask only when they change something; `netsh ... show`,
    # `bcdedit /enum` and plain `bcdedit` only read (blue-team checks use them).
    r"\bnetsh(?:\.exe)?\b[^\r\n;|&]*?\s(?:set|add|delete|del|reset|install|uninstall|import|export|connect|disconnect)\b",
    r"\bset-netfirewall",
    r"\bbcdedit(?:\.exe)?\b[^\r\n;|&]*?\s/(?:set|deletevalue|delete|create|copy|import|export|default|"
    r"bootsequence|displayorder|timeout|toolsdisplayorder|ems|bootems|debug|bootdebug|dbgsettings|"
    r"hypervisorsettings|event|eventsettings|clean)\b",
    r"\brunas\b", r"\bstart-process\b[^\r\n]*\b-verb\s+runas\b",
    r"\b(?:set-content|out-file)\b", r"(?<![<>=])>(?![>=])",
    # Running code that isn't on disk for review. `| iex` and download|shell
    # are refused outright by _clickfix_cmd; these are the variants it lets
    # through: a plain pipe into a shell, installs straight from a URL or git
    # remote, and classic Windows download-to-disk vectors.
    r"\|\s*(?:sh|bash|zsh|dash|ksh|pwsh|powershell)(?:\.exe)?\b",
    r"\bpip3?\s+install\b[^\r\n;&|]*(?:https?://|git\+|\.git(?:\s|$))",
    r"\b(?:npm|pnpm|yarn)\s+(?:install|i|add)\b[^\r\n;&|]*(?:https?://|git\+|github:|gitlab:|bitbucket:)",
    r"\bcertutil(?:\.exe)?\b[^\r\n]*-urlcache\b",
    r"\b(?:downloadstring|downloadfile|downloaddata)\b",
)]


def _soft_command_needs_approval_raw(cmd: str) -> bool:
    """Raw-text soft check, used when a command line hands text to another
    evaluator (iex, cmd /c, bash -c, ...): quoted strings are then commands,
    so nothing can be read as plain data."""
    text = cmd or ""
    git_match = re.search(r"\bgit(?:\.exe)?\s+([a-z][a-z-]*)\b", text, re.IGNORECASE)
    mutating_git = False
    if git_match:
        verb = git_match.group(1).lower()
        mutating_git = verb not in _GIT_READONLY_VERBS
        if verb == "remote":
            remote_tail = text[git_match.end():]
            remote_subcommand = re.match(r"\s*(?:-\S+\s+)*([a-z][a-z-]*)", remote_tail, re.IGNORECASE)
            mutating_git = bool(remote_subcommand
                                and remote_subcommand.group(1).lower() in _GIT_REMOTE_MUTATING_VERBS)
    return (_is_destructive_command(text) or mutating_git
            or any(pattern.search(text) for pattern in _SOFT_SENSITIVE_COMMAND_RES))


# Patterns from the raw lists that the assessment resolves structurally
# (deletions, file writes, redirects, launches); every other pattern in those
# lists still asks whenever it appears in the command words.
_SOFT_STRUCTURAL_PATTERNS = {
    r"\bremove-item\b", r"\bdel(?:ete)?\s+[/a-z0-9(\"\']", r"\berase\s+[/a-z0-9(\"\']",
    r"\brd\s+/s", r"\brmdir\s+/s", r"\brmdir\s+[a-z0-9~./]",
    r"\brm\s+-", r"\brm\s+[/~.]", r"\brm\s+[a-z0-9_]",
    r"\bgit\s+rm\b",
    r"\bstart-process\b", r"\binvoke-item\b", r"\b(?:set-content|out-file)\b",
    r"(?<![<>=])>(?![>=])",
    # Stopping a process is fine when Accuretta started it (see _process_stop_concerns).
    r"\btaskkill\b", r"\bstop-process\b", r"\bkill(?:all)?\b",
}


_SOFT_ALWAYS_ASK_RES = [
    p for p in (*_SOFT_DESTRUCTIVE_RES, *_SOFT_SENSITIVE_COMMAND_RES)
    if p.pattern not in _SOFT_STRUCTURAL_PATTERNS
] + [re.compile(p, re.IGNORECASE) for p in (
    # `schtasks /query` only reads; creating one is a hold-to-approve change.
    r"\bschtasks(?:\.exe)?\b[^\r\n;|&]*?\s/(?:create|delete|change|run|end)\b",
    r"\bfind\b[^\r\n;|]*\s-(?:delete|exec(?:dir)?)\b",
)]


# These judge a command by its arguments (an install URL, a download source),
# which are often quoted, so they are matched with the literals put back.
_SOFT_ARGUMENT_RES = [p for p in _SOFT_ALWAYS_ASK_RES
                      if any(w in p.pattern for w in ("pip3", "(?:npm|pnpm|yarn)", "certutil", "downloadstring"))]


# What each always-ask pattern means, for the approval card. First match wins.
_SOFT_PATTERN_LABELS = (
    (("unlink", "shred"), "deletes files"),
    (("reg\\s", "regedit", "itemproperty"), "changes the Windows registry"),
    (("format", "disk", "mkfs", "wipefs", "of=/dev"), "formats or overwrites a disk"),
    (("git\\s+reset", "git\\s+clean", "git\\s+push", "git\\s+branch"), "rewrites or deletes Git history"),
    (("explorer",), "opens File Explorer"),
    (("taskkill", "stop-process", "kill"), "stops a running program"),
    (("shutdown", "restart-computer", "stop-computer"), "shuts down or restarts the computer"),
    (("\\bsc(",), "changes a Windows service"),
    (("scheduledtask", "schtasks"), "changes scheduled tasks"),
    (("netsh", "netfirewall"), "changes network or firewall settings"),
    (("bcdedit",), "changes boot settings"),
    (("runas",), "runs with administrator or another user's rights"),
    (("|\\s*(?:sh",), "pipes text straight into a shell"),
    (("pip3", "npm"), "installs a package straight from a URL or Git"),
    (("certutil", "download"), "downloads a file with a Windows download tool"),
    (("find",), "deletes files matched by find"),
)


def _soft_pattern_label(pattern: re.Pattern) -> str:
    for needles, label in _SOFT_PATTERN_LABELS:
        if any(n in pattern.pattern for n in needles):
            return label
    return "matches a risky command pattern"


_SOFT_DOC_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg", ".ico",
                  ".mp4", ".webm", ".mov", ".mkv", ".avi", ".mp3", ".wav", ".flac", ".ogg",
                  ".pdf", ".html", ".htm", ".txt", ".md", ".csv", ".json", ".log"}


def _soft_protected(resolved: tuple[str, str] | None) -> bool:
    """Destinations whose change persists or reaches outside the project:
    Windows / Program Files / ProgramData, startup folders, PowerShell profile
    folders, ~/.ssh and shell dotfiles, and the app's own folder."""
    if resolved is None or resolved[0] != "host":
        return False
    path = resolved[1]
    home = Path.home()
    roots = [os.environ.get(k) for k in ("WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")]
    roots += [str(home / ".ssh"), str(home / "Documents" / "WindowsPowerShell"),
              str(home / "Documents" / "PowerShell"), str(ROOT), str(USER_DIR), str(DATA)]
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"))
    roots += [str(home / name) for name in (".bashrc", ".bash_profile", ".profile", ".zshrc",
                                            ".gitconfig", ".npmrc", ".pypirc")]
    for root in filter(None, roots):
        if _soft_depth_under(path, os.path.normcase(os.path.normpath(root))) >= 0:
            return True
    return False


# Accuretta's data folder (settings with tokens, credentials, chats, memories)
# and the MCP key file (bridge_mcp_config.json, in the app folder). The file
# tools refuse both outright (is_data_path); a command that names them, matches
# them with a wildcard, or searches file contents through a folder that holds
# them, asks in Soft mode and gates /goal. Reports and their evidence stay free
# to read; so does bridge_mcp_config.example.json.
_SOFT_DATA_REASON = "reads or changes Accuretta's data folder (settings, tokens, chats)"


_SOFT_KEYS_REASON = "reads or changes Accuretta's MCP key file (bridge_mcp_config.json)"


_SOFT_KEYS_TEXT_RE = re.compile(r"bridge_mcp_config(?!\.example)", re.IGNORECASE)


def _soft_data_text_reason(text: str) -> str:
    """The key file's name, the data folder's path, or its environment
    variable appears in the text (see _soft_data_text_hit)."""
    if _SOFT_KEYS_TEXT_RE.search(text or ""):
        return _SOFT_KEYS_REASON
    return _SOFT_DATA_REASON if _soft_data_text_hit(text) else ""


def _soft_param_values(args: list[str], params: tuple[str, ...]) -> list[str]:
    """Values given for any of `params` (`-Destination x` or `-Destination:x`)."""
    values = []
    for i, a in enumerate(args):
        key, _, inline = a.partition(":")
        if key.lower() in params:
            if inline:
                values.append(inline)
            elif i + 1 < len(args):
                values.append(args[i + 1])
    return values


_SOFT_PY_NAME_RE = re.compile(r"python[\d.]*w?|py")


_SOFT_JS_RUNNERS = {"node", "deno", "bun", "tsx", "ts-node"}


_SOFT_PS_HOSTS = {"powershell", "pwsh"}


_SOFT_SH_HOSTS = {"bash", "sh", "zsh", "dash", "ksh"}


_SOFT_SCRIPT_EXTS = {".py": "python", ".pyw": "python", ".js": "js", ".mjs": "js", ".cjs": "js",
                     ".ts": "js", ".mts": "js", ".ps1": "ps", ".sh": "sh", ".bash": "sh"}


_SOFT_CODE_DELETE_RES = {
    "python": re.compile(r"\b(?:shutil\.rmtree|rmtree|os\.(?:remove|unlink|rmdir|removedirs)|send2trash)\b"
                         r"|\.(?:unlink|rmdir)\s*\("),
    "js": re.compile(r"\b(?:rmSync|rmdirSync|unlinkSync|rimraf|emptyDirSync|removeSync|Deno\.remove(?:Sync)?)\b"
                     r"|\b(?:fse?|fsp|promises)\.(?:rm|rmdir|unlink|remove|emptyDir)\s*\("
                     r"|(?<![.\w])(?:rm|rmdir|unlink)\s*\("),
}


_SOFT_CODE_WRITE_RES = {
    "python": re.compile(r"\bopen\s*\([^)]*['\"][rbt+]*[wax]|\bwrite_(?:text|bytes)\b"
                         r"|\bshutil\.(?:copy\w*|move)\b|\bos\.(?:rename|replace|symlink)\b"),
    "js": re.compile(r"\b(?:writeFile|appendFile|copyFile|createWriteStream|cpSync|symlink)\w*"
                     r"|\b(?:fse?|fsp|promises)\.(?:rename|cp|copy|move)\w*\s*\("),
}


_SOFT_CODE_SPAWN_RE = re.compile(
    r"\b(?:subprocess|os\.system|os\.popen|Popen|child_process|execSync|execFileSync|spawnSync|"
    r"execa|Deno\.Command|Bun\.spawn|shelljs)\b|(?<![.\w])(?:exec|spawn|execFile)\s*\(")


# Python's exec() runs Python, not a program: only these start processes.
_SOFT_PY_SPAWN_RE = re.compile(
    r"\b(?:subprocess|os\.system|os\.popen|Popen|os\.exec[lv]p?e?|os\.spawn\w*|os\.startfile|pty\.spawn|"
    r"asyncio\.create_subprocess_\w+)\b")


def _soft_code_spawns(code: str, lang: str) -> bool:
    return bool((_SOFT_PY_SPAWN_RE if lang == "python" else _SOFT_CODE_SPAWN_RE).search(code or ""))


# A destructive program as the first item of an argv list: ["rm", "-rf", x].
_SOFT_CODE_ARGV_RE = re.compile(
    r"[\[(,]\s*[rbuf]?['\"](?:rm|del|erase|rmdir|rd|remove-item|reg|regedit|schtasks|sc|taskkill|"
    r"shutdown|format|diskpart|bcdedit|netsh|cipher|vssadmin|takeown|icacls)(?:\.exe)?['\"]",
    re.IGNORECASE)


# Arguments that make those programs read-only: `["schtasks", "/query"]`,
# `["netsh", "advfirewall", "show"]`, `["reg", "query"]`, ...
_SOFT_CODE_ARGV_READONLY = {
    "schtasks": re.compile(r"['\"]/query['\"]", re.IGNORECASE),
    "netsh": re.compile(r"['\"](?:show|dump)['\"]", re.IGNORECASE),
    "sc": re.compile(r"['\"](?:query|queryex|qc|qdescription|qfailure|qtriggerinfo|sdshow|getdisplayname|"
                     r"getkeyname|enumdepend|qprivs|qsidtype|showsid)['\"]", re.IGNORECASE),
    "bcdedit": re.compile(r"['\"]/(?:enum|v)['\"]", re.IGNORECASE),
    "reg": re.compile(r"['\"]query['\"]", re.IGNORECASE),
    "icacls": re.compile(r"^(?![^\]\)]*['\"]/(?:grant|deny|reset|setowner|remove|setintegritylevel|restore)\b)",
                         re.IGNORECASE),
    "vssadmin": re.compile(r"['\"]list['\"]", re.IGNORECASE),
}


def _soft_code_argv_mutates(code: str) -> bool:
    """A destructive program starts an argv list in the code, without the
    arguments that make it only read."""
    for m in _SOFT_CODE_ARGV_RE.finditer(code):
        prog = re.search(r"['\"]([\w-]+)", m.group(0)).group(1).lower()
        window = code[m.end():m.end() + 200].split("]", 1)[0].split(")", 1)[0]
        check = _SOFT_CODE_ARGV_READONLY.get(prog)
        if check is not None and check.search(window):
            continue
        return True
    return False


_SOFT_CODE_REGISTRY_RE = re.compile(r"\bwinreg\.(?:SetValue\w*|DeleteKey\w*|DeleteValue|CreateKey\w*)")


# Reaching for the home folder or a system folder through the environment.
_SOFT_CODE_HOME_RE = re.compile(
    r"\b(?:expanduser|Path\.home|homedir|USERPROFILE|APPDATA|LOCALAPPDATA|HOMEPATH|HOMEDRIVE|"
    r"PROGRAMDATA|SYSTEMROOT|WINDIR|OneDrive|HOME)\b|['\"`]~[\\/]")


_SOFT_CODE_SELF_DIR_RE = re.compile(r"__file__|__dirname|import\.meta|Deno\.mainModule|sys\.argv\[0\]")


_SOFT_CODE_STRING_RE = re.compile(r"[rRbBuUfF]{0,2}(\"\"\"|'''|\"|'|`)(.*?)(?<!\\)\1", re.DOTALL)


# Shell patterns that mean the same in code as on the command line; the rest
# (redirects, Start-Process, kill, installs) are ordinary inside a program.
_SOFT_CODE_SHELL_RES = [p for p in (*_SOFT_DESTRUCTIVE_RES, *_SOFT_ALWAYS_ASK_RES)
                        if not any(w in p.pattern for w in (
                            "kill(?:all)", "explorer", "pip3", "npm", "certutil", "download",
                            "|\\s*(?:sh", "find\\b", "itemproperty"))]


_SOFT_DOWNLOAD_RE = re.compile(
    r"\b(?:iwr|irm|invoke-webrequest|invoke-restmethod|start-bitstransfer|wget|curl(?:\.exe)?)\b"
    r"|\b(?:downloadfile|downloadstring)\b|-outfile\b", re.IGNORECASE)


def _soft_code_run(name: str, head: str, args: list[str], posix: bool) -> tuple[str, str, str | None] | None:
    """What code a simple command runs: (language, how, token). `how` is
    "inline" (token holds the code), "file" (token is the script path),
    "command" (PowerShell -Command text), "stdin" or "opaque". None when the
    command runs no code soft can read, e.g. `python -m http.server`."""
    ext = os.path.splitext(name)[1]
    if ext in _SOFT_SCRIPT_EXTS and not name.startswith("-"):
        lang = _SOFT_SCRIPT_EXTS[ext]
        return (lang, "file", head) if lang != "sh" or posix else None
    if _SOFT_PY_NAME_RE.fullmatch(name):
        lang, inline, valued = "python", {"-c"}, {"-x", "-w", "-q"}
    elif name in _SOFT_JS_RUNNERS:
        if name == "node" and any(a.lower() in ("-c", "--check") for a in args):
            return None                      # syntax check only
        lang, inline = "js", {"-e", "--eval", "-p", "--print"}
        valued = {"-r", "--require", "--import", "--loader", "--input-type", "--env-file", "--conditions"}
        if name in ("deno", "bun") and args:
            sub = args[0].lower()
            if sub == "eval":
                return "js", "inline", next((a for a in args[1:] if not a.startswith("-")), None)
            if sub == "run":
                args = args[1:]
            elif name == "deno" or os.path.splitext(sub)[1] not in _SOFT_SCRIPT_EXTS:
                return None                  # deno fmt, bun install, bun x ...
    elif name in _SOFT_PS_HOSTS:
        i = 0
        while i < len(args):
            low = args[i].lower()
            if low in ("-file", "-f"):
                return "ps", "file", args[i + 1] if i + 1 < len(args) else None
            if low in ("-command", "-c"):
                return "ps", "command", " ".join(args[i + 1:])
            if low in ("-encodedcommand", "-enc", "-e", "-ec"):
                return "ps", "opaque", None
            if low in ("-executionpolicy", "-ep", "-windowstyle", "-outputformat", "-inputformat",
                       "-configurationname", "-version", "-psconsolefile", "-workingdirectory", "-wd"):
                i += 2
                continue
            if not low.startswith("-"):
                rest = " ".join(args[i:])
                return ("ps", "file", args[i]) if name == "pwsh" else ("ps", "command", rest)
            i += 1
        return None
    elif name in _SOFT_SH_HOSTS and posix:
        for i, a in enumerate(args):
            if re.fullmatch(r"-\w*c", a):
                return "sh", "inline", args[i + 1] if i + 1 < len(args) else None
            if not a.startswith("-"):
                return "sh", "file", a
        return None
    else:
        return None
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if low in inline:
            return lang, "inline", args[i + 1] if i + 1 < len(args) else None
        if lang == "python" and low == "-m":
            return None
        if a == "-":
            return lang, "stdin", None
        if low in valued:
            i += 2
            continue
        if a.startswith("<"):                # heredoc / redirect feeding stdin
            return lang, "stdin", None
        if re.match(r"[\d*]?>|&>", a):       # output redirect: `2>&1`, `> out.txt`
            i += 1 if re.search(r">.", a.lstrip("&")) or a.endswith("&1") else 2
            continue
        if not a.startswith("-"):
            return lang, "file", a
        i += 1
    return (lang, "stdin", None) if not args else None


def _soft_scan_code(code: str, lang: str, label: str, *, base_ok: bool, self_dir_ok: bool,
                    posix: bool, project_file: bool = False) -> list[str]:
    """Concerns for Python or JavaScript source about to run. `base_ok`: the
    process runs from inside the workspace or temp. `self_dir_ok`: the code's
    own file sits there (it counts when the code builds paths from it)."""
    concerns = []
    hit = _soft_data_text_reason(code)
    if hit:
        concerns.append(f"{label} {hit}")
    strings = [m.group(2) for m in _SOFT_CODE_STRING_RE.finditer(code)]
    outside, protected = [], []
    for s in strings:
        value = s.replace("\\\\", "\\").strip()
        if not (re.match(r"^[a-zA-Z]:[\\/]|^\\\\|^~[\\/]", value) or (posix and value.startswith("/"))):
            if value.startswith(".." + os.sep) or value.startswith("../") or value == "..":
                outside.append(value)
            continue
        res = _soft_resolve('"0"', [value], {}, None, posix)
        if _soft_protected(res):
            protected.append(value)
        # A workspace folder itself (sys.path.insert(0, r"C:\\proj")) is not outside.
        if not _soft_target_ok(res, delete=False) and not (
                res and res[0] == "host" and any(_soft_depth_under(res[1], r) == 0 for r in _workspace_roots())):
            outside.append(value)
    if _code_secret_named(strings, code) and re.search(
            r"\bopen\s*\(|read_(?:text|bytes)|readFile|shutil\.copy|copyFile|createReadStream", code):
        concerns.append(f"{label} reads a private key or saved password file")
    if _SOFT_CODE_DELETE_RES[lang].search(code) and not _soft_code_deletes_only_temp(code, lang):
        here_ok = base_ok or (self_dir_ok and bool(_SOFT_CODE_SELF_DIR_RE.search(code)))
        if outside:
            concerns.append(f"{label} deletes files and names a path outside your workspace: {outside[0][:120]}")
        elif _SOFT_CODE_HOME_RE.search(code):
            concerns.append(f"{label} deletes files and reaches for your home or a system folder")
        elif not here_ok:
            concerns.append(f"{label} deletes files and runs from outside your workspace")
    if protected and _SOFT_CODE_WRITE_RES[lang].search(code):
        concerns.append(f"{label} writes into a protected folder: {protected[0][:120]}")
    if _SOFT_CODE_REGISTRY_RE.search(code) and not (
            project_file and not re.search(r"winreg\.HKEY_(?:LOCAL_MACHINE|CLASSES_ROOT|USERS)|ConnectRegistry", code)):
        # A project's own script that keeps its settings under HKCU (an app's
        # "start with Windows" switch) is the project, not the agent, at work.
        concerns.append(f"{label} changes the Windows registry")
    if _soft_code_spawns(code, lang):
        if _soft_code_argv_mutates(code):
            concerns.append(f"{label} runs a program that deletes or changes the system")
        for s in strings:
            hit = next((p for p in _SOFT_CODE_SHELL_RES if p.search(s)), None)
            if hit:
                concerns.append(f"{label} runs a shell command that {_soft_pattern_label(hit)}"
                                if hit in _SOFT_ALWAYS_ASK_RES
                                else f"{label} runs a shell command that deletes files")
                break
    return concerns


_SOFT_PY_TEMP_ASSIGN_RE = re.compile(
    r"\b(\w+)\s*=\s*(?:Path\s*\(\s*|os\.path\.join\s*\(\s*|(?:Path|pathlib\.Path)\s*\(\s*)?"
    r"tempfile\.(?:mkdtemp|mkstemp|gettempdir|NamedTemporaryFile|TemporaryDirectory)\b"
    r"|\bwith\s+tempfile\.(?:TemporaryDirectory|NamedTemporaryFile)\s*\([^)]*\)\s+as\s+(\w+)")


_SOFT_PY_DELETE_CALL_RE = re.compile(
    r"\b(?:shutil\.rmtree|rmtree|os\.(?:remove|unlink|rmdir|removedirs)|send2trash)\s*\(\s*([^,)]+)"
    r"|\b([A-Za-z_]\w*)\s*(?:/\s*['\"][^'\"]*['\"]\s*)?\)?\.(?:unlink|rmdir)\s*\(")


_SOFT_PY_PATH_ASSIGN_RE = re.compile(
    r"\b(\w+)\s*=\s*(?:Path|pathlib\.Path)?\s*\(?\s*[rR]?([\"'])([^\"'\n]+)\2\s*\)?\s*(?:#.*)?$", re.MULTILINE)


def _soft_code_deletes_only_temp(code: str, lang: str) -> bool:
    """Every deletion in the Python code works on a temp folder it made itself
    (tempfile.mkdtemp / TemporaryDirectory / gettempdir), or on a variable set
    to one literal path that Soft would let a shell delete (inside a project,
    in temp). Whatever else the code names - a test payload like
    "../internal/notes.txt", a sys.path entry - nothing else is removed."""
    if lang != "python":
        return False
    temp_vars = {m.group(1) or m.group(2) for m in _SOFT_PY_TEMP_ASSIGN_RE.finditer(code)}
    literal_vars: dict[str, list[str]] = {}
    for m in _SOFT_PY_PATH_ASSIGN_RE.finditer(code):
        literal_vars.setdefault(m.group(1), []).append(m.group(3))
    calls = list(_SOFT_PY_DELETE_CALL_RE.finditer(code))
    if not calls:
        return False
    for m in calls:
        arg = (m.group(1) or m.group(2) or "").strip()
        head = re.match(r"(?:Path\s*\(\s*)?(\w+)", arg)
        name = head.group(1) if head else ""
        if name in temp_vars or "tempfile." in arg:
            continue
        values = literal_vars.get(name) or []
        if len(values) == 1 and name not in temp_vars:
            res = _soft_resolve('"0"', [values[0].replace("\\\\", "\\")], {}, None, False)
            if res and _soft_target_ok(res, delete=True):
                continue
        return False
    return True


def _workspace_project_names() -> set[str]:
    """Names of the workspace folders and the projects directly inside them."""
    names: set[str] = set()
    for root in _workspace_roots():
        names.add(os.path.basename(root.rstrip("\\/")).lower())
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_dir():
                        names.add(entry.name.lower())
        except OSError:
            continue
    return {n for n in names if len(n) >= 4}


_SOFT_SCRIPTY_EXTS = {".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".scr", ".msi", ".msp",
                      ".reg", ".lnk", ".url", ".cpl", ".ps1"}


def _soft_open_concern(name: str, args: list[str], target, flags: set, resolve, show, text: str) -> str | None:
    """Start-Process / Invoke-Item. Opening a document, or starting a program
    that lives in the workspace (the thing it just built) or temp, runs; so does
    an app on the desktop allowlist. Elevation, installers and scripts that
    can't be read, programs elsewhere and web pages ask."""
    verb = {f.split(":", 1)[0] for f in flags}
    if "-verb" in verb:
        verb_value = " ".join(show(v).lower() for v in _soft_param_values(args, ("-verb",)))
        if "runas" in verb_value:
            return "runs a program as administrator (Start-Process -Verb RunAs)"
    if "-credential" in verb:
        return "runs a program as another user (Start-Process -Credential)"
    if not target:
        return f"starts a program that can't be checked first ({name})"
    shown = show(target)
    if re.match(r"(?i)^(?:https?|ftp)://", shown):
        return f"opens a web page in your browser: {shown}"
    res = resolve(target)
    if res and not re.search(r"[\\/]", shown) and not os.path.exists(res[1]):
        res = None                                     # a bare name is found on PATH, not in this folder
    ext = os.path.splitext(res[1] if res else shown)[1].lower()
    if res and ext in _SOFT_DOC_EXTS and not verb & {"-argumentlist", "-args"}:
        return None                                    # a picture, page or log
    if res and res[0] == "host" and _soft_target_ok(res, delete=False):
        if ext in _SOFT_SCRIPTY_EXTS:
            return f"starts a script or installer that can't be read first: {shown}"
        if _SOFT_DOWNLOAD_RE.search(text) and os.path.basename(res[1]).lower() in text.lower():
            return f"starts a program the same command downloads: {shown}"
        return None                                    # the program it just built
    try:
        if _app_matches_allowlist(shown):
            return None
    except Exception:
        pass
    stem = os.path.splitext(os.path.basename((res[1] if res else shown).rstrip("\\/")))[0].lower()
    if res and ext in (".exe", ".com") and len(stem) >= 4 and stem in _workspace_project_names():
        return None                                    # the installed build of a workspace project
    return f"starts a program outside your workspace: {shown}"


def _code_secret_named(strings: list[str], code: str) -> bool:
    """The same for source code: one string holding the path, or a home lookup
    with the folder and file as separate parts (Path.home() / ".ssh" / "id_rsa")."""
    if any(_secret_named(s) for s in strings):
        return True
    if not _SOFT_CODE_HOME_RE.search(code):
        return False
    parts = {s.strip().strip("/\\").lower() for s in strings}
    if ".ssh" in parts and any(re.fullmatch(r"id_[\w-]+|[\w.-]+\.(?:pem|key|ppk)", p) for p in parts):
        return True
    return ".aws" in parts and "credentials" in parts


def _app_matches_allowlist(name_or_path: str) -> bool:
    """Loose, forgiving match: any token in the allowlist appears in the target.
    Matches on exe basename (minus .exe) AND full path, case-insensitive."""
    s = get_settings()
    allow = [a.strip().lower() for a in (s.get("desktop_app_allowlist") or []) if a.strip()]
    if not allow:
        return False
    target = name_or_path.lower()
    base = os.path.basename(target)
    if base.endswith(".exe"):
        base = base[:-4]
    for entry in allow:
        # entries can be "notepad", "notepad.exe", "C:\\Program Files\\...\\App.exe",
        # or a regex-ish substring. plain substring match covers all cases.
        e = entry[:-4] if entry.endswith(".exe") else entry
        if e in target or e in base or e == base:
            return True
    return False


def _git_argv_is_readonly(argv) -> bool:
    if not isinstance(argv, list) or not argv or not isinstance(argv[0], str):
        return False
    verb = argv[0].lower()
    if verb not in _GIT_READONLY_VERBS:
        return False
    if verb != "remote":
        return True
    subcommand = next((str(arg).lower() for arg in argv[1:]
                       if isinstance(arg, str) and not arg.startswith("-")), "")
    return subcommand not in _GIT_REMOTE_MUTATING_VERBS


_SOFT_GIT_EXTRA_VERBS = {"clone", "fetch", "init", "add"}
