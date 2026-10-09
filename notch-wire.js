/* Accuretta Notch — bridge wiring.
 *
 * Served alongside accuretta-notch.html and appended to the page at serve
 * time by bridge.py (the HTML file on disk is never modified). Everything
 * between the widget and the Accuretta bridge goes through this file:
 *
 *   bridge -> widget : /api/events SSE stream mapped onto window.notch calls
 *   widget -> bridge : the `notch` CustomEvent on #island mapped onto REST
 *
 * The widget never runs a command. It shows approval gates and returns the
 * owner's decision; the bridge's permission layer executes (or refuses).
 */
(() => {
"use strict";

const notch = window.notch;
const island = document.getElementById("island");
if (!notch || !island) return;

// Wired mode first, before any notch call: no fake gates, tool lines,
// replies or model loads may ever appear.
notch.config.demo = false;
notch.config.mockLoadMs = 0;

const TOKEN = document.querySelector('meta[name="accuretta-request-token"]')?.content || "";

const api = (path, body) => fetch(path, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-Accuretta-Token": TOKEN },
  body: JSON.stringify(body || {}),
});

const getJson = (path) => fetch(path).then(r => (r.ok ? r.json() : null)).catch(() => null);

const state = () => island.dataset.state || "idle";

// ---- local bookkeeping ---------------------------------------------------
const pending = new Map();          // gate id -> raw approval entry (+_gate)
let modelList = [];                 // [{name, meta, path, loaded}]
let activeModelIdx = 0;
let pendingLoadPath = "";           // model load we asked the bridge for
let activeChatId = "";              // chat that most recently ran a turn (any surface)
let streaming = false;              // a notch-initiated /api/chat is in flight
let notchChatId = "";
let lastToolStartAt = 0;            // last tool_start seen on any surface

// ---- live turn mirror --------------------------------------------------------
// The compact work pill shows one line. Expanding the island must show the
// whole running turn — the prompt, every tool call as it runs, and the reply
// as it streams — for a turn THIS surface started (runTurn reads its own
// POST stream) or one running on another surface (mirrored off /api/events;
// the bridge forwards token deltas as best-effort transient SSE events so
// the ring buffer keeps approvals durable).
const mirror = { active: false, prompt: "" };
let turnChatId = "";                // chat whose turn the card mirrors right now
const turnToolRows = [];            // live tool rows inside #thread
const handledCalls = new Set();     // tool_start dedupe across POST + SSE copies
let replyOpen = false;              // the card shows the live turn's prompt+reply
let replyRaw = "";                  // reply text rendered into #agentMsg
let fadedChars = 0;                 // plain-text length already given the fade-in
let wordBuf = "";                   // pending word-flush buffer
let lastFlush = 0;
let stripThink = makeThinkStripper();

const turnLive = () => !!(streaming || mirror.active);
try { notchChatId = localStorage.getItem("accuretta.notch.chat") || ""; } catch (e) {}

const WRITE_KINDS = new Set(["write_file", "edit_file", "replace_ast_node"]);

// ---- host (window) messaging ----------------------------------------------
const hostPost = (msg) => {
  try { if (window.chrome && window.chrome.webview) window.chrome.webview.postMessage(msg); } catch (e) {}
};

// Page-side exceptions are invisible from the host otherwise, and a silently
// broken handler looks exactly like "the feature just doesn't work".
window.addEventListener("error", (e) => {
  hostPost({ type: "diag", what: "page-error", hasFocus: false, visibility: "",
             active: "", qlen: -1, hint: "", hintShown: false, keys: 0, lastKey: "",
             pointers: 0, lastPointer: "", toolIcon: false, orb: false,
             planningHidden: false, chipNeural: false, menuScrolls: false,
             taskPanel: false, taskText: "",
             tool: String((e && e.message) || "error").slice(0, 160),
             userAlign: "", inputVisible: null, cardW: 0, cardH: 0,
             streaming: false, state: "error" });
});

// Something worth surfacing: reveal the island even if it was tucked away.
const notify = (kind) => hostPost({ type: "notify", kind });

let rectRaf = 0;
let lastObservedState = state();
function reportRect() {
  if (rectRaf) return;
  const tick = () => {
    const r = island.getBoundingClientRect();
    hostPost({ type: "island-rect", x: r.x, y: r.y, w: r.width, h: r.height, state: state() });
    // Keep reporting while the spring animation runs so the host's
    // click-through region tracks the island instead of lagging behind it.
    rectRaf = island.classList.contains("settled") ? 0 : requestAnimationFrame(tick);
  };
  rectRaf = requestAnimationFrame(tick);
}

// ---- tool icons (same SVG paths the main app uses) -------------------------
// Reused verbatim from app.js TOOL_ICON_MAP so a tool reads identically in
// both surfaces. Long/complex glyphs (search, delete) fall back to the wrench
// rather than dragging kilobytes of path data into the overlay.
const WRENCH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';
const RUNNING_COMMAND_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 17H20"/><path d="M5 7L10 12L5 17"/></svg>';
const WRITING_FILE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.093,1.293l-11.2,11.2a.99.99,0,0,0-.242.391l-1.6,4.8A1,1,0,0,0,5,19a1.014,1.014,0,0,0,.316-.051l4.8-1.6a1.006,1.006,0,0,0,.391-.242l11.2-11.2a1,1,0,0,0,0-1.414l-3.2-3.2A1,1,0,0,0,17.093,1.293ZM9.26,15.526l-2.679-6.433L17.8,3.414,19.586,5.2ZM3,21H20a1,1,0,0,1,0,2H3a1,1,0,0,1,0-2Z"/></svg>';
const EDITING_FILE_SVG = '<svg viewBox="0 0 48 48" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5.5,9.2H9.2v0H35.1a3.9828,3.9828,0,0,1,3.7,3.7l.1123,20.7359"/><path d="M9.281,13.7433,9.2,35.1a3.9807,3.9807,0,0,0,3.7,3.7H38.8v0h3.7"/><path d="M16.6,31.4V27.7L27.7,16.6l3.7,3.7L20.3,31.4Z"/></svg>';
const GLOBE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>';
const REGISTRY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>';
const MCP_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3v5M16 3v5M6 8h12v3a6 6 0 0 1-6 6v4"/></svg>';
const TEST_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6M10 3v5l-5 9a2.7 2.7 0 0 0 2.4 4h9.2a2.7 2.7 0 0 0 2.4-4l-5-9V3"/><path d="M7.8 15h8.4"/></svg>';

const FILE_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
const FOLDER_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const SEARCH_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="m20 20-3.6-3.6"/></svg>';

// Icons are keyed by KIND, not by tool name: every read/list/grep call lands
// on one glyph, so a long turn shows a handful of distinct chips instead of
// a row of identical wrenches. `toolKey` is also what the chip strip groups
// by — same kind, one chip with a count.
const TOOL_ICONS = {
  file: FILE_SVG, folder: FOLDER_SVG, search: SEARCH_SVG,
  write: WRITING_FILE_SVG, edit: EDITING_FILE_SVG,
  run: RUNNING_COMMAND_SVG, test: TEST_SVG,
  web: GLOBE_SVG, registry: REGISTRY_SVG, mcp: MCP_SVG,
};

const TOOL_KEYS = {
  read_file: "file", read_text_file: "file",
  list_directory: "folder", list_files: "folder", find_files: "folder",
  grep_files: "search", search_files: "search",
  write_file: "write", create_file: "write",
  edit_file: "edit", patch_file: "edit", replace_ast_node: "edit",
  run_powershell: "run", powershell: "run", sandbox: "run", session: "run",
  open_program: "run", launch: "run", git: "run",
  run_test: "test", test: "test",
  web_fetch: "web", download_file: "web",
  web_search: "web", network_snapshot: "web", whois: "web",
  registry: "registry",
};

function toolKey(name) {
  const n = String(name || "").toLowerCase();
  if (n.startsWith("mcp_")) return "mcp";
  return TOOL_KEYS[n] || "wrench";
}

function toolIconSvg(name) {
  return TOOL_ICONS[toolKey(name)] || WRENCH_SVG;
}

// The right end of the work pill carries the running tool's glyph — the same
// icon the main app puts on the call, sitting exactly where the redundant
// "on it" echo used to. Created on demand; hidden whenever no tool is running.
function ensureToolIcon() {
  const holder = document.querySelector(".v-work .wr");
  if (!holder) return null;
  let span = holder.querySelector(".tool-ico");
  if (!span) {
    span = document.createElement("span");
    span.className = "tool-ico";
    span.setAttribute("aria-hidden", "true");
    holder.appendChild(span);
  }
  return span;
}

// ---- open in main app -------------------------------------------------------
// The notch is a subset on purpose. When a turn that did real work finishes,
// offer the exit to the full surface instead of pretending it is all here.
function showOpenMain() {
  const host = document.querySelector(".msg.agent");
  if (!host) return;
  let btn = document.getElementById("openMain");
  if (!btn) {
    btn = document.createElement("button");
    btn.id = "openMain";
    btn.className = "om-btn";
    btn.type = "button";
    btn.textContent = "Open in main app \u2197";
    btn.addEventListener("click", () => {
      const cid = notchChatId || activeChatId || "";
      // Tell the desktop window WHICH session to show. The host raises the
      // app; this event makes it land on this conversation (a browser
      // deep-link can't reach the already-running pywebview window).
      if (cid) api("/api/ui/open-chat", { chat_id: cid }).catch(() => {});
      hostPost({ type: "open-url", url: location.origin + (cid ? "/?chat=" + encodeURIComponent(cid) : "/") });
    });
    host.appendChild(btn);
  }
  btn.hidden = false;
}

// ---- live status in the prompt view -----------------------------------------
// The work pill owns the status line, but the owner can leave the pill: any
// click on the island opens the conversation while a turn is still running.
// Without a status line there, the thread reads as finished-or-dead and the
// owner cannot tell the request is still being worked on.
function liveStatusEl() {
  let el = document.getElementById("liveStatus");
  if (!el) {
    el = document.createElement("div");
    el.id = "liveStatus";
    const thread = document.getElementById("thread");
    if (thread) thread.appendChild(el);
  }
  return el;
}
function updateLiveStatus() {
  const el = liveStatusEl();
  if (!el) return;
  const on = turnLive() && (state() === "prompt") &&
    (lastActivity === "thinking" || lastActivity === "tool");
  if (on) {
    const line = document.getElementById("workSub");
    el.textContent = (lastActivity === "tool" && line && line.textContent.trim())
      ? line.textContent.trim() : "Thinking…";
  }
  // The line is the card's own "thinking" shimmer, matching the pill: a
  // highlight sweeps the words instead of a dead dim sentence.
  el.classList.toggle("shimmer", on);
  // The injected base rule pins #liveStatus at display:none; resetting the
  // inline value to "" (the old toggle) re-routed to THAT rule and the line
  // stayed invisible forever while its text said "thinking…" cruelly out of
  // sight. A real display value is the only thing that beats the default.
  el.style.display = on ? "block" : "none";
  updateComposerAction();
}

// ---- composer action (send <-> stop) ----------------------------------------
// While a turn is live (ours or mirrored), the send button morphs into a stop
// button and the input becomes the steering field — exactly the main app's
// contract: Enter sends a correction, the button stops the run.
function updateComposerAction() {
  const btn = document.getElementById("send");
  const q = document.getElementById("q");
  const live = turnLive();
  if (btn) {
    btn.classList.toggle("is-stop", live);
    btn.setAttribute("aria-label", live ? "Stop the running task" : "Send");
    btn.title = live ? "Stop the running task" : "Send (Enter)";
  }
  if (q) {
    q.placeholder = live ? "Add a correction or more context…" : "Tell Accuretta what to do";
  }
}

async function cancelActiveTurn() {
  const cid = turnChatId || notchChatId || activeChatId || "";
  if (!cid) return;
  try { await api("/api/cancel", { chat_id: cid }); } catch (e) {}
}

// ---- steering from the notch ------------------------------------------------
// A turn already running anywhere (this surface's own, or one mirrored from
// the main app) takes the composer text as a STEER — a mid-task correction the
// harness folds in at the next safe boundary, exactly like the main app's
// queue. Only an idle composer starts a new turn.
const steerPending = new Map();     // steer id -> the bubble we rendered for it
const steerSeq = { n: 0 };

function showSteerMessage(id, text) {
  const thread = document.getElementById("thread");
  if (!thread) return;
  const el = document.createElement("div");
  el.className = "msg user steer";
  el.textContent = text;
  el.title = "Sent to the running task — it lands at the next safe step.";
  // Above the live reply, below the prompt and tool strip: it reads as part
  // of the running turn, not as a new question.
  const agent = thread.querySelector(".msg.agent");
  if (agent) thread.insertBefore(el, agent); else thread.appendChild(el);
  thread.scrollTop = 1e9;
  if (id) steerPending.set(id, el);
}

function onSteeringEvent(evt) {
  const id = String(evt.id || "");
  if (evt.type === "steering_applied" && id) {
    const el = steerPending.get(id);
    if (el) { el.classList.add("applied"); steerPending.delete(id); }
  } else if (evt.type === "steering_deferred") {
    for (const mid of (evt.ids || [])) {
      const el = steerPending.get(mid);
      if (el) {
        el.classList.add("deferred");
        el.textContent += "  — not delivered (the task ended first)";
        steerPending.delete(mid);
      }
    }
  }
}

async function sendPrompt(prompt) {
  const text = String(prompt || "").trim();
  if (!text) return;
  const cid = turnChatId || notchChatId || activeChatId || "";
  if (turnLive() && cid) {
    const id = `nt-${Date.now().toString(36)}-${(steerSeq.n++).toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
    let status = "";
    try {
      const r = await api("/api/chat/steer", { chat_id: cid, id, text });
      const j = await r.json().catch(() => null);
      status = String((j && j.status) || "");
    } catch (e) { status = ""; }
    if (status === "pending" || status === "applied") {
      showSteerMessage(status === "pending" ? id : "", text);
      return;
    }
    if (status === "full") {
      const thread = document.getElementById("thread");
      if (thread) {
        const el = document.createElement("div");
        el.className = "msg user steer deferred";
        el.textContent = text + "  — not delivered (too many updates queued)";
        thread.appendChild(el);
        thread.scrollTop = 1e9;
      }
      return;
    }
    // "inactive" (the turn ended between render and send) or a transport
    // error: fall through and send it as a normal new turn.
  }
  runTurn(text);
}

// The button is stop while a turn is live; capture phase so the widget's own
// click handler (which would call send) never sees it.
document.addEventListener("click", (e) => {
  const btn = e.target.closest && e.target.closest("#send");
  if (!btn || !turnLive()) return;
  e.preventDefault();
  e.stopImmediatePropagation();
  cancelActiveTurn();
}, true);

// ---- conversations dropdown --------------------------------------------------
// The button right of the model chip lists every session with "New session"
// pinned first. Picking one loads its latest turn into the card and makes it
// the notch's chat; New session clears the binding so the next send creates a
// fresh one. Picking is disabled while a turn runs (the card is that turn's).
function closeConvMenu() {
  if (!island.hasAttribute("data-conv")) return false;
  island.removeAttribute("data-conv");
  const btn = document.getElementById("convBtn");
  if (btn) btn.setAttribute("aria-expanded", "false");
  return true;
}

const CONV_STATE_LABELS = {
  working: "working", "needs-you": "needs you", finished: "finished",
  interrupted: "interrupted", stopped: "stopped", failed: "failed",
};

async function openConvMenu() {
  const menu = document.getElementById("convMenu");
  if (!menu) return;
  if (island.hasAttribute("data-conv")) { closeConvMenu(); return; }
  if (typeof notch.closeMenu === "function") notch.closeMenu();  // model menu
  const live = turnLive();
  const data = await getJson("/api/chats?summary=1");
  const order = (data && data.order) || [];
  const chats = (data && data.chats) || {};
  const rows = [{ id: "", title: "New session", meta: live ? "task running" : "",
                  selected: !notchChatId, disabled: live }];
  for (const id of order) {
    const c = chats[id] || {};
    rows.push({
      id,
      title: String(c.title || id),
      meta: CONV_STATE_LABELS[c.task_state] || "",
      selected: id === notchChatId,
      disabled: live && id !== notchChatId,
    });
  }
  menu.innerHTML = "";
  for (const row of rows) {
    const opt = document.createElement("button");
    opt.type = "button";
    opt.className = "opt";
    opt.setAttribute("role", "option");
    opt.setAttribute("aria-selected", String(row.selected));
    opt.dataset.id = row.id;
    if (row.disabled) opt.disabled = true;
    const chk = document.createElement("span");
    chk.className = "chk";
    chk.textContent = row.selected ? "\u2713" : "";
    const nm = document.createElement("span");
    nm.className = "nm";
    nm.textContent = row.title;
    const mt = document.createElement("span");
    mt.className = "mt";
    mt.textContent = row.meta;
    opt.append(chk, nm, mt);
    if (!row.disabled) {
      opt.addEventListener("click", (e) => {
        e.stopPropagation();          // the widget's island handler owns .opt
        onConvPicked(row.id);
      });
    }
    menu.appendChild(opt);
  }
  island.setAttribute("data-conv", "");
  const btn = document.getElementById("convBtn");
  if (btn) btn.setAttribute("aria-expanded", "true");
  if (typeof notch.positionMenu === "function") notch.positionMenu(menu);
  reportRect();
}

async function onConvPicked(id) {
  closeConvMenu();
  if (turnLive()) return;                 // belt and braces; menu disables these
  if (id === notchChatId && state() === "prompt") return;
  clearTurnRows();
  const thread = document.getElementById("thread");
  if (thread) thread.querySelectorAll(".msg.arch").forEach((el) => el.remove());
  island.removeAttribute("data-expanded");
  threadExpanded = false;
  const om = document.getElementById("openMain");
  if (om) om.hidden = true;
  replyRaw = ""; fadedChars = 0; wordBuf = ""; lastFlush = 0;
  if (!id) {
    notchChatId = "";
    try { localStorage.removeItem("accuretta.notch.chat"); } catch (e) {}
    notch.beginReply("");
    setActivity("idle");
    return;
  }
  notchChatId = id;
  try { localStorage.setItem("accuretta.notch.chat", id); } catch (e) {}
  const chat = await getJson(`/api/chats/${encodeURIComponent(id)}`);
  const msgs = (chat && Array.isArray(chat.messages)) ? chat.messages : [];
  const visible = msgs.filter(m => (m.role === "user" || m.role === "assistant")
                                 && !m._internal && !m.invisible);
  const lastUser = [...visible].reverse().find(m => m.role === "user");
  const lastAssistant = [...visible].reverse().find(m => m.role === "assistant");
  notch.beginReply(lastUser ? msgText(lastUser.content) : "");
  if (lastAssistant) {
    replyRaw = visibleFinalText(msgText(lastAssistant.content));
    fadedChars = replyRaw.length;         // loaded history must not re-fade
    renderReply();
  }
  setActivity("idle");
}

(function wireConvMenu() {
  const btn = document.getElementById("convBtn");
  if (btn) {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openConvMenu();
    });
  }
  document.addEventListener("click", (e) => {
    if (!island.hasAttribute("data-conv")) return;
    if (e.target.closest && (e.target.closest("#convMenu") || e.target.closest("#convBtn"))) return;
    closeConvMenu();
  }, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && island.hasAttribute("data-conv")) {
      closeConvMenu();
      e.stopPropagation();
    }
  }, true);
  new MutationObserver(() => {
    // Any state change (a gate arriving, the turn ending) dismisses the list.
    if (island.hasAttribute("data-conv") && state() !== "prompt") closeConvMenu();
  }).observe(island, { attributes: true, attributeFilter: ["data-state"] });
})();

// ---- tool chips in the expanded card ---------------------------------------
// Every tool_start becomes ONE small glyph in a compact strip between the
// prompt and the reply — the main app's icon language, sized for the island.
// No bars, no labels, no dots: the running glyph breathes blue, finished ones
// settle dim, and the exact action text lives in the tooltip and in the
// shimmering status line below. A chip stays "running" until the next event
// proves it finished (another tool starts, or words start streaming).
function ensureToolStrip() {
  const thread = document.getElementById("thread");
  if (!thread) return null;
  let strip = thread.querySelector(".turn-tools");
  if (!strip) {
    strip = document.createElement("div");
    strip.className = "turn-tools";
    const userMsg = document.getElementById("userMsg");
    if (userMsg && userMsg.parentNode === thread) userMsg.insertAdjacentElement("afterend", strip);
    else thread.appendChild(strip);
  }
  return strip;
}

function addTurnToolRow(evt) {
  const strip = ensureToolStrip();
  if (!strip) return;
  markRowsDone();
  const key = toolKey(evt.name);
  const label = toolLabel(evt.name, evt.arguments);
  let group = turnToolRows.find((r) => r.key === key);
  if (group) {
    // Same kind of work again: bump the count instead of adding another
    // identical glyph (a 30-call read loop stays one chip).
    group.count += 1;
    if (group.countEl) {
      group.countEl.textContent = "×" + group.count;
    } else {
      group.countEl = document.createElement("b");
      group.countEl.className = "tcount";
      group.countEl.textContent = "×" + group.count;
      group.el.appendChild(group.countEl);
    }
    group.el.title = `${label}  (+${group.count - 1} more of this kind)`;
  } else {
    const chip = document.createElement("span");
    chip.className = "turn-tool running";
    chip.title = label;
    chip.innerHTML = toolIconSvg(evt.name);
    strip.appendChild(chip);
    group = { key, el: chip, count: 1, countEl: null, running: true };
    turnToolRows.push(group);
  }
  group.running = true;
  group.el.classList.remove("done");
  group.el.classList.add("running");
  const thread = document.getElementById("thread");
  if (thread) thread.scrollTop = 1e9;
}

function markRowsDone() {
  for (const r of turnToolRows) {
    if (!r.running) continue;
    r.running = false;
    r.el.classList.remove("running");
    r.el.classList.add("done");
  }
}

function clearTurnRows() {
  for (const r of turnToolRows) r.el.remove();
  turnToolRows.length = 0;
  // Steer bubbles belong to the turn that received them. A new turn or a
  // session switch must not carry one conversation's corrections into the
  // next (they showed up — green — in every conversation switched to).
  for (const el of document.querySelectorAll("#thread .msg.user.steer")) el.remove();
  steerPending.clear();
}

// A turn started on another surface: the user prompt is already persisted by
// the time chat_start broadcasts, so pull it once and echo it above the live
// tool rows when the owner expands the card.
async function seedMirrorPrompt(chatId) {
  if (!chatId) return;
  const chat = await getJson(`/api/chats/${encodeURIComponent(chatId)}`);
  if (!mirror.active || turnChatId !== chatId) return;
  const msgs = (chat && Array.isArray(chat.messages)) ? chat.messages : [];
  const u = [...msgs].reverse().find(m => m && m.role === "user" && !m._internal && !m.invisible);
  if (!u) return;
  mirror.prompt = msgText(u.content);
  if (state() === "prompt") {
    const um = document.getElementById("userMsg");
    if (um) um.textContent = mirror.prompt;
  }
}

// One tool_start handler for BOTH copies of every event (the notch's own
// POST response and the SSE broadcast). The work pill keeps its single-line
// behaviour; the row is what makes the expanded card read like the main app.
function onToolStart(evt) {
  const key = String(evt.call_id || evt.name || "") + "|" + String(evt.name || "");
  if (handledCalls.has(key)) return;
  handledCalls.add(key);
  // Another surface's chat must not paint its tools over our turn's card.
  if (turnChatId && evt.chat_id && String(evt.chat_id) !== turnChatId) return;
  if (state() === "gate" || state() === "alert") return;   // the gate owns the screen
  if (!(streaming && state() === "prompt") && state() !== "work") notch.setState("work");
  setToolLine(evt.name, evt.arguments);
  if (streaming || mirror.active) addTurnToolRow(evt);
}

// ---- activity ------------------------------------------------------------
// ONE live-event line, not a status word plus an echo: the pill starts at
// "thinking…" and the line is then REPLACED by each live event as it happens
// ("Reading main.py…", "Running command…", "writing…"). The whole line
// shimmers so it reads as live; the tool's own glyph sits on the right where
// the old "on it" echo was.
const ACTIVITY_THINKING = "thinking…";
const ACTIVITY_WORDS = { thinking: ACTIVITY_THINKING, composing: "writing…", tool: "" };
let lastActivity = "";
function setActivity(phase) {
  const line = document.getElementById("workSub");
  if (line) {
    // setTool() writes the tool phrase itself; the other phases own theirs.
    if (phase !== "tool") line.textContent = ACTIVITY_WORDS[phase] || "";
    line.classList.toggle("shimmer", !!line.textContent.trim());
    line.style.display = line.textContent.trim() ? "" : "none";
  }
  setWorkIcon(phase);
  if (phase !== lastActivity) {
    lastActivity = phase;
    reportDiag("activity");
  }
  updateLiveStatus();
}

// The right side is the running tool's glyph, not a second line. Hidden while
// the model only thinks/writes — the twinkling dot-matrix A already says the
// agent is the one working.
function setWorkIcon(phase) {
  const ico = ensureToolIcon();
  if (!ico) return;
  ico.style.display = phase === "tool" ? "" : "none";
}

// ---- dot-matrix pill FX -----------------------------------------------------
// The A is the agent. Instead of more text, one-shot animations use the pill's
// width to say what is happening: the A fires a scanning laser across the pill
// while it reads, sweeps itself away like dust when it deletes, assembles
// itself when it writes, and breaks down (physics-ish scatter + gravity) then
// rebuilds to its original shape when a turn with real work completes.
// Every dot already carries --k (column fraction) / --dx / --dy (center-out
// vector) from drawLogo, which is all the choreography needs.
const FX_DUR = { laser: 1300, sweep: 1200, build: 1000, break: 1500 };
let fxTimer = 0;

function ensureFxBeam() {
  const pill = document.querySelector(".v-work .pill");
  if (!pill) return null;
  let beam = pill.querySelector(".fx-beam");
  if (!beam) {
    beam = document.createElement("span");
    beam.className = "fx-beam";
    pill.appendChild(beam);
  }
  return beam;
}

function pillFX(kind) {
  if (!FX_DUR[kind]) return;
  const beam = ensureFxBeam();
  island.dataset.fx = kind;
  if (beam) {
    beam.dataset.kind = kind;
    beam.classList.remove("run");
    void beam.offsetWidth;            // restart the sweep animation
    beam.classList.add("run");
  }
  if (fxTimer) clearTimeout(fxTimer);
  fxTimer = setTimeout(() => {
    delete island.dataset.fx;
    if (beam) beam.classList.remove("run");
  }, FX_DUR[kind]);
}

// Tool class → FX. Reads/scans/searches fire the laser; anything destructive
// sweeps; writes assemble; everything else just fires the laser too — motion
// beats a static "working on it".
function fxForTool(name) {
  const n = String(name || "").toLowerCase();
  if (/delete|unlink|remove|clean|wipe|clear|purge/.test(n)) return "sweep";
  if (/write|edit|patch|replace|save|create/.test(n)) return "build";
  return "laser";
}

// ---- approved-task panel ------------------------------------------------
// The work pill only has room for a truncated line ("Running o…_"). Clicking it
// reveals what the agent is actually cleared to do, styled per action kind so
// a deletion never looks like a file write.
const RECENT_LIMIT = 6;
const recentDecisions = [];   // {gate, decision, at}
const recordedDecisions = new Set();   // gate ids already filed
const escHtml = (s) => (notchMarkdown ? notchMarkdown.escHtml(s)
  : String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));

function rememberDecision(gate, decision) {
  recentDecisions.unshift({ gate: gate || {}, decision, at: Date.now() });
  if (recentDecisions.length > RECENT_LIMIT) recentDecisions.length = RECENT_LIMIT;
}

const KIND_META = {
  registry: { label: "Registry change", tone: "warn" },
  delete: { label: "Deletion", tone: "warn" },
  delete_file: { label: "Deletion", tone: "warn" },
  write_file: { label: "Writing", tone: "write" },
  edit_file: { label: "Editing", tone: "write" },
  patch_file: { label: "Editing", tone: "write" },
  replace_ast_node: { label: "Editing", tone: "write" },
  powershell: { label: "Running on host", tone: "run" },
  sandbox: { label: "Running in sandbox", tone: "run" },
  session: { label: "Session command", tone: "run" },
  git: { label: "Git", tone: "run" },
  test: { label: "Tests", tone: "run" },
};
const gateMeta = (g) => KIND_META[String((g && (g.toolKind || g.kind)) || "")] ||
  { label: "Approved action", tone: "run" };

function taskPanelHtml() {
  // Only ever called with history: toggleTaskPanel skips the panel entirely
  // when recentDecisions is empty, so the island goes straight to the
  // conversation instead of showing a dead-end "nothing approved" card.
  return recentDecisions.map((d) => {
    const g = d.gate || {};
    const meta = gateMeta(g);
    const icon = toolIconSvg(kindToolName(g));
    const cmd = String(g.cmd || "").trim();
    const bits = [];
    if (g.scope) bits.push(escHtml(String(g.scope)));
    if (g.target) bits.push(escHtml(String(g.target)));
    if (g.writes && g.writes !== "none") bits.push("writes " + escHtml(String(g.writes)));
    return (
      '<div class="task-row tone-' + meta.tone + '">' +
        '<div class="task-head">' +
          '<span class="task-ico">' + icon + "</span>" +
          '<span class="task-kind">' + escHtml(meta.label) + "</span>" +
          '<span class="task-verdict ' + escHtml(d.decision) + '">' +
            (d.decision === "approve" ? "approved" : "denied") + "</span>" +
        "</div>" +
        (cmd ? '<code class="task-cmd">' + escHtml(cmd) + "</code>" : "") +
        (bits.length ? '<div class="task-meta">' + bits.join('<span class="dot-sep">·</span>') + "</div>" : "") +
      "</div>"
    );
  }).join("");
}

function kindToolName(g) {
  const k = String((g && (g.toolKind || g.kind)) || "");
  if (k === "registry") return "registry";
  if (k === "delete" || k === "delete_file") return "delete_file";
  return k || "run_powershell";
}

function closeTaskPanel() {
  const existing = document.getElementById("taskPanel");
  if (!existing) return false;
  existing.remove();
  island.removeAttribute("data-panel");
  return true;
}

function toggleTaskPanel() {
  if (closeTaskPanel()) {                // second click closes it
    if (state() === "prompt") notch.setState(turnLive() ? "work" : "idle");
    return;
  }
  island.setAttribute("data-thread", "");
  // A turn is running: the click reveals the LIVE TURN — prompt, tool rows,
  // streaming reply — instead of the historical approvals panel, which would
  // hide the conversation and answer nothing about what is happening now.
  if (turnLive()) {
    island.removeAttribute("data-panel");
    if (mirror.prompt) {
      const um = document.getElementById("userMsg");
      if (um) um.textContent = mirror.prompt;
    }
    notch.setState("prompt");
    if (turnLive()) island.setAttribute("data-busy", "");   // keep the logo twinkling
    if (replyRaw) renderReply();      // repaint words buffered while folded
    const t = document.getElementById("thread");
    if (t) t.scrollTop = 1e9;
    updateLiveStatus();
    reportDiag("task-panel");
    return;
  }
  // the work pill is the entry point; a click while idle also opens it
  if (state() === "idle") notch.setState("work");
  // No decisions recorded: skip the panel entirely. The owner clicked to
  // talk to the agent, not to be told there is no history — the conversation
  // card alone is the answer, and a "nothing approved yet" block read like
  // a dead end.
  if (recentDecisions.length) {
    const panel = document.createElement("div");
    panel.id = "taskPanel";
    panel.className = "task-panel";
    panel.innerHTML = taskPanelHtml();
    // HIDE the conversation, never remove it. #userMsg and #agentMsg are
    // children of #thread, and a click on the island lands here every single
    // time (the capture handler above routes idle/work clicks here), so
    // wiping the thread's innerHTML destroyed the only two elements
    // beginReply() and appendReply() write into.
    island.setAttribute("data-panel", "");
    document.getElementById("thread").appendChild(panel);
  }
  notch.setState("prompt");
  const t = document.getElementById("thread");
  // With a panel, its header is the entry point; without one, keep the latest
  // reply in view instead of the empty top of the thread.
  if (t) t.scrollTop = recentDecisions.length ? 0 : 1e9;
  updateLiveStatus();
  reportDiag("task-panel");
}

// ---- reply presentation ----------------------------------------------------
// Styling for rendered markdown plus hiding the demo caption the widget ships
// with ("Mock reply. Nothing ran."), which is wrong once replies are real.
(function injectReplyStyles() {
  if (document.getElementById("notch-md-styles")) return;
  const style = document.createElement("style");
  style.id = "notch-md-styles";
  style.textContent = [
    ".msg.agent .cap { display: none !important; }",
    ".msg.agent p { margin: 0 0 7px; }",
    ".msg.agent p:last-child { margin-bottom: 0; }",
    ".msg.agent strong { color: var(--text); font-weight: 650; }",
    ".msg.agent em { font-style: italic; }",
    ".msg.agent del { opacity: .6; }",
    ".msg.agent h1, .msg.agent h2, .msg.agent h3,",
    ".msg.agent h4, .msg.agent h5, .msg.agent h6 { margin: 0 0 6px; font-size: 13px; font-weight: 650; }",
    ".msg.agent ul, .msg.agent ol { margin: 0 0 7px; padding-left: 18px; }",
    ".msg.agent li { margin: 2px 0; }",
    ".msg.agent a { color: var(--blue); }",
    ".msg.agent code {",
    "  font: 11.5px/1.5 'JetBrains Mono', 'SF Mono', ui-monospace, monospace;",
    "  background: rgba(255,255,255,.07); border-radius: 5px; padding: 1px 4px;",
    "}",
    ".msg.agent pre.md-code {",
    "  margin: 0 0 8px; padding: 9px 11px; background: #0c0c13;",
    "  border: 1px solid var(--line); border-radius: 11px;",
    "  overflow-x: auto; max-width: 100%;",
    "}",
    ".msg.agent pre.md-code code { background: none; padding: 0; font-size: 11.5px; }",
    ".msg.agent pre.md-code[data-lang]:not([data-lang=''])::before {",
    "  content: attr(data-lang); display: block; margin-bottom: 5px;",
    "  color: var(--dim); font-size: 10px; letter-spacing: .06em; text-transform: uppercase;",
    "}",
    ".msg.agent hr { border: 0; border-top: 1px solid var(--line); margin: 8px 0; }",
    // model-only suggestion tail (cascade block with no answer around it):
    // one dim line, deliberately quiet — the owner declined chips here.
    ".msg.agent .sug-line { color: var(--dim); font-size: 11.5px; }",
    ".msg.agent .sug-line::before { content: '\\2192\\00a0'; opacity: .7; }",
    // "Open in main app" exit shown after a turn that did real work
    ".om-btn {",
    "  display: block; margin: 4px 0 0 auto; padding: 2px 10px;",
    "  font: 500 11px/1.6 'JetBrains Mono', 'SF Mono', ui-monospace, monospace;",
    "  color: var(--dim); background: none; border: 1px solid var(--line);",
    "  border-radius: 99px; cursor: pointer;",
    "}",
    ".om-btn:hover { color: var(--text); border-color: var(--dim); }",
    ".om-btn[hidden] { display: none; }",
    // "Enter sends" is redundant noise; the loading / ready / error states
    // that share this element are still worth showing.
    ".hint.hint-redundant { display: none; }",
    // conversation reads like a chat: the owner on the right, Accuretta left
    ".msg.user { text-align: right; }",
    // a steer sent to the RUNNING task (from this notch while a turn is live):
    // visually dimmer than a real user turn, with its delivery state.
    ".msg.user.steer { color: #6f6c86; opacity: .85; }",
    ".msg.user.steer.applied { color: var(--ok); opacity: .8; }",
    ".msg.user.steer.deferred { color: rgb(var(--risk)); opacity: .9; }",
    // the tool glyph in the work pill
    ".tool-ico { display: inline-flex; align-items: center; color: var(--dim); flex: none; }",
    ".tool-ico svg { width: 15px; height: 15px; display: block; }",
    // live tool chips inside the conversation card: the main app's icon
    // language, sized for the island — a compact strip of glyphs, no bars.
    // The running glyph breathes blue; finished ones settle dim. The action
    // text lives in the tooltip and in the status line, not in a row label.
    ".turn-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 9px; margin: 5px 0 5px; }",
    ".turn-tools:empty { display: none; }",
    ".turn-tool { display: inline-flex; align-items: center; justify-content: center; color: #6a6784; transition: color .2s ease; }",
    ".turn-tool svg { width: 13px; height: 13px; display: block; }",
    ".turn-tool.running { color: var(--blue); animation: tool-breathe 1.6s ease-in-out infinite; transform-origin: center; }",
    ".turn-tool.done { color: #57546b; }",
    ".turn-tool .tcount { margin-left: 3px; font: 600 10px/1 'SF Pro Text', 'Inter', system-ui, sans-serif; color: inherit; }",
    "@keyframes tool-breathe {",
    "  0%, 100% { opacity: .5; transform: scale(.9); }",
    "  50%      { opacity: 1;  transform: scale(1.08); }",
    "}",
    // Live-event shimmer on the minimized pill: a quick white sweep across the
    // mono one-liner while it works.
    "#workSub.shimmer {",
    "  background-image: linear-gradient(100deg,",
    "    var(--dim) 0%, var(--dim) 34%, #ffffff 50%, var(--dim) 66%, var(--dim) 100%);",
    "  background-size: 260% 100%;",
    "  background-repeat: no-repeat;",
    "  -webkit-background-clip: text; background-clip: text;",
    "  color: transparent; -webkit-text-fill-color: transparent;",
    "  animation: notch-shimmer 1.5s linear infinite;",
    "}",
    // The open card's status line copies the MAIN APP's think-line language:
    // sans, italic, 12.5px, and the accent sweeping through slowly (2.2s)
    // instead of a fast white blink.
    "#liveStatus.shimmer {",
    "  font-family: 'SF Pro Text', 'Inter', system-ui, -apple-system, sans-serif;",
    "  font-size: 12.5px; font-style: italic; font-weight: 500;",
    "  background-image: linear-gradient(90deg,",
    "    var(--dim) 0%, var(--dim) 30%, var(--blue) 50%, var(--dim) 70%, var(--dim) 100%);",
    "  background-size: 250% 100%;",
    "  background-repeat: no-repeat;",
    "  -webkit-background-clip: text; background-clip: text;",
    "  color: transparent; -webkit-text-fill-color: transparent;",
    "  animation: notch-shimmer 2.2s linear infinite;",
    "}",
    // Travel stays inside 0..100% so the 250%-wide background always covers
    // the text: beyond that the uncovered tail went transparent, which read
    // as the line "fading in and out". 100% -> 0% sweeps the highlight
    // left-to-right.
    "@keyframes notch-shimmer {",
    "  from { background-position: 100% 0; }",
    "  to   { background-position: 0% 0; }",
    "}",
    "@media (prefers-reduced-motion: reduce) {",
    "  #workSub.shimmer, #liveStatus.shimmer { animation: none; background-image: none;",
    "    -webkit-text-fill-color: currentColor; color: var(--dim); }",
    "}",
    // ---- dot-matrix pill FX -------------------------------------------------
    // One-shot performances the A plays while it works. Each dot already
    // carries --k (column fraction) and --dx/--dy (center-out vector) from
    // drawLogo; the choreography is just those variables, so the same rules
    // fit every logo size on the island. While an FX runs it overrides the
    // idle twinkle (same specificity, later sheet).
    ".island[data-fx='laser'] .logo circle { animation: fx-laser .45s ease-in-out calc(var(--k) * .6s) 2 both; }",
    ".island[data-fx='sweep'] .logo circle { animation: fx-sweep 1.15s cubic-bezier(.5,0,.4,1) calc(var(--k) * .45s) both; }",
    ".island[data-fx='build'] .logo circle { animation: fx-build .95s cubic-bezier(.3,1.4,.5,1) calc((1 - var(--k)) * .5s) both; }",
    ".island[data-fx='break'] .logo circle { animation: fx-break 1.4s cubic-bezier(.4,0,.4,1) calc(var(--k) * .3s) both; }",
    "@keyframes fx-laser {",
    "  0%, 100% { transform: none; filter: none; }",
    "  45% { transform: scale(1.9); filter: brightness(2.3) saturate(1.5); }",
    "}",
    "@keyframes fx-sweep {",
    "  0%, 100% { transform: none; opacity: 1; }",
    "  55% { transform: translate(-15px, 11px) rotate(38deg); opacity: .12; }",
    "}",
    "@keyframes fx-build {",
    "  0% { transform: translate(var(--dx), calc(var(--dy) + 24px)) scale(.35); opacity: 0; }",
    "  100% { transform: none; opacity: 1; }",
    "}",
    // the send-off: scatter along each dot's center-out vector, fall, rebuild
    "@keyframes fx-break {",
    "  0%, 100% { transform: none; opacity: 1; }",
    "  30% { transform: translate(var(--dx), var(--dy)) scale(.8); opacity: .9; }",
    "  58% { transform: translate(calc(var(--dx) * .5), 42px) rotate(24deg); opacity: .12; }",
    "  80% { transform: translate(calc(var(--dx) * -.25), -6px) rotate(-8deg); opacity: .8; }",
    "}",
    // The wide beam that rides the pill while an FX runs is a GRID item
    // spanning the spacer + right columns (everything right of the status
    // text), so its travel lane is pinned to the layout: swapping the tool
    // label for a longer/shorter one never shifts the streak horizontally.
    // The three PRIMARY children carry explicit placements because a grid
    // child with a definite row makes the auto-placement cursor SKIP the
    // occupied cells, wrapping the following items into an implicit second
    // row — that was the old dropped right-side text.
    ".v-work .pill { position: relative; }",
    // The right column is icon-width, so the slack goes to the LEFT — the
    // phase word + literal call need real estate and must not ellipsize to
    // "thin…". The 190px middle track is the beam's reserved lane.
    ".v-work .pill.wings { grid-template-columns: minmax(0, 1fr) 190px auto; }",
    ".v-work .pill.wings .wl { grid-column: 1; grid-row: 1; }",
    ".v-work .pill.wings > span:not(.fx-beam) { grid-column: 2; grid-row: 1; }",
    ".v-work .pill.wings .wr { grid-column: 3; grid-row: 1; }",
    ".fx-beam { grid-column: 2 / -1; grid-row: 1; align-self: center; position: relative; height: 2px; pointer-events: none; opacity: 0; }",
    ".fx-beam::before { content: ''; position: absolute; top: 0; bottom: 0; left: 0; width: 30%; border-radius: 2px; }",
    ".fx-beam.run { opacity: 1; }",
    ".fx-beam.run::before { animation: fx-beam-run .9s ease-in-out both; }",
    ".fx-beam[data-kind='laser']::before {",
    "  background: linear-gradient(90deg, transparent, #7cc4ff 45%, #eaf6ff 50%, #7cc4ff 55%, transparent);",
    "  box-shadow: 0 0 12px 2px rgba(110,180,255,.55);",
    "}",
    ".fx-beam[data-kind='sweep']::before {",
    "  height: 10px; top: -4px; filter: blur(2px);",
    "  background: linear-gradient(90deg, transparent, rgba(255,170,80,.5), rgba(255,220,160,.75), rgba(255,170,80,.5), transparent);",
    "}",
    ".fx-beam[data-kind='build']::before {",
    "  background: linear-gradient(90deg, transparent, rgba(120,255,180,.5), rgba(190,255,220,.8), rgba(120,255,180,.5), transparent);",
    "  box-shadow: 0 0 10px 2px rgba(120,255,180,.4);",
    "}",
    ".fx-beam[data-kind='break']::before {",
    "  background: linear-gradient(90deg, transparent, rgba(255,120,140,.5), rgba(255,190,200,.8), rgba(255,120,140,.5), transparent);",
    "  box-shadow: 0 0 10px 2px rgba(255,120,140,.4);",
    "}",
    "@keyframes fx-beam-run {",
    "  from { transform: translateX(0); }",
    "  to { transform: translateX(360%); }",
    "}",
    "@media (prefers-reduced-motion: reduce) {",
    "  .island[data-fx] .logo circle { animation: none !important; }",
    "  .fx-beam { display: none; }",
    "}",
    // leaving: the island retreats up into the bezel instead of blinking out
    ".island.is-leaving {",
    "  transform: translateX(-50%) translateY(-104%);",
    "  opacity: 0;",
    "  transition: transform .3s cubic-bezier(.4, 0, .2, 1), opacity .26s ease-in;",
    "}",
    ".island.is-entering {",
    "  transform: translateX(-50%) translateY(-104%); opacity: 0;",
    "  transition: none;",
    "}",
    // ---- scrollbars: the WebView2 default is a light, arrow-buttoned bar that
    // clashes with the island. Theme every scrollable inside it (thread, model
    // menu, code blocks) to a quiet dark pill.
    ".island { color-scheme: dark; }",
    ".island ::-webkit-scrollbar { width: 9px; height: 9px; }",
    ".island ::-webkit-scrollbar-track, .island ::-webkit-scrollbar-corner { background: transparent; }",
    ".island ::-webkit-scrollbar-button { display: none; width: 0; height: 0; }",
    ".island ::-webkit-scrollbar-thumb {",
    "  background-color: rgba(255,255,255,.13); border: 2px solid rgba(0,0,0,0);",
    "  border-radius: 99px; background-clip: padding-box;",
    "}",
    ".island ::-webkit-scrollbar-thumb:hover { background-color: rgba(255,255,255,.24); }",
    // The reply gets the FULL width of the card (600px, a step past the
    // 540px pills so a reading reply has room) and grows vertically to fit
    // the answer, rather than the old fixed 560x318 box or a shrink-to-fit
    // sliver.
    ".island[data-state='prompt'] .view.v-prompt { position: relative; inset: auto; }",
    ".island[data-state='prompt'] {",
    "  width: 600px;",
    "  height: auto; max-height: 460px;",
    "}",
    ".island[data-state='prompt'][data-thread] { height: auto; }",
    ".island[data-state='prompt'] .card { height: auto; }",
    // expanded: the full notch conversation gets headroom to actually read
    ".island[data-state='prompt'][data-expanded] { max-height: 640px; }",
    // a quiet divider separates the archive from the live turn below it
    ".island[data-expanded] #userMsg { border-top: 1px solid var(--line); margin-top: 6px; padding-top: 8px; }",
    ".msg.arch.user { color: #6f6c86; }",
    // live status line shown at the bottom of the thread while a notch turn
    // is streaming and the owner is looking at the conversation view
    "#liveStatus {",
    "  display: none; font: 11.5px/1.5 'JetBrains Mono', 'SF Mono', ui-monospace, monospace;",
    "  color: var(--dim); padding: 4px 0 6px;",
    // One line, like the pill: a monster path must ellipsize, not wrap the card.
    "  max-width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;",
    "}",
    // the approved-task panel: what the agent is cleared to do, per kind
    ".task-panel { display: flex; flex-direction: column; gap: 9px; }",
    // The approved-task panel shares #thread with the conversation. Hide the
    // conversation (and the live tool rows) while it is open instead of
    // removing anything from the DOM.
    ".island[data-panel] .msg, .island[data-panel] .turn-tools { display: none; }",
    ".task-row {",
    "  border: 1px solid var(--line); border-radius: 11px; padding: 8px 10px;",
    "  background: #0c0c13;",
    "}",
    ".task-head { display: flex; align-items: center; gap: 7px; margin-bottom: 5px; }",
    ".task-ico { display: inline-flex; color: var(--dim); }",
    ".task-ico svg { width: 13px; height: 13px; display: block; }",
    ".task-kind { font-size: 12px; font-weight: 600; color: var(--text); }",
    ".task-verdict {",
    "  margin-left: auto; font-size: 10.5px; letter-spacing: .04em;",
    "  text-transform: uppercase; color: var(--dim);",
    "}",
    ".task-verdict.approve { color: var(--ok); }",
    ".task-verdict.deny { color: var(--bad); }",
    ".task-row.tone-warn .task-ico { color: var(--amber); }",
    ".task-row.tone-warn .task-kind { color: var(--amber); }",
    ".task-row.tone-write .task-ico, .task-row.tone-write .task-kind { color: var(--blue); }",
    ".task-cmd {",
    "  display: block; font: 11.5px/1.55 'JetBrains Mono', 'SF Mono', ui-monospace, monospace;",
    "  color: #cfd0ff; white-space: pre-wrap; word-break: break-word;",
    "}",
    ".task-meta { margin-top: 5px; font-size: 11px; color: var(--dim); }",
    ".dot-sep { margin: 0 6px; opacity: .5; }",
    // Keep the reply scrollable INSIDE the card so the prompt box and model
    // row always stay visible under a long answer.
    ".island[data-state='prompt'] .thread {",
    "  max-height: 296px; overflow-y: auto; -webkit-mask-image: none; mask-image: none;",
    "}",
    ".island[data-state='prompt'][data-thread] .thread { margin-bottom: 12px; }",
    // a long model list scrolls inside the dropdown instead of being cut off
    ".island[data-state='prompt'] .menu {",
    "  max-height: 232px; overflow-y: auto; overscroll-behavior: contain;",
    "}",
    ".island[data-state='prompt'] .menu::-webkit-scrollbar { width: 6px; }",
    ".island[data-state='prompt'] .menu::-webkit-scrollbar-thumb {",
    "  background: rgba(255,255,255,.16); border-radius: 3px;",
    "}",
  ].join("\n");
  document.head.appendChild(style);
})();

// The shipped model glyph is a CPU. A language model reads better as a small
// neural graph, so swap it for one (same slot, same size).
(function neuralModelChip() {
  const chip = document.getElementById("model");
  if (!chip) return;
  const svg = chip.querySelector("svg");
  if (!svg) return;
  svg.outerHTML =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="6" cy="7" r="2.1"/><circle cx="6" cy="17" r="2.1"/><circle cx="18" cy="12" r="2.1"/>' +
    '<path d="M8.1 7h3.2a2 2 0 0 1 2 2v.9M8.1 17h3.2a2 2 0 0 0 2-2v-.9"/>' +
    '<path d="M13.4 10.2 16 11M13.4 13.8 16 13"/></svg>';
})();

// The widget restores "Enter sends" after every model load, so hide that one
// value rather than the element.
(function suppressRedundantHint() {
  const hint = document.getElementById("hint");
  if (!hint) return;
  const apply = () => {
    hint.classList.toggle("hint-redundant", (hint.textContent || "").trim() === "Enter sends");
  };
  apply();
  new MutationObserver(apply).observe(hint, { childList: true, characterData: true, subtree: true });
})();

// ---- input diagnostics ------------------------------------------------------
// The host logs these. They answer "where did the input stop?" without ever
// touching the approved UI: window focus, DOM focus, whether keydown/pointer
// events arrive at all, and whether page timers are actually running.
const diag = { keys: 0, pointers: 0, lastKey: "", lastPointer: "" };
// One report per burst, not one per event. A keystroke used to run every
// layout read below, serialize the whole object across the WebView2 channel,
// and (host-side) open+wrote+closed the log file — per key. That was the
// typing lag. The counters keep incrementing, so a throttled report still
// proves input arrives; only the per-event cost is gone.
const _diagLastAt = {};
function reportDiag(what) {
  const now = Date.now();
  const minGap = what === "keydown" ? 900 : what === "pointerdown" ? 400 : 0;
  if (minGap && now - (_diagLastAt[what] || 0) < minGap) return;
  _diagLastAt[what] = now;
  const el = document.activeElement;
  const q = document.getElementById("q");
  const hintEl = document.getElementById("hint");
  hostPost({
    type: "diag", what,
    hasFocus: document.hasFocus(),
    visibility: document.visibilityState,
    active: el ? (el.id || el.tagName || "?") : "none",
    qlen: q ? q.value.length : -1,
    hint: hintEl ? (hintEl.textContent || "").trim() : "",
    hintShown: !!hintEl && hintEl.offsetParent !== null,
    keys: diag.keys, lastKey: diag.lastKey,
    pointers: diag.pointers, lastPointer: diag.lastPointer,
    toolIcon: !!document.querySelector(".v-work .tool-ico svg"),
    orb: false,
    planningHidden: (function () {
      const w = document.getElementById("workSub");
      return !!w && w.style.display === "none";
    })(),
    chipNeural: (function () {
      const chip = document.getElementById("model");
      if (!chip) return false;
      const svg = chip.querySelector("svg");
      return !!svg && svg.innerHTML.indexOf("circle") !== -1;
    })(),
    menuScrolls: (function () {
      const m = document.getElementById("menu");
      if (!m) return false;
      const cs = getComputedStyle(m);
      return cs.overflowY === "auto" || cs.overflowY === "scroll";
    })(),
    taskPanel: !!document.getElementById("taskPanel"),
    taskText: (document.getElementById("taskPanel") || {}).textContent || "",
    tool: (document.getElementById("workSub") || {}).textContent || "",
    userAlign: (function () {
      const u = document.getElementById("userMsg");
      return u ? getComputedStyle(u).textAlign : "";
    })(),
    inputVisible: (function () {
      // is the prompt box inside the visible card, or pushed out of view?
      const box = document.querySelector(".v-prompt .promptbox");
      if (!box) return null;
      const b = box.getBoundingClientRect();
      const i = island.getBoundingClientRect();
      return b.height > 0 && b.bottom <= i.bottom + 1 && b.top >= i.top - 1;
    })(),
    cardW: Math.round(island.getBoundingClientRect().width),
    cardH: Math.round(island.getBoundingClientRect().height),
    // How much answer is actually on screen. A reply card at the right height
    // with nothing in it is what a lost turn looks like, and the geometry
    // alone cannot tell the two apart.
    replyLen: (document.getElementById("agentMsg") || {}).textContent
      ? document.getElementById("agentMsg").textContent.trim().length : 0,
    streaming,
    state: state(),
  });
}
window.addEventListener("focus", () => reportDiag("window-focus"));
window.addEventListener("blur", () => reportDiag("window-blur"));
document.addEventListener("focusin", () => reportDiag("focusin"));
document.addEventListener("pointerdown", (e) => {
  diag.pointers++;
  const el = e.target.closest && e.target.closest("[data-act]");
  diag.lastPointer = (el && el.dataset.act) || (e.target.id || e.target.tagName || "?");
  reportDiag("pointerdown");
}, true);
document.addEventListener("keydown", (e) => {
  diag.keys++;
  diag.lastKey = e.key;
  reportDiag("keydown");
}, true);
// Proves page timers actually fire (hold-to-approve, focus timers, glow).
hostPost({ type: "diag", what: "boot", timer: typeof setTimeout, raf: typeof requestAnimationFrame });
setTimeout(() => hostPost({ type: "diag", what: "timer-300ms-ok" }), 300);
island.addEventListener("transitionend", () => { island.classList.add("settled"); reportRect(); });
island.addEventListener("transitionstart", () => { island.classList.remove("settled"); reportRect(); });
new MutationObserver(() => {
  // The widget focuses the input 350ms after the card opens; anyone typing at
  // normal speed loses those first characters. Focus it the moment the card
  // appears instead, so not one keystroke is dropped.
  const st = state();
  if (st === "prompt" && lastObservedState !== "prompt") {
    const q = document.getElementById("q");
    if (q) q.focus();
  }
  lastObservedState = st;
  reportRect();
}).observe(island, { attributes: true, attributeFilter: ["data-state", "data-menu", "data-thread"] });
window.addEventListener("resize", reportRect);

// The work pill collapses the action to a truncated line; a click on it (or on
// the idle pill while work is in flight) reveals what was actually cleared.
// Registered in the capture phase so it runs before the widget's own handler,
// which would otherwise jump straight to the chat card.
document.addEventListener("click", (e) => {
  if (!e.target.closest || !e.target.closest("#island")) return;
  const pill = e.target.closest(".v-work, .v-idle");
  if (!pill) return;
  const busy = state() === "work" || state() === "idle";
  if (!busy) return;
  if (e.target.closest("[data-act]")) return;      // let real controls win
  toggleTaskPanel();
  e.stopPropagation();
  e.preventDefault();
}, true);

// Host -> page messages arrive on window.chrome.webview in WebView2 (a plain
// `window` "message" listener never sees them). Listen there, with the window
// listener kept as a fallback.
function onHostMessage(e) {
  const d = e.data;
  if (d && d.type === "refresh-rect") {
    // The host revealed the island; a hidden page sends no rAF frames, so
    // re-report the geometry now.
    reportRect();
    return;
  }
  if (d && d.type === "collapse") {
    // Ctrl+Alt+Space: shrink to the minimal pill so the widget's own spring
    // animation plays, then the host tucks the window away.
    if (state() !== "idle") notch.setState("idle");
    return;
  }
  if (d && d.type === "focus-input") {
    const q = document.getElementById("q");
    if (q && state() === "prompt") q.focus();
    return;
  }
  if (d && d.type === "leave") {
    // Slide up into the bezel; the host hides the window when this settles.
    island.classList.remove("is-entering");
    void island.offsetWidth;
    island.classList.add("is-leaving");
    reportRect();
    return;
  }
  if (d && d.type === "enter") {
    // Come back down from the bezel.
    island.classList.remove("is-leaving");
    island.classList.add("is-entering");
    void island.offsetWidth;
    requestAnimationFrame(() => {
      island.classList.remove("is-entering");
      // An approval that was hidden by the hotkey is still pending: show it
      // again rather than a bare idle pill. Same for a turn that is still
      // running on any surface — idle above live work reads as "done".
      if (pending.size && state() === "idle") notch.setState("alert");
      else if (turnLive() && state() === "idle") notch.setState("work");
      reportRect();
    });
    return;
  }
  if (d && d.type === "outside-click") {
    // The host saw a click land outside the island while a card was open.
    // Mirror the widget's own Esc handling: gate -> alert, prompt -> alert /
    // work while a turn is still running (the empty idle dot used to flash
    // for a beat before the next tool_start pulled the work pill back).
    const s = state();
    if (s === "gate") notch.setState("alert");
    else if (s === "prompt") notch.setState(pending.size ? "alert" : (turnLive() ? "work" : "idle"));
  }
}
if (window.chrome && window.chrome.webview && window.chrome.webview.addEventListener) {
  window.chrome.webview.addEventListener("message", onHostMessage);
} else {
  window.addEventListener("message", onHostMessage);
}

// The host window is region-clipped to the rect this page reports. The
// transition loop above covered the spring resize, but CONTENT growth fires
// no attribute change and no transition: a long streaming reply grew the card
// under a stale window region and the underside stayed visibly cut off until
// the turn ended. Observe the actual box instead of guessing when size moves.
if (typeof ResizeObserver !== "undefined") {
  new ResizeObserver(() => reportRect()).observe(island);
}

// ---- full conversation expansion --------------------------------------------
// The live thread only ever shows the current turn: beginReply() rewrites the
// same two elements every turn, so earlier notch turns are invisible until the
// island minimizes and comes back empty-looking. Double-clicking the empty
// space of the thread pulls the whole chat from the bridge and lays it out
// above the live pair; double-click again to fold back.
let threadExpanded = false;

const msgText = (content) => {
  if (Array.isArray(content)) {
    return content.filter(p => p && typeof p === "object" && typeof p.text === "string")
      .map(p => p.text).join("");
  }
  return String(content == null ? "" : content);
};

const archBlock = (role, raw) => {
  const div = document.createElement("div");
  div.className = "msg arch " + role;
  const body = document.createElement("div");
  if (role === "assistant") {
    body.innerHTML = notchMarkdown
      ? notchMarkdown.render(visibleFinalText(raw))
      : escHtml(visibleFinalText(raw));
  } else {
    body.textContent = msgText(raw);
  }
  div.appendChild(body);
  return div;
};

async function expandThread() {
  const thread = document.getElementById("thread");
  if (!thread || threadExpanded) return;
  threadExpanded = true;              // set first: a second dblclick folds back
  island.setAttribute("data-expanded", "");
  try {
    const chat = notchChatId
      ? await getJson(`/api/chats/${encodeURIComponent(notchChatId)}`) : null;
    const msgs = (chat && Array.isArray(chat.messages)) ? chat.messages : [];
    const past = msgs.filter(m => (m.role === "user" || m.role === "assistant") && !m._internal);
    // The live pair is already on screen. While any turn streams (this
    // surface's POST, or a mirrored foreign turn), the reply is not persisted
    // yet — only the trailing user is ours; once it ended, the trailing
    // assistant is ours too.
    const tail = turnLive() ? 1 : 2;
    const archive = past.slice(0, Math.max(0, past.length - tail)).slice(-80);
    if (archive.length) {
      const frag = document.createDocumentFragment();
      for (const m of archive) frag.appendChild(archBlock(m.role, m.content));
      thread.insertBefore(frag, thread.firstChild);
    }
  } catch (e) {}
  thread.scrollTop = 1e9;
  thread.title = "Double-click to fold back to the latest turn";
  reportRect();
}

function collapseThread() {
  if (!threadExpanded) return;
  threadExpanded = false;
  island.removeAttribute("data-expanded");
  for (const el of document.querySelectorAll("#thread .msg.arch")) el.remove();
  const thread = document.getElementById("thread");
  if (thread) {
    thread.scrollTop = 1e9;
    thread.title = "Double-click to show the full conversation";
  }
  reportRect();
}

(function wireThreadDblClick() {
  const thread = document.getElementById("thread");
  if (!thread) return;
  thread.title = "Double-click to show the full conversation";
  thread.addEventListener("dblclick", (e) => {
    // Only the empty space counts: selecting text, the live messages, the
    // task panel and the exit button keep their normal double-click behavior.
    if (e.target.closest(".msg, .task-panel, .om-btn, code, pre")) return;
    if (threadExpanded) collapseThread(); else expandThread();
  });
})();

// ---- gates ------------------------------------------------------------------
// Diff preview rows for the gate card. write_file gates carry ready-made
// [op, text] rows from the bridge; edit_file gates carry a text preview of
// "- old" / "+ new" lines, parsed here so both render the same way.
const previewToDiff = (preview) => {
  const rows = [];
  for (const line of String(preview || "").split("\n")) {
    const m = /^\s*([+-])\s?(.+)$/.exec(line);
    if (m) rows.push([m[1], m[2]]);
  }
  return rows;
};

function gateFromApproval(a) {
  const d = a.details || {};
  const kind = d.kind === "registry" ? "registry" : "command";
  const rows = Array.isArray(d.diff)
    ? d.diff.map((r) => [String(r && r[0] === "-" ? "-" : "+"), String(r && r[1] != null ? r[1] : "")])
    : previewToDiff(d.preview);
  const g = {
    id: a.id,
    cmd: String(a.command || a.title || ""),
    kind,
    // The action type as the permission layer classified it (delete, write,
    // powershell, registry...). The widget keeps unknown gate fields and hands
    // them back on the approve/deny event, so this survives the round trip.
    toolKind: String(d.kind || ""),
    scope: String(d.scope || d.target || d.kind || "host"),
    step: String(d.step || ""),
    writes: kind === "registry" ? "registry" : WRITE_KINDS.has(d.kind) ? "files" : d.kind === "delete" ? "delete" : "none",
    target: Array.isArray(d.targets) && d.targets.length ? String(d.targets[0]) : String(d.target_host || ""),
  };
  if (rows.length) {
    g.diff = rows;
    if (d.path) g.path = String(d.path);
    if (d.diff_counts) g.counts = d.diff_counts;
  }
  return g;
}

async function onApprovalNew(a) {
  if (!a || !a.id || pending.has(a.id)) return;
  pending.set(a.id, a);
  const g = gateFromApproval(a);
  if (g.kind === "registry") {
    // Best-effort read-only diff. Never runs anything that writes; on any
    // ambiguity the gate stays a hold-to-approve registry card without rows.
    try {
      const r = await api("/api/notch/reg-query", { command: g.cmd });
      if (r.ok) {
        const info = await r.json();
        if (info && info.path) g.path = String(info.path);
        if (info && Array.isArray(info.diff) && info.diff.length) g.diff = info.diff;
      }
    } catch (e) {}
  }
  a._gate = g;
  notch.addGate(g);
  notify("gate");
}

function onApprovalDecided(id) {
  pending.delete(id);
  notch.removeGate(id); // no-op when the widget itself resolved it
}

// ---- models --------------------------------------------------------------------
const prettyName = (p) => String(p || "").replace(/\.gguf$/i, "");
const metaFor = (name) => /vl|vision|llava|minicp|pixtral|qwen2-vl/i.test(name) ? "vision"
  : /a\d+b|moe|mixtral/i.test(name) ? "MoE" : "dense";

async function refreshModels() {
  const r = await getJson("/api/models");
  if (!r) return;
  modelList = (r.models || []).map(m => ({ name: prettyName(m.name), meta: metaFor(m.name), path: m.path, loaded: !!m.loaded }));
  let idx = modelList.findIndex(m => m.loaded);
  if (idx < 0) idx = 0;
  activeModelIdx = idx;
  notch.setModels(modelList.map(({ name, meta }) => ({ name, meta })), idx);
  if (pendingLoadPath) {
    const hit = modelList.find(m => m.path === pendingLoadPath && m.loaded);
    if (hit && r.llama_running) { pendingLoadPath = ""; notch.modelReady(); }
  }
}

async function onModelPicked(name) {
  const prevIdx = activeModelIdx;
  const target = modelList.find(m => m.name === name);
  if (!target) { notch.modelFailed("unknown model"); return; }
  pendingLoadPath = target.path;
  const revert = () => notch.setModels(modelList.map(({ name, meta }) => ({ name, meta })), prevIdx);
  try {
    const resp = await api("/api/models/load", { path: target.path });
    const res = await resp.json().catch(() => ({}));
    if (resp.ok && res && res.ok) {
      pendingLoadPath = "";
      await refreshModels();
      notch.modelReady();
      notify("model");
    } else {
      pendingLoadPath = "";
      notch.modelFailed(String((res && res.error) || `load failed (${resp.status})`).slice(0, 80));
      revert();
    }
  } catch (e) {
    pendingLoadPath = "";
    notch.modelFailed("load failed");
    revert();
  }
}

// ---- chat (send -> streamed reply) -----------------------------------------------
function makeThinkStripper() {
  let buf = "", inside = false;
  return (piece) => {
    buf += piece;
    let out = "";
    for (;;) {
      if (!inside) {
        const i = buf.indexOf("<think>");
        if (i < 0) { out += buf; buf = ""; break; }
        out += buf.slice(0, i); buf = buf.slice(i + 7); inside = true;
      } else {
        const j = buf.indexOf("</think>");
        if (j < 0) { buf = ""; break; }
        buf = buf.slice(j + 8); inside = false;
      }
    }
    return out;
  };
}

// One-shot: mirror the bridge's _visible_assistant_text so a turn the main
// app can render never dies here as "no readable reply". The running stripper
// handles the ordinary <think>…</think> pair; the hard case is a model that
// opens <think> and never closes it, then writes the answer after a bare
// `response` marker (or tool-call markup) INSIDE the block. The old regex
// deleted everything from <think> to end-of-string, so the bridge's `final`
// — which it considered a perfectly good answer — landed as an empty card.
const visibleFinalText = (raw) => {
  let s = String(raw == null ? "" : raw);
  // A bare `response` line is the answer boundary; the text after the LAST
  // one is the whole visible reply. Checked first, exactly like the bridge.
  const bounds = [...s.matchAll(/(?:^|\n)[ \t]*response(?=[A-Za-z0-9<`])/gi)];
  if (bounds.length) {
    const b = bounds[bounds.length - 1];
    s = s.slice(b.index + b[0].length);
  } else {
    const closes = [...s.matchAll(
      /<\/(?:think|thinking|reasoning)>|<\|\/thinking\|>|\[\/(?:thought|thinking|reasoning|scratchpad)\]/gi)];
    if (closes.length) {
      const c = closes[closes.length - 1];
      s = s.slice(c.index + c[0].length);
    } else {
      const opener = /<(?:think|thinking|reasoning)>|<\|thinking\|>|\[(?:thought|thinking|reasoning|scratchpad)\]/i.exec(s);
      if (opener) {
        // Unclosed block: keep what came before it, then salvage the tail if
        // the model rolled straight into output inside the block.
        let visible = s.slice(0, opener.index);
        const thinking = s.slice(opener.index);
        const implicit = /<\/?tool_call>|<\|tool_call>|<call:[a-zA-Z0-9_\-]+>|\[TOOL_CALLS\]|```(?:tool_call|tool_code)/i.exec(thinking);
        const resp = /\n\s*response(?:[ \t]*\r?\n)?/i.exec(thinking);
        if (implicit && implicit.index > 0 && (!resp || implicit.index < resp.index)) {
          visible += thinking.slice(implicit.index).replace(/<\/?tool_call>/gi, "");
        } else if (resp && resp.index > 0) {
          visible += thinking.slice(resp.index + resp[0].length);
        }
        s = visible;
      }
    }
  }
  s = s.replace(
    /<\/?(?:think|thinking|reasoning)>|<\|\/?thinking\|>|\[\/?(?:thought|thinking|reasoning|scratchpad)\]/gi, "");
  // The bridge splices a bare `" response"` marker into the persisted text
  // when content follows a reasoning stream. Strip it only when it is glued
  // to the answer — never eat a real sentence that starts with "Response".
  s = s.replace(/^\s*response(?=[A-Za-z0-9<`])/i, "");
  return stripCascade(s).trim();
};

