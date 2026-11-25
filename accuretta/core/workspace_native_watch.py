# SPDX-License-Identifier: AGPL-3.0-only
"""Watching workspace folders with the Windows change API, with a scanning fallback.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import threading
from accuretta.core.workspace import _workspace_roots
from accuretta.core.workspace_watch import _WORKSPACE_WATCHERS
from accuretta.core.workspace_watch import _WORKSPACE_WATCH_LOCK
from accuretta.core.workspace_watch import _WORKSPACE_WATCH_STOP
from accuretta.core.workspace_watch import _broadcast_workspace_update_debounced
from pathlib import Path


class _WindowsWorkspaceWatcher:
    """Recursive ReadDirectoryChangesW watcher for one workspace root."""

    def __init__(self, root: str):
        self.root = root
        self._stop = threading.Event()
        self._handle_lock = threading.Lock()
        self._handle = None
        self._thread = threading.Thread(
            target=self._run, name=f"workspace-watch:{Path(root).name}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            cancel_io = kernel32.CancelIoEx
            cancel_io.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
            cancel_io.restype = wintypes.BOOL
            with self._handle_lock:
                handle = self._handle
            if handle:
                cancel_io(handle, None)
        except Exception:
            pass

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create_file.restype = wintypes.HANDLE
        read_changes = kernel32.ReadDirectoryChangesW
        read_changes.argtypes = (
            wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
            wintypes.DWORD, wintypes.LPDWORD, wintypes.LPVOID, wintypes.LPVOID)
        read_changes.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        invalid = ctypes.c_void_p(-1).value
        notify_filter = 0x00000001 | 0x00000002 | 0x00000008 | 0x00000010 | 0x00000040
        while not self._stop.is_set() and not _WORKSPACE_WATCH_STOP.is_set():
            handle = create_file(
                self.root, 0x0001, 0x00000001 | 0x00000002 | 0x00000004,
                None, 3, 0x02000000, None)
            if not handle or handle == invalid:
                if self._stop.wait(10):
                    return
                continue
            with self._handle_lock:
                self._handle = handle
            try:
                buffer = ctypes.create_string_buffer(65536)
                while not self._stop.is_set() and not _WORKSPACE_WATCH_STOP.is_set():
                    returned = wintypes.DWORD()
                    ok = read_changes(
                        handle, buffer, len(buffer), True, notify_filter,
                        ctypes.byref(returned), None, None)
                    if self._stop.is_set() or _WORKSPACE_WATCH_STOP.is_set():
                        break
                    if ok:
                        # A zero-byte success means the kernel buffer overflowed.
                        # Refresh anyway; the slow scanner below repairs any gap.
                        _broadcast_workspace_update_debounced()
                    else:
                        break
            finally:
                with self._handle_lock:
                    self._handle = None
                close_handle(handle)
            if not self._stop.is_set():
                self._stop.wait(1)


def _workspace_native_supervisor() -> None:
    while not _WORKSPACE_WATCH_STOP.is_set():
        wanted = _workspace_roots()
        with _WORKSPACE_WATCH_LOCK:
            current = set(_WORKSPACE_WATCHERS)
            for root in current - wanted:
                watcher = _WORKSPACE_WATCHERS.pop(root)
                watcher.stop()
            for root in wanted:
                watcher = _WORKSPACE_WATCHERS.get(root)
                if watcher is None or not watcher.is_alive():
                    if watcher is not None:
                        watcher.stop()
                    watcher = _WindowsWorkspaceWatcher(root)
                    _WORKSPACE_WATCHERS[root] = watcher
                    watcher.start()
        _WORKSPACE_WATCH_STOP.wait(2)


def _workspace_fallback_scanner() -> None:
    """Slow repair pass for unsupported filesystems and lost native events."""
    snapshot: dict[str, tuple[float, int]] = {}
    skip_dirs = {
        ".git", ".hg", ".svn", ".idea", ".vscode", "node_modules",
        "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        ".venv", "venv", ".tox", "dist", "build", ".next", ".nuxt",
        ".cache", "coverage", "target",
    }
    first_scan = True
    while not _WORKSPACE_WATCH_STOP.is_set():
        try:
            current: dict[str, tuple[float, int]] = {}
            for root in _workspace_roots():
                for dirpath, dirnames, filenames in os.walk(root):
                    dirnames[:] = [d for d in dirnames
                                   if not d.startswith(".") and d.lower() not in skip_dirs]
                    for filename in filenames:
                        name = filename.lower()
                        if name.startswith(".") or name.endswith((".pyc", ".log")):
                            continue
                        path = Path(dirpath) / filename
                        try:
                            stat = path.stat()
                            current[str(path)] = (stat.st_mtime, stat.st_size)
                        except Exception:
                            continue
            if not first_scan and current != snapshot:
                _broadcast_workspace_update_debounced()
            snapshot = current
            first_scan = False
        except Exception:
            pass
        _WORKSPACE_WATCH_STOP.wait(60)


def _start_workspace_watching() -> None:
    _WORKSPACE_WATCH_STOP.clear()
    threading.Thread(
        target=_workspace_fallback_scanner, name="workspace-safety-scan", daemon=True).start()
    if os.name == "nt":
        threading.Thread(
            target=_workspace_native_supervisor, name="workspace-watch-supervisor",
            daemon=True).start()
