# SPDX-License-Identifier: AGPL-3.0-only
"""Browser fingerprint profiles for web fetches: one kept per host when possible,
another on retry.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import threading
import time


# Each profile: family + ua + sec-ch-ua tuple (Chromium only). Versions from
# 2025-Q2; refresh periodically. Desktop only: some sites serve a degraded
# mobile version that breaks our HTML parsers.
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


# Per-host stickiness: a profile that worked for a host is reused for ~10
# minutes, so the UA doesn't change mid-session.
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
