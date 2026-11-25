# SPDX-License-Identifier: AGPL-3.0-only
"""Headers for red-team and browser-like requests: required mission headers, the
user agent, header rotation and request jitter.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import re
import time
from accuretta.core.settings import get_settings
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from typing import Any


_RT_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _rt_clean_required_headers(value: Any) -> dict:
    """Header-safe name->value map from the gate panel (bounded, latin-1 safe)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in list(value.items())[:10]:
        name = str(key or "").strip()
        text = str(raw or "").strip()
        if not _RT_HEADER_NAME_RE.match(name) or not text or len(text) > 512:
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            continue
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            continue
        out[name] = text
    return out


def _bounded_int(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return low


def _rt_clean_user_agent(value: Any) -> str:
    """Return a header-safe custom User-Agent, or blank when invalid."""
    user_agent = str(value or "").strip()
    if not user_agent or len(user_agent) > 512:
        return ""
    if any(ord(char) < 32 or ord(char) == 127 for char in user_agent):
        return ""
    try:
        user_agent.encode("latin-1")
    except UnicodeEncodeError:
        return ""
    return user_agent


def _rt_required_user_agent() -> str:
    """Custom User-Agent attached to the active, authorized engagement."""
    mission = _rt_authorized_mission(_rt_context_chat())
    return _rt_clean_user_agent((mission or {}).get("user_agent"))


def _rt_apply_mission_user_agent(headers: dict | None = None) -> dict:
    """Force engagement-required headers after model- or profile-supplied ones.

    The engagement User-Agent and any program-required headers (HackerOne
    research headers, program tagging) win over whatever the model or the
    browser profile put in the request, in every HTTP path of the suite.
    """
    result = dict(headers or {})
    mission = _rt_authorized_mission(_rt_context_chat())
    if not mission:
        return result
    user_agent = _rt_clean_user_agent(mission.get("user_agent"))
    if user_agent:
        for key in list(result):
            low = str(key).lower()
            if low == "user-agent" or low.startswith("sec-ch-ua"):
                result.pop(key, None)
        result["User-Agent"] = user_agent
    for name, value in _rt_clean_required_headers(mission.get("required_headers")).items():
        for key in list(result):
            if str(key).lower() == name.lower():
                result.pop(key, None)
        result[name] = value
    return result


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
    return _rt_apply_mission_user_agent(h)


# ---- red-team stealth / anti-fingerprint ------------------------------------
# What a target can fingerprint from our probes: User-Agent, header set, timing
# and source IP. No machine GUID, username or hostname is ever sent by these
# tools - Windows simply doesn't put those in HTTP requests. TLS (JA3) stays
# python-native: for a real browser fingerprint drive the playwright MCP tools.
_RT_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
]


def _rt_ua() -> str:
    return _rt_required_user_agent() or random.choice(_RT_UA_POOL)


def _rt_stealth_headers(base: dict | None = None) -> dict:
    """Browser-like headers with the mission UA, or a rotated UA when unset.

    Honors rt_spoof_xff: random source-IP headers so target logs that trust XFF
    record noise, not you.
    """
    s = get_settings()
    hdrs = dict(base or {})
    hdrs.setdefault("User-Agent", _rt_ua())
    hdrs.setdefault("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8")
    hdrs.setdefault("Accept-Language", random.choice(
        ["en-US,en;q=0.9", "en-GB,en;q=0.8,en;q=0.6", "en-US,en;q=0.7"]))
    if s.get("rt_spoof_xff"):
        fake = ".".join(str(random.randint(1, 254)) for _ in range(4))
        hdrs.setdefault("X-Forwarded-For", fake)
        hdrs.setdefault("X-Real-IP", fake)
    return _rt_apply_mission_user_agent(hdrs)


def _rt_jitter() -> None:
    """Small random delay so automated probes don't fire at machine-regular
    intervals (timing fingerprint). Disable via rt_jitter=false."""
    try:
        if get_settings().get("rt_jitter", True):
            time.sleep(random.uniform(0.05, 0.45))
    except Exception:
        pass
