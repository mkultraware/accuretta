# SPDX-License-Identifier: AGPL-3.0-only
"""The agent's desktop tools: screenshots, describing the screen, listing windows, the
control snapshot, and launching, focusing, clicking, dragging, typing, keys, scrolling
and closing.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from accuretta.tools.desktop_state import _desktop_post_state
from accuretta.core.optional_imports import _HAVE_PIL
from accuretta.core.optional_imports import _HAVE_PYAUTOGUI
from accuretta.core.optional_imports import _HAVE_UIA
from accuretta.core.optional_imports import _uia
from accuretta.core.optional_imports import pyautogui
from accuretta.core.window_lib import _HAVE_PGW
from accuretta.core.window_lib import _pgw
from accuretta.runtime.vision_ready import _vision_observation_available
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.security.desktop_intent import _is_accuretta_window
from accuretta.security.soft_patterns import _app_matches_allowlist
from accuretta.tools.desktop_guard import _capture_desktop_target
from accuretta.tools.desktop_guard import _desktop_preflight
from accuretta.tools.desktop_guard import _gate_action
from accuretta.tools.desktop_guard import _grab_view
from accuretta.tools.desktop_guard import _reassert_desktop_target
from accuretta.tools.desktop_guard import _remember_desktop_target
from accuretta.tools.desktop_snapshot import _desktop_cache_descriptor
from accuretta.tools.desktop_snapshot import _desktop_snapshot_cache
from accuretta.tools.desktop_snapshot import _desktop_snapshot_lock
from accuretta.tools.desktop_snapshot import _uia_capture
from accuretta.tools.desktop_snapshot import _uia_diff
from accuretta.tools.desktop_snapshot import _uia_resolve_descriptor
from accuretta.tools.desktop_windows import _find_window
from accuretta.tools.desktop_windows import _list_windows_raw
from accuretta.tools.desktop_windows import _window_id
from accuretta.tools.screen_view import _BLANK_CONTENT_NOTE
from accuretta.tools.screen_view import _SCREEN_SHOT_DIR
from accuretta.tools.screen_view import _content_looks_blank
from accuretta.tools.screen_view import _describe_frame
from accuretta.tools.uia import _uia_pattern
from accuretta.tools.uia import _uia_safe


def tool_screenshot(args: dict) -> dict:
    """Capture the working window (or a region) to a PNG file. Read-only.
    The picture is looked at with view_image; the result is never base64."""
    err = _desktop_preflight(("pyautogui", "pillow"))
    if err:
        return err
    try:
        img, frame = _grab_view(args)
        _SCREEN_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = _SCREEN_SHOT_DIR / f"screen-{time.strftime('%H%M%S')}-{secrets.token_hex(2)}.png"
        img.save(path, format="PNG")
        result = {"ok": True, "path": str(path), "frame": frame,
                  "note": ("Saved. Call view_image with this path and a question to look at it. "
                           "Image pixel (ix, iy) is screen (frame.x + ix/frame.scale, "
                           "frame.y + iy/frame.scale).")}
        if frame.get("window") and _content_looks_blank(img):
            result["warning"] = _BLANK_CONTENT_NOTE
        return result
    except Exception as e:
        return {"error": f"screenshot failed: {e}"}


def tool_describe_screen(args: dict) -> dict:
    """Look at the working window (or a named window or region) through the
    vision model and return its description, with positions as screen
    coordinates."""
    err = _desktop_preflight(("pyautogui", "pillow"))
    if err:
        return err
    if not _vision_observation_available():
        return {"error": "no working vision backend is loaded. Use desktop_snapshot for CPU-only blind control."}
    try:
        img, frame = _grab_view(args)
        return _describe_frame(img, frame, (args.get("hint") or "").strip())
    except Exception as e:
        return {"error": f"describe_screen failed: {e}"}


def tool_list_windows(args: dict) -> dict:
    """List visible top-level windows."""
    err = _desktop_preflight()
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    return {"ok": True, "windows": _list_windows_raw()}


def tool_desktop_snapshot(args: dict) -> dict:
    """Read a window's Windows UI Automation tree without screenshots or a VLM."""
    err = _desktop_preflight(("uiautomation",))
    if err:
        return err
    selector = str(args.get("window") or "").strip()
    try:
        max_depth = max(1, min(20, int(args.get("max_depth") or 8)))
        max_elements = max(20, min(800, int(args.get("max_elements") or 160)))
    except (TypeError, ValueError):
        return {"error": "max_depth and max_elements must be integers"}
    try:
        with _uia.UIAutomationInitializerInThread():
            return _uia_capture(selector, max_depth, max_elements,
                                bool(args.get("include_offscreen")),
                                str(args.get("query") or ""))
    except Exception as exc:
        return {"error": f"accessibility snapshot failed: {exc}"}


