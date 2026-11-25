# SPDX-License-Identifier: AGPL-3.0-only
"""Optional packages: imported when installed, None (with a _HAVE_ flag) when
not. One place for every optional import, so any layer can use them.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import sys


# ---- scoped browser automation (optional) ---------------------------------
# Playwright runs on a dedicated worker thread because its synchronous API is
# thread-affine. Red-team browser sessions never share the generic MCP path.
try:
    from playwright.sync_api import sync_playwright as _sync_playwright  # type: ignore
    _HAVE_PLAYWRIGHT = True
except Exception:
    _sync_playwright = None  # type: ignore
    _HAVE_PLAYWRIGHT = False


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
    from PIL import Image  # type: ignore
    _HAVE_PIL = True
except Exception:
    Image = None  # type: ignore
    _HAVE_PIL = False


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
    import uiautomation as _uia  # type: ignore
    _HAVE_UIA = sys.platform == "win32"
except Exception:
    _uia = None  # type: ignore
    _HAVE_UIA = False


# ---- firmware analysis (optional) ----------------------------------------
# Signature scanning is pure Python (the pip "binwalk" package is an unrelated
# stub). PySquashfsImage extracts squashfs and pyelftools parses ELF headers;
# the tools report a missing dependency themselves.
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
# Ghidra runs in its own process (ghidra_worker.py, started on first use) with
# a capped JVM heap; it exits when idle, so the bridge never holds a JVM.
# Requires pyghidra, JDK 21+ and a Ghidra install (settings.ghidra_path or
# $GHIDRA_INSTALL_DIR). Only presence is checked here: nothing is imported.
try:
    import importlib.util as _ilu
    _HAVE_PYGHIDRA = _ilu.find_spec("pyghidra") is not None
except Exception:
    _HAVE_PYGHIDRA = False


try:
    import dpkt as _dpkt  # type: ignore
    _HAVE_DPKT = True
except Exception:
    _dpkt = None  # type: ignore
    _HAVE_DPKT = False
