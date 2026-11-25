# SPDX-License-Identifier: AGPL-3.0-only
"""Running processes on this PC: start times, parents, names, program paths, listening ports.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ctypes
import os


def _process_table() -> dict[int, tuple[int, str]]:
    """{pid: (parent pid, image name lower-case without .exe)}. {} when it
    can't be told."""
    table: dict[int, tuple[int, str]] = {}
    try:
        if os.name == "nt":
            from ctypes import wintypes

            class _Entry(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
            k32 = ctypes.windll.kernel32
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)
            if not snap or snap == wintypes.HANDLE(-1).value:
                return {}
            try:
                entry = _Entry()
                entry.dwSize = ctypes.sizeof(_Entry)
                ok = k32.Process32FirstW(snap, ctypes.byref(entry))
                while ok:
                    name = str(entry.szExeFile or "").lower()
                    table[int(entry.th32ProcessID)] = (int(entry.th32ParentProcessID),
                                                       name[:-4] if name.endswith(".exe") else name)
                    ok = k32.Process32NextW(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
        else:
            for pid in os.listdir("/proc"):
                if not pid.isdigit():
                    continue
                try:
                    with open(f"/proc/{pid}/stat", "r") as f:
                        raw = f.read()
                    name = raw[raw.find("(") + 1:raw.rfind(")")].lower()
                    table[int(pid)] = (int(raw.rsplit(")", 1)[1].split()[1]), name)
                except (OSError, ValueError, IndexError):
                    continue
    except Exception:
        return {}
    return table


def _proc_image_path(pid: int) -> str:
    """Full path of a running process's program, or "" when it can't be read."""
    try:
        if os.name == "nt":
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                       wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return ""
            try:
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    return buf.value
            finally:
                k32.CloseHandle(h)
            return ""
        return os.readlink(f"/proc/{int(pid)}/exe")
    except Exception:
        return ""


def _process_parent_map() -> dict[int, int]:
    """{pid: parent pid} for every running process. {} when it can't be told."""
    parents: dict[int, int] = {}
    try:
        if os.name == "nt":
            from ctypes import wintypes

            class _Entry(ctypes.Structure):
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]
            k32 = ctypes.windll.kernel32
            k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            snap = k32.CreateToolhelp32Snapshot(0x2, 0)         # TH32CS_SNAPPROCESS
            if not snap or snap == wintypes.HANDLE(-1).value:
                return {}
            try:
                entry = _Entry()
                entry.dwSize = ctypes.sizeof(_Entry)
                ok = k32.Process32FirstW(snap, ctypes.byref(entry))
                while ok:
                    parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                    ok = k32.Process32NextW(snap, ctypes.byref(entry))
            finally:
                k32.CloseHandle(snap)
        else:
            for name in os.listdir("/proc"):
                if not name.isdigit():
                    continue
                try:
                    with open(f"/proc/{name}/stat", "r") as f:
                        ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                except (OSError, ValueError, IndexError):
                    continue
                parents[int(name)] = ppid
    except Exception:
        return {}
    return parents


def _proc_create_time(pid: int) -> float | None:
    """When the running process `pid` started (epoch seconds), or None if it's gone."""
    try:
        if os.name == "nt":
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(ctypes.c_uint64)] * 4
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, int(pid))         # QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                code = wintypes.DWORD(0)
                if k32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value != 259:
                    return None                                  # exited (not STILL_ACTIVE)
                created, ended, kernel, user = (ctypes.c_uint64() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(ended),
                                           ctypes.byref(kernel), ctypes.byref(user)):
                    return None
                return created.value / 1e7 - 11644473600.0       # FILETIME -> epoch
            finally:
                k32.CloseHandle(h)
        with open(f"/proc/{int(pid)}/stat", "r") as f:
            ticks = int(f.read().rsplit(")", 1)[1].split()[19])
        with open("/proc/stat", "r") as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith("btime "))
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def _local_listeners() -> list[tuple[int, int]]:
    """(port, pid) of every TCP socket listening on loopback or on all addresses."""
    out: list[tuple[int, int]] = []
    try:
        if os.name == "nt":
            from ctypes import wintypes
            fn = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
            fn.restype = wintypes.DWORD
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                           wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
            # AF_INET rows are MIB_TCPROW_OWNER_PID (24 bytes), AF_INET6 rows
            # MIB_TCP6ROW_OWNER_PID (56); class 3 = TCP_TABLE_OWNER_PID_LISTENER.
            for family, row_size in ((2, 24), (23, 56)):
                size = wintypes.DWORD(0)
                fn(None, ctypes.byref(size), False, family, 3, 0)
                raw = b""
                for _ in range(4):
                    buf = ctypes.create_string_buffer(size.value + 4096)
                    size.value = len(buf)
                    rc = fn(buf, ctypes.byref(size), False, family, 3, 0)
                    if rc == 0:
                        raw = buf.raw
                        break
                    if rc != 122:                                # ERROR_INSUFFICIENT_BUFFER
                        break
                count = int.from_bytes(raw[:4], "little") if raw else 0
                for i in range(count):
                    row = raw[4 + i * row_size: 4 + (i + 1) * row_size]
                    if len(row) < row_size:
                        break
                    if family == 2:
                        addr, port, pid = row[4:8], row[8:10], row[20:24]
                        local = addr[0] == 127 or addr == bytes(4)
                    else:
                        addr, port, pid = row[0:16], row[20:22], row[52:56]
                        local = (addr in (bytes(16), bytes(15) + b"\x01")
                                 or (addr[:12] == bytes(10) + b"\xff\xff" and addr[12] == 127))
                    pid_n = int.from_bytes(pid, "little")
                    if local and pid_n:
                        out.append((int.from_bytes(port, "big"), pid_n))
        else:
            import psutil                                        # optional off Windows
            for c in psutil.net_connections(kind="tcp"):
                ip = getattr(c.laddr, "ip", "") if c.laddr else ""
                if (c.status == "LISTEN" and c.pid
                        and (ip in ("0.0.0.0", "::", "::1") or ip.startswith("127."))):
                    out.append((int(c.laddr.port), int(c.pid)))
    except Exception:
        return out
    return out
