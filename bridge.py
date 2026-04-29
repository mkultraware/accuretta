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

# ---- firmware analysis (optional) ----------------------------------------
# Signature scanning is pure-Python (no binwalk dependency — the pip
# "binwalk" package is a broken stub unrelated to the real tool).
# PySquashfsImage handles squashfs extraction, pyelftools parses ELF
# headers. Both are graceful — tools self-report the missing dep when
# invoked so the agent can tell the user what to install.
try:
    from PySquashfsImage import SquashFsImage  # type: ignore
    _HAVE_SQUASHFS = True
except Exception:
    SquashFsImage = None  # type: ignore
    _HAVE_SQUASHFS = False

try:
    from elftools.elf.elffile import ELFFile  # type: ignore
    _HAVE_ELFTOOLS = True
except Exception:
    ELFFile = None  # type: ignore
    _HAVE_ELFTOOLS = False

try:
    import capstone as _capstone  # type: ignore
    _HAVE_CAPSTONE = True
except Exception:
    _capstone = None  # type: ignore
    _HAVE_CAPSTONE = False

try:
    import r2pipe as _r2pipe  # type: ignore
    _HAVE_R2PIPE = True
except Exception:
    _r2pipe = None  # type: ignore
    _HAVE_R2PIPE = False

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

# per-chat cancellation. `cancel` flips when user hits Stop (via /api/cancel or
# client disconnect). `resp` is the live urllib response to llama-server, which
# we close explicitly to abort generation server-side — closing the socket is
# the only reliable way to make llama-server stop emitting tokens.
_chat_cancels: dict[str, dict] = {}
_chat_cancels_lock = threading.Lock()


def _register_cancel(chat_id: str) -> threading.Event:
    ev = threading.Event()
    with _chat_cancels_lock:
        _chat_cancels[chat_id] = {"cancel": ev, "resp": None}
    return ev


def _set_cancel_resp(chat_id: str, resp) -> None:
    with _chat_cancels_lock:
        if chat_id in _chat_cancels:
            _chat_cancels[chat_id]["resp"] = resp


def _unregister_cancel(chat_id: str) -> None:
    with _chat_cancels_lock:
        _chat_cancels.pop(chat_id, None)


def cancel_chat(chat_id: str) -> bool:
    """Flip the cancel flag and force-close the active llama-server response
    for this chat. Returns True if something was cancelled."""
    with _chat_cancels_lock:
        entry = _chat_cancels.get(chat_id)
        if not entry:
            return False
        entry["cancel"].set()
        resp = entry.get("resp")
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass
        try:
            # reach through urllib to the raw socket and hard-shut it so
            # llama-server notices within one token's worth of time.
            fp = getattr(resp, "fp", None)
            sock = getattr(getattr(fp, "raw", None), "_sock", None)
            if sock:
                import socket as _sock
                try:
                    sock.shutdown(_sock.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
        except Exception:
            pass
    return True

# thread pool for long-running tools so the HTTP worker stays responsive
_tool_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool-")

# async tool jobs (for POST /api/tools/call returning job-id)
_tool_jobs: dict[str, dict] = {}
_tool_jobs_lock = threading.Lock()

for d in (DATA, VERSIONS_DIR, PENDING_DIR, SNAPSHOTS_DIR):
    d.mkdir(parents=True, exist_ok=True)

def _resolve_llama_url() -> str:
    """Resolve llama-server URL. Default: http://127.0.0.1:8080.
    Env LLAMA_HOST accepts 'host:port', bare host, or full URL."""
    raw = (os.environ.get("LLAMA_HOST") or os.environ.get("LLAMA_URL") or "").strip()
    if not raw:
        return "http://127.0.0.1:8080"
    if not raw.startswith(("http://", "https://")):
        raw = "http://" + raw
    try:
        scheme, rest = raw.split("://", 1)
        hostport = rest.split("/", 1)[0]
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "8080"
    except Exception:
        return "http://127.0.0.1:8080"
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port}"

LLAMA = _resolve_llama_url()
# optional separate vision-capable llama-server. If unset, we assume the main
# llama-server is vision-capable (started with --mmproj). If it isn't, image
# messages just fail — caller should handle.
VISION_LLAMA = (os.environ.get("VISION_LLAMA_HOST") or "").strip()
if VISION_LLAMA and not VISION_LLAMA.startswith(("http://", "https://")):
    VISION_LLAMA = "http://" + VISION_LLAMA
if not VISION_LLAMA:
    VISION_LLAMA = LLAMA
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
    "top_k": 40,
    "min_p": 0.05,
    "repeat_penalty": 1.1,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "keep_alive": "30m",
    "theme": "light",
    "auto_approve_read": True,
    "allow_web_preview": True,
    # memory / performance
    "kv_cache_type": "q8_0",        # q4_0 | q8_0 | f16 — lower = less VRAM, slightly lower quality
    # IDE preview extras (composer toolbar toggles)
    "use_tailwind_cdn": False,      # inject Tailwind Play CDN into preview + ask model to use tailwind classes
    "ide_multifile": False,         # tell the model to emit a small folder structure (index.html / style.css / script.js / assets/)
    # reasoning / thinking (Qwen3-family and other reasoner models)
    "enable_thinking": True,        # when False, suppress <think> blocks entirely via chat_template_kwargs
    "thinking_budget": 2048,        # cap tokens the model spends thinking before it must answer. -1 = unlimited
    "max_tool_rounds": 60,          # how many tool-call rounds the model may run per user turn before forced stop
    "preserve_prior_thinking": True,# rewrite prior <think>…</think> as plain text so it survives chat-template stripping
    # llama-server lifecycle (bridge spawns it for us)
    "models_dir": "",               # folder containing .gguf files (set via Settings -> Models folder)
    "model_path": "",               # full path to the currently loaded .gguf
    "llama_bin": "",                # override path to llama-server.exe (auto-detected if blank)
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


# Hard cap on how many messages we retain per chat in chats.json. Trimmer at
# request time gives the *model* a tight context window; this cap is purely
# disk hygiene so a long-running firmware investigation doesn't grow the JSON
# file unbounded. Anchored on the first user message — it's the task statement
# and the trimmer always keeps it as the second message after system.
CHAT_HISTORY_MAX = 1000


def _enforce_chat_retention(chat: dict) -> None:
    msgs = chat.get("messages") or []
    if len(msgs) <= CHAT_HISTORY_MAX:
        return
    # find the first user message — that's our anchor we never drop
    first_user_idx = next(
        (i for i, m in enumerate(msgs) if m.get("role") == "user"),
        None,
    )
    overflow = len(msgs) - CHAT_HISTORY_MAX
    # drop oldest messages AFTER the first user, walking forward
    if first_user_idx is None:
        # no user message? just trim from the front
        chat["messages"] = msgs[overflow:]
        return
    keep_head = msgs[: first_user_idx + 1]
    rest = msgs[first_user_idx + 1:]
    # drop `overflow` oldest from rest
    rest = rest[overflow:]
    chat["messages"] = keep_head + rest


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
    """Conveyor belt: drop oldest middle messages while anchoring the system
    prompt (current goals/memory) AND the first user message (the original ask
    that frames the whole conversation). Most recent messages always keep.

    Layout: [system] + [first_user] + ...dropped... + [recent...]
    Reserve covers room for the next assistant reply + reasoning.
    """
    if not msgs:
        return msgs
    budget = max_tokens - reserve

    # Anchor 1: system prompt at index 0 (if present).
    system = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
    rest = msgs[len(system):]

    # Anchor 2: the first user message in `rest` (the original request). We
    # also pull the assistant reply that immediately follows it, because tool
    # call / tool result pairs must stay together to be valid OpenAI history.
    anchor: list[dict] = []
    anchor_end = 0
    for i, m in enumerate(rest):
        if m.get("role") == "user":
            anchor = [m]
            anchor_end = i + 1
            # include directly-following assistant + tool messages that pair
            # with this first user turn (avoid splitting a tool_call/result).
            while anchor_end < len(rest) and rest[anchor_end].get("role") in ("assistant", "tool"):
                anchor.append(rest[anchor_end])
                anchor_end += 1
            break

    middle_and_tail = rest[anchor_end:]

    # Count anchored cost first; if it already busts the budget, give up on
    # the anchor (long first message in a tiny ctx) and fall back to plain
    # tail-only behavior.
    anchor_cost = sum(_count_msg_tokens(m) for m in system) + sum(_count_msg_tokens(m) for m in anchor)
    if anchor_cost > budget * 0.6:
        anchor = []
        anchor_cost = sum(_count_msg_tokens(m) for m in system)

    # Walk recent messages from the end backwards, keeping until budget hits.
    keep: list[dict] = []
    total = anchor_cost
    for m in reversed(middle_and_tail):
        t = _count_msg_tokens(m)
        if total + t > budget and keep:
            break
        keep.insert(0, m)
        total += t

    if not keep and not anchor:
        # Emergency: keep only the very last message.
        keep = middle_and_tail[-1:] or rest[-1:]

    # Pair-protect the boundary: a `tool` message at the front of `keep` is
    # an orphan if its parent assistant (with the matching tool_call_id) was
    # dropped. llama-server rejects orphan tool messages, so drop them here.
    while keep and keep[0].get("role") == "tool":
        keep.pop(0)

    # If the anchor is the same object as the first kept message (very short
    # convo), don't duplicate it.
    if anchor and keep and anchor[0] is keep[0]:
        return system + keep
    return system + anchor + keep


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

    # GGUF model directories — check common llama.cpp / unsloth / lm-studio spots
    gguf_dirs = []
    candidates = [
        os.environ.get("LLAMA_MODELS"),
        str(home / "models"),
        str(home / ".cache" / "llama.cpp"),
        str(home / ".cache" / "unsloth"),
        str(home / ".cache" / "huggingface" / "hub"),
        str(home / ".lmstudio" / "models"),
        r"C:\llama.cpp\models",
    ]
    for c in candidates:
        if c and Path(c).exists():
            gguf_dirs.append(c)
    if gguf_dirs:
        facts["gguf_model_dirs"] = gguf_dirs

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


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def tool_web_search(args: dict) -> dict:
    """Search the web via DuckDuckGo's no-JS HTML endpoint. No API key.
    Returns a list of {title, url, snippet}. The model usually wants to
    follow up with web_fetch on the top few URLs to read full content."""
    q = (args.get("query") or args.get("q") or "").strip()
    if not q:
        return {"error": "query required"}
    max_results = int(args.get("max_results") or 6)
    max_results = max(1, min(max_results, 20))
    try:
        body = urllib.parse.urlencode({"q": q}).encode()
        req = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=body,
            method="POST",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) accuretta/1.0",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "text/html",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"search failed: {e}"}
    snippets = [_strip_tags(s) for s in _DDG_SNIPPET_RE.findall(html)]
    results = []
    for i, m in enumerate(_DDG_RESULT_RE.finditer(html)):
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        # ddg wraps outbound links as /l/?uddg=<encoded>
        try:
            pu = urllib.parse.urlparse(url)
            if "duckduckgo.com" in pu.netloc and pu.path.startswith("/l/"):
                qs = urllib.parse.parse_qs(pu.query)
                real = qs.get("uddg", [""])[0]
                if real:
                    url = urllib.parse.unquote(real)
        except Exception:
            pass
        title = _strip_tags(m.group(2))
        snippet = snippets[i] if i < len(snippets) else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    return {"query": q, "results": results, "count": len(results)}


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


# ============================================================================
# Firmware analysis tools — read-only triage on binaries dropped in the
# workspace. Designed for first-pass router / IoT firmware audits. No system
# tools, no shells. Pure Python where possible, optional pip libs elsewhere.
# Extraction is the only destructive op and uses the standard approval card.
# ============================================================================

# Magic-byte signature table for tool_file_inspect. Only formats we expect
# to see in consumer firmware. Order matters — longer signatures first.
_FW_MAGIC_TABLE = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"PK\x03\x04", "zip"),
    (b"PK\x05\x06", "zip (empty)"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"hsqs", "squashfs (le)"),
    (b"sqsh", "squashfs (be)"),
    (b"\x85\x19\x03\x20", "jffs2 (le)"),
    (b"\x19\x85\x20\x03", "jffs2 (be)"),
    (b"\x45\x3d\xcd\x28", "cramfs (le)"),
    (b"\x28\xcd\x3d\x45", "cramfs (be)"),
    (b"HDR0", "trx (router header)"),
    (b"\x7fELF", "ELF"),
    (b"MZ", "PE/DOS executable"),
    (b"\xca\xfe\xba\xbe", "Java class / Mach-O fat"),
    (b"070701", "cpio (newc ascii)"),
    (b"070707", "cpio (binary)"),
]


def _fw_identify_magic(data: bytes) -> str | None:
    for sig, name in _FW_MAGIC_TABLE:
        if data.startswith(sig):
            return name
    # ustar lives at offset 257 in a tar header
    if len(data) >= 263 and data[257:262] == b"ustar":
        return "tar"
    return None


