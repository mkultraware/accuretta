"""The Showcase theme is registered everywhere a theme has to be, and it
keeps its promises: a coloured mid-tone ground (neither a light nor a dark
theme, and not gray), a prompt box of pale glass in its usual shape, your
messages on the right, and a scene that only shows on an empty session."""
import re
import unittest

import bridge
from _source import frontend_source

ROOT = bridge.ROOT
APP = frontend_source()
CSS = (ROOT / "web" / "css" / "showcase.css").read_text(encoding="utf-8")
FIELD = (ROOT / "web" / "js" / "showcase-field.js").read_text(encoding="utf-8")


def _palette():
    css = (ROOT / "web" / "css" / "colors_and_type.css").read_text(encoding="utf-8")
    block = css[css.index('[data-theme="showcase"] {'):]
    return block[:block.index("}")]


def _luminance(hex_colour):
    channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


class ShowcaseRegistrationTests(unittest.TestCase):
    def test_theme_is_in_the_cycle_the_picker_and_the_splash(self):
        cycle = APP[APP.index("const THEME_CYCLE = ["):]
        self.assertIn('"showcase"', cycle[:cycle.index("];")])
        self.assertIn('showcase:          "ph ph-frame-corners",', APP)
        self.assertIn('<option value="showcase">Showcase</option>',
                      (ROOT / "index.html").read_text(encoding="utf-8"))
        desktop = (ROOT / "accuretta_app.py").read_text(encoding="utf-8")
        known = desktop[desktop.index("_KNOWN_THEMES = {"):]
        self.assertIn('"showcase"', known[:known.index("}")])
        # dark ink on the splash: the ground is pale enough to need it
        light = desktop[desktop.index("_LIGHT_THEMES = {"):]
        self.assertIn('"showcase"', light[:light.index("}")])

    def test_stylesheet_and_scene_script_are_served(self):
        self.assertIn("web/css/showcase.css", bridge.STATIC_WHITELIST)
        self.assertIn("web/js/showcase-field.js", bridge.STATIC_WHITELIST)
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="/web/css/showcase.css', html)
        self.assertIn('src="/web/js/showcase-field.js', html)

    def test_it_is_not_treated_as_a_dark_theme(self):
        for name in ("web/css/app.css", "web/css/workspace-shell.css", "web/css/ui-pass.css"):
            with self.subTest(file=name):
                for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
                    if '[data-theme="dark"]' in line:
                        self.assertNotIn('[data-theme="showcase"]', line)


class ShowcaseDesignTests(unittest.TestCase):
    def test_ground_is_mid_tone_with_readable_ink(self):
        block = _palette()
        bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", block).group(1)
        fg = re.search(r"--fg:\s*(#[0-9A-Fa-f]{6})", block).group(1)
        muted = re.search(r"--fg-muted:\s*(#[0-9A-Fa-f]{6})", block).group(1)
        ground = _luminance(bg)
        # between a dark theme (< 0.05) and a light one (> 0.8)
        self.assertGreater(ground, 0.25)
        self.assertLess(ground, 0.55)
        contrast = lambda a, b: (max(a, b) + 0.05) / (min(a, b) + 0.05)
        self.assertGreaterEqual(contrast(ground, _luminance(fg)), 7)
        self.assertGreaterEqual(contrast(ground, _luminance(muted)), 4.5)

    def test_ground_is_a_colour_not_a_gray(self):
        bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", _palette()).group(1)
        channels = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
        self.assertGreaterEqual(max(channels) - min(channels), 60)

    def test_prompt_box_is_pale_glass_in_its_usual_shape(self):
        block = _palette()
        bg = re.search(r"--bg:\s*(#[0-9A-Fa-f]{6})", block).group(1)
        sheet = re.search(r"--sc-sheet:\s*(#[0-9A-Fa-f]{6})", block).group(1)
        # lighter than the ground, never a darker slab on it
        self.assertGreater(_luminance(sheet), _luminance(bg) + 0.15)
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        box = body[body.index(':root[data-theme="showcase"] #app .composer-wrap > .composer {'):]
        box = box[:box.index("}")]
        self.assertIn("border-radius: 26px !important", box)
        self.assertIn("background: var(--sc-glass-hi) !important", box)
        self.assertIn("border: 0 !important", box)      # its size is untouched
        self.assertNotIn("--sc-slate", CSS)

    def test_your_messages_stay_on_the_right_at_normal_size(self):
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        self.assertNotIn(".bubble-row.user", body)       # no re-ordering of the row
        user = body[body.index('[data-theme="showcase"] #app .bubble.user {'):]
        self.assertNotIn("font-size", user[:user.index("}")])

    def test_scene_only_shows_on_an_empty_session_and_can_stand_still(self):
        self.assertIn('document.querySelector(".center .chat.is-welcome .wv-head")', FIELD)
        self.assertIn('matchMedia("(prefers-reduced-motion: reduce)")', FIELD)
        self.assertIn("document.hidden", FIELD)
        self.assertIn('pointerEvents: "none"', FIELD)
        # it never paints: the motion is CSS on a few elements, not a canvas
        self.assertNotIn("getContext", FIELD)
        self.assertNotIn("canvas", FIELD.lower())
        # it sits behind the app's panels, so it never covers what you read
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        scene = body[body.index('[data-theme="showcase"] #showcase-scene {'):]
        self.assertIn("z-index: -1", scene[:scene.index("}")])
        self.assertIn("@media (prefers-reduced-motion: reduce)", CSS)

    def test_every_rule_is_scoped_to_the_theme(self):
        body = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        body = re.sub(r"@keyframes[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", body)
        body = re.sub(r"@(media|container)[^{]*\{", "", body)
        for selector in re.findall(r"(?:^|\})\s*([^{}@]+)\{", body):
            # commas inside :is(...) / :not(...) are not selector separators
            while re.search(r"\([^()]*\)", selector):
                selector = re.sub(r"\([^()]*\)", "", selector)
            for part in selector.split(","):
                part = part.strip()
                if part:
                    self.assertIn('[data-theme="showcase"]', part, part)


if __name__ == "__main__":
    unittest.main()
