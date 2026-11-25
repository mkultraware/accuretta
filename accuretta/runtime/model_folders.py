# SPDX-License-Identifier: AGPL-3.0-only
"""Which folder to offer as the models folder on first run.

New code, not moved from bridge.py. Pure: it only looks at the paths it is given.
"""
from __future__ import annotations

import os
from pathlib import Path


def suggest_models_dir(found: list[dict], home: str | None = None) -> str:
    """The folder that holds the most of the found models, counting subfolders.

    A model kept in its own subfolder (D:\\MODELS\\Inkling\\x.gguf) must not hide
    its siblings (D:\\MODELS\\*.gguf), which is what offering the newest model's
    own folder did. A drive root or the home folder is never offered: scanning
    either would be slow and pull in unrelated files. Ties go to the deeper
    folder, then to the folder with the newest model.
    """
    home_key = os.path.normcase(os.path.abspath(home or str(Path.home())))
    counts: dict[str, int] = {}
    newest: dict[str, int] = {}
    display: dict[str, str] = {}
    for model in found:
        path = str(model.get("path") or "")
        if not path:
            continue
        folder = Path(os.path.abspath(path)).parent
        for cand in (folder, *folder.parents):
            key = os.path.normcase(str(cand))
            if cand.parent == cand or key == home_key:
                break
            counts[key] = counts.get(key, 0) + 1
            newest[key] = max(newest.get(key, 0), int(model.get("modified_at") or 0))
            display.setdefault(key, str(cand))
    if not counts:
        return ""
    best = max(counts, key=lambda k: (counts[k], len(Path(k).parts), newest[k]))
    return display[best]
