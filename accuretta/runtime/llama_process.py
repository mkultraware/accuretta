# SPDX-License-Identifier: AGPL-3.0-only
"""The llama-server manager: starting, stopping and watching the server (or the Maya
engine), the watchdog's self-heal, and the per-model prompt-size caches.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.errors import _error_store
from accuretta.core.endpoints import LLAMA
from accuretta.core.events import broadcast_event
from accuretta.core.jobs import _KillOnCloseJob
from accuretta.core.paths import DATA
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.settings import _normalized_mmproj_mode
from accuretta.core.settings import get_settings
from accuretta.runtime.gguf import read_gguf_metadata
from accuretta.runtime.gpu_detect import detect_vram_gb
from accuretta.runtime.kv_sizing import _checkpoint_spawn_flags
from accuretta.runtime.launch_helpers import _measure_load_speed
from accuretta.runtime.launch_helpers import _mmproj_on_cpu
from accuretta.runtime.launch_helpers import _no_mmap_flags
from accuretta.runtime.launch_helpers import _runtime_spec_strategy
from accuretta.runtime.launch_helpers import _stamp_proven_boot
from accuretta.runtime.llama_bin import find_llama_bin
from accuretta.runtime.llama_caps import llama_caps
from accuretta.runtime.llama_http import _llama_tokenize
from accuretta.runtime.llama_http import wait_for_llama
from accuretta.runtime.maya import _maya_cuda_dirs
from accuretta.runtime.maya import _maya_drafter_for
from accuretta.runtime.maya_find import _maya_engine_for
from accuretta.runtime.maya_find import _maya_gguf_meta
from accuretta.runtime.maya_find import _maya_only_model
from accuretta.runtime.maya_find import _maya_python_cmd
from accuretta.runtime.maya_sizing import _MAYA_DRAFT_CTX
from accuretta.runtime.maya_sizing import _maya_tokenizer_dir
from accuretta.runtime.maya_sizing import _maya_vision_on_gpu
from accuretta.runtime.maya_sizing import _trim_maya_engine_memory
from accuretta.runtime.maya_sizing import maya_fit
from accuretta.runtime.maya_sizing import maya_needed_message
from accuretta.runtime.model_advisor import _RUNTIME_LAUNCH
from accuretta.runtime.model_advisor import _model_is_dense
from accuretta.runtime.model_advisor import _record_load_sample
from accuretta.runtime.model_config import _MODEL_CONFIG_KEYS
from accuretta.runtime.model_config import _save_model_config
from accuretta.runtime.model_files import _resolve_mmproj_for_tune
from accuretta.runtime.model_files import _spec_fingerprint
from accuretta.runtime.model_files import find_spec_draft_for
from accuretta.runtime.model_files import spec_draft_match
from accuretta.runtime.model_inspect import inspect_model
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.runtime.system_ram import _system_ram_info
from accuretta.security.agent_processes import _parse_llama_port
from accuretta.security.child_env import _child_env
from pathlib import Path
from typing import Any


# last known prompt token count from llama-server: STATE.last_prompt_tokens
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


# Cache /props readings — the value never changes for a running server,
# and we'd rather not hit it every turn just to read n_ctx_slot.
_LLAMA_PROPS_CTX_CACHE: tuple[str, int] | None = None  # (base, ctx)


_LLAMA_PROPS_DATA_CACHE: tuple[str, dict] | None = None


_REASONING_CAP_CACHE: dict[tuple, dict] = {}


def _llama_props_data() -> dict:
    """Return a cached /props snapshot for the currently running server."""
    global _LLAMA_PROPS_DATA_CACHE
    base = LLAMA
    if _LLAMA_PROPS_DATA_CACHE and _LLAMA_PROPS_DATA_CACHE[0] == base:
        return _LLAMA_PROPS_DATA_CACHE[1]
    try:
        with urllib.request.urlopen(f"{base}/props", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict):
            _LLAMA_PROPS_DATA_CACHE = (base, data)
            return data
    except Exception:
        pass
    return {}


def _llama_props_ctx() -> int | None:
    """Return the llama-server's actual slot context size, or None if we
    can't reach /props. Cached per LLAMA base so we don't re-poll every turn."""
    global _LLAMA_PROPS_CTX_CACHE
    base = LLAMA
    if _LLAMA_PROPS_CTX_CACHE and _LLAMA_PROPS_CTX_CACHE[0] == base:
        return _LLAMA_PROPS_CTX_CACHE[1]
    try:
        data = _llama_props_data()
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
    global _LLAMA_PROPS_CTX_CACHE, _LLAMA_PROPS_DATA_CACHE, _TOOLS_OVERHEAD_CACHE, _RT_TRAIL_SYS_CACHE
    _LLAMA_PROPS_CTX_CACHE = None
    _LLAMA_PROPS_DATA_CACHE = None
    _REASONING_CAP_CACHE.clear()
    _TOOLS_OVERHEAD_CACHE = ("", 0)
    _RT_TRAIL_SYS_CACHE = None


# Whether this model's chat template accepts a system message that isn't first
# (chatml/llama3/mistral/qwen do; Gemma's errors). Decides whether the
# recency-end mission echo rides as a trailing `system` reminder or inside a
# trailing `user` turn. Probed on the live server; if the probe can't run we
# use the `user` form, since a wrong `system` guess 500s the whole turn on
# strict templates. Cached per server; cleared on restart above.
_RT_TRAIL_SYS_CACHE: bool | None = None


def _rt_trailing_system_ok() -> bool:
    global _RT_TRAIL_SYS_CACHE
    if _RT_TRAIL_SYS_CACHE is not None:
        return _RT_TRAIL_SYS_CACHE
    ok: bool | None = None
    try:
        req = urllib.request.Request(
            f"{LLAMA}/v1/chat/completions",
            data=json.dumps({
                "model": "local", "stream": False, "max_tokens": 1,
                "messages": [
                    {"role": "user", "content": "ping"},
                    {"role": "system", "content": "ok"},
                ],
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            ok = 200 <= resp.status < 300
    except urllib.error.HTTPError:
        ok = False   # template rejected the non-leading system message
    except Exception:
        ok = None    # server unreachable/busy — don't cache a transient failure
    if ok is None:
        # Probe inconclusive (server busy/unreachable): use the trailing `user` form,
        # which is valid under every template.
        return False
    _RT_TRAIL_SYS_CACHE = ok
    return ok


# Cache for the tools-spec token count, re-tokenized only when the rendered
# tools JSON (or the loaded model) changes.
_TOOLS_OVERHEAD_CACHE: tuple[Any, int] = ("", 0)


def _tools_spec_overhead_tokens(tools_json: str) -> int:
    """Estimate the token overhead llama-server's Jinja chat template adds
    when it inlines the tools array into the system message. Uses /tokenize
    for an exact count; falls back to a JSON-density approximation if the
    server is unreachable. +512 covers template boilerplate (the prose the
    chat template wraps tool defs in — varies per model family)."""
    global _TOOLS_OVERHEAD_CACHE
    cache_key = (_llama.loaded_model(), get_settings().get("model_path"), tools_json, int(time.monotonic() // 15))
    if _TOOLS_OVERHEAD_CACHE[0] == cache_key:
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
    _TOOLS_OVERHEAD_CACHE = (cache_key, overhead)
    return overhead


def _watchdog_self_heal(attempt: int, model_path: str = "") -> None:
    """Progressive degrade for a crash-looping config. The usual crash cause is
    resource exhaustion, which identical retries can't fix, so:
      attempt 2 -> halve num_ctx (floor 4096); KV cache is the usual OOM driver.
      attempt 3 -> speculative decoding off + more CPU offload (MoE: n_cpu_moe
                  up; dense: num_gpu down).
    Degraded settings are persisted: a config that boots beats a "perfect" one
    that crash-loops. The user can re-tune upward from Settings.

    model_path is the model being restarted. The settings (and the per-model
    config this saves into) belong to the selected model, so when another
    model is the one crashing, nothing is degraded: that would shrink the
    selected model's setup for a crash it had no part in."""
    try:
        s = get_settings()
        selected = str(s.get("model_path") or "")
        if model_path and selected and os.path.normcase(model_path) != os.path.normcase(selected):
            print(f"[watchdog] self-heal skipped: {os.path.basename(model_path)} is not the selected "
                  f"model ({os.path.basename(selected)}), so its settings aren't the ones to shrink",
                  file=sys.stderr)
            return
        changed: list[str] = []
        bad_draft = ""
        if attempt == 2:
            cur = int(s.get("num_ctx") or 32768)
            new = max(4096, cur // 2)
            if new != cur:
                s["num_ctx"] = new
                changed.append(f"num_ctx {cur:,} → {new:,}")
        elif attempt >= 3:
            if (s.get("spec_strategy") or "off") != "off":
                s["spec_strategy"] = "off"
                changed.append("speculative decoding → off")
            bad_draft = str(s.get("spec_draft_model") or "").strip()
            if bad_draft:
                # A draft that helped crash the server three times must not be
                # retried silently on every restart: drop it here and remember
                # the failure in this model's saved config.
                s["spec_draft_model"] = ""
                changed.append(f"speculative draft {os.path.basename(bad_draft)} disabled")
            try:
                mp = s.get("model_path") or ""
                prof = inspect_model(mp) if mp else {}
            except Exception:
                prof = {}
            if prof.get("is_moe") and (prof.get("block_count") or 0) > 0:
                cur = int(s.get("n_cpu_moe") or 0)
                new = min(prof["block_count"], max(cur + 8, int(prof["block_count"] * 0.75)))
                if new != cur:
                    s["n_cpu_moe"] = new
                    changed.append(f"n_cpu_moe {cur} → {new}")
            else:
                cur = int(s.get("num_gpu") or 99)
                new = 40 if cur >= 99 else max(8, int(cur * 0.7))
                if new != cur:
                    s["num_gpu"] = new
                    changed.append(f"num_gpu {cur} → {new}")
        if changed:
            save_json(SETTINGS_FILE, s)
            # Sync the degraded flags into this model's remembered config too, or
            # _apply_model_config restores the pre-heal ctx on the next load.
            # _MODEL_CONFIG_KEYS includes the spec keys, so a disabled draft is remembered
            # the same way.
            try:
                _save_model_config(str(s.get("model_path") or ""),
                                   {k: s[k] for k in _MODEL_CONFIG_KEYS
                                    if k in s})
            except Exception:
                pass
            msg = f"watchdog self-heal (attempt {attempt}): " + "; ".join(changed)
            print(f"[watchdog] {msg}", file=sys.stderr)
            broadcast_event({"type": "llama:auto_degraded", "message": msg, "changes": changed})
    except Exception as e:
        print(f"[watchdog] self-heal failed: {e!r}", file=sys.stderr)


_child_job = _KillOnCloseJob()


class LlamaProcess:
    """Single llama-server subprocess; thread-safe start / stop / swap-model.

    A watchdog thread polls every WATCHDOG_POLL_S and respawns the subprocess
    with the args of the last successful start(), healing silent crashes (OOM,
    signal, segfault). Guardrails:

      1. _watchdog_disabled, set by stop_permanent() (user stopped the server),
         cleared by the next user-initiated start().
      2. Circuit breaker: >= WATCHDOG_MAX_CRASHES deaths within
         WATCHDOG_WINDOW_S suspends auto-restart, so a doomed config (bad model
         file, wrong mmproj, OOM at this ctx) can't loop forever. It
         self-recovers a bounded number of times after a cooldown.
      3. _watchdog_stop, set by shutdown_watchdog() at bridge exit; checked
         before and after every sleep so the thread exits within ~5s."""

    WATCHDOG_POLL_S = 5.0
    WATCHDOG_MAX_CRASHES = 3
    WATCHDOG_WINDOW_S = 60.0

    def __init__(self):
        self._lock = threading.RLock()
        self._start_lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._loaded_model: str = ""
        # mmproj path the current process started with ("" = text-only), so the chat
        # handler can skip the describe_image side-trip when the model sees images
        # natively.
        self._loaded_mmproj: str = ""
        # Which engine serves the loaded model: "llama" (llama-server) or "maya"
        # (maya's server, see find_maya_engine); "" when nothing runs. For maya,
        # how it was started: {ctx, drafter, vision_gpu}.
        self._engine: str = ""
        self._maya_launch: dict = {}
        # Watchdog bookkeeping.
        self._last_start_args: dict | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_disabled = False
        self._restart_failed = False
        self._restart_history: list[float] = []
        # Breaker auto-recovery: a tripped breaker retries a bounded number of times
        # after a cooldown instead of waiting for a manual restart.
        self._breaker_trip_at = 0.0
        self._breaker_recoveries = 0
        # True while a user-initiated start() is in flight, so the watchdog doesn't
        # see the brief dead window between stop() and the new spawn and launch a
        # second process with the old args. Released in a finally.
        self._starting = False
        # Ring buffer of the child's combined stdout/stderr, drained by a daemon
        # reader thread, so the UI can show the llama.cpp backend (model-load
        # progress, errors) instead of a hidden or stray console window.
        self._log_lines: list[str] = []
        self._log_lock = threading.Lock()
        self._log_reader: threading.Thread | None = None

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

    def engine(self) -> str:
        with self._lock:
            return self._engine if self._proc is not None and self._proc.poll() is None else ""

    def maya_launch(self) -> dict:
        with self._lock:
            return dict(self._maya_launch) if self._engine == "maya" and self._proc is not None \
                and self._proc.poll() is None else {}

    def continues_replies(self) -> bool:
        """Whether the server continues a trailing assistant message
        (continue_final_message). llama-server does; maya's starts a new reply."""
        return self.engine() != "maya"

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

    # Why a launch died, from llama.cpp's own output (the log holds only the
    # latest launch). The first match wins, most specific first.
    _LOAD_ERROR_PATTERNS = (
        (re.compile(r"unknown model architecture: '([^']+)'"),
         "this llama.cpp build doesn't support the model's architecture ({0}). A newer llama.cpp may."),
        (re.compile(r"error loading model: (.*tensor '[^']+' not found.*)"),
         "{0}. This llama.cpp build can't read this model file; a newer llama.cpp may."),
        (re.compile(r"((?:cudaMalloc|ggml_backend_\w*alloc\w*)[^:]*: (?:failed|out of memory).*)", re.I),
         "{0} (not enough graphics memory)"),
        (re.compile(r"error loading model: (.+)"), "{0}"),
        # maya: its engine's own last words (see _start_maya), then the server's.
        (re.compile(r"^\[maya engine\] (.+)"), "{0}"),
        (re.compile(r"RuntimeError: (.+)"), "{0}"),
    )

    def _load_error_reason(self) -> str:
        with self._log_lock:
            lines = list(self._log_lines[-200:])
        for pattern, template in self._LOAD_ERROR_PATTERNS:
            for line in lines:
                m = pattern.search(line)
                if m:
                    return template.format(m.group(1).strip().rstrip("."))[:300]
        return ""

    def stop(self, timeout: float = 5.0) -> bool:
        with self._lock:
            p = self._proc
            self._proc = None
            self._loaded_model = ""
            self._loaded_mmproj = ""
            self._engine = ""
            self._maya_launch = {}
        _llama_props_ctx_invalidate()
        if not p:
            return True
        if getattr(p, "_accuretta_tree", False):
            # maya's server runs the engine and the image encoder as children;
            # ending only the server would leave the engine holding the VRAM
            # until it noticed.
            try:
                if _terminate_process_tree(p, "host"):
                    return True
            except Exception:
                pass
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
        self._breaker_trip_at = 0.0
        self._breaker_recoveries = 0
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
        # Global settings toggle, checked every tick so it takes effect on the next
        # poll.
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
            # Breaker tripped: self-recover a bounded number of times after a fresh
            # cooldown (whatever wedged the first boot, such as VRAM held by another app,
            # is usually gone by then); only a persistent crash loop stays suspended.
            if (now - self._breaker_trip_at >= self.WATCHDOG_WINDOW_S + 30.0
                    and self._breaker_recoveries < 2):
                self._breaker_recoveries += 1
                self._restart_history = []
                self._restart_failed = False
                self._breaker_trip_at = 0.0
                msg = (f"llama-server stayed down after {self.WATCHDOG_MAX_CRASHES} crashes — "
                       f"auto-recovery attempt {self._breaker_recoveries}/2 "
                       "(fresh circuit-breaker cycle, self-healing launch args)")
                print(f"[watchdog] {msg}", file=sys.stderr)
                try:
                    broadcast_event({"type": "llama:watchdog_restart",
                                     "attempt": 4, "delay": 0,
                                     "recovery": self._breaker_recoveries})
                except Exception:
                    pass
            else:
                if self._breaker_trip_at == 0.0:
                    self._breaker_trip_at = now
                self._restart_failed = True
                msg = (
                    f"llama-server crashed {len(self._restart_history)} times in "
                    f"{int(self.WATCHDOG_WINDOW_S)}s; auto-restart suspended. "
                    f"Fix the config (model path, ctx size, mmproj), then pick "
                    f"the model again in the model menu to resume."
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
        # The exit code and the last output lines tell an out-of-memory kill from
        # an assert or a bad flag; the record used to carry neither.
        try:
            with self._lock:
                code = self._proc.poll() if self._proc is not None else None
            with self._log_lock:
                tail = list(self._log_lines[-40:])
            _error_store.record("backend", "ModelServerCrash",
                                f"llama-server died (exit code {code}); restart attempt {attempt}",
                                "llama-server",
                                f"exit code: {code}\nlast llama-server output:\n" + "\n".join(tail))
        except Exception:
            pass
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
        # Self-heal BEFORE the respawn: restart reads settings fresh, so a
        # degrade here takes effect on this very attempt. Attempt 1 is always
        # a plain retry (transient crash); 2+ steps the config down.
        if attempt >= 2:
            _watchdog_self_heal(attempt, str(args.get("model_path") or ""))
            # An explicit ctx override rides the replayed args, not settings —
            # step it down too or the degrade above would be bypassed.
            if int(args.get("ctx_override") or 0) > 0:
                args["ctx_override"] = max(4096, int(args["ctx_override"]) // 2)
        # _last_start_args was captured at successful-start time. The circuit breaker
        # is deliberately not reset here, so each respawn counts toward the limit.
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
        # Guard the call with the _starting flag so the watchdog leaves a transition
        # alone. Setting it before reset_circuit_breaker() and the inner stop() closes
        # the race where a settings-driven reload is caught mid-load by a watchdog
        # tick that spawns a duplicate with the old args. Released in a finally.
        if not self._start_lock.acquire(blocking=False):
            return {"ok": False, "error": "A model is already starting. Wait for it to finish."}
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
            self._start_lock.release()

    def _start_impl(self, model_path: str, wait: bool = True,
                    wait_seconds: int = 120,
                    _from_watchdog: bool = False,
                    port_override: int = 0, mmproj_override: str = None,
                    ctx_override: int = 0, ngl_override: int = -1) -> dict:
        # A user-initiated start clears the circuit breaker and re-enables the
        # watchdog; watchdog retries skip this so each attempt counts toward the
        # crash limit.
        if not _from_watchdog:
            self.reset_circuit_breaker()
        _maya = _maya_engine_for(model_path) if model_path and safe_exists(model_path) else {}
        if _maya:
            return self._start_maya(_maya, model_path, wait=wait, wait_seconds=wait_seconds,
                                    _from_watchdog=_from_watchdog, port_override=port_override,
                                    mmproj_override=mmproj_override, ctx_override=ctx_override,
                                    ngl_override=ngl_override)
        if model_path and safe_exists(model_path) and _maya_only_model(model_path):
            # Made for maya, and maya isn't installed: llama.cpp would load it
            # and answer with gibberish.
            return {"ok": False, "needs_maya": True, "error": maya_needed_message(model_path)}
        bin_path = find_llama_bin()
        if not bin_path:
            return {"ok": False, "error": "llama-server.exe not found. Set llama_bin in Settings or install llama.cpp."}
        if not model_path or not safe_exists(model_path):
            return {"ok": False, "error": f"model not found: {model_path}"}
        # Which optional flags does THIS build understand? Gates every
        # spawn-time flag decision below (unknown flags hard-exit the server).
        caps = llama_caps(bin_path)

        s = get_settings()
        port = port_override if port_override > 0 else _parse_llama_port()
        # Context size: 32k default, hard ceiling 1M tokens. Large windows are viable
        # because --n-cpu-moe spills weights rather than KV cache; the KV cost at big
        # ctx is the user's call.
        ctx_raw = ctx_override if ctx_override > 0 else int(s.get("num_ctx") or 32768)
        ctx = min(max(ctx_raw, 512), 1_048_576) if ctx_raw > 0 else 32768
        n_batch = max(int(s.get("num_batch") or 2048), 32)
        n_ubatch_raw = int(s.get("n_ubatch") or 0)
        n_ubatch = n_ubatch_raw if n_ubatch_raw > 0 else min(max(n_batch // 2, 512), 4096)
        ngl_setting = ngl_override if ngl_override >= 0 else int(s.get("num_gpu") or 99)
        ngl = -1 if ngl_setting >= 99 else ngl_setting
        n_threads = int(s.get("num_thread") or 0)  # 0 = let llama-server pick
        n_parallel = max(int(s.get("n_parallel") or 1), 1)
        n_cpu_moe = max(int(s.get("n_cpu_moe") or 0), 0)
        if n_cpu_moe > 0 and _model_is_dense(model_path):
            print(f"[llama] ignoring n_cpu_moe {n_cpu_moe}: {os.path.basename(model_path)} is a dense model",
                  file=sys.stderr)
            n_cpu_moe = 0
        kv_type = (s.get("kv_cache_type") or "q8_0").strip().lower()
        if kv_type not in ("f16", "bf16", "f32", "q8_0", "q4_0", "q5_0", "q5_1", "q4_1"):
            kv_type = "q8_0"
        # Split K/V: V cache may be typed separately ("" = mirror K). K is the
        # sensitive half; V at 4-bit is near-lossless (llama.cpp KV-quant data).
        kv_type_v = (s.get("kv_cache_type_v") or "").strip().lower()
        if kv_type_v not in ("f16", "bf16", "f32", "q8_0", "q4_0", "q5_0", "q5_1", "q4_1"):
            kv_type_v = kv_type
        flash_on = bool(s.get("flash_attn", True))
        # Speculative strategy, honoring the legacy flag. get_settings() merges
        # DEFAULTS (spec_strategy="ngram-mod") into `s`, so a true legacy settings.json
        # (only enable_speculative) must be detected on the raw file. The legacy
        # `enable_speculative=false` veto applies only when the raw file has no
        # spec_strategy key; once one exists on disk it wins, since auto-tune and the
        # UI write it deliberately.
        spec_strategy = (s.get("spec_strategy") or "").strip().lower()
        _raw_settings = load_json(SETTINGS_FILE, {})
        _has_spec_key = isinstance(_raw_settings, dict) and "spec_strategy" in _raw_settings
        if s.get("enable_speculative") is False and not _has_spec_key:
            spec_strategy = "off"
        elif spec_strategy not in ("off", "ngram-mod", "draft-mtp", "dflash", "dspark"):
            spec_strategy = "ngram-mod" if bool(s.get("enable_speculative", True)) else "off"
            
        if spec_strategy == "draft-mtp" and model_path:
            # Fall back to ngram-mod or off if the model has no MTP layers, the
            # build lacks draft-mtp, or it's a known distilled model.
            model_lower = os.path.basename(model_path).lower()
            _EXCLUDE_MTP = ("distil", "distill", "distilled", "glm")
            forced_fallback = False
            if not caps.get("draft_mtp", True):
                forced_fallback = True
                print("[llama] this llama-server build has no draft-mtp support — falling back", file=sys.stderr)
            elif any(e in model_lower for e in _EXCLUDE_MTP):
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
                
        _RUNTIME_LAUNCH["spec_draft_n"] = 0      # set again below if MTP drafts
        no_warmup = bool(s.get("no_warmup", False))
        enable_metrics = bool(s.get("enable_metrics", False))
        extra_args_raw = (s.get("llama_extra_args") or "").strip()

        # Projector mode is explicit; "off" must never fall through to
        # auto-detection.
        if mmproj_override is not None:
            mmproj_path = mmproj_override if safe_exists(mmproj_override) else ""
        else:
            mode = _normalized_mmproj_mode(s)
            configured_mmproj = str(s.get("mmproj_path") or "").strip()
            mmproj_path = _resolve_mmproj_for_tune(model_path, s)
            if mode == "manual" and configured_mmproj and not mmproj_path:
                print(f"[llama] WARN manual mmproj_path is missing: {configured_mmproj}")
            elif mode == "auto" and mmproj_path:
                print(f"[llama] auto-detected vision projector: {mmproj_path}")
        spec_strategy = _runtime_spec_strategy(spec_strategy, mmproj_path)
        spec_draft_model = str(s.get("spec_draft_model") or "").strip()

        # Stop any existing instance first.
        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        if running:
            self.stop()

        # If something *else* is squatting on the port (e.g. user launched
        # llama-server manually before bridge), refuse rather than fight it.
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.8):
                return {"ok": False, "error": f"Port {port} is already in use. The existing process was left running; no model was started."}
        except OSError:
            pass

        cmd = [
            bin_path,
            "-m", model_path,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--jinja",
            "--no-context-shift",
            "-ngl", str(ngl),
            "-c", str(ctx),
            "-b", str(n_batch),
            "-ub", str(n_ubatch),
            "--cache-type-k", kv_type,
            "--cache-type-v", kv_type_v,
            # --cache-reuse is deliberately not passed: its KV-shift reuse corrupts
            # positional info on some models (repetition / looping), most visibly after a
            # large tool result reshapes the middle of the context, and llama-server
            # disables it anyway for MTP contexts. No explicit --cache-ram either (the
            # 8192 MiB default is fine). Prompt prefix caching (cache_prompt) stays on.
            "--parallel", str(n_parallel),
            "--reasoning-format", "deepseek",
        ]
        # -ngl -1 lets llama.cpp's --fit (on by default) place layers, and its
        # default 1024 MiB per-device margin quietly pushed weights and KV to system
        # RAM whenever the tuner's exact fit left less free (176k ctx on a 4090: 2.6
        # GB offloaded, 93 -> 28 tok/s). The tuner keeps its own margin, so fit stays
        # only as a safety net for a real shortage at boot.
        if ngl == -1 and caps.get("fit_target"):
            cmd += ["--fit-target", "384"]
        # CPU share of an expert offload (an upper bound: the first n_cpu_moe
        # layers' whole size) and free RAM now, after the old instance stopped.
        _cpu_share_gb = _ram_free_gb = 0.0
        if n_cpu_moe > 0:
            try:
                _prof = inspect_model(model_path)
                _nl = int(_prof.get("block_count") or 0)
                if _nl:
                    _cpu_share_gb = float(_prof.get("size_gb") or 0) * min(n_cpu_moe, _nl) / _nl
                _ri = _system_ram_info()
                _ram_free_gb = float(_ri[1]) if _ri else 0.0
            except Exception:
                _cpu_share_gb = 0.0
        cmd += _no_mmap_flags(caps, ngl, n_cpu_moe, extra_args_raw,
                              cpu_share_gb=_cpu_share_gb, ram_free_gb=_ram_free_gb)
        if caps.get("flash_attn", True):
            cmd += ["--flash-attn", "on" if flash_on else "off"]
        elif flash_on:
            print("[llama] this build has no --flash-attn flag; using the binary default", file=sys.stderr)
        try:
            # Context checkpoints are a manual compatibility override (see
            # _checkpoint_spawn_flags).
            _ck_flags = _checkpoint_spawn_flags(caps, inspect_model(model_path))
            if _ck_flags:
                cmd += _ck_flags
                print(f"[llama] context checkpoints on (hybrid/SWA model): {' '.join(_ck_flags)}",
                      file=sys.stderr)
        except Exception:
            pass
        if mmproj_path:
            # Loads the vision tower + projector. After this, /v1/chat/completions
            # accepts {type:"image_url", image_url:{url:"data:..."}} content blocks.
            cmd += ["--mmproj", mmproj_path]
            if _mmproj_on_cpu(caps):
                cmd += ["--no-mmproj-offload"]
            if "moondream" in Path(model_path).stem.lower():
                # Moondream 2 has no built-in jinja template that works with llama.cpp's
                # multimodal message extraction; without one it emits a stop token at once or
                # hallucinates.
                cmd += ["--chat-template", "Question: {{messages[0].content}}\n\nAnswer:"]
        if n_threads > 0:
            cmd += ["-t", str(n_threads)]
        if n_cpu_moe > 0:
            if caps.get("n_cpu_moe", True):
                # --n-cpu-moe (alias -ncmoe) keeps `n` MoE expert tensors on the CPU, so giant
                # MoE models run on small VRAM at a latency cost instead of OOMing.
                cmd += ["--n-cpu-moe", str(n_cpu_moe)]
                # GPU cache for the offloaded experts (llama.cpp #29887): hot
                # experts run on the GPU, only misses cross PCIe. Sized by the
                # tuner; 0 = off.
                try:
                    moe_cache = int(s.get("moe_cache_mib") or 0)
                except (TypeError, ValueError):
                    moe_cache = 0
                if moe_cache > 0 and caps.get("moe_cache"):
                    cmd += ["--moe-cache-mib", str(moe_cache)]
            elif caps.get("override_tensor"):
                # Older build without --n-cpu-moe: -ot pushes ALL expert tensors
                # to CPU (coarser — no per-layer count — but the model boots).
                cmd += ["-ot", ".ffn_.*_exps.=CPU"]
                print("[llama] build lacks --n-cpu-moe; falling back to -ot .ffn_.*_exps.=CPU (all experts on CPU)",
                      file=sys.stderr)
            else:
                print("[llama] WARN build supports neither --n-cpu-moe nor -ot; "
                      "expert offload skipped — big MoEs may OOM", file=sys.stderr)
        if spec_strategy == "ngram-mod":
            # llama.cpp renamed --spec-ngram-size-n to --spec-ngram-mod-n-match
            # in recent builds. The capability probe picks the flag this binary
            # actually understands; with neither, speculative turns itself off
            # instead of hard-exiting the server at startup.
            if caps.get("ngram_mod", False):
                cmd += [
                    "--spec-type", "ngram-mod",
                    "--spec-ngram-mod-n-match", "24",
                    "--spec-ngram-mod-n-min", "48",
                    "--spec-ngram-mod-n-max", "64",
                ]
            elif caps.get("ngram_legacy"):
                cmd += [
                    "--spec-type", "ngram-mod",
                    "--spec-ngram-size-n", "24",
                    "--spec-draft-n-min", "48",
                    "--spec-draft-n-max", "64",
                ]
                print("[llama] using legacy n-gram flag names (older build)", file=sys.stderr)
            else:
                print("[llama] this build supports no n-gram speculative flags — spec_strategy→off",
                      file=sys.stderr)
        elif spec_strategy == "draft-mtp":
            # Multi-token prediction draft. Needs MTP heads in the model (Qwen 3.5/3.6,
            # DeepSeek V3/R1) and a llama.cpp build with PR #22673; without heads
            # llama-server exits at startup with "no MTP layers found".
            # Draft length: the tuner's per-model pick (spec_draft_n_max), else 3. Not the
            # GGUF head count: one MTP head drafts several tokens in a row (4090 / 27B Q4,
            # chained with ngram-mod: n-max 1 -> 70 tok/s, 2 -> 88, 3 -> 93, 4 -> 92).
            # Every draft is verified by the full model, so length only trades speed.
            try:
                spec_n_max = int(s.get("spec_draft_n_max") or 0)
            except (TypeError, ValueError):
                spec_n_max = 0
            spec_n_max = min(max(spec_n_max, 1), 8) if spec_n_max > 0 else 3
            _RUNTIME_LAUNCH["spec_draft_n"] = spec_n_max
            if caps.get("ngram_mod", False):
                # Chained drafters (llama.cpp PR #20981 / #23269): MTP drafts a few
                # high-quality tokens per step and n-gram backs it up with longer, cheaper
                # drafts. Per-type length flags: the n-gram range lives on
                # --spec-ngram-mod-n-*, --spec-draft-n-max sets the MTP length. Chaining gains
                # nothing on builds older than #23269 and has a reported CUDA OOM (#23154);
                # spec_strategy=draft-mtp is the safe fallback.
                cmd += [
                    "--spec-type", "draft-mtp,ngram-mod",
                    "--spec-draft-n-max", str(spec_n_max),
                    "--spec-ngram-mod-n-match", "24",
                    "--spec-ngram-mod-n-min", "48",
                    "--spec-ngram-mod-n-max", "64",
                ]
            else:
                cmd += [
                    "--spec-type", "draft-mtp",
                    "--spec-draft-n-max", str(spec_n_max),
                ]
        elif spec_strategy in ("dflash", "dspark"):
            # DFlash and DSpark use a separate draft network trained for the exact target
            # model. Validate a configured path against the target before trusting it,
            # and otherwise fall back to the validated on-disk pair, unless the decoder
            # was explicitly unloaded (spec_draft_mode=off): then no draft is attached.
            resolved_draft = ""
            _pairing_off = str(s.get("spec_draft_mode") or "auto").strip().lower() == "off"
            if _pairing_off:
                print(f"[llama] {spec_strategy}: decoder unloaded by user — "
                      f"spec_strategy→off", file=sys.stderr)
            else:
                if spec_draft_model and safe_exists(spec_draft_model):
                    match = spec_draft_match(model_path, spec_draft_model)
                    draft_readable = bool((match.get("draft") or {}).get("ok"))
                    target_readable = bool(_spec_fingerprint(model_path).get("ok"))
                    if match.get("matched") or not (draft_readable and target_readable):
                        resolved_draft = spec_draft_model
                        if not match.get("matched"):
                            print(f"[llama] {spec_strategy}: draft metadata unreadable — "
                                  f"using configured {os.path.basename(spec_draft_model)} unverified",
                                  file=sys.stderr)
                    else:
                        print(f"[llama] {spec_strategy}: configured draft does not match the model "
                              f"({'; '.join(match.get('reasons') or [])}) — trying auto-pair",
                              file=sys.stderr)
                if not resolved_draft:
                    paired = find_spec_draft_for(model_path) if model_path else {}
                    if paired.get("matched"):
                        resolved_draft = paired.get("path") or ""
                        if resolved_draft:
                            note = (f"{spec_strategy.upper()}: auto-paired {os.path.basename(resolved_draft)} "
                                    f"({'; '.join(paired.get('reasons') or [])})")
                            print(f"[llama] {note}", file=sys.stderr)
                            try:
                                broadcast_event({"type": "notice", "note": note, "quiet": False})
                            except Exception:
                                pass
            if caps.get(spec_strategy) and resolved_draft:
                # CLI type names are prefixed draft- (draft-dflash / draft-dspark)
                # even though the settings value is the short form.
                cmd += ["--spec-draft-model", resolved_draft, "--spec-type",
                        {"dflash": "draft-dflash", "dspark": "draft-dspark"}.get(
                            spec_strategy, spec_strategy)]
                print(f"[llama] speculative decoding: {spec_strategy} ({os.path.basename(resolved_draft)})",
                      file=sys.stderr)
            else:
                print(f"[llama] {spec_strategy} needs a supported build and matching draft GGUF — spec_strategy→off",
                      file=sys.stderr)
        # "off" emits nothing.
        # The MTP draft's KV cache stays at llama.cpp's f16 default on purpose: q8_0
        # (-ctkd/-ctvd) saves 240 MiB of draft KV but grows the two draft compute
        # buffers 392 -> 928 MiB each, a net 830 MiB loss (4090, 27B, 131k, ub 1024).
        # Auto-YaRN: running ctx past the trained context without rope scaling
        # degrades long-range attention on many architectures. When the saved ctx
        # exceeds the GGUF-trained max and the build supports it, enable YaRN; a rope
        # flag in llama_extra_args always wins.
        # llama-server also caps each slot at the trained context (n_ctx_slot()),
        # so YaRN alone allocates the larger KV cache but never lets a chat use
        # it. Raising {arch}.context_length lifts that cap; --yarn-orig-ctx then
        # has to name the real trained size, or YaRN would read the raised value
        # and scale by 1.
        if caps.get("rope_scaling") and "rope" not in extra_args_raw.lower():
            try:
                _gg_yarn = read_gguf_metadata(model_path)
                _trained = int(_gg_yarn.get("context_length") or 0)
                if _trained > 0 and ctx > _trained:
                    cmd += ["--rope-scaling", "yarn"]
                    _arch = str(_gg_yarn.get("architecture") or "").strip()
                    if _arch and "context_length" not in extra_args_raw and "yarn-orig-ctx" not in extra_args_raw:
                        cmd += ["--yarn-orig-ctx", str(_trained),
                                "--override-kv", f"{_arch}.context_length=int:{ctx}"]
                    print(f"[llama] ctx {ctx:,} > trained max {_trained:,} — auto-enabling YaRN rope scaling",
                          file=sys.stderr)
            except Exception:
                pass
        if no_warmup:
            cmd += ["--no-warmup"]
        if enable_metrics:
            cmd += ["--metrics"]
        if extra_args_raw:
            # A whitespace split covers the typical extras (--alias foo, --rope-scaling
            # linear, ...).
            try:
                import shlex
                cmd += shlex.split(extra_args_raw, posix=False)
            except Exception:
                cmd += extra_args_raw.split()
        # Log the exact command before spawning so unknown-flag crashes are
        # debuggable: it can be pasted into a shell to see the real error.
        try:
            import shlex as _shlex
            print(f"[llama] launch: {_shlex.join(cmd) if hasattr(_shlex, 'join') else ' '.join(cmd)}")
        except Exception:
            print(f"[llama] launch: {cmd}")
        def _load_speed_check(base_url: str, live_ctx: int) -> None:
            # Load-time speed sample for the setup advisor (dense, fully on
            # the GPU): what tells a context that fits from one that spilled.
            if (s.get("passive_model_telemetry", True) and ngl == -1 and n_cpu_moe == 0
                    and _model_is_dense(model_path)):
                _tok_s = _measure_load_speed(base_url)
                _record_load_sample(model_path, live_ctx, _tok_s, s)
                if _tok_s:
                    self._append_log(f"[accuretta] load speed check: {_tok_s:.1f} tok/s "
                                     f"at {live_ctx:,} context")

        return self._spawn_and_track(
            cmd, cwd=str(Path(bin_path).parent), env=_child_env(keep=("gpu",)),
            model_path=model_path, mmproj_path=mmproj_path, port=port, ctx=ctx,
            wait=wait, wait_seconds=wait_seconds, _from_watchdog=_from_watchdog,
            port_override=port_override, mmproj_override=mmproj_override,
            ctx_override=ctx_override, ngl_override=ngl_override,
            engine="llama", on_ready=_load_speed_check)

    def _spawn_and_track(self, cmd: list, *, cwd: str, env: dict, model_path: str, mmproj_path: str,
                         port: int, ctx: int, wait: bool, wait_seconds: int, _from_watchdog: bool,
                         port_override: int, mmproj_override, ctx_override: int, ngl_override: int,
                         engine: str = "llama", on_ready=None, on_fail=None, notes=None) -> dict:
        """Spawn a model server, remember the launch for the watchdog, and wait
        until it answers. Shared by llama-server and maya; `on_ready(base_url,
        live_ctx)` runs once it answers, `on_fail()` before a failed launch's
        reason is read from the log, and `notes` go to the Backend log first."""
        label = "maya" if engine == "maya" else "llama-server"
        try:
            creationflags = 0
            # Always run windowless and capture stdout+stderr so the backend's
            # load progress and errors surface in the app's Backend tab, instead
            # of a hidden (desktop app) or stray (browser mode) console window.
            creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            with self._log_lock:
                self._log_lines.clear()
            for line in notes or ():
                self._append_log(line)
            self._append_log("[accuretta] launching: " + " ".join(cmd))
            p = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=creationflags,
            )
            # maya's server runs its engine as a child: stop() ends the tree.
            p._accuretta_tree = engine == "maya"
            _child_job.assign(p)
            self._start_log_reader(p)
        except Exception as e:
            return {"ok": False, "error": f"spawn failed: {e}"}

        with self._lock:
            self._proc = p
            self._loaded_model = model_path
            self._loaded_mmproj = mmproj_path
            self._engine = engine
        if not port_override:
            _last_prompt_tokens_by_chat.clear()
            _CTX_EST_CACHE.clear()
        # Remember exactly how to respawn this configuration. The watchdog replays
        # these args verbatim (the last known-good launch), deliberately ignoring
        # settings changed in between. wait=False keeps the watchdog non-blocking.
        # The previous launch is kept aside: if this one is a switch that never
        # comes up, the watchdog goes back to it (see the failure branch below).
        prev_args = self._last_start_args
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
        base_url = f"http://127.0.0.1:{port}"
        if wait_for_llama(wait_seconds, base_url=base_url, proc=p):
            # The launched ctx is now proven to fit on this machine; stamp it so
            # boot-time retunes honor it. Prefer the server's own reported ctx (it can
            # clamp below the request).
            if not port_override:
                try:
                    _live_ctx = _llama_props_ctx()
                except Exception:
                    _live_ctx = None
                _stamp_proven_boot(model_path, int(_live_ctx or ctx))
                if on_ready:
                    try:
                        on_ready(base_url, int(_live_ctx or ctx))
                    except Exception:
                        pass
            return {"ok": True, "pid": p.pid, "model": model_path,
                    "ready": True, "mmproj": mmproj_path,
                    "vision_capable": bool(mmproj_path)}
        # if we waited but nothing came up, the child likely died
        if p.poll() is not None:
            with self._lock:
                self._proc = None
                self._loaded_model = ""
                self._loaded_mmproj = ""
                self._engine = ""
            # A switch to a model that never came up is not a known-good launch.
            # Left as the watchdog's args, it crash-looped the broken model and
            # its self-heal shrank the selected model's saved setup. Go back to
            # the previous launch instead, so the watchdog brings that model
            # back. A first start, or a retry of the same model, keeps its args:
            # a boot that hit VRAM held by another app is still retried.
            fallback = ""
            if (not _from_watchdog and prev_args
                    and os.path.normcase(str(prev_args.get("model_path") or ""))
                    != os.path.normcase(model_path)):
                self._last_start_args = prev_args
                fallback = str(prev_args.get("model_path") or "")
            if on_fail:
                try:
                    on_fail()
                except Exception:
                    pass
            reason = self._load_error_reason()
            error = (f"{label} exited (code {p.returncode}): {reason}" if reason else
                     f"{label} exited (code {p.returncode}) — check the model file or VRAM.")
            if fallback:
                error += f" Going back to {Path(fallback).stem}."
            out = {"ok": False, "error": error}
            if fallback:
                out["fallback_model"] = fallback
            return out
        return {"ok": False, "error": f"{label} didn't answer in time. Still loading; check the spawned window."}

    def _start_maya(self, maya: dict, model_path: str, *, wait: bool, wait_seconds: int,
                    _from_watchdog: bool, port_override: int, mmproj_override, ctx_override: int,
                    ngl_override: int) -> dict:
        """Launch maya's server for a model maya runs (see find_maya_engine).
        Context and projector come from settings as for llama-server. The
        drafter rides along when the speculative setting allows it and it still
        fits beside that context; otherwise it is left out, never squeezed in
        (a full card on Windows pages to RAM and everything slows down)."""
        s = get_settings()
        port = port_override if port_override > 0 else _parse_llama_port()
        trained = int(_maya_gguf_meta(model_path).get("context_length") or 0) or 1_048_576
        ctx_raw = ctx_override if ctx_override > 0 else int(s.get("num_ctx") or 32768)
        ctx = min(max(ctx_raw, 8192), trained)
        notes: list[str] = []
        # Projector: the same choice as for llama-server ("off" stays off).
        if mmproj_override is not None:
            mmproj_path = mmproj_override if safe_exists(mmproj_override) else ""
        else:
            mmproj_path = _resolve_mmproj_for_tune(model_path, s)
        if mmproj_path and not maya.get("vision"):
            notes.append("[maya] the checkout has no maya-vision build: images off")
            mmproj_path = ""
        vision_gpu = bool(mmproj_path) and _maya_vision_on_gpu(s)
        drafter = ""
        if (str(s.get("spec_strategy") or "off").strip().lower() != "off"
                and str(s.get("spec_draft_mode") or "auto").strip().lower() != "off"):
            drafter = _maya_drafter_for(model_path)
        _RUNTIME_LAUNCH["spec_draft_n"] = 0
        # The tokenizer files come first: if they can't be made, the running
        # model is left alone.
        try:
            tok_dir = _maya_tokenizer_dir(maya, model_path, log=notes.append)
        except Exception as e:
            return {"ok": False, "error": f"maya: {e}"}

        with self._lock:
            running = self._proc is not None and self._proc.poll() is None
        if running:
            self.stop()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.8):
                return {"ok": False, "error": f"Port {port} is already in use. The existing process was left running; no model was started."}
        except OSError:
            pass
        # Now that nothing of ours holds VRAM, measure what's really free. The
        # drafter goes first when it doesn't fit; a context too big for every
        # expert to stay resident starts at the largest that fits (this launch
        # only, settings untouched). maya's fallback for that case pins the
        # experts in RAM, which crashed with out of memory on a 31 GB PC.
        free = self._maya_free_after_stop(running)
        if free > 0:
            fit = maya_fit(model_path, free, drafter, vision_gpu)
            if drafter and ctx > fit["draft_ctx"] >= 32768 and ctx - fit["draft_ctx"] <= 16384:
                # A sliver over: free VRAM moves a little between launches.
                # Keep the helper the setup was sized for.
                notes.append(f"[maya] context {ctx:,} trimmed to {fit['draft_ctx']:,} "
                             f"to keep the drafter ({free:.1f} GB free)")
                ctx = fit["draft_ctx"]
            elif drafter and ctx > fit["draft_ctx"]:
                notes.append(f"[maya] drafter left out: {ctx:,} context needs its VRAM "
                             f"(fits up to {fit['draft_ctx']:,} beside it)")
                drafter = ""
            if 32768 <= fit["plain_ctx"] < ctx:
                notes.append(f"[maya] context {ctx:,} doesn't fit with every expert in VRAM "
                             f"({free:.1f} GB free): starting at {fit['plain_ctx']:,}")
                ctx = fit["plain_ctx"]

        mdir = DATA / "maya"
        mdir.mkdir(parents=True, exist_ok=True)
        engine_log = mdir / f"engine-{port}.log"
        try:
            engine_log.write_text("", encoding="utf-8")
        except OSError:
            pass
        env_ik2 = {"IK2_KV": "fp8", "IK2_GATE_TILE": "64", "IK2_GATE_BF16": "1",
                   "IK2_Q8_UNROLL": "1", "IK2_BATCH_PREFILL": "1"}
        if drafter:
            env_ik2["IK2_DRAFT_CTX"] = str(_MAYA_DRAFT_CTX)
        cfg = {
            "exe": maya["engine"],
            "args": ["--model", model_path, "--max-context", str(ctx)] + (["--draft", drafter] if drafter else []),
            "env": env_ik2,
            "tokenizer": tok_dir,
            "model_name": Path(model_path).stem,
            "log": str(engine_log),
        }
        if mmproj_path:
            # Resident: the encoder stays loaded, so only the first picture pays
            # its start. Every CPU thread: 9 s instead of 17 s for a big image
            # at mtmd's default of 4 (Ryzen 9800X3D).
            cfg["vision"] = {"exe": maya["vision"], "mmproj": mmproj_path, "model": model_path,
                             "gpu": vision_gpu, "resident": True, "threads": os.cpu_count() or 8}
        cfg_path = mdir / f"launch-{port}.json"
        cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        notes.append(f"[maya] {Path(model_path).name}: context {ctx:,}, drafter "
                     f"{os.path.basename(drafter) if drafter else 'off'}, images "
                     f"{'on the GPU' if vision_gpu else 'on the CPU' if mmproj_path else 'off'}")
        print(notes[-1], file=sys.stderr)
        cmd = _maya_python_cmd(maya, maya["server"], "--engine", "maya", "--config", str(cfg_path),
                               "--host", "127.0.0.1", "--port", str(port))
        env = _child_env({"PATH": os.pathsep.join(_maya_cuda_dirs(maya) + [os.environ.get("PATH", "")]),
                          "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                          # the installed package's own Python libraries (jinja2, regex)
                          "PYTHONPATH": maya.get("pythonpath") or None},
                         keep=("gpu",))

        def _engine_last_words() -> None:
            # The engine writes to its own log, not the server's output: its
            # last line is usually why it stopped.
            try:
                lines = [ln.strip() for ln in engine_log.read_text(encoding="utf-8", errors="replace")
                         .splitlines() if ln.strip()]
            except OSError:
                lines = []
            if lines:
                self._append_log(f"[maya engine] {lines[-1]}")

        with self._lock:
            self._maya_launch = {"ctx": ctx, "drafter": drafter, "vision_gpu": vision_gpu}

        def _ready(base_url: str, live_ctx: int) -> None:
            with self._lock:
                pid = self._proc.pid if self._proc is not None else 0
            gb = _trim_maya_engine_memory(pid)
            if gb >= 0.1:
                self._append_log(f"[accuretta] maya ready at {live_ctx:,} context; "
                                 f"gave {gb:.1f} GB of the model file's RAM back to Windows")

        return self._spawn_and_track(
            cmd, cwd=maya["root"], env=env, model_path=model_path, mmproj_path=mmproj_path,
            port=port, ctx=ctx, wait=wait, wait_seconds=wait_seconds, _from_watchdog=_from_watchdog,
            port_override=port_override, mmproj_override=mmproj_override,
            ctx_override=ctx_override, ngl_override=ngl_override,
            engine="maya", on_ready=_ready, on_fail=_engine_last_words, notes=notes)

    @staticmethod
    def _maya_free_after_stop(stopped: bool) -> float:
        """Free VRAM (GB) once a stopped model's memory is back: Windows frees
        it a moment after the process ends, so wait for two equal readings.
        0 when it can't be read."""
        if stopped:
            time.sleep(1.0)
        last = None
        for _ in range(20):
            try:
                det = detect_vram_gb()
                now = float(det.get("free_total_gb") or det.get("gb") or 0)
            except Exception:
                return 0.0
            if now <= 0:
                return 0.0
            if last is not None and abs(now - last) < 0.05:
                return now
            last = now
            time.sleep(0.5)
        return last or 0.0


_llama = LlamaProcess()
