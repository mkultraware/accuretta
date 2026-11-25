"""The app's source, for tests that check how code is written.

bridge.py was split into the accuretta package, and app.js is being split into
ui-*.js files. A test that reads only bridge.py or app.js keeps passing while the
code it guards moves out, and a "must not appear" check then quietly checks
nothing. Read the whole app instead.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def app_files() -> list[Path]:
    """bridge.py and every module split out of it."""
    return [ROOT / "bridge.py", *sorted((ROOT / "accuretta").rglob("*.py"))]


def app_source() -> str:
    """All of the app's Python as one text (files joined by a newline)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in app_files())


def frontend_files() -> list[Path]:
    """app.js and every section split out of it (ui-*.js)."""
    return [ROOT / "app.js", *sorted(ROOT.glob("ui-*.js"))]


def frontend_source() -> str:
    """app.js and its ui-*.js sections as one text (files joined by a newline)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in frontend_files())