// The system prompt asks the model to end short replies with a
// `<cascade>["suggestion"]</cascade>` block, and the bridge counts that block
// as the visible reply — but the stripper deletes it here. A turn whose whole
// output was suggestions (common in rapid short-answer exchanges with a small
// local model) then died as "no readable reply". Pull the strings out; loose
// string-literal scanning, not JSON.parse, because a token-limit cut often
// leaves the block truncated mid-array.
const cascadeSuggestions = (raw) => {
  const m = /(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*)/i.exec(String(raw == null ? "" : raw));
  if (!m) return [];
  return [...m[1].matchAll(/"((?:[^"\\]|\\.){1,120})"/g)]
    .map(x => x[1].replace(/\\"/g, '"').replace(/\\\\/g, "\\"))
    .filter(t => t.trim());
};

// The bridge streams its `<cascade>["…","…"]</cascade>` prompt-suggestion block
// as ordinary deltas and the main app turns it into suggestion chips. Nothing
// in the notch did, so raw JSON was landing at the end of every reply.
function stripCascade(s) {
  return String(s == null ? "" : s)
    .replace(/(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*?)(?:<|&lt;|\\<)\/cascade(?:>|&gt;|\\>)/gi, "")
    .replace(/(?:<|&lt;|\\<)cascade[\s\S]*$/i, "")
    .replace(/\\u003c/g, "<");
}

// The widget appends plain text spans; model output is markdown, so render
// the accumulated reply ourselves and keep the fade on the newest words.
// Shared by the notch's own POST stream and the SSE mirror of a turn another
// surface started — one paint path for both.
function renderReply() {
  const el = document.getElementById("agentMsg");
  if (!el || !notchMarkdown) return;
  el.innerHTML = notchMarkdown.render(replyRaw);
  wrapNewWords(el);
  const thread = document.getElementById("thread");
  if (thread) thread.scrollTop = 1e9;
}

// Fade ONLY the text that appeared since the previous paint. The old code
// re-wrapped the last few words on every 45ms flush, and replacing those
// nodes restarted their CSS animation from opacity 0 each time — the visible
// streaming flicker. `fadedChars` is a plain-text cursor: text before it was
// already animated (and now renders at full opacity), text after it gets the
// one-shot fade. Code blocks are never wrapped.
function wrapNewWords(el) {
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (node.parentElement && node.parentElement.closest("pre"))
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = [];
  let total = 0;
  while (walker.nextNode()) {
    nodes.push(walker.currentNode);
    total += walker.currentNode.nodeValue.length;
  }
  if (total <= fadedChars) { fadedChars = total; return; }
  let acc = 0;
  for (const node of nodes) {
    const len = node.nodeValue.length;
    if (acc + len <= fadedChars) { acc += len; continue; }
    const off = Math.max(0, fadedChars - acc);
    const tail = node.nodeValue.slice(off);
    if (tail) {
      const span = document.createElement("span");
      span.className = "w";
      span.textContent = tail;
      if (off > 0) {
        node.nodeValue = node.nodeValue.slice(0, off);
        node.parentNode.insertBefore(span, node.nextSibling);
      } else {
        node.parentNode.replaceChild(span, node);
      }
    }
    acc += len;
  }
  fadedChars = total;
}

function ingestWords(text) {
  const clean = stripCascade(stripThink(text));
  if (!clean) return;
  wordBuf += clean;
  const now = performance.now();
  // Paint on a cadence, never mid-buffer: the whole pending tail moves into
  // the reply at once. Flushing PARTIAL buffers glued words together — token
  // deltas arrive word-split ("C" |"leaning…"), so a space-boundary flush
  // would hold "C" and glue it onto the next delta as "CCleaning". 45ms ≈ two
  // frames: the reply reads as live typing, not as one late block.
  if (now - lastFlush > 45) {
    replyRaw += wordBuf;
    wordBuf = "";
    lastFlush = now;
    markRowsDone();            // the model is answering: no tool is running
    renderReply();
    // words are coming out: the orb switches to the composing pose
    setActivity("composing");
  }
}

async function runTurn(prompt) {
  if (streaming) return;
  streaming = true;
  island.setAttribute("data-turn", "live");
  // The approved-task panel shares the thread with the conversation, so the
  // prompt the owner just typed has to come back into view before the reply
  // lands in it.
  closeTaskPanel();
  // Own the thinking state here rather than waiting for the SSE `chat_start`:
  // the pill must say so the moment the owner hits Enter, and it must still do
  // so if the event stream is down.
  setActivity("thinking");
  const turnStartedAt = Date.now();
  wordBuf = ""; replyRaw = ""; fadedChars = 0; lastFlush = 0; replyOpen = false;
  handledCalls.clear();
  clearTurnRows();
  let replyIsSugTail = false;   // the turn's only output was a cascade block
  const openReply = () => {
    if (!replyOpen) {
      // A fresh turn clears the previous turn's "Open in main app" exit.
      const om = document.getElementById("openMain");
      if (om) om.hidden = true;
      notch.beginReply(prompt);
      replyOpen = true;
      // The card just became the visible surface: show the running status
      // under the (still empty) reply immediately, not at the first word.
      updateLiveStatus();
    }
  };

  // The widget appends plain text spans; model output is markdown, so render
  // the accumulated reply ourselves and keep the fade on the newest words.
  // renderReply()/ingestWords() live at module scope so the SSE mirror of a
  // turn started on another surface paints through the exact same pipeline.

  // Echo the prompt and open the card before anything streams back. Waiting
  // for the first content delta left the thinking/tool phase invisible — a
  // click during that window opened the previous turn's thread and looked
  // like the request went nowhere.
  openReply();

  const pushWords = (text) => {
    openReply();
    ingestWords(text);
  };
  // Nothing readable came out of this turn. That is NOT a successful turn: the
// island used to collapse straight back to the idle dot, which is
// indistinguishable from never having asked - the reply was simply lost with no
// card and no error. Keep the card open and say what happened instead.
  const finish = (why) => {
    if (wordBuf) { openReply(); replyRaw += wordBuf; wordBuf = ""; }
    let cardIsError = !!why;
    if (!replyRaw.trim()) {
      openReply();
      replyRaw = why || "The agent finished the turn without a readable reply. "
        + "Nothing was changed. Try again, or check the agent log.";
      cardIsError = true;
      renderReply();
    } else if (replyOpen) {
      renderReply();
    }
    // chat_end re-renders (wiping any class the final handler added), so the
    // minimal suggestion styling is applied here, after the last render.
    if (replyIsSugTail) {
      const p = document.querySelector("#agentMsg p:last-of-type");
      if (p) p.classList.add("sug-line");
    }
    updateLiveStatus();
    if (replyOpen) {
      // A turn that actually touched the machine gets a send-off: the A breaks
      // down and rebuilds, and the exit to the full surface appears — that is
      // where the heavy follow-up belongs.
      const didWork = lastToolStartAt >= turnStartedAt;
      if (didWork) {
        pillFX("break");
        showOpenMain();
      }
      // A failed turn must surface even if the owner folded the island away:
      // the reply card only shows when the island opens, so the notification
      // is what tells an away-owner the request did not go through.
      if (cardIsError) {
        notify("error");
        // The error text lives INSIDE the reply card. If the owner is staring
        // at the pill (or the pill was auto-revealed by the notify above),
        // they watched a stuck "thinking" state with the reason hidden —
        // "then nothing". Open the card so the failure is READ, not felt.
        if (state() !== "prompt") notch.setState("prompt");
      } else if (replyRaw.trim()) {
        // Asked here, answered here: a successful notch-sent turn pops the
        // reply card open even if the owner folded it to the pill mid-run.
        // Gates/alert/off own the screen and are left alone.
        const s = state();
        if (s === "idle" || s === "work" || s === "ready") notch.setState("prompt");
      }
      notch.endReply();
    }
    streaming = false;
    handledCalls.clear();
    updateComposerAction();          // live just ended: stop -> send
    if (!mirror.active) island.removeAttribute("data-turn");
    // A turn that ENDED (ok or error) must never leave the work pill frozen:
    // the error path in particular carries no chat_end at all.
    if (state() === "work" && !turnLive()) notch.setState("idle");
  };
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Accuretta-Token": TOKEN },
      body: JSON.stringify({ chat_id: notchChatId || undefined, message: prompt, mode: "agent", surface: "notch" }),
    });
    if (!resp.ok || !resp.body) {
      let msg = `request failed (${resp.status})`;
      try { const j = await resp.json(); if (j && j.error) msg = String(j.error); } catch (e) {}
      openReply();
      replyRaw += msg;
      renderReply();
      notch.endReply();
      // Same visibility rule as the stream-failure path: the owner must READ
      // why the turn died, with the card open, never a frozen thinking pill.
      if (state() !== "prompt") notch.setState("prompt");
      streaming = false;
      handledCalls.clear();
      updateComposerAction();        // live just ended: stop -> send
      if (!mirror.active) island.removeAttribute("data-turn");
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let carry = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      carry += dec.decode(value, { stream: true });
      const chunks = carry.split(/\n\n/);
      carry = chunks.pop();
      for (const chunk of chunks) {
        const line = chunk.split("\n").find(l => l.startsWith("data: "));
        if (!line) continue;
        let evt;
        try { evt = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (evt.chat_id && !notchChatId) {
          notchChatId = evt.chat_id;
          try { localStorage.setItem("accuretta.notch.chat", notchChatId); } catch (e) {}
        }
        if (evt.chat_id && !turnChatId) turnChatId = String(evt.chat_id);
        if (evt.type === "delta" && evt.content) pushWords(String(evt.content));
        else if (evt.type === "tool_start" && evt.name) onToolStart(evt);
        else if (evt.type === "steering_applied" || evt.type === "steering_deferred") onSteeringEvent(evt);
        else if (evt.type === "error" && evt.error) { pushWords("\n\n" + String(evt.error)); }
        else if (evt.type === "final" && evt.message) {
          // The authoritative answer. A turn can end with the reply only here:
          // all budget spent on tool calls, a model that reasoned instead of
          // answering, or a salvaged partial. It was previously used ONLY when
          // nothing had streamed at all, so any turn whose answer landed here
          // after some prose dropped the answer on the floor.
          //
          // A `final` with no readable text (reasoning only) must fall through
          // to finish(), never return: bailing out here would leave `streaming`
          // set and wedge the notch against every later turn.
          const text = visibleFinalText(evt.message.content);
          if (text) {
            // The final answer is the whole message the bridge assembled, so
            // use it whenever it is not already on screen.
            if (text !== replyRaw.trim()) {
              openReply();
              replyRaw = text;
              renderReply();
            }
            // The final supersedes the tail still sitting in the flush
            // buffer; leaving it there made finish() append it twice.
            wordBuf = "";
            markRowsDone();   // no tool is running under a finished answer
          } else {
            // Nothing survived the strippers, but the bridge still sent this
            // message as the turn's answer — that combination means the model
            // produced nothing but a cascade suggestion block. The owner does
            // not want suggestion chips in the notch, so this stays the ONLY
            // place they surface: a small dim line, never chips or cards.
            const sug = cascadeSuggestions(evt.message.content);
            if (sug.length) {
              openReply();
              replyRaw = sug.join(" · ");
              replyIsSugTail = true;
              renderReply();
              wordBuf = "";
            }
          }
        }
        else if (evt.type === "chat_end") { finish(); return; }
      }
    }
    finish();
  } catch (e) {
    // Never swallow the card: an owner who cannot see that the turn broke will
    // just ask again and assume the notch is broken.
    try {
      finish("The connection to the bridge dropped before the agent replied. "
        + "Nothing was changed. Try again.");
    } catch (_) {
      try { openReply(); notch.appendReply("connection to the bridge dropped"); notch.endReply(); } catch (_) {}
      try { if (state() !== "prompt") notch.setState("prompt"); } catch (_) {}
      streaming = false;
      handledCalls.clear();
      updateComposerAction();        // live just ended: stop -> send
      if (!mirror.active) island.removeAttribute("data-turn");
    }
  }
}

