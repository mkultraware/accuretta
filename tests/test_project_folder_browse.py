"""The project picker lists a folder's subfolders through /api/list-folder,
which only allows paths inside a workspace folder."""
import tempfile
import unittest
from pathlib import Path

import bridge
from _source import frontend_source


class PathInWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "proj" / "src" / "deep").mkdir(parents=True)
        (self.tmp / "projects-other").mkdir()

    def test_root_and_its_subfolders_are_allowed(self):
        ws = [str(self.tmp / "proj")]
        self.assertTrue(bridge._path_in_workspace((self.tmp / "proj").resolve(), ws))
        self.assertTrue(bridge._path_in_workspace((self.tmp / "proj" / "src" / "deep").resolve(), ws))

    def test_sibling_with_the_same_prefix_is_refused(self):
        # The old string-prefix check let C:\\proj admit C:\\projects-other.
        ws = [str(self.tmp / "proj")]
        self.assertFalse(bridge._path_in_workspace((self.tmp / "projects-other").resolve(), ws))

    def test_parent_and_unrelated_paths_are_refused(self):
        ws = [str(self.tmp / "proj")]
        self.assertFalse(bridge._path_in_workspace(self.tmp, ws))
        self.assertFalse(bridge._path_in_workspace(Path(tempfile.gettempdir()).resolve(), ws))
        self.assertFalse(bridge._path_in_workspace((self.tmp / "proj").resolve(), []))


class ListSubfoldersTests(unittest.TestCase):
    """The picker can browse any folder, like the native Open folder… dialog."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        (self.tmp / "b-folder").mkdir()
        (self.tmp / "A-folder" / "inner").mkdir(parents=True)
        (self.tmp / "file.txt").write_text("x", encoding="utf-8")

    def test_lists_folders_only_sorted(self):
        r = bridge._list_subfolders(str(self.tmp))
        self.assertEqual([e["name"] for e in r["entries"]], ["A-folder", "b-folder"])
        self.assertNotIn("error", r)

    def test_goes_anywhere_not_just_the_workspace(self):
        r = bridge._list_subfolders(str(self.tmp / "A-folder"))
        self.assertEqual([e["name"] for e in r["entries"]], ["inner"])

    def test_starting_points_are_home_and_drives(self):
        r = bridge._list_subfolders("")
        kinds = [e["kind"] for e in r["entries"]]
        self.assertEqual(kinds[0], "home")
        self.assertIn("drive", kinds)

    def test_missing_folder_and_file_report_an_error(self):
        self.assertEqual(bridge._list_subfolders(str(self.tmp / "nope"))["error"], "not a folder")
        self.assertEqual(bridge._list_subfolders(str(self.tmp / "file.txt"))["error"], "not a folder")


class PickerWiringTests(unittest.TestCase):
    """The menu re-renders while open, so navigation must stop the click before
    the outside-click handler closes the menu."""

    def test_navigation_clicks_stop_propagation(self):
        src = frontend_source()
        start = src.index("function initProjectPicker()")
        body = src[start:src.index("wireOverflow(\"#project-chip\"", start)]
        nav = body.index('closest("[data-drill-path], #project-back")')
        self.assertIn("event.stopPropagation()", body[nav:nav + 400])
        self.assertIn("menu._reposition = positionMenu", src)


if __name__ == "__main__":
    unittest.main()
