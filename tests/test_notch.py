"""Notch overlay: serve-path, settings toggle and registry-diff helper tests.

The window host (notch_host.py) is exercised separately by a live end-to-end
check; these tests cover everything the bridge itself owns.
"""
import json
import threading
import unittest
import urllib.error
import urllib.request

import bridge
from _source import app_source, frontend_source
from _patching import bridge_patch
import time

_LOOPBACK = ".".join(("127", "0", "0", "1"))


class NotchServeTests(unittest.TestCase):
    """The overlay page is served with host-side injections only — the file
    on disk must stay byte-identical to the approved UI."""

    @classmethod
    def setUpClass(cls):
        bridge.Handler.log_message = lambda self, fmt, *a: None
        cls.httpd = bridge.AccurettaHTTPServer((_LOOPBACK, 0), bridge.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _get(self, path):
        with urllib.request.urlopen(f"http://{_LOOPBACK}:{self.port}{path}", timeout=5) as r:
            return r.status, dict(r.headers), r.read()

    def test_page_injections(self):
        status, headers, body = self._get("/notch")
        text = body.decode("utf-8", "replace")
        self.assertEqual(status, 200)
        self.assertIn('name="accuretta-request-token"', text)
        self.assertIn(bridge._REQUEST_TOKEN, text)
        self.assertIn("background: transparent !important", text)
        self.assertIn('<script src="/notch-wire.js"></script></body>', text)
        self.assertIn('id="island"', text)
        self.assertIn("no-store", headers.get("Cache-Control", ""))

    def test_raw_files_served_unchanged(self):
        status, _, body = self._get("/accuretta-notch.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, (bridge.ROOT / "accuretta-notch.html").read_bytes())
        status, _, body = self._get("/notch-wire.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, (bridge.ROOT / "notch-wire.js").read_bytes())

    def test_wire_script_is_not_the_demo(self):
        _, _, body = self._get("/notch-wire.js")
        text = body.decode("utf-8", "replace")
        # wired mode must be forced before any notch call
        self.assertIn("notch.config.demo = false", text)
        self.assertIn("notch.config.mockLoadMs = 0", text)

    def test_markdown_renderer_is_served(self):
        status, _, body = self._get("/notch-markdown.js")
        self.assertEqual(status, 200)
        self.assertEqual(body, (bridge.ROOT / "notch-markdown.js").read_bytes())

    def test_main_window_sounds_are_the_notch_sounds(self):
        # sound-cues.js carries the notch's Sound engine for the main window;
        # the cue table and the synth must match the notch's own copy.
        status, _, body = self._get("/sound-cues.js")
        self.assertEqual(status, 200)
        cues = body.decode("utf-8").replace("\r\n", "\n")
        notch = (bridge.ROOT / "accuretta-notch.html").read_text(encoding="utf-8").replace("\r\n", "\n")
        for start, end in (("  const N = {", "  let ctx = null"),
                           ("  function room(sec) {", "  function play(name) {")):
            with self.subTest(part=start.strip()):
                theirs = notch[notch.index(start):notch.index(end, notch.index(start))]
                self.assertIn(theirs, cues)
        # the old sample files are gone, and nothing serves them
        self.assertFalse((bridge.ROOT / "assets" / "audio").exists())
        self.assertFalse(any(name.startswith("assets/audio/") for name in bridge.STATIC_WHITELIST))
        app = frontend_source()
        self.assertNotIn("new Audio(", app)
        for cue in ('playCue("gate")', 'playCue("done")', 'playCue("error")'):
            self.assertIn(cue, app)
        # on this PC the notch sounds requests, so the main window stays quiet
        self.assertIn("return !!state.settings?.notch_enabled && !state.clientContext?.remote;", app)

    def test_page_loads_renderer_before_wiring(self):
        _, _, body = self._get("/notch")
        text = body.decode("utf-8", "replace")
        self.assertLess(text.index("/notch-markdown.js"), text.index("/notch-wire.js"))

    def test_host_messages_use_the_webview_channel(self):
        # Host -> page messages arrive on window.chrome.webview in WebView2;
        # a plain window listener never sees them.
        _, _, body = self._get("/notch-wire.js")
        text = body.decode("utf-8", "replace")
        self.assertIn("window.chrome.webview.addEventListener", text)
        self.assertIn('"collapse"', text)
        self.assertIn('"outside-click"', text)

    def test_gate_diff_scrolls_instead_of_stopping_at_eight_lines(self):
        _, _, body = self._get("/notch")
        text = body.decode("utf-8", "replace")
        self.assertNotIn("g.diff.slice(0, 8)", text)
        self.assertIn('<div class="drows">', text)
        self.assertRegex(text, r"\.drows \{[^}]*overflow-y: auto")
        rows, adds, dels = bridge._diff_preview_rows("", "\n".join(f"line {i}" for i in range(30)))
        self.assertEqual((len(rows), adds, dels), (30, 30, 0))

    def test_approval_keys_follow_the_settings(self):
        _, _, page = self._get("/notch")
        _, _, wire = self._get("/notch-wire.js")
        page, wire = page.decode("utf-8", "replace"), wire.decode("utf-8", "replace")
        self.assertIn("setApprovalKeys", page)
        self.assertNotIn("'Allow <kbd>Y</kbd>'", page, "the approve key must not be hard-coded")
        self.assertIn('case "settings:update":', wire)
        self.assertIn("k.approve_pending", wire)
        self.assertIn("k.deny_pending", wire)

    def test_hint_line_only_speaks_when_it_has_news(self):
        # The line under the send button never says "Enter sends" (that is
        # obvious); it only carries loading / ready / error news, then clears.
        widget = (bridge.ROOT / "accuretta-notch.html").read_text(encoding="utf-8")
        self.assertNotIn("Enter sends", widget)
        import re
        values = re.findall(r"setHint\((?:`[^`]*`|'[^']*')", widget)
        for keep in ("Loading", "ready"):
            self.assertTrue(any(keep in v for v in values), f"{keep} missing from {values}")
        self.assertIn("setHint('')", widget)


class NotchReplyContractTests(unittest.TestCase):
    """A turn must never end as silence.

    The failure this guards against looked like nothing at all: the island went
    to its work pill, then snapped back to the minimal idle dot, with no reply
    and no error - indistinguishable from never having asked. Every one of these
    assertions is a case that previously produced exactly that.
    """

    @classmethod
    def setUpClass(cls):
        cls.wire = (bridge.ROOT / "notch-wire.js").read_text(encoding="utf-8")
        cls.html = (bridge.ROOT / "accuretta-notch.html").read_text(encoding="utf-8")

    def test_finish_reports_a_turn_that_produced_nothing(self):
        # finish() must open the card when nothing readable ever arrived.
        self.assertIn("if (!replyRaw.trim())", self.wire)
        self.assertIn("openReply();", self.wire)
        self.assertIn("without a readable reply", self.wire,
                      "an empty turn must say so rather than collapse to idle")

    def test_final_event_is_honoured_instead_of_dropped(self):
        # The authoritative answer used to be used ONLY when nothing had
        # streamed, so a turn whose reply arrived only in `final` lost it.
        self.assertIn('evt.type === "final"', self.wire)
        self.assertIn("visibleFinalText", self.wire)
        self.assertIn("if (text !== replyRaw.trim())", self.wire)
        # The final supersedes the tail still sitting in the flush buffer;
        # leaving it there made finish() append it a second time. Slicing up to
        # chat_end proves the clear happens inside the final branch.
        branch = self.wire[self.wire.index('evt.type === "final"'):]
        branch = branch[:branch.index('evt.type === "chat_end"')]
        self.assertIn('wordBuf = "";', branch,
                      "the final must clear the pending tail or finish() doubles it")

    def test_final_answer_has_its_reasoning_stripped(self):
        # \even.py`iven final.content` → carries the whole assembled message,
        # reasoning block included. The live stripper instance is reset per
        # turn and visibleFinalText strips tags/closers that survive it, so
        # rendering never shows the model's private reasoning.
        self.assertIn("stripThink = makeThinkStripper()", self.wire)
        self.assertIn("<\\/?(?:think|thinking|reasoning)>", self.wire)

    def test_cascade_suggestions_never_reach_the_card(self):
        # The bridge streams <cascade>["…"]</cascade> as ordinary deltas and the
        # main app turns it into suggestion chips; the notch rendered raw JSON.
        self.assertIn("function stripCascade(", self.wire)
        self.assertIn("cascade(?:>|&gt;|\\\\>)", self.wire)
        # Stripping each delta alone leaked the block whenever the tag split
        # across deltas ("<cascade" | ">[...]"); the filter carries state, is
        # reset per turn, and releases held text when the turn ends.
        self.assertIn("cascadeFilter.push(stripThink(text))", self.wire)
        self.assertIn("cascadeFilter = makeCascadeFilter()", self.wire)
        self.assertIn("wordBuf += cascadeFilter.flush()", self.wire)

    def test_final_with_no_readable_text_does_not_abort_the_turn(self):
        # `return` out of the event loop here would skip finish(), leaving
        # `streaming` true and wedging the notch against every later turn.
        branch = self.wire[self.wire.index('evt.type === "final"'):]
        branch = branch[:branch.index('evt.type === "chat_end"')]
        self.assertNotIn("return;", branch,
                         "the final branch must fall through to finish(), not return")
        self.assertIn("if (text) {", branch)

    def test_thinking_state_is_owned_locally_not_only_by_the_event_stream(self):
        # The pill must say "thinking…" the moment Enter is pressed, even if
        # the SSE stream is down.
        self.assertIn('setActivity("thinking")', self.wire)
        self.assertIn('line.classList.toggle("shimmer", !!line.textContent.trim())', self.wire)

    def test_work_pill_is_one_live_event_line(self):
        # One line, replaced by each live event ("thinking…" -> "Reading
        # main.py…"): no second status word, no right-side echo. The tool's
        # glyph rides on the right where "on it" used to be.
        self.assertNotIn('id="workState"', self.html)
        self.assertNotIn('id="workRight"', self.html)
        self.assertNotIn("WORK_RIGHT_WORDS", self.wire)
        self.assertIn('const ACTIVITY_WORDS = { thinking: ACTIVITY_THINKING', self.wire)
        self.assertIn("setWorkIcon(phase)", self.wire)
        self.assertIn('.v-work .wr', self.wire)

    def test_shimmer_sweep_is_injected(self):
        # Both the minimized pill's live event line and the open card's live
        # status line shimmer. The card line has no dot and the sweep stays
        # inside 0..100% so the background always covers the text (travel
        # beyond that left the tail transparent — the "fading in and out").
        self.assertIn("#workSub.shimmer", self.wire)
        self.assertIn("#liveStatus.shimmer", self.wire)
        self.assertNotIn("#liveStatus::before", self.wire)
        self.assertIn("@keyframes notch-shimmer", self.wire)
        self.assertIn("background-position: 100% 0", self.wire)
        self.assertIn("background-position: 0% 0", self.wire)
        self.assertIn("prefers-reduced-motion", self.wire,
                      "the sweep must respect reduced-motion")

    def test_narration_pieces_become_paragraphs(self):
        # The agent narrates between tool calls; glued end to end the pieces
        # read as one run-on ("…if available.My bad — …"). A tool call marks
        # a break and the next piece starts a new paragraph.
        tool = self.wire[self.wire.index("function onToolStart(evt)"):]
        tool = tool[:tool.index("\n}")]
        self.assertIn("segBreak = true", tool)
        ingest = self.wire[self.wire.index("function ingestWords(text)"):]
        ingest = ingest[:ingest.index("\n}")]
        self.assertIn('clean = "\\n\\n" + clean', ingest)

    def test_rect_is_reported_until_the_reveal_slide_settles(self):
        # The reveal slides the island down from above the screen edge (a CSS
        # transition the widget's `settled` flag never sees). Stopping the
        # rect reports on `settled` alone left the host's window region cut to
        # the first, mostly-off-screen frame: a sliver of pill.
        fn = self.wire[self.wire.index("function reportRect()"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn("cssMoving", fn)
        self.assertIn('island.addEventListener("transitionend"', self.wire)
        self.assertIn('island.addEventListener("transitionrun"', self.wire)

    def test_lifecycle_is_still_wired_mode(self):
        self.assertIn("notch.config.demo = false", self.wire)
        self.assertIn("notch.config.mockLoadMs = 0", self.wire)


class NotchSettingsTests(unittest.TestCase):
    def test_default_off(self):
        self.assertIn("notch_enabled", bridge.DEFAULT_SETTINGS)
        self.assertFalse(bridge.DEFAULT_SETTINGS["notch_enabled"])

    def test_update_settings_roundtrip(self):
        calls = []
        with bridge_patch("notch_sync", lambda: calls.append(1)):
            try:
                cur = bridge.update_settings({"notch_enabled": True})
                self.assertTrue(cur["notch_enabled"])
            finally:
                bridge.update_settings({"notch_enabled": False})


class NotchRegistryDiffTests(unittest.TestCase):
    """The diff helper parses the pending command but only ever runs
    read-only `reg query`; unparseable input degrades to no rows."""

    def test_reg_add_new_value(self):
        r = bridge._notch_registry_diff(
            'reg add "HKCU\\Software\\AccurettaNotchUnitTest" /v Flag /t REG_DWORD /d 1 /f')
        self.assertEqual(r["path"], "HKCU\\Software\\AccurettaNotchUnitTest")
        self.assertEqual(r["diff"], [["+", "Flag  REG_DWORD  1"]])

    def test_reg_add_quoted_data(self):
        r = bridge._notch_registry_diff(
            'reg add "HKCU\\Software\\AccurettaNotchUnitTest" /v Name /t REG_SZ /d "hello world" /f')
        self.assertEqual(r["diff"], [["+", "Name  REG_SZ  hello world"]])

    def test_reg_delete_key(self):
        r = bridge._notch_registry_diff('reg delete "HKCU\\Software\\AccurettaNotchUnitTest" /f')
        self.assertEqual(r["diff"], [["-", "(entire key)"]])

    def test_set_itemproperty(self):
        r = bridge._notch_registry_diff(
            'Set-ItemProperty -Path "HKCU:\\Software\\AccurettaNotchUnitTest" -Name Flag -Value 3')
        self.assertEqual(r["path"], "HKCU\\Software\\AccurettaNotchUnitTest")
        self.assertEqual(r["diff"], [["+", "Flag  3"]])

    def test_unparseable_degrades(self):
        self.assertEqual(bridge._notch_registry_diff("format C:"), {"path": "", "diff": []})
        self.assertEqual(bridge._notch_registry_diff(""), {"path": "", "diff": []})

    def test_hostile_input_never_reaches_a_query(self):
        r = bridge._notch_registry_diff(
            'reg add "HKCU\\Software\\X & whoami" /v A /t REG_SZ /d b /f')
        self.assertEqual(r, {"path": "", "diff": []})
        r = bridge._notch_registry_diff(
            'reg add "HKCU\\Software\\X" /v A /t REG_SZ /d b /f & del /q C:\\')
        # trailing junk after the command is ignored by the parser
        self.assertEqual(r["path"], "HKCU\\Software\\X")


class NotchChatOriginTests(unittest.TestCase):
    """A notch-born session must be recognized and recency-ranked in the app.

    The persistence itself was never broken — every turn lands in
    chats.json. What failed was discoverability: `order` tracked creation
    order only, so a notch session (created days ago, updated minutes ago)
    sat mid-list while the app auto-selected a stale first row; and the app
    had no idea which sessions were notch-born. Together that read exactly
    like "the whole conversation is nowhere in the app".
    """

    @classmethod
    def setUpClass(cls):
        bridge.Handler.log_message = lambda self, fmt, *a: None
        cls.httpd = bridge.AccurettaHTTPServer((_LOOPBACK, 0), bridge.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post_chat(self, body: dict):
        # /api/chat answers as an SSE stream: collect the events, return the
        # last complete one (chat_end/final carry the useful fields).
        req = urllib.request.Request(
            f"http://{_LOOPBACK}:{self.port}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Accuretta-Token": bridge._REQUEST_TOKEN},
            method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = r.read().decode("utf-8", "replace")
        last = None
        for line in payload.split("\n"):
            if line.startswith("data: "):
                try:
                    last = json.loads(line[6:])
                except Exception:
                    pass
        return 200, (last or {})

    def test_notch_chat_is_tagged_and_rails_recency_first(self):
        from unittest.mock import patch
        import tempfile
        from pathlib import Path as _Path
        sandbox = _Path(tempfile.mkdtemp())
        chats_file = sandbox / "chats.json"
        chats_file.write_text("{}", encoding="utf-8")
        journal_dir = sandbox / "turn_journal"
        journal_dir.mkdir()

        canned = {"role": "assistant", "content": "canned reply"}
        with bridge_patch("CHATS_FILE", chats_file), \
             bridge_patch("TURN_JOURNAL_DIR", journal_dir), \
             bridge_patch("run_chat_turn", return_value=dict(canned)), \
             bridge_patch("llama_ready", return_value=True), \
             bridge_patch("llama_ping", return_value=True), \
             bridge_patch("_maybe_roll_summary", return_value=True):
            base = int(time.time())
            # main-app turn first (older updated), then a notch turn (newer)
            _, r1 = self._post_chat({"message": "app-born question", "mode": "agent",
                                     "chat_id": "orig-apptest"})
            self.assertEqual(r1.get("chat_id"), "orig-apptest")
            _, r2 = self._post_chat({"message": "notch-born question", "mode": "agent",
                                     "chat_id": "island-apptest", "surface": "notch"})
            self.assertEqual(r2.get("chat_id"), "island-apptest")
            idx = bridge.get_chat_index()
            self.assertTrue(idx["order"].index("island-apptest")
                            < idx["order"].index("orig-apptest"),
                            f"recency order broken: {idx['order'][:4]}")
            self.assertEqual(idx["chats"]["island-apptest"].get("origin"), "island",
                             "a notch-born session must carry the island origin tag")
            # untagged sessions keep no invented origin
            self.assertEqual(idx["chats"]["orig-apptest"].get("origin", ""), "")
        # the sandbox held everything: the live rail must be untouched
        live = bridge.get_chat_index()
        self.assertNotIn("island-apptest", live["chats"])
        self.assertNotIn("orig-apptest", live["chats"])

    def test_recency_sort_is_stable_and_read_time(self):
        # get_chat_index sorts by updated, not creation: craft an index whose
        # order is stale (oldest first) and prove the sort repairs it.
        from unittest.mock import patch
        now = int(time.time())
        doc = {"chats": {
            "old": {"id": "old", "title": "old", "updated": now - 1000, "messages": []},
            "fresh": {"id": "fresh", "title": "fresh", "updated": now, "messages": []},
        }, "order": ["old", "fresh"]}
        with bridge_patch("get_chats", return_value=doc):
            idx = bridge.get_chat_index()
        self.assertEqual(idx["order"], ["fresh", "old"])


class NotchSessionDiscoverabilityTests(unittest.TestCase):
    """The main app must treat notch-born sessions as first-class: keep the
    rail visible for an island-born active chat, and re-sync the rail on any
    focus/visibility return (a missed SSE chat_start once hid fresh sessions
    until a reload). Source-level contract, matching the repo's frontend-test
    style."""

    @classmethod
    def setUpClass(cls):
        cls.app = frontend_source()

    def test_island_born_chat_keeps_the_sidebar_visible(self):
        seg = self.app[self.app.index("else if (isCompactViewport()) {"):]
        seg = seg[:seg.index('app.classList.add("sidebar-collapsed")')]
        self.assertIn('chat.origin === "island"', seg)
        self.assertIn('app.classList.remove("sidebar-collapsed")', seg)

    def test_rail_resyncs_on_focus_and_visibility(self):
        self.assertIn('"visibilitychange"', self.app)
        focus_seg = self.app[self.app.index('window.addEventListener("focus"'):]
        focus_seg = focus_seg[:focus_seg.index("});", focus_seg.index("loadChats()"))]
        self.assertIn("loadChats()", focus_seg)

    def test_notch_turn_declares_its_surface(self):
        wire = (bridge.ROOT / "notch-wire.js").read_text(encoding="utf-8")
        self.assertIn('surface: "notch"', wire)

    def test_open_in_main_app_targets_the_desktop_window(self):
        # The overlay host raises the pywebview window (restore if minimized)
        # and only falls back to the browser when it isn't running at all; the
        # bridge broadcast is what makes that window select the session.
        host = (bridge.ROOT / "notch_host.py").read_text(encoding="utf-8")
        self.assertIn("_raise_main_window", host)
        # FindWindowW alone hits Windows 11's TabProxyWindow ghost; the real
        # window is found by enumeration that skips shell proxies.
        self.assertIn("EnumWindows", host)
        self.assertIn("Windows.Internal.Shell", host)
        self.assertIn("os.startfile(url)", host)     # browser fallback kept
        self.assertIn("SW_RESTORE", host)
        bridge_src = app_source()
        self.assertIn('"/api/ui/open-chat"', bridge_src)
        self.assertIn('"ui:open-chat"', bridge_src)
        self.assertIn('"ui:open-chat"', self.app)
        self.assertIn("openRequestedChat", self.app)

    def _fn(self, sig):
        seg = self.app[self.app.index(sig):]
        return seg[:seg.index("\n  }\n")]

    def test_live_turn_leaves_the_whole_welcome_layout(self):
        # Opening a live session from the notch kept the new-session layout
        # (centred composer, recents, dot field) under the mirrored turn:
        # only the heading was removed.
        for sig in ("function beginForeignTurn(", "async function selectChat("):
            seg = self._fn(sig)
            w = seg.index('.welcome-screen")')
            self.assertIn("leaveWelcome()", seg[w:w + 200], sig)

    def test_stale_loaded_chat_is_refetched(self):
        seg = self._fn("async function selectChat(")
        self.assertIn("chat._loadedUpdated !== chat.updated", seg)
        self.assertIn("chat._loadedUpdated = chat.updated", seg)
        # The attached live turn keeps its cached copy (no duplicate prompt).
        self.assertIn("const stale = !attached", seg)
        self.assertIn("_loadedUpdated = loaded._loadedUpdated", self._fn("async function loadChats("))


class NotchChatBindingTests(unittest.TestCase):
    """The follow-up dead end: a conversation runs in the main app, plays out
    in the notch card, and the owner's follow-up from the notch used to land
    in an old chat (the stale pinned one) or a brand-new one. What the card
    shows must be where Enter sends. Source-level contract, matching the
    repo's frontend-test style."""

    @classmethod
    def setUpClass(cls):
        cls.wire = (bridge.ROOT / "notch-wire.js").read_text(encoding="utf-8")

    def _chat_start_handler(self):
        seg = self.wire[self.wire.index('case "chat_start": {'):]
        return seg[:seg.index('case "tool_start":')]

    def _fn(self, name):
        fn = self.wire[self.wire.index(f"function {name}("):]
        return fn[:fn.index("\n}")]

    def test_mirrored_turn_rebinds_the_notch_to_its_chat(self):
        seg = self._chat_start_handler()
        self.assertIn("startMirror(cid)", seg)
        self.assertIn("bindNotchChat(cid)", self._fn("startMirror"))
        # Our own streaming turn's SSE echo must not rebind anything.
        self.assertLess(seg.index("if (streaming) break;"), seg.index("startMirror(cid)"))

    def test_turn_already_running_is_adopted(self):
        # A notch that connects mid-turn never sees chat_start. The pill still
        # showed the tool line, but the expanded card stayed empty.
        tool = self.wire[self.wire.index('case "tool_start":'):]
        tool = tool[:tool.index("break;")]
        self.assertLess(tool.index("onToolStart(evt)"), tool.index("adoptLiveTurn(cid, evt)"))
        delta = self.wire[self.wire.index('case "delta":'):]
        delta = delta[:delta.index('case "ctx_fill":')]
        self.assertLess(delta.index("if (streaming) break;"), delta.index("adoptLiveTurn(cid, evt)"))
        # Only a turn the index confirms: a stray tool event outside any turn
        # must not open a mirror that no chat_end will ever close.
        adopt = self.wire[self.wire.index("async function adoptLiveTurn("):]
        adopt = adopt[:adopt.index("\n}\n")]
        self.assertIn('chat.task_state !== "working"', adopt)
        self.assertLess(adopt.index('task_state !== "working"'), adopt.index("startMirror(cid)"))
        # Boot stays idle even when another session is working.
        self.assertNotIn("adopt", self.wire[self.wire.index("sse.onopen"):][:80])

    def test_binding_is_persisted(self):
        fn = self.wire[self.wire.index("function bindNotchChat(id)"):]
        fn = fn[:fn.index("\n}")]
        self.assertIn('localStorage.setItem("accuretta.notch.chat", id)', fn)

    def test_sends_go_to_the_bound_chat(self):
        self.assertIn("chat_id: notchChatId || undefined", self.wire)

    def test_stale_binding_is_checked_before_events_connect(self):
        hydrate = self.wire[self.wire.rindex("// ---- hydrate"):]
        self.assertLess(hydrate.index("await restoreNotchChat()"),
                        hydrate.index("connectEvents()"))
        fn = self.wire[self.wire.index("async function restoreNotchChat()"):]
        fn = fn[:fn.index("\n}")]
        # A deleted chat's id is dropped (the bridge would re-create it empty),
        # but only on a definite answer from the bridge.
        self.assertIn('localStorage.removeItem("accuretta.notch.chat")', fn)
        self.assertIn("!idx.chats[id]", fn)


if __name__ == "__main__":
    unittest.main()
