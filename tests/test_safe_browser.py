"""Untrusted pages open in the installed Edge or Chrome (they update
themselves), never in Playwright's bundled Chromium while either is there,
and always with Chromium's sandbox on."""
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch


class _FakeChromium:
    def __init__(self, available):
        self.available = available
        self.calls = []

    def launch(self, **options):
        self.calls.append(options)
        channel = options.get("channel") or "bundled"
        if channel not in self.available:
            raise RuntimeError(f'Chromium distribution "{channel}" is not found\nmore detail')
        return f"browser:{channel}"


class _FakeEngine:
    def __init__(self, available):
        self.chromium = _FakeChromium(available)


class SafeBrowserTests(unittest.TestCase):
    def test_edge_comes_first_on_windows_and_the_sandbox_is_on(self):
        engine = _FakeEngine({"msedge", "chrome", "bundled"})
        with bridge_patch("_SAFE_CHROMIUM_CHANNELS", ("msedge", "chrome")):
            self.assertEqual(bridge._launch_safe_chromium(engine), "browser:msedge")
        options = engine.chromium.calls[0]
        self.assertIs(options["chromium_sandbox"], True)
        self.assertIs(options["headless"], True)
        self.assertIn("--disable-component-update", options["args"])

    def test_falls_back_in_order_and_stays_sandboxed(self):
        engine = _FakeEngine({"bundled"})
        with bridge_patch("_SAFE_CHROMIUM_CHANNELS", ("msedge", "chrome")):
            self.assertEqual(bridge._launch_safe_chromium(engine), "browser:bundled")
        self.assertEqual([c.get("channel") for c in engine.chromium.calls], ["msedge", "chrome", None])
        self.assertTrue(all(c["chromium_sandbox"] is True for c in engine.chromium.calls))

    def test_no_browser_means_a_clear_error_not_an_unsandboxed_one(self):
        engine = _FakeEngine(set())
        with self.assertRaises(RuntimeError) as caught:
            bridge._launch_safe_chromium(engine)
        message = str(caught.exception)
        self.assertIn("No browser could start with its sandbox on.", message)
        self.assertIn("bundled Chromium:", message)
        self.assertNotIn("more detail", message)
        self.assertTrue(all(c["chromium_sandbox"] is True for c in engine.chromium.calls))

    def test_both_workers_use_it(self):
        # All of the app's Python, since code moves from bridge.py into accuretta/.
        root = bridge.ROOT
        files = [root / "bridge.py", *sorted((root / "accuretta").rglob("*.py"))]
        src = "\n".join(p.read_text(encoding="utf-8") for p in files)
        self.assertEqual(src.count("browser = _launch_safe_chromium(engine)"), 2)
        self.assertEqual(src.count("chromium.launch("), 1)  # the helper's own call


class McpSandboxWarningTests(unittest.TestCase):
    """An MCP server told to run its browser without the sandbox is flagged so
    the MCP window can warn about it (it still runs as configured)."""

    def test_no_sandbox_is_spotted_in_args_and_env(self):
        for sc in ({"args": ["@playwright/mcp@latest", "--no-sandbox"]},
                   {"args": ["--NO-SANDBOX=true"]},
                   {"args": [], "env": {"PLAYWRIGHT_MCP_NO_SANDBOX": "1"}}):
            with self.subTest(sc=sc):
                self.assertTrue(bridge._mcp_unsandboxed(sc))
        for sc in ({"args": ["@playwright/mcp@latest", "--browser=msedge"]},
                   {"args": ["--no-sandboxing-docs"], "env": {"PLAYWRIGHT_MCP_NO_SANDBOX": "false"}},
                   {}):
            with self.subTest(sc=sc):
                self.assertFalse(bridge._mcp_unsandboxed(sc))

    def test_the_server_list_carries_the_flag(self):
        cfg = {"mcpServers": {"pw": {"command": "npx", "args": ["@playwright/mcp", "--no-sandbox"]},
                              "osint": {"command": "npx", "args": ["osint-mcp-server"]}}}
        with bridge_patch("_mcp_read_config", return_value=cfg):
            servers = {s["name"]: s for s in bridge._mcp_list_servers()["servers"]}
        self.assertIs(servers["pw"]["unsandboxed"], True)
        self.assertIs(servers["osint"]["unsandboxed"], False)


if __name__ == "__main__":
    unittest.main()
