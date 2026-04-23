"""
Accuretta bridge — local Ollama proxy + tools + approvals + static server.

One process. Serves the frontend (index.html / app.js / app.css / Design Change)
and exposes JSON/SSE endpoints for chat streaming, tool invocation, workspace,
versioning, settings, and command approvals.

Runs on 0.0.0.0:8787 so Tailscale / LAN peers can reach it from phones.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any
import webbrowser
import base64 as _b64
import io as _io
from concurrent.futures import ThreadPoolExecutor

# ---- desktop automation (optional) ---------------------------------------
# Graceful imports — desktop tools will clearly report when these are missing
# so the agent can tell the user what to install. No import failures kill startup.
try:
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = True       # moving mouse to (0,0) aborts any automation
    pyautogui.PAUSE = 0.05          # small pause between clicks/keys for stability
    _HAVE_PYAUTOGUI = True
except Exception:
    pyautogui = None  # type: ignore
    _HAVE_PYAUTOGUI = False

try:
    from PIL import Image  # type: ignore
    _HAVE_PIL = True
except Exception:
    Image = None  # type: ignore
    _HAVE_PIL = False

try:
    import pygetwindow as _pgw  # type: ignore
    _HAVE_PGW = True
except Exception:
    _pgw = None  # type: ignore
    _HAVE_PGW = False

# Kill switch: when set, every desktop action tool refuses immediately.
# The frontend panic button and the user deny-action both flip this via
# `/api/desktop/panic`. Cleared by /api/desktop/resume or a new chat turn.
_desktop_panic = threading.Event()

# Rate limiter for desktop actions — defense in depth. Hard cap regardless
# of what the agent requests. Tunable in settings.
_desktop_action_times: list[float] = []
_desktop_action_lock = threading.Lock()


ROOT = Path(__file__).parent.resolve()
DATA = ROOT / "data"
VERSIONS_DIR = DATA / "versions"
PENDING_DIR = DATA / "pending"
SNAPSHOTS_DIR = DATA / "snapshots"
CHATS_FILE = DATA / "chats.json"
SETTINGS_FILE = DATA / "settings.json"
WORKSPACE_FILE = DATA / "workspace.json"
SYSTEM_CONTEXT_FILE = DATA / "ACCURETTA.md"
MEMORIES_FILE = DATA / "memories.jsonl"
MEMORIES_MAX_INJECT = 15          # how many to load into every system prompt
MEMORIES_TEXT_CAP = 220           # per-entry char cap — token-efficient
IGNORE_FILE_NAME = ".accurettaignore"

# per-chat ephemeral desktop kill switch.  lives in memory only — restarting
# the bridge resets every chat to its global setting.
_chat_desktop_disabled: set[str] = set()

# tracks which chat a tool is being invoked inside, so the per-chat kill
# switch can short-circuit desktop tools without plumbing chat_id through
# every function. it's a plain module variable set on the worker thread
# before run_chat_turn and cleared after.
import contextvars
_current_chat_id: contextvars.ContextVar[str] = contextvars.ContextVar("_current_chat_id", default="")

# per-chat SSE emitter so tools can stream progress without plumbing emit through every call
_chat_emitters: dict[str, callable] = {}

# thread pool for long-running tools so the HTTP worker stays responsive
_tool_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-")

# async tool jobs (for POST /api/tools/call returning job-id)
_tool_jobs: dict[str, dict] = {}
_tool_jobs_lock = threading.Lock()

for d in (DATA, VERSIONS_DIR, PENDING_DIR, SNAPSHOTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

def _resolve_ollama_url() -> str:
    raw = (os.environ.get("OLLAMA_HOST") or "").strip()
    if not raw:
        return "http://127.0.0.1:11434"
    # accept forms: "http://x:port", "x:port", "x", "0.0.0.0", "0.0.0.0:11434"
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    # split into scheme://host[:port]
    try:
        scheme, rest = raw.split("://", 1)
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "11434"
    except Exception:
        return "http://127.0.0.1:11434"
    # 0.0.0.0 / :: mean "bind all" server-side — meaningless as a client target
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port}"

OLLAMA = _resolve_ollama_url()
PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))

# ---- persistence helpers ---------------------------------------------------

_FILE_LOCK = threading.Lock()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, value: Any) -> None:
    with _FILE_LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


DEFAULT_SETTINGS = {
    "model": "",
    "vision_model": "lighton-ocr",
    # desktop automation (off by default — opt-in, gated behind approvals)
    "desktop_enabled": False,
    "desktop_app_allowlist": [],            # e.g. ["notepad", "chrome", "code"]; matched case-insensitive against exe/launch target
    "desktop_max_actions_per_minute": 30,   # hard rate limit for agent-driven actions
    "desktop_auto_approve_read": True,      # screenshot/describe/list_windows never require approval
    "num_ctx": 8192,
    "num_gpu": 99,
    "num_batch": 512,
    "num_thread": 0,
    "num_predict": -1,
    "temperature": 0.7,
    "top_p": 0.9,
    "keep_alive": "30m",
    "theme": "light",
    "auto_approve_read": True,
    "allow_web_preview": True,
    # memory / performance
    "kv_cache_type": "q8_0",        # q4_0 | q8_0 | f16 — lower = less VRAM, slightly lower quality
    # IDE preview extras (composer toolbar toggles)
    "use_tailwind_cdn": False,      # inject Tailwind Play CDN into preview + ask model to use tailwind classes
    "ide_multifile": False,         # tell the model to emit a small folder structure (index.html / style.css / script.js / assets/)
}


def get_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    out = {**DEFAULT_SETTINGS, **(s if isinstance(s, dict) else {})}
    return out


def get_workspace() -> dict:
    ws = load_json(WORKSPACE_FILE, {"folders": []})
    if not isinstance(ws, dict):
        ws = {"folders": []}
    ws.setdefault("folders", [])
    return ws


def get_chats() -> dict:
    c = load_json(CHATS_FILE, {"chats": {}, "order": []})
    if not isinstance(c, dict):
        c = {"chats": {}, "order": []}
    c.setdefault("chats", {})
    c.setdefault("order", [])
    return c


# ---- token counting (approximation) ----------------------------------------
# We don't bundle tiktoken — this is a fast, conservative heuristic that
# works across languages. It errs on the side of over-counting so we don't
# accidentally overflow the context window.
# English text ~4 chars/tok, code ~3 chars/tok, CJK ~1.5 chars/tok.
# We use a blended 3.0 to stay safe.
CHARS_PER_TOKEN = 3.0


def _approx_tokens(text: str) -> int:
    if not text:
        return 0
    # byte-length penalises non-ASCII (CJK, emoji) which are multi-byte
    return max(len(text), len(text.encode("utf-8"))) // CHARS_PER_TOKEN


def _count_msg_tokens(msg: dict) -> int:
    content = msg.get("content") or ""
    # tool_call JSON is also text the model sees
    tcs = msg.get("tool_calls") or []
    extra = json.dumps(tcs, ensure_ascii=False) if tcs else ""
    return _approx_tokens(content) + _approx_tokens(extra) + 4  # role overhead


def truncate_messages(msgs: list[dict], max_tokens: int, reserve: int = 256) -> list[dict]:
    """Drop oldest non-system messages until the total fits under max_tokens.
    Always keeps the system prompt (index 0) and the most recent user message.
    """
    if not msgs:
        return msgs
    budget = max_tokens - reserve
    system = [msgs[0]] if msgs[0].get("role") == "system" else []
    tail = msgs[len(system):]

    # Count from the end backwards until we hit budget
    total = sum(_count_msg_tokens(m) for m in system)
    keep = []
    for m in reversed(tail):
        t = _count_msg_tokens(m)
        if total + t > budget and keep:
            # We've kept at least one message; drop the rest
            break
        keep.insert(0, m)
        total += t

    if not keep:
        # Emergency: keep only the very last user message
        last_user = [m for m in tail if m.get("role") == "user"][-1:] or tail[-1:]
        keep = last_user

    return system + keep


# ---- system context (ACCURETTA.md) ----------------------------------------
# First-run scan of the user's machine so models know where things are.
# Not automatically readable by tools — it's injected into the system prompt.

# Cache system context scans for 5 minutes so we don't re-scan dirs every turn.
_SYSTEM_CONTEXT_CACHE: tuple[float, dict] | None = None
_SYSTEM_CONTEXT_TTL = 300


def _scan_system_context() -> dict:
    """Return a dict of facts about the machine, cheap to compute."""
    global _SYSTEM_CONTEXT_CACHE
    if _SYSTEM_CONTEXT_CACHE is not None:
        ts, cached = _SYSTEM_CONTEXT_CACHE
        if time.time() - ts < _SYSTEM_CONTEXT_TTL:
            return cached
    facts: dict = {}
    # OS / platform
    try:
        import platform as _plat
        facts["os"] = f"{_plat.system()} {_plat.release()} (build {_plat.version()})"
        facts["machine"] = _plat.machine()
        facts["hostname"] = _plat.node()
    except Exception:
        pass
    # User
    try:
        facts["user"] = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        facts["userprofile"] = os.environ.get("USERPROFILE") or str(Path.home())
    except Exception:
        pass

    home = Path(facts.get("userprofile") or str(Path.home()))

    # Known folders — only include if they exist
    candidates = [
        ("Desktop",     home / "Desktop"),
        ("Documents",   home / "Documents"),
        ("Downloads",   home / "Downloads"),
        ("Pictures",    home / "Pictures"),
        ("Screenshots", home / "Pictures" / "Screenshots"),
        ("Videos",      home / "Videos"),
        ("Music",       home / "Music"),
        ("OneDrive",    home / "OneDrive"),
        ("OneDrive Desktop",   home / "OneDrive" / "Desktop"),
        ("OneDrive Documents", home / "OneDrive" / "Documents"),
        ("OneDrive Pictures",  home / "OneDrive" / "Pictures"),
        ("AppData Local",   home / "AppData" / "Local"),
        ("AppData Roaming", home / "AppData" / "Roaming"),
    ]
    known: list[dict] = []
    for label, p in candidates:
        try:
            if p.exists() and p.is_dir():
                # top-level child count, bounded
                try:
                    n = 0
                    for _ in p.iterdir():
                        n += 1
                        if n >= 5000: break
                except Exception:
                    n = None
                known.append({"label": label, "path": str(p), "items": n})
        except Exception:
            pass
    facts["folders"] = known

    # System drives
    drives: list[str] = []
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            if Path(f"{letter}:\\").exists():
                drives.append(f"{letter}:\\")
    facts["drives"] = drives

    # Program roots (read-only existence check)
    program_roots = []
    for p in [r"C:\Program Files", r"C:\Program Files (x86)", str(home / "AppData" / "Local" / "Programs")]:
        if Path(p).exists():
            program_roots.append(p)
    facts["program_roots"] = program_roots

    # Ollama models dir
    for candidate in [os.environ.get("OLLAMA_MODELS"), str(home / ".ollama" / "models")]:
        if candidate and Path(candidate).exists():
            facts["ollama_models_dir"] = candidate
            break

    facts["scanned_at"] = int(time.time())
    _SYSTEM_CONTEXT_CACHE = (time.time(), facts)
    return facts


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
    if facts.get("ollama_models_dir"):
        lines.append(f"- Ollama models dir: {facts['ollama_models_dir']}")
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


def ensure_system_context() -> str:
    """Create ACCURETTA.md on first run; return the current markdown content."""
    try:
        if not SYSTEM_CONTEXT_FILE.exists():
            facts = _scan_system_context()
            SYSTEM_CONTEXT_FILE.write_text(_render_system_context_md(facts), encoding="utf-8")
        return SYSTEM_CONTEXT_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return f"# ACCURETTA\n\n(could not generate: {e})\n"


def rescan_system_context() -> str:
    try:
        facts = _scan_system_context()
        SYSTEM_CONTEXT_FILE.write_text(_render_system_context_md(facts), encoding="utf-8")
        return SYSTEM_CONTEXT_FILE.read_text(encoding="utf-8")
    except Exception as e:
        return f"# ACCURETTA\n\n(rescan failed: {e})\n"


# ---- workspace / path safety ----------------------------------------------

BLOCKED_PATH_PATTERNS = [
    re.compile(r"^[a-zA-Z]:\\Windows(\\|$)", re.IGNORECASE),
    re.compile(r"\\System32\\", re.IGNORECASE),
    re.compile(r"^[a-zA-Z]:\\Windows\\System32", re.IGNORECASE),
]


def normalize_path(p: str) -> str:
    p = os.path.expandvars(os.path.expanduser(p or ""))
    try:
        return str(Path(p).resolve())
    except Exception:
        return p


def is_blocked_path(p: str) -> bool:
    n = normalize_path(p)
    for pat in BLOCKED_PATH_PATTERNS:
        if pat.search(n):
            return True
    return False


def is_in_workspace(p: str) -> bool:
    """True if path is inside any workspace folder. Empty workspace = open access."""
    ws = get_workspace().get("folders", [])
    if not ws:
        return True
    n = normalize_path(p).lower()
    for folder in ws:
        f = normalize_path(folder).lower()
        if n == f or n.startswith(f + os.sep):
            return True
    return False


# ---- .accurettaignore -----------------------------------------------------
# gitignore-lite. each workspace folder may ship a `.accurettaignore` with one
# glob per line. blank lines and `#` comments are skipped. matching is done
# with fnmatch against (a) the path's basename and (b) the POSIX path relative
# to the workspace root — so `node_modules` catches any subtree of that name
# and `build/*.map` targets nested files. lines starting with `!` negate.
import fnmatch


_IGNORE_CACHE: dict[str, tuple[float, list[tuple[bool, str]]]] = {}


def _read_ignore_rules(ws_root: str) -> list[tuple[bool, str]]:
    ip = Path(ws_root) / IGNORE_FILE_NAME
    try:
        mtime = ip.stat().st_mtime if ip.exists() else 0.0
    except Exception:
        mtime = 0.0
    cached = _IGNORE_CACHE.get(ws_root)
    if cached and cached[0] == mtime:
        return cached[1]
    rules: list[tuple[bool, str]] = []
    if ip.exists():
        try:
            for raw in ip.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                neg = line.startswith("!")
                if neg:
                    line = line[1:].strip()
                if not line:
                    continue
                # strip trailing slashes; a trailing `/` in gitignore-speak means
                # "directory only" but we match by glob regardless
                line = line.rstrip("/")
                rules.append((neg, line))
        except Exception:
            rules = []
    _IGNORE_CACHE[ws_root] = (mtime, rules)
    return rules


def _workspace_root_for(path: str) -> str | None:
    n = normalize_path(path).lower()
    for folder in get_workspace().get("folders", []):
        f = normalize_path(folder)
        fl = f.lower()
        if n == fl or n.startswith(fl + os.sep):
            return f
    return None


def is_ignored(path: str) -> bool:
    """True if `path` matches a rule in the enclosing workspace's .accurettaignore."""
    root = _workspace_root_for(path)
    if not root:
        return False
    rules = _read_ignore_rules(root)
    if not rules:
        return False
    rel = os.path.relpath(normalize_path(path), root).replace("\\", "/")
    if rel == "." or rel.startswith(".."):
        return False
    base = os.path.basename(rel)
    ignored = False
    for neg, pat in rules:
        # match against basename for patterns without a slash; match full rel path
        # for patterns that contain a slash (so `build/*.map` works).
        hit = False
        if "/" in pat:
            if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, pat.rstrip("/") + "/*") or any(
                fnmatch.fnmatch(rel, pat + "/" + "*") for _ in [0]
            ):
                hit = True
        else:
            # bare pattern: match any basename along the path
            parts = rel.split("/")
            if any(fnmatch.fnmatch(p, pat) for p in parts) or fnmatch.fnmatch(base, pat):
                hit = True
        if hit:
            ignored = not neg
    return ignored


