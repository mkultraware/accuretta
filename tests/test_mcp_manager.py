"""MCP window backend: the config file stays the source of truth, saved env
values never come back out, and servers start/stop live with their tools
registered and removed from TOOLS."""
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _source import app_source
from _patching import bridge_patch

# Minimal stdio MCP server: answers initialize and tools/list, ignores the rest.
FAKE_SERVER = textwrap.dedent('''
    import json, sys
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if "id" not in msg:
            continue
        if msg.get("method") == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}}
        elif msg.get("method") == "tools/list":
            result = {"tools": [{"name": "ping", "description": "Say pong", "inputSchema": {"type": "object"}}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\\n")
        sys.stdout.flush()
''')


class McpManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cfg = root / "bridge_mcp_config.json"
        self.server = root / "fake_mcp.py"
        self.server.write_text(FAKE_SERVER, encoding="utf-8")
        self.patches = [bridge_patch("_MCP_CONFIG_FILE", self.cfg),
                        bridge_patch("_MCP_STATE", {}),
                        bridge_patch("_ACTIVE_MCP_CLIENTS", [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for name in list(bridge._MCP_STATE):
            bridge._mcp_stop_server(name)
        for p in reversed(self.patches):
            p.stop()
        for name in [n for n in bridge.TOOLS if n.startswith("mcp_fake")]:
            bridge.TOOLS.pop(name, None)
        self.tmp.cleanup()

    def _wait(self, name, want, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            st = next((s for s in bridge._mcp_list_servers()["servers"] if s["name"] == name), None)
            if st and st["state"] == want:
                return st
            time.sleep(0.1)
        self.fail(f"{name} never reached {want}: {st}")

    def test_save_starts_server_and_registers_tools(self):
        out = bridge._mcp_save_server({"name": "fake", "command": sys.executable,
                                       "args": [str(self.server)], "env": {"TOKEN": "s3cret"}})
        self.assertEqual(out["servers"][0]["state"], "starting")
        st = self._wait("fake", "running")
        self.assertEqual([t["name"] for t in st["tools"]], ["ping"])
        self.assertIn("mcp_fake_ping", bridge.TOOLS)
        # the value is stored on disk but never returned to the window
        self.assertEqual(json.loads(self.cfg.read_text())["mcpServers"]["fake"]["env"], {"TOKEN": "s3cret"})
        self.assertEqual(st["env"], [{"key": "TOKEN", "set": True}])
        self.assertNotIn("s3cret", json.dumps(bridge._mcp_list_servers()))

        # turning it off stops the process and removes its tools
        bridge._mcp_save_server({"name": "fake", "original": "fake", "command": sys.executable,
                                 "args": [str(self.server)], "env": {"TOKEN": None}, "disabled": True})
        self.assertNotIn("mcp_fake_ping", bridge.TOOLS)
        self.assertEqual(bridge._ACTIVE_MCP_CLIENTS, [])
        saved = json.loads(self.cfg.read_text())["mcpServers"]["fake"]
        self.assertTrue(saved["disabled"])
        self.assertEqual(saved["env"], {"TOKEN": "s3cret"}, "None keeps the stored value")

        bridge._mcp_delete_server("fake")
        self.assertEqual(json.loads(self.cfg.read_text())["mcpServers"], {})

    def test_failed_start_reports_the_error(self):
        bridge._mcp_save_server({"name": "fakebroken", "command": sys.executable,
                                 "args": ["-c", "import sys; sys.stderr.write('boom: missing key\\n')"]})
        st = self._wait("fakebroken", "error", timeout=45)
        self.assertTrue(st["error"])
        self.assertIn("boom: missing key", "\n".join(st["stderr"]))

    def test_import_accepts_readme_snippets(self):
        snippet = '"fakeimport": {"command": "npx", "args": ["-y", "some-mcp"], "disabled": true}'
        out = bridge._mcp_import(snippet)
        self.assertEqual(out["imported"], ["fakeimport"])
        self.assertEqual(out["servers"][0]["state"], "disabled")
        with self.assertRaises(ValueError):
            bridge._mcp_import('{"mcpServers": {"x": {"url": "https://example.com/mcp"}}}')

    def test_validation(self):
        bad = [{"name": "has space", "command": "x"},
               {"name": "ok", "command": ""},
               {"name": "ok", "command": "x", "env": {"1BAD": "v"}},
               {"name": "ok", "command": "x\nrm"}]
        for body in bad:
            with self.assertRaises(ValueError):
                bridge._mcp_save_server(body)

    def test_hand_edited_keys_survive_a_save(self):
        self.cfg.write_text(json.dumps({"mcpServers": {"fakekeep": {
            "command": "x", "args": [], "disabled": True, "cwd": "C:/somewhere"}}, }), encoding="utf-8")
        bridge._mcp_save_server({"name": "fakekeep", "original": "fakekeep", "command": "y", "disabled": True})
        self.assertEqual(json.loads(self.cfg.read_text())["mcpServers"]["fakekeep"]["cwd"], "C:/somewhere")

    def test_broken_file_is_reported_not_overwritten(self):
        self.cfg.write_text("{not json", encoding="utf-8")
        self.assertIn("not valid JSON", bridge._mcp_list_servers()["error"])
        with self.assertRaises(ValueError):
            bridge._mcp_save_server({"name": "fake", "command": "x"})
        self.assertEqual(self.cfg.read_text(), "{not json")


class McpRouteTests(unittest.TestCase):
    def test_only_loopback_may_edit(self):
        src = app_source()
        post = src[src.index("    def _handle_api_post(self, p: str, parsed):"):]
        post = post[:post.index('if p == "/api/security/refresh"')]
        self.assertIn("if not self._peer_is_loopback():", post)
        for route in ("/api/mcp/servers", "/api/mcp/servers/delete", "/api/mcp/servers/restart", "/api/mcp/import"):
            self.assertIn(route, post)

    def test_config_file_is_gitignored(self):
        ignore = (bridge.ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("bridge_mcp_config.json", ignore)


if __name__ == "__main__":
    unittest.main()
