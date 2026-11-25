# SPDX-License-Identifier: AGPL-3.0-only
"""Red-team test identities: the accounts and headers a mission may use, cleaned,
saved per mission, and applied to requests.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re
import threading
import traceback
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.redteam.mission import _rt_authorized_mission
from accuretta.redteam.mission import _rt_context_chat
from accuretta.redteam.request_headers import _RT_HEADER_NAME_RE
from typing import Any


# ---- engagement test accounts (Access Control playbook) ----------------------
# The questionnaire collects a sign-in (a cookie or an auth header) for two test
# accounts. The values stay out of chats.json and out of the model's context:
# the mission record lists only the labels, the values live in their own file
# keyed by the engagement's authorization id, and the model names an account
# ("A", "B" or "anonymous") for the bridge to attach. They are deleted when the
# engagement ends.
_RT_IDENTITY_LABELS = ("A", "B")


_RT_IDENTITY_ANON = "anonymous"


_RT_IDENTITY_HEADER_MAX = 6


_RT_IDENTITY_VALUE_MAX = 8192


# Headers that describe the request rather than who is signed in. A sign-in may
# not set them: the engagement, the tool or the HTTP client owns each one.
_RT_IDENTITY_BLOCKED_HEADERS = {
    "host", "content-length", "content-type", "transfer-encoding", "connection",
    "user-agent", "accept-encoding",
}


_RT_IDENTITY_ALIASES = {
    "anonymous", "anon", "none", "guest", "public", "unauthenticated",
    "signed out", "logged out", "no auth", "not signed in", "no account",
}


_RT_IDENTITIES_FILE = DATA / "redteam" / "identities.json"


_RT_IDENTITIES_LOCK = threading.Lock()


# (authorization id, label) -> when the target last answered that account
# differently from a signed-out request, i.e. the sign-in was seen to work.
_RT_IDENTITY_SEEN: dict[tuple[str, str], float] = {}


def _rt_clean_identity_headers(value: Any) -> dict:
    """One account's sign-in headers from the gate panel (bounded, latin-1 safe)."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in list(value.items())[:_RT_IDENTITY_HEADER_MAX * 3]:
        name = str(key or "").strip()
        text = str(raw or "").strip()
        if (not _RT_HEADER_NAME_RE.match(name) or name.lower() in _RT_IDENTITY_BLOCKED_HEADERS
                or not text or len(text) > _RT_IDENTITY_VALUE_MAX):
            continue
        if any(ord(char) < 32 or ord(char) == 127 for char in text):
            continue
        try:
            text.encode("latin-1")
        except UnicodeEncodeError:
            continue
        if any(name.lower() == existing.lower() for existing in out):
            continue
        out[name] = text
        if len(out) >= _RT_IDENTITY_HEADER_MAX:
            break
    return out


def _rt_clean_identities(value: Any) -> dict:
    """label -> sign-in headers for every account the panel filled in."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict] = {}
    for label in _RT_IDENTITY_LABELS:
        headers = _rt_clean_identity_headers(value.get(label))
        if headers:
            out[label] = headers
    return out


def _rt_identities_usable(value: Any) -> bool:
    """The Access Control playbook needs both accounts, and they must differ."""
    cleaned = _rt_clean_identities(value)
    if len(cleaned) != len(_RT_IDENTITY_LABELS):
        return False
    lowered = [{k.lower(): v for k, v in cleaned[label].items()} for label in _RT_IDENTITY_LABELS]
    return lowered[0] != lowered[1]


def _rt_clean_owned_urls(value: Any) -> list[str]:
    """URLs (or site-relative paths) the user says belong to account A."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in list(value)[:40]:
        text = str(item or "").strip()
        if not text or len(text) > 400 or any(ord(char) < 33 or ord(char) == 127 for char in text):
            continue
        if not (text.startswith(("http://", "https://")) or (text.startswith("/") and not text.startswith("//"))):
            continue
        if text not in out:
            out.append(text)
        if len(out) >= 10:
            break
    return out