# ---- command classification -----------------------------------------------

WRITE_PATTERNS = [
    re.compile(r"\bRemove-Item\b", re.IGNORECASE),
    re.compile(r"\bRemove-ItemProperty\b", re.IGNORECASE),
    re.compile(r"\bSet-Content\b", re.IGNORECASE),
    re.compile(r"\bAdd-Content\b", re.IGNORECASE),
    re.compile(r"\bOut-File\b", re.IGNORECASE),
    re.compile(r"\bNew-Item\b", re.IGNORECASE),
    re.compile(r"\bCopy-Item\b", re.IGNORECASE),
    re.compile(r"\bMove-Item\b", re.IGNORECASE),
    re.compile(r"\bRename-Item\b", re.IGNORECASE),
    re.compile(r"\bSet-ItemProperty\b", re.IGNORECASE),
    re.compile(r"\bNew-ItemProperty\b", re.IGNORECASE),
    re.compile(r"\bInvoke-WebRequest\b.*-OutFile\b", re.IGNORECASE),
    re.compile(r"\bInvoke-RestMethod\b.*-OutFile\b", re.IGNORECASE),
    re.compile(r"\bStart-Process\b", re.IGNORECASE),
    re.compile(r"\b(rm|del|erase|rmdir|rd|mkdir|md|move|copy|xcopy|robocopy|ren)\b", re.IGNORECASE),
    re.compile(r"(^|\s)>\s*\S"),
    re.compile(r"(^|\s)>>\s*\S"),
    re.compile(r"\bgit\s+(push|reset\s+--hard|clean\s+-|checkout\s+--|branch\s+-D)", re.IGNORECASE),
    re.compile(r"\bnpm\s+(install|uninstall|publish)\b", re.IGNORECASE),
    re.compile(r"\bpip\s+(install|uninstall)\b", re.IGNORECASE),
    re.compile(r"\bwinget\s+(install|uninstall|upgrade)\b", re.IGNORECASE),
    re.compile(r"\bchoco\s+(install|uninstall|upgrade)\b", re.IGNORECASE),
    re.compile(r"\breg\s+(add|delete|import)\b", re.IGNORECASE),
    re.compile(r"\bformat\b", re.IGNORECASE),
    re.compile(r"\bdiskpart\b", re.IGNORECASE),
]


def needs_approval(cmd: str) -> bool:
    if not cmd:
        return False
    for pat in WRITE_PATTERNS:
        if pat.search(cmd):
            return True
    return False


# ---- approval queue --------------------------------------------------------

_approvals: dict[str, dict] = {}
_approval_events: dict[str, threading.Event] = {}
_approvals_lock = threading.Lock()


def request_approval(title: str, command: str, details: dict | None = None, timeout_s: int = 600) -> dict:
    """Create a pending approval, block worker until user responds, return decision."""
    aid = uuid.uuid4().hex[:12]
    ev = threading.Event()
    entry = {
        "id": aid,
        "title": title,
        "command": command,
        "details": details or {},
        "created": time.time(),
        "status": "pending",
        "decision": None,
    }
    with _approvals_lock:
        _approvals[aid] = entry
        _approval_events[aid] = ev
    save_json(PENDING_DIR / f"{aid}.json", entry)
    broadcast_event({"type": "approval:new", "approval": entry})
    got = ev.wait(timeout=timeout_s)
    with _approvals_lock:
        final = _approvals.pop(aid, entry)
        _approval_events.pop(aid, None)
    (PENDING_DIR / f"{aid}.json").unlink(missing_ok=True)
    if not got:
        final["status"] = "timeout"
        final["decision"] = "deny"
    return final


def decide_approval(aid: str, decision: str) -> bool:
    with _approvals_lock:
        entry = _approvals.get(aid)
        ev = _approval_events.get(aid)
        if not entry or not ev:
            return False
        entry["decision"] = "approve" if decision == "approve" else "deny"
        entry["status"] = "decided"
        ev.set()
    broadcast_event({"type": "approval:decided", "id": aid, "decision": entry["decision"]})
    return True


def list_approvals() -> list[dict]:
    with _approvals_lock:
        return [dict(v) for v in _approvals.values() if v.get("status") == "pending"]


# ---- SSE event bus ---------------------------------------------------------

_subscribers: list[Queue] = []
_subs_lock = threading.Lock()


def subscribe() -> Queue:
    q: Queue = Queue(maxsize=1024)
    with _subs_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: Queue) -> None:
    with _subs_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def broadcast_event(evt: dict) -> None:
    with _subs_lock:
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except Exception:
                pass


# ---- tool implementations --------------------------------------------------

def tool_list_directory(args: dict) -> dict:
    path = normalize_path(args.get("path") or str(Path.home()))
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}"}
    out = []
    skipped = 0
    try:
        for entry in sorted(os.listdir(path)):
            full = os.path.join(path, entry)
            if is_ignored(full):
                skipped += 1
                continue
            try:
                st = os.stat(full)
                out.append({
                    "name": entry,
                    "path": full,
                    "dir": os.path.isdir(full),
                    "size": st.st_size if os.path.isfile(full) else None,
                    "mtime": int(st.st_mtime),
                })
            except Exception:
                continue
    except PermissionError as e:
        return {"error": f"permission denied: {e}"}
    resp = {"path": path, "entries": out[:200]}
    if skipped:
        resp["ignored"] = skipped
    if len(out) > 200:
        resp["truncated"] = True
    return resp


def tool_read_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    try:
        raw = Path(path).read_bytes()
        if len(raw) > 64 * 1024:
            raw = raw[: 64 * 1024]
            truncated = True
        else:
            truncated = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        return {"path": path, "content": text, "truncated": truncated, "size": os.path.getsize(path)}
    except Exception as e:
        return {"error": str(e)}


def tool_write_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    content = args.get("content", "")
    if not path:
        return {"error": "missing path"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    approval = request_approval(
        title="Write file",
        command=f'Set-Content -Path "{path}" -Value <{len(content)} chars>',
        details={"kind": "write_file", "path": path, "bytes": len(content.encode('utf-8'))},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied write ({approval.get('status')})"}
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": str(e)}


def tool_edit_file(args: dict) -> dict:
    """Apply surgical search-and-replace edits to an existing file.
    Each edit finds old_text and replaces it with new_text.
    old_text must be unique or an exact single match — ambiguous matches are rejected.
    """
    path = normalize_path(args.get("path") or "")
    edits = args.get("edits") or []
    if not path:
        return {"error": "missing path"}
    if not isinstance(edits, list) or not edits:
        return {"error": "edits must be a non-empty list of {old_text, new_text}"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"read failed: {e}"}

    applied = []
    errors = []
    modified = text

    for i, edit in enumerate(edits):
        old = edit.get("old_text", "")
        new = edit.get("new_text", "")
        if not old:
            errors.append(f"edit {i}: old_text is empty")
            continue

        count = modified.count(old)
        if count == 0:
            # try stripping common surrounding whitespace for a fuzzy match
            old_stripped = old.strip()
            if old_stripped and old_stripped != old:
                count = modified.count(old_stripped)
                if count == 1:
                    modified = modified.replace(old_stripped, new, 1)
                    applied.append({"edit": i, "match": "fuzzy", "old": old[:60], "new": new[:60]})
                    continue
                elif count > 1:
                    errors.append(f"edit {i}: fuzzy match '{old[:40]}' appears {count} times — ambiguous")
                    continue
            errors.append(f"edit {i}: '{old[:40]}' not found in file")
            continue
        elif count > 1:
            errors.append(f"edit {i}: '{old[:40]}' appears {count} times — must be unique")
            continue
        else:
            modified = modified.replace(old, new, 1)
            applied.append({"edit": i, "match": "exact", "old": old[:60], "new": new[:60]})

    if errors:
        return {
            "error": "; ".join(errors),
            "applied": len(applied),
            "failed": len(errors),
            "path": path,
        }

    # approval: show a diff-like summary
    diff_lines = []
    for a in applied:
        diff_lines.append(f"- {a['old'][:50]}")
        diff_lines.append(f"+ {a['new'][:50]}")
    preview = "\n".join(diff_lines) if diff_lines else "(no preview)"
    approval = request_approval(
        title="Edit file",
        command=f'edit {len(applied)} location(s) in "{path}"',
        details={"kind": "edit_file", "path": path, "edits": len(applied), "preview": preview},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied edit ({approval.get('status')})"}

    try:
        Path(path).write_text(modified, encoding="utf-8")
        return {
            "ok": True,
            "path": path,
            "edits_applied": len(applied),
            "bytes": len(modified.encode("utf-8")),
        }
    except Exception as e:
        return {"error": str(e)}


def tool_delete_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": f"not found: {path}"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(path):
        return {"error": "path outside workspace"}
    approval = request_approval(
        title="Delete",
        command=f'Remove-Item -Path "{path}" -Recurse -Force',
        details={"kind": "delete", "path": path, "dir": os.path.isdir(path)},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied delete ({approval.get('status')})"}
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}


def _emit_tool_stream(name: str, text: str) -> None:
    """If a chat turn is active, emit a tool_stream SSE event."""
    cid = _current_chat_id.get()
    emit = _chat_emitters.get(cid) if cid else None
    if emit:
        try:
            emit({"type": "tool_stream", "name": name, "text": text[:240]})
        except Exception:
            pass


def _run_powershell(cmd: str, timeout: int = 120) -> dict:
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        return {"error": str(e)}

    out_lines: list[str] = []
    err_lines: list[str] = []

    def reader(pipe, sink, name):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                _emit_tool_stream(name, line.rstrip("\n\r"))
            pipe.close()
        except Exception:
            pass

    t_out = threading.Thread(target=reader, args=(proc.stdout, out_lines, "run_powershell"), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_lines, "run_powershell"), daemon=True)
    t_out.start()
    t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"error": f"timeout after {timeout}s"}

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = "".join(out_lines)[-16000:]
    stderr = "".join(err_lines)[-4000:]
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


def tool_run_powershell(args: dict) -> dict:
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return {"error": "empty command"}
    if needs_approval(cmd):
        approval = request_approval(
            title="PowerShell (write/modify)",
            command=cmd,
            details={"kind": "powershell"},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied command ({approval.get('status')})"}
    return _run_powershell(cmd, timeout=int(args.get("timeout", 120)))


def tool_open_program(args: dict) -> dict:
    """Launch a program. Allowed from Program Files and user areas; blocked for Windows/System32 only."""
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": f"not found: {path}"}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    approval = request_approval(
        title="Launch program",
        command=f'Start-Process "{path}"',
        details={"kind": "launch", "path": path},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied launch ({approval.get('status')})"}
    try:
        subprocess.Popen([path] + list(args.get("args", []) or []), shell=False)
        return {"ok": True, "path": path}
    except Exception as e:
        return {"error": str(e)}


_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "to", "of", "in", "on",
    "at", "for", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "being", "do", "does", "did", "have", "has", "had", "you", "your", "me", "my",
    "i", "we", "us", "our", "it", "its", "this", "that", "these", "those", "can",
    "could", "would", "should", "will", "just", "please", "hey", "hi", "hello",
    "some", "any", "all", "only", "also", "much", "more", "most", "very", "how",
    "what", "when", "where", "why", "which", "who", "whom", "whose", "make",
    "create", "build", "need", "want", "like", "get", "got", "put", "see", "tell",
    "say", "know", "good", "bad", "new", "old", "here", "there", "them", "now",
}


def _title_from_prompt(text: str, max_len: int = 44) -> str:
    """Produce a concise, readable chat title from the first user message.
    Grabs the most distinctive keywords; falls back to a truncation of the raw
    text if nothing useful remains after filtering."""
    if not text:
        return "new session"
    cleaned = re.sub(r"```[\s\S]*?```", " ", text)        # drop code fences
    cleaned = re.sub(r"https?://\S+", " ", cleaned)        # drop urls
    cleaned = re.sub(r"[^\w\s\-']", " ", cleaned)          # keep letters/digits
    words = [w for w in cleaned.split() if w]
    keywords: list[str] = []
    seen: set[str] = set()
    for w in words:
        lw = w.lower()
        if lw in _STOPWORDS:
            continue
        if len(lw) < 2:
            continue
        if lw in seen:
            continue
        seen.add(lw)
        keywords.append(w)
    if not keywords:
        fallback = " ".join(words[:6]) or text.strip()
        return (fallback[: max_len - 1].rstrip() + "…") if len(fallback) > max_len else fallback
    title = " ".join(keywords[:6])
    if len(title) > max_len:
        title = title[: max_len - 1].rstrip() + "…"
    return title.lower()


def _load_memories() -> list[dict]:
    if not MEMORIES_FILE.exists():
        return []
    out = []
    try:
        with MEMORIES_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


def _save_memories(memories: list[dict]) -> None:
    MEMORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with MEMORIES_FILE.open("w", encoding="utf-8") as f:
        for m in memories:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")


def tool_remember(args: dict) -> dict:
    """Save a terse lesson from this turn so future sessions start smarter."""
    text = (args.get("text") or "").strip()
    if not text:
        return {"error": "text required"}
    text = text[:MEMORIES_TEXT_CAP]
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(t).strip().lower()[:24] for t in tags if str(t).strip()][:5]
    memories = _load_memories()
    # de-dupe: if an identical text already exists, bump its use_count instead of adding
    for m in memories:
        if m.get("text") == text:
            m["use_count"] = int(m.get("use_count", 0)) + 1
            m["updated"] = int(time.time())
            _save_memories(memories)
            return {"saved": False, "reason": "duplicate", "id": m.get("id"), "count": m["use_count"]}
    entry = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "tags": tags,
        "created": int(time.time()),
        "use_count": 1,
    }
    memories.append(entry)
    # cap total at 200 entries — drop oldest unused first
    if len(memories) > 200:
        memories.sort(key=lambda m: (m.get("use_count", 0), m.get("created", 0)))
        memories = memories[-200:]
    _save_memories(memories)
    return {"saved": True, "id": entry["id"], "total": len(memories)}