// ---- power / wake -------------------------------------------------------------
async function onPower() {
  // Stop the current agent run and deny every pending gate. Never kills the
  // app, the model server or other sessions.
  const ids = [...pending.keys()];
  const chatIds = new Set();
  for (const a of pending.values()) if (a.chat_id) chatIds.add(a.chat_id);
  if (activeChatId) chatIds.add(activeChatId);
  if (notchChatId) chatIds.add(notchChatId);
  for (const id of ids) { try { await api("/api/approvals/decide", { id, decision: "deny" }); } catch (e) {} }
  for (const cid of chatIds) { try { await api("/api/cancel", { chat_id: cid }); } catch (e) {} }
  pending.clear();
}

async function onWake() {
  // Gates that arrived while the widget was off were dropped by design;
  // re-hydrate whatever is still pending on the bridge.
  const list = await getJson("/api/approvals");
  for (const a of (list && list.pending) || []) await onApprovalNew(a);
}

// ---- SSE: bridge -> widget -----------------------------------------------------
// Labels mirror the main app's toolLabel()/humanizeToolName() so a tool reads
// the same in both surfaces: the ICON carries the tool identity, the text is a
// short human phrase — never a raw tool id like "mcp_playwright".
const shortPath = (p) => {
  if (!p) return "";
  const s = String(p).replace(/\\/g, "/");
  const parts = s.split("/").filter(Boolean);
  return parts.length <= 2 ? s : "…/" + parts.slice(-2).join("/");
};
const humanizeToolName = (name) => {
  const cleaned = String(name || "tool").replace(/^mcp_[^_]+_/, "").replace(/_/g, " ").trim();
  return cleaned ? cleaned[0].toUpperCase() + cleaned.slice(1) : "Tool";
};
const phrase = (p) => shortPath(p) || "file";

