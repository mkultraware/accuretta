"""bridge_patch must behave exactly like patch.object(bridge, ...) and also reach
the same name in accuretta.* modules (see tests/_patching.py)."""
import sys
import types
import unittest
from unittest import mock

import bridge
from _patching import bridge_patch


def _fake_module():
    module = types.ModuleType("accuretta._patchtest")
    module.__dict__["_approval_mode"] = lambda: "original-in-module"
    return module


class BridgePatchTests(unittest.TestCase):
    def setUp(self):
        self.module = _fake_module()
        sys.modules["accuretta._patchtest"] = self.module
        self.addCleanup(sys.modules.pop, "accuretta._patchtest", None)
        self.original_bridge = bridge._approval_mode
        self.original_module = self.module._approval_mode

    def test_one_replacement_lands_everywhere_and_is_restored(self):
        with bridge_patch("_approval_mode", return_value="soft") as fake:
            self.assertIs(bridge._approval_mode, fake)
            self.assertIs(self.module._approval_mode, fake)
            self.assertEqual(bridge._approval_mode(), "soft")
            self.assertEqual(self.module._approval_mode(), "soft")
        self.assertIs(bridge._approval_mode, self.original_bridge)
        self.assertIs(self.module._approval_mode, self.original_module)

    def test_explicit_value_and_side_effect(self):
        value = lambda: "hard"  # noqa: E731
        with bridge_patch("_approval_mode", value):
            self.assertIs(self.module._approval_mode, value)
        with bridge_patch("_approval_mode", side_effect=["a", "b"]):
            self.assertEqual([bridge._approval_mode(), self.module._approval_mode()], ["a", "b"])

    def test_start_stop_like_patch_object(self):
        patcher = bridge_patch("_approval_mode", lambda: "medium")
        patcher.start()
        self.assertEqual(self.module._approval_mode(), "medium")
        patcher.stop()
        self.assertIs(self.module._approval_mode, self.original_module)
        patcher.stop()                              # a second stop is harmless

    def test_nested_patches_restore_in_order(self):
        with bridge_patch("_approval_mode", lambda: "outer"):
            with bridge_patch("_approval_mode", lambda: "inner"):
                self.assertEqual(self.module._approval_mode(), "inner")
            self.assertEqual(self.module._approval_mode(), "outer")
        self.assertIs(bridge._approval_mode, self.original_bridge)

    def test_a_missing_name_fails_like_patch_object(self):
        with self.assertRaises(AttributeError):
            bridge_patch("no_such_name_anywhere", 1).start()
        with bridge_patch("no_such_name_anywhere", 1, create=True):
            self.assertEqual(bridge.no_such_name_anywhere, 1)
        self.assertFalse(hasattr(bridge, "no_such_name_anywhere"))

    def test_autospec_is_shared_not_silently_dropped(self):
        with bridge_patch("_approval_mode", autospec=True) as fake:
            self.assertIs(self.module._approval_mode, fake)
            fake.return_value = "soft"
            self.assertEqual(self.module._approval_mode(), "soft")

    def test_modules_without_the_name_are_left_alone(self):
        other = types.ModuleType("accuretta._other")
        sys.modules["accuretta._other"] = other
        self.addCleanup(sys.modules.pop, "accuretta._other", None)
        with bridge_patch("_approval_mode", lambda: "x"):
            self.assertNotIn("_approval_mode", other.__dict__)

    def test_unrelated_modules_are_never_touched(self):
        foreign = types.ModuleType("someone_else")
        foreign._approval_mode = lambda: "theirs"
        sys.modules["someone_else"] = foreign
        self.addCleanup(sys.modules.pop, "someone_else", None)
        with bridge_patch("_approval_mode", lambda: "x"):
            self.assertEqual(foreign._approval_mode(), "theirs")

    def test_same_as_patch_object_for_bridge_alone(self):
        sys.modules.pop("accuretta._patchtest", None)
        with bridge_patch("_approval_mode", return_value="soft") as a:
            mine = bridge._approval_mode
        with mock.patch.object(bridge, "_approval_mode", return_value="soft") as b:
            theirs = bridge._approval_mode
        self.assertIsInstance(mine, mock.MagicMock)
        self.assertIsInstance(theirs, mock.MagicMock)
        self.assertEqual(a.return_value, b.return_value)


if __name__ == "__main__":
    unittest.main()
