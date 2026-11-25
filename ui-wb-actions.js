/* SPDX-License-Identifier: AGPL-3.0-only */
/* actions: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["wb-actions"] = ({
  $, app, applyMobileTab, closePalette, isMobile, openPalette, state, wbCloseTab, wbCycleTab,
  wbStore,
}) => {
  function wbRunAction(action) {
    if (action === "open_palette") {
      if (state.palette.open && state.palette.mode !== "files") closePalette();
      else { if (state.palette.open) closePalette(); openPalette("all"); }
    } else if (action === "quick_open") {
      if (state.palette.open && state.palette.mode === "files") closePalette();
      else { if (state.palette.open) closePalette(); openPalette("files"); }
    } else if (action === "toggle_sidebar") {
      if (isMobile()) { state.mobileTab = state.mobileTab === "sessions" ? "chat" : "sessions"; applyMobileTab(); }
      else app.classList.toggle("sidebar-collapsed");
    } else if (action === "toggle_explorer") {
      wbToggleExplorer();
    } else if (action === "toggle_terminal") {
      wbToggleTerminal();
    } else if (action === "toggle_preview") {
      app.classList.toggle("preview-collapsed");
    } else if (action === "next_tab" || action === "prev_tab") {
      wbCycleTab(action === "next_tab" ? 1 : -1);
    } else if (action === "close_tab") {
      if (state.chatId) wbCloseTab(state.chatId);
    }
  }
  function wbToggleExplorer(force) {
    const sidebar = $("#sidebar");
    if (!sidebar) return;
    if (!isMobile() && app.classList.contains("sidebar-collapsed")) {
      app.classList.remove("sidebar-collapsed");
      force = true;
    }
    const hide = typeof force === "boolean" ? !force : !sidebar.classList.contains("wb-explorer-hidden");
    sidebar.classList.toggle("wb-explorer-hidden", hide);
    wbStore.set("accuretta:wb:explorer-hidden", hide);
    const btn = $("#btn-wb-explorer");
    if (btn) {
      btn.setAttribute("aria-expanded", String(!hide));
      const icon = btn.querySelector("i");
      if (icon) icon.className = `ph ph-caret-${hide ? "right" : "down"}`;
    }
  }
  function wbToggleTerminal(force) {
    const collapsed = app.classList.contains("preview-collapsed");
    const show = typeof force === "boolean" ? force : (collapsed || app.classList.contains("wb-term-hidden"));
    if (show && collapsed && !isMobile()) app.classList.remove("preview-collapsed");
    app.classList.toggle("wb-term-hidden", !show);
    wbStore.set("accuretta:wb:terminal-hidden", !show);
    wbSyncTerminalButton();
  }
  function wbSyncTerminalButton() {
    $("#wb-sb-terminal")?.classList.toggle("active",
      !app.classList.contains("wb-term-hidden") && !app.classList.contains("preview-collapsed"));
  }
  return { wbRunAction, wbToggleExplorer, wbToggleTerminal, wbSyncTerminalButton };
};