def tool_forget(args: dict) -> dict:
    mid = (args.get("id") or "").strip()
    if not mid:
        return {"error": "id required"}
    memories = _load_memories()
    before = len(memories)
    memories = [m for m in memories if m.get("id") != mid]
    _save_memories(memories)
    return {"removed": before - len(memories), "total": len(memories)}


def _select_memories_for_prompt() -> list[dict]:
    """Pick the most useful memories for the system prompt — favor recent + used."""
    memories = _load_memories()
    if not memories:
        return []
    memories.sort(
        key=lambda m: (int(m.get("use_count", 0)), int(m.get("updated", 0) or m.get("created", 0))),
        reverse=True,
    )
    return memories[:MEMORIES_MAX_INJECT]


def tool_web_fetch(args: dict) -> dict:
    url = args.get("url") or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Accuretta/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(1024 * 1024)
        text = raw.decode("utf-8", errors="replace")
        clean = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        clean = re.sub(r"<style[\s\S]*?</style>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        return {"url": url, "text": clean[:16000], "truncated": len(clean) > 16000}
    except Exception as e:
        return {"error": str(e)}


# ---- desktop automation ---------------------------------------------------
# Layered defense:
#   1. Feature flag (`desktop_enabled`) must be ON in settings.
#   2. Required libraries must be present (pyautogui / PIL; pygetwindow is nice-to-have).
#   3. Kill switch (`_desktop_panic`) — any pending or new action is refused immediately.
#   4. Rate limit — hard cap on actions per minute regardless of agent intent.
#   5. Allowlist — only apps the user explicitly whitelisted can be launched.
#   6. Approval — every action tool routes through request_approval with full context.
#
# Read-only tools (screenshot/describe/list) run without approval when
# `desktop_auto_approve_read` is on (default), because observation can't
# mutate the machine and constant approval prompts would be unusable.

def _desktop_preflight(require_libs: bool = True) -> dict | None:
    """Return an error dict if desktop tools can't run right now, else None."""
    s = get_settings()
    if not s.get("desktop_enabled"):
        return {"error": "desktop automation is disabled. Enable it in Settings -> Desktop automation."}
    if _desktop_panic.is_set():
        return {"error": "desktop automation is paused (panic/kill switch active). User must resume in Settings."}
    cid = _current_chat_id.get()
    if cid and cid in _chat_desktop_disabled:
        return {"error": "desktop automation is off for this chat. User must re-enable it on the session header."}
    if require_libs and not (_HAVE_PYAUTOGUI and _HAVE_PIL):
        missing = []
        if not _HAVE_PYAUTOGUI:
            missing.append("pyautogui")
        if not _HAVE_PIL:
            missing.append("Pillow")
        return {
            "error": f"missing dependencies: {', '.join(missing)}. Install with: pip install {' '.join(missing)}"
        }
    return None


def _desktop_rate_check() -> dict | None:
    """Sliding-window rate limit. Returns error dict if over limit."""
    s = get_settings()
    cap = int(s.get("desktop_max_actions_per_minute") or 30)
    now = time.time()
    with _desktop_action_lock:
        _desktop_action_times[:] = [t for t in _desktop_action_times if now - t < 60.0]
        if len(_desktop_action_times) >= cap:
            oldest = _desktop_action_times[0]
            wait = 60.0 - (now - oldest)
            return {"error": f"rate limit: {cap} actions/min exceeded. Retry in {wait:.0f}s."}
        _desktop_action_times.append(now)
    return None


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


def _take_screenshot_b64(region: tuple[int, int, int, int] | None = None, max_dim: int = 1600) -> tuple[str, tuple[int, int]]:
    """Capture screen (or region) and return (base64-png, (orig_w, orig_h)).
    Downscales to max_dim for the vision model so we don't flood its context."""
    img = pyautogui.screenshot(region=region) if region else pyautogui.screenshot()
    orig_w, orig_h = img.size
    # shrink for the vision model — reading 4K screenshots burns tokens and
    # LightOnOCR's projector is happy with ~1600px on the long edge
    if max(orig_w, orig_h) > max_dim:
        scale = max_dim / max(orig_w, orig_h)
        img = img.resize((int(orig_w * scale), int(orig_h * scale)), Image.LANCZOS)
    buf = _io.BytesIO()
    img.save(buf, format="PNG")
    return _b64.b64encode(buf.getvalue()).decode(), (orig_w, orig_h)


def _list_windows_raw() -> list[dict]:
    """Return visible top-level windows with title + bbox. Requires pygetwindow."""
    if not _HAVE_PGW:
        return []
    out = []
    try:
        for w in _pgw.getAllWindows():
            title = (w.title or "").strip()
            if not title:
                continue
            try:
                if w.width <= 0 or w.height <= 0:
                    continue
                out.append({
                    "title": title,
                    "x": int(w.left), "y": int(w.top),
                    "w": int(w.width), "h": int(w.height),
                    "active": bool(w.isActive),
                    "minimized": bool(getattr(w, "isMinimized", False)),
                })
            except Exception:
                continue
    except Exception:
        return []
    return out


def _find_window(substring: str):
    """Fuzzy find a single window by title substring (case-insensitive).
    Returns pygetwindow handle or None."""
    if not _HAVE_PGW or not substring:
        return None
    needle = substring.strip().lower()
    try:
        candidates = [w for w in _pgw.getAllWindows() if (w.title or "").lower().find(needle) >= 0]
        candidates = [w for w in candidates if (w.title or "").strip()]
        if not candidates:
            return None
        # prefer active, then largest
        candidates.sort(key=lambda w: (not getattr(w, "isActive", False), -(w.width * w.height)))
        return candidates[0]
    except Exception:
        return None


# ---- desktop tools: read-only (observation) ---------------------------------

def tool_screenshot(args: dict) -> dict:
    """Capture the screen and return a base64 PNG. Read-only."""
    err = _desktop_preflight()
    if err:
        return err
    try:
        region = None
        if all(k in args for k in ("x", "y", "w", "h")):
            region = (int(args["x"]), int(args["y"]), int(args["w"]), int(args["h"]))
        b64, (ow, oh) = _take_screenshot_b64(region)
        return {"ok": True, "image_b64": b64, "width": ow, "height": oh, "note": "PNG, downscaled to ≤1600px long edge if needed"}
    except Exception as e:
        return {"error": f"screenshot failed: {e}"}


def tool_describe_screen(args: dict) -> dict:
    """Take a screenshot, run it through the vision model, return the description.
    This is the agent's 'eyes' — prefer this over raw screenshots so the main
    model stays text-only and VRAM stays efficient."""
    err = _desktop_preflight()
    if err:
        return err
    try:
        region = None
        if all(k in args for k in ("x", "y", "w", "h")):
            region = (int(args["x"]), int(args["y"]), int(args["w"]), int(args["h"]))
        hint = (args.get("hint") or "").strip()
        b64, (ow, oh) = _take_screenshot_b64(region)
        desc = describe_image(b64, hint=hint)
        return {"ok": True, "description": desc, "width": ow, "height": oh}
    except Exception as e:
        return {"error": f"describe_screen failed: {e}"}


def tool_list_windows(args: dict) -> dict:
    """List visible top-level windows."""
    err = _desktop_preflight(require_libs=False)
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    return {"ok": True, "windows": _list_windows_raw()}


# ---- desktop tools: actions (gated) -----------------------------------------

def _gate_action(title: str, command: str, details: dict | None = None) -> dict | None:
    """Common gate for every action tool: panic -> rate -> approval.
    Returns an error dict if the caller should abort, else None."""
    err = _desktop_preflight()
    if err:
        return err
    rerr = _desktop_rate_check()
    if rerr:
        return rerr
    approval = request_approval(title=title, command=command, details=details or {})
    if approval.get("decision") != "approve":
        return {"error": f"user denied action ({approval.get('status')})"}
    if _desktop_panic.is_set():
        return {"error": "panic/kill switch was flipped after approval — action aborted."}
    return None


def tool_desktop_launch_app(args: dict) -> dict:
    """Launch an application. Only apps in the user's allowlist are permitted."""
    target = (args.get("name") or args.get("path") or "").strip()
    if not target:
        return {"error": "name or path required"}
    if not _app_matches_allowlist(target):
        s = get_settings()
        allow = s.get("desktop_app_allowlist") or []
        return {
            "error": (
                f"'{target}' is not in the desktop allowlist. "
                f"User must add it in Settings -> Desktop automation first. "
                f"Current allowlist: {allow or '(empty)'}"
            )
        }
    gate_err = _gate_action(
        title="Launch app (desktop automation)",
        command=target,
        details={"kind": "desktop.launch", "target": target},
    )
    if gate_err:
        return gate_err
    try:
        # shell=True lets 'notepad' / 'chrome' resolve from PATH/App Paths
        subprocess.Popen(target, shell=True)
        return {"ok": True, "launched": target}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_focus_window(args: dict) -> dict:
    """Bring a window to the foreground by title substring."""
    err = _desktop_preflight(require_libs=False)
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title substring required"}
    w = _find_window(title)
    if not w:
        return {"error": f"no visible window matches '{title}'"}
    gate_err = _gate_action(
        title="Focus window",
        command=f'focus: "{w.title}"',
        details={"kind": "desktop.focus", "title": w.title},
    )
    if gate_err:
        return gate_err
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
        return {"ok": True, "focused": w.title}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_click(args: dict) -> dict:
    """Move the mouse to (x, y) and click. Coordinates are in screen pixels.
    The agent should derive coords from describe_screen + list_windows, not guess."""
    try:
        x = int(args["x"])
        y = int(args["y"])
    except Exception:
        return {"error": "x and y (screen pixel coords) required"}
    button = (args.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        return {"error": "button must be left, right, or middle"}
    clicks = int(args.get("clicks") or 1)
    if not (1 <= clicks <= 3):
        return {"error": "clicks must be 1..3"}
    gate_err = _gate_action(
        title=f"Click at ({x}, {y})",
        command=f"{button} click x{clicks} at ({x}, {y})",
        details={"kind": "desktop.click", "x": x, "y": y, "button": button, "clicks": clicks},
    )
    if gate_err:
        return gate_err
    try:
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        return {"ok": True, "clicked": [x, y], "button": button, "clicks": clicks}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_type_text(args: dict) -> dict:
    """Type a string into the focused window. No special keys — use press_keys for those."""
    text = args.get("text") or ""
    if not isinstance(text, str) or not text:
        return {"error": "text (string) required"}
    if len(text) > 2000:
        return {"error": "text too long (>2000 chars). Split into smaller chunks."}
    preview = text if len(text) <= 200 else (text[:200] + "…")
    gate_err = _gate_action(
        title="Type text (keyboard)",
        command=f"type: {preview}",
        details={"kind": "desktop.type", "text": text, "length": len(text)},
    )
    if gate_err:
        return gate_err
    try:
        pyautogui.typewrite(text, interval=0.01)
        return {"ok": True, "typed_chars": len(text)}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_press_keys(args: dict) -> dict:
    """Press a key combo like 'ctrl+s', 'alt+tab', 'enter', 'win'.
    For a single key use `keys: 'enter'`; for combos use `keys: 'ctrl+s'`."""
    keys = args.get("keys") or ""
    if not isinstance(keys, str) or not keys.strip():
        return {"error": "keys (string like 'ctrl+s' or 'enter') required"}
    combo = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not combo:
        return {"error": "no keys parsed"}
    # pyautogui hotkey whitelist — block anything not a known key to avoid surprises
    allowed = set(pyautogui.KEYBOARD_KEYS) if _HAVE_PYAUTOGUI else set()
    bad = [k for k in combo if k not in allowed and len(k) != 1]
    if bad:
        return {"error": f"unknown keys: {bad}. See pyautogui.KEYBOARD_KEYS."}
    gate_err = _gate_action(
        title="Press keys",
        command=f"hotkey: {'+'.join(combo)}",
        details={"kind": "desktop.keys", "combo": combo},
    )
    if gate_err:
        return gate_err
    try:
        if len(combo) == 1:
            pyautogui.press(combo[0])
        else:
            pyautogui.hotkey(*combo)
        return {"ok": True, "pressed": "+".join(combo)}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_close_window(args: dict) -> dict:
    """Close a window by title substring. Requires approval."""
    err = _desktop_preflight(require_libs=False)
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title substring required"}
    w = _find_window(title)
    if not w:
        return {"error": f"no visible window matches '{title}'"}
    gate_err = _gate_action(
        title="Close window",
        command=f'close: "{w.title}"',
        details={"kind": "desktop.close", "title": w.title},
    )
    if gate_err:
        return gate_err
    try:
        w.close()
        return {"ok": True, "closed": w.title}
    except Exception as e:
        return {"error": str(e)}


TOOLS: dict[str, dict] = {
    "list_directory": {
        "description": "List files and folders at a path. Use to explore.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path. ~ and %VARS% expanded."}},
            "required": ["path"],
        },
        "fn": tool_list_directory,
    },
    "read_file": {
        "description": "Read a file from the workspace. Works on text, code, markdown, and binary files (returns best-effort decoded text).",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "fn": tool_read_file,
    },
    "write_file": {
        "description": "Write or overwrite a file. Requires user approval. ONLY for new files or complete rewrites (>30 lines changed).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        "fn": tool_write_file,
    },
    "edit_file": {
        "description": (
            "Surgical search-and-replace edits on an existing file. "
            "PREFERRED for changes affecting ≤30 lines. Each edit finds old_text and replaces with new_text. "
            "old_text must appear exactly once in the file (unique). "
            "NEVER use this to rewrite an entire file — use write_file for that."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string", "description": "exact text to find (must be unique in file)"},
                            "new_text": {"type": "string", "description": "replacement text"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
        "fn": tool_edit_file,
    },
    "delete_file": {
        "description": "Delete a file or folder. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "fn": tool_delete_file,
    },
    "run_powershell": {
        "description": "Run a PowerShell command. Read-only commands run freely; write/modify commands require approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "seconds, default 120"},
            },
            "required": ["command"],
        },
        "fn": tool_run_powershell,
    },
    "open_program": {
        "description": "Launch a program by absolute path. Requires approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["path"],
        },
        "fn": tool_open_program,
    },
    "web_fetch": {
        "description": "Fetch a URL and return stripped text content.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "fn": tool_web_fetch,
    },
    "remember": {
        "description": (
            "Save a terse lesson (<= 220 chars) so future sessions start smarter. "
            "Use ONLY for durable facts: a working command, a file layout, a user "
            "preference. Never store the current task or chat transcript."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the lesson, single sentence ideally"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "2-3 short tags for recall"},
            },
            "required": ["text"],
        },
        "fn": tool_remember,
    },
    "forget": {
        "description": "Remove a memory by id (returned by remember).",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "fn": tool_forget,
    },
    # ---- desktop automation (gated behind settings + approvals) ----
    "screenshot": {
        "description": (
            "Capture the screen (or a rectangular region) and return a base64 PNG. "
            "Read-only. Prefer `describe_screen` over this for reasoning — describe_screen "
            "runs the image through the vision model so the main model only sees text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "region left (optional)"},
                "y": {"type": "integer", "description": "region top (optional)"},
                "w": {"type": "integer", "description": "region width (optional)"},
                "h": {"type": "integer", "description": "region height (optional)"},
            },
        },
        "fn": tool_screenshot,
    },
    "describe_screen": {
        "description": (
            "Take a screenshot and ask the local vision model to describe it. "
            "Returns a text description including visible UI text, button labels, "
            "window titles, and app state. Use this as the agent's 'eyes' every "
            "time you need to observe the screen — the main model never sees pixels."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "hint": {"type": "string", "description": "optional context for the vision model, e.g. 'look for update notifications'"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "w": {"type": "integer"},
                "h": {"type": "integer"},
            },
        },
        "fn": tool_describe_screen,
    },
    "list_windows": {
        "description": "List visible top-level windows with their title and bounding box.",
        "parameters": {"type": "object", "properties": {}},
        "fn": tool_list_windows,
    },
    "desktop_launch_app": {
        "description": (
            "Launch an application. The target must appear in the user's desktop "
            "allowlist (Settings -> Desktop automation) or the call is refused. "
            "Requires approval. `name` can be a PATH-resolvable exe ('notepad', "
            "'chrome') or an absolute path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "exe name or absolute path"},
            },
            "required": ["name"],
        },
        "fn": tool_desktop_launch_app,
    },
    "desktop_focus_window": {
        "description": "Bring a window to the foreground by title substring (case-insensitive). Requires approval.",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string", "description": "substring of target window title"}},
            "required": ["title"],
        },
        "fn": tool_desktop_focus_window,
    },
    "desktop_click": {
        "description": (
            "Click the mouse at screen pixel (x, y). Derive coords from "
            "describe_screen + list_windows; never guess. Requires approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "default left"},
                "clicks": {"type": "integer", "description": "1-3, default 1"},
            },
            "required": ["x", "y"],
        },
        "fn": tool_desktop_click,
    },
    "desktop_type_text": {
        "description": "Type a literal string into the currently focused control. Requires approval. Max 2000 chars.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "fn": tool_desktop_type_text,
    },
    "desktop_press_keys": {
        "description": "Press a key or combo like 'enter', 'ctrl+s', 'alt+tab', 'win'. Requires approval.",
        "parameters": {
            "type": "object",
            "properties": {"keys": {"type": "string"}},
            "required": ["keys"],
        },
        "fn": tool_desktop_press_keys,
    },
    "desktop_close_window": {
        "description": "Close a window by title substring. Requires approval.",
        "parameters": {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        },
        "fn": tool_desktop_close_window,
    },
}


