# SPDX-License-Identifier: AGPL-3.0-only
"""The long-lived Ghidra worker process for binary analysis, and its environment.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ghidra_worker
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.security.child_env import _child_env
from queue import Empty
from queue import Queue


def _ghidra_worker_env(env_extra: dict) -> dict:
    """The worker's settings, Ghidra's own install variable (pyghidra falls
    back to it) and the Java/Python paths it needs. Nothing else."""
    extra = {}
    install = os.environ.get("GHIDRA_INSTALL_DIR")
    if install:
        extra["GHIDRA_INSTALL_DIR"] = install
    extra.update(env_extra or {})
    return _child_env(extra, keep=("dev",))


class _GhidraWorker:
    """Runs ghidra_worker.py on demand and talks to it over pipes.

    One analysis at a time. Started on the first request, it exits by itself
    after an idle spell (or when the bridge goes away and its stdin closes), so
    Ghidra's memory comes back once it is done. A timed-out analysis is killed,
    not left running. A heap-limit change takes effect on the next request."""

    BOOT_TIMEOUT_S = 180
    LOG_CAP = 5_000_000

    def __init__(self, cmd: list[str] | None = None):
        self._cmd = cmd                      # tests swap in a fake worker
        self._proc: subprocess.Popen | None = None
        self._answers: Queue = Queue()
        self._lock = threading.Lock()
        self._next_id = 0
        self._heap_mb = 0

    def _command(self) -> list[str]:
        if self._cmd:
            return self._cmd
        python = sys.executable
        if getattr(sys, "frozen", False):
            # In the packaged app sys.executable is Accuretta.exe itself;
            # starting it again would open a second app, not the worker.
            python = shutil.which("python") or shutil.which("py") or ""
            if not python:
                raise RuntimeError("Ghidra analysis needs Python with pyghidra installed and on PATH.")
        return [python, "-u", str(ROOT / "ghidra_worker.py")]

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass

    @staticmethod
    def _pump(proc: subprocess.Popen, answers: Queue) -> None:
        mark = ghidra_worker.MARK
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(mark):
                    try:
                        answers.put(json.loads(line[len(mark):]))
                    except ValueError:
                        pass
        except Exception:
            pass
        answers.put({"exited": True})

    def _start(self, env_extra: dict, heap_mb: int) -> str | None:
        answers: Queue = Queue()
        self._answers = answers
        log_path = DATA / "ghidra_worker.log"
        try:
            if log_path.exists() and log_path.stat().st_size > self.LOG_CAP:
                log_path.replace(log_path.with_name(log_path.name + ".1"))
            log = open(log_path, "ab")
        except OSError:
            log = subprocess.DEVNULL
        try:
            self._proc = subprocess.Popen(
                self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                env=_ghidra_worker_env(env_extra), cwd=str(ROOT),
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
        except Exception as e:
            return f"could not start the Ghidra worker: {type(e).__name__}: {e}"
        finally:
            if log is not subprocess.DEVNULL:
                log.close()
        threading.Thread(target=self._pump, args=(self._proc, answers),
                         name="ghidra-worker-out", daemon=True).start()
        try:
            msg = answers.get(timeout=self.BOOT_TIMEOUT_S)
        except Empty:
            self.stop()
            return f"Ghidra took over {self.BOOT_TIMEOUT_S}s to start"
        if msg.get("ready"):
            self._heap_mb = heap_mb
            return None
        self.stop()
        return msg.get("fatal") or "the Ghidra worker exited while starting (see data/ghidra_worker.log)"

    def analyze(self, request: dict, timeout: float, env_extra: dict, heap_mb: int) -> dict:
        with self._lock:
            for attempt in (1, 2):
                if self.alive() and self._heap_mb != heap_mb:
                    self.stop()                     # the memory limit changed
                if not self.alive():
                    err = self._start(env_extra, heap_mb)
                    if err:
                        return {"error": err}
                self._next_id += 1
                rid = self._next_id
                try:
                    self._proc.stdin.write((json.dumps({**request, "id": rid}) + "\n").encode("utf-8"))
                    self._proc.stdin.flush()
                except (OSError, ValueError):
                    self.stop()                     # it exited (idle) as we wrote
                    continue
                deadline = time.time() + timeout
                while True:
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        self.stop()
                        return {"error": (
                            f"ghidra analysis timed out after {int(timeout)}s and was stopped, so it is "
                            f"no longer using memory or CPU. Try a larger timeout, or skip "
                            f"decompile=true and run a smaller follow-up.")}
                    try:
                        msg = self._answers.get(timeout=min(remaining, 5.0))
                    except Empty:
                        continue
                    if msg.get("exited"):
                        proc, self._proc = self._proc, None
                        code = proc.poll() if proc is not None else None
                        if code == 0 and attempt == 1:
                            break                   # idle exit raced our request: retry once
                        return {"error": (
                            "the Ghidra worker stopped mid-analysis (often out of memory). Raise "
                            "Settings > Ghidra memory limit, or analyse a smaller binary. "
                            "Details: data/ghidra_worker.log")}
                    if msg.get("id") == rid:
                        if "result" in msg and isinstance(msg["result"], dict):
                            return msg["result"]
                        return {"error": f"ghidra analysis failed: {msg.get('error')}"}
            return {"error": "the Ghidra worker could not take the request"}


_GHIDRA_WORKER = _GhidraWorker()
