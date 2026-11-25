/* SPDX-License-Identifier: AGPL-3.0-only */
/* cost widget start-up: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["cost-widget-init"] = ({
  $, CLOUD_PRICING, _fetchSavings, initUsageProfile, renderCostWidget, shareSavingsCard, state,
}) => {
  function initCostWidget() {
    // Restore persisted provider selection
    const saved = localStorage.getItem("accuretta:cost-provider");
    if (saved && CLOUD_PRICING[saved]) state.costProvider = saved;
    // Seed all-time totals from the offline cache for an instant paint, then
    // reconcile with the server (_fetchSavings at the end of this function).
    state._allTimeTokOut = parseInt(localStorage.getItem("accuretta:all-tok-out") || "0", 10) || 0;
    state._allTimeTokIn  = parseInt(localStorage.getItem("accuretta:all-tok-in")  || "0", 10) || 0;
    state._savingsSince  = parseInt(localStorage.getItem("accuretta:savings-since") || "0", 10) || 0;
    // Collapsible: start minimized to save vertical space; remember the choice.
    const widget = $("#cost-widget");
    const toggle = $("#cost-widget-toggle");
    if (widget && toggle) {
      const expanded = localStorage.getItem("accuretta:cost-expanded") === "1";
      widget.classList.toggle("collapsed", !expanded);
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.addEventListener("click", () => {
        const isExpanded = !widget.classList.toggle("collapsed");
        toggle.setAttribute("aria-expanded", isExpanded ? "true" : "false");
        localStorage.setItem("accuretta:cost-expanded", isExpanded ? "1" : "0");
      });
    }
    // Wire up dropdown
    const sel = $("#cost-select");
    if (sel) {
      sel.value = state.costProvider;
      sel.addEventListener("change", () => {
        state.costProvider = sel.value;
        localStorage.setItem("accuretta:cost-provider", state.costProvider);
        renderCostWidget();
      });
    }
    // Wire up the shareable savings card
    const shareBtn = $("#cost-share");
    if (shareBtn) shareBtn.addEventListener("click", shareSavingsCard);
    initUsageProfile();
    renderCostWidget();
    // Reconcile against the durable server-side totals (overrides the cache).
    _fetchSavings();
  }
  return { initCostWidget };
};
