"""First-run setup offers the folder that holds the most models.

It used to offer the newest model's own folder. When the newest model sat in a
subfolder (D:\\MODELS\\Inkling\\x.gguf), the models folder became that subfolder
and every other model was hidden from the model list.
"""
import os
import unittest

import bridge  # noqa: F401  (loads the package the normal way first)
from accuretta.runtime.model_folders import suggest_models_dir

HOME = os.path.abspath(os.path.join(os.sep, "Users", "someone"))
DRIVE = os.path.abspath(os.sep)


def model(*parts, modified=0):
    return {"path": os.path.join(*parts), "modified_at": modified}


class SuggestModelsDirTests(unittest.TestCase):
    def test_newest_in_subfolder_does_not_hide_siblings(self):
        found = [
            model(DRIVE, "MODELS", "Inkling", "inkling.gguf", modified=900),
            model(DRIVE, "MODELS", "qwen-a.gguf", modified=100),
            model(DRIVE, "MODELS", "qwen-b.gguf", modified=200),
        ]
        self.assertEqual(suggest_models_dir(found, home=HOME), os.path.join(DRIVE, "MODELS"))

    def test_one_folder_per_model_offers_their_shared_parent(self):
        # LM Studio keeps each model in publisher\repo\ folders.
        base = os.path.join(HOME, ".lmstudio", "models")
        found = [
            model(base, "pub1", "repo1", "a.gguf"),
            model(base, "pub2", "repo2", "b.gguf"),
        ]
        self.assertEqual(suggest_models_dir(found, home=HOME), base)

    def test_never_offers_home_or_drive_root(self):
        found = [
            model(HOME, "Downloads", "a.gguf"),
            model(HOME, "Desktop", "b.gguf", modified=5),
            model(DRIVE, "c.gguf"),
        ]
        out = suggest_models_dir(found, home=HOME)
        self.assertNotIn(os.path.normcase(out), {os.path.normcase(HOME), os.path.normcase(DRIVE), ""})
        self.assertEqual(out, os.path.join(HOME, "Desktop"))   # tie: the newest model wins

    def test_single_model_in_subfolder_keeps_its_own_folder(self):
        found = [model(DRIVE, "MODELS", "Inkling", "inkling.gguf")]
        self.assertEqual(suggest_models_dir(found, home=HOME),
                         os.path.join(DRIVE, "MODELS", "Inkling"))

    def test_most_models_wins_over_newest(self):
        found = [
            model(HOME, "Downloads", "new.gguf", modified=999),
            model(DRIVE, "MODELS", "a.gguf"),
            model(DRIVE, "MODELS", "b.gguf"),
        ]
        self.assertEqual(suggest_models_dir(found, home=HOME), os.path.join(DRIVE, "MODELS"))

    def test_nothing_found(self):
        self.assertEqual(suggest_models_dir([], home=HOME), "")
        self.assertEqual(suggest_models_dir([{"path": ""}], home=HOME), "")


if __name__ == "__main__":
    unittest.main()
