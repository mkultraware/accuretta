import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

import bridge
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files


class ProjectWorkspaceMetadataTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "cellar-shop"
        self.project.mkdir()
        workspace_file = self.root / "workspace.json"
        workspace_file.write_text(json.dumps({"folders": [str(self.root)]}), encoding="utf-8")
        workspace_patch = bridge_patch("WORKSPACE_FILE", workspace_file)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)

    def test_plain_folder_inside_workspace_is_accepted(self):
        result = bridge._project_workspace_metadata({"path": str(self.project)})
        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "cellar-shop")
        self.assertEqual(result["branch"], "")

    def test_git_metadata_survives_round_trip(self):
        result = bridge._project_workspace_metadata({
            "name": "cellar-shop", "path": str(self.project), "branch": "main",
            "brief": "inventory app", "published": True, "owner": "octocat",
        })
        self.assertEqual(result["branch"], "main")
        self.assertTrue(result["published"])
        self.assertEqual(result["owner"], "octocat")

    def test_path_outside_workspace_is_rejected(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        self.assertIsNone(bridge._project_workspace_metadata({"path": str(outside)}))

    def test_name_mismatch_and_control_characters_are_rejected(self):
        self.assertIsNone(bridge._project_workspace_metadata({"name": "other", "path": str(self.project)}))
        self.assertIsNone(bridge._project_workspace_metadata({"name": "bad\nname", "path": str(self.project)}))

    def test_prompt_omits_git_lines_for_a_plain_folder(self):
        prompt = bridge._project_workspace_prompt({"project_workspace": {"path": str(self.project)}})
        self.assertIn("cellar-shop", prompt)
        self.assertIn(str(self.project), prompt)
        self.assertNotIn("expected branch", prompt)
        self.assertNotIn("GitHub publication", prompt)


class ProjectEndpointTests(unittest.TestCase):
    def setUp(self):
        sandbox_user_files(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "cellar-shop"
        self.project.mkdir()
        workspace_file = self.root / "workspace.json"
        workspace_file.write_text(json.dumps({"folders": [str(self.root)]}), encoding="utf-8")
        workspace_patch = bridge_patch("WORKSPACE_FILE", workspace_file)
        workspace_patch.start()
        self.addCleanup(workspace_patch.stop)
        chats = bridge.get_chats()
        chats["chats"]["chat-a"] = {"id": "chat-a", "title": "t", "created": 0,
                                    "updated": 0, "messages": [], "origin": "desktop"}
        chats["order"] = ["chat-a"]
        bridge.save_json(bridge.CHATS_FILE, chats)

        class QuietHandler(bridge.Handler):
            def log_message(self, *args):
                pass

        self.server = bridge.AccurettaHTTPServer(("127.0.0.1", 0), QuietHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 1)
        self.addCleanup(self.server.shutdown)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/")
            page = connection.getresponse().read()
        finally:
            connection.close()
        self.token = re.search(rb'name="accuretta-request-token" content="([^"]+)"', page)[1].decode()

    def post(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("POST", path, json.dumps(payload),
                               {"X-Accuretta-Token": self.token, "Content-Type": "application/json"})
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_set_and_clear_project_on_a_chat(self):
        status, body = self.post("/api/chats/chat-a/project",
                                 {"project_workspace": {"path": str(self.project)}})
        self.assertEqual(status, 200)
        self.assertEqual(body["project_workspace"]["name"], "cellar-shop")
        saved = bridge.get_chats()["chats"]["chat-a"]["project_workspace"]
        self.assertEqual(saved["path"], str(self.project))
        status, body = self.post("/api/chats/chat-a/project", {"project_workspace": None})
        self.assertEqual(status, 200)
        self.assertNotIn("project_workspace", bridge.get_chats()["chats"]["chat-a"])

    def test_outside_folder_is_refused_and_unknown_chat_404(self):
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        status, body = self.post("/api/chats/chat-a/project",
                                 {"project_workspace": {"path": str(outside)}})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertEqual(self.post("/api/chats/nope/project", {"project_workspace": None})[0], 404)
