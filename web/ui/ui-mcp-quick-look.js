/* SPDX-License-Identifier: AGPL-3.0-only */
/* MCP quick look: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["mcp-quick-look"] = ({
  $, confirmModal, esc, mcpPop, mcpUi, state, toast, wbMcpApply, wbMcpClose, wbMcpCurrent,
  wbMcpEnvRow, wbMcpFormBody, wbMcpLoad, wbMcpOpen, wbMcpPost, wbMcpRenderDetail, wbMcpRenderList,
  wbMcpSetDisabled,
}) => {
  // The top-bar MCP chip opens this: every server at a glance (state, tool
  // count and roughly what its tool definitions cost in context), with Retry
  // and Turn on in place. Edit, Add server and Manage hand off to the full
  // MCP servers window, which stays the one place servers are configured.

  function wbMcpTokens(s) {
    if (Number(s.tokens) > 0) return Number(s.tokens);
    // Bridges older than the tokens field: estimate from names and descriptions.
    return (s.tools || []).reduce((n, t) => n + 40 + Math.ceil((String(t.name || "").length + String(t.description || "").length) / 4), 0);
  }
  function wbFmtTokens(n) {
    return n >= 1000 ? `${(n / 1000).toFixed(n >= 9950 ? 0 : 1)}k` : String(Math.round(n));
  }
  function wbMcpPopEl() {
    let pop = $("#mcp-pop");
    if (pop) return pop;
    pop = document.createElement("section");
    pop.id = "mcp-pop";
    pop.className = "mcp-pop";
    pop.hidden = true;
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "MCP servers");
    pop.innerHTML = `<header class="mcp-pop-head"><span class="mcp-pop-title"><i class="ph ph-plugs" aria-hidden="true"></i>MCP servers</span>`
      + `<button type="button" class="mcp-pop-btn" data-pop="add"><i class="ph ph-plus" aria-hidden="true"></i>Add server</button></header>`
      + `<div class="mcp-pop-sum"></div><div class="mcp-pop-list" role="list"></div>`
      + `<footer class="mcp-pop-foot"><button type="button" class="mcp-pop-link" data-pop="manage">Manage servers<i class="ph ph-arrow-up-right" aria-hidden="true"></i></button><span>Saved on this machine</span></footer>`;
    document.body.appendChild(pop);
    pop.addEventListener("click", wbMcpPopClick);
    pop.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === " ") && e.target.matches(".mcp-pop-line")) { e.preventDefault(); wbMcpPopClick(e); }
    });
    document.addEventListener("pointerdown", (e) => {
      if (mcpPop.open && !e.target.closest("#mcp-pop, #wb-chip-mcp")) wbMcpPopClose();
    }, true);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && mcpPop.open) { wbMcpPopClose(); $("#wb-chip-mcp")?.focus(); }
    });
    window.addEventListener("resize", () => { if (mcpPop.open) wbMcpPopPlace(); });
    return pop;
  }
  function wbMcpPopPlace() {
    const pop = $("#mcp-pop");
    const chip = $("#wb-chip-mcp");
    if (!pop || !chip) return;
    const r = chip.getBoundingClientRect();
    pop.style.top = `${Math.round(r.bottom + 8)}px`;
    pop.style.right = `${Math.max(12, Math.round(window.innerWidth - r.right))}px`;
  }
  function wbMcpPopOpen() {
    const pop = wbMcpPopEl();
    mcpPop.open = true;
    wbMcpPopPlace();
    pop.hidden = false;
    requestAnimationFrame(() => pop.classList.add("open"));
    $("#wb-chip-mcp")?.setAttribute("aria-expanded", "true");
    wbMcpPopRender();
    wbMcpLoad();
  }
  function wbMcpPopClose() {
    const pop = $("#mcp-pop");
    if (!pop || !mcpPop.open) return;
    mcpPop.open = false;
    pop.classList.remove("open");
    $("#wb-chip-mcp")?.setAttribute("aria-expanded", "false");
    setTimeout(() => { if (!mcpPop.open) pop.hidden = true; }, 160);
  }
  function wbMcpPopStatus(st) {
    return ({
      running: ["ok", "ph-check-circle", "Connected"],
      starting: ["busy", "ph-circle-notch", "Starting"],
      error: ["err", "ph-warning-circle", "Error"],
      disabled: ["off", "ph-pause-circle", "Off"],
    })[st] || ["off", "ph-stop-circle", "Not running"];
  }
  function wbMcpPopRender() {
    const pop = $("#mcp-pop");
    if (!pop) return;
    const data = mcpUi.data;
    const sum = pop.querySelector(".mcp-pop-sum");
    const list = pop.querySelector(".mcp-pop-list");
    if (!data) {
      sum.innerHTML = "";
      list.innerHTML = `<p class="mcp-pop-empty">Loading servers…</p>`;
      return;
    }
    const servers = data.servers || [];
    const canEdit = data.can_edit !== false && !data.unavailable;
    pop.querySelector('[data-pop="add"]').hidden = !canEdit;
    if (data.unavailable) {
      sum.innerHTML = "";
      list.innerHTML = `<p class="mcp-pop-empty"><strong>MCP servers aren't available</strong>${esc(data.error || "")}</p>`;
      return;
    }
    const running = servers.filter(s => s.state === "running");
    const tools = running.reduce((n, s) => n + (s.tools || []).length, 0);
    const tokens = running.reduce((n, s) => n + wbMcpTokens(s), 0);
    const cap = Math.max(1, Number(state._ctxCapacity) || Number(state.settings?.num_ctx) || 32768);
    sum.innerHTML = `<div class="mcp-pop-sumline"><span><strong>${tools}</strong> tool${tools === 1 ? "" : "s"} · <strong>≈${wbFmtTokens(tokens)}</strong> tokens of context</span>`
      + `<span class="mcp-pop-cap">${cap >= 1024 ? `${Math.round(cap / 1024)}k` : cap} window</span></div>`
      + `<div class="mcp-pop-bar" role="img" aria-label="MCP tools use about ${Math.round(tokens / cap * 100)}% of the context window">`
      + running.map(s => {
        const t = wbMcpTokens(s);
        return `<i style="width:${Math.max(0.8, t / cap * 100).toFixed(2)}%" title="${esc(s.name)} · ≈${wbFmtTokens(t)} tokens"></i>`;
      }).join("") + `</div>`;
    if (!servers.length) {
      list.innerHTML = `<p class="mcp-pop-empty"><strong>No MCP servers yet</strong>They give the agent extra tools: a browser, a database, Blender and so on.</p>`;
      return;
    }
    list.innerHTML = servers.map(s => {
      const [tone, icon, text] = wbMcpPopStatus(s.state);
      const open = mcpPop.expanded.has(s.name);
      const n = (s.tools || []).length;
      let right = "";
      if (s.state === "running") {
        right = `<span class="mcp-pop-count">${n} tool${n === 1 ? "" : "s"}</span><strong class="mcp-pop-tok">${wbFmtTokens(wbMcpTokens(s))}</strong>`;
      } else if (canEdit && (s.state === "error" || s.state === "stopped")) {
        right = `<button type="button" class="mcp-pop-btn" data-pop="retry">${s.state === "error" ? "Retry" : "Start"}</button>`;
      } else if (canEdit && s.state === "disabled") {
        right = `<button type="button" class="mcp-pop-btn" data-pop="enable">Turn on</button>`;
      }
      const more = !open ? "" : `<div class="mcp-pop-more">`
        + (s.state === "error" && s.error ? `<p class="mcp-pop-err">${esc(s.error)}</p>` : "")
        + (n ? `<ul class="mcp-pop-tools">${s.tools.map(t => `<li title="${esc(t.description || "")}">${esc(t.name)}</li>`).join("")}</ul>`
          : s.state === "running" ? `<p class="mcp-pop-note">This server offers no tools.</p>` : "")
        + `<div class="mcp-pop-actions"><code>${esc([s.command, ...(s.args || [])].join(" "))}</code>`
        + `<button type="button" class="mcp-pop-btn" data-pop="edit"><i class="ph ph-pencil-simple" aria-hidden="true"></i>${canEdit ? "Edit" : "Details"}</button></div></div>`;
      return `<div class="mcp-pop-row${open ? " open" : ""}" data-name="${esc(s.name)}" data-tone="${tone}" role="listitem">`
        + `<div class="mcp-pop-line" data-pop="toggle" role="button" tabindex="0" aria-expanded="${open}">`
        + `<span class="mcp-pop-ico" aria-hidden="true">${esc((s.name[0] || "?").toUpperCase())}</span>`
        + `<span class="mcp-pop-name"><strong>${esc(s.name)}</strong><small><i class="ph ${icon}" aria-hidden="true"></i>${text}<span>· ${s.remote ? "Remote" : "Local"}</span></small></span>`
        + right + `<i class="ph ph-caret-down mcp-pop-caret" aria-hidden="true"></i></div>${more}</div>`;
    }).join("");
  }
  async function wbMcpPopClick(e) {
    const el = e.target.closest("[data-pop]");
    if (!el) return;
    const act = el.dataset.pop;
    const name = el.closest(".mcp-pop-row")?.dataset.name || "";
    if (act === "toggle") {
      if (mcpPop.expanded.has(name)) mcpPop.expanded.delete(name); else mcpPop.expanded.add(name);
      wbMcpPopRender();
      $(`#mcp-pop .mcp-pop-row[data-name="${CSS.escape(name)}"] .mcp-pop-line`)?.focus();
      return;
    }
    if (act === "add" || act === "manage" || act === "edit") {
      wbMcpPopClose();
      wbMcpOpen(act === "add" ? "__new" : act === "edit" ? name : "");
      return;
    }
    const s = (mcpUi.data?.servers || []).find(x => x.name === name);
    if (!s) return;
    el.disabled = true;
    const data = act === "retry" ? await wbMcpPost("/api/mcp/servers/restart", { name })
      : act === "enable" ? await wbMcpSetDisabled(s, false) : null;
    if (data) wbMcpApply(data); else el.disabled = false;
  }

  function wbInitMcpWindow() {
    const win = $("#mcp-window");
    if (!win || win._wired) return;
    win._wired = true;
    $("#mcp-close")?.addEventListener("click", wbMcpClose);
    $("#mcp-scrim")?.addEventListener("click", wbMcpClose);
    $("#btn-open-mcp")?.addEventListener("click", () => wbMcpOpen());
    const pick = async (name) => {
      if (name === mcpUi.selected) return;
      if (mcpUi.dirty && !(await confirmModal({ title: "Discard changes?", message: "This server has unsaved changes.", confirmText: "Discard", danger: true }))) return;
      mcpUi.selected = name;
      mcpUi.dirty = false;
      mcpUi.paste = false;
      wbMcpRenderList();
      wbMcpRenderDetail();
    };
    $("#mcp-add")?.addEventListener("click", () => pick("__new"));
    $("#mcp-list")?.addEventListener("click", (e) => {
      const row = e.target.closest(".mcp-row");
      if (row) pick(row.dataset.name);
    });
    const detail = $("#mcp-detail");
    detail.addEventListener("input", (e) => {
      if (!e.target.closest("#mcp-form")) return;
      mcpUi.dirty = true;
      const save = detail.querySelector('[data-mcp="save"]');
      if (save) save.disabled = false;
    });
    detail.addEventListener("click", async (e) => {
      const btn = e.target.closest("[data-mcp]");
      if (!btn) return;
      const act = btn.dataset.mcp;
      const s = wbMcpCurrent();
      if (act === "new") return pick("__new");
      if (act === "cancel") { mcpUi.dirty = false; mcpUi.selected = ""; wbMcpApply(mcpUi.data); return; }
      if (act === "mode-form" || act === "mode-paste") {
        mcpUi.paste = act === "mode-paste";
        mcpUi.dirty = false;
        wbMcpRenderDetail();
        return;
      }
      if (act === "env-add") {
        $("#mcp-env").insertAdjacentHTML("beforeend", wbMcpEnvRow());
        $("#mcp-env").lastElementChild.querySelector(".mcp-env-key").focus();
        return;
      }
      if (act === "env-del") {
        btn.closest(".mcp-env-row").remove();
        mcpUi.dirty = true;
        const save = detail.querySelector('[data-mcp="save"]');
        if (save) save.disabled = false;
        return;
      }
      if (!s) return;
      if (act === "restart") {
        const data = await wbMcpPost("/api/mcp/servers/restart", { name: s.name });
        if (data) wbMcpApply(data);
      } else if (act === "toggle") {
        const data = await wbMcpSetDisabled(s, !s.disabled);
        if (data) wbMcpApply(data);
      } else if (act === "delete") {
        const ok = await confirmModal({ title: `Remove ${s.name}?`, message: "Its process stops and its entry, including saved environment values, is deleted from bridge_mcp_config.json.", confirmText: "Remove", danger: true });
        if (!ok) return;
        const data = await wbMcpPost("/api/mcp/servers/delete", { name: s.name }, `${s.name} removed`);
        if (data) { mcpUi.selected = ""; wbMcpApply(data); }
      }
    });
    detail.addEventListener("submit", async (e) => {
      e.preventDefault();
      const form = e.target;
      const s = wbMcpCurrent();
      const save = form.querySelector('[data-mcp="save"]');
      if (save) save.disabled = true;
      let data;
      if (mcpUi.selected === "__new" && mcpUi.paste) {
        data = await wbMcpPost("/api/mcp/import", { text: form.elements.paste.value });
        if (data) {
          toast(`Added ${data.imported.join(", ")}`, "ok", 2400, "mcp");
          mcpUi.selected = data.imported[0];
          mcpUi.paste = false;
        }
      } else {
        const body = wbMcpFormBody(form, s);
        data = await wbMcpPost("/api/mcp/servers", body, s ? `${body.name} saved` : `${body.name} added`);
        if (data) mcpUi.selected = body.name;
      }
      if (data) wbMcpApply(data); else if (save) save.disabled = false;
    });
  }
  return { wbMcpPopOpen, wbMcpPopClose, wbMcpPopRender, wbInitMcpWindow };
};
