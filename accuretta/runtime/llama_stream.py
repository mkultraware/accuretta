# SPDX-License-Identifier: AGPL-3.0-only
"""Talking to llama-server: a stream that gives up when it stalls, and readable errors.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json


class _StallGuardedStream:
    """Response wrapper that applies the tight inter-token timeout only after the
    first chunk. llama-server sends headers as soon as it accepts a request and
    then prefills, possibly for minutes, before the first SSE event; urlopen
    returns at the headers, so the first readline() keeps the generous
    first-token window."""
    def __init__(self, resp, stall_t: float):
        self._resp = resp
        self._stall_t = stall_t
        self._started = False

    def __iter__(self):
        return self

    def __next__(self):
        line = self._resp.readline()
        if line == b"":
            raise StopIteration
        if not self._started:
            self._started = True
            try:
                self._resp.fp.raw._sock.settimeout(self._stall_t)
            except Exception:
                pass
        return line

    def close(self):
        try:
            self._resp.close()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._resp, name)


def _llama_error_text(body: str) -> str:
    """llama-server's own message from a JSON error body ({"error": {"message":
    ...}}), so the chat shows the sentence instead of the raw JSON. Anything
    that isn't that shape comes back as the body itself, on one line."""
    text = (body or "").strip()
    try:
        obj = json.loads(text)
    except Exception:
        return text.replace("\n", " ")
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict) and str(err.get("message") or "").strip():
        return str(err["message"]).strip().replace("\n", " ")
    if isinstance(err, str) and err.strip():
        return err.strip().replace("\n", " ")
    if isinstance(obj, dict) and str(obj.get("message") or "").strip():
        return str(obj["message"]).strip().replace("\n", " ")
    return text.replace("\n", " ")


def _is_llama_unreachable(e: Exception) -> bool:
    """True when llama-server is not answering at all (down or restarting:
    connection refused, port closed), as opposed to a live server returning an
    error. The watchdog heals that window in seconds, so a turn can retry
    instead of dying instantly."""
    import urllib.error
    if isinstance(e, urllib.error.HTTPError):
        return False
    blob = str(e).lower()
    reason = getattr(e, "reason", None)
    return ("connection refused" in blob
            or "10061" in blob
            or "actively refused" in blob
            or isinstance(reason, ConnectionRefusedError)
            or "connectionreseterror" in blob.lower()
            or "name or service not known" in blob)