def _fw_check_path(path: str, must_exist: bool = True) -> tuple[str, dict | None]:
    """Resolve + validate a workspace path. Returns (resolved, error_or_None)."""
    p = normalize_path(path or "")
    if not p:
        return "", {"error": "missing path"}
    if must_exist and not os.path.exists(p):
        return p, {"error": f"not found: {p}"}
    if is_blocked_path(p):
        return p, {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(p):
        return p, {"error": "path outside workspace. Add folder in Workspace panel."}
    return p, None


# Signatures scanned for at any offset in tool_binwalk_scan. Kept short and
# unambiguous to minimize false positives. This isn't binwalk's full database
# — just the formats that actually show up in consumer router firmware.
_FW_SCAN_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\x1f\x8b\x08", "gzip"),
    (b"BZh9", "bzip2 (level 9)"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"7z\xbc\xaf\x27\x1c", "7z"),
    (b"PK\x03\x04", "zip (local file)"),
    (b"PK\x05\x06", "zip (end of central dir)"),
    (b"hsqs", "squashfs (le)"),
    (b"sqsh", "squashfs (be)"),
    (b"\x85\x19\x03\x20", "jffs2 (le)"),
    (b"\x19\x85\x20\x03", "jffs2 (be)"),
    (b"\x45\x3d\xcd\x28", "cramfs (le)"),
    (b"\x28\xcd\x3d\x45", "cramfs (be)"),
    (b"\x7fELF", "ELF executable"),
    (b"HDR0", "trx router header"),
    (b"-rom1fs-", "romfs"),
    (b"070701", "cpio (newc ascii)"),
    (b"070707", "cpio (binary)"),
    (b"UBI#", "UBI image"),
    (b"!<arch>", "ar archive"),
]


def tool_binwalk_scan(args: dict) -> dict:
    """Scan a binary for known magic-byte signatures at any offset.
    Pure-Python — covers gzip, bzip2, xz, 7z, zip, squashfs, jffs2,
    cramfs, ELF, TRX, romfs, cpio, UBI, ar. Returns sorted offset table."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    max_results = max(1, min(int(args.get("max_results") or 200), 1000))
    max_bytes = max(1024, min(int(args.get("max_bytes") or 64 * 1024 * 1024),
                              256 * 1024 * 1024))
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        truncated = size > len(data)
        results: list[dict] = []
        for sig, name in _FW_SCAN_SIGNATURES:
            start = 0
            while True:
                idx = data.find(sig, start)
                if idx < 0:
                    break
                results.append({
                    "offset": idx,
                    "offset_hex": f"0x{idx:x}",
                    "description": name,
                })
                if len(results) >= max_results:
                    break
                start = idx + 1
            if len(results) >= max_results:
                break
        results.sort(key=lambda r: r["offset"])
        return {
            "path": path,
            "size": size,
            "scanned_bytes": len(data),
            "truncated": truncated,
            "count": len(results),
            "matches": results,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_strings_dump(args: dict) -> dict:
    """Extract printable ASCII string runs from a binary file. Optional
    regex filter. Caps reads at max_bytes so large firmwares stay sane."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    min_len = max(4, int(args.get("min_length") or 8))
    pattern = args.get("pattern")
    max_results = max(1, min(int(args.get("max_results") or 500), 5000))
    max_bytes = max(1024, min(int(args.get("max_bytes") or 16 * 1024 * 1024),
                              64 * 1024 * 1024))
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error as e:
        return {"error": f"bad pattern: {e}"}
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        run_re = re.compile(rb"[\x20-\x7e]{%d,}" % min_len)
        found: list[dict] = []
        for m in run_re.finditer(data):
            s = m.group(0).decode("ascii", errors="replace")
            if rx and not rx.search(s):
                continue
            found.append({"offset": m.start(), "string": s})
            if len(found) >= max_results:
                break
        return {
            "path": path,
            "scanned_bytes": len(data),
            "truncated": len(data) >= max_bytes,
            "match_count": len(found),
            "matches": found,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_file_inspect(args: dict) -> dict:
    """Identify a file by magic bytes. For ELF binaries, also report
    architecture, type, entry point, interpreter, and strip status."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(512)
        magic = _fw_identify_magic(head)
        info: dict = {
            "path": path,
            "size": size,
            "magic": magic or "unknown",
            "header_hex": head[:32].hex(),
        }
        if magic == "ELF" and _HAVE_ELFTOOLS:
            try:
                with open(path, "rb") as f:
                    elf = ELFFile(f)
                    interp_section = elf.get_section_by_name(".interp")
                    info["elf"] = {
                        "class": elf.header["e_ident"]["EI_CLASS"],
                        "data": elf.header["e_ident"]["EI_DATA"],
                        "machine": elf.header["e_machine"],
                        "type": elf.header["e_type"],
                        "entry": hex(elf.header["e_entry"]),
                        "stripped": elf.get_section_by_name(".symtab") is None,
                        "interpreter": (
                            interp_section.data().decode(errors="replace").rstrip("\x00")
                            if interp_section else None
                        ),
                    }
            except Exception as e:
                info["elf_error"] = str(e)
        return info
    except Exception as e:
        return {"error": str(e)}


def tool_read_bytes(args: dict) -> dict:
    """Read raw bytes at an offset. Returns hex + printable ASCII view.
    Use to inspect a header or an offset surfaced by binwalk_scan."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    offset = max(0, int(args.get("offset") or 0))
    length = max(1, min(int(args.get("length") or 256), 4096))
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(length)
        ascii_view = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        return {
            "path": path,
            "offset": offset,
            "length": len(data),
            "hex": data.hex(),
            "ascii": ascii_view,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_find_files(args: dict) -> dict:
    """Recursive glob under a directory with optional size cap. Good for
    triaging extracted firmware roots without reading everything."""
    root, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}"}
    pattern = (args.get("pattern") or "*").strip() or "*"
    max_size = int(args.get("max_size") or 0)  # 0 = no cap
    max_results = max(1, min(int(args.get("max_results") or 500), 5000))
    try:
        matches: list[dict] = []
        for p in Path(root).rglob(pattern):
            if not p.is_file():
                continue
            try:
                sz = p.stat().st_size
            except OSError:
                continue
            if max_size and sz > max_size:
                continue
            matches.append({"path": str(p), "size": sz})
            if len(matches) >= max_results:
                break
        return {"root": root, "count": len(matches), "matches": matches}
    except Exception as e:
        return {"error": str(e)}


def tool_extract_archive(args: dict) -> dict:
    """Auto-detect format and extract gzip/tar/zip/xz/bzip2/squashfs into
    a sandbox subdirectory next to the source. Destructive — gated by an
    approval card. Refuses path-traversal in archive members."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    dest_name = (args.get("dest_name") or f"{src.stem}_extracted").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    approval = request_approval(
        title="Extract archive",
        command=f'extract: "{src.name}" -> "{dest.name}"',
        details={"kind": "extract_archive", "source": str(src), "dest": str(dest)},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied extraction ({approval.get('status')})"}

    import gzip as _gz
    import tarfile as _tar
    import zipfile as _zip
    import lzma as _xz
    import bz2 as _bz

    def _safe_member(name: str) -> str | None:
        n = (name or "").replace("\\", "/").lstrip("/")
        if not n or ".." in n.split("/"):
            return None
        return n

    try:
        with open(src, "rb") as f:
            head = f.read(512)
        magic = _fw_identify_magic(head) or ""

        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        files_written = 0

        if magic == "gzip":
            # gzip usually wraps a tar in firmware contexts
            with _gz.open(src, "rb") as gz:
                inner_head = gz.read(512)
            if len(inner_head) >= 263 and inner_head[257:262] == b"ustar":
                with _gz.open(src, "rb") as gz, _tar.open(fileobj=gz, mode="r:") as tar:
                    for m in tar.getmembers():
                        n = _safe_member(m.name)
                        if not n:
                            continue
                        m.name = n
                        tar.extract(m, dest)
                        files_written += 1
            else:
                out = dest / (src.stem + ".bin")
                with _gz.open(src, "rb") as gz, open(out, "wb") as o:
                    shutil.copyfileobj(gz, o)
                files_written = 1
        elif magic == "tar":
            with _tar.open(src, "r:") as tar:
                for m in tar.getmembers():
                    n = _safe_member(m.name)
                    if not n:
                        continue
                    m.name = n
                    tar.extract(m, dest)
                    files_written += 1
        elif magic.startswith("zip"):
            with _zip.ZipFile(src) as z:
                for info in z.infolist():
                    n = _safe_member(info.filename)
                    if not n:
                        continue
                    info.filename = n
                    z.extract(info, dest)
                    files_written += 1
        elif magic == "xz":
            out = dest / (src.stem + ".bin")
            with _xz.open(src, "rb") as xz, open(out, "wb") as o:
                shutil.copyfileobj(xz, o)
            files_written = 1
        elif magic == "bzip2":
            out = dest / (src.stem + ".bin")
            with _bz.open(src, "rb") as bz, open(out, "wb") as o:
                shutil.copyfileobj(bz, o)
            files_written = 1
        elif magic.startswith("squashfs"):
            if not _HAVE_SQUASHFS:
                return {"error": "squashfs detected but PySquashfsImage not installed."}
            with SquashFsImage.from_file(str(src)) as img:
                for entry in img:
                    if getattr(entry, "is_dir", False):
                        continue
                    if not getattr(entry, "is_file", True):
                        continue
                    rel = (getattr(entry, "path", "") or "").lstrip("/")
                    n = _safe_member(rel)
                    if not n:
                        continue
                    out = dest / n
                    out.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        data = entry.read_bytes()
                    except Exception:
                        # API drift fallback
                        with entry.open() as fp:  # type: ignore[attr-defined]
                            data = fp.read()
                    with open(out, "wb") as o:
                        o.write(data)
                    files_written += 1
        else:
            shutil.rmtree(dest, ignore_errors=True)
            return {"error": f"unsupported or unrecognized format (magic={magic or 'unknown'})"}

        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "files_written": files_written,
            "magic": magic,
        }
    except Exception as e:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        return {"error": str(e)}


def tool_carve_file(args: dict) -> dict:
    """Carve a byte range [offset, offset+length] out of a source file into
    a new file in the same directory. Use when binwalk_scan surfaces an
    embedded gzip/squashfs/etc inside a custom container (e.g. the 0xd00dfe
    TP-Link/ASUS .pkgtb wrapper) — carve the range, THEN run extract_archive
    or extract_squashfs on the carved file. length=0 carves to EOF."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    try:
        size = os.path.getsize(src)
    except OSError as e:
        return {"error": str(e)}
    offset = max(0, int(args.get("offset") or 0))
    if offset >= size:
        return {"error": f"offset 0x{offset:x} >= file size 0x{size:x}"}
    length_raw = int(args.get("length") or 0)
    if length_raw <= 0:
        length = size - offset
    else:
        length = min(length_raw, size - offset)
    # cap absurd carves so runaway tool calls can't fill the disk
    max_carve = max(1024, min(int(args.get("max_bytes") or 512 * 1024 * 1024),
                              2 * 1024 * 1024 * 1024))
    if length > max_carve:
        return {"error": f"carve length {length} exceeds max_bytes {max_carve}; pass max_bytes explicitly to override."}

    dest_name = (args.get("dest_name") or f"{src.stem}_at_0x{offset:x}.bin").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single filename (no slashes)"}
    dest = src.parent / dest_name
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    # carving creates a new file, so gate behind approval like other writes
    approval = request_approval(
        title="Carve file",
        command=f'carve: "{src.name}" [0x{offset:x}..+0x{length:x}] -> "{dest.name}"',
        details={"kind": "carve_file", "source": str(src), "dest": str(dest),
                 "offset": offset, "length": length},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied carve ({approval.get('status')})"}

    try:
        chunk = 1024 * 1024
        written = 0
        head_hex = ""
        with open(src, "rb") as f, open(dest, "wb") as o:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                if written == 0:
                    head_hex = buf[:16].hex()
                o.write(buf)
                written += len(buf)
                remaining -= len(buf)
        # identify carved magic so the model knows what tool to chain next
        try:
            with open(dest, "rb") as f:
                head = f.read(512)
            magic = _fw_identify_magic(head) or "unknown"
        except Exception:
            magic = "unknown"
        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "offset": offset,
            "offset_hex": f"0x{offset:x}",
            "length": written,
            "head_hex": head_hex,
            "magic": magic,
        }
    except Exception as e:
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        return {"error": str(e)}


def tool_extract_squashfs(args: dict) -> dict:
    """Extract a squashfs image into a sandbox subdirectory next to the source.
    Pure-Python via PySquashfsImage — no system squashfs-tools needed. Refuses
    path-traversal in archive members. Requires user approval (destructive)."""
    if not _HAVE_SQUASHFS:
        return {"error": "PySquashfsImage not installed. pip install PySquashfsImage"}
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    dest_name = (args.get("dest_name") or f"{src.stem}_rootfs").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    if dest.exists() and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    # quick magic sanity check
    try:
        with open(src, "rb") as f:
            head = f.read(8)
    except Exception as e:
        return {"error": f"could not read source: {e}"}
    if not (head.startswith(b"hsqs") or head.startswith(b"sqsh")):
        return {"error": f"not a squashfs image (magic={head[:4].hex()}). Try binwalk_scan to find the offset and carve first."}

    approval = request_approval(
        title="Extract squashfs",
        command=f'extract squashfs: "{src.name}" -> "{dest.name}"',
        details={"kind": "extract_squashfs", "source": str(src), "dest": str(dest)},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied extraction ({approval.get('status')})"}

    def _safe_member(name: str) -> str | None:
        n = (name or "").replace("\\", "/").lstrip("/")
        if not n or ".." in n.split("/"):
            return None
        return n

    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)

        files_written = 0
        symlinks = 0
        skipped = 0
        total_bytes = 0

        with SquashFsImage.from_file(str(src)) as img:
            for entry in img:
                try:
                    rel = (getattr(entry, "path", "") or "").lstrip("/")
                    n = _safe_member(rel)
                    if not n:
                        skipped += 1
                        continue
                    out = dest / n
                    if getattr(entry, "is_dir", False):
                        out.mkdir(parents=True, exist_ok=True)
                        continue
                    out.parent.mkdir(parents=True, exist_ok=True)
                    if getattr(entry, "is_symlink", False):
                        # write symlink target as a tiny text file - Windows
                        # can't always make real symlinks without admin
                        target = getattr(entry, "readlink", lambda: "")()
                        try:
                            with open(out, "w", encoding="utf-8") as o:
                                o.write(f"[symlink] -> {target}\n")
                            symlinks += 1
                        except Exception:
                            skipped += 1
                        continue
                    if not getattr(entry, "is_file", True):
                        skipped += 1
                        continue
                    try:
                        data = entry.read_bytes()
                    except Exception:
                        # API drift fallback
                        try:
                            with entry.open() as fp:  # type: ignore[attr-defined]
                                data = fp.read()
                        except Exception:
                            skipped += 1
                            continue
                    with open(out, "wb") as o:
                        o.write(data)
                    files_written += 1
                    total_bytes += len(data)
                except Exception:
                    skipped += 1
                    continue

        return {
            "ok": True,
            "source": str(src),
            "dest": str(dest),
            "files_written": files_written,
            "symlinks": symlinks,
            "skipped": skipped,
            "total_bytes": total_bytes,
        }
    except Exception as e:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        except Exception:
            pass
        return {"error": str(e)}


# Skip these directories during recursive grep — they explode result counts
# without surfacing useful matches in firmware-analysis contexts.
_GREP_SKIP_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", "node_modules",
    ".venv", "venv", ".tox", "dist", "build",
}

