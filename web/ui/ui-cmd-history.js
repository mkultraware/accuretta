/* SPDX-License-Identifier: AGPL-3.0-only */
/* Command history drawer: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["cmd-history"] = ({
  $, api, confirmModal, copyText, esc, openSettingsAt, relTime, state, toast,
}) => {
  // Host and WSL guest command audit log (target, tool, chat, exit code,
  // stdout/stderr, duration). Fetches /api/cmd-history on open and renders each
  // entry as a collapsed row with click-to-expand detail, using the Settings
  // scrim mechanism.
  function openCmdHistory() {
    openSettingsAt("usage", "commands");
  }
  function closeCmdHistory() {
    $("#cmd-history-scrim").classList.remove("open");
    $("#cmd-history-drawer").classList.remove("open");
  }
  async function loadCmdHistory() {
    const body = $("#cmd-history-body");
    const countEl = $("#cmd-history-count");
    if (!body) return;
    body.innerHTML = `<div class="cmd-history-empty">Loading…</div>`;
    try {
      const r = await api("/api/cmd-history?limit=300");
      const entries = Array.isArray(r?.entries) ? r.entries : [];
      if (countEl) countEl.textContent = entries.length ? `${entries.length} entries` : "0 entries";
      renderCmdHistory(entries);
    } catch (e) {
      body.innerHTML = `<div class="cmd-history-empty">Failed to load history: ${esc(e.message || String(e))}</div>`;
      if (countEl) countEl.textContent = "—";
    }
  }
  function renderCmdHistory(entries) {
    const body = $("#cmd-history-body");
    if (!body) return;
    if (!entries.length) {
      body.innerHTML = `<div class="cmd-history-empty">No commands recorded yet.<br><br><span style="font-size:11px;">Host PowerShell, project tests, and WSL guest commands show up here after they actually start.</span></div>`;
      return;
    }
    const chatsMap = (state.chats && state.chats.chats) || {};
    const chevSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
    body.innerHTML = entries.map((e, idx) => {
      const okClass = e.spawn_error ? "fail" : (e.timed_out ? "warn" : (e.ok ? "ok" : "fail"));
      const exitLabel = e.spawn_error ? "ERR" : (e.timed_out ? "TIME" : (e.ok ? "OK" : `EXIT ${e.exit ?? "?"}`));
      const cmdPreview = String(e.command || "").split("\n")[0].slice(0, 240);
      // ts comes from the bridge as milliseconds (Python int(time.time() * 1000));
      // relTime expects seconds, so divide.
      const when = e.ts ? relTime(Math.floor(e.ts / 1000)) : "—";
      const chatLabel = e.chat_id ? (chatsMap[e.chat_id]?.title || e.chat_id.slice(0, 8)) : "—";
      const stdoutPre = e.stdout && e.stdout.trim()
        ? `<pre class="cmd-detail-pre">${esc(e.stdout)}</pre>`
        : `<pre class="cmd-detail-pre empty">(empty)</pre>`;
      const stderrPre = e.stderr && e.stderr.trim()
        ? `<pre class="cmd-detail-pre stderr">${esc(e.stderr)}</pre>`
        : `<pre class="cmd-detail-pre empty">(empty)</pre>`;
      const dur = e.duration_ms != null ? `${e.duration_ms.toLocaleString()} ms` : "—";
      const noteLine = e.timed_out ? "<b>Timed out</b> · " : (e.spawn_error ? "<b>Spawn error</b> · " : "");
      const target = e.target === "sandbox" ? "WSL GUEST" : "HOST";
      const targetClass = e.target === "sandbox" ? "is-sandbox" : "is-host";
      return `<div class="cmd-row" data-idx="${idx}">
  <div class="cmd-row-summary">
    <span class="cmd-row-time">${esc(when)}</span>
    <span class="cmd-row-target ${targetClass}">${target}</span>
    <span class="cmd-row-exit ${okClass}">${esc(exitLabel)}</span>
    <span class="cmd-row-cmd" title="${esc(cmdPreview)}">${esc(cmdPreview)}</span>
    <span class="cmd-row-chev">${chevSvg}</span>
  </div>
  <div class="cmd-row-detail">
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">Full command<button class="cmd-detail-copy" data-copy="cmd">Copy</button></div>
      <pre class="cmd-detail-pre" data-cmd>${esc(e.command || "")}</pre>
    </div>
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">stdout<button class="cmd-detail-copy" data-copy="stdout">Copy</button></div>
      ${stdoutPre}
    </div>
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">stderr<button class="cmd-detail-copy" data-copy="stderr">Copy</button></div>
      ${stderrPre}
    </div>
    <div class="cmd-detail-meta">
      ${noteLine}<span><b>Duration:</b> ${esc(dur)}</span>
      <span><b>Chat:</b> ${esc(chatLabel)}</span>
      <span><b>Tool:</b> ${esc(e.tool || "run_powershell")}</span>
      ${e.cwd ? `<span><b>Folder:</b> ${esc(e.cwd)}</span>` : ""}
    </div>
  </div>
</div>`;
    }).join("");
    // wire row expand + copy buttons (delegated)
    body.querySelectorAll(".cmd-row-summary").forEach(sum => {
      sum.addEventListener("click", () => sum.parentElement.classList.toggle("expanded"));
    });
    body.querySelectorAll(".cmd-detail-copy").forEach(btn => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const what = btn.dataset.copy;
        const row = btn.closest(".cmd-row");
        if (!row) return;
        const idx = Number(row.dataset.idx);
        const e = entries[idx];
        let text = "";
        if (what === "cmd") text = e.command || "";
        else if (what === "stdout") text = e.stdout || "";
        else if (what === "stderr") text = e.stderr || "";
        if (!text) { toast("nothing to copy", "warn", 1500); return; }
        copyText(text).then(
          () => { toast("copied", "ok", 1500); },
          () => { toast("clipboard failed", "error", 2000); },
        );
      });
    });
  }
  async function clearCmdHistory() {
    const ok = await confirmModal({
      title: "Clear command history",
      message: "Clear all host and WSL guest command history? This can't be undone.",
      confirmText: "Clear history",
      danger: true,
      icon: "ph-trash",
    });
    if (!ok) return;
    try {
      await api("/api/cmd-history", { method: "DELETE" });
      toast("history cleared", "ok", 1500);
      await loadCmdHistory();
    } catch (e) {
      toast("clear failed: " + (e.message || e), "error");
    }
  }

  function normalizedMmprojMode(settings = {}) {
    const mode = String(settings.mmproj_mode || "").trim().toLowerCase();
    if (["off", "auto", "manual"].includes(mode)) return mode;
    if (String(settings.mmproj_path || "").trim()) return "manual";
    return settings.mmproj_auto === false ? "off" : "auto";
  }

  function syncMmprojModeUi() {
    const modeSel = $("#set-mmproj-mode");
    const input = $("#set-mmproj");
    const hint = $("#set-mmproj-hint");
    const mode = modeSel?.value || "off";
    if (input) input.disabled = mode !== "manual";
    const device = $("#set-mmproj-device");
    if (device) device.disabled = mode === "off";
    if (!hint) return;
    if (mode === "off") {
      hint.textContent = state.visionCapable
        ? "native vision is currently active; save settings to unload it."
        : "projector off. no mmproj will load, even if one is beside the model.";
    } else if (mode === "auto") {
      hint.textContent = "the app will choose a matching projector beside this model when it reloads.";
    } else if (input?.value.trim()) {
      hint.textContent = "manual projector selected. save settings to load it.";
    } else {
      hint.textContent = "choose or paste a projector .gguf path, then save settings.";
    }
  }
  return {
    openCmdHistory, closeCmdHistory, loadCmdHistory, clearCmdHistory, normalizedMmprojMode,
    syncMmprojModeUi,
  };
};
