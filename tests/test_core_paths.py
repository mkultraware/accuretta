"""accuretta/core/paths.py: the folders moved out of bridge.py must keep their values."""
import os
import unittest
from pathlib import Path

import bridge
import accuretta.core.paths as paths


class CorePathTests(unittest.TestCase):
    def test_root_is_still_the_folder_that_holds_bridge_py(self):
        self.assertEqual(paths.ROOT, Path(bridge.__file__).resolve().parent)
        self.assertTrue((paths.ROOT / "bridge.py").is_file())

    def test_bridge_re_exports_the_same_objects(self):
        self.assertIs(bridge.ROOT, paths.ROOT)
        self.assertIs(bridge.USER_DIR, paths.USER_DIR)
        self.assertIs(bridge.DATA, paths.DATA)

    def test_data_follows_the_environment(self):
        # The suite sets ACCURETTA_DATA_DIR (conftest); DATA must honour it.
        self.assertEqual(os.path.normcase(str(paths.DATA)),
                         os.path.normcase(str(Path(os.environ["ACCURETTA_DATA_DIR"]).resolve())))


if __name__ == "__main__":
    unittest.main()
