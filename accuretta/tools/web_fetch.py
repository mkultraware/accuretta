# SPDX-License-Identifier: AGPL-3.0-only
"""The web tools: web_fetch and web_search, link previews for chat bubbles, and rotating
browser profiles across requests.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from accuretta.redteam.recon_fetch import _ScopeCheckedRedirect
from accuretta.redteam.request_headers import _browser_headers
from accuretta.redteam.request_headers import _rt_apply_mission_user_agent
from accuretta.security.web_taint import _mark_web_taint
from accuretta.redteam.browser_profiles import _PROFILE_BY_HOST
from accuretta.redteam.browser_profiles import _PROFILE_LOCK
from accuretta.redteam.browser_profiles import _pick_profile
from accuretta.tools.link_preview import _link_preview_extract


# ---- Link preview (for clickable links in chat bubbles) ------------------
# In-process cache keyed by URL so repeat hovers don't re-fetch; trimmed
# LRU-style to stay small.
_LINK_PREVIEW_CACHE: dict[str, dict] = {}


_LINK_PREVIEW_CACHE_LOCK = threading.Lock()


_LINK_PREVIEW_CACHE_MAX = 512


def _remember_profile(host: str, profile: dict) -> None:
    if not host:
        return
    with _PROFILE_LOCK:
        _PROFILE_BY_HOST[host] = (profile, time.time())


def _open_with_rotation(url: str, *, timeout: float = 15.0,
                        max_bytes: int = 1024 * 1024,
                        attempts: int = 3,
                        accept_html: bool = True,
                        referer: str | None = None,
                        method: str | None = None,
                        data: bytes | None = None,
                        extra_headers: dict | None = None):
    """Fetch a URL with browser-identity rotation + 403/429 retry.

    Returns (status, content_type, raw_bytes, profile_used). Earlier failures
    are retried with a fresh profile and a small jittered backoff; the final
    attempt's exception is raised. A profile that works for a host is reused for
    `_PROFILE_STICKY_TTL` seconds."""
    try:
        host = urllib.parse.urlparse(url).netloc
    except Exception:
        host = ""

    last_exc: Exception | None = None
    tried: list[dict] = []
    for attempt in range(max(1, attempts)):
        profile = _pick_profile(host, exclude=tried[-1] if tried else None)
        tried.append(profile)
        headers = _browser_headers(profile, referer=referer, accept_html=accept_html)
        if extra_headers:
            headers.update(extra_headers)
        headers = _rt_apply_mission_user_agent(headers)
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            opener = urllib.request.build_opener(_ScopeCheckedRedirect())
            with opener.open(req, timeout=timeout) as resp:
                status = getattr(resp, "status", 200) or 200
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read(max_bytes)
                _remember_profile(host, profile)
                return status, ctype, raw, profile
        except urllib.error.HTTPError as e:
            last_exc = e
            # 401/403/405/429/503 may be about our client shape, so rotate and retry.
            # Other 4xx (404, 410, ...) won't be fixed by a different UA.
            if e.code not in (401, 403, 405, 429, 503):
                raise
            # Drop this host's sticky profile — clearly didn't work.
            with _PROFILE_LOCK:
                _PROFILE_BY_HOST.pop(host, None)
            # Small jittered sleep before the next try (avoids back-to-back
            # spam patterns; also gives Cloudflare's edge a tick to forget).
            time.sleep(0.4 + random.random() * 0.6)
            continue
        except Exception as e:
            last_exc = e
            # Network-level failures (DNS, timeout) probably won't be cured
            # by a UA swap, but one retry doesn't cost much.
            time.sleep(0.3 + random.random() * 0.4)
            continue
    # All attempts exhausted — re-raise the last error.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fetch failed without a recorded exception")


def fetch_link_preview(url: str) -> dict:
    """Fetch a URL, extract Open Graph / standard meta tags, return a small
    dict the frontend can render in a hover card. Cached per-process so a
    user mousing across the same link 10 times only hits the network once."""
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    with _LINK_PREVIEW_CACHE_LOCK:
        cached = _LINK_PREVIEW_CACHE.get(url)
        if cached is not None:
            return cached

    try:
        # Link previews are short-lived hover popovers, so two attempts at most.
        _status, ctype, raw, _profile = _open_with_rotation(
            url, timeout=8, max_bytes=256 * 1024, attempts=2, accept_html=True,
        )
        # Only text/html is parsed; other types get a minimal hostname +
        # content-type preview so the hover still says something.
        if "text/html" not in ctype and "application/xhtml" not in ctype:
            try:
                pu = urllib.parse.urlparse(url)
                host = pu.netloc
            except Exception:
                host = ""
            out = {
                "url": url,
                "host": host,
                "title": (url.rsplit("/", 1)[-1] or host),
                "description": ctype.split(";", 1)[0].strip() or "",
                "image": "",
                "site_name": host,
                "content_type": ctype,
            }
            with _LINK_PREVIEW_CACHE_LOCK:
                _LINK_PREVIEW_CACHE[url] = out
            return out
        # Decode best-effort. Many pages declare utf-8 even when they aren't,
        # which is fine since errors='replace' keeps the regexes happy.
        text = raw.decode("utf-8", errors="replace")
    except Exception as e:
        out = {"error": f"{type(e).__name__}: {e}", "url": url}
        # Cache failures briefly too so a dead host doesn't get hammered.
        with _LINK_PREVIEW_CACHE_LOCK:
            _LINK_PREVIEW_CACHE[url] = out
        return out

    meta = _link_preview_extract(text)
    try:
        pu = urllib.parse.urlparse(url)
        host = pu.netloc
        # Resolve relative og:image against the page URL.
        if meta.get("image") and not meta["image"].startswith(("http://", "https://", "data:")):
            meta["image"] = urllib.parse.urljoin(url, meta["image"])
    except Exception:
        host = ""
    out = {
        "url": url,
        "host": host,
        "title": meta.get("title") or host or url,
        "description": meta.get("description") or "",
        "image": meta.get("image") or "",
        "site_name": meta.get("site_name") or host,
    }
    with _LINK_PREVIEW_CACHE_LOCK:
        # Trim oldest if the cache has grown unbounded. dict preserves
        # insertion order so popping items removes the earliest inserts.
        if len(_LINK_PREVIEW_CACHE) >= _LINK_PREVIEW_CACHE_MAX:
            for k in list(_LINK_PREVIEW_CACHE.keys())[: _LINK_PREVIEW_CACHE_MAX // 4]:
                _LINK_PREVIEW_CACHE.pop(k, None)
        _LINK_PREVIEW_CACHE[url] = out
    return out


def tool_web_fetch(args: dict) -> dict:
    url = args.get("url") or ""
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}
    try:
        # Three attempts with rotating browser identity (see _open_with_rotation).
        _status, _ctype, raw, _profile = _open_with_rotation(
            url, timeout=20, max_bytes=1024 * 1024, attempts=3, accept_html=True,
        )
        text = raw.decode("utf-8", errors="replace")
        # Sanitize before extraction: hidden-text vectors carry prompt
        # injections (display:none, white-on-white, offscreen, aria-hidden,
        # noscript fallbacks, meta keywords, comments).
        clean = re.sub(r"<!--[\s\S]*?-->", " ", text, flags=re.IGNORECASE)
        clean = re.sub(r"<noscript[\s\S]*?</noscript>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<meta[\s\S]*?>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<title[\s\S]*?</title>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<script[\s\S]*?</script>", " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<style[\s\S]*?</style>", " ", clean, flags=re.IGNORECASE)
        # Elements styled hidden (display:none / visibility:hidden / opacity:0 /
        # font-size:0 / offscreen positioning) or flagged aria-hidden=true get
        # dropped WITH their contents — that is where injections hide.
        clean = re.sub(
            r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*style\s*=\s*[\"'][^\"']*"
            r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|"
            r"font-size\s*:\s*0|left\s*:\s*-\d+px)[^\"']*[\"'][^>]*>[\s\S]*?</\1\s*>",
            " ", clean, flags=re.IGNORECASE)
        clean = re.sub(
            r"<[^>]*\b(?:aria-hidden\s*=\s*[\"']?true[\"']?|hidden\b)[^>]*>[\s\S]*?</[a-zA-Z][a-zA-Z0-9]*\s*>",
            " ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        _mark_web_taint(clean)
        # Provenance framing: anything inside [web_content] is untrusted DATA,
        # not instructions (see system-prompt rule on web content).
        return {
            "url": url,
            "text": f"[web_content url={url}]\n{clean[:16000]}\n[/web_content]",
            "truncated": len(clean) > 16000,
        }
    except Exception as e:
        return {"error": str(e)}


_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", s).strip()


def tool_web_search(args: dict) -> dict:
    """Search the web via DuckDuckGo's no-JS HTML endpoint. No API key.
    Returns a list of {title, url, snippet}. The model usually wants to
    follow up with web_fetch on the top few URLs to read full content."""
    q = (args.get("query") or args.get("q") or "").strip()
    if not q:
        return {"error": "query required"}
    max_results = int(args.get("max_results") or 6)
    max_results = max(1, min(max_results, 20))
    try:
        body = urllib.parse.urlencode({"q": q}).encode()
        # The DDG html endpoint still 403s plain UAs occasionally; send a Referer
        # from the duckduckgo homepage so its referer check passes.
        _status, _ctype, raw, _profile = _open_with_rotation(
            "https://html.duckduckgo.com/html/",
            timeout=15, max_bytes=2 * 1024 * 1024, attempts=3,
            accept_html=True, referer="https://duckduckgo.com/",
            method="POST", data=body,
            extra_headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        html = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"search failed: {e}"}
    snippets = [_strip_tags(s) for s in _DDG_SNIPPET_RE.findall(html)]
    results = []
    for i, m in enumerate(_DDG_RESULT_RE.finditer(html)):
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        # ddg wraps outbound links as /l/?uddg=<encoded>
        try:
            pu = urllib.parse.urlparse(url)
            if "duckduckgo.com" in pu.netloc and pu.path.startswith("/l/"):
                qs = urllib.parse.parse_qs(pu.query)
                real = qs.get("uddg", [""])[0]
                if real:
                    url = urllib.parse.unquote(real)
        except Exception:
            pass
        title = _strip_tags(m.group(2))
        snippet = snippets[i] if i < len(snippets) else ""
        if title and url:
            results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break
    _mark_web_taint(" \n".join(f"{r['title']} {r['snippet']}" for r in results))
    return {"query": q, "results": results, "count": len(results)}
