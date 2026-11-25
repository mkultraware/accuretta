"""Settings window: the Storage page's report and clear (safe kinds only,
never outside the data folder), the profile name, and the source contracts
that put the old pages and windows inside settings."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bridge
from _source import app_source, frontend_source
from _patching import bridge_patch

APP = frontend_source()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.data.mkdir()
        self.patches = [bridge_patch("DATA", self.data),
                        bridge_patch("get_workspace", return_value={"folders": []})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, rel, size=10):
        path = self.data / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_report_sizes_each_kind(self):
        self.write("chats.json", 100)
        self.write("versions/c1/v1.html", 30)
        self.write("versions/c1/v2.html", 20)
        self.write("settings.json.pre-sage-20261022.bak", 7)
        kinds = {k["key"]: k for k in bridge.storage_report()["kinds"]}
        self.assertEqual((kinds["chats"]["size"], kinds["chats"]["clearable"]), (100, False))
        self.assertEqual((kinds["versions"]["files"], kinds["versions"]["size"]), (2, 50))
        self.assertEqual(kinds["backups"]["files"], 1)

    def test_clear_empties_folders_and_removes_files(self):
        self.write("versions/c1/v1.html")
        self.write("errors.jsonl")
        self.write("compaction_debug.log")
        self.assertEqual(bridge.storage_clear("versions")["removed"], 1)
        self.assertTrue((self.data / "versions").is_dir())
        self.assertEqual(list((self.data / "versions").iterdir()), [])
        out = bridge.storage_clear("logs")
        self.assertEqual((out["removed"], out["freed"]), (2, 20))
        self.assertFalse((self.data / "errors.jsonl").exists())

    def test_only_safe_kinds_can_be_cleared(self):
        chats = self.write("chats.json")
        for key in ("chats", "audit", "models", "security", "cache", "journals", "uploads", "nope", ""):
            with self.subTest(key=key), self.assertRaises(ValueError):
                bridge.storage_clear(key)
        self.assertTrue(chats.exists())

    def test_a_link_out_of_the_data_folder_is_not_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        keep = outside / "keep.txt"
        keep.write_text("mine")
        (self.data / "reports").mkdir()
        try:
            os.symlink(outside, self.data / "reports" / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks need extra rights here")
        bridge.storage_clear("reports")
        self.assertTrue(keep.exists())
        self.assertFalse((self.data / "reports" / "link").exists())


class ProfileNameTests(unittest.TestCase):
    def test_name_is_short_plain_text(self):
        with bridge_patch("get_settings", return_value=dict(bridge.DEFAULT_SETTINGS)), \
             bridge_patch("save_json"):
            self.assertEqual(bridge.update_settings({"profile_name": "  Ernest  "})["profile_name"], "Ernest")
            for bad in ("x" * 41, "a\nb", 5):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    bridge.update_settings({"profile_name": bad})


class SettingsWindowContractTests(unittest.TestCase):
    def test_old_pages_open_inside_settings(self):
        for opener, page in (("function openUsageStats()", 'openSettingsAt("usage", "stats")'),
                             ("function ghActivityOpen()", 'openSettingsAt("github")'),
                             ("function openCmdHistory()", 'openSettingsAt("usage", "commands")'),
                             ("function wbMcpOpen(select)", 'openSettingsAt("tools")')):
            with self.subTest(opener=opener):
                body = APP[APP.index(opener):]
                self.assertIn(page, body[:body.index("\n  }\n")])

    def test_security_overview_keeps_its_full_page(self):
        opener = APP[APP.index("async function openSecurityOverview()"):]
        opener = opener[:opener.index("\n  }\n")]
        self.assertIn('page.classList.add("open");', opener)
        self.assertNotIn("openSettingsAt", opener)
        # the shell is not moved into the settings window
        self.assertNotIn('$("#security-page .security-shell")', APP)
        self.assertIn('security.querySelector("#settings-open-security").addEventListener("click", openSecurityOverview);', APP)

    def test_every_page_has_a_home(self):
        for page in ("general", "profile", "appearance", "usage", "memory", "tools", "storage", "model",
                     "performance", "generation", "developer", "approvals", "security", "red-team",
                     "skills", "github", "remote"):
            with self.subTest(page=page):
                self.assertIn(f'{{ id: "{page}", label:', APP)
        # MCP's "Manage" row gives way to the servers on the same page
        self.assertIn('const SETTINGS_ABSORBED = ["mcp-servers"];', APP)
        # unsaved MCP edits still ask before the window closes
        close = APP[APP.index("function closeSettings(force = false)"):]
        self.assertIn("if (!force && mcpUi.dirty && wbMcpShown())", close[:400])

    def test_storage_clear_is_for_this_machine_only(self):
        src = app_source()
        route = src[src.index('if p == "/api/storage/clear":'):]
        self.assertIn("if not self._peer_is_loopback():", route[:200])


class SidebarContractTests(unittest.TestCase):
    def test_sidebar_is_built_and_wired(self):
        init = APP[APP.index("function initWorkbench()"):]
        self.assertIn("wbInitSidebar();", init[:init.index("\n  }\n")])
        side = APP[APP.index("function wbInitSidebar()"):]
        side = side[:side.index("\n  }\n")]
        # the theme control steps through every theme, both ways, not a light/dark pair
        self.assertIn('btn.addEventListener("click", () => stepTheme(Number(btn.dataset.step))));', side)
        self.assertIn('data-step="-1"', side)
        self.assertNotIn("ph-sun", side)
        self.assertIn('openSettingsAt("profile")', side)
        # no automations here: the bolt opens the Security overview page
        self.assertIn('$("#sb-security").addEventListener("click", openSecurityOverview);', side)
        self.assertNotIn("Automations", side)
        self.assertIn('openSettingsAt("skills")', side)
        self.assertIn('openSettingsAt("usage", "stats")', side)

    def test_theme_steps_wrap_both_ways_from_what_is_shown(self):
        self.assertIn("return THEME_CYCLE[((idx + step) % n + n) % n];", APP)
        step = APP[APP.index("async function stepTheme(step = 1)"):]
        self.assertIn('nextTheme(document.documentElement.dataset.theme || state.settings.theme || "light", step)',
                      step[:step.index("\n  }\n")])
        self.assertIn('$("#btn-theme").addEventListener("click", () => stepTheme(1));', APP)

    def test_every_project_folder_shows_and_files_stay_out_of_the_session_list(self):
        render = APP[APP.index("function renderChatList()"):]
        render = render[:render.index("\n  }\n")]
        self.assertIn("for (const folder of state.workspace?.folders || [])", render)
        # no file tree among the sessions: a project only links to the Files view
        self.assertNotIn("sbFilesRoot(", render)
        self.assertNotIn("renderTreeNode(", render)
        self.assertIn('files.addEventListener("click", () => sbSetView("files", g.path));', render)
        self.assertIn("sbRenderFilesView();", render)

    def test_sessions_and_files_are_two_views_of_the_list(self):
        side = APP[APP.index("function wbInitSidebar()"):]
        side = side[:side.index("\n  }\n")]
        self.assertIn('data-view="sessions"', side)
        self.assertIn('data-view="files"', side)
        self.assertIn('filesView.id = "sb-files-view";', side)
        css = (bridge.ROOT / "web" / "css" / "workbench.css").read_text(encoding="utf-8")
        self.assertIn('#sidebar.sb-v2[data-view="files"] #chatlist,', css)
        self.assertIn('#sidebar.sb-v2:not([data-view="files"]) #sb-files-view { display: none; }', css)
        # the Files view lists every project folder, with or without sessions
        roots = APP[APP.index("function sbProjectRoots()"):]
        self.assertIn("for (const folder of state.workspace?.folders || []) add(folder);",
                      roots[:roots.index("\n  }\n")])

    def test_projects_and_chats_fold_apart(self):
        render = APP[APP.index("function renderChatList()"):]
        render = render[:render.index("\n  }\n")]
        self.assertIn('if ($("#sidebar")?.classList.contains("sb-v2")) sections = sbListSections(wrap);', render)
        self.assertIn("(sections ? sections.projects : wrap).appendChild(box);", render)
        self.assertIn('const SB_SECTIONS_KEY = "accuretta:sb:collapsed-sections";', APP)
        # opening a session elsewhere unfolds its section
        reveal = APP[APP.index("function wbRevealChatGroup(id)"):]
        self.assertIn('sbRevealSection(key === "chats" ? "chats" : "projects");', reveal[:400])
        # the list drawn before the new layout is redrawn in sections
        side = APP[APP.index("function wbInitSidebar()"):]
        self.assertIn("if (state.chats) renderChatList();", side[:side.index("\n  }\n")])


class ActivityHeatmapTests(unittest.TestCase):
    def test_the_year_fits_and_a_narrow_panel_opens_on_today(self):
        css = (bridge.ROOT / "web" / "css" / "ui-refresh.css").read_text(encoding="utf-8")
        self.assertIn("min-width: calc(var(--weeks, 53) * 11px + 34px);", css)
        self.assertNotIn("min-width: 700px", css)
        wire = APP[APP.index("function wireUsageHeatmap(data)"):]
        self.assertIn("if (scroller) scroller.scrollLeft = scroller.scrollWidth;", wire[:wire.index("\n  }\n")])

    def test_rows_and_columns_share_one_gap(self):
        # the Mon/Wed/Fri labels must not stretch the rows apart
        css = (bridge.ROOT / "web" / "css" / "ui-refresh.css").read_text(encoding="utf-8")
        weekdays = css[css.index(".usage-weekdays {"):]
        self.assertIn("contain: size;", weekdays[:weekdays.index("}")])
        self.assertIn("grid-template-rows: repeat(7, auto); gap: 3px;", css)


class SettingsFadeTests(unittest.TestCase):
    def test_scrolling_sides_fade_instead_of_cutting_off(self):
        nav = APP[APP.index("  function initSettingsNav() {"):]
        nav = nav[:nav.index("\n  }\n")]
        # after the page list exists, so both sides get it
        self.assertTrue(nav.rstrip().endswith("initSettingsFades();"))
        css = (bridge.ROOT / "web" / "css" / "workbench.css").read_text(encoding="utf-8")
        self.assertIn("#settings-drawer .settings-fade.fade-bottom { --fade-bottom: 44px; }", css)


class WebSearchTreeTests(unittest.TestCase):
    def test_a_search_only_card_reads_as_a_tree(self):
        sync = APP[APP.index("  function tcardSync(group) {"):]
        sync = sync[:sync.index("\n  }\n")]
        self.assertIn('const searchOnly = rows.length > 0 && rows.every(r => r.dataset.name === "web_search");', sync)
        self.assertIn('searchOnly ? "Searching the web"', sync)
        self.assertIn('`Ran ${calls} search${calls === 1 ? "" : "es"}`', sync)
        # Sources opens the row's result chips; the test hook is gone
        self.assertIn('if (sources) { tcardToggleRow(sources.closest(".tcard-row")); return; }', APP)
        self.assertNotIn("__tcardTest", APP)

    def test_sites_are_named(self):
        for site in ('name: "X", key: "x", icon: "ph-x-logo", unit: "post"',
                     'name: "Reddit", key: "reddit", icon: "ph-reddit-logo", unit: "thread"'):
            self.assertIn(site, APP)


class GoalRowTests(unittest.TestCase):
    """While /goal runs, its task shows as a row in the composer box."""

    def setUp(self):
        self.chats = {"chats": {"c1": {"id": "c1", "messages": []}}, "order": ["c1"]}
        self.events = []
        self.patches = [bridge_patch("get_chats", side_effect=lambda: self.chats),
                        bridge_patch("save_json"),
                        bridge_patch("broadcast_event", side_effect=self.events.append)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        bridge._set_goal_mode("c1", False)
        for p in self.patches:
            p.stop()

    def test_goal_text_is_kept_while_on_and_cleared_when_off(self):
        chat = self.chats["chats"]["c1"]
        bridge._apply_goal_latch("c1", True, "build the parser")
        self.assertEqual((chat["goal_mode"], chat["goal_text"]), (True, "build the parser"))
        self.assertEqual(self.events[-1]["goal"], "build the parser")
        # a bare "/goal" (latch only) keeps the task it already has
        bridge._apply_goal_latch("c1", True)
        self.assertEqual(self.events[-1]["goal"], "build the parser")
        bridge._apply_goal_latch("c1", False)
        self.assertNotIn("goal_text", chat)
        self.assertEqual((self.events[-1]["active"], self.events[-1]["goal"]), (False, ""))

    def test_the_goal_reads_as_one_short_line(self):
        self.assertEqual(bridge._goal_summary("  build\n the   parser "), "build the parser")
        self.assertEqual(len(bridge._goal_summary("x" * 900)), 500)

    def test_the_row_sits_in_the_box_and_ends_the_goal(self):
        html = (bridge.ROOT / "index.html").read_text(encoding="utf-8")
        inbox = html[html.index('<div class="composer-inbox">'):]
        self.assertIn('id="composer-goal"', inbox[:inbox.index('id="revealer-deck"')])
        row = APP[APP.index("  function renderGoalRow() {"):]
        self.assertIn('row.querySelector(".composer-goal-off").addEventListener("click", turnGoalOff);',
                      row[:row.index("\n  }\n")])
        # the Access chip and the row end it the same way
        self.assertIn("if (permsBtn.dataset.goal) {\n          turnGoalOff();", APP.replace("\r\n", "\n"))


class ComposerInboxTests(unittest.TestCase):
    def test_notices_and_the_queue_sit_inside_the_box(self):
        html = (bridge.ROOT / "index.html").read_text(encoding="utf-8")
        box = html[html.index('<div class="composer">'):]
        box = box[:box.index('<div class="composer-tools">')]
        self.assertIn('<div class="composer-inbox">', box)
        self.assertIn('id="revealer-deck"', box)
        self.assertIn('id="message-queue"', box)
        self.assertEqual(html.count('id="revealer-deck"'), 1)

    def test_the_edit_banner_joins_them(self):
        banner = APP[APP.index("function renderEditBanner()"):]
        banner = banner[:banner.index("\n  }\n")]
        self.assertIn("if (deck) deck.after(banner);", banner)


if __name__ == "__main__":
    unittest.main()