const TOOL_LABELS = {
  list_directory: (a) => `Looking in ${shortPath(a.path) || "folder"}…`,
  read_file: (a) => `Reading ${phrase(a.path)}…`,
  find_files: (a) => `Searching ${shortPath(a.path) || "files"}${a.pattern ? ` for ${String(a.pattern).slice(0, 48)}` : ""}…`,
  grep_files: (a) => `Searching ${shortPath(a.path) || "files"}${a.pattern ? ` for ${String(a.pattern).slice(0, 48)}` : ""}…`,
  project_map: (a) => `Mapping ${shortPath(a.path) || "project"}…`,
  write_file: (a) => `Writing ${phrase(a.path)}…`,
  edit_file: (a) => `Editing ${phrase(a.path)}…`,
  replace_ast_node: (a) => `Editing ${phrase(a.path)}…`,
  delete_file: (a) => `Deleting ${phrase(a.path)}…`,
  run_powershell: () => "Running on host…",
  sandbox_run: () => "Running in WSL guest…",
  session_start: () => "Starting an interactive session…",
  session_send: () => "Sending input to the session…",
  session_read: () => "Reading session output…",
  run_tests: () => "Running project tests…",
  check_syntax: (a) => `Checking ${phrase(a.path)}…`,
  git_status: () => "Checking repository status…",
  git_log: () => "Reading repository history…",
  git_diff: () => "Reading changes…",
  git_push: () => "Pushing to the remote…",
  git_pull: () => "Pulling from the remote…",
  git_commit: () => "Committing…",
  git_clone: () => "Cloning the repository…",
  open_program: (a) => `Opening ${a.name || shortPath(a.path) || "program"}…`,
  web_fetch: (a) => `Fetching ${a.url ? String(a.url).slice(0, 48) : "the web"}…`,
  web_search: (a) => `Searching the web${a.query ? ` for ${String(a.query).slice(0, 48)}` : ""}…`,
  network_snapshot: () => "Scanning network…",
  screenshot: () => "Taking a screenshot…",
  list_windows: () => "Listing open windows…",
  desktop_click: () => "Clicking on screen…",
  compact_history: () => "Compacting the conversation…",
  update_plan: () => "Updating the task plan…",
  registry: (a) => `Editing ${shortPath((a.targets || [])[0]) || "the registry"}…`,
  binary_inspect: (a) => `Inspecting binary${a.path ? ` · ${shortPath(a.path)}` : ""}…`,
  yara_scan: (a) => `Scanning with YARA${a.path ? ` · ${shortPath(a.path)}` : ""}…`,
};

