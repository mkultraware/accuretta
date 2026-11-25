"""Removing a project from the sidebar deletes its sessions, never its files.

The owner's choice (2026-11-24): the project leaves the sidebar, its sessions are
deleted like Clear conversations does, and the folder leaves the workspace list.
The folder and its files on disk are not touched.
"""
import re
import unittest

from _source import frontend_files

FILES = {p.name: p.read_text(encoding="utf-8") for p in frontend_files()}
CSS = (frontend_files()[0].parent / "workbench.css").read_text(encoding="utf-8")


def _function(name):
    for text in FILES.values():
        start = text.find(f"  async function {name}(")
        if start >= 0:
            return text[start:text.index("\n  }\n", start)]
    raise AssertionError(f"{name} not found")


class RemoveProjectTests(unittest.TestCase):
    def test_asks_first_and_says_files_stay(self):
        body = _function("removeProjectGroup")
        self.assertIn("confirmModal(", body)
        self.assertIn("The folder and its files stay on disk.", body)
        self.assertLess(body.index("confirmModal("), body.index('method: "DELETE"'))
        self.assertIn("if (!ok) return;", body)

    def test_deletes_sessions_and_leaves_the_workspace_list(self):
        body = _function("removeProjectGroup")
        self.assertIn("/api/chats/${encodeURIComponent(id)}", body)
        self.assertIn('method: "DELETE"', body)
        self.assertIn('api("/api/workspace"', body)
        self.assertIn("JSON.stringify({ folders: next })", body)

    def test_never_touches_files(self):
        body = _function("removeProjectGroup")
        for forbidden in ("/api/tools", "delete_file", "/delete", "/trash", "/remove", "remove_item"):
            self.assertNotIn(forbidden, body)

    def test_only_folder_projects_get_the_button(self):
        text = FILES["ui-command-palette.js"]
        at = text.index('remove.className = "iconbtn sb-group-remove";')
        guard = text.rfind("if (sections && g.path) {", 0, at)
        self.assertGreater(guard, 0)
        self.assertNotIn("\n        }\n", text[guard:at])   # still inside that block
        self.assertIn("removeProjectGroup(g)", text[at:at + 600])

    def test_button_shows_with_the_files_button(self):
        self.assertIn(".sb-group-remove { position: absolute; top: 5px; right: 40px;", CSS)
        self.assertTrue(re.search(r"> :is\(\.sb-group-files, \.sb-group-remove\) \{ opacity: 1; \}", CSS))


if __name__ == "__main__":
    unittest.main()
