"""
Accuretta Notch host for macOS — the island (served at /notch) in a
borderless, transparent, always-on-top panel at the top of the screen.

The Windows host (notch_host.py) uses a raw Win32 window + WebView2; this is
its macOS counterpart, built on AppKit + WebKit through PyObjC (pywebview
already depends on it on macOS, so nothing new is installed). Same contract:

  - one fixed canvas (720 x 560 pt), top edge of the screen that has the
    notch (or the main screen), centred; on a MacBook the idle pill sits over
    the hardware notch and reads as an extension of it
  - above the menu bar, on every Space, beside full-screen apps; no Dock icon
  - never steals focus: a non-activating panel. A click on the island makes
    it key (keyboard goes to its field) without activating Accuretta, the way
    Spotlight works
  - click-through everywhere except the island: macOS has no window regions,
    so the panel ignores mouse events unless the cursor is over the island
    rect notch-wire.js reports
  - Control+Option+N toggles it (Carbon RegisterEventHotKey: a true global
    hotkey, no Accessibility permission). Resting the pointer on the top edge
    above it brings the pill down
  - dies with the bridge (health poll), single instance (lock file)

notch-wire.js talks to its host over WebView2's channel
(window.chrome.webview.postMessage / addEventListener("message")). A user
script installed at document start provides that same surface over WebKit's
message handlers, so the page and its wiring run unchanged on both systems.

The show/hide/hover/auto-hide behaviour lives in IslandLogic, which has no
AppKit dependency (it is unit-tested on any platform); MacNotch is the thin
AppKit/WebKit layer under it.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))
LOOPBACK = ".".join(("127", "0", "0", "1"))
BASE = f"http://{LOOPBACK}:{PORT}"
PAGE_URL = f"{BASE}/notch"
LOG_FILE = ROOT / "data" / "notch_host.log"
LOCK_FILE = ROOT / "data" / "notch_host_mac.lock"

WIDTH_PT = 720              # 640 pt expanded island + room for its shadow
HEIGHT_PT = 560
POLL_S = 0.04               # cursor / click polling
HEALTH_MAX_MISS = 3
NAV_RETRY_S = 5.0           # the bridge may spend minutes loading a model

# Hover wake: the top edge of the screen, centred, is where the notch lives.
WAKE_HALF_W_PT = 150
WAKE_EDGE_PT = 2
WAKE_DWELL_S = 0.12
HOVER_GRACE_S = 2.5

# Control+Option+N. Carbon key code and modifier bits (Events.h).
KVK_ANSI_N = 45
CONTROL_KEY = 0x1000
OPTION_KEY = 0x0800

SHIM_JS = r"""
(function () {
  // WebView2's host channel, provided over WebKit's message handlers so
  // notch-wire.js runs unchanged on macOS.
  var listeners = [];
  window.chrome = window.chrome || {};
  window.chrome.webview = {
    postMessage: function (m) {
      try { window.webkit.messageHandlers.notch.postMessage(JSON.stringify(m)); } catch (e) {}
    },
    addEventListener: function (type, fn) { if (type === "message") listeners.push(fn); },
    removeEventListener: function (type, fn) {
      listeners = listeners.filter(function (f) { return f !== fn; });
    }
  };
  window.__notchHost = function (msg) {
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i]({ data: msg }); } catch (e) {}
    }
  };
})();
"""

_LOG_FH = None


def log(msg: str) -> None:
    global _LOG_FH
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _LOG_FH is None or _LOG_FH.closed:
            _LOG_FH = LOG_FILE.open("a", encoding="utf-8", buffering=1)
        _LOG_FH.write(f"{time.strftime('%H:%M:%S')} mac: {msg}\n")
    except Exception:
        _LOG_FH = None


# ---------------------------------------------------------------------------
# Behaviour (no AppKit): when the island shows, hides, folds and lets clicks
# through. `ui` supplies the platform: show(), hide(), post(msg),
# set_interactive(bool), raise_main() -> bool, open_url(url), and log(str).
# ---------------------------------------------------------------------------
class IslandLogic:
    SHOW_GRACE_S = 6.0            # how long a notification keeps it up
    FIRST_SHOW_S = 6.0            # one confirmation when the overlay starts
    COLLAPSE_HIDE_S = 0.62        # the page's spring settles before it hides
    LEAVE_SLIDE_S = 0.34          # slide up into the bezel, then hide
    # Cards the owner dismisses deliberately (or the page folds on its own
    # countdown): never tucked away by the idle timer.
    HELD_STATES = ("alert", "gate", "prompt", "work", "home", "notice", "drop")
    CARD_STATES = ("gate", "prompt", "home", "notice", "drop")

    def __init__(self, ui):
        self.ui = ui
        self.visible = False
        self.sticky = False
        self.hover_reveal = False
        self.gate_visible_until = 0.0
        self.leaving = False
        self.leave_started = 0.0
        self.hide_at = 0.0
        self.hide_reason = ""
        self.island = None            # (x, y, w, h) in page points
        self.island_state = "idle"
        self.pointer_inside = False
        self.revealed_once = False
        self.wake_since = None        # pointer resting on the wake strip since
        self.lmb_was_down = False

    # ---- visibility --------------------------------------------------------
    def show(self, reason: str = "", now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.visible:
            self.gate_visible_until = max(self.gate_visible_until, now + self.SHOW_GRACE_S)
            if reason != "hover":
                self.hover_reveal = False
            return
        self.hover_reveal = reason == "hover"
        self.leaving = False
        self.ui.show()
        self.visible = True
        self.gate_visible_until = now + (HOVER_GRACE_S if self.hover_reveal else self.SHOW_GRACE_S)
        self.ui.post({"type": "enter"})
        self.ui.post({"type": "refresh-rect"})
        self.ui.log(f"shown ({reason})")

    def hide(self, reason: str = "", now: float | None = None) -> None:
        if not self.visible:
            return
        self.leaving = True
        self.leave_started = time.monotonic() if now is None else now
        self.ui.post({"type": "leave"})
        self.visible = False
        self.gate_visible_until = 0.0
        self.ui.log(f"leaving ({reason})")

    def finish_hide(self) -> None:
        if not self.leaving:
            return
        self.leaving = False
        self.ui.hide()
        self.island = None
        self.ui.set_interactive(False)
        self.ui.log("hidden")

    def toggle(self, reason: str = "hotkey", now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if self.visible:
            # Always a full toggle: shrink to the pill (the page's spring
            # plays), then retreat into the bezel. A pending approval stays
            # pending on the bridge; it just isn't pinned open.
            self.hide_at = now + self.COLLAPSE_HIDE_S
            self.hide_reason = reason
            self.ui.post({"type": "collapse"})
        else:
            # The page needs "enter" (via show) or it keeps `is-leaving` and
            # stays parked above the screen edge, invisible.
            self.sticky = True
            self.show(reason, now)

    # ---- page messages -----------------------------------------------------
    def on_page_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "island-rect":
            state = str(msg.get("state") or "idle")
            first = self.island is None
            self.island = (float(msg.get("x", 0)), float(msg.get("y", 0)),
                           float(msg.get("w", 0)), float(msg.get("h", 0)))
            self.island_state = state
            if first and not self.revealed_once:
                # The page is alive: show once so a freshly enabled overlay is
                # visibly working; the idle timer tucks it away again.
                self.revealed_once = True
                self.show("first run")
        elif kind == "notify":
            self.show(str(msg.get("kind") or "notify"))
        elif kind == "open-url":
            # "Open in main app". Only the bridge's own origin: the page must
            # never become a way to open arbitrary URLs.
            url = str(msg.get("url") or "")
            if url.startswith(BASE + "/"):
                if not self.ui.raise_main():
                    self.ui.open_url(url)
            else:
                self.ui.log(f"open-url refused (not the bridge origin): {url[:120]}")
        elif kind == "diag":
            self.ui.log("diag " + json.dumps({k: v for k, v in msg.items() if k != "type"}))

    # ---- the poll ----------------------------------------------------------
    def inside(self, cursor) -> bool:
        if not (self.visible and self.island and cursor):
            return False
        x, y, w, h = self.island
        return x <= cursor[0] < x + w and y <= cursor[1] < y + h

    def tick(self, now: float, cursor, mouse_down: bool, on_wake_strip: bool,
             fullscreen: bool) -> None:
        """cursor: (x, y) in page points relative to the panel's top-left, or
        None when the pointer is on another screen."""
        inside = self.inside(cursor)
        self.ui.set_interactive(inside)
        if inside != self.pointer_inside:
            self.pointer_inside = inside
            self.ui.post({"type": "pointer", "inside": inside})

        # hover wake
        if self.visible and not self.leaving:
            if self.hover_reveal and on_wake_strip:
                self.gate_visible_until = max(self.gate_visible_until, now + HOVER_GRACE_S)
            self.wake_since = None
        elif self.leaving or not on_wake_strip or fullscreen:
            self.wake_since = None
        elif self.wake_since is None:
            self.wake_since = now
        elif now - self.wake_since >= WAKE_DWELL_S:
            self.wake_since = None
            self.show("hover", now)

        # a click outside an open card folds it, same as Esc
        pressed = mouse_down and not self.lmb_was_down
        self.lmb_was_down = mouse_down
        if pressed and not inside and self.visible and self.island_state in self.CARD_STATES:
            self.ui.post({"type": "outside-click"})

        # the hotkey's collapse, then the slide-out
        if self.hide_at and self.island_state in ("idle", "off") and now >= self.hide_at:
            reason, self.hide_at = self.hide_reason, 0.0
            self.sticky = False
            self.hide(reason, now)
        if self.leaving and now >= self.leave_started + self.LEAVE_SLIDE_S:
            self.finish_hide()

        # idle auto-hide
        if self.visible and not self.sticky:
            if self.island_state in self.HELD_STATES or self.pointer_inside:
                grace = HOVER_GRACE_S if self.hover_reveal else self.SHOW_GRACE_S
                self.gate_visible_until = max(self.gate_visible_until, now + grace)
            elif now >= self.gate_visible_until:
                self.hide("hover ended" if self.hover_reveal else f"idle after {self.SHOW_GRACE_S:.0f}s", now)


# ---------------------------------------------------------------------------
# Carbon global hotkey (no Accessibility permission, unlike key monitors)
# ---------------------------------------------------------------------------
def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), "big")


class CarbonHotKey:
    def __init__(self, on_press):
        self.on_press = on_press
        self.ok = False
        self.failures = 0
        self._ref = ctypes.c_void_p()
        self._handler_ref = ctypes.c_void_p()
        self._cb = None
        self._carbon = None
        try:
            carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")

            class EventTypeSpec(ctypes.Structure):
                _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

            class EventHotKeyID(ctypes.Structure):
                _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

            handler_t = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
            carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
            carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, handler_t, ctypes.c_uint32,
                                                   ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                                   ctypes.POINTER(ctypes.c_void_p)]
            carbon.InstallEventHandler.restype = ctypes.c_int32
            carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
                                                   ctypes.c_void_p, ctypes.c_uint32,
                                                   ctypes.POINTER(ctypes.c_void_p)]
            carbon.RegisterEventHotKey.restype = ctypes.c_int32

            def _handler(_call, _event, _data):
                try:
                    self.on_press()
                except Exception as exc:
                    log(f"hotkey handler failed: {exc!r}")
                return 0

            self._cb = handler_t(_handler)        # keep alive
            self._carbon, self._EventHotKeyID = carbon, EventHotKeyID
            spec = EventTypeSpec(_fourcc("keyb"), 5)   # kEventClassKeyboard, kEventHotKeyPressed
            status = carbon.InstallEventHandler(carbon.GetApplicationEventTarget(), self._cb, 1,
                                                ctypes.byref(spec), None, ctypes.byref(self._handler_ref))
            if status != 0:
                log(f"hotkey handler install failed (OSStatus {status})")
                self._carbon = None
        except Exception as exc:
            log(f"Carbon unavailable for the hotkey: {exc!r}")
            self._carbon = None

    def register(self) -> bool:
        """Claim Control+Option+N; retried from the tick until it succeeds
        (another app, or a previous overlay still shutting down, may hold it)."""
        if self.ok or self._carbon is None:
            return self.ok
        carbon = self._carbon
        status = carbon.RegisterEventHotKey(KVK_ANSI_N, CONTROL_KEY | OPTION_KEY,
                                            self._EventHotKeyID(_fourcc("ACCN"), 1),
                                            carbon.GetApplicationEventTarget(), 0,
                                            ctypes.byref(self._ref))
        if status == 0:
            self.ok = True
            if self.failures:
                log(f"hotkey registered after {self.failures} retries")
            return True
        if not self.failures:
            log(f"hotkey registration failed (OSStatus {status}"
                f"{': another app owns Control+Option+N' if status == -9878 else ''}); retrying")
        self.failures += 1
        return False


# ---------------------------------------------------------------------------
# AppKit / WebKit layer
# ---------------------------------------------------------------------------
def _single_instance():
    import fcntl
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = LOCK_FILE.open("w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh                         # held for the life of the process


def main() -> int:  # pragma: no cover - needs macOS
    if sys.platform != "darwin":
        print("notch_host_mac is macOS-only", file=sys.stderr)
        return 1
    lock = _single_instance()
    if lock is None:
        return 0
    log(f"starting (port {PORT})")

    import objc
    from AppKit import (NSApplication, NSBackingStoreBuffered, NSColor, NSEvent, NSPanel,
                        NSRunningApplication, NSScreen)
    from Foundation import NSMakeRect, NSObject, NSTimer, NSURL, NSURLRequest
    from WebKit import WKUserScript, WKWebView, WKWebViewConfiguration

    STYLE = 0 | (1 << 7)               # borderless | non-activating panel
    BEHAVIOR = (1 << 0) | (1 << 4) | (1 << 6) | (1 << 8)   # all Spaces, stationary,
    #                                     ignores Cmd-Tab cycling, beside full-screen apps
    LEVEL = 25 + 1                     # just above the status/menu bar level
    PRESENT_FULLSCREEN = 1 << 10
    PRESENT_HIDE_MENU = 1 << 3

    class IslandPanel(NSPanel):
        def canBecomeKeyWindow(self):      # the island's field takes typing
            return True

        def canBecomeMainWindow(self):
            return False

    # The panel's controller is also WebKit's message handler and navigation
    # delegate; declaring the protocols gives PyObjC their method signatures.
    protocols = []
    for name in ("WKScriptMessageHandler", "WKNavigationDelegate", "NSWindowDelegate"):
        try:
            protocols.append(objc.protocolNamed(name))
        except Exception:
            pass

    class MacNotch(NSObject, protocols=protocols):
        # -- platform surface for IslandLogic --------------------------------
        def show(self):
            self.panel.orderFrontRegardless()

        def hide(self):
            self.panel.orderOut_(None)

        def post(self, msg):
            js = "window.__notchHost && window.__notchHost(%s)" % json.dumps(msg)
            try:
                self.web.evaluateJavaScript_completionHandler_(js, None)
            except Exception as exc:
                log(f"post failed: {exc!r}")

        def set_interactive(self, on):
            if self._interactive != bool(on):
                self._interactive = bool(on)
                self.panel.setIgnoresMouseEvents_(not on)

        def raise_main(self):
            # The desktop app runs the bridge in-process, and the bridge
            # spawned this host: the parent IS the app. Bring it forward when
            # it has a window on screen; otherwise (a headless bridge) say no
            # and the URL opens in the browser.
            ppid = os.getppid()
            try:
                import Quartz
                windows = Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                    Quartz.kCGNullWindowID) or []
                if not any(int(w.get("kCGWindowOwnerPID", -1)) == ppid and int(w.get("kCGWindowLayer", 1)) == 0
                           for w in windows):
                    return False
            except Exception:
                pass
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(ppid)
            if app is None:
                return False
            app.activateWithOptions_(1 << 1)        # ignoring other apps
            log(f"raised main app (pid {ppid})")
            return True

        def open_url(self, url):
            try:
                subprocess.Popen(["open", url])
                log(f"open in main app (browser fallback): {url}")
            except Exception as exc:
                log(f"open-url failed: {exc!r}")

        def log(self, text):
            log(text)

        # -- WebKit delegates -------------------------------------------------
        def userContentController_didReceiveScriptMessage_(self, _ucc, message):
            try:
                msg = json.loads(str(message.body()))
            except Exception:
                return
            if isinstance(msg, dict):
                self.logic.on_page_message(msg)

        def webView_didFinishNavigation_(self, _web, _nav):
            self.nav_retry_due = 0.0
            log("page loaded")

        def webView_didFailProvisionalNavigation_withError_(self, _web, _nav, _err):
            # Keep retrying until the bridge serves (a model load can block
            # it for minutes at boot). The health poll handles real shutdowns.
            self.nav_retry_due = time.monotonic() + NAV_RETRY_S
            log("navigation failed; retrying")

        def webView_didFailNavigation_withError_(self, web, nav, err):
            self.webView_didFailProvisionalNavigation_withError_(web, nav, err)

        # -- window delegate ---------------------------------------------------
        def windowDidBecomeKey_(self, _note):
            # A click on the island made it key: route typing to the page and
            # put the caret in the field.
            self.panel.makeFirstResponder_(self.web)
            self.post({"type": "focus-input"})

        # -- the poll ------------------------------------------------------------
        def tick_(self, _timer):
            try:
                if self.exit_requested:
                    NSApplication.sharedApplication().terminate_(None)
                    return
                now = time.monotonic()
                self.ticks += 1
                if self.ticks % 50 == 0:
                    self.place()
                    self.hotkey.register()
                if self.nav_retry_due and now >= self.nav_retry_due:
                    self.nav_retry_due = 0.0
                    self.load()
                m = NSEvent.mouseLocation()
                f = self.panel.frame()
                sf = self.screen_frame
                on_screen = (sf.origin.x <= m.x < sf.origin.x + sf.size.width
                             and sf.origin.y <= m.y <= sf.origin.y + sf.size.height)
                cursor = ((m.x - f.origin.x, (f.origin.y + f.size.height) - m.y) if on_screen else None)
                top = sf.origin.y + sf.size.height
                mid = sf.origin.x + sf.size.width / 2
                on_wake = on_screen and m.y >= top - WAKE_EDGE_PT and abs(m.x - mid) <= WAKE_HALF_W_PT
                opts = NSApplication.sharedApplication().currentSystemPresentationOptions()
                fullscreen = bool(opts & (PRESENT_FULLSCREEN | PRESENT_HIDE_MENU))
                self.logic.tick(now, cursor, bool(NSEvent.pressedMouseButtons() & 1), on_wake, fullscreen)
            except Exception as exc:
                log(f"tick error: {exc!r}")

        # -- setup -----------------------------------------------------------------
        def target_screen(self):
            # The screen with the notch (a non-zero top safe area, macOS 12+),
            # else the main screen.
            for screen in NSScreen.screens() or []:
                try:
                    if screen.safeAreaInsets().top > 0:
                        return screen
                except Exception:
                    break
            return NSScreen.mainScreen()

        def place(self):
            sf = self.target_screen().frame()
            if self.screen_frame is not None and sf == self.screen_frame:
                return
            self.screen_frame = sf
            x = sf.origin.x + (sf.size.width - WIDTH_PT) / 2
            y = sf.origin.y + sf.size.height - HEIGHT_PT
            self.panel.setFrame_display_(NSMakeRect(x, y, WIDTH_PT, HEIGHT_PT), True)

        def load(self):
            self.web.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(PAGE_URL)))

        def build(self):
            self.ticks = 0
            self.exit_requested = False
            self.nav_retry_due = 0.0
            self.screen_frame = None
            self._interactive = True
            self.logic = IslandLogic(self)

            self.panel = IslandPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, WIDTH_PT, HEIGHT_PT), STYLE, NSBackingStoreBuffered, False)
            p = self.panel
            p.setLevel_(LEVEL)
            p.setCollectionBehavior_(BEHAVIOR)
            p.setOpaque_(False)
            p.setBackgroundColor_(NSColor.clearColor())
            p.setHasShadow_(False)               # the page draws its own
            p.setHidesOnDeactivate_(False)
            p.setFloatingPanel_(True)
            p.setMovable_(False)
            p.setDelegate_(self)
            self.set_interactive(False)

            config = WKWebViewConfiguration.alloc().init()
            try:
                # sounds play when something happens, not after a click
                config.setMediaTypesRequiringUserActionForPlayback_(0)
            except Exception:
                pass
            ucc = config.userContentController()
            ucc.addUserScript_(WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
                SHIM_JS, 0, True))                # at document start
            ucc.addScriptMessageHandler_name_(self, "notch")
            self.web = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, WIDTH_PT, HEIGHT_PT), config)
            self.web.setValue_forKey_(False, "drawsBackground")     # transparent page
            self.web.setNavigationDelegate_(self)
            p.setContentView_(self.web)
            self.place()
            p.orderOut_(None)                    # starts tucked away
            self.load()

            self.hotkey = CarbonHotKey(lambda: self.logic.toggle("hotkey"))
            self.hotkey.register()
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                POLL_S, self, "tick:", None, True)
            threading.Thread(target=self.health_loop, daemon=True).start()
            return self

        def health_loop(self):
            seen, misses = False, 0
            while True:
                try:
                    with urllib.request.urlopen(f"{BASE}/api/health", timeout=1.5) as resp:
                        alive = b"accuretta" in resp.read(2048)
                except Exception:
                    alive = False
                if alive:
                    seen, misses = True, 0
                elif seen:
                    # Only an already-contacted bridge going silent means
                    # "shut down"; at boot it can be bound but not serving.
                    misses += 1
                    if misses >= HEALTH_MAX_MISS:
                        log("bridge unreachable; exiting")
                        self.exit_requested = True
                        return
                time.sleep(2.0)

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)          # accessory: no Dock icon, no menu bar
    host = MacNotch.alloc().init().build()
    log("panel ready; navigating")
    try:
        app.run()
    finally:
        log("stopped")
        del host
    return 0


if __name__ == "__main__":
    sys.exit(main())
