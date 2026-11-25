"""app.js sections that live in their own ui-*.js file are wired up everywhere.

A section file only registers a function (window.Acc.sections["name"]); app.js
calls it where the section used to be, so the code still runs at the same point
of start-up. That only works when the file is served, loaded before app.js, and
called exactly once.
"""
import re
import unittest

import bridge
from accuretta.server.http_handler import STATIC_WHITELIST, UI_SECTION_FILES

ROOT = bridge.ROOT
FILES = sorted(f"web/ui/{p.name}" for p in (ROOT / "web" / "ui").glob("ui-*.js"))
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")
APP = (ROOT / "web" / "js" / "app.js").read_text(encoding="utf-8")


def _slug(name):
    base = name.rsplit("/", 1)[-1]
    return base[len("ui-"):-len(".js")]


class UiSectionWiringTests(unittest.TestCase):
    def test_every_file_is_served(self):
        self.assertEqual(sorted(UI_SECTION_FILES), FILES)
        for name in FILES:
            self.assertIn(name, STATIC_WHITELIST)

    def test_every_file_loads_before_app_js(self):
        app_tag = re.search(r'<script src="/web/js/app\.js\?v=[^"]*"></script>', INDEX)
        self.assertIsNotNone(app_tag)
        for name in FILES:
            with self.subTest(file=name):
                tags = list(re.finditer(rf'<script src="/{re.escape(name)}\?v=\d+"></script>', INDEX))
                self.assertEqual(len(tags), 1)
                self.assertLess(tags[0].start(), app_tag.start())

    def test_each_section_is_registered_and_called_once(self):
        for name in FILES:
            slug = _slug(name)
            with self.subTest(file=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertEqual(text.count(f'window.Acc.sections["{slug}"] = '), 1)
                self.assertEqual(len(re.findall(rf'Acc\.sections\["{re.escape(slug)}"\]\(', APP)), 1)

    def test_app_js_calls_no_missing_section(self):
        called = set(re.findall(r'Acc\.sections\["([a-z0-9-]+)"\]\(', APP))
        self.assertEqual(called, {_slug(n) for n in FILES})


if __name__ == "__main__":
    unittest.main()