# Files larger than this (per file) are skipped. Avoids wasting cycles
# grepping multi-MB binaries where strings_dump is the right tool.
_GREP_MAX_FILE_BYTES = 4 * 1024 * 1024


def tool_grep_files(args: dict) -> dict:
    """Recursive regex search across a directory tree. Returns matches with
    file path, line number, and the matched line (truncated). Skips binary
    files (null-byte heuristic) and obvious noise dirs (.git, node_modules).
    Best for searching extracted firmware roots for keywords like 'system(',
    'sprintf', auth strings, hardcoded creds, CGI handler names."""
    root, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isdir(root):
        return {"error": f"not a directory: {root}"}
    pattern = args.get("pattern") or ""
    if not pattern:
        return {"error": "missing pattern"}
    glob_pat = (args.get("glob") or "").strip() or None
    case_insensitive = bool(args.get("case_insensitive"))
    max_matches = max(1, min(int(args.get("max_matches") or 200), 2000))
    max_files = max(1, min(int(args.get("max_files") or 5000), 50000))
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return {"error": f"bad regex: {e}"}

    matches: list[dict] = []
    files_scanned = 0
    files_skipped_binary = 0
    files_skipped_size = 0
    truncated = False

    try:
        for cur_dir, dirnames, filenames in os.walk(root):
            # prune noise dirs in-place
            dirnames[:] = [d for d in dirnames if d not in _GREP_SKIP_DIRS]
            for fn in filenames:
                if files_scanned >= max_files:
                    truncated = True
                    break
                fp = os.path.join(cur_dir, fn)
                if glob_pat:
                    try:
                        if not Path(fp).match(glob_pat) and not fnmatch.fnmatch(fn, glob_pat):
                            continue
                    except Exception:
                        if not fnmatch.fnmatch(fn, glob_pat):
                            continue
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                if st.st_size > _GREP_MAX_FILE_BYTES:
                    files_skipped_size += 1
                    continue
                # null-byte heuristic on first 4KB to skip binaries
                try:
                    with open(fp, "rb") as f:
                        sniff = f.read(4096)
                    if b"\x00" in sniff:
                        files_skipped_binary += 1
                        continue
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        for lineno, line in enumerate(f, start=1):
                            if rx.search(line):
                                matches.append({
                                    "path": fp,
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:400],
                                })
                                if len(matches) >= max_matches:
                                    truncated = True
                                    break
                    files_scanned += 1
                    if truncated:
                        break
                except Exception:
                    continue
            if truncated:
                break
        return {
            "root": root,
            "pattern": pattern,
            "match_count": len(matches),
            "files_scanned": files_scanned,
            "files_skipped_binary": files_skipped_binary,
            "files_skipped_size": files_skipped_size,
            "truncated": truncated,
            "matches": matches,
        }
    except Exception as e:
        return {"error": str(e)}


# Capstone arch/mode mapping. Values resolved lazily so we don't fail
# import on systems without capstone.
def _capstone_md(arch: str, mode: str):
    if not _HAVE_CAPSTONE:
        return None, "capstone not installed"
    arch = (arch or "").lower()
    mode = (mode or "").lower()
    cs = _capstone
    arch_map = {
        "x86": (cs.CS_ARCH_X86, cs.CS_MODE_32),
        "x64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "x86_64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "amd64": (cs.CS_ARCH_X86, cs.CS_MODE_64),
        "arm": (cs.CS_ARCH_ARM, cs.CS_MODE_ARM),
        "thumb": (cs.CS_ARCH_ARM, cs.CS_MODE_THUMB),
        "arm64": (cs.CS_ARCH_ARM64, cs.CS_MODE_ARM),
        "aarch64": (cs.CS_ARCH_ARM64, cs.CS_MODE_ARM),
        "mips": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS32),
        "mips32": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS32),
        "mips64": (cs.CS_ARCH_MIPS, cs.CS_MODE_MIPS64),
        "ppc": (cs.CS_ARCH_PPC, cs.CS_MODE_32),
        "ppc64": (cs.CS_ARCH_PPC, cs.CS_MODE_64),
    }
    if arch not in arch_map:
        return None, f"unsupported arch: {arch}. Try: x86, x64, arm, thumb, arm64, mips, mips64, ppc, ppc64."
    cs_arch, cs_mode = arch_map[arch]
    # endianness override for MIPS/ARM/PPC
    if mode in ("le", "little"):
        cs_mode |= cs.CS_MODE_LITTLE_ENDIAN
    elif mode in ("be", "big"):
        cs_mode |= cs.CS_MODE_BIG_ENDIAN
    try:
        md = cs.Cs(cs_arch, cs_mode)
        md.detail = False
        return md, None
    except Exception as e:
        return None, f"capstone init failed: {e}"


