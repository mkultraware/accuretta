# SPDX-License-Identifier: AGPL-3.0-only
"""The anonymous release check: the app's version, the latest GitHub release,
and the installer download when there is one.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.paths import DATA
from accuretta.core.paths import ROOT
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.settings import _offline_active


UPDATE_CHECK_FILE = DATA / "update_check.json"


# ---- public release checks -------------------------------------------------
# This is intentionally anonymous. One bridge instance checks GitHub and every
# browser connected to it reads the same cached result.
_UPDATE_CHECK_LOCK = threading.Lock()


_UPDATE_AUTO_TTL_S = 12 * 60 * 60


_UPDATE_MANUAL_COOLDOWN_S = 5 * 60


_UPDATE_ERROR_RETRY_S = 30 * 60


_UPDATE_REPO = "mkultraware/accuretta"


_UPDATE_API_URL = f"https://api.github.com/repos/{_UPDATE_REPO}/releases/latest"


_UPDATE_RELEASES_URL = f"https://github.com/{_UPDATE_REPO}/releases/latest"


def _app_version() -> str:
    """Read the visible app version so the splash/UI remains the source."""
    try:
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        match = re.search(r'id=["\']brand-v["\'][^>]*>\s*v?([^<\s]+)', html)
        if match:
            return match.group(1)
    except Exception:
        pass
    return "0.0.0"


def _version_key(value: str) -> tuple:
    """Comparable SemVer-like key. Stable releases sort after prereleases."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)(?:[-+]([^\s]+))?", str(value or ""))
    if not match:
        return (0, 0, 0, 0, "")
    major, minor, patch = (int(match.group(i)) for i in range(1, 4))
    prerelease = match.group(4) or ""
    return (major, minor, patch, 1 if not prerelease else 0, prerelease)


def _release_summary(body: str, name: str, tag: str) -> str:
    """Return one short plain-text line from GitHub release notes."""
    for raw in str(body or "").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "![", "<!--")):
            continue
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", line)
        line = re.sub(r"[`*_~>]", "", line).strip()
        if line:
            return line[:177].rstrip() + ("..." if len(line) > 177 else "")
    clean_name = str(name or "").strip()
    if clean_name and clean_name.lower() != str(tag or "").strip().lower():
        return clean_name[:180]
    return "A new Accuretta release is ready."