def tool_desktop_launch_app(args: dict) -> dict:
    """Launch an application. It asks exactly when starting the same program
    from the shell would (asks in Soft/Medium/Hard, runs under /goal); apps in
    the user's allowlist open without asking except in Hard mode.

    It used to refuse anything off the allowlist, in every mode, while the
    shell could start the same program: a /goal run told "notepad is not in the
    allowlist" just ran Start-Process notepad instead, and an empty allowlist
    meant a dead end ("add it in Settings") rather than a question."""
    target = (args.get("name") or args.get("path") or "").strip()
    if not target:
        return {"error": "name or path required"}
    allowlisted = _app_matches_allowlist(target)
    gate_err = _gate_action(
        title="Launch app (desktop automation)",
        command=target,
        details={"kind": "desktop.launch", "target": target, "allowlisted": allowlisted},
    )
    if gate_err:
        return gate_err
    try:
        # shell=True lets 'notepad' / 'chrome' resolve from PATH/App Paths
        subprocess.Popen(target, shell=True, env=_child_env(keep=_ENV_TOOLS))
        result = {"ok": True, "launched": target}
        # A web page opens with desktop_open_url (the browser comes up already
        # on the page); a run once spent its turns launching 'chrome' instead.
        if re.search(r"(?i)\b(chrome|msedge|edge|firefox|brave|opera|vivaldi|browser)\b", target):
            result["note"] = "To open a web page, call desktop_open_url with the URL."
            result["suggested_tool"] = "desktop_open_url"
        return result
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_focus_window(args: dict) -> dict:
    """Bring a window to the foreground by title substring."""
    err = _desktop_preflight()
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title substring required"}
    w = _find_window(title)
    if not w:
        return {"error": f"no visible window matches '{title}'"}
    gate_err = _gate_action(
        title="Focus window",
        command=f'focus: "{w.title}"',
        details={"kind": "desktop.focus", "title": w.title},
    )
    if gate_err:
        return gate_err
    try:
        if getattr(w, "isMinimized", False):
            w.restore()
        w.activate()
        _remember_desktop_target(w)   # this is now the intended input target
        return {"ok": True, "focused": w.title}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_click(args: dict) -> dict:
    """Move the mouse to (x, y) and click. Coordinates are in screen pixels.
    The agent should derive coords from describe_screen + list_windows, not guess."""
    try:
        x = int(args["x"])
        y = int(args["y"])
    except Exception:
        return {"error": "x and y (screen pixel coords) required"}
    button = (args.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        return {"error": "button must be left, right, or middle"}
    clicks = int(args.get("clicks") or 1)
    if not (1 <= clicks <= 3):
        return {"error": "clicks must be 1..3"}
    _capture_desktop_target()   # snapshot target before the approval card can steal focus
    gate_err = _gate_action(
        title=f"Click at ({x}, {y})",
        command=f"{button} click x{clicks} at ({x}, {y})",
        details={"kind": "desktop.click", "x": x, "y": y, "button": button, "clicks": clicks},
        required=("pyautogui",),
    )
    if gate_err:
        return gate_err
    _reassert_desktop_target()  # re-aim at the intended window before the click fires
    hit = _element_at_point(x, y)
    try:
        pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        out = {"ok": True, "clicked": [x, y], "button": button, "clicks": clicks}
        if hit:
            out["clicked_element"] = hit
        return out
    except Exception as e:
        return {"error": str(e)}


def _element_at_point(x: int, y: int) -> dict | None:
    """What sits at a screen point per UI Automation (name and kind), read just
    before a click, so the model learns at once whether it hit the button it
    aimed at. A vision estimate a few pixels off clicked a row's label instead
    of its Claim button, and the run spent minutes cropping screenshots to
    find out why. Fast (no model call); None when UIA can't say."""
    if not _HAVE_UIA:
        return None
    try:
        ctrl = _uia.ControlFromPoint(x, y)
        if not ctrl:
            return None
        name = str(getattr(ctrl, "Name", "") or "").strip()[:80]
        kind = str(getattr(ctrl, "ControlTypeName", "") or "").replace("Control", "").lower()
        # Unnamed containers (a browser page without its accessibility tree
        # on) say nothing useful; only report a named element.
        if not name:
            return None
        return {"kind": kind, "name": name}
    except Exception:
        return None


def tool_desktop_drag(args: dict) -> dict:
    """Press at (x1, y1), move to (x2, y2) and release: drag-and-drop, sliders,
    reordering. Coordinates are screen pixels, as describe_screen reports them."""
    try:
        x1, y1, x2, y2 = (int(args[k]) for k in ("x1", "y1", "x2", "y2"))
    except Exception:
        return {"error": "x1, y1, x2, y2 (screen pixel coords) required"}
    button = (args.get("button") or "left").lower()
    if button not in ("left", "right", "middle"):
        return {"error": "button must be left, right, or middle"}
    try:
        seconds = max(0.1, min(5.0, float(args.get("duration_ms") or 600) / 1000.0))
    except (TypeError, ValueError):
        return {"error": "duration_ms must be a number"}
    _capture_desktop_target()
    gate_err = _gate_action(
        title=f"Drag ({x1}, {y1}) → ({x2}, {y2})",
        command=f"{button} drag from ({x1}, {y1}) to ({x2}, {y2})",
        details={"kind": "desktop.drag", "from": [x1, y1], "to": [x2, y2], "button": button},
        required=("pyautogui",),
    )
    if gate_err:
        return gate_err
    _reassert_desktop_target()
    try:
        pyautogui.moveTo(x1, y1)
        pyautogui.mouseDown(button=button)
        time.sleep(0.08)
        # A few pixels of movement first: browsers start an HTML5 drag only
        # after the pointer moves while the button is down.
        pyautogui.moveTo(x1 + (6 if x2 >= x1 else -6), y1, duration=0.08)
        pyautogui.moveTo(x2, y2, duration=seconds)
        time.sleep(0.08)
        pyautogui.mouseUp(button=button)
        return {"ok": True, "dragged": [[x1, y1], [x2, y2]], "button": button,
                "post_state": _desktop_post_state()}
    except Exception as e:
        try:
            pyautogui.mouseUp(button=button)
        except Exception:
            pass
        return {"error": str(e)}


def _default_browser_exe() -> str:
    """The user's default browser executable on Windows ("" when unknown)."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations"
                            r"\https\UserChoice") as key:
            prog_id = winreg.QueryValueEx(key, "ProgId")[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\shell\open\command") as key:
            command = str(winreg.QueryValueEx(key, "")[0] or "")
    except Exception:
        return ""
    m = re.match(r'\s*"([^"]+\.exe)"', command) or re.match(r"\s*(\S+\.exe)", command, re.IGNORECASE)
    return m.group(1) if m and os.path.isfile(m.group(1)) else ""


def tool_desktop_open_url(args: dict) -> dict:
    """Open an http(s) URL in a new window of the user's default browser, wait
    for it to come up, and make it the window the desktop tools work in."""
    url = str(args.get("url") or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return {"error": "url must start with http:// or https://"}
    try:
        wait_s = max(1.0, min(30.0, float(args.get("wait_s") or 8)))
    except (TypeError, ValueError):
        return {"error": "wait_s must be a number"}
    err = _desktop_preflight(("pygetwindow",))
    if err:
        return err
    gate_err = _gate_action(
        title="Open URL in the browser",
        command=f"open {url}",
        details={"kind": "desktop.open_url", "url": url},
    )
    if gate_err:
        return gate_err
    before = {int(getattr(w, "_hWnd", 0) or 0) for w in _pgw.getAllWindows()}
    exe = _default_browser_exe()
    try:
        if exe:
            flag = "-new-window" if "firefox" in exe.lower() else "--new-window"
            subprocess.Popen([exe, flag, url], env=_child_env(keep=_ENV_TOOLS))
        else:
            import webbrowser
            webbrowser.open(url, new=1)
    except Exception as e:
        return {"error": f"could not open the browser: {e}"}
    # Wait for the new window (or, failing that, a non-Accuretta foreground
    # window) whose title has moved past a blank/new-tab placeholder.
    deadline = time.time() + wait_s
    found = None
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            fresh = [w for w in _pgw.getAllWindows()
                     if int(getattr(w, "_hWnd", 0) or 0) not in before
                     and str(getattr(w, "title", "")).strip()]
            active = _pgw.getActiveWindow()
        except Exception:
            fresh, active = [], None
        candidate = fresh[0] if fresh else (
            active if active is not None and not _is_accuretta_window(getattr(active, "title", ""))
            else None)
        if candidate is not None:
            found = candidate
            title = str(getattr(candidate, "title", "")).lower()
            if title and not re.match(r"^(new tab|untitled|about:blank)", title):
                break
    if found is None:
        return {"ok": True, "opened": url, "window": None,
                "note": "the browser did not show a new window in time; use list_windows"}
    try:
        if not getattr(found, "isActive", False):
            found.activate()
    except Exception:
        pass
    _remember_desktop_target(found)
    hwnd = int(getattr(found, "_hWnd", 0) or 0)
    result = {"ok": True, "opened": url, "window": str(getattr(found, "title", ""))[:200],
              "window_id": _window_id(hwnd) if hwnd else "",
              "box": [int(found.left), int(found.top), int(found.width), int(found.height)],
              "note": "This window is now the working window: describe_screen and screenshot show it."}
    # The title arrives before the page is drawn; a deskbench run looked at an
    # empty page twice before it thought to reload. Wait a few seconds for it.
    if _HAVE_PIL:
        try:
            from PIL import ImageGrab
            painted = False
            paint_deadline = time.time() + 4.0
            while time.time() < paint_deadline:
                box = (int(found.left), int(found.top),
                       int(found.left) + int(found.width), int(found.top) + int(found.height))
                if not _content_looks_blank(ImageGrab.grab(bbox=box, all_screens=True)):
                    painted = True
                    break
                time.sleep(0.5)
            if not painted:
                result["warning"] = _BLANK_CONTENT_NOTE
        except Exception:
            pass
    return result


def tool_desktop_type_text(args: dict) -> dict:
    """Type a string into the focused window. No special keys — use press_keys for those."""
    text = args.get("text") or ""
    if not isinstance(text, str) or not text:
        return {"error": "text (string) required"}
    if len(text) > 2000:
        return {"error": "text too long (>2000 chars). Split into smaller chunks."}
    preview = text if len(text) <= 200 else (text[:200] + "…")
    _capture_desktop_target()   # snapshot target before the approval card can steal focus
    gate_err = _gate_action(
        title="Type text (keyboard)",
        command=f"type: {preview}",
        details={"kind": "desktop.type", "text": text, "length": len(text)},
        required=("pyautogui",),
    )
    if gate_err:
        return gate_err
    _reassert_desktop_target()  # re-aim at the intended window before typing
    try:
        pyautogui.typewrite(text, interval=0.01)
        return {"ok": True, "typed_chars": len(text)}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_press_keys(args: dict) -> dict:
    """Press a key combo like 'ctrl+s', 'alt+tab', 'enter', 'win'.
    For a single key use `keys: 'enter'`; for combos use `keys: 'ctrl+s'`."""
    keys = args.get("keys") or ""
    if not isinstance(keys, str) or not keys.strip():
        return {"error": "keys (string like 'ctrl+s' or 'enter') required"}
    combo = [k.strip().lower() for k in keys.split("+") if k.strip()]
    if not combo:
        return {"error": "no keys parsed"}
    # pyautogui hotkey whitelist — block anything not a known key to avoid surprises
    allowed = set(pyautogui.KEYBOARD_KEYS) if _HAVE_PYAUTOGUI else set()
    bad = [k for k in combo if k not in allowed and len(k) != 1]
    if bad:
        return {"error": f"unknown keys: {bad}. See pyautogui.KEYBOARD_KEYS."}
    _capture_desktop_target()   # snapshot target before the approval card can steal focus
    gate_err = _gate_action(
        title="Press keys",
        command=f"hotkey: {'+'.join(combo)}",
        details={"kind": "desktop.keys", "combo": combo},
        required=("pyautogui",),
    )
    if gate_err:
        return gate_err
    _reassert_desktop_target()  # re-aim at the intended window before the keypress
    try:
        if len(combo) == 1:
            pyautogui.press(combo[0])
        else:
            pyautogui.hotkey(*combo)
        return {"ok": True, "pressed": "+".join(combo)}
    except Exception as e:
        return {"error": str(e)}


def tool_desktop_close_window(args: dict) -> dict:
    """Close a window by title substring. Requires approval."""
    err = _desktop_preflight()
    if err:
        return err
    if not _HAVE_PGW:
        return {"error": "missing dependency: pygetwindow. Install with: pip install pygetwindow"}
    title = (args.get("title") or "").strip()
    if not title:
        return {"error": "title substring required"}
    w = _find_window(title)
    if not w:
        return {"error": f"no visible window matches '{title}'"}
    gate_err = _gate_action(
        title="Close window",
        command=f'close: "{w.title}"',
        details={"kind": "desktop.close", "title": w.title},
    )
    if gate_err:
        return gate_err
    try:
        w.close()
        return {"ok": True, "closed": w.title}
    except Exception as e:
        return {"error": str(e)}


def _desktop_element_action(ref: str, action_name: str, command: str,
                            details: dict, operation) -> dict:
    err = _desktop_preflight(("uiautomation",))
    if err:
        return err
    cache, descriptor, cache_error = _desktop_cache_descriptor(ref)
    if cache_error:
        return {"error": cache_error}
    public = dict(descriptor.get("public") or {})
    details = dict(details, ref=ref, window=cache.get("window"), element=public)
    gate_err = _gate_action(
        title=f"{action_name} accessibility element",
        command=command,
        details=details,
        required=("uiautomation",),
    )
    if gate_err:
        return gate_err
    try:
        with _uia.UIAutomationInitializerInThread():
            control = _uia_resolve_descriptor(cache, descriptor)
            if control is None:
                return {"error": "the accessibility element changed or disappeared; call desktop_snapshot again"}
            outcome = operation(control)
            if outcome is False:
                return {"error": f"the application refused the semantic {action_name.lower()} action"}
            after_result = _uia_capture(
                str((cache.get("window") or {}).get("window_id") or ""),
                max_depth=8,
                max_elements=max(250, len(cache.get("records") or [])),
            )
    except Exception as exc:
        return {"error": f"{action_name.lower()} failed: {exc}"}

    result = {"ok": True, "action": action_name.lower(), "element": public}
    if after_result.get("ok"):
        with _desktop_snapshot_lock:
            after_cache = _desktop_snapshot_cache.get(after_result["snapshot_id"], {})
        result.update({
            "next_snapshot_id": after_result["snapshot_id"],
            "state_token": after_result["state_token"],
            "window": after_result["window"],
            "changes": _uia_diff(cache, after_cache),
        })
    else:
        result["verification_error"] = after_result.get("error")
    return result


def tool_desktop_invoke(args: dict) -> dict:
    ref = str(args.get("ref") or "").strip()
    if not ref:
        return {"error": "ref is required; call desktop_snapshot first"}
    cache, descriptor, cache_error = _desktop_cache_descriptor(ref)
    if cache_error:
        return {"error": cache_error}
    public = descriptor.get("public") or {}
    label = public.get("name") or public.get("role") or ref

    def invoke(control):
        pattern = _uia_pattern(control, "GetInvokePattern")
        if pattern is not None:
            return pattern.Invoke()
        pattern = _uia_pattern(control, "GetSelectionItemPattern")
        if pattern is not None:
            return pattern.Select()
        pattern = _uia_pattern(control, "GetTogglePattern")
        if pattern is not None:
            return pattern.Toggle()
        pattern = _uia_pattern(control, "GetExpandCollapsePattern")
        if pattern is not None:
            state = int(pattern.ExpandCollapseState)
            if state == int(_uia.ExpandCollapseState.Expanded):
                return pattern.Collapse()
            if state != int(_uia.ExpandCollapseState.LeafNode):
                return pattern.Expand()
        if bool(_uia_safe(control, "IsKeyboardFocusable", False)):
            return control.SetFocus()
        raise RuntimeError("element exposes no supported semantic action")

    return _desktop_element_action(
        ref, "Invoke", f'invoke accessibility element "{label}"',
        {"kind": "desktop.invoke"}, invoke,
    )


def tool_desktop_set_value(args: dict) -> dict:
    ref = str(args.get("ref") or "").strip()
    value = args.get("value")
    if not ref:
        return {"error": "ref is required; call desktop_snapshot first"}
    if not isinstance(value, str):
        return {"error": "value must be a string"}
    if len(value) > 10000:
        return {"error": "value is too long (>10000 chars); split the operation"}
    cache, descriptor, cache_error = _desktop_cache_descriptor(ref)
    if cache_error:
        return {"error": cache_error}
    public = descriptor.get("public") or {}
    sensitive = bool(public.get("password"))
    label = public.get("name") or public.get("role") or ref
    preview = "[redacted password]" if sensitive else (value[:200] + ("…" if len(value) > 200 else ""))

    def set_value(control):
        pattern = _uia_pattern(control, "GetValuePattern")
        if pattern is None:
            raise RuntimeError("element does not support the UI Automation Value pattern")
        return pattern.SetValue(value)

    return _desktop_element_action(
        ref, "Set value", f'set "{label}" to {preview}',
        {"kind": "desktop.set_value", "length": len(value), "sensitive": sensitive},
        set_value,
    )


def tool_desktop_scroll(args: dict) -> dict:
    ref = str(args.get("ref") or "").strip()
    direction = str(args.get("direction") or "down").strip().lower()
    if not ref:
        return {"error": "ref is required; call desktop_snapshot first"}
    if direction not in {"up", "down", "left", "right"}:
        return {"error": "direction must be up, down, left, or right"}
    try:
        amount = max(1, min(10, int(args.get("amount") or 1)))
    except (TypeError, ValueError):
        return {"error": "amount must be an integer from 1 to 10"}

    def scroll(control):
        pattern = _uia_pattern(control, "GetScrollPattern")
        if pattern is None:
            raise RuntimeError("element does not support the UI Automation Scroll pattern")
        increment = _uia.ScrollAmount.SmallIncrement
        decrement = _uia.ScrollAmount.SmallDecrement
        no_amount = _uia.ScrollAmount.NoAmount
        horizontal = no_amount
        vertical = no_amount
        for _ in range(amount):
            if direction == "down":
                vertical = increment
            elif direction == "up":
                vertical = decrement
            elif direction == "right":
                horizontal = increment
            else:
                horizontal = decrement
            if pattern.Scroll(horizontal, vertical) is False:
                return False
        return True

    return _desktop_element_action(
        ref, "Scroll", f"scroll accessibility element {direction} x{amount}",
        {"kind": "desktop.scroll", "direction": direction, "amount": amount}, scroll,
    )
