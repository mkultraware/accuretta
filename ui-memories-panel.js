/* SPDX-License-Identifier: AGPL-3.0-only */
/* memories panel: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["memories-panel"] = ({
  $, api, closeUsageStats, confirmModal, esc, mcpUi, settingsReturnTarget, toast, wbMcpShown,
}) => {
  function renderMemoriesList(items) {
    const host = $("#mem-list");
    if (!host) return;
    host.innerHTML = "";
    if (!items || !items.length) {
      host.innerHTML = `<div class="mem-empty">No saved preferences or facts yet. Add one below or ask Accuretta to remember it.</div>`;
      return;
    }
    for (const m of items) {
      const row = document.createElement("div");
      row.className = "mem-item";
      const tags = Array.isArray(m.tags) && m.tags.length
        ? `<span class="mem-tags">${m.tags.map(t => `<span class="mem-tag">${esc(t)}</span>`).join("")}</span>`
        : "";
      row.innerHTML = `
        <div class="mem-text">${esc(m.text || "")}</div>
        <div class="mem-foot">
          <select class="mem-kind" aria-label="Memory type">
            <option value="preference" ${m.kind === "preference" ? "selected" : ""}>Standing preference</option>
            <option value="fact" ${m.kind === "fact" ? "selected" : ""}>Searchable fact</option>
          </select>
          ${tags}
          <span class="mem-ts">${m.kind === "preference" ? "Always included" : "Retrieved when needed"}</span>
          <button class="btn ghost sm mem-edit" type="button">Edit</button>
          <button class="btn ghost sm mem-del" type="button" title="Forget" aria-label="Forget memory"><i class="ph ph-trash"></i></button>
        </div>`;
      row.querySelector(".mem-kind").addEventListener("change", async (event) => {
        const control = event.currentTarget;
        control.disabled = true;
        try {
          await api("/api/memories/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: m.id, kind: control.value }) });
          await loadMemories();
        } catch (e) { control.value = m.kind; toast(e.message, "error"); }
        finally { control.disabled = false; }
      });
      row.querySelector(".mem-edit").addEventListener("click", () => {
        if (row.querySelector("textarea")) return;
        const editor = document.createElement("textarea");
        editor.value = m.text || "";
        editor.maxLength = 2000;
        editor.setAttribute("aria-label", "Edit memory");
        const actions = document.createElement("div");
        actions.className = "mem-edit-actions";
        const save = document.createElement("button");
        save.type = "button"; save.className = "btn sm"; save.textContent = "Save";
        const cancel = document.createElement("button");
        cancel.type = "button"; cancel.className = "btn ghost sm"; cancel.textContent = "Cancel";
        cancel.addEventListener("click", () => { editor.remove(); actions.remove(); });
        save.addEventListener("click", async () => {
          save.disabled = true;
          try {
            await api("/api/memories/edit", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: m.id, text: editor.value }) });
            await loadMemories();
          } catch (e) { toast(e.message, "error"); }
          finally { save.disabled = false; }
        });
        actions.append(save, cancel); row.append(editor, actions); editor.focus();
      });
      row.querySelector(".mem-del").addEventListener("click", async () => {
        try {
          await api("/api/memories/forget", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: m.id }),
          });
          await loadMemories();
        } catch (e) { toast("forget failed: " + e.message, "error"); }
      });
      host.appendChild(row);
    }
  }
  async function loadMemories() {
    try {
      const r = await api("/api/memories");
      renderMemoriesList(r.memories || []);
      const profile = r.profile || {};
      const status = $("#mem-profile-status");
      if (status) status.textContent = `${profile.characters || 0} / ${profile.budget || 6000} preference characters. ${profile.warning || "Adding a note never removes another."}`;
      const count = $("#mem-count");
      if (count) count.textContent = `${(r.memories || []).length} saved`;
    } catch (e) {
      const host = $("#mem-list");
      if (host) host.innerHTML = `<div class="mem-empty">(failed: ${esc(e.message || String(e))})</div>`;
    }
  }
  async function addMemoryFromInput() {
    const input = $("#mem-add-text");
    if (!input) return;
    const text = (input.value || "").trim();
    if (!text) return;
    const btn = $("#btn-mem-add");
    if (btn) btn.disabled = true;
    try {
      await api("/api/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, kind: $("#mem-add-kind")?.value || "fact" }),
      });
      input.value = "";
      await loadMemories();
    } catch (e) {
      toast("add failed: " + e.message, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function rescanSystemContext() {
    const btn = $("#btn-sysctx-rescan");
    const ta = $("#set-sysctx");
    if (btn) btn.disabled = true;
    if (ta) ta.value = "scanning…";
    try {
      const r = await api("/api/system-context/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (ta) ta.value = r.md || "";
    } catch (e) {
      if (ta) ta.value = `(rescan failed: ${e.message || e})`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  function closeSettings(force = false) {
    // Unsaved MCP server edits ask first, as their own window did.
    if (!force && mcpUi.dirty && wbMcpShown()) {
      confirmModal({ title: "Discard changes?", message: "This server has unsaved changes.", confirmText: "Discard", danger: true })
        .then(ok => { if (ok) { mcpUi.dirty = false; closeSettings(true); } });
      return;
    }
    clearTimeout(mcpUi.poll);
    closeUsageStats();
    $("#drawer-scrim").classList.remove("open");
    $("#settings-drawer").classList.remove("open");
    settingsReturnTarget()?.focus();
    $("#settings-drawer").setAttribute("aria-hidden", "true");
    // Reset the section search so reopening starts clean.
    const searchInput = $("#settings-search-input");
    if (searchInput && searchInput.value) {
      searchInput.value = "";
      searchInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }
  return { loadMemories, addMemoryFromInput, rescanSystemContext, closeSettings };
};