def tool_disasm_at(args: dict) -> dict:
    """Disassemble N instructions at a file offset. Supports x86/x64/arm/
    thumb/arm64/mips/mips32/mips64/ppc/ppc64. Auto-detects arch/endianness
    when target is an ELF and arch arg is omitted. Use to inspect a function
    near an offset surfaced by binwalk_scan or a string xref."""
    if not _HAVE_CAPSTONE:
        return {"error": "capstone not installed. pip install capstone"}
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    offset = max(0, int(args.get("offset") or 0))
    count = max(1, min(int(args.get("count") or 32), 256))
    bytes_to_read = max(16, min(int(args.get("max_bytes") or 4096), 16 * 1024))
    arch = (args.get("arch") or "").strip()
    mode = (args.get("mode") or "").strip()
    base_addr = int(args.get("address") or offset)

    try:
        # auto-detect from ELF if arch not provided
        if not arch and _HAVE_ELFTOOLS:
            try:
                with open(path, "rb") as f:
                    if f.read(4) == b"\x7fELF":
                        f.seek(0)
                        elf = ELFFile(f)
                        em = elf.header["e_machine"]
                        ei = elf.header["e_ident"]["EI_DATA"]
                        cls = elf.header["e_ident"]["EI_CLASS"]
                        endian = "le" if "LSB" in ei else "be"
                        m = {
                            "EM_X86_64": ("x64", endian),
                            "EM_386": ("x86", endian),
                            "EM_ARM": ("arm", endian),
                            "EM_AARCH64": ("arm64", endian),
                            "EM_MIPS": ("mips64" if "ELFCLASS64" in cls else "mips32", endian),
                            "EM_PPC": ("ppc", endian),
                            "EM_PPC64": ("ppc64", endian),
                        }
                        if em in m:
                            arch = arch or m[em][0]
                            mode = mode or m[em][1]
            except Exception:
                pass

        if not arch:
            return {"error": "could not detect architecture; pass arch (e.g. mips, arm, x64)."}

        md, merr = _capstone_md(arch, mode)
        if merr:
            return {"error": merr}

        with open(path, "rb") as f:
            f.seek(offset)
            blob = f.read(bytes_to_read)
        if not blob:
            return {"error": f"no bytes at offset 0x{offset:x}"}

        instrs: list[dict] = []
        for ins in md.disasm(blob, base_addr):
            instrs.append({
                "address": f"0x{ins.address:x}",
                "bytes": ins.bytes.hex(),
                "mnemonic": ins.mnemonic,
                "op_str": ins.op_str,
            })
            if len(instrs) >= count:
                break

        return {
            "path": path,
            "arch": arch,
            "mode": mode or "default",
            "offset": offset,
            "address": f"0x{base_addr:x}",
            "instruction_count": len(instrs),
            "bytes_read": len(blob),
            "instructions": instrs,
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# SECURITY ANALYSIS TOOLS
# All tools degrade gracefully — they report the missing
# binary/package when invoked so the agent can tell the user.
# None of these kill startup if not installed.
# ============================================================

def _sec_which(cmd: str) -> str | None:
    """Return full path to a CLI tool or None if not found."""
    return shutil.which(cmd)


def _sec_run(cmd: list[str], timeout: int = 120) -> tuple[str, str, int]:
    """Run a subprocess, return (stdout, stderr, returncode)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.stdout, r.stderr, r.returncode
    except FileNotFoundError:
        return "", f"binary not found: {cmd[0]}", 127
    except subprocess.TimeoutExpired:
        return "", f"timed out after {timeout}s", 1
    except Exception as e:
        return "", str(e), 1


def _sec_check_path(raw: str) -> tuple[str, dict | None]:
    """Reuse firmware path checker for security tools."""
    return _fw_check_path(raw)


def tool_unblob_extract(args: dict) -> dict:
    """Extract firmware using unblob. Handles nested containers (SquashFS
    inside CPIO inside ZIP, etc). Requires 'unblob' on PATH.
    pip install unblob — also needs system deps: e2tools, jefferson, etc."""
    exe = _sec_which("unblob")
    if not exe:
        return {"error": "unblob not found. pip install unblob (also needs system deps — see unblob.io)"}

    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    dest_name = (args.get("dest_name") or "").strip()
    if not dest_name:
        dest_name = Path(path).stem + "_unblob"
    dest = str(Path(path).parent / dest_name)

    if os.path.exists(dest) and not args.get("overwrite"):
        return {"error": f"destination exists: {dest}. Pass overwrite=true to replace."}

    stdout, stderr, rc = _sec_run([exe, "-e", dest, path], timeout=300)

    if rc != 0:
        return {"error": f"unblob failed (rc={rc})", "stderr": stderr[:2000]}

    # Inventory the extraction — bounded to 500 entries
    entries: list[dict] = []
    try:
        for root, dirs, files in os.walk(dest):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__"}]
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    sz = -1
                entries.append({
                    "path": fp,
                    "size": sz,
                })
                if len(entries) >= 500:
                    break
            if len(entries) >= 500:
                break
    except Exception:
        pass

    return {
        "ok": True,
        "dest": dest,
        "files_found": len(entries),
        "truncated": len(entries) >= 500,
        "files": entries,
    }


def tool_checksec(args: dict) -> dict:
    """Check ELF binary for exploit mitigations: NX, stack canary, RELRO,
    PIE, ASLR, FORTIFY. Accepts a single binary or a directory (scans all ELFs).
    Requires checksec: pip install checksec.py"""
    exe = _sec_which("checksec")
    if not exe:
        return {"error": "checksec not found. pip install checksec.py"}

    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.exists(path):
        return {"error": f"path not found: {path}"}

    is_dir = os.path.isdir(path)

    if is_dir:
        stdout, stderr, rc = _sec_run(
            [exe, "--dir", path, "--output", "json"], timeout=120
        )
    else:
        stdout, stderr, rc = _sec_run(
            [exe, "--file", path, "--output", "json"], timeout=30
        )

    if not stdout.strip():
        # Try alternate flag style (checksec.sh compatible)
        if is_dir:
            stdout, stderr, rc = _sec_run([exe, "--dir=" + path], timeout=120)
        else:
            stdout, stderr, rc = _sec_run([exe, "--file=" + path], timeout=30)

    # Try to parse JSON, fall back to raw text
    result: dict = {"path": path}
    try:
        result["data"] = json.loads(stdout)
    except Exception:
        # Return raw but bounded
        lines = stdout.strip().splitlines()
        result["raw"] = lines[:200]
        result["truncated"] = len(lines) > 200

    if stderr.strip():
        result["stderr"] = stderr[:500]

    return result


def tool_flawfinder(args: dict) -> dict:
    """Static analysis of C/C++ source for common vulnerabilities.
    Returns findings ranked by severity (1-5). Saves full output to
    workspace as flawfinder_output.txt if findings exceed threshold.
    Requires: pip install flawfinder"""
    exe = _sec_which("flawfinder")
    if not exe:
        return {"error": "flawfinder not found. pip install flawfinder"}

    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.exists(path):
        return {"error": f"path not found: {path}"}

    min_level = max(1, min(int(args.get("min_level") or 1), 5))
    max_findings = max(1, min(int(args.get("max_findings") or 50), 500))

    stdout, stderr, rc = _sec_run(
        [exe, "--minlevel", str(min_level), "--dataonly", "--quiet", path],
        timeout=180,
    )

    if rc not in (0, 1):  # flawfinder returns 1 when findings exist
        return {"error": f"flawfinder failed (rc={rc})", "stderr": stderr[:1000]}

    # Parse findings — flawfinder text format
    # Each finding starts with "filename:line:" pattern
    finding_re = re.compile(
        r'^(?P<file>[^\s:]+):(?P<line>\d+):\s+'
        r'\[(?P<level>\d)\]\s+\((?P<type>[^)]+)\)\s+(?P<func>\S+):\s*(?P<desc>.+)$'
    )
    findings: list[dict] = []
    for line in stdout.splitlines():
        m = finding_re.match(line.strip())
        if m:
            findings.append({
                "file": m.group("file"),
                "line": int(m.group("line")),
                "level": int(m.group("level")),
                "type": m.group("type"),
                "function": m.group("func"),
                "description": m.group("desc").strip(),
            })

    # Sort by severity descending
    findings.sort(key=lambda x: x["level"], reverse=True)

    total = len(findings)
    truncated = total > max_findings

    # Save full output to workspace if large
    save_path = None
    if total > 20:
        try:
            ws = _get_workspace_dirs()
            if ws:
                save_path = os.path.join(ws[0], "flawfinder_output.txt")
                with open(save_path, "w", encoding="utf-8") as fh:
                    fh.write(stdout)
        except Exception:
            pass

    return {
        "path": path,
        "total_findings": total,
        "min_level": min_level,
        "truncated": truncated,
        "shown": min(total, max_findings),
        "findings": findings[:max_findings],
        "full_output_saved": save_path,
    }


def tool_firmwalker(args: dict) -> dict:
    """Search an extracted firmware rootfs for sensitive files: passwords,
    ssh keys, SSL certs, config files, hardcoded IPs, default creds.
    Requires firmwalker.sh on PATH or provide explicit path via exe_path.
    Download: https://github.com/craigz28/firmwalker"""
    exe_path = (args.get("exe_path") or "").strip()
    exe = exe_path or _sec_which("firmwalker.sh") or _sec_which("firmwalker")
    if not exe:
        return {
            "error": (
                "firmwalker not found on PATH. "
                "Download from https://github.com/craigz28/firmwalker "
                "and pass exe_path=/full/path/to/firmwalker.sh"
            )
        }

    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isdir(path):
        return {"error": f"not a directory: {path}. firmwalker needs the extracted rootfs."}

    stdout, stderr, rc = _sec_run(["bash", exe, path], timeout=120)

    if not stdout.strip():
        return {"error": "firmwalker produced no output", "stderr": stderr[:500]}

    # Parse into categories — firmwalker outputs section headers in ALL CAPS
    # followed by file paths / matches
    categories: dict[str, list[str]] = {}
    current_cat = "general"
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Section headers are lines that are all-uppercase words + punctuation
        if stripped.isupper() or (stripped.endswith(":") and stripped[:-1].isupper()):
            current_cat = stripped.rstrip(":")
            categories.setdefault(current_cat, [])
        else:
            categories.setdefault(current_cat, []).append(stripped)

    # Build summary — cap each category at 20 items
    summary: dict[str, dict] = {}
    for cat, items in categories.items():
        total = len(items)
        summary[cat] = {
            "total": total,
            "items": items[:20],
            "truncated": total > 20,
        }

    total_findings = sum(len(v) for v in categories.values())

    # Save full output
    save_path = None
    if total_findings > 30:
        try:
            ws = _get_workspace_dirs()
            if ws:
                save_path = os.path.join(ws[0], "firmwalker_output.txt")
                with open(save_path, "w", encoding="utf-8") as fh:
                    fh.write(stdout)
        except Exception:
            pass

    return {
        "path": path,
        "total_findings": total_findings,
        "categories": summary,
        "full_output_saved": save_path,
    }


def _r2_open(path: str) -> tuple[Any, str | None]:
    """Open a file in radare2 via r2pipe. Returns (r2, error_or_None)."""
    if not _HAVE_R2PIPE:
        return None, "r2pipe not installed. pip install r2pipe (also needs radare2 system binary)"
    if not _sec_which("radare2") and not _sec_which("r2"):
        return None, "radare2 binary not found. Install from https://rada.re"
    try:
        r2 = _r2pipe.open(path, flags=["-2"])  # -2 silences stderr
        return r2, None
    except Exception as e:
        return None, f"r2pipe open failed: {e}"


def tool_r2_info(args: dict) -> dict:
    """Get basic info about a binary via radare2: file type, architecture,
    OS, compiler, entry point, linked libraries, security mitigations (from r2).
    Does NOT run full analysis — fast, safe to call first."""
    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    r2, err = _r2_open(path)
    if err:
        return {"error": err}

    try:
        info = r2.cmdj("ij") or {}
        # Pull out the useful bits
        core = info.get("core", {})
        bin_ = info.get("bin", {})
        result = {
            "path": path,
            "file_type": core.get("type"),
            "size": core.get("size"),
            "arch": bin_.get("arch"),
            "bits": bin_.get("bits"),
            "os": bin_.get("os"),
            "endian": bin_.get("endian"),
            "entry_point": hex(bin_.get("baddr", 0) + bin_.get("entry", 0)) if bin_.get("entry") else None,
            "compiler": bin_.get("compiler"),
            "stripped": bin_.get("stripped"),
            "static": bin_.get("static"),
            "canary": bin_.get("canary"),
            "nx": bin_.get("nx"),
            "pic": bin_.get("pic"),
            "relocs": bin_.get("relocs"),
        }
        # Linked libraries
        libs = r2.cmdj("ilj") or []
        result["libraries"] = libs[:50]
        return result
    except Exception as e:
        return {"error": f"r2 info failed: {e}"}
    finally:
        try:
            r2.quit()
        except Exception:
            pass


def tool_r2_functions(args: dict) -> dict:
    """List functions in a binary using radare2 analysis.
    Runs 'aaa' (full analysis) then returns function list sorted by size.
    Caps at 200 functions. Pass filter_pattern to regex-match function names.
    WARNING: aaa on large binaries can take 30-120 seconds."""
    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    filter_pat = (args.get("filter_pattern") or "").strip()
    max_funcs = max(1, min(int(args.get("max_functions") or 100), 200))

    r2, err = _r2_open(path)
    if err:
        return {"error": err}

    try:
        r2.cmd("aaa")  # full analysis
        funcs = r2.cmdj("aflj") or []
    except Exception as e:
        return {"error": f"r2 analysis failed: {e}"}
    finally:
        try:
            r2.quit()
        except Exception:
            pass

    # Filter
    if filter_pat:
        try:
            rx = re.compile(filter_pat, re.IGNORECASE)
            funcs = [f for f in funcs if rx.search(f.get("name", ""))]
        except re.error as e:
            return {"error": f"bad filter_pattern: {e}"}

    # Sort by size descending — largest functions first
    funcs.sort(key=lambda f: f.get("size", 0), reverse=True)

    total = len(funcs)
    truncated = total > max_funcs
    funcs = funcs[:max_funcs]

    # Return clean subset of fields
    clean = []
    for f in funcs:
        clean.append({
            "name": f.get("name"),
            "offset": hex(f.get("offset", 0)),
            "size": f.get("size"),
            "nbbs": f.get("nbbs"),   # number of basic blocks
            "nargs": f.get("nargs"),
            "nlocals": f.get("nlocals"),
        })

    return {
        "path": path,
        "total_functions": total,
        "shown": len(clean),
        "truncated": truncated,
        "functions": clean,
    }


def tool_r2_disasm_function(args: dict) -> dict:
    """Disassemble a single named function using radare2.
    Provide function name (from r2_functions) or hex address.
    Hard-capped at 100 instructions to protect context window.
    Run r2_functions first to find function names."""
    path, err = _sec_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    func_name = (args.get("function_name") or "").strip()
    func_addr = (args.get("function_address") or "").strip()
    max_instrs = max(1, min(int(args.get("max_instructions") or 64), 100))

    if not func_name and not func_addr:
        return {"error": "provide function_name or function_address"}

    r2, err = _r2_open(path)
    if err:
        return {"error": err}

    try:
        r2.cmd("aaa")

        # Seek to function
        if func_addr:
            r2.cmd(f"s {func_addr}")
        else:
            r2.cmd(f"s sym.{func_name}")
            # Try without sym. prefix if that fails
            info = r2.cmdj("afij") or []
            if not info:
                r2.cmd(f"s {func_name}")

        # Disassemble the function (pdfj = print disasm function as json)
        disasm = r2.cmdj("pdfj") or {}
    except Exception as e:
        return {"error": f"r2 disasm failed: {e}"}
    finally:
        try:
            r2.quit()
        except Exception:
            pass

    if not disasm:
        return {"error": f"function not found: {func_name or func_addr}. Run r2_functions to list available functions."}

    ops = disasm.get("ops", [])
    total_ops = len(ops)
    truncated = total_ops > max_instrs
    ops = ops[:max_instrs]

    # Clean fields
    clean_ops = []
    for op in ops:
        clean_ops.append({
            "offset": hex(op.get("offset", 0)),
            "bytes": op.get("bytes"),
            "type": op.get("type"),
            "disasm": op.get("disasm"),
            "comment": op.get("comment"),  # r2 inline comments (xrefs, strings)
        })

    return {
        "path": path,
        "function": disasm.get("name"),
        "offset": hex(disasm.get("offset", 0)),
        "size": disasm.get("size"),
        "total_instructions": total_ops,
        "shown": len(clean_ops),
        "truncated": truncated,
        "instructions": clean_ops,
    }


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
    "web_search": {
        "description": (
            "Search the web for current information (news, weather, prices, docs, "
            "anything time-sensitive). Returns a list of {title, url, snippet}. "
            "Follow up with web_fetch on the most promising URLs to read full text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "search query, plain english"},
                "max_results": {"type": "integer", "description": "1-20, default 6"},
            },
            "required": ["query"],
        },
        "fn": tool_web_search,
    },
    "web_fetch": {
        "description": "Fetch a URL and return stripped text content. Use after web_search to read a specific page.",
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
    # ---- firmware analysis -------------------------------------------------
    "binwalk_scan": {
        "description": "Scan a binary file for embedded archives, filesystems, and known signatures. Use first on unknown firmware blobs to find offsets.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "fn": tool_binwalk_scan,
    },
    "strings_dump": {
        "description": "Extract printable ASCII strings from a binary, optional regex filter. Good for finding hardcoded creds, URLs, paths, default passwords.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "min_length": {"type": "integer", "description": "minimum run length, default 8"},
                "pattern": {"type": "string", "description": "regex filter, optional"},
                "max_results": {"type": "integer", "description": "default 500, max 5000"},
                "max_bytes": {"type": "integer", "description": "default 16MB, max 64MB"},
            },
            "required": ["path"],
        },
        "fn": tool_strings_dump,
    },
    "file_inspect": {
        "description": "Identify a file by magic bytes. For ELF binaries also reports architecture, type, entry point, interpreter, and strip status.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "fn": tool_file_inspect,
    },
    "read_bytes": {
        "description": "Read raw bytes at an offset. Returns hex + printable ASCII view. Use to inspect a header or an offset surfaced by binwalk_scan.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "byte offset, default 0"},
                "length": {"type": "integer", "description": "bytes to read, default 256, max 4096"},
            },
            "required": ["path"],
        },
        "fn": tool_read_bytes,
    },
    "find_files": {
        "description": "Recursive file search under a directory with optional glob and size cap. Triages extracted firmware roots without reading every file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory to search"},
                "pattern": {"type": "string", "description": "glob like *.cgi, default *"},
                "max_size": {"type": "integer", "description": "skip files larger than this (bytes), 0 = no cap"},
                "max_results": {"type": "integer", "description": "default 500, max 5000"},
            },
            "required": ["path"],
        },
        "fn": tool_find_files,
    },
    "extract_archive": {
        "description": "Auto-detect and extract gzip/tar/zip/xz/bzip2/squashfs into a sandbox folder next to the source. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "dest_name": {"type": "string", "description": "subfolder name, default <stem>_extracted"},
                "overwrite": {"type": "boolean", "description": "replace existing destination"},
            },
            "required": ["path"],
        },
        "fn": tool_extract_archive,
    },
    "carve_file": {
        "description": (
            "Carve a byte range out of a source file into a new file in the "
            "same directory. Use when binwalk_scan surfaces an embedded "
            "gzip/squashfs/etc inside a custom container (e.g. the 0xd00dfe "
            "TP-Link/ASUS .pkgtb wrapper) — carve the range, then run "
            "extract_archive or extract_squashfs on the carved file. "
            "length=0 carves to EOF. Requires user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "source file"},
                "offset": {"type": "integer", "description": "start byte, default 0"},
                "length": {"type": "integer", "description": "bytes to copy, 0 = to EOF"},
                "dest_name": {"type": "string", "description": "output filename, default <stem>_at_0x<offset>.bin"},
                "overwrite": {"type": "boolean", "description": "replace existing destination"},
                "max_bytes": {"type": "integer", "description": "safety cap, default 512MB, max 2GB"},
            },
            "required": ["path"],
        },
        "fn": tool_carve_file,
    },
    "extract_squashfs": {
        "description": (
            "Extract a squashfs filesystem image (hsqs/sqsh magic) into a "
            "sandbox folder next to the source. Pure-Python via PySquashfsImage "
            "— no system squashfs-tools needed. Use AFTER you've located a "
            "squashfs.img (e.g. via binwalk_scan + extract_archive on a tar). "
            "Requires user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path to a .img/.sqsh squashfs file"},
                "dest_name": {"type": "string", "description": "subfolder name, default <stem>_rootfs"},
                "overwrite": {"type": "boolean", "description": "replace existing destination"},
            },
            "required": ["path"],
        },
        "fn": tool_extract_squashfs,
    },
    "grep_files": {
        "description": (
            "Recursive regex search across a directory tree. Returns matches "
            "with file path, line number, and the matched line. Skips binaries "
            "(null-byte heuristic) and noise dirs (.git, node_modules, etc). "
            "Best for searching extracted firmware roots — find CGI handlers, "
            "system()/sprintf calls, hardcoded creds, auth strings, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "directory to search"},
                "pattern": {"type": "string", "description": "Python regex"},
                "glob": {"type": "string", "description": "filename glob filter, e.g. *.cgi or *.sh"},
                "case_insensitive": {"type": "boolean", "description": "default false"},
                "max_matches": {"type": "integer", "description": "default 200, max 2000"},
                "max_files": {"type": "integer", "description": "default 5000, max 50000"},
            },
            "required": ["path", "pattern"],
        },
        "fn": tool_grep_files,
    },
    "disasm_at": {
        "description": (
            "Disassemble N instructions at a file offset using capstone. "
            "Auto-detects arch/endianness from ELF header; pass arch explicitly "
            "for raw blobs. Supported: x86, x64, arm, thumb, arm64, mips, "
            "mips64, ppc, ppc64. Use to inspect a function near an offset "
            "surfaced by binwalk_scan or a string xref."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "byte offset into file, default 0"},
                "count": {"type": "integer", "description": "number of instructions, default 32, max 256"},
                "arch": {"type": "string", "description": "x86|x64|arm|thumb|arm64|mips|mips64|ppc|ppc64 (auto for ELF)"},
                "mode": {"type": "string", "description": "le|be (auto for ELF)"},
                "address": {"type": "integer", "description": "virtual address for display, default = offset"},
                "max_bytes": {"type": "integer", "description": "bytes to read, default 4096, max 16384"},
            },
            "required": ["path"],
        },
        "fn": tool_disasm_at,
    },
    # ---- security analysis tools -------------------------------------------
    "unblob_extract": {
        "description": (
            "Extract firmware using unblob. Handles nested/unknown containers "
            "(SquashFS inside CPIO inside ZIP etc) better than extract_archive. "
            "Use when extract_archive fails or firmware has unusual packaging. "
            "Requires: pip install unblob + system deps. Requires user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "firmware file to extract"},
                "dest_name": {"type": "string", "description": "output folder name, default <stem>_unblob"},
                "overwrite": {"type": "boolean", "description": "replace existing destination"},
            },
            "required": ["path"],
        },
        "fn": tool_unblob_extract,
    },
    "checksec": {
        "description": (
            "Check ELF binaries for exploit mitigations: NX, stack canary, "
            "RELRO (full/partial/none), PIE, ASLR, FORTIFY. "
            "Pass a single binary path or a directory to scan all ELFs. "
            "Run this on every network-facing binary before disassembling. "
            "Requires: pip install checksec.py"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ELF binary or directory containing ELFs"},
            },
            "required": ["path"],
        },
        "fn": tool_checksec,
    },
    "flawfinder": {
        "description": (
            "Static analysis of C/C++ source code for common vulnerabilities. "
            "Finds: buffer overflows, format strings, race conditions, crypto misuse, shell injection. "
            "Returns findings ranked 1-5 by severity. Saves full output to workspace. "
            "Use on extracted firmware source or CGI scripts. "
            "Requires: pip install flawfinder"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "source file or directory"},
                "min_level": {"type": "integer", "description": "minimum severity 1-5, default 1"},
                "max_findings": {"type": "integer", "description": "findings to return, default 50, max 500"},
            },
            "required": ["path"],
        },
        "fn": tool_flawfinder,
    },
    "firmwalker": {
        "description": (
            "Search an extracted firmware rootfs for sensitive material: "
            "passwords, SSH keys, SSL certs, config files, hardcoded IPs, "
            "default credentials, web server files, databases. "
            "Run on the extracted rootfs directory AFTER extraction. "
            "Saves full output to workspace. "
            "Requires firmwalker.sh — download from https://github.com/craigz28/firmwalker. "
            "Pass exe_path if not on PATH."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "extracted firmware rootfs directory"},
                "exe_path": {"type": "string", "description": "full path to firmwalker.sh if not on PATH"},
            },
            "required": ["path"],
        },
        "fn": tool_firmwalker,
    },
    "r2_info": {
        "description": (
            "Get basic binary info via radare2: arch, bits, OS, entry point, "
            "linked libraries, compiler, and security flags (canary, NX, PIC). "
            "Fast — does not run full analysis. Always call this before r2_functions. "
            "Requires: pip install r2pipe + radare2 system binary."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "ELF or PE binary"},
            },
            "required": ["path"],
        },
        "fn": tool_r2_info,
    },
    "r2_functions": {
        "description": (
            "List functions in a binary using radare2 full analysis (aaa). "
            "Returns function names, offsets, sizes, basic block count. "
            "Sorted by size — largest/most complex first. "
            "Use filter_pattern to narrow to e.g. 'auth', 'login', 'parse'. "
            "WARNING: aaa can take 30-120s on large binaries. "
            "Requires: pip install r2pipe + radare2."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "filter_pattern": {"type": "string", "description": "regex to filter function names, e.g. auth|login|parse"},
                "max_functions": {"type": "integer", "description": "default 100, max 200"},
            },
            "required": ["path"],
        },
        "fn": tool_r2_functions,
    },
    "r2_disasm_function": {
        "description": (
            "Disassemble ONE function by name or address using radare2. "
            "Hard cap: 100 instructions. Always use r2_functions first to get "
            "the correct function name. Do not call this more than 3 times per "
            "checkpoint — save checkpoint.json after every 3 functions. "
            "Requires: pip install r2pipe + radare2."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "function_name": {"type": "string", "description": "function name from r2_functions (e.g. sym.check_auth)"},
                "function_address": {"type": "string", "description": "hex address (e.g. 0x00401234) if name unknown"},
                "max_instructions": {"type": "integer", "description": "default 64, hard max 100"},
            },
            "required": ["path"],
        },
        "fn": tool_r2_disasm_function,
    },
}


