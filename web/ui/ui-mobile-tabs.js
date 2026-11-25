/* SPDX-License-Identifier: AGPL-3.0-only */
/* mobile tabs: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["mobile-tabs"] = ({ $$, app, openSettings, positionAppUpdateCard, state }) => {
  function applyMobileTab() {
    $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === state.mobileTab));
    app.classList.remove("m-tab-chat", "m-tab-sessions", "m-tab-approvals", "m-tab-settings");
    if (state.mobileTab === "settings") {
      openSettings();
      state.mobileTab = "chat";
      $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === "chat"));
      app.classList.add("m-tab-chat");
      positionAppUpdateCard();
      return;
    }
    app.classList.add("m-tab-" + state.mobileTab);
    positionAppUpdateCard();
  }
  return { applyMobileTab };
};
