/* SPDX-License-Identifier: AGPL-3.0-only */
/* cost savings widget: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["savings-widget"] = ({ $, API_RATE_NOTE, CLOUD_PRICING, state }) => {

  function calcCost(provider) {
    const p = CLOUD_PRICING[provider];
    if (!p) return 0;
    // Use all-time persistent totals + any live streaming estimate
    const promptTok = state._allTimeTokIn + (state._streamPromptEstimate || 0);
    const outTok = state._allTimeTokOut + (state._streamOutEstimate || 0);
    const inCost  = (promptTok / 1_000_000) * p.input;
    const outCost = (outTok / 1_000_000) * p.output;
    return inCost + outCost;
  }

  // Persist all-time token totals to localStorage
  function _persistAllTimeTok() {
    try {
      localStorage.setItem("accuretta:all-tok-out", String(state._allTimeTokOut));
      localStorage.setItem("accuretta:all-tok-in", String(state._allTimeTokIn));
    } catch {}
  }

  // Apply server-authoritative lifetime totals to state, the local cache and
  // the UI. The server is the source of truth; localStorage is an offline cache
  // so the card isn't blank before the fetch.
  function _applySavings(obj) {
    if (!obj) return;
    const inTok = parseInt(obj.tok_in, 10);
    const outTok = parseInt(obj.tok_out, 10);
    if (Number.isFinite(inTok)) state._allTimeTokIn = inTok;
    if (Number.isFinite(outTok)) state._allTimeTokOut = outTok;
    if (obj.since) state._savingsSince = obj.since;
    if (Number.isFinite(parseInt(obj.turns, 10))) state._savingsTurns = parseInt(obj.turns, 10);
    _persistAllTimeTok();
    try { if (obj.since) localStorage.setItem("accuretta:savings-since", String(obj.since)); } catch {}
    renderCostWidget();
  }

  // Pull the durable lifetime totals from the server. Called on load and as a
  // fallback; live turns update via the "savings" SSE event.
  async function _fetchSavings() {
    try {
      const r = await fetch("/api/savings");
      if (!r.ok) return;
      _applySavings(await r.json());
    } catch {}
  }

  // Unix seconds -> "Mon YYYY" for the "since" line ("saving since Mar 2026").
  function _fmtSince(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "short", year: "numeric" });
  }

  // Calculate session-only cost (uses session token counters, not all-time)
  function calcSessionCost(provider) {
    const p = CLOUD_PRICING[provider];
    if (!p) return 0;
    const promptTok = state.tokPromptTotal + (state._streamPromptEstimate || 0);
    const outTok = state.tokTotal + (state._streamOutEstimate || 0);
    const inCost  = (promptTok / 1_000_000) * p.input;
    const outCost = (outTok / 1_000_000) * p.output;
    return inCost + outCost;
  }

  function formatEstimatedCost(value) {
    return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value);
  }

  function renderCostWidget() {
    const el = $("#cost-amount");
    if (!el) return;
    const allTimeCost = calcCost(state.costProvider);
    const sessionCost = calcSessionCost(state.costProvider);
    // Main big number = all-time total
    el.textContent = formatEstimatedCost(allTimeCost);
    el.classList.toggle("zero", allTimeCost < 0.005);

    // Update the "saved vs" label dynamically with the provider name
    const label = $("#cost-widget .cost-widget-label");
    if (label) {
      const p = CLOUD_PRICING[state.costProvider];
      label.textContent = `Estimated ${p ? p.label : state.costProvider} API cost`;
      label.title = "Token-count comparison using the listed rates. Excludes hardware and electricity, and does not imply equal model quality.";
    }
    // Collapsed one-liner: amount + a short form of the same label.
    const short = $("#cost-widget .cost-widget-short");
    if (short) {
      const p = CLOUD_PRICING[state.costProvider];
      short.textContent = `est. ${p ? p.label : state.costProvider} cost`;
    }

    // Session row
    const sessionEl = $("#cost-session");
    if (sessionEl) {
      sessionEl.textContent = formatEstimatedCost(sessionCost);
    }
    // All-time row
    const alltimeEl = $("#cost-alltime");
    if (alltimeEl) {
      alltimeEl.textContent = formatEstimatedCost(allTimeCost);
    }
    // Since row — the start date makes months of accrual legible.
    const sinceEl = $("#cost-since");
    if (sinceEl) sinceEl.textContent = _fmtSince(state._savingsSince) || "—";
  }
  return { calcCost, _applySavings, _fetchSavings, _fmtSince, renderCostWidget };
};