def _rt_identities_read() -> dict:
    stored = load_json(_RT_IDENTITIES_FILE, {})
    return stored if isinstance(stored, dict) else {}


def _rt_identities_save(key: str, identities: dict) -> None:
    with _RT_IDENTITIES_LOCK:
        stored = _rt_identities_read()
        stored[str(key)] = identities
        _RT_IDENTITIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        save_json(_RT_IDENTITIES_FILE, stored)


def _rt_identities_for(mission: dict | None) -> dict:
    """label -> sign-in headers for the accounts a mission was started with."""
    if not isinstance(mission, dict):
        return {}
    labels = [label for label in (mission.get("identities") or []) if label in _RT_IDENTITY_LABELS]
    key = str(mission.get("authorization_id") or "")
    if not labels or not key:
        return {}
    with _RT_IDENTITIES_LOCK:
        cleaned = _rt_clean_identities(_rt_identities_read().get(key))
    return {label: cleaned[label] for label in labels if label in cleaned}


def _rt_active_identities() -> dict:
    """The test accounts of the active, authorized engagement (empty otherwise)."""
    return _rt_identities_for(_rt_authorized_mission(_rt_context_chat()))


def _rt_identities_forget(mission: dict | None) -> None:
    """Delete an engagement's stored sign-ins. Safe to call for any mission."""
    key = str((mission or {}).get("authorization_id") or "") if isinstance(mission, dict) else ""
    if not key:
        return
    for seen in [k for k in _RT_IDENTITY_SEEN if k[0] == key]:
        _RT_IDENTITY_SEEN.pop(seen, None)
    try:
        with _RT_IDENTITIES_LOCK:
            stored = _rt_identities_read()
            if key in stored:
                stored.pop(key, None)
                save_json(_RT_IDENTITIES_FILE, stored)
    except Exception:
        traceback.print_exc()


def _rt_identity_label(value: Any) -> str:
    """Normalize how a model names an account: 'a', 'Account A', 'user_b',
    'signed out'. Returns 'A', 'B', 'anonymous', or '' when unrecognized."""
    text = re.sub(r"[\s_\-]+", " ", str(value or "").strip().lower()).strip(" :'\".")
    if text in _RT_IDENTITY_ALIASES:
        return _RT_IDENTITY_ANON
    text = re.sub(r"^(?:test )?(?:account|user|identity|session)\s*", "", text).strip(" :'\".")
    if text in _RT_IDENTITY_ALIASES:
        return _RT_IDENTITY_ANON
    return text.upper() if text.upper() in _RT_IDENTITY_LABELS else ""


def _rt_identities_missing_reason() -> str:
    """Why no test account can be used right now, in words the user can act on."""
    mission = _rt_authorized_mission(_rt_context_chat()) or {}
    if mission.get("identities"):
        return ("The sign-ins for this engagement's test accounts are no longer stored (an engagement "
                "left open for a week has them deleted). Tell the user to start the Access Control "
                "playbook again with fresh sign-ins.")
    return ("This engagement has no test accounts. Named accounts exist only in an engagement "
            "started from the Access Control playbook.")


def _rt_identity_request(value: Any) -> dict:
    """Resolve a model-supplied account name to {'label', 'headers'} or {'error'}."""
    identities = _rt_active_identities()
    if not identities:
        return {"error": _rt_identities_missing_reason()}
    label = _rt_identity_label(value)
    if label == _RT_IDENTITY_ANON:
        return {"label": label, "headers": {}}
    if label not in identities:
        return {"error": f"Unknown account {str(value)[:40]!r}. Use one of: "
                         + ", ".join([*identities, _RT_IDENTITY_ANON]) + "."}
    return {"label": label, "headers": dict(identities[label])}


def _rt_identity_reserved_headers() -> set[str]:
    """Header names (lowercase) that carry a sign-in in this engagement."""
    names = {"authorization", "cookie"}
    for headers in _rt_active_identities().values():
        names |= {str(name).lower() for name in headers}
    return names