_DESKTOP_TOOL_NAMES = {
    "screenshot", "describe_screen", "list_windows",
    "desktop_launch_app", "desktop_focus_window", "desktop_click",
    "desktop_type_text", "desktop_press_keys", "desktop_close_window",
}

# Tools whose results are bulky-by-design (string dumps, grep hits, disasm
# listings, signature scans). Truncated looser so the model can actually
# reason over the output instead of seeing the head + a cliff.
_ANALYSIS_TOOL_NAMES = {
    "strings_dump", "grep_files", "disasm_at",
    "binwalk_scan", "find_files", "file_inspect", "read_bytes",
}


def _active_tools() -> dict:
    """Return TOOLS filtered by current settings — desktop tools only show
    when enabled, so the model doesn't keep calling tools that will refuse."""
    s = get_settings()
    if s.get("desktop_enabled"):
        return TOOLS
    return {k: v for k, v in TOOLS.items() if k not in _DESKTOP_TOOL_NAMES}


def tools_for_llama() -> list[dict]:
    """Tool spec for llama-server's OpenAI-compatible /v1/chat/completions.
    Same shape as OpenAI function calling."""
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


# back-compat alias — some old call sites may still use the ollama name.
tools_for_ollama = tools_for_llama


def tools_for_prompt() -> str:
    lines = ["Available tools (call via <tool_call>{\"name\":\"...\",\"arguments\":{...}}</tool_call>):"]
    for name, t in _active_tools().items():
        params = t["parameters"].get("properties", {})
        sig = ", ".join(f"{k}:{v.get('type','any')}" for k, v in params.items())
        lines.append(f"- {name}({sig}) — {t['description']}")
    return "\n".join(lines)


# Common synonyms the model invents instead of the canonical tool names. Map
# them so a single typo doesn't burn an entire tool round on "unknown tool".
TOOL_ALIASES = {
    "create_file": "write_file",
    "save_file": "write_file",
    "make_file": "write_file",
    "new_file": "write_file",
    "patch_file": "edit_file",
    "modify_file": "edit_file",
    "update_file": "edit_file",
    "view_file": "read_file",
    "open_file": "read_file",
    "cat_file": "read_file",
    "cat": "read_file",
    "list_dir": "list_directory",
    "ls": "list_directory",
    "ls_dir": "list_directory",
    "dir": "list_directory",
    "rm": "delete_file",
    "remove_file": "delete_file",
    "rm_file": "delete_file",
    "powershell": "run_powershell",
    "shell": "run_powershell",
    "bash": "run_powershell",
    "cmd": "run_powershell",
    "exec": "run_powershell",
    "search_web": "web_search",
    "google": "web_search",
    "duckduckgo": "web_search",
    "fetch": "web_fetch",
    "http_get": "web_fetch",
    "screenshot_screen": "screenshot",
    "take_screenshot": "screenshot",
    "windows": "list_windows",
    "save_memory": "remember",
    "delete_memory": "forget",
}


def _resolve_tool_name(name: str) -> str:
    if not name:
        return name
    if name in TOOLS:
        return name
    lc = name.lower()
    if lc in TOOLS:
        return lc
    if lc in TOOL_ALIASES:
        return TOOL_ALIASES[lc]
    return name


def invoke_tool(name: str, args: dict) -> dict:
    canon = _resolve_tool_name(name)
    t = TOOLS.get(canon)
    if not t:
        # Surface the available names so a repair-retry round can fix a typo.
        return {"error": f"unknown tool: {name}", "available": sorted(TOOLS.keys())}
    try:
        return t["fn"](args or {})
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


# ---- tool-call parsing (fallback for models w/o native tools) -------------
# Self-healing — accepts lightly broken JSON from the model, since local
# models routinely emit trailing prose, unbalanced braces, or code fences.

TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>", re.IGNORECASE)
TOOL_CALL_FENCE_RE = re.compile(r"```tool_call\s*\n([\s\S]*?)\n```", re.IGNORECASE)
# Extra dialects from non-OpenAI fine-tunes. Tier-2 fallback only — native
# tool_calls on the streamed delta still wins. Patterns are anchored on
# dialect-specific markers so they cannot collide with each other.
#   <call:NAME>{args}</call:NAME>      seen on BugTraceAI and similar tunes
#   <|python_tag|>{...}<|eom_id|>      llama-3.1 / 3.2 native format
#   [TOOL_CALLS][{...}, ...]           mistral native format
TOOL_CALL_NAMED_RE = re.compile(
    # Accept either </call> or </call:NAME> for the closer. BugTraceAI and
    # other gemma-derived tunes drop the name in the close tag.
    r"<call:([a-zA-Z0-9_\-]+)>\s*(\{[\s\S]*?\})\s*</call(?::\1)?>",
    re.IGNORECASE)
TOOL_CALL_PYTAG_RE = re.compile(
    r"<\|python_tag\|>\s*(\{[\s\S]*?\})\s*(?:<\|eom_id\|>|<\|eot_id\|>|$)",
    re.IGNORECASE)
TOOL_CALL_MISTRAL_RE = re.compile(
    r"\[TOOL_CALLS\]\s*(\[[\s\S]*?\])", re.IGNORECASE)
# XML-tag dialect: Hermes-3 / GLM-4 / some Qwen3 finetunes emit
#   <tool_call><function=NAME><parameter=KEY>VAL</parameter>...</function></tool_call>
# Tolerant: closing </function> and/or </tool_call> may be missing if the
# model truncates. We anchor on <tool_call> + <function=NAME> and then walk
# parameters until we hit a closer or the next tool_call/end-of-string.
TOOL_CALL_XMLTAG_RE = re.compile(
    r"<tool_call>\s*<function=([a-zA-Z0-9_\-\.]+)>"
    r"([\s\S]*?)"
    r"(?:</function>\s*</tool_call>|</tool_call>|(?=<tool_call>)|$)",
    re.IGNORECASE)
TOOL_PARAM_XMLTAG_RE = re.compile(
    r"<parameter=([a-zA-Z0-9_\-\.]+)>\s*([\s\S]*?)\s*</parameter>",
    re.IGNORECASE)
# Heuristic: model emitted tool-call syntax but no parser matched it.
# When this fires with zero parsed calls, the reply is almost certainly
# a hallucination from a chat-template mismatch.
TOOL_SYNTAX_HINT_RE = re.compile(
    r"<call:[a-zA-Z]|<\|python_tag\|>|\[TOOL_CALLS\]|<tool_call>|```tool_call",
    re.IGNORECASE)


def _js_to_json(s: str) -> str:
    """Best-effort: turn a JavaScript-style object literal into valid JSON.
    Handles two failure modes seen on local fine-tunes:
      - unquoted identifier keys:  {path: "..."}      → {"path": "..."}
      - invalid backslash escapes: "C:\\Users\\..."   → "C:\\\\Users\\\\..."
        (Windows paths emitted as raw \\ in JSON strings)
    Conservative: only touches obvious problems, leaves valid JSON alone."""
    # quote unquoted identifier keys appearing after { or ,
    s = re.sub(r'([{,]\s*)([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', s)
    # double any backslash that isn't part of a valid JSON escape sequence.
    # Valid: \" \\ \/ \b \f \n \r \t \uXXXX. Anything else gets escaped.
    s = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)
    return s


