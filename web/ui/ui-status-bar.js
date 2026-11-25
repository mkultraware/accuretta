/* SPDX-License-Identifier: AGPL-3.0-only */
/* status bar, meters, chips: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["status-bar"] = ({
  $, api, computeCtxUsage, esc, renderSidebarUsage, state, statusSpeedText, wb, wbModelColor,
  wbModelShort,
}) => {
  function wbRenderStatusbar(speed, stateStr) {
    const bar = $("#wb-statusbar");
    if (!bar) return;
    const set = (sel, text) => { const el = bar.querySelector(sel); if (el && el.textContent !== text) el.textContent = text; };
    const isStreaming = !!state.streaming || stateStr === "streaming";
    const model = (state.missingModel && !state.loadedModel) ? "model file missing" : (state.settings?.model || "no model loaded");
    set("[data-sb=model]", wbModelShort(model) || model);
    const dot = bar.querySelector("[data-sb=dot]");
    if (dot) { dot.style.setProperty("--wb-dot", wbModelColor(model)); dot.classList.toggle("live", isStreaming); }
    const ctx = computeCtxUsage();
    const pct = Math.round(ctx.pct * 100);
    const cap = ctx.capacity >= 1024 ? `${Math.round(ctx.capacity / 1024)}k` : String(ctx.capacity);
    set("[data-sb=ctx]", `${pct}% of ${cap}`);
    const fill = bar.querySelector("[data-sb=ctxbar]");
    if (fill) {
      fill.style.width = `${Math.min(100, pct)}%`;
      fill.dataset.level = ctx.pct >= 0.9 ? "crit" : ctx.pct >= 0.7 ? "warn" : "";
    }
    set("[data-sb=speed]", statusSpeedText(speed, isStreaming));
    const stateText = state._reloading ? "reloading…" : isStreaming ? "streaming" : (stateStr && stateStr !== "streaming" ? stateStr : "idle");
    set("[data-sb=state] span", stateText);
    bar.querySelector("[data-sb=state]")?.classList.toggle("on", isStreaming);
    wbRenderMeters(ctx);
  }
  function wbApplyStatus(s) {
    if (!s) return;
    wb.status = s;
    const bar = $("#wb-statusbar");
    if (bar) {
      const v = s.vram || {};
      const vram = bar.querySelector("[data-sb=vram]");
      if (vram) { vram.hidden = !(v.total_gb > 0); vram.querySelector("span").textContent = `VRAM ${v.used_gb} / ${v.total_gb} GB`; vram.title = v.name || "GPU memory"; }
      const temp = bar.querySelector("[data-sb=gputemp]");
      if (temp) {
        // Only cards that report a sensor (nvidia-smi); hidden everywhere else.
        const t = v.temp_c == null ? NaN : Number(v.temp_c);
        temp.hidden = !(v.total_gb > 0 && Number.isFinite(t));
        if (!temp.hidden) {
          temp.querySelector("span").textContent = `${Math.round(t)}°C`;
          temp.dataset.level = t >= 90 ? "crit" : t >= 83 ? "warn" : "";
          temp.title = `${v.name || "GPU"} temperature`;
        }
      }
      const backend = bar.querySelector("[data-sb=backend]");
      if (backend) {
        backend.dataset.up = s.llama_up ? "1" : "0";
        backend.querySelector("span:last-child").textContent = s.llama_up ? "llama-server ready" : "llama-server offline";
      }
    }
    const mcp = $("#wb-chip-mcp");
    if (mcp) {
      const m = s.mcp || {};
      mcp.hidden = !(m.configured > 0);
      mcp.querySelector("span").textContent = `MCP ${m.running || 0}/${m.configured || 0}`;
      mcp.title = `${m.running || 0} of ${m.configured || 0} MCP servers running · click to manage`;
      mcp.dataset.level = (m.running || 0) < (m.configured || 0) ? "warn" : "";
    }
    const skills = $("#wb-chip-skills");
    if (skills) {
      const n = Number(s.skills?.count || 0);
      skills.hidden = n < 1;
      skills.querySelector("span").textContent = `Skills ${n}`;
      skills.title = `${n} skill${n === 1 ? "" : "s"} · click to pick one with #`;
    }
    wbRenderMeters();
  }
  function wbRenderMeters(ctxUse) {
    const box = $("#wb-meters");
    if (!box) return;
    const s = wb.status || {};
    const v = s.vram || {};
    const ctx = ctxUse || computeCtxUsage();
    const rows = [];
    if (v.total_gb > 0) rows.push(["VRAM", Math.min(1, v.used_gb / v.total_gb), `${v.used_gb}/${v.total_gb}G`, "vram", "VRAM"]);
    rows.push(["Context", Math.min(1, ctx.pct), `${Math.round(ctx.pct * 100)}%`, "ctx", "CONTEXT"]);
    if (s.tokens_today != null) {
      const n = Number(s.tokens_today) || 0;
      rows.push(["Today", null, n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}k tok` : `${n} tok`, "today", "TOKENS"]);
    }
    // Each meter also carries a ring (shown only in the collapsed rail): the
    // fill as an arc with the percent inside, or just the value for counts.
    // A short title curves over the top of the ring, on a circle sharing its
    // centre (radius 19 = the 16px ring plus a 3px gap).
    const html = rows.map(([label, frac, value, key, arc]) => {
      const pct = frac == null ? 0 : Math.round(frac * 100);
      const level = frac == null ? "" : frac >= 0.9 ? "crit" : frac >= 0.75 ? "warn" : "";
      const short = frac == null ? value.replace(/\s*tok$/, "") : String(pct);
      return `<div class="wb-meter" data-m="${key}" style="--p:${pct}" title="${esc(`${label} ${value}`)}">`
        + `<span class="wb-meter-ring" data-level="${level}" aria-hidden="true"><b>${esc(short)}</b>`
        + `<svg class="wb-meter-arc" viewBox="-28 -28 56 56" focusable="false"><path id="wb-arc-${key}" d="M-19 0A19 19 0 0 1 19 0"/>`
        + `<text><textPath href="#wb-arc-${key}" startOffset="50%">${arc}</textPath></text></svg></span>`
        + `<span class="wb-meter-k">${label}</span>`
        + (frac == null ? `<span class="grow"></span>` : `<span class="wb-meter-bar"><i style="width:${pct}%" data-level="${level}"></i></span>`)
        + `<span class="wb-meter-v">${esc(value)}</span></div>`;
    }).join("");
    // cached string: reading innerHTML back on every streaming tick costs a serialize
    if (box._wbHtml !== html) { box._wbHtml = html; box.innerHTML = html; }
    renderSidebarUsage();
  }
  async function wbPollStatus() {
    if (document.visibilityState !== "visible") return;
    try { wbApplyStatus(await api("/api/workbench/status")); } catch {}
  }
  return { wbRenderStatusbar, wbPollStatus };
};
