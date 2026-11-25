/* SPDX-License-Identifier: AGPL-3.0-only */
/* shortcuts: blocking, keycap toast, hints: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["shortcuts"] = ({
  $, effectiveShortcuts, esc, isMobile, shortcutLabel, syncSidebarToggle, wb, wbStore,
}) => {
  const WB_NAV_ACTIONS = new Set(["toggle_sidebar", "toggle_explorer", "toggle_terminal", "toggle_preview", "next_tab", "prev_tab", "close_tab", "open_palette", "quick_open"]);
  function wbOverlayOpen() {
    return !!(document.querySelector("dialog[open]")
      || $("#settings-drawer")?.classList.contains("open")
      || $("#security-page")?.classList.contains("open")
      || $("#faq-page")?.classList.contains("open")
      || $("#usage-page")?.classList.contains("open"));
  }
  function wbShortcutBlocked(action) {
    return WB_NAV_ACTIONS.has(action) && wbOverlayOpen();
  }
  function wbKeycapsOn() { return wbStore.get("accuretta:wb:keycaps", true) !== false; }
  function wbShowKeycap(chord, label) {
    if (!chord || !wbKeycapsOn() || isMobile()) return;
    const el = $("#wb-keycap");
    if (!el) return;
    el.innerHTML = shortcutLabel(chord).split(" + ").map(k => `<kbd>${esc(k)}</kbd>`).join("")
      + (label ? `<span class="wb-keycap-label">${esc(label)}</span>` : "");
    el.classList.remove("show");
    void el.offsetWidth;
    el.classList.add("show");
    clearTimeout(wb.keycapTimer);
    wb.keycapTimer = setTimeout(() => el.classList.remove("show"), 1100);
  }
  function wbChordText(action) {
    const chord = effectiveShortcuts()[action];
    return chord ? shortcutLabel(chord).replace(/ \+ /g, " ") : "";
  }
  function wbRefreshShortcutHints() {
    const hint = (el, base, action) => {
      if (!el) return;
      const keys = wbChordText(action);
      el.title = keys ? `${base} (${keys})` : base;
    };
    hint($("#btn-palette"), "Command palette", "open_palette");
    hint($("#btn-toggle-preview"), "Toggle preview", "toggle_preview");
    syncSidebarToggle();
    hint($("#btn-wb-explorer"), "Show or hide the workspace folders", "toggle_explorer");
    hint($("#wb-sb-terminal"), "Toggle terminal", "toggle_terminal");
    const searchKbd = $("#btn-search-sessions kbd");
    if (searchKbd) searchKbd.textContent = wbChordText("open_palette");
    const termKbd = $("#wb-sb-terminal kbd");
    if (termKbd) termKbd.textContent = wbChordText("toggle_terminal");
  }
  return { wbShortcutBlocked, wbKeycapsOn, wbShowKeycap, wbChordText, wbRefreshShortcutHints };
};
