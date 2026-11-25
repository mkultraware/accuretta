"""Regression tests for the 2026-11-20 UI audit (crawler + fake-model chat run).

Static checks read the shipped files; the markdown list test runs the real
_mdListHtml from app.js under node (skipped when node isn't installed).
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import bridge
from _source import app_source, frontend_files, frontend_source

ROOT = Path(__file__).resolve().parent.parent
APP_JS = frontend_source()
APP_CSS = (ROOT / "app.css").read_text(encoding="utf-8")
INDEX = (ROOT / "index.html").read_text(encoding="utf-8")

# Stylesheets the main window loads, plus the page itself.
_MAIN_STYLE_SOURCES = ["app.css", "workbench.css", "ui-pass.css", "ui-refresh.css",
                       "colors_and_type.css", "showcase.css", "opal.css", "research-ui.css",
                       "workspace-shell.css", "index.html", "app.js",
                       *(p.name for p in sorted(ROOT.glob("ui-*.js")))]
# Set at runtime by a script with a helper the scan can't see.
_RUNTIME_TOKENS = {"--xl", "--xr", "--yt", "--yb"}


class UndefinedCssTokenTests(unittest.TestCase):
    def test_every_token_used_without_fallback_is_defined(self):
        """var(--x) with no fallback and no definition drops the whole rule; the
        session search's selected row and the close-dialog spinner went blank."""
        defined, used = set(), {}
        for name in _MAIN_STYLE_SOURCES + [p.name for p in ROOT.glob("*.js")]:
            text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
            defined |= set(re.findall(r"(--[\w-]+)\s*:", text))
            defined |= set(re.findall(r"setProperty\(\s*['\"`](--[\w-]+)", text))
            defined |= set(re.findall(r"['\"](--[\w-]+)['\"]\s*:", text))
            if name in _MAIN_STYLE_SOURCES:
                for m in re.finditer(r"var\(\s*(--[\w-]+)\s*\)", text):
                    used.setdefault(m.group(1), f"{name}:{text.count(chr(10), 0, m.start()) + 1}")
        missing = {k: v for k, v in used.items() if k not in defined and k not in _RUNTIME_TOKENS}
        self.assertEqual(missing, {})


class PhoneLayoutTests(unittest.TestCase):
    def test_settings_hints_break_between_words(self):
        rule = re.search(r"\.drawer-body \.hint \{([^}]*)\}", APP_CSS)
        self.assertIsNotNone(rule)
        self.assertNotIn("break-all", rule.group(1))
        self.assertIn("overflow-wrap: anywhere", rule.group(1))

    def test_touch_padding_skips_checkbox_switches(self):
        self.assertIn('.form-row input:not([type="checkbox"]):not([type="radio"]),', APP_CSS)
        self.assertNotRegex(APP_CSS, r"\n  \.form-row input,\n  \.form-row select,")


