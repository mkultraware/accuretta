/* SPDX-License-Identifier: AGPL-3.0-only */
/* context breakdown hover: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["ctx-breakdown"] = ({ $, computeCtxUsage, esc, state }) => {
  // llama.cpp-style ring tooltip: where the window went (system, tool specs,
  // user/assistant turns, tool results). Live per-category tokens ride on
  // ctx_fill / stats events; before the first round they are estimated locally
  // in the same shape. Reuses the chat-link-popover chrome.
  let _ctxPopoverEl = null;
  let _ctxPopoverHideTimer = null;
  // For code in other files, which would only get a copy of the let above.
  function ctxPopoverOpen() { return !!_ctxPopoverEl && !_ctxPopoverEl.hasAttribute("hidden"); }
  const _CTXBD_ORDER = [
    ["system", "System"],
    ["tools", "Tool specs"],
    ["user", "User"],
    ["assistant", "Assistant"],
    ["tool", "Tool results"],
  ];
  function _ctxBreakdownData() {
    const use = computeCtxUsage();
    const live = state._ctxBreakdown;
    if (live && typeof live === "object") {
      const rows = [];
      for (const [key, label] of _CTXBD_ORDER) {
        const t = Math.max(0, Math.round(Number(live[key]) || 0));
        if (t > 0) rows.push({ label, tokens: t });
      }
      if (rows.length) return { rows, used: use.used, capacity: use.capacity, live: true };
    }
    // Cold start: estimate from visible bubbles (chars/3, tool traffic 1.5x),
    // mirroring the estimate path in computeCtxUsage.
    const msgs = state.messages || [];
    const acc = { system: Math.round(2500 / 3), user: 0, assistant: 0, tool: 0 };
    for (const m of msgs) {
      const role = m && (m.role === "user" || m.role === "assistant" || m.role === "tool") ? m.role : "assistant";
      const mult = role === "tool" ? 1.5 : 1.0;
      acc[role] += Math.round(String((m && m.content) || "").length * mult / 3);
    }
    const rows = [
      { label: "System", tokens: acc.system },
      { label: "User", tokens: acc.user },
      { label: "Assistant", tokens: acc.assistant },
      { label: "Tool results", tokens: acc.tool },
    ].filter(r => r.tokens > 0);
    if (!rows.length) return null;
    return { rows, used: use.used, capacity: use.capacity, live: false };
  }
  function ensureCtxPopover() {
    if (_ctxPopoverEl) return _ctxPopoverEl;
    const el = document.createElement("div");
    el.className = "chat-link-popover ctx-breakdown";
    el.setAttribute("hidden", "");
    el.innerHTML = `<div class="ctxbd-body">
      <div class="ctxbd-head">Context usage</div>
      <dl class="ctxbd-rows"></dl>
      <div class="ctxbd-foot"></div>
      <button type="button" class="ctxbd-compact" id="btn-compact"
              title="Fold older turns into the session summary now — frees context on long chats without losing detail">
        <i class="ph ph-arrows-in-line-vertical" aria-hidden="true"></i><span>Compact now</span>
      </button>
    </div>`;
    el.addEventListener("mouseenter", () => {
      if (_ctxPopoverHideTimer) { clearTimeout(_ctxPopoverHideTimer); _ctxPopoverHideTimer = null; }
    });
    el.addEventListener("mouseleave", scheduleHideCtxPopover);
    document.body.appendChild(el);
    _ctxPopoverEl = el;
    return el;
  }
  function renderCtxBreakdown() {
    const el = ensureCtxPopover();
    const data = _ctxBreakdownData();
    const rowsEl = el.querySelector(".ctxbd-rows");
    const footEl = el.querySelector(".ctxbd-foot");
    if (!data) {
      rowsEl.innerHTML = `<div class="ctxbd-empty">No context data yet — run a turn first.</div>`;
      footEl.textContent = "";
      return;
    }
    const cap = Math.max(1, data.capacity);
    rowsEl.innerHTML = data.rows.map(r => {
      const pct = Math.min(100, (r.tokens / cap) * 100);
      return `<div class="ctxbd-row"><dt>${esc(r.label)}</dt>`
        + `<dd class="ctxbd-bar"><span style="width:${pct.toFixed(1)}%"></span></dd>`
        + `<dd class="ctxbd-val">${r.tokens.toLocaleString()} · ${pct.toFixed(1)}%</dd></div>`;
    }).join("");
    const free = Math.max(0, cap - data.used);
    footEl.innerHTML = `<span>${data.used.toLocaleString()} of ${cap.toLocaleString()} used · ${free.toLocaleString()} free</span>`
      + `<span>${data.live ? "live" : "estimate"}</span>`;
  }
  // The card opens from the ring above the box, or, on phones where that strip
  // is hidden, from the context item in the status line under the title bar.
  let _ctxPopoverAnchor = null;
  function ctxPopoverAnchor() {
    const shown = el => !!el && el.getBoundingClientRect().width > 0;
    if (shown(_ctxPopoverAnchor)) return _ctxPopoverAnchor;
    const gauge = document.getElementById("ctx-gauge");
    return shown(gauge) ? gauge : document.querySelector(".status-ctx");
  }
  function positionCtxPopover() {
    if (!_ctxPopoverEl) return;
    const anchor = ctxPopoverAnchor();
    if (!anchor) return;
    const r = anchor.getBoundingClientRect();
    const pw = _ctxPopoverEl.offsetWidth || 300;
    const ph = _ctxPopoverEl.offsetHeight || 200;
    // Ring sits in the composer foot: open above it, right edges aligned,
    // and drop below only when there is no room overhead.
    let left = Math.min(r.right - pw, window.innerWidth - pw - 8);
    if (left < 8) left = 8;
    let top = r.top - ph - 8;
    if (top < 8) top = Math.min(r.bottom + 8, window.innerHeight - ph - 8);
    _ctxPopoverEl.style.left = left + "px";
    _ctxPopoverEl.style.top = top + "px";
  }
  function showCtxPopover(anchor) {
    if (anchor) _ctxPopoverAnchor = anchor;
    const el = ensureCtxPopover();
    if (_ctxPopoverHideTimer) { clearTimeout(_ctxPopoverHideTimer); _ctxPopoverHideTimer = null; }
    renderCtxBreakdown();
    el.removeAttribute("hidden");
    // Place it before the next paint: an unplaced card can land under the
    // finger mid-tap, and the rest of the tap then reads as a tap outside it.
    positionCtxPopover();
    requestAnimationFrame(() => {
      el.classList.add("visible");
      positionCtxPopover();
    });
  }
  function scheduleHideCtxPopover() {
    if (_ctxPopoverHideTimer) clearTimeout(_ctxPopoverHideTimer);
    _ctxPopoverHideTimer = setTimeout(() => {
      if (_ctxPopoverEl) {
        _ctxPopoverEl.classList.remove("visible");
        _ctxPopoverEl.setAttribute("hidden", "");
      }
      _ctxPopoverHideTimer = null;
    }, 220);
  }
  // Global delegated hover — the gauge is static chrome, same pattern as the
  // chat-link preview. Moving within the gauge or onto the card keeps it open.
  document.addEventListener("mouseover", (ev) => {
    const g = ev.target && ev.target.closest && ev.target.closest("#ctx-gauge");
    if (!g) return;
    if (g.contains(ev.relatedTarget)) return;
    showCtxPopover(g);
  });
  document.addEventListener("mouseout", (ev) => {
    const g = ev.target && ev.target.closest && ev.target.closest("#ctx-gauge");
    if (!g) return;
    if (g.contains(ev.relatedTarget)) return;
    if (_ctxPopoverEl && _ctxPopoverEl.contains(ev.relatedTarget)) return;
    scheduleHideCtxPopover();
  });
  // Click and keyboard focus open the same card, so touch (remote over
  // Tailscale) and keyboard users can reach the Compact button too.
  document.addEventListener("click", (ev) => {
    const anchor = ev.target && ev.target.closest && ev.target.closest("#ctx-gauge, .status-ctx");
    if (anchor) { showCtxPopover(anchor); return; }
    if (_ctxPopoverEl && !_ctxPopoverEl.hasAttribute("hidden") && !_ctxPopoverEl.contains(ev.target)) scheduleHideCtxPopover();
  });
  document.addEventListener("focusin", (ev) => {
    if (ev.target && (ev.target.id === "ctx-gauge" || ev.target.classList?.contains("status-ctx"))) showCtxPopover(ev.target);
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && _ctxPopoverEl && !_ctxPopoverEl.hasAttribute("hidden")) scheduleHideCtxPopover();
  });
  function renderTokTotal() {
    const el = $("#tok-total");
    if (!el) return;
    el.textContent = `${state.tokTotal.toLocaleString()} tok`;
  }
  return { ctxPopoverOpen, renderCtxBreakdown, renderTokTotal };
};
