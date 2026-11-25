# SPDX-License-Identifier: AGPL-3.0-only
"""Pull a title, description and image out of a web page for the link cards in chat.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import re


def _link_preview_extract(html: str) -> dict:
    """Pull a small set of meta tags out of HTML. Order of preference for each
    field: og:* > twitter:* > stdlib equivalents. We don't run a real parser —
    a couple of regexes are accurate enough for 99% of pages and don't bring in
    a dependency."""
    def _meta(prop_re: str) -> str:
        # match <meta property/name="X" content="Y"> in either order.
        m = re.search(
            r'<meta[^>]+(?:property|name)\s*=\s*["\']' + prop_re + r'["\'][^>]*content\s*=\s*["\']([^"\']*)["\']',
            html, flags=re.IGNORECASE,
        )
        if m:
            return m.group(1)
        m = re.search(
            r'<meta[^>]+content\s*=\s*["\']([^"\']*)["\'][^>]*(?:property|name)\s*=\s*["\']' + prop_re + r'["\']',
            html, flags=re.IGNORECASE,
        )
        return m.group(1) if m else ""

    title = (
        _meta(r"og:title")
        or _meta(r"twitter:title")
        or ""
    )
    if not title:
        m = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, flags=re.IGNORECASE)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    desc = (
        _meta(r"og:description")
        or _meta(r"twitter:description")
        or _meta(r"description")
        or ""
    )
    image = (
        _meta(r"og:image(?::secure_url)?")
        or _meta(r"twitter:image(?::src)?")
        or ""
    )
    site = (
        _meta(r"og:site_name")
        or ""
    )

    # Decode HTML entities the cheap way — these meta values are usually
    # short and safe to feed through html.unescape.
    import html as _htmllib
    return {
        "title": _htmllib.unescape(title)[:300],
        "description": _htmllib.unescape(desc)[:600],
        "image": image[:600],
        "site_name": _htmllib.unescape(site)[:120],
    }
