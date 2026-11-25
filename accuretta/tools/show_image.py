# SPDX-License-Identifier: AGPL-3.0-only
"""show_image: put a picture in front of the user, inline in the chat.

The model shows an image file from this PC, or a capture of the working window, a named
window or a screen region. A copy goes to the data folder's chat-media (named by its
content, so one picture is stored once); the chat shows it from /api/chat-media/<name>,
and the reply keeps the list (_images) so it is still there when the chat is reopened.
The model gets a short "shown" note, never the pixels: looking is view_image's job.

Any picture on the PC may be shown, without asking (the owner's choice, 2026-11-24): it
is only shown to the user. Never Accuretta's own data folder, and only real image files
(checked by their first bytes, not by the name).
"""
from __future__ import annotations

import hashlib
import io as _io
import json
import os
import re
import secrets
import time
from pathlib import Path

from accuretta.core.optional_imports import Image, _HAVE_PIL
from accuretta.core.paths import DATA
from accuretta.core.workspace import normalize_path
from accuretta.security.data_paths import is_data_path
from accuretta.security.path_guards import DATA_PATH_ERROR
from accuretta.tools.casual_browser import _sniff_image_mime
from accuretta.tools.desktop_guard import _desktop_preflight, _grab_view
from accuretta.tools.screen_view import _SCREEN_SHOT_DIR

CHAT_MEDIA_DIR = DATA / "chat-media"
CHAT_MEDIA_NAME_RE = re.compile(r"^[0-9a-f]{24}\.(?:png|jpg|gif|webp)$")
CHAT_MEDIA_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
_SHOW_MAX_BYTES = 25 * 1024 * 1024   # the largest file it reads
_SHOW_KEEP_BYTES = 8 * 1024 * 1024   # stored unchanged up to this size...
_SHOW_MAX_EDGE = 2560                # ...and this many pixels on the long edge
_EXT = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}
_SHOWN_PER_REPLY = 12


def _store(raw: bytes, ext: str) -> str:
    """Save the bytes once under a name made from their hash; return the name."""
    name = f"{hashlib.sha256(raw).hexdigest()[:24]}.{ext}"
    CHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    target = CHAT_MEDIA_DIR / name
    if not target.exists():
        tmp = CHAT_MEDIA_DIR / f".{name}.{secrets.token_hex(4)}.tmp"
        tmp.write_bytes(raw)
        os.replace(tmp, target)
    return name


def _fit(raw: bytes, kind: str) -> tuple[bytes, str, int, int]:
    """Keep a modest picture as it is; shrink a huge one. Returns (bytes, kind, w, h)."""
    if not _HAVE_PIL:
        if len(raw) > _SHOW_KEEP_BYTES:
            raise ValueError("the picture is over 8 MB and Pillow isn't installed to shrink it")
        return raw, kind, 0, 0
    img = Image.open(_io.BytesIO(raw))
    w, h = img.size
    if len(raw) <= _SHOW_KEEP_BYTES and max(w, h) <= _SHOW_MAX_EDGE:
        return raw, kind, w, h
    img.load()
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGBA")
    if max(w, h) > _SHOW_MAX_EDGE:
        scale = _SHOW_MAX_EDGE / max(w, h)
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    buf = _io.BytesIO()
    if kind == "png" or img.mode == "RGBA":
        img.save(buf, format="PNG", optimize=True)   # keeps transparency and crisp text
        kind = "png"
    else:
        img.convert("RGB").save(buf, format="JPEG", quality=88)
        kind = "jpeg"
    return buf.getvalue(), kind, img.size[0], img.size[1]


def tool_show_image(args: dict) -> dict:
    """Show the user an image file or a screen capture, inline in the chat. Read-only."""
    caption = " ".join(str(args.get("caption") or "").split())[:140]
    raw_path = str(args.get("path") or "").strip()
    wants_screen = (bool(args.get("screen")) or bool(str(args.get("window") or "").strip())
                    or all(k in args for k in ("x", "y", "w", "h")))
    saved_path = ""
    if raw_path:
        path = normalize_path(raw_path)
        if not path or not os.path.isfile(path):
            return {"error": f"not a file: {raw_path}"}
        if is_data_path(path):
            return {"error": DATA_PATH_ERROR}
        try:
            size = os.path.getsize(path)
            if size > _SHOW_MAX_BYTES:
                return {"error": f"the picture is {size // (1024 * 1024)} MB; the limit is 25 MB"}
            raw = Path(path).read_bytes()
        except OSError as e:
            return {"error": f"can't read {raw_path}: {e}"}
        kind = _sniff_image_mime(raw)
        if kind not in _EXT:
            return {"error": "not a picture this can show (PNG, JPEG, GIF or WebP)"}
        default_caption = Path(path).name
    elif wants_screen:
        err = _desktop_preflight(("pyautogui", "pillow"))
        if err:
            return err
        try:
            img, frame = _grab_view(args)
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
        except Exception as e:
            return {"error": f"screen capture failed: {e}"}
        raw, kind = buf.getvalue(), "png"
        # Also kept with the other screenshots, so view_image can look at it.
        _SCREEN_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        saved = _SCREEN_SHOT_DIR / f"shown-{time.strftime('%H%M%S')}-{secrets.token_hex(2)}.png"
        saved.write_bytes(raw)
        saved_path = str(saved)
        default_caption = f"Screenshot: {frame['window']}" if frame.get("window") else "Screenshot"
    else:
        return {"error": "give `path` (an image file), or `screen: true` (or a window name, or x, y, w, h) "
                         "to show a capture of the screen"}
    try:
        raw, kind, w, h = _fit(raw, kind)
    except Exception as e:
        return {"error": f"could not prepare the picture: {e}"}
    name = _store(raw, _EXT[kind])
    result = {
        "ok": True,
        "shown": True,
        "image": {"url": f"/api/chat-media/{name}", "caption": caption or default_caption, "width": w, "height": h},
        "note": ("The user sees this picture in the chat now. You don't: view_image is how you look at a picture"
                 + (f" (this capture is at {saved_path})." if saved_path else ".")),
    }
    if saved_path:
        result["path"] = saved_path
    return result


def _shown_images(messages: list) -> list[dict]:
    """The pictures show_image put in front of the user during one turn, read back from
    that turn's tool messages, for the reply to keep."""
    out = []
    for m in messages or []:
        if not isinstance(m, dict) or m.get("role") != "tool" or m.get("name") != "show_image":
            continue
        try:
            data = json.loads(m.get("content") or "")
        except (TypeError, ValueError):
            continue
        img = data.get("image") if isinstance(data, dict) else None
        if not isinstance(img, dict):
            continue
        url = str(img.get("url") or "")
        if not url.startswith("/api/chat-media/") or not CHAT_MEDIA_NAME_RE.match(url.rsplit("/", 1)[-1]):
            continue
        try:
            w, h = int(img.get("width") or 0), int(img.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        out.append({"url": url, "caption": str(img.get("caption") or "")[:140], "width": w, "height": h})
    return out[-_SHOWN_PER_REPLY:]


def chat_media_file(name: str) -> Path | None:
    """The stored picture for a /api/chat-media/<name> request, or None."""
    if not CHAT_MEDIA_NAME_RE.match(name or ""):
        return None
    target = CHAT_MEDIA_DIR / name
    return target if target.is_file() else None