_DESKTOP_TOOL_NAMES = {
    "screenshot", "describe_screen", "list_windows",
    "desktop_launch_app", "desktop_focus_window", "desktop_click",
    "desktop_type_text", "desktop_press_keys", "desktop_close_window",
}


def _active_tools() -> dict:
    """Return TOOLS filtered by current settings — desktop tools only show
    when enabled, so the model doesn't keep calling tools that will refuse."""
    s = get_settings()
    if s.get("desktop_enabled"):
        return TOOLS
    return {k: v for k, v in TOOLS.items() if k not in _DESKTOP_TOOL_NAMES}


def tools_for_ollama() -> list[dict]:
    out = []
    for name, t in _active_tools().items():
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": t["description"],
                "parameters": t["parameters"],
            },
        })
    return out


def tools_for_prompt() -> str:
    lines = ["Available tools (call via <tool_call>{\"name\":\"...\",\"arguments\":{...}}</tool_call>):"]
    for name, t in _active_tools().items():
        params = t["parameters"].get("properties", {})
        sig = ", ".join(f"{k}:{v.get('type','any')}" for k, v in params.items())
        lines.append(f"- {name}({sig}) — {t['description']}")
    return "\n".join(lines)


def invoke_tool(name: str, args: dict) -> dict:
    t = TOOLS.get(name)
    if not t:
        return {"error": f"unknown tool: {name}"}
    try:
        return t["fn"](args or {})
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


# ---- tool-call parsing (fallback for models w/o native tools) -------------

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
TOOL_CALL_FENCE_RE = re.compile(r"```tool_call\s*\n([\s\S]*?)\n```", re.IGNORECASE)


def extract_tool_calls(text: str) -> list[dict]:
    calls = []
    for m in TOOL_CALL_RE.finditer(text):
        try:
            calls.append(json.loads(m.group(1)))
        except Exception:
            pass
    for m in TOOL_CALL_FENCE_RE.finditer(text):
        try:
            calls.append(json.loads(m.group(1)))
        except Exception:
            pass
    return calls


# ---- Ollama helpers --------------------------------------------------------

