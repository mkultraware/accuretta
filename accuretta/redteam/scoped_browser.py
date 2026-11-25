# SPDX-License-Identifier: AGPL-3.0-only
"""The red-team browser worker: a sandboxed Chromium whose every request is checked
against the mission scope.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.core.optional_imports import _HAVE_PLAYWRIGHT
from accuretta.core.optional_imports import _sync_playwright
from accuretta.core.paths import DATA
from accuretta.redteam.governor import _rt_gov_admit
from accuretta.redteam.governor import _rt_gov_wait_turn
from accuretta.redteam.mission import _rt_url_host
from accuretta.redteam.network_scope import _rt_resolves_to_local
from accuretta.redteam.scope import _rt_tok_matches
from queue import Empty
from queue import Queue
from typing import Any


def _rt_browser_url_block(url: str, policy: dict | None) -> str | None:
    """Return a refusal reason for a browser network request outside policy."""
    if not policy:
        return "browser session has no active authorization policy"
    raw = str(url or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except Exception:
        return "browser request has an invalid URL"
    scheme = parsed.scheme.lower()
    if scheme in {"about", "data", "blob"}:
        return None
    if scheme not in {"http", "https", "ws", "wss"}:
        return f"browser scheme {scheme or '(missing)'} is not allowed"
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return "browser request has no hostname"
    try:
        port = parsed.port
    except ValueError:
        return "browser request has an invalid port"
    if port is None:
        port = 443 if scheme in {"https", "wss"} else 80
    if port in set(policy.get("self_ports") or ()) and _rt_resolves_to_local(host):
        return f"browser request targets Accuretta's own service at {host}:{port}"
    denied = tuple(policy.get("denied") or ())
    if any(_rt_tok_matches(token, host, port) for token in denied):
        return f"browser request targets out-of-scope host {host}:{port}"
    allowed = tuple(policy.get("allowed") or ())
    if not any(_rt_tok_matches(token, host, port) for token in allowed):
        return f"browser request targets {host}:{port}, outside the authorized allowlist"
    return None


def _rt_browser_session_name(value: Any) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "default"))[:64]
    return name or "default"


# Untrusted pages (red-team targets, pages the model renders) open in the
# installed Edge or Chrome: those update themselves, while the Chromium that
# Playwright downloads stays at whatever build came with it and can sit many
# versions behind. Chromium's sandbox is always on (Playwright leaves it off
# unless asked); a browser that cannot start sandboxed is not used at all.
_SAFE_CHROMIUM_ARGS = (
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-sync",
    "--no-default-browser-check",
)


_SAFE_CHROMIUM_CHANNELS = ("msedge", "chrome") if os.name == "nt" else ("chrome", "msedge")


def _launch_safe_chromium(engine):
    """Launch headless, sandboxed Chromium for untrusted pages: the installed
    Edge or Chrome first, Playwright's bundled build only when neither runs."""
    tried = []
    for channel in (*_SAFE_CHROMIUM_CHANNELS, None):
        options = {"headless": True, "args": list(_SAFE_CHROMIUM_ARGS), "chromium_sandbox": True}
        if channel:
            options["channel"] = channel
        try:
            return engine.chromium.launch(**options)
        except Exception as exc:
            first_line = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
            tried.append(f"{channel or 'bundled Chromium'}: {first_line[:200]}")
    raise RuntimeError("No browser could start with its sandbox on. " + " | ".join(tried))


