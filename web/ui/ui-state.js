/* SPDX-License-Identifier: AGPL-3.0-only */
/* state: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["state"] = ({ $, wbChordText }) => {
  const state = {
    chats: { chats: {}, order: [] },
    chatId: null,
    messages: [],
    messageWindowStart: 0,
    messageWindowSize: 40,
    settings: {},
    workspace: { folders: [] },
    models: [],
    mode: "agent",         // agent | ide
    view: "preview",       // preview | code
    versions: [],
    activeVersion: null,   // vid
    currentHtml: "",
    currentFiles: {},      // { "style.css": "...", "script.js": "...", ... } parsed from the current assistant turn
    streaming: false,
    abortCtl: null,
    editingTurn: null,       // { index } while a user turn is being edited
    _comparePair: null,      // { before, beforeLabel } captured for the Compare chip
    approvals: new Map(),
    skills: null,            // [ {name, description, budget, body_chars, lines} ] catalog
    skillsAt: 0,
    activeSkill: null,       // { name, budget, body_chars } of the chat's active skill
    draftPairs: {},          // model path -> { draft, reasons } when a DFlash pair is valid
    compactingChats: new Set(),
    compactionRows: new Map(),
    skillSaveRows: new Map(),
    mobileTab: "chat",
    pendingImages: [],  // [{ dataUrl, name }]
    pendingFiles: [],   // browser File objects uploaded on send
    uploadingFiles: false, // true while send uploads the tray
    messageQueues: new Map(), // chat id -> queued composer snapshots
    queueDispatching: false,
    queueEditId: null,
    queueDrainTimer: null,
    projectScope: "",        // sidebar filter: "" all | "__none__" | normalized project path
    clientContext: null,
    remoteAccess: null,
    executionTarget: localStorage.getItem("accuretta:execution-target") || "host",
    viewport: "full",        // full | desktop | tablet | mobile
    consoleOpen: false,
    consoleLogs: [],         // [{level, text, t}]
    tokTotal: 0,             // cumulative generated tokens for this session (client-side)
    tokPromptTotal: 0,       // cumulative prompt tokens this session (for cost calc)
    totalGenDuration: 0,     // cumulative generation duration (seconds) across session
    _streamOutEstimate: 0,   // live output token estimate during streaming (chars/4)
    _streamPromptEstimate: 0,// live prompt token estimate during streaming
    costProvider: "openai",  // selected provider for cost widget
    sessionDesktopDisabled: false,
    palette: { open: false, items: [], idx: 0 },
    _versionsExpanded: false,
    _lastMsgTokens: 0,
    _lastMsgPromptTokens: 0,
    _ctxBreakdown: null, // live per-category tokens from ctx_fill/stats (bridge _ctx_breakdown)
    _ctxPoll: null,
    touchedFiles: new Set(),
    imageUrlToSourceMap: new Map(),
    reasoningCapability: { supported: false, mode: "none", source: "no_model" },
    reasoningEffort: "auto",
  };

  const SHORTCUT_ACTIONS = Object.freeze({
    approve_pending: { label: "Approve newest request", detail: "ordinary approvals only", icon: "ph-check-circle" },
    deny_pending: { label: "Deny newest request", detail: "closes the latest gate", icon: "ph-x-circle" },
    open_settings: { label: "Open settings", detail: "opens the settings drawer", icon: "ph-gear-six" },
    open_faq: { label: "Open FAQ", detail: "opens the field guide", icon: "ph-question" },
    open_usage: { label: "Open usage stats", detail: "shows local counters", icon: "ph-chart-line-up" },
    new_chat: { label: "New chat", detail: "creates a fresh session", icon: "ph-plus-circle" },
    focus_composer: { label: "Focus composer", detail: "puts the cursor in the prompt", icon: "ph-cursor-text" },
    stop_generation: { label: "Stop generation", detail: "stops the current reply", icon: "ph-stop-circle" },
    open_palette: { label: "Command palette", detail: "sessions, commands and files", icon: "ph-magnifying-glass" },
    quick_open: { label: "Quick open file", detail: "fuzzy-find a workspace file", icon: "ph-file-magnifying-glass" },
    toggle_sidebar: { label: "Toggle sidebar", detail: "shows or hides the session rail", icon: "ph-sidebar-simple" },
    toggle_explorer: { label: "Toggle workspace", detail: "shows or hides the workspace folders", icon: "ph-files" },
    toggle_terminal: { label: "Toggle terminal", detail: "shows or hides the terminal panel", icon: "ph-terminal-window" },
    toggle_preview: { label: "Toggle preview", detail: "shows or hides the right pane", icon: "ph-browser" },
    next_tab: { label: "Next tab", detail: "switches to the next open session", icon: "ph-arrow-down" },
    prev_tab: { label: "Previous tab", detail: "switches to the previous open session", icon: "ph-arrow-up" },
    close_tab: { label: "Close tab", detail: "closes the current session tab", icon: "ph-x-square" },
  });
  // Built-in key combinations. A user binding replaces its default; a stored
  // "" switches the default off. Ctrl+W cannot be intercepted in a browser tab,
  // so outside the desktop window close-tab defaults to Alt+W.
  function defaultShortcuts() {
    return {
      open_palette: "Ctrl+KeyK",
      quick_open: "Ctrl+KeyP",
      toggle_sidebar: "Ctrl+KeyB",
      toggle_explorer: "Ctrl+Shift+KeyB",
      toggle_terminal: "Ctrl+KeyJ",
      toggle_preview: "Ctrl+Backslash",
      next_tab: "Ctrl+Shift+ArrowDown",
      prev_tab: "Ctrl+Shift+ArrowUp",
      close_tab: window.pywebview ? "Ctrl+KeyW" : "Alt+KeyW",
    };
  }
  function effectiveShortcuts() {
    return { ...defaultShortcuts(), ...(state.settings?.keyboard_shortcuts || {}) };
  }

  const app = $("#app");
  const isMobile = () => window.matchMedia("(max-width: 600px)").matches;
  const isCompactViewport = () => window.matchMedia("(max-width: 820px)").matches;
  let wasCompactViewport = false;

  function syncCompactShell() {
    const compact = !isMobile() && isCompactViewport();
    document.body.classList.toggle("is-compact", compact);
    if (compact && !wasCompactViewport) {
      app.classList.add("sidebar-collapsed", "preview-collapsed");
    }
    wasCompactViewport = compact;
    syncSidebarToggle();
  }

  function syncSidebarToggle() {
    // Collapsed, the sidebar is an icon rail and its own toggle expands it.
    const railToggle = $("#btn-toggle-sidebar");
    if (railToggle && !isMobile()) {
      const label = app.classList.contains("sidebar-collapsed") ? "Expand sidebar" : "Collapse sidebar";
      let keys = "";
      try { keys = wbChordText("toggle_sidebar"); } catch {}
      railToggle.title = keys ? `${label} (${keys})` : label;
      railToggle.setAttribute("aria-label", label);
    }
    const button = $("#btn-toggle-sidebar-m");
    const icon = button?.querySelector("i");
    if (!button || !icon) return;
    const mobile = isMobile();
    const collapsed = app.classList.contains("sidebar-collapsed");
    const label = mobile ? "Sessions" : (collapsed ? "Open sidebar" : "Collapse sidebar");
    icon.className = `ph ${mobile || !collapsed ? "ph-list" : "ph-caret-right"}`;
    button.title = label;
    button.setAttribute("aria-label", label);
  }

  new MutationObserver(syncSidebarToggle).observe(app, {
    attributes: true,
    attributeFilter: ["class"],
  });
  return {
    state, SHORTCUT_ACTIONS, defaultShortcuts, effectiveShortcuts, app, isMobile, isCompactViewport,
    syncCompactShell, syncSidebarToggle,
  };
};
