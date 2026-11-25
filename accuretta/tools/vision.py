# SPDX-License-Identifier: AGPL-3.0-only
"""Describing images for models without vision: one image, several images, or the main
model's own vision.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from accuretta.core.endpoints import LLAMA
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.runtime.llama_post import llama_post


def _describe_image_native(b64: str, hint: str = "") -> str:
    """Vision side-trip against the MAIN server (native mmproj + selected GGUF).

    Same shape as describe_image(), but always posts to LLAMA — never to a
    separate vision server — so the description is written by the same model
    that will act on it, perceiving through the mmproj paired with its GGUF."""
    payload = {
        "model": get_settings().get("model", "local"),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": hint or "Describe this image in detail."},
                ],
            }
        ],
        "stream": False,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
        "max_tokens": 1536,
        "chat_template_kwargs": {
            "enable_thinking": True,
            "reasoning_effort": "low",
        },
    }
    out = llama_post("/v1/chat/completions", payload, base=LLAMA, timeout=180)
    text = ""
    if isinstance(out, dict):
        choices = out.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("the vision model returned no description (try again when the model is idle)")
    return text


def _describe_image_multi(content: list[dict]) -> str:
    """Vision call with prebuilt content blocks (multiple images + one text)."""
    payload = {
        "model": get_settings().get("model", "local"),
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
        "max_tokens": 1536,
        "chat_template_kwargs": {
            "enable_thinking": True,
            "reasoning_effort": "low",
        },
    }
    out = llama_post("/v1/chat/completions", payload, base=LLAMA, timeout=240)
    text = ""
    if isinstance(out, dict):
        choices = out.get("choices") or []
        if choices:
            text = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not text:
        raise RuntimeError("the vision model returned no storyboard (try again when the model is idle)")
    return text


def describe_image(b64: str, hint: str = "", question: bool = False) -> str:
    """Hand a base64 image to a vision-capable llama-server via
    /v1/chat/completions with image_url content (main server started with
    --mmproj, or a separate VISION_LLAMA server). The chat model then sees only
    the text description, so context stays small for multi-image turns.

    question=True when `hint` is the model's own question about the image
    (view_image, describe_screen with a hint). It is answered directly: as
    "Describe this image. Context: <question>" every answer came back as a
    whole-page description, ~600 tokens and ~6 s per look in a computer-use run
    where the answer needed 40-150."""
    if b64.startswith("data:"):
        comma = b64.find(",")
        if comma >= 0:
            b64_clean = b64[comma + 1:]
        else:
            b64_clean = b64
    else:
        b64_clean = b64
    _ = b64_clean  # kept for future (raw b64 endpoints)
    prompt_text = "Describe this image in detail."
    if hint and question:
        prompt_text = ("Answer the question about this image exactly and briefly. Look "
                       "carefully, and describe only what the question needs, not the rest "
                       "of the image.\n\nQuestion: " + hint)
    elif hint:
        prompt_text = f"Describe this image. Context: {hint}"

    payload = {
        "model": get_settings().get("model", "local") if STATE.vision_llama == LLAMA else "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_clean}"}},
                    {"type": "text", "text": prompt_text}
                ]
            }
        ],
        "stream": False,
        # Qwen3.8 defaults to xhigh reasoning. On a bounded image-description
        # request that can consume the whole output allowance before producing
        # visible content. Keep this override local to the vision side-trip;
        # the user's saved effort and the main chat request are untouched.
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
        "max_tokens": 1536,
        "chat_template_kwargs": {
            "enable_thinking": True,
            "reasoning_effort": "low",
        },
    }
    try:
        out = llama_post("/v1/chat/completions", payload, base=STATE.vision_llama, timeout=180)
        if isinstance(out, dict):
            choices = out.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content", "").strip()
                if text:
                    return text

        # A reasoning model can still spend its complete allowance inside an
        # invisible think block. Retry only this stateless vision request with
        # Qwen3.8's documented non-thinking sampler. Conversation history and
        # the user's normal reasoning selection never enter either request.
        fallback_payload = {
            **payload,
            "temperature": 0.7,
            "top_p": 0.8,
            "presence_penalty": 1.5,
            "max_tokens": 768,
            "chat_template_kwargs": {
                "enable_thinking": False,
                "thinking_budget": 0,
            },
        }
        out = llama_post(
            "/v1/chat/completions", fallback_payload,
            base=STATE.vision_llama, timeout=180,
        )
        if isinstance(out, dict):
            choices = out.get("choices") or []
            if choices:
                text = (choices[0].get("message") or {}).get("content", "").strip()
                if text:
                    return text
        return "[image attached — empty description]"
    except Exception as e:
        return f"[image attached — vision llama-server at {STATE.vision_llama} failed: {e}]"
