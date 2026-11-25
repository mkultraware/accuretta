# SPDX-License-Identifier: AGPL-3.0-only
"""Replaying saved messages to llama-server: images clamped to the size the model
accepts, with a small cache so a long chat isn't re-encoded every turn.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import hashlib
import io as _io
from accuretta.core.optional_imports import Image
from accuretta.core.optional_imports import _HAVE_PIL


# Vision prompts spend tokens in proportion to pixels, and every agentic round
# replays the stored images. Cap what reaches the model so a 4K screenshot does
# not cost thousands of vision tokens, re-encode the vision tower each round,
# and pressure VRAM on tight cards. 1568px on the long edge keeps screenshots
# and text legible; PNG stays lossless for exactly that case.
IMAGE_WIRE_MAX_EDGE = 1568


_clamped_image_cache: dict[str, str] = {}


_CLAMPED_IMAGE_CACHE_MAX = 48


def _clamp_image_data_url(url: str, max_edge: int = IMAGE_WIRE_MAX_EDGE) -> str:
    """Downscale one image data URL to `max_edge` on its long side.

    Returns the original untouched on any failure or when it already fits, and
    caches results so replayed history re-encodes at most once per image."""
    if not isinstance(url, str) or not url.startswith("data:image/") or not _HAVE_PIL:
        return url
    key = url if len(url) <= 160 else (
        hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest() + ":" + str(len(url)))
    cached = _clamped_image_cache.get(key)
    if cached is not None:
        return cached
    try:
        header, _, payload = url.partition(",")
        mime = header[5:].split(";", 1)[0].strip().lower()
        if mime not in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
            return url
        img = Image.open(_io.BytesIO(_b64.b64decode(payload, validate=False)))
        width, height = img.size
        if max(width, height) <= max_edge:
            return url
        scale = max_edge / float(max(width, height))
        size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
        img = img.resize(size, Image.LANCZOS)
        buf = _io.BytesIO()
        if mime in {"image/jpeg", "image/jpg"}:
            img.convert("RGB").save(buf, format="JPEG", quality=90, optimize=True)
            out_mime = "image/jpeg"
        else:
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.save(buf, format="PNG", optimize=True)
            out_mime = "image/png"
        result = f"data:{out_mime};base64," + _b64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return url
    if len(_clamped_image_cache) >= _CLAMPED_IMAGE_CACHE_MAX:
        _clamped_image_cache.pop(next(iter(_clamped_image_cache)))
    _clamped_image_cache[key] = result
    return result


def _replay_wire_msg(m: dict, replay_vision: bool) -> dict | None:
    """Map one STORED chat message to its wire shape for the model (the same
    mapping the chat handler's replay loop uses). Returns None for roles that
    are never replayed."""
    role = m.get("role")
    if role not in ("user", "assistant", "tool"):
        return None
    stored_images = m.get("images") if role == "user" else None
    if stored_images and replay_vision:
        # Rebuild OpenAI-style content array. Text first (the model reads
        # top-to-bottom), then each image as image_url.
        parts: list[dict] = []
        txt = m.get("content", "") or ""
        if txt:
            parts.append({"type": "text", "text": txt})
        for img in stored_images:
            if not isinstance(img, str) or not img:
                continue
            url = img if img.startswith("data:") else f"data:image/png;base64,{img}"
            parts.append({"type": "image_url", "image_url": {"url": _clamp_image_data_url(url)}})
        out: dict = {"role": role, "content": parts if parts else (txt or "")}
    else:
        out = {"role": role, "content": m.get("content", "") or ""}
    if role == "assistant" and m.get("tool_calls"):
        out["tool_calls"] = m["tool_calls"]
    if role == "tool":
        if m.get("tool_call_id"):
            out["tool_call_id"] = m["tool_call_id"]
        if m.get("name"):
            out["name"] = m["name"]
    return out
