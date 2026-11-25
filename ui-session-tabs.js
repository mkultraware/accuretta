/* SPDX-License-Identifier: AGPL-3.0-only */
/* session tabs: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["session-tabs"] = ({
  $, WB_TABS_KEY, esc, newChat, selectChat, state, wb, wbModelColor, wbRefreshGitForChat,
  wbRenderContextRow, wbRevealChatGroup, wbStore,
}) => {
  function wbTabs() {
    const ids = wbStore.get(WB_TABS_KEY, []);
    return Array.isArray(ids) ? ids.filter(x => typeof x === "string") : [];
  }
  function wbSaveTabs(ids) { wbStore.set(WB_TABS_KEY, ids.slice(-16)); }
  function wbOnChatSelected(id) {
    if (!id) return;
    const tabs = wbTabs();
    if (!tabs.includes(id)) {
      // New tabs open right after the one you were on.
      const at = tabs.indexOf(wb.lastTab);
      if (at >= 0) tabs.splice(at + 1, 0, id); else tabs.push(id);
      wbSaveTabs(tabs);
    }
    if (wb.lastTab && wb.lastTab !== id) wbRevealChatGroup(id);
    wb.lastTab = id;
    wbRenderTabs();
    wbRenderContextRow();
    if (wb.ready) wbRefreshGitForChat();
  }
  function wbRenderTabs() {
    const host = $("#wb-tabs");
    if (!host) return;
    const chats = state.chats?.chats || {};
    let tabs = wbTabs();
    if (state.chats?.order?.length) {
      const live = tabs.filter(id => chats[id]);
      if (live.length !== tabs.length) { tabs = live; wbSaveTabs(tabs); }
    }
    if (state.chatId && !tabs.includes(state.chatId) && chats[state.chatId]) { tabs.push(state.chatId); wbSaveTabs(tabs); }
    const strip = host.querySelector(".wb-tab-strip");
    // The sidebar re-renders often (every chat event); only touch the DOM
    // when a tab actually changed.
    const pendingIds = new Set([...state.approvals.values()].filter(a => a.status === "pending").map(a => a.chat_id));
    const html = tabs.map(id => {
      const c = chats[id] || {};
      const needsYou = pendingIds.has(id);
      const working = (state.liveTurn?.chatId === id && state.streaming) || c.task_state === "working";
      const st = needsYou ? "needs-you" : working ? "live" : "";
      const title = c.title || "new session";
      return `<div class="wb-tab${id === state.chatId ? " active" : ""}" role="tab" tabindex="0" aria-selected="${id === state.chatId}" data-id="${esc(id)}" title="${esc(title)}">`
        + `<span class="wb-mdot${st ? " " + st : ""}" style="--wb-dot:${wbModelColor(c.last_model || "")}"></span>`
        + `<span class="wb-tab-title">${esc(title)}</span>`
        + `<button class="wb-tab-close" type="button" tabindex="-1" aria-label="Close ${esc(title)}" title="Close tab"><i class="ph ph-x"></i></button></div>`;
    }).join("");
    if (strip._wbHtml === html) return;
    strip._wbHtml = html;
    strip.innerHTML = html;
    // Scroll the strip itself; scrollIntoView would also scroll (and force a
    // layout of) every scrollable ancestor.
    const active = strip.querySelector(".wb-tab.active");
    if (active) {
      const left = active.offsetLeft, right = left + active.offsetWidth;
      if (left < strip.scrollLeft) strip.scrollLeft = left;
      else if (right > strip.scrollLeft + strip.clientWidth) strip.scrollLeft = right - strip.clientWidth;
    }
  }
  function wbCycleTab(delta) {
    const tabs = wbTabs().filter(id => state.chats?.chats?.[id]);
    if (tabs.length < 2) return;
    const at = Math.max(0, tabs.indexOf(state.chatId));
    selectChat(tabs[(at + delta + tabs.length) % tabs.length]);
  }
  async function wbCloseTab(id) {
    const tabs = wbTabs();
    const at = tabs.indexOf(id);
    if (at < 0) return;
    tabs.splice(at, 1);
    wbSaveTabs(tabs);
    if (id !== state.chatId) { wbRenderTabs(); return; }
    const next = tabs[at] || tabs[at - 1];
    if (next && state.chats?.chats?.[next]) await selectChat(next);
    else await newChat();
  }
  return { wbOnChatSelected, wbRenderTabs, wbCycleTab, wbCloseTab };
};
