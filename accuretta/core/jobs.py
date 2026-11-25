# SPDX-License-Identifier: AGPL-3.0-only
"""A Windows job object that ends child processes when Accuretta closes.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import subprocess  # only for the Popen type hint
import sys
import threading


class _KillOnCloseJob:
    """Windows job object that ties llama-server's lifetime to the bridge.

    atexit cleanup only runs on a clean interpreter exit. Closing the desktop
    app mid-boot, a crash or Task Manager skipped it and left llama-server
    holding port 8080 and the VRAM, so the next launch refused to start a
    model. With KILL_ON_JOB_CLOSE, Windows kills every assigned process the
    moment the last handle to the job closes, i.e. when this process ends.
    Best effort: elsewhere, or if Windows refuses the assignment, the atexit
    path still applies.

    With other limit flags (and quiet=True) the same class makes a plain
    grouping job: see _agent_job."""

    _KILL_ON_JOB_CLOSE = 0x2000
    _BREAKAWAY_OK = 0x0800
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, limit_flags: int = _KILL_ON_JOB_CLOSE, quiet: bool = False):
        self._lock = threading.Lock()
        self._handle = None
        self._failed = sys.platform != "win32"
        self._limit_flags = limit_flags
        self._quiet = quiet

    def _create(self):
        import ctypes
        from ctypes import wintypes

        class _BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimits),
                        ("IoInfo", _IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.IsProcessInJob.restype = wintypes.BOOL
        k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = self._limit_flags
        if not k32.SetInformationJobObject(handle, self._EXTENDED_LIMIT_INFORMATION,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
        self._k32 = k32
        # Deliberately never closed: the OS closes it when this process ends,
        # which is exactly the moment the children must die.
        self._handle = handle

    def assign(self, proc: "subprocess.Popen") -> bool:
        with self._lock:
            if self._failed:
                return False
            try:
                if self._handle is None:
                    self._create()
                if self._k32.AssignProcessToJobObject(self._handle, int(proc._handle)):
                    return True
                if not self._quiet:
                    import ctypes
                    print(f"[llama] could not tie llama-server to the app (error "
                          f"{ctypes.get_last_error()}); it relies on exit cleanup instead",
                          file=sys.stderr)
            except Exception as e:
                self._failed = True
                if not self._quiet:
                    print(f"[llama] kill-on-close job unavailable: {e}", file=sys.stderr)
            return False

    def contains(self, pid: int) -> bool:
        """Is the running process `pid` in this job? False when it can't be told."""
        if self._handle is None or self._failed:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            h = self._k32.OpenProcess(0x1000, False, int(pid))     # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                inside = wintypes.BOOL(False)
                return bool(self._k32.IsProcessInJob(h, self._handle, ctypes.byref(inside))
                            and inside.value)
            finally:
                self._k32.CloseHandle(h)
        except Exception:
            return False
