# SPDX-License-Identifier: AGPL-3.0-only
"""The MCP client: starting an MCP server, speaking JSON-RPC to it over stdio, and
listing and calling its tools.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from accuretta.security.child_env import _child_env
from collections import deque


class MCPClient:
    def __init__(self, name: str, command: str, args: list[str] = None, env: dict[str, str] = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.process = None
        self.request_id = 0
        self.responses = {}
        self.condition = threading.Condition()
        self.running = False
        self._reader_thread = None
        # stderr was piped but never read: a chatty server could fill the pipe
        # and stall, and a failed start left no clue why. Keep the tail.
        self._stderr_tail = deque(maxlen=40)

    def stderr_lines(self) -> list[str]:
        return [ln[:400] for ln in self._stderr_tail]

    def _read_stderr(self):
        try:
            for line in self.process.stderr:
                line = line.rstrip()
                if line:
                    self._stderr_tail.append(line)
        except Exception:
            pass

    def start(self):
        # Third-party code: the basics plus the variables saved for this
        # server in the MCP servers window, never the bridge's own environment.
        process_env = _child_env(self.env, keep=("network_public",))

        try:
            self.process = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=process_env,
                text=True,
                bufsize=1,
                shell=(os.name == 'nt')
            )
        except Exception as e:
            return {"error": str(e)}
            
        self.running = True
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        init_response = self._send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "accuretta-bridge",
                "version": "1.0.0"
            }
        })
        
        self._send_notification("notifications/initialized")
        return init_response

    def _read_stdout(self):
        for line in self.process.stdout:
            if not self.running:
                break
            try:
                message = json.loads(line)
                if "id" in message and ("result" in message or "error" in message):
                    with self.condition:
                        self.responses[message["id"]] = message
                        self.condition.notify_all()
            except json.JSONDecodeError:
                pass

    def _send_request(self, method: str, params: dict = None, deadline: float = 30.0) -> dict:
        self.request_id += 1
        req_id = self.request_id
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {}
        }
        
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except Exception as e:
            return {"error": {"message": f"Failed to write to MCP stdin: {e}"}}

        # Deadline guard: bound every request so a wedged MCP server returns a
        # structured error instead of freezing the agent turn.
        start = time.monotonic()
        with self.condition:
            while req_id not in self.responses and self.running and self.process.poll() is None:
                remaining = deadline - (time.monotonic() - start)
                if remaining <= 0:
                    break
                self.condition.wait(timeout=min(0.1, remaining))

            if req_id in self.responses:
                return self.responses.pop(req_id)

        code = self.process.poll()
        if code is not None:
            return {"error": {"message": f"the server exited (code {code}) before answering '{method}'. Its output below usually says why."}}
        return {"error": {"message": f"MCP request '{method}' timed out after {deadline:.0f}s — the server may be stuck. Check the MCP server's console/logs, then restart it."}}

    def _send_notification(self, method: str, params: dict = None):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {}
        }
        try:
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
        except Exception:
            pass

    def list_tools(self) -> dict:
        return self._send_request("tools/list", deadline=30.0)

    def call_tool(self, name: str, arguments: dict) -> dict:
        # Long deadline: real tool calls (browser navigation, disk scans, big page
        # snapshots) can take minutes; only a genuinely hung server crosses this.
        return self._send_request("tools/call", {
            "name": name,
            "arguments": arguments
        }, deadline=300.0)

    def stop(self):
        self.running = False
        proc = self.process
        if not proc:
            return
        if proc.poll() is not None:
            self._close_pipes()
            return
        try:
            if os.name == "nt":
                # shell=True means cmd.exe owns the real server (node/python);
                # terminate() would leave that child running. Kill the tree.
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10, env=_child_env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        self._close_pipes()

    def _close_pipes(self):
        # A dead server's stdin may still hold an unflushed request; closing
        # it here (and swallowing the broken-pipe flush) keeps it from
        # surfacing later as an unraisable error during garbage collection.
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