function toolLabel(name, args) {
  const a = args && typeof args === "object" ? args : {};
  if (name && String(name).startsWith("mcp_")) {
    const m = String(name).match(/^mcp_([^_]+)_(.+)$/);
    return m ? `${m[1]} · ${m[2].replace(/_/g, " ")}…` : `${humanizeToolName(name)}…`;
  }
  const fn = TOOL_LABELS[String(name || "")];
  return fn ? fn(a) : `${humanizeToolName(name)}…`;
}

function setToolLine(name, args) {
  const ico = ensureToolIcon();
  if (ico) ico.innerHTML = toolIconSvg(name);
  const text = toolLabel(name, args);
  notch.setTool(text);
  // A real tool is running: show the icon + what it is doing, and let the
  // dot-matrix A act it out (laser for scans/reads, sweep for deletions,
  // assembly for writes).
  lastToolStartAt = Date.now();
  pillFX(fxForTool(name));
  setActivity("tool");
  updateLiveStatus();
  reportDiag("tool");
  return text;
}

async function refreshCtx(chatId) {
  const qs = chatId ? `?chat_id=${encodeURIComponent(chatId)}` : "";
  const r = await getJson("/api/ctx-stats" + qs);
  if (r && r.capacity > 0 && r.prompt_tokens != null) notch.setContext(Math.min(1, r.prompt_tokens / r.capacity));
}