def ollama_get(path: str) -> dict:
    with urllib.request.urlopen(f"{OLLAMA}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ollama_post(path: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _parse_size_to_b(s: str) -> float:
    """parse '7.6B' / '13b' / '70B' -> billions of params. Returns 0.0 on fail."""
    if not s:
        return 0.0
    m = re.search(r"([\d.]+)\s*([bBmM])", s)
    if not m:
        return 0.0
    val = float(m.group(1))
    return val if m.group(2).lower() == "b" else val / 1000.0


def recommended_settings(model: str) -> dict:
    """Query Ollama /api/show, derive speed-oriented defaults.
    Heuristic — assumes consumer GPU (8-24GB VRAM). User can override.
    """
    info = {}
    try:
        info = ollama_post("/api/show", {"name": model})
    except Exception as e:
        return {"error": f"ollama show failed: {e}"}

    details = info.get("details") or {}
    size_b = _parse_size_to_b(details.get("parameter_size") or "")
    quant = (details.get("quantization_level") or "").upper()

    # approximate bytes per param for common quant levels
    bpp_map = {"Q2": 0.375, "Q3": 0.5, "Q4": 0.55, "Q5": 0.7, "Q6": 0.85, "Q8": 1.05, "F16": 2.0}
    bpp = 0.6
    for k, v in bpp_map.items():
        if k in quant:
            bpp = v
            break
    weight_gb = size_b * bpp if size_b > 0 else 0.0

    # native context from model config (various field paths across ollama versions)
    native_ctx = 0
    mi = info.get("model_info") or {}
    for k, v in mi.items():
        if "context_length" in k and isinstance(v, (int, float)):
            native_ctx = int(v); break
    if not native_ctx:
        native_ctx = 8192

    # recommended num_ctx: cap at native and at 16k for sanity
    rec_ctx = min(native_ctx, 16384)
    # if model is big, drop ctx to preserve VRAM for KV cache
    if weight_gb > 12:
        rec_ctx = min(rec_ctx, 8192)
    if weight_gb > 24:
        rec_ctx = min(rec_ctx, 4096)

    # num_gpu: 0 means auto (ollama picks layer split). Keep auto by default.
    rec_gpu = 0

    # num_batch: bigger is faster but more VRAM. Default 512; 256 for big models.
    rec_batch = 512 if weight_gb <= 12 else 256

    return {
        "model": model,
        "size_b": size_b,
        "quant": quant,
        "est_weights_gb": round(weight_gb, 2),
        "native_ctx": native_ctx,
        "recommended": {
            "num_ctx": rec_ctx,
            "num_gpu": rec_gpu,          # 0 = auto
            "num_batch": rec_batch,
            "num_thread": 0,             # auto
            "num_predict": -1,
            "temperature": 0.7,
            "top_p": 0.9,
            "keep_alive": "30m",
        },
    }


def ollama_post_stream(path: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=None)


def describe_image(b64: str, hint: str = "") -> str:
    """one-shot: hand a base64 image to the vision model, get a text description.
    the chat model then only sees text — no VRAM overhead for vision tokens in the big model.
    ollama loads the vision model in a separate slot so the main model stays resident."""
    settings = get_settings()
    vmodel = (settings.get("vision_model") or "").strip()
    if not vmodel:
        return "[image attached — no vision model configured]"
    # strip any data URL prefix the browser included
    if b64.startswith("data:"):
        comma = b64.find(",")
        if comma >= 0:
            b64 = b64[comma + 1 :]
    prompt = (
        "Describe this image precisely and completely. "
        "Transcribe ALL visible text verbatim. "
        "Describe UI elements (buttons, menus, labels, errors) and their state. "
        "Note window titles, app names, and any numbers/versions shown. "
        "Be factual — do not invent."
    )
    if hint:
        prompt += f"\n\nContext from user: {hint}"
    payload = {
        "model": vmodel,
        "prompt": prompt,
        "images": [b64],
        "stream": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.1, "num_predict": 768},
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{OLLAMA}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return (out.get("response") or "").strip() or "[image attached — empty description]"
    except Exception as e:
        return f"[image attached — vision model failed: {e}]"


# ---- system prompt ---------------------------------------------------------

SYSTEM_PROMPT_BASE = """you are accuretta, a local agent running on the user's own machine.

voice: precise, lowercase, unceremonious. no sales voice, no hype, no emoji.

you have two modes, chosen by context:

1. IDE mode. when the user asks you to build, design, or edit a web page / app,
   reply with a single complete HTML document wrapped in a ```html ... ```
   code block. include inline CSS and JS. the renderer will preview it live
   and save a version the user can flip back to. when editing, rewrite the
   whole document (do not emit partial diffs).

2. Agent mode. when the user asks you to do something on their computer,
   call tools. windows and system32 are off-limits; all writes and all
   modify commands require the user to approve before running.

tools are called by emitting:
  <tool_call>{"name":"<tool>","arguments":{...}}</tool_call>
one per line. after each tool result, continue reasoning. do not emit
tool calls inside code blocks.

feedback discipline — this is a chat UI, silence looks like a hang:
- status narration ("looking in your screenshots folder…", "found 3
  files, reading them now…") MUST go INSIDE <think>...</think> tags.
  the UI renders thinking as a shimmering status line above the tool
  cards; text outside think tags becomes the final answer bubble and
  should NOT contain status pings.
- emit one short <think> status ping before the first tool call and
  between rounds. do not repeat the same ping word-for-word across
  rounds — advance the state ("reading index.html…", "checking
  script.py…", "drafting fixes…").
- if a path isn't found, close thinking and tell the user in the
  bubble what you tried and ask for the correct path.
- CHAIN TOOLS AGGRESSIVELY: when the user asks you to read a file,
  read it immediately after finding it. do not stop after list_directory.
  when asked to write, write immediately after confirming the path.
  complete tasks in the fewest tool calls possible. never ask the user
  "shall i read it?" or "would you like me to proceed?" — just do it.
- the final bubble must contain only the answer/summary (1–3
  sentences when wrapping up a task), never status narration.
- MANDATORY: every turn MUST end with visible text OUTSIDE of <think>
  tags. ending a turn with only tool calls (or only thinking) looks
  like a freeze — the user can't see it. after each tool round:
    * if you have everything you need, answer the user's question
      ("yes — the Test folder has index.html and script.py").
    * if you need another tool, briefly say so before calling it
      ("reading index.html now…") and emit the next call.
    * if the user's request is complete, confirm it ("done. wrote
      index.html. want me to check script.py too?").
  never stop silently after a tool result. one short sentence beats
  zero every time.
- never fix or delete partial code without saying so first. if a file
  is broken, read it, describe the problems, then propose the fix.
  when the user confirms ("yes", "fix them", "do it"), CALL write_file
  — do not just re-describe the fix.

workspace resolution — the user almost always refers to their workspace:
- "workspace folders" are listed at the end of this prompt. treat them
  as authoritative roots. if the user says "the Test folder", "my Test
  project", "the folder I added", etc. match it to the workspace entry
  whose last path segment matches (case-insensitive) — do NOT ask which
  folder unless there's a real ambiguity (two entries with the same
  leaf name).
- once matched, start with a list_dir on that root before asking the
  user for the path.
- only ask the user to clarify if there are zero matches or multiple
  plausible matches.

write honesty — never lie about persistence:
- do NOT say "saved", "applied", "written", "fixed the file", or
  "updated" unless you just called write_file (or the appropriate
  mutating tool) for that file in THIS turn AND received a success
  result. claims without a successful tool result are prohibited.
- if you only showed the corrected code in chat, say exactly that:
  "here's the proposed fix — confirm and I'll write it to <path>." do
  not imply the change is on disk.
- if write_file returned an error, surface it verbatim and stop.

file editing discipline — use the right tool for the job:
- edit_file is for surgical changes: changing a color, renaming a variable, adding one function, fixing a typo. old_text must be UNIQUE in the file. include 2-3 lines of surrounding context in old_text so the match is unambiguous.
- write_file is ONLY for: creating a new file, or rewriting >30 lines at once.
- examples:
  GOOD edit_file: old_text="  background: blue;\\n  color: white;" new_text="  background: red;\\n  color: black;"
  BAD edit_file: old_text="(entire 400-line file)" new_text="(entire 400-line file with one word changed)"
  BAD: rewriting a file in chat text then also calling write_file — confirm "saved" and stop.

memories — you have a persistent `remember(text, tags?)` tool:
- at the end of a task, if you learned something durable (a working
  command, the user's preferred style, where a project lives, a gotcha
  that tripped you up), call remember with ONE short sentence (<=220
  chars). tag with 1-3 short keywords.
- what NOT to remember: the current task, chat transcript, one-off
  paths you were just handed. those expire with the session.
- existing memories are listed at the top of this prompt under
  "learned memories" — read them first and lean on them instead of
  re-deriving things. if a memory turns out wrong, call forget(id)
  and remember the corrected version.

desktop automation — the agent can see and drive the screen when enabled:
- enabled state is in settings (`desktop_enabled`). if a desktop tool returns
  "desktop automation is disabled", tell the user to enable it in
  Settings -> Desktop automation and stop. do not keep retrying.
- observe before you act. every desktop workflow is:
  1. call `describe_screen` (or `list_windows`) to see current state,
  2. decide the next single action,
  3. call ONE action tool (launch/focus/click/type/keys/close),
  4. call `describe_screen` again to confirm it worked,
  5. repeat.
- derive click coordinates from the vision description + list_windows
  bounding boxes. never guess coordinates. if you can't locate a target,
  say so and ask the user.
- prefer keyboard shortcuts over clicks when available (ctrl+s, alt+f4,
  ctrl+tab, win+r, etc.) — they are more reliable than coordinate clicks.
- `desktop_launch_app` refuses anything not in the user's allowlist.
  if an app isn't allowed, do NOT try workarounds (powershell, focus by
  title after shelling out, etc.). tell the user: "'<app>' isn't in your
  desktop allowlist - add it in Settings -> Desktop automation."
- every action prompts the user to approve. if the user denies, stop
  and report why you wanted it — don't retry with a slightly different
  command.
- every action respects the global panic/kill switch. if any tool
  returns a panic error, stop the whole task and tell the user.

keep responses tight. when reporting results, use the voice above.
"""


IDE_TAILWIND_ADDENDUM = """IDE addendum — Tailwind is ENABLED:
- the renderer will inject the Tailwind Play CDN (`https://cdn.tailwindcss.com`)
  into the preview automatically. do NOT add a <script> for it yourself.
- style the page with Tailwind utility classes (flex, grid, rounded-2xl,
  shadow-sm, text-slate-700, etc.). prefer Tailwind over hand-written CSS.
- you may include a tiny `tailwind.config = { ... }` inline script before the
  closing </head> when you need theme extensions or the `darkMode: 'class'`
  hook — that is the Play-CDN config pattern.
- avoid dumping raw <style> rules for things Tailwind already covers.
  keep the result looking polished and modern by default.
"""

IDE_MULTIFILE_ADDENDUM = """IDE addendum — multi-file output is ENABLED:
- when the task warrants separating concerns (more than a trivial page),
  emit a small folder structure instead of a single HTML file. use fenced
  code blocks with a `path=` info string, one per file:

    ```html path=index.html
    <!doctype html><html>... link style.css / script.js here ...</html>
    ```

    ```css path=style.css
    /* stylesheet */
    ```

    ```js path=script.js
    // client script
    ```

- ALWAYS include `path=index.html` as the entry point. other common paths:
  `style.css`, `script.js`, `assets/...`. keep paths relative and
  POSIX-style (forward slashes). no absolute paths, no `..`.
- the renderer will inline linked css/js into the preview iframe so
  `<link rel="stylesheet" href="style.css">` and `<script src="script.js">`
  both work in the live preview. Export Project will zip the files
  separately, preserving the stated paths.
- for trivial single-page work, a single ```html ...``` block is still fine.
"""


def build_system_prompt(include_tools: bool, chat_mode: str = "auto") -> str:
    """Build a token-efficient system prompt. Target: < 1500 tokens total."""
    settings = get_settings()
    parts = []

    # === CORE PROMPT (compact, always present) ===
    core = f"""you are accuretta, a local agent on the user's machine.

voice: precise, lowercase, no hype.

modes:
- IDE: reply with complete HTML in ```html ... ``` block. include inline CSS/JS.
- AGENT: call tools. windows/system32 blocked. all writes need approval.
- AUTO: bridge picks based on request.

tool format: <tool_call>{{"name":"...","arguments":{{...}}}}</tool_call>

rules:
1. status/thinking goes in <think>...</think> tags (UI shows as status line)
2. final answer goes OUTSIDE think tags — never end a turn with only tools or thinking
3. workspace folders are listed below — use them, don't ask
4. only say "saved"/"wrote" if write_file returned success THIS turn
5. call remember(text,tags?) for durable facts (≤220 chars)
6. desktop: enabled={settings.get("desktop_enabled", False)}. if disabled, tell user to enable in Settings. observe before act (describe_screen → decide → act → verify). only allowlisted apps. every action needs approval.
7. CHAIN TOOLS AGGRESSIVELY: when the user asks you to read a file, read it immediately after finding it. do not stop after list_directory. when asked to write, write immediately after confirming the path. complete tasks in the fewest tool calls possible. never ask the user "shall i read it?" or "would you like me to proceed?" — just do it.
8. NEVER re-emit full file content you already generated in a previous turn. if the user asks you to save something you already built, call write_file with the content but do NOT dump the full code in the visible chat text — just confirm "saved to <path>".

keep responses tight."""
    parts.append(core)

    # === MODE-SPECIFIC ADDENDUM (only when relevant) ===
    if chat_mode == "ide" or (chat_mode == "auto" and include_tools is False):
        ide_add = []
        if settings.get("use_tailwind_cdn"):
            ide_add.append("Tailwind CDN is injected automatically. Use utility classes (flex, grid, rounded-2xl, etc.).")
        if settings.get("ide_multifile"):
            ide_add.append("Multi-file: emit ```html path=index.html```, ```css path=style.css```, etc.")
        if ide_add:
            parts.append("IDE mode:\n" + "\n".join(ide_add))

    # === TOOLS (compressed format) ===
    if include_tools and chat_mode != "ide":
        tool_lines = ["tools:"]
        for name, t in _active_tools().items():
            params = t["parameters"].get("properties", {})
            sig = ",".join(f"{k}:{v.get('type','any')[:3]}" for k, v in params.items())
            tool_lines.append(f"- {name}({sig})")
        parts.append("\n".join(tool_lines))

    # === MEMORIES (most useful only, not all) ===
    mems = _select_memories_for_prompt()
    if mems:
        mem_lines = ["mem:"]
        for m in mems[:3]:
            tag = f"[{m.get('tags',[None])[0]}]" if m.get('tags') else ""
            mem_lines.append(f"- {m.get('text','')[:100]}{tag}")
        parts.append("\n".join(mem_lines))

    # === SYSTEM CONTEXT (summarized) ===
    try:
        if SYSTEM_CONTEXT_FILE.exists():
            facts = _scan_system_context()
            ctx_lines = ["context:"]
            ctx_lines.append(f"os={facts.get('os','')}")
            ctx_lines.append(f"user={facts.get('user','')}")
            folders = facts.get("folders", [])[:3]
            for f in folders:
                ctx_lines.append(f"{f['label']}={f['path']}")
            parts.append("\n".join(ctx_lines))
    except Exception:
        pass

    # === WORKSPACE (compact) ===
    ws = get_workspace().get("folders", [])
    if ws:
        parts.append("workspace:\n" + "\n".join(f"- {f}" for f in ws))
    else:
        parts.append("workspace: none (file tools will refuse)")

    return "\n".join(parts)



def ollama_options(settings: dict) -> dict:
    opt = {
        "num_ctx": int(settings.get("num_ctx") or 8192),
        "num_batch": int(settings.get("num_batch") or 512),
        "temperature": float(settings.get("temperature") or 0.7),
        "top_p": float(settings.get("top_p") or 0.9),
    }
    # num_gpu: 0 means "let ollama auto-decide layer split" (best for mixed VRAM)
    ngpu = int(settings.get("num_gpu") or 0)
    if ngpu > 0:
        opt["num_gpu"] = ngpu
    if int(settings.get("num_thread") or 0) > 0:
        opt["num_thread"] = int(settings["num_thread"])
    if int(settings.get("num_predict") or -1) != -1:
        opt["num_predict"] = int(settings["num_predict"])
    kv = (settings.get("kv_cache_type") or "q8_0").strip()
    if kv in ("q4_0", "q8_0", "f16"):
        opt["kv_cache_type"] = kv
    return opt


# ---- hardware auto-detection for optimal settings -------------------------

def _detect_gpu_info() -> list[dict]:
    """Detect NVIDIA/AMD GPUs."""
    gpus = []
    try:
        import pynvml
        pynvml.nvmlInit()
        for i in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total_mb = mem.total // (1024 * 1024)
            gpus.append({
                "vendor": "nvidia",
                "name": name,
                "vram_mb": total_mb,
                "vram_gb": round(total_mb / 1024, 1),
                "cuda_cores": _estimate_cuda_cores(name),
            })
        pynvml.nvmlShutdown()
        return gpus
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                parts = line.split(",")
                if len(parts) >= 2:
                    name = parts[0].strip()
                    mem_str = parts[1].strip().replace(" MiB", "").replace(" MB", "")
                    try:
                        total_mb = int(mem_str)
                        gpus.append({
                            "vendor": "nvidia",
                            "name": name,
                            "vram_mb": total_mb,
                            "vram_gb": round(total_mb / 1024, 1),
                            "cuda_cores": _estimate_cuda_cores(name),
                        })
                    except ValueError:
                        continue
            if gpus:
                return gpus
    except Exception:
        pass
    return gpus


def _estimate_cuda_cores(gpu_name: str) -> int:
    name_lower = gpu_name.lower()
    if "4090" in name_lower: return 16384
    if "4080 super" in name_lower: return 10240
    if "4080" in name_lower: return 9728
    if "4070 ti super" in name_lower: return 8448
    if "4070 ti" in name_lower: return 7680
    if "4070 super" in name_lower: return 7168
    if "4070" in name_lower: return 5888
    if "4060 ti" in name_lower: return 4352
    if "4060" in name_lower: return 3072
    if "3090" in name_lower: return 10496
    if "3080 ti" in name_lower: return 10240
    if "3080" in name_lower: return 8704
    if "3070 ti" in name_lower: return 6144
    if "3070" in name_lower: return 5888
    if "3060 ti" in name_lower: return 4864
    if "3060" in name_lower: return 3584
    return 0


def _detect_cpu_info() -> dict:
    info = {"cores": 0, "threads": 0, "arch": "unknown", "is_x3d": False}
    try:
        import os
        info["cores"] = os.cpu_count() or 0
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["wmic", "cpu", "get", "NumberOfCores,NumberOfLogicalProcessors,Name"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    lines = result.stdout.strip().split("\n")
                    for line in lines[1:]:
                        parts = line.strip().split()
                        if len(parts) >= 3:
                            info["cores"] = int(parts[0])
                            info["threads"] = int(parts[1])
                            name = " ".join(parts[2:])
                            info["is_x3d"] = "x3d" in name.lower() or "3d" in name.lower()
                            info["arch"] = "amd" if "amd" in name.lower() else "intel"
                            break
            except Exception:
                pass
    except Exception:
        pass
    if info["threads"] == 0:
        info["threads"] = info["cores"] or 4
    return info


def _detect_system_ram() -> int:
    try:
        import psutil
        return psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            mem = MEMORYSTATUSEX()
            mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
            return mem.ullTotalPhys // (1024 * 1024)
        except Exception:
            pass
    return 32768


def _calculate_optimal_settings(gpus: list, cpu: dict, ram_mb: int) -> dict:
    settings = {}
    primary_gpu = gpus[0] if gpus else None
    total_vram_mb = sum(g["vram_mb"] for g in gpus) if gpus else 0

    if primary_gpu:
        vram_gb = primary_gpu["vram_gb"]
        if vram_gb >= 24:
            settings["num_gpu"] = 99
            settings["num_ctx"] = 16384
            settings["num_batch"] = 1024
        elif vram_gb >= 16:
            settings["num_gpu"] = 99
            settings["num_ctx"] = 12288
            settings["num_batch"] = 768
        elif vram_gb >= 12:
            settings["num_gpu"] = 99
            settings["num_ctx"] = 8192
            settings["num_batch"] = 512
        elif vram_gb >= 8:
            settings["num_gpu"] = 99
            settings["num_ctx"] = 6144
            settings["num_batch"] = 384
        elif vram_gb >= 6:
            settings["num_gpu"] = 50
            settings["num_ctx"] = 4096
            settings["num_batch"] = 256
        else:
            settings["num_gpu"] = 20
            settings["num_ctx"] = 2048
            settings["num_batch"] = 128
    else:
        settings["num_gpu"] = 0
        settings["num_ctx"] = 4096
        settings["num_batch"] = 256

    if cpu["threads"] >= 32:
        settings["num_thread"] = min(cpu["cores"], 16)
    elif cpu["threads"] >= 16:
        settings["num_thread"] = min(cpu["cores"], 12)
    elif cpu["threads"] >= 8:
        settings["num_thread"] = min(cpu["cores"], 8)
    else:
        settings["num_thread"] = max(2, cpu["cores"] - 1)

    if cpu.get("is_x3d"):
        settings["num_thread"] = max(6, cpu["cores"])
        settings["num_batch"] = min(settings["num_batch"] * 2, 2048)

    ram_gb = ram_mb / 1024
    if ram_gb < 16 and settings["num_ctx"] > 4096:
        settings["num_ctx"] = 4096
    elif ram_gb < 32 and settings["num_ctx"] > 8192:
        settings["num_ctx"] = 8192

    if primary_gpu and primary_gpu.get("vram_gb", 0) >= 16:
        settings["num_batch"] = max(settings["num_batch"], 512)

    if total_vram_mb > 16000:
        settings["keep_alive"] = "1h"
    else:
        settings["keep_alive"] = "30m"

    settings["num_predict"] = -1
    settings.setdefault("temperature", 0.7)
    settings.setdefault("top_p", 0.9)

    # VRAM-poor GPUs benefit from q4_0 KV cache (halves cache size vs q8_0)
    if primary_gpu and primary_gpu.get("vram_gb", 0) <= 16:
        settings["kv_cache_type"] = "q4_0"
    else:
        settings["kv_cache_type"] = "q8_0"

    return settings


def get_optimal_settings() -> dict:
    global _HW_CACHE
    gpus = _detect_gpu_info()
    cpu = _detect_cpu_info()
    ram_mb = _detect_system_ram()
    _HW_CACHE = (time.time(), gpus, cpu, ram_mb)
    optimal = _calculate_optimal_settings(gpus, cpu, ram_mb)
    optimal["_hardware"] = {
        "gpus": gpus,
        "cpu": cpu,
        "ram_gb": round(ram_mb / 1024, 1),
        "detected_at": int(time.time()),
    }
    return optimal


# Cache hardware scans for 5 minutes so Apply Optimal doesn't re-scan every click.
_HW_CACHE: tuple[float, list, dict, int] | None = None
_HW_CACHE_TTL = 300


def get_combined_optimal_settings(model: str | None = None) -> dict:
    """Merge hardware detection with model-specific constraints.

    Hardware sets the ceiling (VRAM, CPU threads).
    Model sets safety limits (big models need smaller ctx/batch to fit).
    The most conservative value wins for each parameter.
    """
    global _HW_CACHE
    # ---- hardware baseline (cached) ----
    if _HW_CACHE is not None:
        ts, gpus, cpu, ram_mb = _HW_CACHE
        if time.time() - ts < _HW_CACHE_TTL:
            pass  # use cached
        else:
            _HW_CACHE = None
    if _HW_CACHE is None:
        gpus = _detect_gpu_info()
        cpu = _detect_cpu_info()
        ram_mb = _detect_system_ram()
        _HW_CACHE = (time.time(), gpus, cpu, ram_mb)
    else:
        _, gpus, cpu, ram_mb = _HW_CACHE
    hw = _calculate_optimal_settings(gpus, cpu, ram_mb)
    hw_meta = {
        "gpus": gpus,
        "cpu": cpu,
        "ram_gb": round(ram_mb / 1024, 1),
        "detected_at": int(time.time()),
    }

    if not model:
        hw["_hardware"] = hw_meta
        return hw

    # ---- model-specific constraints ----
    model_rec = recommended_settings(model)
    if "error" in model_rec or not model_rec.get("recommended"):
        hw["_hardware"] = hw_meta
        hw["_model"] = {"name": model, "error": model_rec.get("error", "unknown")}
        return hw

    mrec = model_rec["recommended"]
    combined = dict(hw)

    # Context window: most constrained wins
    hw_ctx = hw.get("num_ctx", 8192)
    model_ctx = mrec.get("num_ctx", hw_ctx)
    native_ctx = model_rec.get("native_ctx", 0)
    combined["num_ctx"] = min(hw_ctx, model_ctx)
    if native_ctx > 0:
        combined["num_ctx"] = min(combined["num_ctx"], native_ctx)

    # Batch size: model may suggest smaller for huge models
    hw_batch = hw.get("num_batch", 512)
    model_batch = mrec.get("num_batch", hw_batch)
    combined["num_batch"] = min(hw_batch, model_batch)

    # GPU layers & threading: hardware is more specific than model defaults
    combined["num_gpu"] = hw.get("num_gpu", 0)
    combined["num_thread"] = hw.get("num_thread", 0)

    # KV cache: hardware decides (q4_0 for <=16GB, q8_0 for bigger)
    combined["kv_cache_type"] = hw.get("kv_cache_type", "q8_0")

    # Other params: model rec is fine for these (same values usually)
    for key in ["temperature", "top_p", "keep_alive", "num_predict"]:
        if key in mrec:
            combined[key] = mrec[key]

    combined["_hardware"] = hw_meta
    combined["_model"] = {
        "name": model,
        "size_b": model_rec.get("size_b"),
        "quant": model_rec.get("quant"),
        "est_weights_gb": model_rec.get("est_weights_gb"),
        "native_ctx": native_ctx,
    }
    return combined


def run_chat_turn(chat_id: str, messages: list[dict], use_tools: bool, emit):
    """
    messages: list of {role, content, [tool_calls], [tool_call_id]}
    emit(event_dict): pushes a chunk to the caller (SSE producer).
    Returns the final assistant message dict (with content fully assembled).
    """
    settings = get_settings()
    model = settings.get("model") or ""
    if not model:
        emit({"type": "error", "error": "no model selected. Pick one in Settings."})
        return None

    _chat_emitters[chat_id] = emit
    try:
        max_tool_rounds = 6
        rounds = 0
        empty_retry_done = False
        conversation = list(messages)

        while True:
            # keep context window under control — drop oldest non-system messages
            ctx_limit = int(settings.get("num_ctx") or 8192)
            trimmed = truncate_messages(conversation, ctx_limit, reserve=512)
            if len(trimmed) < len(conversation):
                dropped = len(conversation) - len(trimmed)
                emit({"type": "notice", "note": f"dropped {dropped} old messages to fit context window"})

            payload = {
                "model": model,
                "messages": trimmed,
                "stream": True,
                "keep_alive": settings.get("keep_alive", "30m"),
                "options": ollama_options(settings),
            }
            if use_tools:
                payload["tools"] = tools_for_ollama()

            try:
                resp = ollama_post_stream("/api/chat", payload)
            except Exception as e:
                emit({"type": "error", "error": f"ollama unreachable: {e}"})
                return None

            content_buf = []
            tool_calls_native: list[dict] = []
            done = False
            last_stats = {}
            for raw in resp:
                if not raw:
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8"))
                except Exception:
                    continue
                msg = obj.get("message") or {}
                piece = msg.get("content") or ""
                if piece:
                    content_buf.append(piece)
                    emit({"type": "delta", "content": piece})
                for tc in (msg.get("tool_calls") or []):
                    tool_calls_native.append(tc)
                if obj.get("done"):
                    done = True
                    last_stats = {
                        "eval_count": obj.get("eval_count"),
                        "eval_duration": obj.get("eval_duration"),
                        "prompt_eval_count": obj.get("prompt_eval_count"),
                    }
                    if last_stats.get("eval_count") is not None:
                        emit({"type": "stats", **last_stats})
                    break
            resp.close()

            full_text = "".join(content_buf)
            parsed_calls = []
            for tc in tool_calls_native:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                parsed_calls.append({"name": fn.get("name"), "arguments": args or {}})
            if not parsed_calls and use_tools:
                for c in extract_tool_calls(full_text):
                    parsed_calls.append({
                        "name": c.get("name") or c.get("tool"),
                        "arguments": c.get("arguments") or c.get("args") or {},
                    })

            assistant_msg = {"role": "assistant", "content": full_text}
            if parsed_calls:
                assistant_msg["tool_calls"] = [
                    {"function": {"name": c["name"], "arguments": c["arguments"]}} for c in parsed_calls
                ]

            if not parsed_calls or rounds >= max_tool_rounds:
                # If the model returned empty content after tool results, nudge once.
                if not (assistant_msg.get("content") or "").strip() and rounds > 0 and not empty_retry_done:
                    empty_retry_done = True
                    conversation.append({
                        "role": "system",
                        "content": "Continue. Complete the user's request using the tool results you just received. Do not ask for permission.",
                    })
                    continue
                # attach stats to the final message so the client can show per-msg token count
                assistant_msg["_stats"] = last_stats
                emit({"type": "final", "message": assistant_msg})
                return assistant_msg

            conversation.append(assistant_msg)
            for call in parsed_calls:
                name = call.get("name") or ""
                args = call.get("arguments") or {}
                emit({"type": "tool_start", "name": name, "arguments": args})
                # run tool in thread pool so we can heartbeat while it blocks.
                # copy context vars so _current_chat_id and _chat_emitters work inside the worker.
                _ctx = contextvars.copy_context()
                future = _tool_executor.submit(_ctx.run, invoke_tool, name, args if isinstance(args, dict) else {})
                while not future.done():
                    try:
                        future.result(timeout=1.0)
                    except Exception:
                        pass
                    if not future.done():
                        try:
                            emit({"type": "heartbeat", "note": f"waiting for {name}…"})
                        except Exception:
                            pass
                result = future.result()
                emit({"type": "tool_result", "name": name, "result": result})
                conversation.append({
                    "role": "tool",
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:4000],
                })
            rounds += 1
    finally:
        _chat_emitters.pop(chat_id, None)


# ---- versioning ------------------------------------------------------------

HTML_BLOCK_RE = re.compile(r"```(?:html|HTML)\s*\n([\s\S]*?)```", re.MULTILINE)


def extract_html(text: str) -> str | None:
    if not text:
        return None
    m = HTML_BLOCK_RE.search(text)
    if m:
        html = m.group(1).strip()
        if "<" in html and ">" in html:
            return html
    stripped = text.strip()
    if stripped.lower().startswith("<!doctype") or stripped.lower().startswith("<html"):
        return stripped
    return None


def save_version(chat_id: str, html: str, label: str = "") -> dict:
    folder = VERSIONS_DIR / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    idx = len([f for f in folder.iterdir() if f.suffix == ".html"]) + 1
    name = f"v{idx:04d}.html"
    (folder / name).write_text(html, encoding="utf-8")
    meta_path = folder / "index.json"
    meta = load_json(meta_path, {"versions": []})
    entry = {"id": name, "n": idx, "t": int(time.time()), "label": label, "bytes": len(html.encode("utf-8"))}
    meta["versions"].append(entry)
    save_json(meta_path, meta)
    return entry


def list_versions(chat_id: str) -> list[dict]:
    folder = VERSIONS_DIR / chat_id
    meta = load_json(folder / "index.json", {"versions": []})
    return meta.get("versions", [])


def read_version(chat_id: str, vid: str) -> str | None:
    p = VERSIONS_DIR / chat_id / vid
    if not p.exists() or p.suffix != ".html":
        return None
    return p.read_text(encoding="utf-8")


# ---- HTTP handler ----------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".jsx": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".ico": "image/x-icon",
}

