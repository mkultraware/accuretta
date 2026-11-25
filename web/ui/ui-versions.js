/* SPDX-License-Identifier: AGPL-3.0-only */
/* versions / preview: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["versions"] = ({
  $, api, app, esc, humanBytes, injectMobilePreviewCard, isMobile, maybeUnescapeJsonFence, relTime,
  renderPreview, state, toast,
}) => {
  async function loadVersions() {
    if (!state.chatId) return;
    try {
      const r = await api(`/api/versions/${state.chatId}`);
      state.versions = r.versions || [];
    } catch { state.versions = []; }
    renderVersions();
    if (state.versions.length) {
      setActiveVersion(state.versions[state.versions.length - 1].id);
    } else {
      clearPreview();
    }
  }

  function renderVersions() {
    const bar = $("#version-bar");
    bar.innerHTML = "";
    if (!state.versions.length) {
      bar.innerHTML = `<span id="versions-empty" style="color:var(--fg-faint)">no versions yet</span><span class="spacer"></span>`;
      return;
    }
    const maxVisible = state._versionsExpanded ? Infinity : 8;
    const hiddenCount = Math.max(0, state.versions.length - maxVisible);
    const visible = state._versionsExpanded ? state.versions : state.versions.slice(-maxVisible);
    if (hiddenCount > 0) {
      const expand = document.createElement("button");
      expand.className = "version-chip";
      expand.innerHTML = `<span class="n">…</span><span style="opacity:.6">+${hiddenCount} more</span>`;
      expand.title = `Show all ${state.versions.length} versions`;
      expand.addEventListener("click", () => {
        state._versionsExpanded = true;
        renderVersions();
      });
      bar.appendChild(expand);
    }
    for (const v of visible) {
      const wrap = document.createElement("span");
      wrap.className = "version-wrap" + (v.id === state.activeVersion ? " active" : "");
      const chip = document.createElement("button");
      chip.className = "version-chip" + (v.id === state.activeVersion ? " active" : "");
      chip.innerHTML = `<span class="n">v${String(v.n).padStart(2, "0")}</span>${v.label ? `<span style="opacity:.6">· ${esc(v.label).slice(0, 32)}</span>` : ""}`;
      chip.title = `${v.id} · ${humanBytes(v.bytes)} · ${relTime(v.t)}`;
      chip.addEventListener("click", () => setActiveVersion(v.id));
      wrap.appendChild(chip);
      const rerun = document.createElement("button");
      rerun.className = "version-rerun";
      rerun.type = "button";
      rerun.title = "Re-run: reload this version into the preview";
      rerun.innerHTML = `<i class="ph ph-arrow-counter-clockwise"></i>`;
      rerun.addEventListener("click", (e) => {
        e.stopPropagation();
        setActiveVersion(v.id);
        toast(`v${String(v.n).padStart(2, "0")} re-loaded`, "info", 1600, "vrerun");
      });
      wrap.appendChild(rerun);
      bar.appendChild(wrap);
    }
    const spacer = document.createElement("span");
    spacer.className = "spacer";
    bar.appendChild(spacer);
  }

  async function setActiveVersion(vid) {
    state.activeVersion = vid;
    // user is opening a model-generated version → leave workspace-preview mode
    state.workspacePreview = null;
    const resp = await fetch(`/api/versions/${state.chatId}/${vid}`);
    let html = await resp.text();
    // safety net for older versions saved before the bridge unescape pass
    html = maybeUnescapeJsonFence(html);
    state.currentHtml = html;
    // companion-file map is per-turn; switching to a persisted version clears it
    state.currentFiles = {};
    const v = state.versions.find(x => x.id === vid);
    $("#preview-url").textContent = vid;
    $("#preview-meta").textContent = v ? `v${String(v.n).padStart(2, "0")} · ${relTime(v.t)}` : "—";
    $("#preview-size").textContent = humanBytes((html || "").length);
    renderPreview();
    renderVersions();
    // auto-open preview pane if collapsed (desktop only)
    if (app.classList.contains("preview-collapsed") && !isMobile()) {
      app.classList.remove("preview-collapsed");
    }
    // On mobile: inject a tappable artifact card in the chat
    if (isMobile() && html) {
      injectMobilePreviewCard({
        filename: v ? `v${String(v.n).padStart(2, "0")} preview` : "preview",
        size: html.length,
        html,
      });
    }
  }

  function clearPreview() {
    state.currentHtml = "";
    state.currentFiles = {};
    state.activeVersion = null;
    state.workspacePreview = null;
    $("#preview-url").textContent = "—";
    $("#preview-meta").textContent = "—";
    $("#preview-size").textContent = "—";
    $("#preview-frame").classList.add("hidden");
    $("#preview-stage").classList.add("hidden");
    $("#code-view").classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    $("#preview-empty").classList.remove("hidden");
    renderVersions();
  }

  function injectCspIfNeeded(html) {
    if (state.settings.allow_web_preview !== false) return html;
    // when Tailwind CDN is on we must relax CSP enough to load it, otherwise the script is blocked.
    const scriptSrc = state.settings.use_tailwind_cdn
      ? "'unsafe-inline' 'self' data: https://cdn.tailwindcss.com"
      : "'unsafe-inline' 'self' data:";
    const styleSrc = state.settings.use_tailwind_cdn
      ? "'unsafe-inline' 'self' data: https://cdn.tailwindcss.com"
      : "'unsafe-inline' 'self' data:";
    const connectSrc = state.settings.use_tailwind_cdn
      ? "'self' https://cdn.tailwindcss.com"
      : "'self'";
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'self' data: blob:; style-src ${styleSrc}; script-src ${scriptSrc}; img-src 'self' data: blob: https:; font-src 'self' data: https:; connect-src ${connectSrc};">`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + csp);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + csp + "</head>");
    return csp + html;
  }

  function injectTailwindIfNeeded(html) {
    if (!state.settings.use_tailwind_cdn) return html;
    // idempotent — bail out if the doc already pulls in Tailwind
    if (/cdn\.tailwindcss\.com/i.test(html)) return html;
    const tag = `<script src="https://cdn.tailwindcss.com"></script>`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + tag);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + tag + "</head>");
    return tag + html;
  }
  return {
    loadVersions, renderVersions, setActiveVersion, clearPreview, injectCspIfNeeded,
    injectTailwindIfNeeded,
  };
};