def repair_tool_args(raw) -> dict:
    """Coerce a tool-call arguments blob to a dict. Accepts dict, JSON string,
    or broken JSON (trailing prose, missing close brace, code fences,
    JavaScript-style unquoted keys, raw Windows backslashes). Returns {}
    on total failure — the agentic loop then feeds that back as an error
    and the model retries with a clean call."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    s = str(raw).strip()
    if not s:
        return {}
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?\s*```\s*$", "", s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    # try JS-style → JSON repair (unquoted keys + Windows-path backslashes)
    try:
        v = json.loads(_js_to_json(s))
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    # slice to outermost braces — catches leading/trailing prose
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        sliced = s[start:end + 1]
        try:
            v = json.loads(sliced)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
        try:
            v = json.loads(_js_to_json(sliced))
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
    # balance missing closing braces
    if start >= 0:
        tail = s[start:]
        opens = tail.count("{")
        closes = tail.count("}")
        if opens > closes:
            patched = tail + ("}" * (opens - closes))
            try:
                v = json.loads(patched)
                return v if isinstance(v, dict) else {"value": v}
            except Exception:
                pass
            try:
                v = json.loads(_js_to_json(patched))
                return v if isinstance(v, dict) else {"value": v}
            except Exception:
                pass
    return {}


def extract_tool_calls(text: str) -> list[dict]:
    """Parse tool calls from free text. Tries multiple dialects so models
    that weren't fine-tuned on the OpenAI/llama-server schema can still
    drive the agent loop. All paths normalize to {name, arguments}.
    Native tool_calls on the streamed delta still take precedence; this
    only runs when that field came back empty."""
    calls: list[dict] = []
    seen = set()

    def _add(parsed) -> None:
        if not isinstance(parsed, dict):
            return
        if not (parsed.get("name") or parsed.get("tool")):
            return
        # de-dup identical consecutive calls (some models double-emit)
        key = json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        if key in seen:
            return
        seen.add(key)
        calls.append(parsed)

    # 1. <tool_call>{...}</tool_call> and ```tool_call fences  (hermes/qwen)
    for m in list(TOOL_CALL_RE.finditer(text)) + list(TOOL_CALL_FENCE_RE.finditer(text)):
        _add(repair_tool_args(m.group(1)))

    # 2. <call:NAME>{args}</call:NAME>  (BugTraceAI dialect and similar)
    for m in TOOL_CALL_NAMED_RE.finditer(text):
        args = repair_tool_args(m.group(2))
        _add({"name": m.group(1), "arguments": args})

    # 3. <|python_tag|>{...}<|eom_id|>  (llama-3.1 / 3.2 native)
    for m in TOOL_CALL_PYTAG_RE.finditer(text):
        parsed = repair_tool_args(m.group(1))
        # llama uses "parameters" instead of "arguments" — normalize
        if isinstance(parsed, dict) and "parameters" in parsed and "arguments" not in parsed:
            parsed["arguments"] = parsed.pop("parameters")
        _add(parsed)

    # 4. [TOOL_CALLS][{...}, ...]  (mistral native)
    for m in TOOL_CALL_MISTRAL_RE.finditer(text):
        try:
            arr = json.loads(m.group(1))
        except Exception:
            continue
        if isinstance(arr, list):
            for item in arr:
                if isinstance(item, dict):
                    _add(item)

    # 5. <tool_call><function=NAME><parameter=K>V</parameter>...</function></tool_call>
    #    (Hermes-3 / GLM-4 / some Qwen3 finetunes — XML-tag dialect)
    for m in TOOL_CALL_XMLTAG_RE.finditer(text):
        name = m.group(1)
        body = m.group(2) or ""
        args: dict = {}
        for pm in TOOL_PARAM_XMLTAG_RE.finditer(body):
            k = pm.group(1)
            v = (pm.group(2) or "").strip()
            # try to coerce numeric / bool / json-ish values, fall back to str
            if v.lower() in ("true", "false"):
                args[k] = (v.lower() == "true")
            else:
                try:
                    args[k] = int(v)
                except Exception:
                    try:
                        args[k] = float(v)
                    except Exception:
                        if (v.startswith("{") and v.endswith("}")) or \
                           (v.startswith("[") and v.endswith("]")):
                            parsed_v = repair_tool_args(v)
                            args[k] = parsed_v if parsed_v is not None else v
                        else:
                            args[k] = v
        _add({"name": name, "arguments": args})

    return calls


# ---- llama-server helpers --------------------------------------------------
# Everything below talks to llama.cpp's `llama-server` over its OpenAI-
# compatible /v1 endpoints. No Ollama. If you want to use Ollama you're in
# the wrong file.

def llama_get(path: str, base: str | None = None) -> dict:
    with urllib.request.urlopen(f"{base or LLAMA}{path}", timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def llama_post(path: str, payload: dict, base: str | None = None, timeout: float = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base or LLAMA}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Cache /props readings — the value never changes for a running server,
# and we'd rather not hit it every turn just to read n_ctx_slot.
_LLAMA_PROPS_CTX_CACHE: tuple[str, int] | None = None  # (base, ctx)

def _llama_props_ctx() -> int | None:
    """Return the llama-server's actual slot context size, or None if we
    can't reach /props. Cached per LLAMA base so we don't re-poll every turn."""
    global _LLAMA_PROPS_CTX_CACHE
    base = LLAMA
    if _LLAMA_PROPS_CTX_CACHE and _LLAMA_PROPS_CTX_CACHE[0] == base:
        return _LLAMA_PROPS_CTX_CACHE[1]
    try:
        with urllib.request.urlopen(f"{base}/props", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # llama-server exposes the slot ctx under default_generation_settings
        # as `n_ctx`, and at the top level as `n_ctx`. Try a few keys.
        ctx = (
            (data.get("default_generation_settings") or {}).get("n_ctx")
            or data.get("n_ctx")
            or (data.get("model_meta") or {}).get("n_ctx")
        )
        if isinstance(ctx, int) and ctx > 0:
            _LLAMA_PROPS_CTX_CACHE = (base, ctx)
            return ctx
    except Exception:
        pass
    return None


def _llama_props_ctx_invalidate() -> None:
    """Call after stopping/restarting llama-server so the next turn re-polls."""
    global _LLAMA_PROPS_CTX_CACHE, _TOOLS_OVERHEAD_CACHE
    _LLAMA_PROPS_CTX_CACHE = None
    _TOOLS_OVERHEAD_CACHE = ("", 0)


# Cache for the tools-spec token count. Re-tokenized only when the rendered
# tools JSON changes (e.g. user toggles desktop tools, we add a new tool, or
# llama-server is restarted with a different tokenizer).
_TOOLS_OVERHEAD_CACHE: tuple[str, int] = ("", 0)


def _llama_tokenize(text: str) -> int | None:
    """Ask llama-server to tokenize `text` and return the token count.
    Returns None if the server is unreachable or the response is malformed."""
    try:
        req = urllib.request.Request(
            f"{LLAMA}/tokenize",
            data=json.dumps({"content": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        toks = data.get("tokens") or []
        return len(toks) if isinstance(toks, list) else None
    except Exception:
        return None


def _tools_spec_overhead_tokens(tools_json: str) -> int:
    """Estimate the token overhead llama-server's Jinja chat template adds
    when it inlines the tools array into the system message. Uses /tokenize
    for an exact count; falls back to a JSON-density approximation if the
    server is unreachable. +512 covers template boilerplate (the prose the
    chat template wraps tool defs in — varies per model family)."""
    global _TOOLS_OVERHEAD_CACHE
    if _TOOLS_OVERHEAD_CACHE[0] == tools_json:
        return _TOOLS_OVERHEAD_CACHE[1]
    exact = _llama_tokenize(tools_json)
    if exact is not None:
        # +1024 covers Jinja boilerplate (tool-use prose, role tags, format
        # instructions) which varies by template. Conservative on purpose —
        # better to have a tiny bit of unused ctx than overflow the slot.
        overhead = exact + 1024
    else:
        # JSON tokenizes denser than English (~2 chars/tok). Plus boilerplate.
        overhead = int(len(tools_json) / 2.0) + 1024
    _TOOLS_OVERHEAD_CACHE = (tools_json, overhead)
    return overhead


# back-compat shims — old call sites; rewritten to go through llama-server.
def ollama_get(path: str) -> dict:  # type: ignore[no-redef]
    return llama_get(path)


def ollama_post(path: str, payload: dict) -> dict:  # type: ignore[no-redef]
    return llama_post(path, payload)


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
    """Return heuristic defaults for the UI. llama-server's context window,
    GPU layers, batch size and thread count are server-launch flags, not
    per-request params — so these numbers are informational only; the user
    tunes them on the llama-server command line."""
    native_ctx = 8192
    try:
        info = llama_get("/v1/models")
        # llama-server exposes one model; no size/quant detail in /v1/models.
        # If the user hit a custom /props endpoint we'd use it, but keep simple.
        _ = info
    except Exception:
        pass
    return {
        "model": model or "local",
        "size_b": 0.0,
        "quant": "",
        "est_weights_gb": 0.0,
        "native_ctx": native_ctx,
        "recommended": {
            "num_ctx": native_ctx,
            "num_gpu": 99,
            "num_batch": 512,
            "num_thread": 0,
            "num_predict": -1,
            "temperature": 0.7,
            "top_p": 0.9,
            "keep_alive": "30m",
        },
    }


def llama_post_stream(path: str, payload: dict, base: str | None = None):
    """POST and return the raw response object — caller iterates over SSE lines."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base or LLAMA}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=None)


# back-compat: old name some callers may still use
ollama_post_stream = llama_post_stream


def describe_image(b64: str, hint: str = "") -> str:
    """Hand a base64 image to a vision-capable llama-server via
    /v1/chat/completions with image_url content. llama-server must be started
    with --mmproj pointing at the vision projector (or VISION_LLAMA env var
    pointed at a separate vision server).
    The main chat model then only sees the text description — so context
    stays small even for multi-image turns."""
    if b64.startswith("data:"):
        data_url = b64
        comma = b64.find(",")
        if comma >= 0:
            b64_clean = b64[comma + 1:]
        else:
            b64_clean = b64
    else:
        b64_clean = b64
        data_url = f"data:image/png;base64,{b64}"
    _ = b64_clean  # kept for future (raw b64 endpoints)
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
        "model": "vision",
        "stream": False,
        "temperature": 0.1,
        "max_tokens": 768,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }],
    }
    try:
        out = llama_post("/v1/chat/completions", payload, base=VISION_LLAMA, timeout=180)
        choices = out.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            text = (msg.get("content") or "").strip()
            if text:
                return text
        return "[image attached — empty description]"
    except Exception as e:
        return f"[image attached — vision llama-server at {VISION_LLAMA} failed: {e}]"


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

workspace refusal — do not loop on denied writes:
- if write_file, edit_file, or delete_file returns
  `"path outside workspace. Add folder in Workspace panel."`, STOP immediately.
  do not retry with a different path, do not fall back to PowerShell, do not
  try to create the file under a plausible-looking root. the user has not
  configured a workspace that covers that path — only they can fix it.
- respond in the bubble: name the path you tried, and tell the user to add
  the parent folder in Settings -> Workspace folders, then ask them to retry.
- the same rule applies to any tool that returns a sandbox / allowlist refusal
  (e.g. `desktop_launch_app` refusing an app, `run_powershell` refusing a
  destructive command without approval). one clear explanation, then stop.

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



def llama_options(settings: dict) -> dict:
    """Map our settings to llama-server /v1/chat/completions top-level params.
    Unlike Ollama, llama-server treats ctx size / GPU layers / batch / threads
    as server-launch flags — not per-request. We only ship per-request
    sampling/predict params here."""
    opt: dict = {
        "temperature": float(settings.get("temperature") or 0.7),
        "top_p": float(settings.get("top_p") or 0.9),
    }
    # Pass through additional sampler params if set. These prevent the model
    # from looping or producing low-diversity output.
    for key, default in (("top_k", 40), ("min_p", 0.05),
                         ("repeat_penalty", 1.1),
                         ("presence_penalty", 0.0),
                         ("frequency_penalty", 0.0)):
        v = settings.get(key)
        if v is not None and v != "":
            try:
                opt[key] = float(v)
            except (ValueError, TypeError):
                pass
    np = int(settings.get("num_predict") or -1)
    if np > 0:
        opt["max_tokens"] = np
    return opt