let sse = null;
function connectEvents() {
  if (sse) sse.close();
  sse = new EventSource("/api/events");
  sse.onopen = () => reportDiag("sse-open");
  sse.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (_) { return; }
    const cid = String(evt.chat_id || "");
    switch (evt.type) {
      case "chat_start": {
        // Our own POST stream already owns its turn (openReply ran before the
        // fetch resolved); the SSE copy must not reset the card mid-stream.
        if (streaming) break;
        if (evt.chat_id) activeChatId = evt.chat_id;
        turnChatId = cid;
        mirror.active = true;
        mirror.prompt = "";
        handledCalls.clear();
        clearTurnRows();
        closeTaskPanel();
        island.setAttribute("data-turn", "live");
        // Do not yank this surface off the streaming conversation: when the
        // notch itself started the turn and the owner is looking at the card,
        // the prompt + live status are already on screen. Other surfaces'
        // turns (or a folded island) still flip to the work pill.
        if (!(streaming && state() === "prompt") &&
            state() !== "gate" && state() !== "alert") notch.setState("work");
        setActivity("thinking");
        // The card may still show the PREVIOUS turn's reply; a fresh mirror
        // starts empty and seeds the prompt from the persisted chat.
        const um = document.getElementById("userMsg");
        if (um) um.textContent = "";
        const am = document.getElementById("agentMsg");
        if (am) am.textContent = "";
        replyOpen = false; replyRaw = ""; fadedChars = 0; wordBuf = ""; lastFlush = 0;
        stripThink = makeThinkStripper();
        seedMirrorPrompt(cid);
        updateLiveStatus();
        break;
      }
      case "tool_start":
        onToolStart(evt);
        break;
      case "delta":
        // Our own POST reader paints its own deltas; the SSE copy would
        // double-render. Mirror only a turn this surface did not start.
        if (streaming) break;
        if (mirror.active && evt.content &&
            (!cid || !turnChatId || cid === turnChatId)) {
          ingestWords(String(evt.content));
          updateLiveStatus();
        }
        break;
      case "ctx_fill":
        if (evt.capacity > 0 && evt.prompt_tokens != null) notch.setContext(Math.min(1, evt.prompt_tokens / evt.capacity));
        break;
      case "stats": {
        const n = evt.eval_count, ns = evt.eval_duration;
        if (n != null && ns > 0) notch.setStats({ tps: n / (ns / 1e9) });
        break;
      }
      case "summary_folded":
        notch.compact();
        refreshCtx(evt.chat_id);
        break;
      case "approval:new":
        onApprovalNew(evt.approval);
        break;
case "approval:decided": {
          // Record before dropping it: this is the single place every
          // resolution funnels through, whichever surface decided.
          const known = pending.get(evt.id);
          if (known && known._gate && !recordedDecisions.has(evt.id)) {
            recordedDecisions.add(evt.id);
            rememberDecision(known._gate, evt.decision === "approve" ? "approve" : "deny");
          }
          onApprovalDecided(evt.id);
          break;
        }
      case "models:update":
        refreshModels();
        break;
      case "steering_applied":
      case "steering_deferred":
        // Delivery receipt for a steer this notch sent (applied at the next
        // safe boundary, or dropped because the turn ended first).
        onSteeringEvent(evt);
        break;
      case "chat_end": {
        // Scope the ending to the chat the notch is actually mirroring. Two
        // things used to collapse the island to the idle dot MID-TASK: a
        // chat_end from another surface's chat, and a reconnect replaying an
        // older turn's chat_end (Last-Event-ID). Both read as "the agent is
        // done" while it was not — the idle flash that snapped back to work
        // on the next tool_start.
        if (streaming) break;
        if (cid && turnChatId && cid !== turnChatId) break;
        notch.setStats(); // hide t/s
        mirror.active = false;
        turnChatId = "";
        markRowsDone();
        island.removeAttribute("data-turn");
        island.removeAttribute("data-busy");
        setActivity("idle");
        // A finished mirrored turn whose owner is NOT at the main app pops the
        // reply out here — asking from the phone or leaving the app minimized
        // otherwise means the answer lands unseen. Focused app => stay quiet
        // (the main window already showed the turn live).
        const shouldPop = !evt.web_focused && !!replyRaw.trim() &&
          state() !== "gate" && state() !== "alert" && state() !== "off";
        // Never yank the card away from a turn this surface is streaming —
        // the SSE copy can arrive before we have read our own response.
        if (state() === "work") notch.setState(shouldPop ? "prompt" : "idle");
        else if (shouldPop && (state() === "idle" || state() === "ready")) notch.setState("prompt");
        // A mirrored turn that did real work gets the same send-off the
        // notch's own turns get.
        if (state() === "prompt" && turnToolRows.length) showOpenMain();
        break;
      }
      case "error":
        if (evt.error) notify("error");
        break;
    }
  };
  sse.onerror = () => {
    /* EventSource auto-reconnects with Last-Event-ID replay */
    reportDiag("sse-error");
  };
}