class _ScopedBrowserWorker:
    """Own persistent Playwright objects on one dedicated daemon thread."""

    def __init__(self):
        self._jobs: Queue = Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="accuretta-rt-browser", daemon=True)
            self._thread.start()

    def call(self, payload: dict) -> dict:
        if not _HAVE_PLAYWRIGHT or _sync_playwright is None:
            return {
                "error": "Playwright is not installed. Run: pip install playwright, then: python -m playwright install chromium",
                "dependency_missing": "playwright",
            }
        self._ensure_started()
        response: Queue = Queue(maxsize=1)
        self._jobs.put((dict(payload), response))
        timeout = max(5.0, min(float(payload.get("timeout") or 30.0), 45.0)) + 10.0
        try:
            return response.get(timeout=timeout)
        except Empty:
            return {"error": "browser worker timed out", "session": payload.get("session", "default")}

    @staticmethod
    def _append_event(state: dict, event: dict) -> None:
        events = state.setdefault("events", [])
        events.append(event)
        if len(events) > 500:
            del events[:-500]

    def _new_session(self, browser: Any, name: str, policy: dict) -> dict:
        options: dict[str, Any] = {
            "ignore_https_errors": True,
            "accept_downloads": False,
            "service_workers": "block",
        }
        if policy.get("user_agent"):
            options["user_agent"] = policy["user_agent"]
        context = browser.new_context(**options)
        state = {
            "name": name,
            "context": context,
            "page": None,
            "events": [],
            "policy": dict(policy),
            "policy_key": json.dumps(policy, sort_keys=True),
            "last_used": time.monotonic(),
        }

        def route_request(route: Any, request: Any) -> None:
            reason = _rt_browser_url_block(request.url, state["policy"])
            _policy = state["policy"]
            if (not reason and _policy.get("allow_destructive") is False
                    and str(request.method).upper() == "DELETE"):
                reason = "DELETE is disabled for this engagement (non-destructive testing only)"
            if (not reason and str(urllib.parse.urlsplit(request.url).scheme).lower()
                    in {"http", "https"}):
                # The page's own requests count against the engagement budget
                # and land in the ledger, same as every other tool. Fonts,
                # scripts and images it draws with are ledgered but not
                # counted, and the hook never stalls the page (wait=False):
                # navigations wait for their turn before they start instead.
                _asset = (str(request.method).upper() == "GET"
                          and request.resource_type in _RT_BROWSER_ASSET_TYPES)
                reason = _rt_gov_admit(_rt_url_host(request.url), request.method,
                                       url=request.url, mission=_policy,
                                       kind="asset" if _asset else "request", wait=False)
            event = {
                "kind": "request",
                "method": request.method,
                "url": request.url,
                "resource_type": request.resource_type,
                "blocked": bool(reason),
            }
            if reason:
                event["reason"] = reason
            self._append_event(state, event)
            if reason:
                route.abort()
            else:
                route.continue_()

        def record_response(response: Any) -> None:
            self._append_event(state, {
                "kind": "response", "status": response.status, "url": response.url,
            })

        def cancel_download(download: Any) -> None:
            self._append_event(state, {
                "kind": "download", "url": download.url, "blocked": True,
                "reason": "downloads require a separate evidence-safe transfer path",
            })
            try:
                download.cancel()
            except Exception:
                pass

        def activate_page(page: Any) -> None:
            if page not in state.setdefault("pages", []):
                state["pages"].append(page)
                page.on("download", cancel_download)
            state["page"] = page

        def route_websocket(socket_route: Any) -> None:
            reason = _rt_browser_url_block(socket_route.url, state["policy"])
            self._append_event(state, {
                "kind": "websocket", "url": socket_route.url,
                "blocked": bool(reason), **({"reason": reason} if reason else {}),
            })
            if reason:
                socket_route.close(code=1008, reason="outside authorized scope")
            else:
                socket_route.connect_to_server()

        context.route("**/*", route_request)
        if not hasattr(context, "route_web_socket"):
            context.close()
            raise RuntimeError("Playwright 1.48 or newer is required for WebSocket scope enforcement")
        context.route_web_socket("**/*", route_websocket)
        context.add_init_script(
            """(() => {
              for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebTransport']) {
                try {
                  Object.defineProperty(globalThis, name, {
                    value: undefined, writable: false, configurable: false
                  });
                } catch (_) {}
              }
            })();"""
        )
        context.on("response", record_response)
        context.on("page", activate_page)
        page = context.new_page()
        activate_page(page)
        return state

    @staticmethod
    def _snapshot(page: Any, max_chars: int) -> dict:
        text = ""
        try:
            text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            pass
        elements = page.locator(
            "a,button,input,textarea,select,[role=button],[contenteditable=true]"
        ).evaluate_all(
            """els => els.slice(0, 100).map((el) => {
              const esc = (v) => CSS.escape(String(v));
              const tag = el.tagName.toLowerCase();
              let selector = el.id ? `#${esc(el.id)}` : '';
              if (!selector && el.getAttribute('name')) {
                selector = `${tag}[name=\"${esc(el.getAttribute('name'))}\"]`;
              }
              if (!selector) {
                const parts = [];
                let node = el;
                while (node && node.nodeType === 1 && parts.length < 5) {
                  const nodeTag = node.tagName.toLowerCase();
                  const siblings = Array.from(node.parentElement?.children || [])
                    .filter((item) => item.tagName === node.tagName);
                  const nth = siblings.length > 1 ? `:nth-of-type(${siblings.indexOf(node) + 1})` : '';
                  parts.unshift(nodeTag + nth);
                  node = node.parentElement;
                }
                selector = parts.join(' > ');
              }
              return {
                selector,
                tag,
                type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '',
                text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 100),
                placeholder: el.getAttribute('placeholder') || '',
                href: el.href || '',
                disabled: Boolean(el.disabled)
              };
            })"""
        )
        return {
            "url": page.url,
            "title": page.title(),
            "text": text[:max_chars],
            "text_truncated": len(text) > max_chars,
            "elements": elements,
        }

    def _execute(self, browser: Any, sessions: dict, payload: dict) -> dict:
        action = str(payload.get("action") or "snapshot").strip().lower()
        name = _rt_browser_session_name(payload.get("session"))
        policy = dict(payload.get("policy") or {})
        policy_key = json.dumps(policy, sort_keys=True)
        authorization_id = str(policy.get("authorization_id") or "missing-authorization")
        session_key = f"{authorization_id}:{name}"
        now = time.monotonic()
        for stale_key, stale in list(sessions.items()):
            if now - float(stale.get("last_used") or now) <= 1800:
                continue
            try:
                stale["context"].close()
            finally:
                sessions.pop(stale_key, None)
        state = sessions.get(session_key)
        if state and state.get("policy_key") != policy_key:
            try:
                state["context"].close()
            finally:
                sessions.pop(session_key, None)
            state = None
        if action == "close":
            if not state:
                return {"ok": True, "session": name, "closed": False}
            state["context"].close()
            sessions.pop(session_key, None)
            return {"ok": True, "session": name, "closed": True}
        if not state:
            state = self._new_session(browser, name, policy)
            sessions[session_key] = state
        state["last_used"] = now
        page = state["page"]
        timeout_ms = int(max(2.0, min(float(payload.get("timeout") or 20.0), 40.0)) * 1000)
        page.set_default_timeout(timeout_ms)
        # Actions that can load a page wait for the engagement's next paced
        # slot first, so a run of clicks reads like a person using the site.
        if action in {"open", "navigate", "click", "fill", "press"}:
            _rt_gov_wait_turn(policy)

        def finish(result: dict) -> dict:
            blocked = [event for event in state.get("events", []) if event.get("blocked")]
            result["blocked_request_count"] = len(blocked)
            if blocked:
                result["blocked_requests"] = blocked[-10:]
            return result

        try:
            if action in {"open", "navigate"}:
                url = str(payload.get("url") or "").strip()
                if not url:
                    return {"error": "url required", "session": name}
                if "://" not in url:
                    url = "https://" + url
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                result = self._snapshot(
                    page, min(max(int(payload.get("max_chars") or 16000), 1000), 32000))
                result.update({"ok": True, "session": name,
                               "status": response.status if response else None})
                return finish(result)
            if action == "snapshot":
                result = self._snapshot(
                    page, min(max(int(payload.get("max_chars") or 16000), 1000), 32000))
                result.update({"ok": True, "session": name})
                return finish(result)
            if action == "click":
                selector = str(payload.get("selector") or "").strip()
                if not selector:
                    return {"error": "selector required", "session": name}
                before = page.url
                page.locator(selector).first.click(timeout=timeout_ms)
                page.wait_for_timeout(min(int(payload.get("wait_ms") or 600), 5000))
                active_page = state.get("page") or page
                return finish({"ok": True, "session": name, "before_url": before,
                               "url": active_page.url, "title": active_page.title(),
                               "popup_opened": active_page is not page})
            if action == "fill":
                selector = str(payload.get("selector") or "").strip()
                if not selector:
                    return {"error": "selector required", "session": name}
                page.locator(selector).first.fill(str(payload.get("value") or ""), timeout=timeout_ms)
                if payload.get("submit"):
                    page.locator(selector).first.press("Enter", timeout=timeout_ms)
                    page.wait_for_timeout(min(int(payload.get("wait_ms") or 600), 5000))
                active_page = state.get("page") or page
                return finish({"ok": True, "session": name, "url": active_page.url,
                               "filled": selector, "submitted": bool(payload.get("submit"))})
            if action == "press":
                key = str(payload.get("key") or "").strip()
                if not key:
                    return {"error": "key required", "session": name}
                selector = str(payload.get("selector") or "").strip()
                if selector:
                    page.locator(selector).first.press(key, timeout=timeout_ms)
                else:
                    page.keyboard.press(key)
                page.wait_for_timeout(min(int(payload.get("wait_ms") or 400), 5000))
                return finish({"ok": True, "session": name, "url": page.url, "key": key})
            if action == "wait":
                waited = min(max(int(payload.get("wait_ms") or 500), 0), 5000)
                page.wait_for_timeout(waited)
                return finish({"ok": True, "session": name, "url": page.url,
                               "waited_ms": waited})
            if action == "requests":
                limit = min(max(int(payload.get("limit") or 100), 1), 300)
                events = list(state.get("events") or [])[-limit:]
                return {"ok": True, "session": name, "url": page.url,
                        "events": events, "blocked_count": sum(bool(e.get("blocked")) for e in events)}
            if action == "pages":
                pages = [candidate for candidate in state.get("pages", []) if not candidate.is_closed()]
                state["pages"] = pages
                return {"ok": True, "session": name, "pages": [
                    {"index": index, "url": candidate.url, "title": candidate.title(),
                     "active": candidate is state.get("page")}
                    for index, candidate in enumerate(pages)
                ]}
            if action == "select_page":
                pages = [candidate for candidate in state.get("pages", []) if not candidate.is_closed()]
                index = int(payload.get("page_index") or 0)
                if index < 0 or index >= len(pages):
                    return {"error": f"page_index {index} is not available", "session": name}
                state["pages"] = pages
                state["page"] = pages[index]
                return finish({"ok": True, "session": name, "page_index": index,
                               "url": pages[index].url, "title": pages[index].title()})
            if action == "screenshot":
                evidence_dir = DATA / "recon_evidence"
                evidence_dir.mkdir(parents=True, exist_ok=True)
                label = re.sub(r"[^A-Za-z0-9_.-]", "_", str(payload.get("label") or "browser"))[:60]
                host = re.sub(r"[^A-Za-z0-9_.-]", "_", urllib.parse.urlsplit(page.url).netloc or "target")
                path = evidence_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{label}_{host}.png"
                page.screenshot(path=str(path), full_page=bool(payload.get("full_page", True)))
                raw = path.read_bytes()
                return finish({"ok": True, "session": name, "url": page.url,
                               "evidence_saved": str(path),
                               "evidence_sha256": hashlib.sha256(raw).hexdigest(),
                               "bytes": len(raw)})
            return {"error": f"unknown browser action: {action}", "session": name}
        except Exception as exc:
            blocked = [event for event in state.get("events", [])[-25:] if event.get("blocked")]
            return {
                "error": str(exc), "session": name, "url": getattr(page, "url", ""),
                "scope_blocked": bool(blocked), "blocked_requests": blocked[-10:],
            }

    def _run(self) -> None:
        engine = None
        browser = None
        sessions: dict[str, dict] = {}
        while True:
            payload, response = self._jobs.get()
            try:
                if engine is None:
                    engine = _sync_playwright().start()
                    browser = _launch_safe_chromium(engine)
                result = self._execute(browser, sessions, payload)
            except Exception as exc:
                message = str(exc)
                if "Executable doesn't exist" in message or "playwright install" in message.lower():
                    message += " Run: python -m playwright install chromium"
                result = {"error": message, "browser_unavailable": True}
                for state in sessions.values():
                    try:
                        state["context"].close()
                    except Exception:
                        pass
                sessions.clear()
                try:
                    if browser:
                        browser.close()
                    if engine:
                        engine.stop()
                except Exception:
                    pass
                browser = None
                engine = None
            response.put(result)


# Files a browser fetches to draw a page. In scope they are what every visitor
# downloads, so they go in the ledger but do not spend the request budget;
# counting them is what broke pages before (a font over budget, a page half drawn).
_RT_BROWSER_ASSET_TYPES = frozenset({"stylesheet", "script", "font", "image", "media", "manifest"})