def _installer_info() -> dict | None:
    """The Windows installer writes install.json next to the app folder
    (`<install dir>\\install.json`, the app in `<install dir>\\app`). Returns
    its contents for an installer build, None for a git checkout or a zip."""
    try:
        info = json.loads((ROOT.parent / "install.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) and info.get("type") == "installer" else None


_INSTALLER_ASSET_RE = re.compile(r"^Accuretta-Setup-[\w.+-]+\.exe$")


def _update_response(cache: dict, *, cached: bool, now: float) -> dict:
    current = _app_version()
    release = cache.get("release") if isinstance(cache.get("release"), dict) else None
    latest = (release or {}).get("version") or ""
    return {
        "ok": not bool(cache.get("error")),
        "install_type": "installer" if _installer_info() else "source",
        "current_version": current,
        "available": bool(release and _version_key(latest) > _version_key(current)),
        "release": release,
        "cached": cached,
        "checked_at": int(cache.get("checked_at") or 0),
        "next_check_at": int(cache.get("next_allowed_at") or 0),
        "rate_limited": bool(cache.get("rate_limited")),
        "error": str(cache.get("error") or ""),
        "server_time": int(now),
    }


def app_update_status(manual: bool = False, now: float | None = None) -> dict:
    """Check the latest public GitHub release with a durable rate-safe cache."""
    now = float(time.time() if now is None else now)
    if _offline_active:
        return {"ok": True, "available": False, "offline": True, "current_version": _app_version()}
    with _UPDATE_CHECK_LOCK:
        cache = load_json(UPDATE_CHECK_FILE, {})
        if not isinstance(cache, dict):
            cache = {}

        next_allowed = float(cache.get("next_allowed_at") or 0)
        if next_allowed > now:
            return _update_response(cache, cached=True, now=now)

        last_request = float(cache.get("last_request_at") or 0)
        checked_at = float(cache.get("checked_at") or 0)
        if manual and last_request and now - last_request < _UPDATE_MANUAL_COOLDOWN_S:
            return _update_response(cache, cached=True, now=now)
        if not manual and checked_at and now - checked_at < _UPDATE_AUTO_TTL_S:
            return _update_response(cache, cached=True, now=now)

        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Accuretta/{_app_version()}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if cache.get("etag"):
            headers["If-None-Match"] = str(cache["etag"])
        request = urllib.request.Request(_UPDATE_API_URL, headers=headers)
        cache["last_request_at"] = int(now)
        try:
            with urllib.request.urlopen(request, timeout=6.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
                tag = str(payload.get("tag_name") or "").strip()
                version_match = re.search(r"\d+\.\d+\.\d+(?:[-+][^\s]+)?", tag)
                if not version_match:
                    raise ValueError("latest release has no valid version tag")
                version = version_match.group(0)
                release_url = str(payload.get("html_url") or "")
                expected_prefix = f"https://github.com/{_UPDATE_REPO}/releases/"
                if not release_url.startswith(expected_prefix):
                    release_url = _UPDATE_RELEASES_URL
                # The Windows installer for this release, when one is attached.
                installer_url = ""
                for asset in payload.get("assets") or []:
                    if not isinstance(asset, dict):
                        continue
                    url = str(asset.get("browser_download_url") or "")
                    if (_INSTALLER_ASSET_RE.match(str(asset.get("name") or ""))
                            and url.startswith(f"https://github.com/{_UPDATE_REPO}/releases/download/")):
                        installer_url = url
                        break
                cache.update({
                    "checked_at": int(now),
                    "next_allowed_at": 0,
                    "rate_limited": False,
                    "error": "",
                    "etag": response.headers.get("ETag") or "",
                    "release": {
                        "version": version,
                        "tag": tag,
                        "name": str(payload.get("name") or tag)[:180],
                        "summary": _release_summary(payload.get("body") or "", payload.get("name") or "", tag),
                        "url": release_url,
                        "installer_url": installer_url,
                        "published_at": str(payload.get("published_at") or ""),
                    },
                })
        except urllib.error.HTTPError as exc:
            if exc.code == 304 and cache.get("release"):
                cache.update({"checked_at": int(now), "next_allowed_at": 0,
                              "rate_limited": False, "error": ""})
            elif exc.code == 404:
                # A valid public repository with no published release yet.
                cache.update({"checked_at": int(now), "next_allowed_at": 0,
                              "rate_limited": False, "error": "", "release": None})
            elif exc.code in (403, 429):
                retry_after = exc.headers.get("Retry-After")
                reset_at = exc.headers.get("X-RateLimit-Reset")
                try:
                    blocked_until = now + max(1, int(retry_after)) if retry_after else float(reset_at or 0)
                except (TypeError, ValueError):
                    blocked_until = 0
                if blocked_until <= now:
                    blocked_until = now + 60 * 60
                cache.update({
                    "next_allowed_at": int(blocked_until),
                    "rate_limited": True,
                    "error": "GitHub rate limit reached",
                })
            else:
                cache.update({"next_allowed_at": int(now + _UPDATE_ERROR_RETRY_S),
                              "rate_limited": False, "error": f"GitHub returned {exc.code}"})
        except Exception as exc:
            cache.update({"next_allowed_at": int(now + _UPDATE_ERROR_RETRY_S),
                          "rate_limited": False, "error": str(exc)[:180]})

        save_json(UPDATE_CHECK_FILE, cache)
        return _update_response(cache, cached=False, now=now)
