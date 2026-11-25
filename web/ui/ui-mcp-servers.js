/* SPDX-License-Identifier: AGPL-3.0-only */
/* MCP servers window: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["mcp-servers"] = ({
  $, confirmModal, esc, mcpPop, mcpUi, openSettingsAt, toast, wbMcpPopRender, wbMcpShown,
  wbPollStatus,
}) => {
  // Front end for bridge_mcp_config.json: list, add (form or a pasted README
  // snippet), edit, turn on/off, restart and remove servers. The bridge starts
  // and stops them live. Saved env values never come back here, so a saved field
  // shows "saved" and an empty input means "keep it".

  function wbMcpOpen(select) {
    mcpUi.selected = select || mcpUi.selected || "";
    mcpUi.dirty = false;
    openSettingsAt("tools");
  }
  function wbMcpClose() {
    if (!$("#mcp-window")?.classList.contains("open")) return;
    const close = () => {
      const win = $("#mcp-window");
      win.classList.remove("open");
      win.setAttribute("aria-hidden", "true");
      $("#mcp-scrim")?.classList.remove("open");
      clearTimeout(mcpUi.poll);
      mcpUi.dirty = false;
      wbPollStatus();
    };
    if (!mcpUi.dirty) { close(); return; }
    confirmModal({ title: "Discard changes?", message: "This server has unsaved changes.", confirmText: "Discard", danger: true })
      .then(ok => { if (ok) close(); });
  }
  async function wbMcpLoad() {
    clearTimeout(mcpUi.poll);
    let data;
    try {
      const r = await fetch("/api/mcp/servers");
      data = await r.json();
      if (!r.ok) data = { servers: [], error: data.error || `HTTP ${r.status}`, unavailable: true };
    } catch (e) {
      data = { servers: [], error: String(e.message || e), unavailable: true };
    }
    wbMcpApply(data);
  }
  function wbMcpApply(data) {
    mcpUi.data = data;
    const names = (data.servers || []).map(s => s.name);
    if (mcpUi.selected !== "__new" && !names.includes(mcpUi.selected)) mcpUi.selected = names[0] || "";
    wbMcpRenderList();
    // Don't rebuild a form the user is typing into; just refresh its status.
    if (mcpUi.dirty) wbMcpRenderStatus(); else wbMcpRenderDetail();
    if (mcpPop.open) wbMcpPopRender();
    if ((data.servers || []).some(s => s.state === "starting") && (wbMcpShown() || mcpPop.open)) {
      mcpUi.poll = setTimeout(wbMcpLoad, 1200);
    }
  }
  function wbMcpState(s) {
    if (s.state === "running") return { label: `${s.tools.length} tool${s.tools.length === 1 ? "" : "s"}`, text: "Running" };
    if (s.state === "starting") return { label: "starting…", text: "Starting" };
    if (s.state === "error") return { label: "failed", text: "Couldn't start" };
    if (s.state === "disabled") return { label: "off", text: "Turned off" };
    return { label: "stopped", text: "Not running" };
  }
  function wbMcpRenderList() {
    const list = $("#mcp-list");
    if (!list) return;
    const servers = mcpUi.data?.servers || [];
    list.innerHTML = servers.map(s => {
      const st = wbMcpState(s);
      return `<button type="button" class="mcp-row${s.name === mcpUi.selected ? " active" : ""}" data-name="${esc(s.name)}">`
        + `<span class="mcp-dot" data-state="${esc(s.state)}" aria-hidden="true"></span>`
        + `<span class="mcp-row-name">${esc(s.name)}</span>`
        + (s.unsandboxed ? `<i class="ph ph-warning mcp-row-warn" title="Runs its browser without the sandbox" aria-label="Not sandboxed"></i>` : "")
        + `<span class="mcp-row-meta">${esc(st.label)}</span></button>`;
    }).join("") + (mcpUi.selected === "__new"
      ? `<button type="button" class="mcp-row active" data-name="__new"><span class="mcp-dot" aria-hidden="true"></span><span class="mcp-row-name">New server</span></button>` : "");
    $("#mcp-add").hidden = mcpUi.data?.can_edit === false || !!mcpUi.data?.unavailable;
  }
  function wbMcpCurrent() {
    return (mcpUi.data?.servers || []).find(s => s.name === mcpUi.selected) || null;
  }
  function wbMcpStatusHtml(s) {
    if (!s) return "";
    const st = wbMcpState(s);
    const canEdit = mcpUi.data?.can_edit !== false;
    const detail = s.state === "error" && s.error ? `<p class="mcp-status-error">${esc(s.error)}</p>` : "";
    const log = s.stderr?.length ? `<details class="mcp-log"><summary>Server output</summary><pre>${esc(s.stderr.join("\n"))}</pre></details>` : "";
    return `<div class="mcp-status" data-state="${esc(s.state)}">`
      + `<span class="mcp-dot" data-state="${esc(s.state)}" aria-hidden="true"></span>`
      + `<div class="mcp-status-text"><strong>${esc(st.text)}</strong>`
      + `<span>${s.state === "running" ? `${esc(st.label)} available to the agent` : s.state === "disabled" ? "Its tools are hidden from the agent" : s.state === "starting" ? "Launching and asking for its tools" : "Its tools are unavailable"}</span></div>`
      + (canEdit && s.state !== "disabled" ? `<button type="button" class="btn ghost sm" data-mcp="restart"${s.state === "starting" ? " disabled" : ""}><i class="ph ph-arrow-clockwise"></i>Restart</button>` : "")
      + (canEdit ? `<button type="button" class="sw${s.disabled ? "" : " on"}" data-mcp="toggle" role="switch" aria-checked="${!s.disabled}" title="${s.disabled ? "Turn on" : "Turn off"}" aria-label="${s.disabled ? "Turn on" : "Turn off"} ${esc(s.name)}"></button>` : "")
      + `</div>${detail}${s.unsandboxed ? MCP_UNSANDBOXED_WARNING : ""}${log}`;
  }
  // Shown for a server whose browser runs with --no-sandbox (or Playwright
  // MCP's PLAYWRIGHT_MCP_NO_SANDBOX). Accuretta still runs it as configured.
  const MCP_UNSANDBOXED_WARNING = `<div class="mcp-warn" role="note"><i class="ph ph-warning" aria-hidden="true"></i><div>`
    + `<strong>This server's browser isn't sandboxed</strong>`
    + `<p>It runs with <code>--no-sandbox</code>, so every page it opens has the same access to this PC as you do. `
    + `A malicious or hacked site that exploits a browser bug could then read or change your files, take saved logins and API keys, and run programs, `
    + `instead of being stuck inside the browser. Pages the agent visits on its own are the riskiest.</p>`
    + `<p>Remove <code>--no-sandbox</code> from its arguments unless it runs inside a container or VM that needs it.</p></div></div>`;
  function wbMcpRenderStatus() {
    const box = $("#mcp-detail .mcp-status-slot");
    if (box) box.innerHTML = wbMcpStatusHtml(wbMcpCurrent());
  }
  function wbMcpEnvRow(key = "", saved = false) {
    return `<div class="mcp-env-row"><input class="mcp-input mcp-env-key" value="${esc(key)}" placeholder="NAME" spellcheck="false" autocomplete="off" aria-label="Variable name">`
      + `<input class="mcp-input mcp-env-val" type="password" data-saved="${saved ? 1 : 0}" placeholder="${saved ? "saved · leave empty to keep" : "value"}" spellcheck="false" autocomplete="new-password" aria-label="Value">`
      + `<button type="button" class="iconbtn sm" data-mcp="env-del" title="Remove variable" aria-label="Remove variable"><i class="ph ph-x"></i></button></div>`;
  }
  function wbMcpRenderDetail() {
    const box = $("#mcp-detail");
    if (!box) return;
    const data = mcpUi.data || {};
    if (data.unavailable || data.error && !(data.servers || []).length) {
      box.innerHTML = `<div class="mcp-empty"><i class="ph ph-plugs" aria-hidden="true"></i><strong>MCP servers aren't available</strong><p>${esc(data.error || "")}</p></div>`;
      return;
    }
    const s = wbMcpCurrent();
    const isNew = mcpUi.selected === "__new";
    if (!s && !isNew) {
      box.innerHTML = `<div class="mcp-empty"><i class="ph ph-plugs" aria-hidden="true"></i><strong>No MCP servers yet</strong>`
        + `<p>MCP servers give the agent extra tools: a browser, a database, Blender, an OSINT API and so on. Add one by its command, or paste the JSON snippet from its README.</p>`
        + (data.can_edit === false ? "" : `<button type="button" class="btn accent sm" data-mcp="new"><i class="ph ph-plus"></i>Add server</button>`) + `</div>`;
      return;
    }
    const canEdit = data.can_edit !== false;
    const ro = canEdit ? "" : " readonly";
    const tools = s?.tools?.length
      ? `<details class="mcp-tools"><summary>${s.tools.length} tool${s.tools.length === 1 ? "" : "s"}</summary><ul>${s.tools.map(t => `<li><code>${esc(t.name)}</code><span>${esc(t.description)}</span></li>`).join("")}</ul></details>` : "";
    box.innerHTML = `<form class="mcp-form" id="mcp-form" autocomplete="off">`
      + (isNew ? `<div class="mcp-seg" role="tablist"><button type="button" role="tab" data-mcp="mode-form" aria-selected="${!mcpUi.paste}">Fill in</button><button type="button" role="tab" data-mcp="mode-paste" aria-selected="${mcpUi.paste}">Paste JSON</button></div>` : "")
      + (data.error ? `<p class="mcp-status-error">${esc(data.error)}</p>` : "")
      + (canEdit ? "" : `<p class="mcp-note"><i class="ph ph-lock-simple"></i>Servers can only be changed on the machine running Accuretta.</p>`)
      + `<div class="mcp-status-slot">${wbMcpStatusHtml(s)}</div>`
      + (isNew && mcpUi.paste
        ? `<label class="mcp-field"><span>Snippet from the server's README</span><textarea class="mcp-input mcp-paste" name="paste" rows="10" spellcheck="false" placeholder='{\n  "mcpServers": {\n    "playwright": {\n      "command": "npx",\n      "args": ["@playwright/mcp@latest"]\n    }\n  }\n}'></textarea><small>Several servers at once are fine. A name that already exists is replaced.</small></label>`
        : `<label class="mcp-field"><span>Name</span><input class="mcp-input" name="name" value="${esc(s?.name || "")}" placeholder="playwright" spellcheck="false" maxlength="40" required${ro}><small>Tools show up as mcp_&lt;name&gt;_&lt;tool&gt;. Letters, numbers, - and _.</small></label>`
        + `<label class="mcp-field"><span>Command</span><input class="mcp-input mono" name="command" value="${esc(s?.command || "")}" placeholder="npx" spellcheck="false" required${ro}><small>The program that starts the server, like npx, uvx, python or a full path.</small></label>`
        + `<label class="mcp-field"><span>Arguments</span><textarea class="mcp-input mono" name="args" rows="3" spellcheck="false" placeholder="-y&#10;@playwright/mcp@latest"${ro}>${esc((s?.args || []).join("\n"))}</textarea><small>One per line.</small></label>`
        + `<div class="mcp-field"><span>Environment variables</span><div class="mcp-env" id="mcp-env">${(s?.env || []).map(e => wbMcpEnvRow(e.key, e.set)).join("")}</div>`
        + (canEdit ? `<button type="button" class="btn ghost sm mcp-env-add" data-mcp="env-add"><i class="ph ph-plus"></i>Add variable</button>` : "")
        + `<small>API keys and other values stay on this machine and are never shown again after saving.</small></div>`)
      + tools
      + (canEdit ? `<footer class="mcp-foot">`
        + (s ? `<button type="button" class="btn danger sm" data-mcp="delete"><i class="ph ph-trash"></i>Remove</button>` : "")
        + `<span class="grow"></span>`
        + (isNew ? `<button type="button" class="btn ghost sm" data-mcp="cancel">Cancel</button>` : "")
        + `<button type="submit" class="btn accent sm" data-mcp="save"${isNew ? "" : " disabled"}>${isNew ? (mcpUi.paste ? "Import" : "Add and start") : "Save and restart"}</button></footer>` : "")
      + `</form>`;
    const first = box.querySelector(isNew ? (mcpUi.paste ? ".mcp-paste" : "[name=name]") : null);
    if (first) setTimeout(() => first.focus(), 30);
  }
  function wbMcpFormBody(form, s) {
    const env = {};
    for (const row of form.querySelectorAll(".mcp-env-row")) {
      const key = row.querySelector(".mcp-env-key").value.trim();
      if (!key) continue;
      const val = row.querySelector(".mcp-env-val");
      env[key] = val.value ? val.value : (val.dataset.saved === "1" ? null : "");
    }
    return {
      name: form.elements.name.value.trim(),
      original: s?.name || "",
      command: form.elements.command.value.trim(),
      args: form.elements.args.value.split(/\r?\n/).map(a => a.trim()).filter(Boolean),
      env,
      disabled: !!s?.disabled,
    };
  }
  async function wbMcpPost(path, body, okText) {
    try {
      const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await r.json();
      if (!r.ok) { toast(data.error || `HTTP ${r.status}`, "err", 4200, "mcp"); return null; }
      mcpUi.dirty = false;
      if (okText) toast(okText, "ok", 2200, "mcp");
      return data;
    } catch (e) {
      toast(String(e.message || e), "err", 4200, "mcp");
      return null;
    }
  }
  // Saving a server with its env keys set to null keeps their stored values.
  function wbMcpSetDisabled(s, disabled) {
    const env = Object.fromEntries((s.env || []).map(v => [v.key, null]));
    return wbMcpPost("/api/mcp/servers", { name: s.name, original: s.name, command: s.command, args: s.args, env, disabled },
      disabled ? `${s.name} turned off` : `${s.name} is starting`);
  }
  return {
    wbMcpOpen, wbMcpClose, wbMcpLoad, wbMcpApply, wbMcpRenderList, wbMcpCurrent, wbMcpEnvRow,
    wbMcpRenderDetail, wbMcpFormBody, wbMcpPost, wbMcpSetDisabled,
  };
};