STATIC_WHITELIST = {"index.html", "app.js", "app.css", "colors_and_type.css", "logo-mark.png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "Accuretta/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- helpers

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,PUT,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, status: int, obj: Any):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._set_cors()
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _send_bytes(self, status: int, data: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self._set_cors()
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _read_json(self) -> dict:
        ln = int(self.headers.get("Content-Length") or 0)
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    # ---- dispatch

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        try:
            if p == "/" or p == "":
                return self._serve_static("index.html")
            if p.startswith("/api/"):
                return self._handle_api_get(p, parsed)
            name = p.lstrip("/")
            if name in STATIC_WHITELIST:
                return self._serve_static(name)
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        try:
            if p.startswith("/api/"):
                return self._handle_api_post(p, parsed)
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        try:
            if p.startswith("/api/chats/"):
                cid = p.split("/")[-1]
                chats = get_chats()
                if cid in chats["chats"]:
                    chats["chats"].pop(cid)
                    chats["order"] = [x for x in chats["order"] if x != cid]
                    save_json(CHATS_FILE, chats)
                    shutil.rmtree(VERSIONS_DIR / cid, ignore_errors=True)
                    return self._send_json(200, {"ok": True})
                return self._send_json(404, {"error": "not found"})
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _serve_static(self, name: str):
        full = ROOT / name
        if not full.is_file():
            return self._send_json(404, {"error": f"missing: {name}"})
        data = full.read_bytes()
        ctype = MIME.get(full.suffix, "application/octet-stream")
        self._send_bytes(200, data, ctype)

    # ---- API routes

    def _handle_api_get(self, p: str, parsed):
        if p == "/api/health":
            return self._send_json(200, {"ok": True, "ollama": OLLAMA})
        if p == "/api/hardware":
            qs = urllib.parse.parse_qs(parsed.query)
            model = (qs.get("model") or [""])[0].strip()
            optimal = get_combined_optimal_settings(model or None)
            return self._send_json(200, {
                "hardware": optimal.pop("_hardware", {}),
                "model": optimal.pop("_model", None),
                "recommended": optimal,
                "current": get_settings(),
            })
        if p == "/api/models":
            try:
                data = ollama_get("/api/tags")
                return self._send_json(200, data)
            except Exception as e:
                return self._send_json(200, {"models": [], "error": str(e)})
        if p.startswith("/api/model-info/"):
            name = urllib.parse.unquote(p[len("/api/model-info/"):])
            return self._send_json(200, recommended_settings(name))
        if p == "/api/settings":
            return self._send_json(200, get_settings())
        if p == "/api/workspace":
            return self._send_json(200, get_workspace())
        if p == "/api/chats":
            return self._send_json(200, get_chats())
        if p == "/api/approvals":
            return self._send_json(200, {"pending": list_approvals()})
        if p.startswith("/api/versions/"):
            parts = p.split("/")
            # /api/versions/<chat_id>          -> list
            # /api/versions/<chat_id>/<vid>    -> html
            if len(parts) == 4:
                return self._send_json(200, {"versions": list_versions(parts[3])})
            if len(parts) == 5:
                html = read_version(parts[3], parts[4])
                if html is None:
                    return self._send_json(404, {"error": "not found"})
                return self._send_bytes(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if p == "/api/events":
            return self._serve_sse()
        if p == "/api/system-context":
            try:
                md = ensure_system_context()
            except Exception as e:
                return self._send_json(200, {"md": "", "path": str(SYSTEM_CONTEXT_FILE), "exists": False, "error": str(e)})
            return self._send_json(200, {
                "md": md,
                "path": str(SYSTEM_CONTEXT_FILE),
                "exists": SYSTEM_CONTEXT_FILE.exists(),
            })
        if p == "/api/memories":
            return self._send_json(200, {"memories": _load_memories(), "path": str(MEMORIES_FILE)})
        if p.startswith("/api/desktop/chat-state/"):
            # GET the per-chat desktop-disabled flag
            cid = p.split("/", 4)[4]
            return self._send_json(200, {
                "chat_id": cid,
                "disabled": cid in _chat_desktop_disabled,
            })
        if p.startswith("/api/chats/") and p.count("/") == 3:
            # GET a single chat's metadata (for restoring last_mode on switch)
            cid = p.split("/")[3]
            chats = get_chats()
            if cid in chats["chats"]:
                return self._send_json(200, chats["chats"][cid])
            return self._send_json(404, {"error": "not found"})
        if p == "/api/snapshots":
            out = []
            for f in sorted(SNAPSHOTS_DIR.glob("*.html")):
                try:
                    st = f.stat()
                    out.append({"name": f.name, "size": st.st_size, "mtime": int(st.st_mtime)})
                except Exception:
                    continue
            return self._send_json(200, {"snapshots": out, "path": str(SNAPSHOTS_DIR)})
        if p.startswith("/api/snapshots/"):
            # serve a single saved snapshot by filename (no path traversal)
            fname = p.split("/", 3)[3]
            if "/" in fname or "\\" in fname or ".." in fname:
                return self._send_json(400, {"error": "bad name"})
            fp = SNAPSHOTS_DIR / fname
            if not fp.exists() or not fp.is_file():
                return self._send_json(404, {"error": "not found"})
            return self._send_bytes(200, fp.read_bytes(), "text/html; charset=utf-8")
        if p.startswith("/api/jobs/"):
            job_id = p.split("/")[3]
            with _tool_jobs_lock:
                job = _tool_jobs.get(job_id)
            if not job:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {
                "id": job_id,
                "status": job.get("status"),
                "result": job.get("result"),
                "started": job.get("started"),
                "finished": job.get("finished"),
            })
        if p == "/api/desktop/status":
            s = get_settings()
            return self._send_json(200, {
                "enabled": bool(s.get("desktop_enabled")),
                "panic": _desktop_panic.is_set(),
                "have_pyautogui": _HAVE_PYAUTOGUI,
                "have_pil": _HAVE_PIL,
                "have_pygetwindow": _HAVE_PGW,
                "allowlist": s.get("desktop_app_allowlist") or [],
                "max_actions_per_minute": int(s.get("desktop_max_actions_per_minute") or 30),
            })
        if p == "/api/list-folder":
            qs = urllib.parse.parse_qs(parsed.query)
            raw = (qs.get("path") or [""])[0]
            if not raw:
                return self._send_json(400, {"error": "path required"})
            target = Path(normalize_path(raw))
            # only allow listing inside configured workspace folders
            ws = get_workspace().get("folders", [])
            target_resolved = None
            try:
                target_resolved = target.resolve()
            except Exception:
                return self._send_json(400, {"error": "bad path"})
            allowed = False
            for f in ws:
                try:
                    root = Path(f).resolve()
                    if str(target_resolved).lower().startswith(str(root).lower()):
                        allowed = True
                        break
                except Exception:
                    continue
            if not allowed:
                return self._send_json(403, {"error": "path outside workspace"})
            if not target_resolved.exists() or not target_resolved.is_dir():
                return self._send_json(404, {"error": "not a directory"})
            entries = []
            try:
                for child in sorted(target_resolved.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    try:
                        is_dir = child.is_dir()
                        info = {
                            "name": child.name,
                            "path": str(child),
                            "is_dir": is_dir,
                            "size": 0 if is_dir else child.stat().st_size,
                            "ext": "" if is_dir else child.suffix.lstrip(".").lower(),
                        }
                        entries.append(info)
                    except Exception:
                        continue
            except PermissionError:
                return self._send_json(403, {"error": "permission denied"})
            return self._send_json(200, {"path": str(target_resolved), "entries": entries})
        return self._send_json(404, {"error": "not found"})

    def _handle_api_post(self, p: str, parsed):
        body = self._read_json()
        if p == "/api/settings":
            cur = get_settings()
            cur.update({k: v for k, v in body.items() if k in DEFAULT_SETTINGS})
            save_json(SETTINGS_FILE, cur)
            broadcast_event({"type": "settings:update"})
            return self._send_json(200, cur)
        if p == "/api/hardware/apply":
            model = (body.get("model") or "").strip()
            optimal = get_combined_optimal_settings(model or None)
            hw_info = optimal.pop("_hardware", {})
            model_info = optimal.pop("_model", None)
            cur = get_settings()
            for key in ["num_ctx", "num_gpu", "num_batch", "num_thread", "num_predict", "keep_alive", "temperature", "top_p", "kv_cache_type"]:
                if key in optimal:
                    cur[key] = optimal[key]
            save_json(SETTINGS_FILE, cur)
            broadcast_event({"type": "settings:update"})
            if model_info and not model_info.get("error"):
                msg = f"Tuned for {hw_info['gpus'][0]['name'] if hw_info['gpus'] else 'CPU'} + {model_info['name']} ({model_info['est_weights_gb']}GB weights)"
            else:
                msg = f"Tuned for {hw_info['gpus'][0]['name'] if hw_info['gpus'] else 'CPU'} ({hw_info['ram_gb']}GB RAM)"
            return self._send_json(200, {
                "ok": True,
                "applied": optimal,
                "hardware": hw_info,
                "model": model_info,
                "message": msg,
            })
        if p == "/api/workspace":
            folders = body.get("folders") or []
            folders = [normalize_path(f) for f in folders if isinstance(f, str) and f.strip()]
            save_json(WORKSPACE_FILE, {"folders": folders})
            broadcast_event({"type": "workspace:update"})
            return self._send_json(200, {"folders": folders})
        if p == "/api/browse-folder":
            # native OS folder picker, only on the machine running the bridge.
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title="Add workspace folder")
                root.destroy()
                return self._send_json(200, {"path": path or ""})
            except Exception as e:
                return self._send_json(200, {"path": "", "error": str(e)})
        if p == "/api/chats":
            chat_id = body.get("id") or uuid.uuid4().hex[:12]
            chats = get_chats()
            if chat_id not in chats["chats"]:
                chats["chats"][chat_id] = {
                    "id": chat_id,
                    "title": body.get("title") or "new session",
                    "created": int(time.time()),
                    "updated": int(time.time()),
                    "messages": [],
                }
                chats["order"].insert(0, chat_id)
                save_json(CHATS_FILE, chats)
            return self._send_json(200, chats["chats"][chat_id])
        if p.startswith("/api/chats/") and p.endswith("/rename"):
            cid = p.split("/")[3]
            chats = get_chats()
            if cid in chats["chats"]:
                chats["chats"][cid]["title"] = (body.get("title") or "").strip() or chats["chats"][cid]["title"]
                save_json(CHATS_FILE, chats)
                return self._send_json(200, chats["chats"][cid])
            return self._send_json(404, {"error": "not found"})
        if p == "/api/approvals/decide":
            ok = decide_approval(body.get("id") or "", body.get("decision") or "deny")
            return self._send_json(200 if ok else 404, {"ok": ok})
        if p == "/api/tools/call":
            job_id = uuid.uuid4().hex[:12]
            name = body.get("name") or ""
            args = body.get("arguments") or {}

            def _do_job():
                with _tool_jobs_lock:
                    _tool_jobs[job_id]["status"] = "running"
                try:
                    result = invoke_tool(name, args)
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["status"] = "done"
                        _tool_jobs[job_id]["result"] = result
                except Exception as e:
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["status"] = "error"
                        _tool_jobs[job_id]["result"] = {"error": str(e)}
                finally:
                    with _tool_jobs_lock:
                        _tool_jobs[job_id]["finished"] = int(time.time())

            with _tool_jobs_lock:
                _tool_jobs[job_id] = {
                    "id": job_id,
                    "status": "queued",
                    "name": name,
                    "started": int(time.time()),
                    "finished": None,
                    "result": None,
                }
            _tool_executor.submit(_do_job)
            return self._send_json(202, {"job_id": job_id, "status": "queued"})
        if p == "/api/chat":
            return self._handle_chat(body)
        if p == "/api/prewarm":
            # load the model into VRAM so the first chat isn't a 30s cold start.
            # ollama's /api/generate with empty prompt + keep_alive does this.
            model = (body.get("model") or "").strip()
            if not model:
                return self._send_json(400, {"error": "model required"})
            try:
                payload = {"model": model, "prompt": "", "keep_alive": get_settings().get("keep_alive", "30m"), "stream": False}
                req = urllib.request.Request(
                    f"{OLLAMA}/api/generate",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=120) as resp:
                    resp.read()
                return self._send_json(200, {"ok": True, "model": model})
            except Exception as e:
                return self._send_json(200, {"ok": False, "error": str(e)})
        if p == "/api/desktop/panic":
            _desktop_panic.set()
            # deny every pending approval so any in-flight action unblocks fast
            with _approvals_lock:
                pending = [a["id"] for a in _approvals.values() if a.get("status") == "pending"]
            for aid in pending:
                decide_approval(aid, "deny")
            broadcast_event({"type": "desktop:panic", "on": True})
            return self._send_json(200, {"ok": True, "panic": True})
        if p == "/api/desktop/resume":
            _desktop_panic.clear()
            broadcast_event({"type": "desktop:panic", "on": False})
            return self._send_json(200, {"ok": True, "panic": False})
        if p == "/api/memories/forget":
            mid = (body.get("id") or "").strip()
            if not mid:
                return self._send_json(400, {"error": "id required"})
            r = tool_forget({"id": mid})
            broadcast_event({"type": "memories:update"})
            return self._send_json(200, r)
        if p == "/api/memories/clear":
            try:
                _save_memories([])
                broadcast_event({"type": "memories:update"})
                return self._send_json(200, {"ok": True})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/memories":
            # manual add from the memories panel in Settings
            text = (body.get("text") or "").strip()
            tags = body.get("tags") or []
            if not text:
                return self._send_json(400, {"error": "text required"})
            r = tool_remember({"text": text, "tags": tags if isinstance(tags, list) else []})
            broadcast_event({"type": "memories:update"})
            return self._send_json(200, r)
        if p == "/api/snapshots":
            # save the currently-rendered preview html (or any html blob the
            # client wants to keep) to data/snapshots/ with a safe filename.
            raw_name = (body.get("name") or "snapshot").strip()
            html = body.get("html") or ""
            if not html:
                return self._send_json(400, {"error": "html required"})
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name)[:60] or "snapshot"
            if not safe.lower().endswith(".html"):
                safe = safe + ".html"
            ts = time.strftime("%Y%m%d-%H%M%S")
            final_name = f"{ts}-{safe}"
            out_path = SNAPSHOTS_DIR / final_name
            out_path.write_text(html, encoding="utf-8")
            return self._send_json(200, {
                "ok": True,
                "name": final_name,
                "path": str(out_path),
                "url": f"/api/snapshots/{final_name}",
            })
        if p == "/api/desktop/chat-toggle":
            cid = (body.get("chat_id") or "").strip()
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            disabled = bool(body.get("disabled"))
            if disabled:
                _chat_desktop_disabled.add(cid)
            else:
                _chat_desktop_disabled.discard(cid)
            return self._send_json(200, {"chat_id": cid, "disabled": disabled})
        if p == "/api/system-context/refresh":
            try:
                md = rescan_system_context()
                broadcast_event({"type": "system-context:update"})
                return self._send_json(200, {"md": md, "path": str(SYSTEM_CONTEXT_FILE)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/system-context":
            md = (body.get("md") or "").strip()
            if not md:
                return self._send_json(400, {"error": "md empty"})
            try:
                SYSTEM_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
                SYSTEM_CONTEXT_FILE.write_text(md + "\n", encoding="utf-8")
                broadcast_event({"type": "system-context:update"})
                return self._send_json(200, {"md": md, "path": str(SYSTEM_CONTEXT_FILE)})
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        return self._send_json(404, {"error": "not found"})

    # ---- SSE

    def _serve_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self._set_cors()
        self.end_headers()
        q = subscribe()
        try:
            # send hello + any pending approvals so reconnects resync
            self._sse_send({"type": "hello", "t": int(time.time())})
            for a in list_approvals():
                self._sse_send({"type": "approval:new", "approval": a})
            last_ping = time.time()
            while True:
                try:
                    evt = q.get(timeout=15)
                    self._sse_send(evt)
                except Empty:
                    if time.time() - last_ping > 14:
                        try:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_ping = time.time()
                        except Exception:
                            break
        except Exception:
            pass
        finally:
            unsubscribe(q)

    def _sse_send(self, obj: dict):
        try:
            self.wfile.write(b"data: ")
            self.wfile.write(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            self.wfile.write(b"\n\n")
            self.wfile.flush()
        except Exception:
            raise

    # ---- chat streaming endpoint

    def _handle_chat(self, body: dict):
        chat_id = body.get("chat_id") or uuid.uuid4().hex[:12]
        user_text = (body.get("message") or "").strip()
        mode = body.get("mode") or "auto"  # auto | ide | agent
        images = body.get("images") or []  # list of base64 data URLs
        regenerate = bool(body.get("regenerate"))
        if not user_text and not images and not regenerate:
            return self._send_json(400, {"error": "empty message"})

        # describe any attached images with the small vision model first; the main
        # chat model only ever sees text, so VRAM stays free for reasoning context
        if images:
            descriptions = []
            for i, img in enumerate(images):
                desc = describe_image(img, hint=user_text)
                descriptions.append(f"[image {i + 1} — transcribed by vision model]\n{desc}")
            vision_block = "\n\n".join(descriptions)
            user_text = (user_text + "\n\n" + vision_block).strip() if user_text else vision_block

        chats = get_chats()
        if chat_id not in chats["chats"]:
            chats["chats"][chat_id] = {
                "id": chat_id,
                "title": _title_from_prompt(user_text),
                "created": int(time.time()),
                "updated": int(time.time()),
                "messages": [],
            }
            chats["order"].insert(0, chat_id)
        chat = chats["chats"][chat_id]
        # remember the mode this chat was last used in so the client can
        # restore it on session switch
        chat["last_mode"] = mode
        # regenerate: drop the last assistant turn so we re-run on the same
        # prior user message.  only valid if the most recent message is
        # actually an assistant reply.
        if regenerate:
            while chat["messages"] and chat["messages"][-1].get("role") == "assistant":
                chat["messages"].pop()
            if not chat["messages"] or chat["messages"][-1].get("role") != "user":
                save_json(CHATS_FILE, chats)
                return self._send_json(400, {"error": "nothing to regenerate"})
            user_text = chat["messages"][-1].get("content", "")
        else:
            # auto-name any chat still using the default placeholder when its first
            # user message comes in
            is_first_user_msg = not any(m.get("role") == "user" for m in chat.get("messages", []))
            if is_first_user_msg and chat.get("title", "").strip().lower() in ("", "new session", "new conversation"):
                chat["title"] = _title_from_prompt(user_text)
                broadcast_event({"type": "chat:rename", "chat_id": chat_id, "title": chat["title"]})
            chat["messages"].append({"role": "user", "content": user_text, "t": int(time.time())})
        chat["updated"] = int(time.time())
        save_json(CHATS_FILE, chats)

        use_tools = mode != "ide"
        system_prompt = build_system_prompt(include_tools=use_tools)
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        for m in chat["messages"]:
            if m.get("role") in ("user", "assistant"):
                msgs.append({"role": m["role"], "content": m.get("content", "")})

        # set up SSE response — Connection: close so the browser reader resolves done
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self._set_cors()
        self.end_headers()

        def emit(evt: dict):
            evt = {**evt, "chat_id": chat_id}
            try:
                self.wfile.write(b"data: ")
                self.wfile.write(json.dumps(evt, ensure_ascii=False).encode("utf-8"))
                self.wfile.write(b"\n\n")
                self.wfile.flush()
            except Exception:
                raise
            broadcast_event(evt)

        emit({"type": "chat_start", "chat_id": chat_id})
        tok = _current_chat_id.set(chat_id)
        try:
            final = run_chat_turn(chat_id, msgs, use_tools=use_tools, emit=emit)
        except Exception as e:
            traceback.print_exc()
            try:
                emit({"type": "error", "error": str(e)})
            except Exception:
                return
            final = None
        finally:
            _current_chat_id.reset(tok)

        if final:
            chats = get_chats()
            if chat_id in chats["chats"]:
                chat = chats["chats"][chat_id]
                msg = {
                    "role": "assistant",
                    "content": final.get("content", ""),
                    "t": int(time.time()),
                }
                stats = final.get("_stats") or {}
                if stats.get("eval_count") is not None:
                    msg["tokens"] = stats["eval_count"]
                if stats.get("prompt_eval_count") is not None:
                    msg["prompt_tokens"] = stats["prompt_eval_count"]
                chat["messages"].append(msg)
                chat["updated"] = int(time.time())
                html = extract_html(final.get("content", ""))
                if html:
                    entry = save_version(chat_id, html, label=user_text[:80])
                    chat["last_version"] = entry["id"]
                    try:
                        emit({"type": "version_saved", "version": entry})
                    except Exception:
                        pass
                save_json(CHATS_FILE, chats)

        try:
            emit({"type": "chat_end"})
        except Exception:
            pass


# ---- ollama management -----------------------------------------------------

def ollama_ping(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False


def ensure_ollama(wait_seconds: int = 30) -> bool:
    """Return True if Ollama is reachable. Start it if not, wait for readiness."""
    if ollama_ping():
        return True
    exe = shutil.which("ollama")
    if not exe:
        print("  [warn] ollama not on PATH. install from https://ollama.com/download")
        return False
    print("  starting ollama serve with perf env ...")
    try:
        env = os.environ.copy()
        # perf tuning applied to the spawned ollama process only
        env.setdefault("OLLAMA_FLASH_ATTENTION", "1")
        env.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
        env.setdefault("OLLAMA_KEEP_ALIVE", "30m")
        creationflags = 0
        if os.name == "nt":
            creationflags = 0x00000008 | 0x00000200  # DETACHED | NEW_PROCESS_GROUP
        subprocess.Popen(
            [exe, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception as e:
        print(f"  [warn] could not spawn ollama: {e}")
        return False
    t0 = time.time()
    while time.time() - t0 < wait_seconds:
        time.sleep(0.6)
        if ollama_ping(timeout=1.5):
            print(f"  ollama up in {time.time() - t0:.1f}s")
            return True
    print(f"  [warn] ollama not responding after {wait_seconds}s")
    return False


# ---- main ------------------------------------------------------------------

def main():
    print(f"accuretta bridge")
    print(f"  root:    {ROOT}")
    print(f"  ollama:  {OLLAMA}")
    print(f"  port:    {PORT}")
    print(f"  bind:    0.0.0.0  (reachable over LAN / Tailscale)")

    # Auto-enable Flash Attention for high-end GPUs
    try:
        gpus = _detect_gpu_info()
        if any(g.get("vram_gb", 0) >= 16 for g in gpus):
            os.environ.setdefault("OLLAMA_FLASH_ATTENTION", "1")
            os.environ.setdefault("OLLAMA_KV_CACHE_TYPE", "q8_0")
            print("  flash attention: enabled (16GB+ GPU detected)")
    except Exception:
        pass

    # first-run system context scan (creates data/ACCURETTA.md if missing)
    if not SYSTEM_CONTEXT_FILE.exists():
        print("  scanning system (first run) ...")
        ensure_system_context()
        print(f"  wrote:   {SYSTEM_CONTEXT_FILE}")
    else:
        print(f"  context: {SYSTEM_CONTEXT_FILE} (edit or delete to rescan)")

    ensure_ollama(wait_seconds=30)

    try:
        tags = ollama_get("/api/tags")
        models = [m.get("name") for m in tags.get("models", [])]
        print(f"  models:  {', '.join(models) if models else '(none — run: ollama pull qwen3:8b)'}")
    except Exception as e:
        print(f"  models:  (ollama unreachable: {e})")

    s = get_settings()
    if not s.get("model"):
        try:
            tags = ollama_get("/api/tags")
            for pref in ["qwen3:8b", "qwen2.5:7b", "qwen2.5-coder:7b", "llama3.1:8b"]:
                if any(m.get("name", "").startswith(pref) for m in tags.get("models", [])):
                    s["model"] = pref
                    save_json(SETTINGS_FILE, s)
                    print(f"  chose default model: {pref}")
                    break
            else:
                if tags.get("models"):
                    s["model"] = tags["models"][0].get("name")
                    save_json(SETTINGS_FILE, s)
                    print(f"  chose default model: {s['model']}")
        except Exception:
            pass

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"\nopen {url}  — or from phone, http://<tailscale-ip>:{PORT}")

    # open browser shortly after bind (in a thread, so serve_forever owns the main thread)
    def _open_browser():
        time.sleep(0.8)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")


if __name__ == "__main__":
    main()