# back-compat alias
ollama_options = llama_options




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
    cancel_ev = _register_cancel(chat_id)
    try:
        try:
            max_tool_rounds = int(settings.get("max_tool_rounds") or 60)
        except Exception:
            max_tool_rounds = 60
        max_tool_rounds = max(1, min(max_tool_rounds, 500))
        rounds = 0
        empty_retry_done = False
        conversation = list(messages)
        # Anything appended past this index is the model's working memory
        # for THIS turn — intermediate assistant messages with tool_calls,
        # tool results, the empty-retry nudge. We hand it back to the caller
        # via `final["_appended_intermediate"]` so it can be persisted and
        # the next turn replays it (instead of the model waking up amnesic).
        _start_len = len(messages)

        while True:
            if cancel_ev.is_set():
                emit({"type": "notice", "note": "stopped by user"})
                return None
            # Use the llama-server's *actual* slot context if we can read it;
            # falling back to settings only if /props isn't reachable. Without
            # this, a settings default of 8K would make the trimmer chop a
            # conversation the server happily holds at 32K — and tool results
            # the model discovered earlier in the turn vanish from history.
            ctx_limit = _llama_props_ctx() or int(settings.get("num_ctx") or 32768)
            # Reserve ~25% of ctx for the response + thinking so the model
            # always has headroom to answer. The frontend gets a single event
            # with the elided count so it can render a pill — no spammy toast.
            reserve = max(int(ctx_limit * 0.25), 1024)
            # Tool spec overhead: llama-server's Jinja template inlines the
            # FULL tools array into the system message server-side. That can
            # be 6-10K tokens for our ~21 tools — invisible to us until the
            # server rejects with "exceeds context size". Use /tokenize for
            # an exact count (cached per spec) so the trimmer's budget reflects
            # what actually gets sent.
            tools_overhead = 0
            if use_tools:
                try:
                    _tools_json = json.dumps(tools_for_llama(), ensure_ascii=False)
                    tools_overhead = _tools_spec_overhead_tokens(_tools_json)
                except Exception:
                    tools_overhead = 4096  # conservative fallback
            # Floor the messages budget at 2048 tokens — even if tools overhead
            # is huge, we still need room for at least the system + last user.
            effective_reserve = min(reserve + tools_overhead, ctx_limit - 2048)
            trimmed = truncate_messages(conversation, ctx_limit, reserve=effective_reserve)
            dropped = max(0, len(conversation) - len(trimmed))
            if dropped > 0:
                emit({"type": "context_trimmed", "dropped": dropped, "total": len(conversation)})

            payload = {
                "model": model or "local",
                "messages": _sanitize_messages_for_openai(trimmed),
                "stream": True,
                **llama_options(settings),
            }
            # Qwen3 / reasoner-family chat_template_kwargs. llama-server forwards
            # these into the Jinja chat template: lets us toggle thinking mode
            # per-request and cap thinking tokens so the model can't spin forever.
            tpl_kwargs: dict = {}
            enable_thinking = settings.get("enable_thinking")
            if enable_thinking is not None:
                tpl_kwargs["enable_thinking"] = bool(enable_thinking)
            tb = settings.get("thinking_budget")
            try:
                tb_int = int(tb) if tb is not None else 2048
            except Exception:
                tb_int = 2048
            if tb_int >= 0:
                tpl_kwargs["thinking_budget"] = tb_int
            if tpl_kwargs:
                payload["chat_template_kwargs"] = tpl_kwargs
            if use_tools:
                payload["tools"] = tools_for_llama()
                payload["tool_choice"] = "auto"

            try:
                resp = llama_post_stream("/v1/chat/completions", payload)
            except Exception as e:
                emit({"type": "error",
                      "error": f"llama-server unreachable at {LLAMA}: {e}. "
                               f"Start it with: llama-server -m <model.gguf> --host 127.0.0.1 --port 8080 --jinja"})
                return None
            _set_cancel_resp(chat_id, resp)

            content_buf: list[str] = []
            tool_calls_by_index: dict[int, dict] = {}
            last_stats: dict = {}
            # llama-server with --reasoning-format deepseek splits thinking into
            # its own `reasoning_content` delta. The frontend's splitThinking()
            # only recognizes inline <think>…</think>, so we re-wrap here and
            # forward as one continuous stream.
            reasoning_open = False

            try:
                for raw in resp:
                    if cancel_ev.is_set():
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    # llama-server may emit a bare timings object after [DONE]
                    if "timings" in obj and "choices" not in obj:
                        t = obj["timings"]
                        last_stats = {
                            "eval_count": t.get("predicted_n"),
                            "eval_duration": int((t.get("predicted_ms") or 0) * 1e6),
                            "prompt_eval_count": t.get("prompt_n"),
                        }
                        continue
                    if "usage" in obj and "choices" not in obj:
                        u = obj["usage"]
                        last_stats.setdefault("eval_count", u.get("completion_tokens"))
                        last_stats.setdefault("prompt_eval_count", u.get("prompt_tokens"))
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    ch = choices[0]
                    delta = ch.get("delta") or ch.get("message") or {}
                    # reasoning first — wrap as <think>…</think> for the UI
                    rpiece = delta.get("reasoning_content") or delta.get("reasoning") or ""
                    if rpiece:
                        if not reasoning_open:
                            content_buf.append("<think>")
                            emit({"type": "delta", "content": "<think>"})
                            reasoning_open = True
                        content_buf.append(rpiece)
                        emit({"type": "delta", "content": rpiece})
                    piece = delta.get("content") or ""
                    if piece:
                        if reasoning_open:
                            content_buf.append("</think>")
                            emit({"type": "delta", "content": "</think>"})
                            reasoning_open = False
                        content_buf.append(piece)
                        emit({"type": "delta", "content": piece})
                    # tool-call deltas come as partial fragments — `arguments`
                    # is a string that concatenates into a JSON blob across
                    # many chunks.
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        slot = tool_calls_by_index.setdefault(idx, {
                            "id": tc.get("id") or f"call_{idx}",
                            "name": "",
                            "arguments": "",
                        })
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        args_chunk = fn.get("arguments")
                        if args_chunk:
                            if isinstance(args_chunk, dict):
                                # non-streaming mode occasionally returns a dict directly
                                slot["arguments"] = json.dumps(args_chunk, ensure_ascii=False)
                            else:
                                slot["arguments"] += args_chunk
                    if obj.get("usage"):
                        u = obj["usage"]
                        last_stats.setdefault("eval_count", u.get("completion_tokens"))
                        last_stats.setdefault("prompt_eval_count", u.get("prompt_tokens"))
                # if the stream ended while still inside reasoning (no answer
                # tokens came), close the tag so the UI can render it cleanly.
                if reasoning_open:
                    content_buf.append("</think>")
                    emit({"type": "delta", "content": "</think>"})
                    reasoning_open = False
                if last_stats.get("eval_count") is not None:
                    emit({"type": "stats", **last_stats})
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                _set_cancel_resp(chat_id, None)

            if cancel_ev.is_set():
                emit({"type": "notice", "note": "stopped by user"})
                return None

            full_text = "".join(content_buf)

            # assemble native tool calls
            parsed_calls: list[dict] = []
            for idx in sorted(tool_calls_by_index.keys()):
                slot = tool_calls_by_index[idx]
                if not slot.get("name"):
                    continue
                args = repair_tool_args(slot.get("arguments", ""))
                parsed_calls.append({"id": slot["id"], "name": slot["name"], "arguments": args})

            # fallback: parse tool calls emitted in content (hermes/qwen/llama/mistral/named)
            if not parsed_calls and use_tools:
                for c in extract_tool_calls(full_text):
                    name = c.get("name") or c.get("tool")
                    args = c.get("arguments") or c.get("args") or {}
                    if not isinstance(args, dict):
                        args = repair_tool_args(args)
                    if name:
                        parsed_calls.append({
                            "id": f"call_{len(parsed_calls)}",
                            "name": name,
                            "arguments": args,
                        })
                # diagnostic — model emitted tool-call syntax but nothing
                # parsed. Almost always a chat-template / dialect mismatch,
                # which means the rest of the reply is hallucinated narration
                # of a tool that never ran. Surface that to the UI.
                if not parsed_calls and TOOL_SYNTAX_HINT_RE.search(full_text or ""):
                    print(
                        "[tool] WARNING: model emitted tool-call syntax but "
                        "no dialect matched - likely chat-template mismatch",
                        flush=True,
                    )
                    emit({
                        "type": "tool_dialect_warning",
                        "message": (
                            "Model produced tool-call syntax that this build "
                            "couldn't parse. The reply may be a hallucination "
                            "- no tool actually ran."
                        ),
                    })

            assistant_msg = {"role": "assistant", "content": full_text}
            if parsed_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            "arguments": json.dumps(c["arguments"], ensure_ascii=False),
                        },
                    }
                    for c in parsed_calls
                ]

            if not parsed_calls or rounds >= max_tool_rounds:
                if not (assistant_msg.get("content") or "").strip() and rounds > 0 and not empty_retry_done:
                    empty_retry_done = True
                    conversation.append({
                        "role": "system",
                        "content": "Continue. Complete the user's request using the tool results you just received. Do not ask for permission.",
                    })
                    continue
                assistant_msg["_stats"] = last_stats
                # Intermediate working memory for this turn: every tool result
                # and intermediate-assistant-with-tool-calls the loop appended.
                # The caller persists these so the next user turn replays the
                # full agentic context, not just the final bubble.
                assistant_msg["_appended_intermediate"] = list(conversation[_start_len:])
                emit({"type": "final", "message": assistant_msg})
                return assistant_msg

            conversation.append(assistant_msg)
            for call in parsed_calls:
                name = call.get("name") or ""
                args = call.get("arguments") or {}
                emit({"type": "tool_start", "name": name, "arguments": args})
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
                # analysis tools produce large structured output (string lists,
                # grep hit lists, disasm listings). Cap looser so the model can
                # actually reason over the output. Chatty tools stay tight.
                _trunc = 16000 if name in _ANALYSIS_TOOL_NAMES else 4000
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)[:_trunc],
                })
            rounds += 1
    finally:
        _chat_emitters.pop(chat_id, None)
        _unregister_cancel(chat_id)


# Rewrite prior assistant <think>…</think> blocks as plain-text scratchpad
# notes so they survive chat-template stripping (Qwen3, DeepSeek, etc. all
# discard prior reasoning by default — the model loses its own context).
_PRIOR_THINK_RE = re.compile(r"<think>([\s\S]*?)</think>", re.IGNORECASE)

def _preserve_prior_thinking(text: str) -> str:
    if not text or "<think>" not in text.lower():
        return text
    def _rewrite(m: "re.Match[str]") -> str:
        body = (m.group(1) or "").strip()
        if not body:
            return ""
        # plain text the chat template can't recognize as reasoning, but the
        # model can still read. compact-ish to keep ctx in check.
        return f"[scratchpad-from-earlier-turn]\n{body}\n[/scratchpad-from-earlier-turn]"
    return _PRIOR_THINK_RE.sub(_rewrite, text)


