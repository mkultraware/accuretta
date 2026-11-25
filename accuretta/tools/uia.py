# SPDX-License-Identifier: AGPL-3.0-only
"""Windows UI Automation helpers for desktop control: safe property reads, control
patterns, bounds, and matching a control to a descriptor.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

from typing import Any


def _uia_safe(control, name: str, default=None):
    try:
        return getattr(control, name)
    except Exception:
        return default


def _uia_pattern(control, method: str):
    try:
        fn = getattr(control, method, None)
        return fn() if callable(fn) else None
    except Exception:
        return None


def _uia_bounds(control) -> list[int]:
    rect = _uia_safe(control, "BoundingRectangle")
    if not rect:
        return [0, 0, 0, 0]
    try:
        left, top = int(rect.left), int(rect.top)
        return [left, top, max(0, int(rect.right) - left), max(0, int(rect.bottom) - top)]
    except Exception:
        return [0, 0, 0, 0]


def _uia_control_record(control, path: tuple[int, ...]) -> tuple[dict, dict]:
    role = str(_uia_safe(control, "ControlTypeName", "Control") or "Control")
    if role.endswith("Control"):
        role = role[:-7]
    password = bool(_uia_safe(control, "IsPassword", False))
    actions: list[str] = []
    patterns: dict[str, Any] = {}
    for action, method in (
        ("invoke", "GetInvokePattern"),
        ("set_value", "GetValuePattern"),
        ("toggle", "GetTogglePattern"),
        ("select", "GetSelectionItemPattern"),
        ("expand_collapse", "GetExpandCollapsePattern"),
        ("scroll", "GetScrollPattern"),
    ):
        pattern = _uia_pattern(control, method)
        if pattern is not None:
            actions.append(action)
            patterns[action] = pattern
    if bool(_uia_safe(control, "IsKeyboardFocusable", False)):
        actions.append("focus")

    value = None
    if "set_value" in patterns:
        if password:
            value = "[redacted]"
        else:
            try:
                value = str(patterns["set_value"].Value)[:2000]
            except Exception:
                value = None
    if value in (None, "") and role in {"Document", "Edit", "Text"} and not password:
        text_pattern = _uia_pattern(control, "GetTextPattern")
        if text_pattern is not None:
            try:
                value = str(text_pattern.DocumentRange.GetText(2000))
            except Exception:
                pass

    state: dict[str, Any] = {}
    try:
        if "toggle" in patterns:
            state["toggle"] = int(patterns["toggle"].ToggleState)
        if "select" in patterns:
            state["selected"] = bool(patterns["select"].IsSelected)
        if "expand_collapse" in patterns:
            state["expanded"] = int(patterns["expand_collapse"].ExpandCollapseState)
    except Exception:
        pass

    runtime_id = []
    try:
        runtime_id = [int(v) for v in control.GetRuntimeId()]
    except Exception:
        pass
    name = str(_uia_safe(control, "Name", "") or "")[:500]
    automation_id = str(_uia_safe(control, "AutomationId", "") or "")[:240]
    class_name = str(_uia_safe(control, "ClassName", "") or "")[:240]
    public = {
        "role": role or "Control",
        "name": name,
        "bounds": _uia_bounds(control),
        "enabled": bool(_uia_safe(control, "IsEnabled", True)),
        "actions": actions,
        "depth": len(path),
    }
    if automation_id:
        public["automation_id"] = automation_id
    if class_name:
        public["class_name"] = class_name
    if bool(_uia_safe(control, "HasKeyboardFocus", False)):
        public["focused"] = True
    if bool(_uia_safe(control, "IsOffscreen", False)):
        public["offscreen"] = True
    if password:
        public["password"] = True
    if value not in (None, ""):
        public["value"] = value
    if state:
        public["state"] = state
    descriptor = {
        "path": list(path),
        "runtime_id": runtime_id,
        "fingerprint": [role, name, automation_id, class_name],
        "public": public,
    }
    return public, descriptor


def _uia_descriptor_matches(control, descriptor: dict) -> bool:
    expected_runtime = descriptor.get("runtime_id") or []
    if expected_runtime:
        try:
            if [int(v) for v in control.GetRuntimeId()] == expected_runtime:
                return True
        except Exception:
            pass
    _, actual_descriptor = _uia_control_record(control, tuple(descriptor.get("path") or ()))
    return actual_descriptor.get("fingerprint") == (descriptor.get("fingerprint") or [])
