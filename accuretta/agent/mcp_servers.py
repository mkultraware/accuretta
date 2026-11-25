# SPDX-License-Identifier: AGPL-3.0-only
"""Managing MCP servers: their config file, starting, stopping and restarting them,
importing and saving entries, and listing them with their tools.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from accuretta.agent.tool_contracts import _ANALYSIS_TOOL_NAMES
from accuretta.agent.workbench_status import _ACTIVE_MCP_CLIENTS
from accuretta.agent.workbench_status import _WORKBENCH_CACHE
from accuretta.core.mcp_paths import _MCP_CONFIG_FILE
from accuretta.core.paths import USER_DIR
from accuretta.core.tool_registry import TOOLS
from accuretta.tools.mcp_calls import _mcp_make_tool_callable
from accuretta.tools.mcp_client import MCPClient
from typing import Any


def _prune_playwright_mcp(days: int = 3) -> None:
    try:
        pw_dir = USER_DIR / ".playwright-mcp"
        if not pw_dir.exists():
            return
        now = time.time()
        for f in pw_dir.iterdir():
            if f.is_file() and now - f.stat().st_mtime > days * 86400:
                f.unlink(missing_ok=True)
    except Exception as e:
        print(f"[!] Failed to prune playwright-mcp: {e}")


def _load_mcp_servers():
    """Startup: start every enabled server in bridge_mcp_config.json, one
    after another (same order and blocking behavior as before the MCP
    window existed)."""
    _prune_playwright_mcp()
    try:
        config = _mcp_read_config()
    except ValueError as e:
        print(f"[!] Failed to parse bridge_mcp_config.json: {e}")
        return
    for server_name, server_config in config.get("mcpServers", {}).items():
        if isinstance(server_config, dict) and not server_config.get("disabled"):
            _mcp_start_server(server_name, server_config)


_MCP_LOCK = threading.RLock()


_MCP_STATE: dict[str, dict] = {}


_MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")


_MCP_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _mcp_read_config() -> dict:
    try:
        raw = _MCP_CONFIG_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"mcpServers": {}}
    try:
        cfg = json.loads(raw) if raw.strip() else {}
    except ValueError as e:
        raise ValueError(f"bridge_mcp_config.json is not valid JSON ({e})") from None
    if not isinstance(cfg, dict):
        raise ValueError("bridge_mcp_config.json must hold a JSON object")
    if not isinstance(cfg.get("mcpServers"), dict):
        cfg["mcpServers"] = {}
    return cfg


def _mcp_write_config(cfg: dict) -> None:
    tmp = _MCP_CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, _MCP_CONFIG_FILE)
    _WORKBENCH_CACHE.pop("mcp", None)


def _mcp_clean_server(raw: Any, existing: dict | None) -> dict:
    """Validate one server entry from the window. Env values of None keep
    what is already stored, so saved keys never round-trip through the UI."""
    if not isinstance(raw, dict):
        raise ValueError("server entry must be an object")
    if raw.get("url") or str(raw.get("type") or "").lower() in {"http", "sse", "streamable-http"}:
        raise ValueError("remote (HTTP/SSE) MCP servers aren't supported yet; "
                         "use a command that runs the server locally")
    command = str(raw.get("command") or "").strip()
    if not command or len(command) > 512 or any(c in command for c in "\r\n\0"):
        raise ValueError("command is required (one line, at most 512 characters)")
    args = raw.get("args") or []
    if isinstance(args, str):
        args = [args]
    if not isinstance(args, list) or len(args) > 64:
        raise ValueError("args must be a list of at most 64 strings")
    clean_args = []
    for arg in args:
        if isinstance(arg, bool) or not isinstance(arg, (str, int, float)):
            raise ValueError("every arg must be text")
        arg = str(arg)
        if len(arg) > 4096 or "\0" in arg:
            raise ValueError("an arg is too long")
        clean_args.append(arg)
    env = raw.get("env") or {}
    if not isinstance(env, dict) or len(env) > 64:
        raise ValueError("env must map at most 64 names to values")
    old_env = (existing or {}).get("env") or {}
    clean_env = {}
    for key, value in env.items():
        key = str(key).strip()
        if not _MCP_ENV_KEY_RE.match(key):
            raise ValueError(f"invalid environment variable name: {key[:40]!r}")
        if value is None:
            if key in old_env:
                clean_env[key] = str(old_env[key])
            continue
        value = str(value)
        if len(value) > 8192 or "\0" in value:
            raise ValueError(f"value for {key} is too long")
        clean_env[key] = value
    out = {k: v for k, v in (existing or {}).items() if k not in {"command", "args", "env", "disabled"}}
    out.update({"command": command, "args": clean_args, "env": clean_env})
    if raw.get("disabled"):
        out["disabled"] = True
    return out


def _mcp_stop_server(name: str) -> None:
    with _MCP_LOCK:
        state = _MCP_STATE.get(name) or {}
        for tool in state.get("registered", []):
            TOOLS.pop(tool, None)
            _ANALYSIS_TOOL_NAMES.discard(tool)
        if state:
            state.update(state="stopped", registered=[], tools=[])
        clients = [c for c in _ACTIVE_MCP_CLIENTS if c.name == name]
        for client in clients:
            _ACTIVE_MCP_CLIENTS.remove(client)
        _WORKBENCH_CACHE.pop("mcp", None)
    for client in clients:
        client.stop()


def _mcp_start_server(name: str, server_config: dict, gen: int | None = None) -> dict:
    """(Re)start one server and register its tools as mcp_<server>_<tool>.
    A newer start for the same name (a quick second save) wins: the older
    one sees its generation is stale and shuts its own process down."""
    with _MCP_LOCK:
        if gen is None:
            gen = _mcp_next_gen(name)
        elif _MCP_STATE.get(name, {}).get("gen") != gen:
            return _MCP_STATE.get(name, {})
    _mcp_stop_server(name)
    with _MCP_LOCK:
        if _MCP_STATE.get(name, {}).get("gen") != gen:
            return _MCP_STATE.get(name, {})
        _MCP_STATE[name] = {"state": "starting", "error": "", "tools": [], "registered": [],
                            "started": time.time(), "gen": gen, "stderr": []}

    def fail(error: str, client=None) -> dict:
        if client is not None:
            client.stop()
        with _MCP_LOCK:
            cur = _MCP_STATE.get(name)
            if cur and cur.get("gen") == gen:
                cur.update(state="error", error=error,
                           stderr=client.stderr_lines() if client is not None else [])
            _WORKBENCH_CACHE.pop("mcp", None)
        return _MCP_STATE.get(name, {})

    try:
        client = MCPClient(name=name, command=server_config["command"],
                           args=server_config.get("args", []), env=server_config.get("env", {}))
    except Exception as e:
        return fail(str(e))
    print(f"[*] Starting MCP server: {name}...")
    res = client.start()
    if "error" in res:
        print(f"[!] Failed to start MCP {name}: {res['error']}")
        return fail(_mcp_error_text(res["error"]), client)
    tools_response = client.list_tools()
    if "error" in tools_response:
        print(f"[!] Failed to list tools for {name}: {tools_response['error']}")
        return fail(f"tools/list failed: {_mcp_error_text(tools_response['error'])}", client)
    mcp_tools = tools_response.get("result", {}).get("tools", []) or []
    with _MCP_LOCK:
        cur = _MCP_STATE.get(name)
        stale = not cur or cur.get("gen") != gen
        if not stale:
            _ACTIVE_MCP_CLIENTS.append(client)
            registered, listed = [], []
            for tool_def in mcp_tools:
                if not isinstance(tool_def, dict) or not tool_def.get("name"):
                    continue
                tool_name = tool_def["name"]
                # prefix tool name to avoid collisions
                prefixed_name = f"mcp_{name}_{tool_name}"
                # Inject into the global TOOLS dictionary
                TOOLS[prefixed_name] = {
                    "description": f"[MCP: {name}] {tool_def.get('description', '')}. Approval follows the selected access mode.",
                    "parameters": tool_def.get("inputSchema", {}),
                    "fn": _mcp_make_tool_callable(client, tool_name, tool_def.get('description', ''), prefixed_name),
                }
                # Add to analysis tool names so it's registered
                _ANALYSIS_TOOL_NAMES.add(prefixed_name)
                registered.append(prefixed_name)
                listed.append({"name": tool_name, "description": str(tool_def.get("description") or "")[:300]})
                print(f"  -> Registered MCP tool: {prefixed_name}")
            cur.update(state="running", error="", tools=listed, registered=registered)
            _WORKBENCH_CACHE.pop("mcp", None)
    if stale:
        client.stop()
    return _MCP_STATE.get(name, {})


def _mcp_error_text(err: Any) -> str:
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


def _mcp_next_gen(name: str) -> int:
    cur = _MCP_STATE.setdefault(name, {"gen": 0})
    cur["gen"] = int(cur.get("gen") or 0) + 1
    return cur["gen"]


def _mcp_start_async(name: str, server_config: dict) -> None:
    with _MCP_LOCK:
        gen = _mcp_next_gen(name)
        _MCP_STATE[name].update(state="starting", error="", stderr=[])
        _WORKBENCH_CACHE.pop("mcp", None)
    threading.Thread(target=_mcp_start_server, args=(name, server_config, gen),
                     daemon=True, name=f"mcp-start-{name}").start()


def _mcp_tool_tokens(registered: list) -> int:
    """Rough context cost of a server's tool definitions (name, description
    and input schema as the model receives them), at ~4 characters a token.
    Shown in the MCP popover; it's an estimate, not a tokenizer count."""
    chars = 0
    for name in registered:
        spec = TOOLS.get(name)
        if not spec:
            continue
        try:
            chars += len(json.dumps({"name": name, "description": spec.get("description", ""),
                                     "parameters": spec.get("parameters", {})}))
        except (TypeError, ValueError):
            chars += len(name) + len(str(spec.get("description", "")))
    return (chars + 3) // 4


