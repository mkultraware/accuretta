# SPDX-License-Identifier: AGPL-3.0-only
"""How much RAM the machine has and has free, and the Ghidra heap that fits.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import sys
from accuretta.core.settings import get_settings
from accuretta.security.child_env import _child_env


def _ghidra_heap_mb(settings: dict | None = None) -> int:
    """Max JVM heap for Ghidra. Settings > Ghidra memory limit (GB) wins; 0 =
    auto: an eighth of system RAM, at least 2 GB (Ghidra's own headless
    default) and at most 8 GB. Java left to itself takes up to a quarter of
    RAM, and it grows into whatever ceiling it gets."""
    s = settings if settings is not None else get_settings()
    try:
        gb = float(s.get("ghidra_max_heap_gb") or 0)
    except (TypeError, ValueError):
        gb = 0.0
    if gb > 0:
        return int(max(0.5, gb) * 1024)
    info = _system_ram_info()
    total_gb = info[0] if info else 16.0
    return int(min(8.0, max(2.0, total_gb / 8.0)) * 1024)


def _system_ram_info():
    """(total_gb, available_gb) of system RAM, or None. Windows via
    GlobalMemoryStatusEx, Linux via /proc/meminfo, macOS via sysctl."""
    try:
        if os.name == "nt":
            import ctypes

            class _MEMSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            m = _MEMSTATUSEX()
            m.dwLength = ctypes.sizeof(_MEMSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                return m.ullTotalPhys / 1e9, m.ullAvailPhys / 1e9
        elif sys.platform == "darwin":
            import subprocess as _sp
            total = int(_sp.check_output(["sysctl", "-n", "hw.memsize"], text=True,
                                         env=_child_env()).strip())
            vm = _sp.check_output(["vm_stat"], text=True, env=_child_env())
            free_pages = 0
            for line in vm.splitlines():
                if line.startswith("Pages free") or line.startswith("Pages inactive"):
                    free_pages += int(line.split(":")[1].strip().rstrip("."))
            return total / 1e9, (free_pages * 16384) / 1e9
        else:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, _, rest = line.partition(":")
                    parts = rest.strip().split()
                    if parts and parts[0].isdigit():
                        info[k.strip()] = int(parts[0])  # kB
            if "MemTotal" in info:
                return info["MemTotal"] / 1e6, info.get("MemAvailable", info.get("MemFree", 0)) / 1e6
    except Exception:
        pass
    return None