// ---- widget -> bridge ----------------------------------------------------------
island.addEventListener("notch", (e) => {
  const d = e.detail || {};
  switch (d.type) {
    case "approve":
    case "deny": {
      const g = d.gate || {};
      if (!g.id) return;
      pending.delete(g.id);
      api("/api/approvals/decide", { id: g.id, decision: d.type }).then(r => {
        if (!r.ok) console.warn("notch: gate already resolved elsewhere", g.id);
      }).catch(() => {});
      // The decision itself is recorded from the approval:decided broadcast,
      // so approvals resolved anywhere (here, Discord, the main app, push)
      // all land in the task panel.
      break;
    }
    case "send":
      // Live turn anywhere => steer; idle => new turn. See sendPrompt.
      if (d.prompt) sendPrompt(String(d.prompt));
      break;
    case "model":
      if (d.model) onModelPicked(String(d.model));
      break;
    case "power":
      onPower();
      break;
    case "wake":
      onWake();
      break;
  }
});

// ---- hydrate ------------------------------------------------------------------
(async () => {
  await refreshModels();
  await refreshCtx("");
  const list = await getJson("/api/approvals");
  for (const a of (list && list.pending) || []) await onApprovalNew(a);
  connectEvents();
  reportDiag("hydrated");
  reportRect();
  hostPost({ type: "ready" });
})();

})();