def _mcp_unsandboxed(sc: dict) -> bool:
    """True when a server is told to run its browser without Chromium's
    sandbox: --no-sandbox in its arguments, or Playwright MCP's env switch.
    The MCP window warns about it; Accuretta still runs it as configured."""
    for arg in sc.get("args") or []:
        if isinstance(arg, str) and arg.strip().lower().split("=", 1)[0] == "--no-sandbox":
            return True
    flag = str((sc.get("env") or {}).get("PLAYWRIGHT_MCP_NO_SANDBOX") or "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def _mcp_list_servers() -> dict:
    out = {"file": _MCP_CONFIG_FILE.name, "servers": [], "error": ""}
    try:
        cfg = _mcp_read_config()
    except ValueError as e:
        out["error"] = str(e)
        return out
    with _MCP_LOCK:
        for name, sc in cfg["mcpServers"].items():
            sc = sc if isinstance(sc, dict) else {}
            st = _MCP_STATE.get(name, {})
            disabled = bool(sc.get("disabled"))
            state = "disabled" if disabled else st.get("state") or "stopped"
            out["servers"].append({
                "name": name,
                "command": str(sc.get("command") or ""),
                "args": [str(a) for a in (sc.get("args") or []) if not isinstance(a, (dict, list))],
                # names only: stored values (API keys) never leave the bridge
                "env": [{"key": str(k), "set": bool(str(v))} for k, v in (sc.get("env") or {}).items()],
                "disabled": disabled,
                "remote": bool(sc.get("url")),
                "unsandboxed": _mcp_unsandboxed(sc),
                "state": state,
                "error": st.get("error", "") if state == "error" else "",
                "tools": st.get("tools", []) if state == "running" else [],
                "tokens": _mcp_tool_tokens(st.get("registered") or []) if state == "running" else 0,
                "stderr": (st.get("stderr") or [])[-12:] if state == "error" else [],
                "started": st.get("started") or 0,
            })
    return out


def _mcp_save_server(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    original = str(body.get("original") or "").strip()
    if not _MCP_NAME_RE.match(name):
        raise ValueError("name: letters, numbers, - and _, up to 40 characters, "
                         "starting with a letter or number")
    with _MCP_LOCK:
        cfg = _mcp_read_config()
        servers = cfg["mcpServers"]
        if original and original not in servers:
            raise ValueError(f"no server named {original}")
        if name != original and name in servers:
            raise ValueError(f"a server named {name} already exists")
        entry = _mcp_clean_server(body, servers.get(original or name))
        if original and original != name:
            # rename in place so the list keeps its order
            cfg["mcpServers"] = {(name if k == original else k): (entry if k == original else v)
                                 for k, v in servers.items()}
        else:
            servers[name] = entry
        _mcp_write_config(cfg)
    if original and original != name:
        _mcp_stop_server(original)
        with _MCP_LOCK:
            _MCP_STATE.pop(original, None)
    if entry.get("disabled"):
        _mcp_stop_server(name)
    else:
        _mcp_start_async(name, entry)
    return _mcp_list_servers()


def _mcp_delete_server(name: str) -> dict:
    with _MCP_LOCK:
        cfg = _mcp_read_config()
        if name not in cfg["mcpServers"]:
            raise ValueError(f"no server named {name}")
        cfg["mcpServers"].pop(name)
        _mcp_write_config(cfg)
        _mcp_next_gen(name)   # a start still in flight shuts itself down
    _mcp_stop_server(name)
    with _MCP_LOCK:
        _MCP_STATE.pop(name, None)
    return _mcp_list_servers()


def _mcp_restart_server(name: str) -> dict:
    cfg = _mcp_read_config()
    entry = cfg["mcpServers"].get(name)
    if not isinstance(entry, dict):
        raise ValueError(f"no server named {name}")
    if entry.get("disabled"):
        raise ValueError(f"{name} is turned off; turn it on first")
    _mcp_start_async(name, entry)
    return _mcp_list_servers()


def _mcp_import(text: str) -> dict:
    """Paste-in from a server's README: {"mcpServers": {...}}, a bare
    {"name": {...}} map, or a fragment like "name": {...}. Existing names
    are replaced, which is what pasting an updated snippet means."""
    text = str(text or "").strip()
    if not text:
        raise ValueError("paste a JSON snippet first")
    if len(text) > 200_000:
        raise ValueError("that snippet is too large")
    try:
        data = json.loads(text)
    except ValueError:
        try:
            data = json.loads("{" + text.strip().strip(",") + "}")
        except ValueError:
            raise ValueError("that isn't valid JSON") from None
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        data = data["mcpServers"]
    elif isinstance(data, dict) and isinstance(data.get("servers"), dict):
        data = data["servers"]
    if not isinstance(data, dict) or not data or any(not isinstance(v, dict) for v in data.values()):
        raise ValueError('expected {"mcpServers": {"name": {"command": "...", "args": [...]}}}')
    added = {}
    for name, raw in data.items():
        name = str(name).strip()
        if not _MCP_NAME_RE.match(name):
            raise ValueError(f"invalid server name: {name[:40]!r}")
        added[name] = _mcp_clean_server(raw, None)
    with _MCP_LOCK:
        cfg = _mcp_read_config()
        cfg["mcpServers"].update(added)
        _mcp_write_config(cfg)
    for name, entry in added.items():
        if entry.get("disabled"):
            _mcp_stop_server(name)
        else:
            _mcp_start_async(name, entry)
    result = _mcp_list_servers()
    result["imported"] = list(added)
    return result