def _sanitize_messages_for_openai(msgs: list[dict]) -> list[dict]:
    """llama-server's OpenAI endpoint is stricter about message shape than
    Ollama. Strip local-only fields (`t`, `_stats`), coerce tool messages to
    the `{role:'tool', tool_call_id, content}` shape, and ensure assistant
    tool_calls have a string `arguments` field."""
    try:
        preserve_thinking = bool(get_settings().get("preserve_prior_thinking", True))
    except Exception:
        preserve_thinking = True
    out = []
    # the LAST assistant message is "current" reasoning being authored — we
    # only rewrite think blocks for messages strictly older than the latest.
    last_assistant_idx = -1
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant":
            last_assistant_idx = i
    for i, m in enumerate(msgs):
        role = m.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue
        clean: dict = {"role": role}
        content = m.get("content", "")
        if isinstance(content, list):
            clean["content"] = content
        else:
            text_content = content or ""
            if (preserve_thinking and role == "assistant"
                    and i < last_assistant_idx and isinstance(text_content, str)):
                text_content = _preserve_prior_thinking(text_content)
            clean["content"] = text_content
        if role == "tool":
            clean["tool_call_id"] = m.get("tool_call_id") or m.get("name") or "tool"
            if m.get("name"):
                clean["name"] = m["name"]
        if role == "assistant" and m.get("tool_calls"):
            tcs = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, dict):
                    args = json.dumps(args, ensure_ascii=False)
                tcs.append({
                    "id": tc.get("id") or f"call_{len(tcs)}",
                    "type": "function",
                    "function": {"name": fn.get("name", ""), "arguments": args or "{}"},
                })
            if tcs:
                clean["tool_calls"] = tcs
        out.append(clean)
    return out


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
            return self._send_json(200, {
                "ok": True,
                "llama": LLAMA,
                "vision_llama": VISION_LLAMA,
                "llama_up": llama_ping(timeout=1.0),
                # legacy alias so older frontend builds don't break
                "ollama": LLAMA,
            })
        if p == "/api/models":
            # List .gguf files under settings.models_dir. Each entry includes a
            # `loaded` flag so the UI can highlight the active model.
            s = get_settings()
            mdir = (s.get("models_dir") or "").strip()
            files = scan_gguf_dir(mdir)
            loaded = _llama.loaded_model() or s.get("model_path") or ""
            for f in files:
                f["loaded"] = (f["path"] == loaded)
            return self._send_json(200, {
                "models_dir": mdir,
                "loaded_model": loaded,
                "llama_running": _llama.is_running() or llama_ping(timeout=0.5),
                "models": files,
            })
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
        if p == "/api/workspace":
            folders = body.get("folders") or []
            folders = [normalize_path(f) for f in folders if isinstance(f, str) and f.strip()]
            save_json(WORKSPACE_FILE, {"folders": folders})
            broadcast_event({"type": "workspace:update"})
            return self._send_json(200, {"folders": folders})
        if p == "/api/browse-folder":
            # native OS folder picker, only on the machine running the bridge.
            title = (body.get("title") or "Pick a folder").strip()
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askdirectory(title=title)
                root.destroy()
                return self._send_json(200, {"path": path or ""})
            except Exception as e:
                return self._send_json(200, {"path": "", "error": str(e)})
        if p == "/api/models/scan-dir":
            # Save settings.models_dir and immediately return the scan.
            new_dir = (body.get("path") or "").strip()
            if new_dir and not Path(new_dir).is_dir():
                return self._send_json(400, {"error": f"not a directory: {new_dir}"})
            s = get_settings()
            s["models_dir"] = new_dir
            save_json(SETTINGS_FILE, s)
            files = scan_gguf_dir(new_dir)
            loaded = _llama.loaded_model() or s.get("model_path") or ""
            for f in files:
                f["loaded"] = (f["path"] == loaded)
            broadcast_event({"type": "models:update"})
            return self._send_json(200, {
                "models_dir": new_dir,
                "loaded_model": loaded,
                "models": files,
            })
        if p == "/api/models/load":
            # Switch the active model. Kills current llama-server, spawns new.
            target = (body.get("path") or "").strip()
            if not target:
                return self._send_json(400, {"error": "path required"})
            if not Path(target).exists():
                return self._send_json(400, {"error": f"file not found: {target}"})
            res = _llama.start(target)
            if res.get("ok"):
                s = get_settings()
                s["model_path"] = target
                # Use the basename without extension as the model id the chat
                # API sends to llama-server. (llama-server accepts any id but
                # logs/UI look nicer with a clean name.)
                s["model"] = Path(target).stem
                save_json(SETTINGS_FILE, s)
                broadcast_event({"type": "models:update", "loaded_model": target})
            return self._send_json(200 if res.get("ok") else 500, res)
        if p == "/api/models/stop":
            _llama.stop()
            broadcast_event({"type": "models:update", "loaded_model": ""})
            return self._send_json(200, {"ok": True})
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
        if p == "/api/cancel":
            cid = (body.get("chat_id") or "").strip()
            if not cid:
                return self._send_json(400, {"error": "chat_id required"})
            ok = cancel_chat(cid)
            return self._send_json(200, {"ok": ok, "chat_id": cid})
        if p == "/api/prewarm":
            # llama-server keeps the model resident after startup, so "prewarm"
            # is just a 1-token ping that forces any lazy mmap to fault in.
            model = (body.get("model") or "local").strip() or "local"
            try:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                    "stream": False,
                }
                llama_post("/v1/chat/completions", payload, timeout=120)
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
            # Pop the whole prior agentic tail — final assistant + every
            # intermediate assistant and tool message — back to the last user
            # turn. With server-side history, "regenerate" means re-run from
            # exactly that user message, so the loop's internal context resets.
            while chat["messages"] and chat["messages"][-1].get("role") in ("assistant", "tool"):
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
        # Replay the FULL stored history including intermediate-assistant
        # turns (with tool_calls) and tool-result messages from prior agentic
        # loops. This is the anti-amnesia change — the model picks up its
        # working memory from where the last turn left off, not from a sanitised
        # bubble-only transcript.
        for m in chat["messages"]:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            out: dict = {"role": role, "content": m.get("content", "") or ""}
            if role == "assistant" and m.get("tool_calls"):
                out["tool_calls"] = m["tool_calls"]
            if role == "tool":
                if m.get("tool_call_id"):
                    out["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    out["name"] = m["name"]
            msgs.append(out)

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
                # Persist the agentic loop's working memory (tool calls + tool
                # results + intermediate assistant messages) BEFORE the final
                # bubble. Marked _internal=true on assistants so the renderer
                # skips them — they stay in the JSON purely so the model can
                # replay them on the next user turn.
                appended = final.pop("_appended_intermediate", None) or []
                now_t = int(time.time())
                for im in appended:
                    role = im.get("role")
                    if role not in ("assistant", "tool"):
                        # the empty-retry "system" nudge isn't worth persisting
                        continue
                    persisted = {
                        "role": role,
                        "content": im.get("content", "") or "",
                        "t": now_t,
                    }
                    if role == "assistant":
                        persisted["_internal"] = True
                        if im.get("tool_calls"):
                            persisted["tool_calls"] = im["tool_calls"]
                    elif role == "tool":
                        if im.get("tool_call_id"):
                            persisted["tool_call_id"] = im["tool_call_id"]
                        if im.get("name"):
                            persisted["name"] = im["name"]
                    chat["messages"].append(persisted)

                msg = {
                    "role": "assistant",
                    "content": final.get("content", ""),
                    "t": now_t,
                }
                stats = final.get("_stats") or {}
                if stats.get("eval_count") is not None:
                    msg["tokens"] = stats["eval_count"]
                if stats.get("prompt_eval_count") is not None:
                    msg["prompt_tokens"] = stats["prompt_eval_count"]
                chat["messages"].append(msg)
                chat["updated"] = now_t
                # Retention cap: keep the chat under CHAT_HISTORY_MAX messages.
                # Anchors (system + first user) are always kept; oldest beyond
                # that get dropped. Trimmer at request time gives the model a
                # tight tail; this cap keeps the JSON file from ballooning.
                _enforce_chat_retention(chat)
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


# ---- llama-server management -----------------------------------------------
# Bridge owns the llama-server subprocess so the user can swap models from the
# UI without restarting anything. Set settings.model_path to the .gguf and we
# (re)spawn llama-server with the unsloth-tuned flag set.

def find_llama_bin() -> str:
    """Locate llama-server.exe — settings override > env > PATH > known dirs."""
    s = get_settings()
    explicit = (s.get("llama_bin") or "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    env = (os.environ.get("ACCURETTA_LLAMA_BIN") or "").strip()
    if env and Path(env).exists():
        return env
    found = shutil.which("llama-server.exe") or shutil.which("llama-server")
    if found:
        return found
    home = Path.home()
    candidates = [
        home / ".unsloth/llama.cpp/build/bin/Release/llama-server.exe",
        home / ".unsloth/llama.cpp/build/bin/llama-server.exe",
        home / ".docker/bin/inference/llama-server.exe",
        home / "llama.cpp/build/bin/Release/llama-server.exe",
        home / "llama.cpp/llama-server.exe",
        Path("C:/llama.cpp/build/bin/Release/llama-server.exe"),
        Path("C:/llama.cpp/llama-server.exe"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return ""


def _parse_llama_port() -> int:
    m = re.search(r":(\d+)", LLAMA)
    return int(m.group(1)) if m else 8080


def scan_gguf_dir(root: str) -> list[dict]:
    """List .gguf files under root (recursive). Returns [{name,path,size,modified_at}]."""
    if not root:
        return []
    rp = Path(root)
    if not rp.exists() or not rp.is_dir():
        return []
    out = []
    for p in rp.rglob("*.gguf"):
        try:
            st = p.stat()
        except Exception:
            continue
        out.append({
            "name": p.name,
            "path": str(p.resolve()),
            "size": st.st_size,
            "modified_at": int(st.st_mtime),
        })
    out.sort(key=lambda m: m["name"].lower())
    return out


class LlamaProcess:
    """Single llama-server subprocess; thread-safe start / stop / swap-model."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._loaded_model: str = ""

    def loaded_model(self) -> str:
        with self._lock:
            return self._loaded_model

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            p = self._proc
            self._proc = None
            self._loaded_model = ""
        _llama_props_ctx_invalidate()
        if not p:
            return True
        try:
            p.terminate()
            try:
                p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2.0)
        except Exception:
            pass
        return True

    def start(self, model_path: str, wait: bool = True, wait_seconds: int = 120) -> dict:
        bin_path = find_llama_bin()
        if not bin_path:
            return {"ok": False, "error": "llama-server.exe not found. Set llama_bin in Settings or install llama.cpp."}
        if not model_path or not Path(model_path).exists():
            return {"ok": False, "error": f"model not found: {model_path}"}

        s = get_settings()
        port = _parse_llama_port()
        # Cap ctx to keep KV cache from blowing past VRAM. A 256k ctx with f16
        # KV on a 16-attn-head model burns ~16 GiB by itself, leaving nothing
        # for the weights and forcing layer offload to system RAM. 32k is a
        # sane chat default; users who want more can raise it knowing the cost.
        ctx_raw = int(s.get("num_ctx") or 32768)
        # 65k is the max we trust for a 16 GB GPU with q8_0 KV cache. Above
        # that, even a mid-size model starts spilling layers to host RAM.
        ctx = min(ctx_raw, 65536) if ctx_raw > 0 else 32768
        n_batch = max(int(s.get("num_batch") or 2048), 512)
        n_ubatch = min(max(n_batch // 2, 512), 1024)
        ngl_setting = int(s.get("num_gpu") or 99)
        ngl = -1 if ngl_setting >= 99 else ngl_setting
        kv_type = (s.get("kv_cache_type") or "q8_0").strip().lower()
        if kv_type not in ("f16", "f32", "q8_0", "q4_0", "q5_0", "q5_1", "q4_1"):
            kv_type = "q8_0"

        # Stop any existing instance first.
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        if running:
            self.stop()

        # If something *else* is squatting on the port (e.g. user launched
        # llama-server manually before bridge), refuse rather than fight it.
        if llama_ping(timeout=0.8):
            return {"ok": False, "error": f"port {port} already in use by another llama-server. Stop it first."}

        cmd = [
            bin_path,
            "-m", model_path,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--jinja",
            "--flash-attn", "on",
            "--no-context-shift",
            "-ngl", str(ngl),
            "-c", str(ctx),
            "-b", str(n_batch),
            "-ub", str(n_ubatch),
            "--cache-type-k", kv_type,
            "--cache-type-v", kv_type,
            "--parallel", "1",
            "--spec-type", "ngram-mod",
            "--spec-ngram-size-n", "24",
            "--draft-min", "48",
            "--draft-max", "64",
            "--reasoning-format", "deepseek",
        ]
        try:
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_CONSOLE
            p = subprocess.Popen(
                cmd,
                cwd=str(Path(bin_path).parent),
                creationflags=creationflags,
            )
        except Exception as e:
            return {"ok": False, "error": f"spawn failed: {e}"}

        with self._lock:
            self._proc = p
            self._loaded_model = model_path

        if not wait:
            return {"ok": True, "pid": p.pid, "model": model_path, "ready": False}
        if wait_for_llama(wait_seconds):
            return {"ok": True, "pid": p.pid, "model": model_path, "ready": True}
        # if we waited but nothing came up, the child likely died
        if p.poll() is not None:
            with self._lock:
                self._proc = None
                self._loaded_model = ""
            return {"ok": False, "error": f"llama-server exited (code {p.returncode}) — check the model file or VRAM."}
        return {"ok": False, "error": "llama-server didn't answer in time. Still loading; check the spawned window."}


_llama = LlamaProcess()


def llama_ping(timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{LLAMA}/v1/models", timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False


# back-compat alias for any stray callers
ollama_ping = llama_ping


def wait_for_llama(wait_seconds: int = 30) -> bool:
    """Poll llama-server up to wait_seconds. Returns True once it's up."""
    if llama_ping():
        return True
    t0 = time.time()
    printed = False
    while time.time() - t0 < wait_seconds:
        time.sleep(0.6)
        if llama_ping(timeout=1.5):
            print(f"  llama-server up in {time.time() - t0:.1f}s")
            return True
        if not printed and time.time() - t0 > 2:
            print(f"  waiting for llama-server at {LLAMA} ...")
            printed = True
    return False


# ---- main ------------------------------------------------------------------

def main():
    print(f"accuretta bridge")
    print(f"  root:    {ROOT}")
    print(f"  llama:   {LLAMA}")
    if VISION_LLAMA and VISION_LLAMA != LLAMA:
        print(f"  vision:  {VISION_LLAMA}")
    print(f"  port:    {PORT}")
    print(f"  bind:    0.0.0.0  (reachable over LAN / Tailscale)")

    # first-run system context scan (creates data/ACCURETTA.md if missing)
    if not SYSTEM_CONTEXT_FILE.exists():
        print("  scanning system (first run) ...")
        ensure_system_context()
        print(f"  wrote:   {SYSTEM_CONTEXT_FILE}")
    else:
        print(f"  context: {SYSTEM_CONTEXT_FILE} (edit or delete to rescan)")

    # auto-spawn llama-server with the user's last-loaded model, if any.
    # Skip if something is already answering on LLAMA_HOST (user launched
    # their own — we don't fight them).
    s_initial = get_settings()
    last_model = (s_initial.get("model_path") or "").strip()
    if llama_ping(timeout=1.0):
        print(f"  llama-server: already running at {LLAMA} (using existing instance)")
        try:
            info = llama_get("/v1/models")
            names = [m.get("id") for m in (info.get("data") or []) if m.get("id")]
            if names and not s_initial.get("model"):
                s_initial["model"] = names[0]
                save_json(SETTINGS_FILE, s_initial)
        except Exception:
            pass
    elif last_model and Path(last_model).exists():
        print(f"  spawning llama-server with {Path(last_model).name} ...")
        res = _llama.start(last_model, wait=True, wait_seconds=120)
        if res.get("ok"):
            print(f"  llama-server: ready (pid {res.get('pid')})")
        else:
            print(f"  [warn] llama-server didn't start: {res.get('error')}")
            print(f"         pick a different model or update settings via the UI.")
    else:
        bin_path = find_llama_bin()
        if not bin_path:
            print(f"  [warn] llama-server.exe not found.")
            print(f"         install llama.cpp or set llama_bin in Settings.")
        elif not s_initial.get("models_dir"):
            print(f"  [info] no models folder set yet.")
            print(f"         open Settings -> Models folder, pick where your .gguf files live.")
        else:
            print(f"  [info] no model loaded yet. Pick one in Settings -> Models.")

    # ensure the spawned llama-server dies with us
    import atexit
    atexit.register(_llama.stop)

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
    finally:
        try:
            _llama.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
