# SPDX-License-Identifier: AGPL-3.0-only
"""Downloading the sandbox's Ubuntu root filesystem with live progress.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import threading
import time
import urllib.error
import urllib.parse
import urllib.request


# Ubuntu cloud rootfs (URLs verified live). `.tar.xz` - we decompress to `.tar`
# before import because `wsl --import` does not reliably accept xz. Tried in
# order; first reachable one wins.
SANDBOX_ROOTFS_URLS = [
    "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64-root.tar.xz",
    "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64-root.tar.xz",
    "https://cloud-images.ubuntu.com/releases/22.04/release/ubuntu-22.04-server-cloudimg-amd64-root.tar.xz",
]


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
