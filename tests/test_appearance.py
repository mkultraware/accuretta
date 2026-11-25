import ast
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bridge
from _patching import bridge_patch
from tests._sandbox import sandbox_user_files


class AppearanceTests(unittest.TestCase):

    def setUp(self):
        sandbox_user_files(self)

    def test_palette_persistence_validation_and_splash(self):
        with tempfile.TemporaryDirectory() as directory:
            with bridge_patch("SETTINGS_FILE", Path(directory) / "settings.json"):
                colors = {"background": "#142030", "surface": "#253040", "accent": "#eeaa77"}
                bridge.update_settings({"theme": "paper", "custom_palettes": {"paper": colors}})
                self.assertEqual(bridge.get_settings()["custom_palettes"]["paper"], colors)
                tree = ast.parse((bridge.ROOT / "accuretta_app.py").read_text(encoding="utf-8"))
                function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_splash_palette")
                namespace = {"bridge": bridge, "re": re}
                exec(compile(ast.Module(body=[function], type_ignores=[]), "splash", "exec"), namespace)
                palette = namespace["_splash_palette"]("paper")
                self.assertEqual(palette["bg"], colors["background"])
                self.assertEqual(palette["accent"], colors["accent"])
                self.assertEqual(palette["fg"], "#faf9f6")
                for invalid in ([], {"paper": {"background": "red"}}, {"paper": {**colors, "accent": "#fff; color:red"}}):
                    with self.assertRaises(ValueError):
                        bridge.update_settings({"custom_palettes": invalid})
                self.assertEqual(bridge.get_settings()["custom_palettes"]["paper"], colors)
                bridge.update_settings({"custom_palettes": {}})
                # "paper" is a retired theme: with no [data-theme="paper"] block
                # in colors_and_type.css the splash falls back to the light
                # default rather than the old warm-paper tone.
                self.assertEqual(namespace["_splash_palette"]("paper")["bg"], "#FAFAFB")


if __name__ == "__main__":
    unittest.main()