class MenuAndErrorHandlingTests(unittest.TestCase):
    def test_overflow_menus_close_on_escape_before_the_composer_sees_it(self):
        start = APP_JS.index("function wireOverflow(")
        body = APP_JS[start:APP_JS.index("\n  }\n", start)]
        self.assertIn('e.key !== "Escape"', body)
        self.assertIn("e.preventDefault()", body)
        self.assertIn("}, true);", body)   # capture phase

    def test_failed_settings_save_is_tagged_and_reported(self):
        start = APP_JS.index("function saveSettings(")
        body = APP_JS[start:APP_JS.index("settingsSaveChain = settingsSaveChain", start)]
        self.assertIn("settingsSave: true", body)
        self.assertIn("if (r && r.settingsSave)", APP_JS)

    def test_theme_step_handles_a_failed_save(self):
        start = APP_JS.index("async function stepTheme(")
        body = APP_JS[start:APP_JS.index("\n  }\n", start)]
        self.assertIn("try { await saveSettings({ theme: next }); }", body)

    def test_new_session_stops_when_the_bridge_returns_no_id(self):
        start = APP_JS.index("async function newChat(")
        body = APP_JS[start:APP_JS.index("\n  }\n", start)]
        self.assertLess(body.index("if (!c || !c.id)"), body.index("await selectChat(c.id)"))

    def test_error_text_from_the_bridge_is_escaped(self):
        self.assertIn('`<option value="">(${esc(msg)})</option>`', APP_JS)
        self.assertIn("escSbx(status.error_msg", INDEX)
        self.assertNotIn("+ sysinfo.gpu_name +", INDEX)

    def test_icon_close_buttons_have_names(self):
        for bid in ("btn-close-shutdown", "btn-close-recon", "btn-close-github-activity"):
            tag = re.search(r'<button[^>]*id="%s"[^>]*>' % bid, INDEX).group(0)
            self.assertIn('aria-label="Close"', tag, bid)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class MarkdownListTests(unittest.TestCase):
    """Runs the shipped list renderer: nesting, a numbered list that continues
    after a paragraph, and type changes at one level."""

    @classmethod
    def setUpClass(cls):
        # The list helpers sit at the end of their section, in app.js or in the
        # ui-*.js file that section moved to (ending at its `return {` line).
        text = next(t for t in (p.read_text(encoding="utf-8") for p in frontend_files())
                    if "  const _MD_LIST_ITEM =" in t)
        start = text.index("  const _MD_LIST_ITEM =")
        ends = [i for i in (text.find("  // ---------- markdown-lite for chat bubbles", start),
                            text.find("\n  return {", start)) if i >= 0]
        cls.src = text[start:min(ends)]

    def render(self, lines):
        script = self.src + (
            "\nconst lines = JSON.parse(process.argv[1]);"
            "\nconst items = lines.map(l => { const m = _MD_LIST_ITEM.exec(l);"
            " return { indent: m[1].length, ordered: /\\d/.test(m[2]), num: parseInt(m[2], 10), text: m[3] }; });"
            "\nprocess.stdout.write(_mdListHtml(items));")
        out = subprocess.run(["node", "-e", script, json.dumps(lines)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_indented_items_nest(self):
        html = self.render(["- a", "  - b", "    1. c", "- d"])
        self.assertEqual(html, '<ul class="md-list"><li>a<ul class="md-list"><li>b'
                               '<ol class="md-list"><li>c</li></ol></li></ul></li><li>d</li></ul>')

    def test_numbered_list_keeps_its_first_number(self):
        self.assertEqual(self.render(["3. three", "4. four"]),
                         '<ol class="md-list" start="3"><li>three</li><li>four</li></ol>')

    def test_switching_marker_type_starts_a_new_list(self):
        self.assertEqual(self.render(["1. one", "- dash"]),
                         '<ol class="md-list"><li>one</li></ol><ul class="md-list"><li>dash</li></ul>')

    def test_marker_regex_ignores_bold_text(self):
        self.assertIsNone(re.match(r"^(\s*)([-*+]|\d+\.)\s+(.*)$", "**bold** start"))


class SteerKeepsPartialTests(unittest.TestCase):
    """A correction sent mid-answer keeps the part already written (the owner's
    choice, 2026-11-20): in the model's context, in the saved chat, and on screen."""

    def test_written_prose_is_kept_with_its_reasoning(self):
        kept = bridge._steer_kept_partial("<think>plan</think>", "Here are the first two points")
        self.assertEqual(kept, {"role": "assistant", "_steer_partial": True,
                                "content": "<think>plan</think>Here are the first two points"})

    def test_unfinished_tool_call_is_dropped_and_thinking_only_keeps_nothing(self):
        kept = bridge._steer_kept_partial("", 'Reading it now.<tool_call>{"name": "read_fi')
        self.assertEqual(kept["content"], "Reading it now.")
        self.assertIsNone(bridge._steer_kept_partial("<think>still planning</think>", ""))
        self.assertIsNone(bridge._steer_kept_partial("", "<tool_call>{\"name\": \"x\"}</tool_call>"))

    def test_continue_note_goes_on_the_models_copy_only(self):
        wire = bridge._with_steer_continue_note({"role": "user", "content": "make it shorter"})
        self.assertTrue(wire["content"].startswith("make it shorter\n\n[automatic note]"))
        parts = bridge._with_steer_continue_note(
            {"role": "user", "content": [{"type": "text", "text": "see this"}]})
        self.assertEqual(parts["content"][-1], {"type": "text", "text": bridge._STEER_CONTINUE_NOTE})
        source = app_source()
        start = source.index("def _apply_task_updates(")
        body = source[start:source.index("return bool(updates)", start)]
        # the saved text stays the user's own words
        self.assertIn("_steering_text=update[\"text\"]", body)
        self.assertLess(body.index("conversation.append(kept_partial)"), body.index("for n, update"))

    def test_both_interruption_points_keep_the_partial(self):
        source = app_source()
        # mid-stream, just after the stream, and when the final answer was complete
        self.assertEqual(source.count("_apply_task_updates(_updates, "), 3)
        self.assertEqual(source.count("_apply_task_updates(_updates, _steer_kept_partial("), 2)
        # the between-rounds check (nothing written yet) stays as it was
        self.assertEqual(source.count("if _apply_task_updates(_steering.take(chat_id)):"), 1)

    def test_saved_partial_stays_flagged(self):
        source = app_source()
        self.assertIn('"_steering_images", "_steer_partial")', source)            # turn journal
        self.assertEqual(source.count('persisted["_steer_partial"] = True'), 2)   # both save paths

    def test_chat_view_and_search_show_the_partial(self):
        self.assertIn("(!m._internal || !!m._steer_partial)", APP_JS)
        self.assertEqual(APP_JS.count(".filter(isRenderedMessage)"), 2)
        self.assertIn("if (evt.kept_partial)", APP_JS)
        import conversation_tools
        chats = {"chats": {"c1": {"updated": 1, "messages": [
            {"role": "user", "content": "write a long poem"},
            {"role": "assistant", "content": "Roses are red", "_internal": True, "_steer_partial": True},
            {"role": "user", "content": "make it about the sea", "_steering_id": "u1"},
            {"role": "assistant", "content": "the sea is wide, zebrafinch"},
        ]}}}
        hit = conversation_tools.search_conversations(chats, "zebrafinch")[0]
        self.assertEqual(hit["visible_index"], 3)


class PhoneComposerTests(unittest.TestCase):
    """Phones (2026-11-20, owner's pick "A"): no strip above the box, the folder
    chip in the box's bottom row after Access, context from the status line."""

    def test_strip_is_hidden_on_phones_and_the_chip_styled_in_the_row(self):
        block = APP_CSS[APP_CSS.index("/* Phones: no strip above the box."):]
        block = block[:block.index("\n}\n") + 3]
        self.assertIn("@media (max-width: 600px)", block)
        self.assertIn("#app .composer-wrap > .composer-projects {\n    display: none !important;", block)
        self.assertIn(":root[data-theme] #app .composer-tools > .project-chip {", block)

    def test_chip_moves_after_access_on_phones_and_back_on_wider_screens(self):
        start = APP_JS.index("function applyMobileToolbarLayout(")
        body = APP_JS[start:APP_JS.index("\n  }\n", start)]
        self.assertIn("if (mobile && projectChip.parentNode !== tools) permsControl.after(projectChip);", body)
        self.assertIn("projectStrip.prepend(projectChip)", body)

    def test_status_line_context_opens_the_context_card(self):
        self.assertIn('class="status-item status-ctx" data-s="ctx" role="button" tabindex="0"', APP_JS)
        self.assertIn('ev.target.closest("#ctx-gauge, .status-ctx")', APP_JS)
        start = APP_JS.index("function showCtxPopover(")
        body = APP_JS[start:APP_JS.index("\n  }\n", start)]
        # placed before it can paint, so a tap can't land on an unplaced card
        self.assertLess(body.index("positionCtxPopover();"), body.index("requestAnimationFrame"))

    def test_taps_do_not_pop_hover_tooltips(self):
        start = APP_JS.index("function initTooltips(")
        body = APP_JS[start:APP_JS.index("\n  function wireEvents(", start)]
        self.assertIn('touchPointer = e.pointerType !== "mouse";', body)
        self.assertIn("if (!el.dataset.tip || touchPointer) return;", body)


class LlamaErrorTextTests(unittest.TestCase):
    def test_openai_style_error_gives_the_message(self):
        body = json.dumps({"error": {"code": 500, "message": "failed to allocate KV cache", "type": "server_error"}})
        self.assertEqual(bridge._llama_error_text(body), "failed to allocate KV cache")

    def test_plain_string_error_and_top_level_message(self):
        self.assertEqual(bridge._llama_error_text('{"error": "model is loading"}'), "model is loading")
        self.assertEqual(bridge._llama_error_text('{"message": "busy"}'), "busy")

    def test_non_json_body_comes_back_on_one_line(self):
        self.assertEqual(bridge._llama_error_text("Internal\nServer Error"), "Internal Server Error")
        self.assertEqual(bridge._llama_error_text(""), "")
        self.assertEqual(bridge._llama_error_text('{"other": 1}'), '{"other": 1}')


if __name__ == "__main__":
    unittest.main()
