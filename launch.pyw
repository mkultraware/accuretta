# SPDX-License-Identifier: AGPL-3.0-only
"""Start Accuretta from the Windows installer's shortcut.

The installer puts the app in `<install dir>\\app` and its own Python in
`<install dir>\\python`. This launcher keeps the user's files (chats, settings,
skills, keys, downloaded llama.cpp builds) in a folder outside the app, so an
upgrade that replaces the app files never touches them, then opens the normal
desktop app. A git checkout uses start.bat instead and never runs this file.
"""
import os
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _user_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Accuretta"


def main() -> None:
    home = Path(os.environ.get("ACCURETTA_HOME") or _user_folder())
    home.mkdir(parents=True, exist_ok=True)
    os.environ["ACCURETTA_HOME"] = str(home)
    os.chdir(HERE)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    runpy.run_path(str(HERE / "accuretta_app.py"), run_name="__main__")


if __name__ == "__main__":
    main()
