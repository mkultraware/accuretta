# SPDX-License-Identifier: AGPL-3.0-only
"""Reading a window's controls through UI Automation: the snapshot, its short-lived
cache, finding a control again from its descriptor, and the before/after diff.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import uuid
from accuretta.core.chats import _current_chat_id
from accuretta.core.optional_imports import _uia
from accuretta.tools.desktop_procs import _process_path
from accuretta.tools.desktop_windows import _window_handle_from_id
from accuretta.tools.desktop_windows import _window_id
from accuretta.tools.desktop_windows import _window_process_id
from accuretta.tools.uia import _uia_bounds
from accuretta.tools.uia import _uia_control_record
from accuretta.tools.uia import _uia_descriptor_matches
from accuretta.tools.uia import _uia_safe
from typing import Any


# UI Automation controls are COM objects and cannot safely cross request
# threads. Snapshots therefore cache only serializable lookup descriptors;
# every action resolves a fresh control inside its own COM apartment.
_desktop_snapshot_cache: dict[str, dict] = {}


_desktop_snapshot_lock = threading.Lock()


_DESKTOP_SNAPSHOT_TTL_S = 180.0


_DESKTOP_SNAPSHOT_CACHE_MAX = 12


def _uia_window(selector: str = ""):
    selector = str(selector or "").strip()
    hwnd = _window_handle_from_id(selector)
    if not hwnd and not selector and sys.platform == "win32":
        try:
            import ctypes
            hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            hwnd = 0
    if hwnd:
        control = _uia.ControlFromHandle(hwnd)
        if control:
            return control

    needle = selector.lower()
    candidates = []
    for control in _uia.GetRootControl().GetChildren():
        try:
            title = str(control.Name or "").strip()
            handle = int(control.NativeWindowHandle or 0)
            if not title or not handle:
                continue
            if needle and needle not in title.lower():
                continue
            candidates.append(control)
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda c: (
        str(_uia_safe(c, "Name", "")).lower() != needle,
        -(_uia_bounds(c)[2] * _uia_bounds(c)[3]),
    ))
    return candidates[0]


def _desktop_cache_put(snapshot_id: str, entry: dict) -> None:
    now = time.time()
    with _desktop_snapshot_lock:
        stale = [sid for sid, item in _desktop_snapshot_cache.items()
                 if now - float(item.get("created") or 0) > _DESKTOP_SNAPSHOT_TTL_S]
        for sid in stale:
            _desktop_snapshot_cache.pop(sid, None)
        _desktop_snapshot_cache[snapshot_id] = entry
        while len(_desktop_snapshot_cache) > _DESKTOP_SNAPSHOT_CACHE_MAX:
            oldest = min(_desktop_snapshot_cache,
                         key=lambda sid: float(_desktop_snapshot_cache[sid].get("created") or 0))
            _desktop_snapshot_cache.pop(oldest, None)


def _desktop_cache_descriptor(ref: str) -> tuple[dict | None, dict | None, str]:
    parts = str(ref or "").split(":", 2)
    if len(parts) != 3 or parts[0] != "e":
        return None, None, "invalid element reference; call desktop_snapshot again"
    snapshot_id = parts[1]
    with _desktop_snapshot_lock:
        cache = _desktop_snapshot_cache.get(snapshot_id)
        descriptor = (cache.get("elements") or {}).get(ref) if cache else None
    if not cache or not descriptor:
        return None, None, "unknown or expired element reference; call desktop_snapshot again"
    if time.time() - float(cache.get("created") or 0) > _DESKTOP_SNAPSHOT_TTL_S:
        return None, None, "element reference expired; call desktop_snapshot again"
    chat_id = _current_chat_id.get()
    if chat_id and cache.get("chat_id") and cache.get("chat_id") != chat_id:
        return None, None, "element reference belongs to another chat"
    return cache, descriptor, ""


def _uia_resolve_descriptor(cache: dict, descriptor: dict):
    window = _uia.ControlFromHandle(int(cache.get("window_handle") or 0))
    if not window:
        return None
    control = window
    try:
        for index in descriptor.get("path") or []:
            children = control.GetChildren()
            if index < 0 or index >= len(children):
                control = None
                break
            control = children[index]
        if control is not None and _uia_descriptor_matches(control, descriptor):
            return control
    except Exception:
        pass

    target_runtime = descriptor.get("runtime_id") or []
    if not target_runtime:
        return None
    queue = [window]
    visited = 0
    while queue and visited < 1600:
        control = queue.pop(0)
        visited += 1
        try:
            if [int(v) for v in control.GetRuntimeId()] == target_runtime:
                return control
            queue.extend(control.GetChildren())
        except Exception:
            continue
    return None


def _uia_capture(selector: str = "", max_depth: int = 8,
                 max_elements: int = 160, include_offscreen: bool = False,
                 query: str = "") -> dict:
    window = _uia_window(selector)
    if not window:
        return {"error": f"no accessible window matches '{selector}'" if selector
                         else "no foreground window is available"}
    hwnd = int(_uia_safe(window, "NativeWindowHandle", 0) or 0)
    if not hwnd:
        return {"error": "the selected window has no stable native handle"}
    snapshot_id = uuid.uuid4().hex[:10]
    elements: list[dict] = []
    descriptors: dict[str, dict] = {}
    queue: list[tuple[Any, tuple[int, ...]]] = []
    try:
        queue = [(child, (index,)) for index, child in enumerate(window.GetChildren())]
    except Exception as exc:
        return {"error": f"could not read the accessibility tree: {exc}"}
    visited = 0
    max_nodes = max(500, max_elements * 8)
    query = str(query or "").strip().lower()
    while queue and len(elements) < max_elements and visited < max_nodes:
        control, path = queue.pop(0)
        visited += 1
        public, descriptor = _uia_control_record(control, path)
        relevant = bool(
            public.get("name") or public.get("value") or public.get("actions")
            or public.get("focused") or public.get("role") in {"Document", "Edit", "StatusBar"}
        )
        if query:
            searchable = " ".join(str(public.get(key) or "") for key in
                                  ("role", "name", "automation_id", "class_name", "value")).lower()
            relevant = relevant and query in searchable
        if relevant and (include_offscreen or not public.get("offscreen")):
            ref = f"e:{snapshot_id}:{len(elements) + 1}"
            public["ref"] = ref
            descriptor["public"] = dict(public)
            descriptors[ref] = descriptor
            elements.append(public)
        if len(path) < max_depth:
            try:
                children = control.GetChildren()
                queue.extend((child, path + (index,)) for index, child in enumerate(children))
            except Exception:
                pass

    title = str(_uia_safe(window, "Name", "") or "")[:500]
    pid = int(_uia_safe(window, "ProcessId", 0) or _window_process_id(hwnd))
    window_info = {
        "window_id": _window_id(hwnd),
        "title": title,
        "process_id": pid,
        "process_path": _process_path(pid),
        "bounds": _uia_bounds(window),
        "framework": str(_uia_safe(window, "FrameworkId", "") or "")[:100],
    }
    normalized = [{k: v for k, v in item.items() if k != "ref"} for item in elements]
    state_token = hashlib.sha256(json.dumps(normalized, sort_keys=True,
                                            ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    cache_entry = {
        "created": time.time(),
        "chat_id": _current_chat_id.get(),
        "window_handle": hwnd,
        "window": window_info,
        "elements": descriptors,
        "records": elements,
        "state_token": state_token,
    }
    _desktop_cache_put(snapshot_id, cache_entry)
    return {
        "ok": True,
        "mode": "accessibility",
        "snapshot_id": snapshot_id,
        "state_token": state_token,
        "window": window_info,
        "elements": elements,
        "truncated": bool(queue),
        "visited_nodes": visited,
        "note": "Use element refs with desktop_invoke, desktop_set_value, or desktop_scroll. Re-snapshot after major UI changes.",
    }


def _uia_record_key(descriptor: dict) -> tuple:
    runtime = tuple(descriptor.get("runtime_id") or ())
    return ("runtime",) + runtime if runtime else ("path",) + tuple(descriptor.get("path") or ())


def _uia_diff(before: dict, after: dict) -> dict:
    before_map = {_uia_record_key(v): v.get("public") or {}
                  for v in (before.get("elements") or {}).values()}
    after_map = {_uia_record_key(v): v.get("public") or {}
                 for v in (after.get("elements") or {}).values()}
    added_keys = after_map.keys() - before_map.keys()
    removed_keys = before_map.keys() - after_map.keys()
    changed = []
    for key in before_map.keys() & after_map.keys():
        old = {k: v for k, v in before_map[key].items() if k != "ref"}
        new = {k: v for k, v in after_map[key].items() if k != "ref"}
        if old != new:
            changed.append(after_map[key])
    return {
        "added_count": len(added_keys),
        "removed_count": len(removed_keys),
        "changed_count": len(changed),
        "added": [after_map[k] for k in list(added_keys)[:20]],
        "removed": [before_map[k] for k in list(removed_keys)[:20]],
        "changed": changed[:20],
    }
