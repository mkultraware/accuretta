/* SPDX-License-Identifier: AGPL-3.0-only */
/* settings drawer + Settings > Email: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["settings"] = ({
  $, SHORTCUT_ACTIONS, _syncDeckStack, _toasts, api, closeFaq, closeSecurityOverview, closeSettings,
  closeUsageStats, confirmModal, decideApproval, defaultShortcuts, drawUsageMark,
  effectiveShortcuts, esc, humanBytes, kTokens, loadClientContext, loadCmdHistory, loadDetectedVram,
  loadGitHubActivity, loadModels, loadSkills, loadSystemContext, loadUsageStats, newChat, openFaq,
  openSecurityOverview, openUsageStats, persistUiPreference, populateSettingsForm, refreshModels,
  refreshSandboxStatus, renderModelPill, renderSidebarProfile, renderStatus, requestModelLoad,
  state, stopStreaming, toast, touchedLoadControls, wbMcpLoad, wbRefreshShortcutHints, wbRunAction,
  wbShortcutBlocked, wbShowKeycap,
}) => {

  function settingsGroupTitle(el) {
    const label = el.querySelector(":scope > span:not(.grow):not(.hint)");
    if (label) return label.textContent.trim();
    return Array.from(el.childNodes)
      .filter(n => n.nodeType === Node.TEXT_NODE)
      .map(n => n.textContent)
      .join(" ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function settingsSectionId(title) {
    return String(title || "settings")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "settings";
  }

  const SETTINGS_SECTION_ICONS = Object.freeze({
    model: "ph-cpu",
    "auto-tune": "ph-magic-wand",
    "model-health": "ph-heartbeat",
    "advanced-llama-server": "ph-terminal-window",
    sampling: "ph-sliders-horizontal",
    reasoning: "ph-brain",
    "sub-agents": "ph-tree-structure",
    preview: "ph-browser",
    appearance: "ph-palette",
    notifications: "ph-bell",
    "keyboard-shortcuts": "ph-keyboard",
    "machine-context": "ph-desktop",
    approvals: "ph-shield-check",
    "red-team-tools": "ph-crosshair",
    "red-team-stealth": "ph-eye-slash",
    "analysis-tools": "ph-magnifying-glass",
    sandbox: "ph-cube",
    "tailscale-remote-access": "ph-device-mobile",
    "discord-remote-bridge": "ph-discord-logo",
    "github-integration": "ph-github-logo",
    email: "ph-envelope-simple",
    "desktop-automation": "ph-mouse",
    "long-term-memory": "ph-database"
  });

  function moveSettingsHeadingMeta(heading, content) {
    const label = heading.querySelector(":scope > span:not(.grow):not(.hint)");
    if (!label) {
      const note = heading.querySelector(":scope > .hint");
      if (note) {
        note.classList.add("settings-section-note");
        content.appendChild(note);
      }
      return;
    }
    const extras = Array.from(heading.children).filter(
      child => child !== label && !child.classList.contains("grow")
    );
    if (!extras.length) return;
    const actions = document.createElement("div");
    actions.className = "settings-section-actions";
    extras.forEach(child => actions.appendChild(child));
    content.appendChild(actions);
  }

  function initSettingsSections() {
    const body = $("#settings-body");
    if (!body || body.dataset.sectionsReady === "1") return;
    body.dataset.sectionsReady = "1";

    const saveRow = $("#btn-save-settings")?.closest(".form-row");
    const chunks = [];
    let current = null;
    Array.from(body.children).forEach(node => {
      if (node === saveRow) return;
      if (node.classList?.contains("form-group")) {
        current = { title: settingsGroupTitle(node), nodes: [node] };
        chunks.push(current);
      } else if (current) {
        current.nodes.push(node);
      }
    });

    const fragment = document.createDocumentFragment();
    chunks.forEach(chunk => {
      const sectionId = settingsSectionId(chunk.title);
      const section = document.createElement("details");
      section.className = "settings-section";
      section.dataset.settingsSection = sectionId;
      section.open = true;

      const summary = document.createElement("summary");
      summary.className = "settings-section-summary";
      const icon = SETTINGS_SECTION_ICONS[sectionId] || "ph-gear";
      summary.innerHTML = `
        <span class="settings-section-icon" aria-hidden="true"><i class="ph ${icon}"></i></span>
        <strong class="settings-section-title">${esc(chunk.title)}</strong>
        <i class="ph ph-caret-down settings-section-chevron" aria-hidden="true"></i>`;
      // Sections stay open in the settings window; the summary is a heading.
      summary.addEventListener("click", (event) => event.preventDefault());
      summary.tabIndex = -1;
      section.appendChild(summary);

      const content = document.createElement("div");
      content.className = "settings-section-content";
      moveSettingsHeadingMeta(chunk.nodes[0], content);
      chunk.nodes.slice(1).forEach(node => content.appendChild(node));
      section.appendChild(content);
      fragment.appendChild(section);
    });

    body.replaceChildren(fragment);
    // A setting's description belongs under its name: a hint standing in its
    // own row right above a toggle row moves into that row.
    body.querySelectorAll(".settings-section-content > .form-row").forEach(row => {
      const label = row.nextElementSibling?.matches(".form-row.inline") && row.nextElementSibling.querySelector(":scope > label");
      if (label && row.children.length && [...row.children].every(child => child.matches("span.hint"))) {
        label.after(...row.children);
        row.remove();
      }
    });
    if (saveRow) {
      saveRow.classList.add("settings-savebar");
      body.after(saveRow);
    }
    initSettingsNav();
  }

  // ===== Pages that live in settings =====
  // Usage stats, the security overview, MCP servers, command history and
  // GitHub activity were pages and windows of their own. Their content moves
  // into settings pages (ids, handlers and renderers stay as they were), their
  // open functions open settings on that page, and each page loads its data
  // when it is shown.
  let settingsOpenPage = "";
  let settingsUsageTab = "stats";
  const SETTINGS_PAGE_SHOWN = {
    usage: () => selectSettingsTab(settingsUsageTab),
    tools: () => wbMcpLoad(),
    github: () => loadGitHubActivity(),
    profile: () => renderSettingsProfile(),
    skills: () => loadSkills(true).then(renderSettingsSkills),
    storage: () => loadSettingsStorage(),
    email: () => loadEmailSettings(),
  };
  // Sections a panel replaces: MCP servers' "Manage" row opened the window
  // that now sits on the same page.
  const SETTINGS_ABSORBED = ["mcp-servers"];

  function openSettingsAt(pageId, tab = "") {
    if (tab) settingsUsageTab = tab;
    const drawer = $("#settings-drawer");
    if (drawer?.classList.contains("open") && drawer.querySelector(".settings-pages")) {
      showSettingsPage(pageId);
      return;
    }
    settingsOpenPage = pageId;
    openSettings();
  }

  function selectSettingsTab(tab) {
    const panel = $('#settings-body > .settings-panel[data-settings-page="usage"]');
    if (!panel) return;
    settingsUsageTab = tab === "commands" ? "commands" : "stats";
    panel.querySelectorAll("[data-settings-tab]").forEach(btn => {
      btn.setAttribute("aria-selected", String(btn.dataset.settingsTab === settingsUsageTab));
    });
    panel.querySelectorAll("[data-settings-tab-panel]").forEach(el => {
      el.hidden = el.dataset.settingsTabPanel !== settingsUsageTab;
    });
    if (settingsUsageTab === "commands") loadCmdHistory();
    else { drawUsageMark(true); loadUsageStats(); }
  }

  // Profile: the picture (the usage card's, cropped and saved the same way)
  // and a name, both shown at the top of the sidebar. Saved as they change.
  function renderSettingsProfile() {
    const url = state.settings?.profile_avatar || "";
    const name = String(state.settings?.profile_name || "");
    const pic = $("#settings-avatar");
    if (pic) {
      pic.innerHTML = url ? `<img src="${esc(url)}" alt="">` : esc((name.trim()[0] || "A").toUpperCase());
      pic.classList.toggle("has-photo", !!url);
    }
    const remove = $("#settings-avatar-remove");
    if (remove) remove.hidden = !url;
    const input = $("#settings-profile-name");
    if (input && document.activeElement !== input && input.value !== name) input.value = name;
    if (typeof renderSidebarProfile === "function") renderSidebarProfile();
  }

  // Skills: what the skills folder holds; the loaded one is marked.
  function renderSettingsSkills() {
    const list = $("#settings-skill-list");
    if (!list) return;
    const skills = state.skills || [];
    const active = state.activeSkill?.name || "";
    $("#settings-skill-count").textContent = `${skills.length} skill${skills.length === 1 ? "" : "s"}`;
    list.innerHTML = skills.length ? skills.map(s => `<div class="settings-card-row settings-skill">`
      + `<i class="ph ph-book-open-text" aria-hidden="true"></i>`
      + `<div class="settings-card-text"><b>${esc(s.name)}${s.name === active ? ` <span class="settings-badge">Loaded</span>` : ""}</b>`
      + `<small title="${esc(s.description || "")}">${esc(s.description || "")}</small></div>`
      + `<span class="settings-card-meta">~${esc(kTokens(s.budget || 0))} tok</span></div>`).join("")
      : `<div class="settings-empty">No skills yet. Add a folder with a SKILL.md to the skills folder, then refresh.</div>`;
  }

  // Storage: the app's data on disk by kind. Clearable kinds get a delete
  // button that asks first; the rest are shown for their size only.
  const STORAGE_ICONS = {
    chats: "ph-chats", versions: "ph-clock-counter-clockwise", snapshots: "ph-arrow-counter-clockwise",
    evidence: "ph-crosshair", reports: "ph-file-text", history: "ph-terminal-window", logs: "ph-scroll",
    backups: "ph-archive", audit: "ph-list-checks", models: "ph-cpu", security: "ph-shield-check",
    cache: "ph-browser", journals: "ph-notebook", uploads: "ph-upload-simple",
  };
  let settingsStorage = null;
  async function loadSettingsStorage() {
    const table = $("#settings-storage-table");
    if (!table) return;
    try {
      settingsStorage = await api("/api/storage");
    } catch (e) {
      table.innerHTML = `<div class="settings-empty">Could not read storage: ${esc(e.message || e)}</div>`;
      return;
    }
    const day = (secs) => secs ? new Date(secs * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) : "—";
    $("#settings-storage-total").textContent = `${humanBytes(settingsStorage.total || 0)} in ${settingsStorage.path || "the data folder"}`;
    table.innerHTML = `<div class="settings-storage-head"><span>Kind</span><span>Files</span><span>Size</span><span>Last changed</span><span></span></div>`
      + (settingsStorage.kinds || []).map(k => `<div class="settings-storage-row" title="${esc(k.path || "")}">`
        + `<span class="settings-storage-kind"><i class="ph ${STORAGE_ICONS[k.key] || "ph-file"}" aria-hidden="true"></i>${esc(k.label)}</span>`
        + `<span>${Number(k.files || 0).toLocaleString()}</span><span>${esc(humanBytes(k.size || 0))}</span><span>${esc(day(k.modified))}</span>`
        + (k.clearable && k.files
          ? `<button type="button" class="settings-storage-clear" data-storage-clear="${esc(k.key)}" title="Delete ${esc(k.label.toLowerCase())}" aria-label="Delete ${esc(k.label.toLowerCase())}"><i class="ph ph-trash" aria-hidden="true"></i></button>`
          : `<span></span>`)
        + `</div>`).join("");
  }
  async function clearSettingsStorage(key) {
    const kind = (settingsStorage?.kinds || []).find(k => k.key === key);
    if (!kind) return;
    const ok = await confirmModal({
      title: `Delete ${kind.label.toLowerCase()}?`,
      message: `This permanently deletes ${Number(kind.files).toLocaleString()} file${kind.files === 1 ? "" : "s"} (${humanBytes(kind.size)}) from ${settingsStorage.path}. It can't be undone.`,
      confirmText: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      const res = await api("/api/storage/clear", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key }),
      });
      if (res?.error) throw new Error(res.error);
      toast(`Freed ${humanBytes(res.freed || 0)}${res.skipped ? ` · ${res.skipped} in use, left as they were` : ""}`, "ok", 3200);
    } catch (e) {
      toast(`Could not delete: ${e.message || e}`, "err", 4000);
    }
    loadSettingsStorage();
  }

  function initSettingsOwnPages(body) {
    const panel = (pageId, html) => {
      const el = document.createElement("div");
      el.className = "settings-panel";
      el.dataset.settingsPage = pageId;
      el.innerHTML = html;
      body.appendChild(el);
      return el;
    };
    const profile = panel("profile", `<div class="settings-card">`
      + `<div class="settings-card-row"><div class="settings-card-text"><b>Picture</b><small>Shown at the top of the sidebar and on your usage card.</small></div>`
      + `<div class="settings-profile-pic"><span class="settings-avatar" id="settings-avatar" aria-hidden="true"></span>`
      + `<button type="button" class="btn sm" id="settings-avatar-pick">Upload</button>`
      + `<button type="button" class="btn sm" id="settings-avatar-remove" hidden>Remove</button></div></div>`
      + `<div class="settings-card-row"><div class="settings-card-text"><b>Name</b><small>Shown at the top of the sidebar.</small></div>`
      + `<input type="text" id="settings-profile-name" maxlength="40" placeholder="Your name" autocomplete="off" spellcheck="false"></div></div>`);
    profile.querySelector("#settings-avatar-pick").addEventListener("click", () => $("#usage-avatar-file")?.click());
    profile.querySelector("#settings-avatar-remove").addEventListener("click", () => $("#usage-avatar-remove")?.click());
    const nameInput = profile.querySelector("#settings-profile-name");
    nameInput.addEventListener("change", async () => {
      await persistUiPreference({ profile_name: nameInput.value.trim().slice(0, 40) });
      renderSettingsProfile();
    });

    const skills = panel("skills", `<div class="settings-panel-bar"><span id="settings-skill-count"></span>`
      + `<button type="button" id="settings-skills-refresh"><i class="ph ph-arrow-clockwise" aria-hidden="true"></i>Refresh</button></div>`
      + `<div class="settings-card" id="settings-skill-list"></div>`
      + `<p class="settings-panel-note">Load a skill from the composer's + menu, or type # in the message box.</p>`);
    skills.querySelector("#settings-skills-refresh").addEventListener("click", () => loadSkills(true).then(renderSettingsSkills));

    const storage = panel("storage", `<div class="settings-panel-bar"><span id="settings-storage-total"></span>`
      + `<button type="button" id="settings-storage-refresh"><i class="ph ph-arrow-clockwise" aria-hidden="true"></i>Refresh</button></div>`
      + `<div class="settings-card settings-storage" id="settings-storage-table"></div>`
      + `<p class="settings-panel-note">Only data that is safe to lose can be deleted here. Chats are cleared from the sidebar; uploads live in your workspace.</p>`);
    storage.querySelector("#settings-storage-refresh").addEventListener("click", loadSettingsStorage);
    storage.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-storage-clear]");
      if (btn) clearSettingsStorage(btn.dataset.storageClear);
    });
  }

  // The page list and the page fade at an edge with more behind it, instead
  // of cutting off there.
  function initSettingsFades() {
    for (const el of [$("#settings-body"), $("#settings-drawer .settings-tools")]) {
      if (!el) continue;
      el.classList.add("settings-fade");
      const sync = () => {
        el.classList.toggle("fade-top", el.scrollTop > 2);
        el.classList.toggle("fade-bottom", el.scrollTop + el.clientHeight < el.scrollHeight - 2);
      };
      el.addEventListener("scroll", sync, { passive: true });
      new ResizeObserver(sync).observe(el);
      new MutationObserver(sync).observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "hidden"] });
      sync();
    }
  }

  function initSettingsPanels(body) {
    const section = id => body.querySelector(`details.settings-section[data-settings-section="${id}"]`);
    const panel = (pageId, before) => {
      const el = document.createElement("div");
      el.className = "settings-panel";
      el.dataset.settingsPage = pageId;
      if (before) before.before(el);
      else body.appendChild(el);
      return el;
    };
    const bar = (...nodes) => {
      const el = document.createElement("div");
      el.className = "settings-panel-bar";
      el.append(...nodes.filter(Boolean));
      return el;
    };
    const tabPanel = (name, ...nodes) => {
      const el = document.createElement("div");
      el.dataset.settingsTabPanel = name;
      el.append(...nodes.filter(Boolean));
      return el;
    };

    // Usage: the stats, with the command history as a second tab.
    const usage = panel("usage");
    usage.innerHTML = `<div class="settings-tabs" role="tablist" aria-label="Usage">`
      + `<button type="button" role="tab" data-settings-tab="stats" aria-selected="true">Stats</button>`
      + `<button type="button" role="tab" data-settings-tab="commands" aria-selected="false">Commands</button></div>`;
    usage.append(
      tabPanel("stats", bar($("#btn-refresh-usage")), $("#usage-page .usage-shell")),
      tabPanel("commands", bar($("#cmd-history-count"), $("#btn-cmd-history-refresh"), $("#btn-cmd-history-clear")), $("#cmd-history-body")));
    usage.querySelector(".settings-tabs").addEventListener("click", (e) => {
      const tab = e.target.closest("[data-settings-tab]");
      if (tab) selectSettingsTab(tab.dataset.settingsTab);
    });

    // Security scans: the schedule, and a way to the overview's full page.
    const security = panel("security", section("scheduled-security-overview"));
    security.innerHTML = `<div class="settings-panel-bar"><span>The scan results have their own page.</span>`
      + `<button type="button" id="settings-open-security"><i class="ph ph-lightning" aria-hidden="true"></i>Open Security overview</button></div>`;
    security.querySelector("#settings-open-security").addEventListener("click", openSecurityOverview);

    // Tools: MCP servers lead the page.
    const tools = panel("tools", section("mcp-servers"));
    tools.append($("#mcp-window .mcp-head"), $("#mcp-window .mcp-body"));

    // GitHub: activity under the integration settings.
    const github = panel("github");
    section("github-integration")?.after(github);
    github.append(bar($("#gh-activity-login")), $("#gh-activity-body"));

    initSettingsOwnPages(body);
  }

  // ---------- Settings > Email ----------
  // The account lives in its own file beside the email skill's scripts, not in
  // settings.json, and goes through /api/email. The password is write-only:
  // the bridge never sends it back, so the field stays empty once saved.
  const EMAIL_TEXT_FIELDS = {
    "#set-email-name": "from_name",
    "#set-email-from": "from",
    "#set-email-smtp-host": "smtp_host",
    "#set-email-imap-host": "imap_host",
    "#set-email-signature": "signature",
    "#set-email-style": "style",
  };
  const EMAIL_PORT_FIELDS = { "#set-email-smtp-port": "smtp_port", "#set-email-imap-port": "imap_port" };
  const EMAIL_INPUTS = [...Object.keys(EMAIL_TEXT_FIELDS), ...Object.keys(EMAIL_PORT_FIELDS),
    "#set-email-password", "#set-email-identities", "#set-email-contacts", "#btn-email-test"];

  const emailContactsToText = (contacts) =>
    Object.entries(contacts || {}).map(([name, address]) => `${name} <${address}>`).join("\n");

  // "Name <a@b.c>", "Name: a@b.c" and "Name a@b.c" all work; the address is
  // the last word with an @ in it and the rest is the name.
  function emailContactsFromText(text) {
    const contacts = {};
    for (const raw of String(text || "").split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      const m = /^(.*?)[\s<:,=]*([^\s<>:,=]+@[^\s<>:,=]+)>?$/.exec(line);
      const name = m ? m[1].replace(/^["']|["']$/g, "").trim() : "";
      if (!m || !name) throw new Error(`"${line}" needs a name and an address, like: Sam Rivera <sam@example.com>`);
      contacts[name] = m[2];
    }
    return contacts;
  }

  function showEmailStatus(data) {
    const status = $("#email-conn-status");
    if (status) status.textContent = data.configured ? `set up for ${data.from}` : "not set up";
    const pw = $("#set-email-password");
    if (pw) pw.placeholder = data.password_set ? "saved. type a new one to replace it" : "mailbox password";
    EMAIL_INPUTS.forEach(sel => { const el = $(sel); if (el) el.disabled = data.can_edit === false; });
    const result = $("#email-test-result");
    if (result && data.can_edit === false) result.textContent = "the email account can only be changed on the machine running Accuretta.";
  }

  function fillEmailSettings(data) {
    const set = (sel, value) => { const el = $(sel); if (el && document.activeElement !== el) el.value = value ?? ""; };
    Object.entries(EMAIL_TEXT_FIELDS).forEach(([sel, key]) => set(sel, data[key]));
    Object.entries(EMAIL_PORT_FIELDS).forEach(([sel, key]) => set(sel, data[key]));
    set("#set-email-identities", (data.identities || []).join(", "));
    set("#set-email-contacts", emailContactsToText(data.contacts));
    showEmailStatus(data);
  }

  async function loadEmailSettings() {
    try {
      const data = await api("/api/email");
      if (data && !data.error) fillEmailSettings(data);
    } catch (_) { /* bridge unreachable: leave the form as it is */ }
  }

  async function saveEmailSettings(update) {
    try {
      const saved = await api("/api/email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      });
      if (!saved || saved.error) throw new Error(saved?.error || "Email settings could not be saved");
      showEmailStatus(saved);
      return true;
    } catch (err) {
      toast(err.message, "warn", 4000);
      return false;
    }
  }

  function initEmailSettings() {
    Object.entries(EMAIL_TEXT_FIELDS).forEach(([sel, key]) =>
      $(sel)?.addEventListener("change", (e) => saveEmailSettings({ [key]: e.target.value.trim() })));
    Object.entries(EMAIL_PORT_FIELDS).forEach(([sel, key]) =>
      $(sel)?.addEventListener("change", (e) => saveEmailSettings({ [key]: Number(e.target.value) })));
    $("#set-email-password")?.addEventListener("change", async (e) => {
      if (!e.target.value) return;   // empty keeps the saved password
      if (await saveEmailSettings({ password: e.target.value })) e.target.value = "";
    });
    $("#set-email-identities")?.addEventListener("change", (e) =>
      saveEmailSettings({ identities: e.target.value.split(/[,;\s]+/).filter(Boolean) }));
    $("#set-email-contacts")?.addEventListener("change", (e) => {
      try { saveEmailSettings({ contacts: emailContactsFromText(e.target.value) }); }
      catch (err) { toast(err.message, "warn", 5000); }
    });
    $("#btn-email-test")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget, result = $("#email-test-result");
      btn.disabled = true;
      result.className = "hint";
      result.textContent = "signing in…";
      try {
        const out = await api("/api/email/test", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
        });
        result.textContent = out.message || out.error || "No answer from the bridge.";
        result.classList.add(out.ok ? "is-ok" : "is-bad");
      } catch (_) {
        result.textContent = "Could not reach the bridge.";
        result.classList.add("is-bad");
      }
      btn.disabled = false;
    });
  }

  function wbMcpShown() {
    const drawer = $("#settings-drawer");
    return !!drawer?.classList.contains("open") && drawer.dataset.settingsPage === "tools";
  }

  // ===== Settings window: page list + search =====
  // A centered window: grouped pages on the left with search on top, one page
  // at a time on the right. A page shows its sections fully open, each titled
  // like a group; search looks across every page. Sections no page claims land
  // on "More", so a new settings group never goes missing.
  const SETTINGS_PAGES = [
    { group: "Settings", pages: [
      { id: "general", label: "General", icon: "ph-gear-six", sections: ["network", "notifications", "keyboard-shortcuts", "preview"] },
      { id: "profile", label: "Profile", icon: "ph-user-circle", sections: [] },
      { id: "appearance", label: "Appearance", icon: "ph-palette", sections: ["appearance"] },
      { id: "usage", label: "Usage", icon: "ph-chart-bar", sections: [] },
      { id: "memory", label: "Memory & rules", icon: "ph-notebook", sections: ["machine-context", "preferences-and-memory"] },
      { id: "tools", label: "Tools", icon: "ph-wrench", sections: ["mcp-servers", "analysis-tools", "desktop-automation", "sandbox"] },
      { id: "storage", label: "Storage", icon: "ph-database", sections: [] },
    ] },
    { group: "Models", pages: [
      { id: "model", label: "Model", icon: "ph-cpu", sections: ["model"] },
      { id: "performance", label: "Performance", icon: "ph-gauge", sections: ["auto-tune", "model-health"] },
      { id: "generation", label: "Generation", icon: "ph-sliders-horizontal", sections: ["sampling", "reasoning", "sub-agents"] },
      { id: "developer", label: "Developer", icon: "ph-terminal-window", sections: ["advanced-llama-server"] },
    ] },
    { group: "Security", pages: [
      { id: "approvals", label: "Approvals", icon: "ph-shield-check", sections: ["approvals"] },
      { id: "security", label: "Security scans", icon: "ph-shield-star", sections: ["scheduled-security-overview"] },
      { id: "red-team", label: "Red team", icon: "ph-crosshair", sections: ["red-team-tools", "red-team-stealth"] },
    ] },
    { group: "Connections", pages: [
      { id: "skills", label: "Skills", icon: "ph-book-open-text", sections: [] },
      { id: "github", label: "GitHub", icon: "ph-github-logo", sections: ["github-integration"] },
      { id: "email", label: "Email", icon: "ph-envelope-simple", sections: ["email"] },
      { id: "remote", label: "Remote", icon: "ph-device-mobile", sections: ["tailscale-remote-access", "discord-remote-bridge"] },
    ] },
  ];
  const SETTINGS_PAGE_KEY = "accuretta:settings-page";
  const settingsPageList = () => SETTINGS_PAGES.flatMap(group => group.pages);

  function showSettingsPage(pageId) {
    const body = $("#settings-body");
    const drawer = $("#settings-drawer");
    if (!body || !drawer) return;
    const pages = settingsPageList();
    const page = pages.find(p => p.id === pageId) || pages[0];
    body.querySelectorAll(":scope > [data-settings-page]").forEach(el => {
      el.classList.toggle("off-page", el.dataset.settingsPage !== page.id);
    });
    drawer.querySelectorAll(".settings-pages [data-settings-page]").forEach(btn => {
      const on = btn.dataset.settingsPage === page.id;
      btn.classList.toggle("is-current", on);
      if (on) btn.setAttribute("aria-current", "page");
      else btn.removeAttribute("aria-current");
    });
    const title = drawer.querySelector(".drawer-head h3");
    if (title) title.textContent = page.label;
    drawer.dataset.settingsPage = page.id;
    body.scrollTop = 0;
    try { localStorage.setItem(SETTINGS_PAGE_KEY, page.id); } catch (_) {}
    if (drawer.classList.contains("open")) SETTINGS_PAGE_SHOWN[page.id]?.();
  }

  function initSettingsNav() {
    const body = $("#settings-body");
    const drawer = $("#settings-drawer");
    if (!body || !drawer || body.dataset.navReady === "1") return;
    body.dataset.navReady = "1";
    drawer.classList.add("settings-window");

    const sections = () => [...body.querySelectorAll("details.settings-section")];
    // Claim each section for its page; strays go to "More".
    const claimed = new Set([...settingsPageList().flatMap(p => p.sections), ...SETTINGS_ABSORBED]);
    const strays = sections().map(s => s.dataset.settingsSection).filter(id => !claimed.has(id));
    if (strays.length) SETTINGS_PAGES[0].pages.push({ id: "more", label: "More", icon: "ph-dots-three-circle", sections: strays });
    const pages = settingsPageList();
    sections().forEach(section => {
      if (SETTINGS_ABSORBED.includes(section.dataset.settingsSection)) {
        section.classList.add("absorbed");
        return;
      }
      const page = pages.find(p => p.sections.includes(section.dataset.settingsSection));
      section.dataset.settingsPage = page.id;
      // A page with one section already says its name in the header.
      section.toggleAttribute("data-solo", page.sections.length === 1);
    });
    // Sections follow their page's order, so a page reads top to bottom.
    pages.forEach(page => page.sections.forEach(id => {
      const section = body.querySelector(`details.settings-section[data-settings-section="${id}"]`);
      if (section) body.appendChild(section);
    }));
    initSettingsPanels(body);

    const head = drawer.querySelector(".drawer-head");
    const tools = document.createElement("div");
    tools.className = "settings-tools";
    tools.innerHTML = `
      <label class="settings-search">
        <i class="ph ph-magnifying-glass" aria-hidden="true"></i>
        <input type="search" id="settings-search-input" placeholder="Search settings" autocomplete="off" spellcheck="false" aria-label="Search settings">
        <button type="button" class="settings-search-clear" title="Clear search" aria-label="Clear search"><i class="ph ph-x"></i></button>
      </label>
      <nav class="settings-pages" aria-label="Settings pages">${SETTINGS_PAGES.map(group =>
        `<div class="settings-pages-group">${esc(group.group)}</div>`
        + group.pages.map(page => `<button type="button" data-settings-page="${esc(page.id)}">`
          + `<i class="ph ${page.icon}" aria-hidden="true"></i><span>${esc(page.label)}</span></button>`).join("")).join("")}</nav>
      <p class="settings-search-note" id="settings-search-note" aria-live="polite"></p>`;
    const input = tools.querySelector("#settings-search-input");
    const clearBtn = tools.querySelector(".settings-search-clear");
    const note = tools.querySelector("#settings-search-note");

    tools.querySelector(".settings-pages").addEventListener("click", (e) => {
      const btn = e.target.closest("[data-settings-page]");
      if (!btn) return;
      if (input.value) { input.value = ""; applySearch(); }
      showSettingsPage(btn.dataset.settingsPage);
    });

    // Search: rows that match show, across every page, under their section's
    // title; the rest hide. Empty restores the current page.
    let searchTimer = null;
    // textContent misses placeholders, titles, aria-labels, select options and
    // typed values, so row matching uses all of them.
    const rowHaystack = (row) => {
      const parts = [
        row.textContent || "",
        row.title || "",
        row.getAttribute?.("aria-label") || "",
        ...[...row.querySelectorAll("input, select, textarea")].map(c => [
          c.placeholder || "",
          c.title || "",
          c.getAttribute?.("aria-label") || "",
          typeof c.value === "string" ? c.value : "",
          ...[...(c.querySelectorAll?.("option") || [])].map(o => o.textContent || ""),
        ].join(" ")),
      ];
      return parts.join(" ").toLowerCase();
    };
    const applySearch = () => {
      const q = input.value.trim().toLowerCase();
      tools.querySelector(".settings-search").classList.toggle("has-value", !!q);
      const searching = !!q;
      drawer.classList.toggle("is-searching", searching);
      let hitRows = 0;
      let hitSections = 0;
      sections().forEach(section => {
        const titleHit = searching && (section.querySelector(".settings-section-title")?.textContent || "").toLowerCase().includes(q);
        const content = section.querySelector(".settings-section-content");
        let sectionHits = 0;
        [...(content?.children || [])].forEach(row => {
          // A section-title match shows the whole section, so searching a
          // section's name ("key" -> Keyboard shortcuts) shows its controls.
          const match = !searching || titleHit || rowHaystack(row).includes(q);
          row.hidden = searching && !match;
          if (searching && match) sectionHits++;
        });
        const isMatch = sectionHits > 0 || !!titleHit;
        if (searching && isMatch) hitSections++;
        hitRows += sectionHits;
        section.hidden = searching && !isMatch;
      });
      const title = drawer.querySelector(".drawer-head h3");
      if (searching) {
        if (title) title.textContent = "Search";
        note.textContent = hitRows
          ? `${hitRows} setting${hitRows === 1 ? "" : "s"} in ${hitSections} section${hitSections === 1 ? "" : "s"}`
          : "No settings match. Check the spelling or try a shorter word.";
        note.classList.add("has-hits");
      } else {
        showSettingsPage(drawer.dataset.settingsPage);
        note.textContent = "";
        note.classList.remove("has-hits");
      }
    };
    input.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(applySearch, 120);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && input.value) { e.stopPropagation(); input.value = ""; applySearch(); }
    });
    clearBtn.addEventListener("click", () => { input.value = ""; applySearch(); input.focus(); });

    // The page list sits beside the header and the scrollable body.
    head.before(tools);
    let saved = "";
    try { saved = localStorage.getItem(SETTINGS_PAGE_KEY) || ""; } catch (_) {}
    showSettingsPage(saved);
    initSettingsFades();
  }

  function revealSettingsControl(selector) {
    const el = typeof selector === "string" ? $(selector) : selector;
    if (!el) return;
    const holder = el.closest("#settings-body > [data-settings-page]");
    if (holder) showSettingsPage(holder.dataset.settingsPage);
    requestAnimationFrame(() => el.scrollIntoView({ behavior: "smooth", block: "center" }));
  }

  function pct(value) {
    return `${Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100)}%`;
  }

  function modelHealthValue(value) {
    if (typeof value === "boolean") return value ? "On" : "Off";
    if (typeof value === "number") return value.toLocaleString();
    if (value == null || value === "") return "Auto";
    return String(value).replace(/_/g, " ");
  }

  function modelHealthChanges(changes) {
    if (!Array.isArray(changes) || !changes.length) return "";
    return changes.map(change => `
      <div class="model-health-change">
        <span>${esc(change.label || change.key || "Setting")}</span>
        <strong>${esc(modelHealthValue(change.from))}<i class="ph ph-arrow-right"></i>${esc(modelHealthValue(change.to))}</strong>
      </div>`).join("");
  }

  function renderModelHealth(data) {
    const card = $("#model-health-card");
    if (!card) return;
    const turns = Math.max(0, Number(data?.turns) || 0);
    const calls = Math.max(0, Number(data?.tool_calls) || 0);
    const observed = data?.observed || {};
    const enabled = data?.enabled !== false;
    const advice = Array.isArray(data?.advice) ? data.advice.filter(Boolean) : [];
    const context = Math.max(0, Number(data?.last_context) || 0);
    const peakContext = Math.max(0, Number(observed.p90_peak_context_tokens || observed.avg_peak_context_tokens) || 0);
    const contextRatio = context ? peakContext / context : 0;
    const learning = data?.learning || {};
    const recommendation = data?.recommendation || null;
    const trial = data?.trial || null;
    // Load-time speed check (dense models): measured right after each load, so
    // it doesn't wait for the 25-turn baseline.
    const fitRec = data?.fit?.recommendation || null;
    const fitResult = data?.fit?.result || "";
    const sessions = Math.max(0, Number(learning.sessions) || 0);
    const turnsRequired = Math.max(1, Number(learning.turns_required) || 25);
    const sessionsRequired = Math.max(1, Number(learning.sessions_required) || 3);
    const ready = turns >= turnsRequired && sessions >= sessionsRequired;

    const model = data?.model || state.settings?.model || "No model selected";
    $("#model-health-title").textContent = model;
    $("#model-health-privacy").textContent = data?.privacy ||
      "Local aggregate counters only. No conversation or tool content.";
    const badge = $("#model-health-badge");
    const subtitle = $("#model-health-subtitle");

    if (!enabled) {
      card.dataset.state = "paused";
      badge.textContent = "paused";
      subtitle.textContent = "Learning is turned off";
    } else if (!data?.model) {
      card.dataset.state = "collecting";
      badge.textContent = "waiting";
      subtitle.textContent = "Choose a model to begin";
    } else if (fitRec) {
      card.dataset.state = "attention";
      badge.textContent = "speed check";
      subtitle.textContent = "Measured right after loading";
    } else if (!ready) {
      card.dataset.state = "collecting";
      badge.textContent = `${Math.min(turns, turnsRequired)}/${turnsRequired} turns`;
      subtitle.textContent = turns
        ? `Building a baseline across ${sessions}/${sessionsRequired} sessions`
        : "Learning begins with your next chat";
    } else if (trial?.status === "worse") {
      card.dataset.state = "attention";
      badge.textContent = "undo suggested";
      subtitle.textContent = "The recommended setup performed worse";
    } else if (trial?.status === "measuring") {
      card.dataset.state = "testing";
      badge.textContent = `${trial.progress || 0}/${trial.target || 10} trial turns`;
      subtitle.textContent = "Checking the recommended setup";
    } else if (recommendation) {
      card.dataset.state = "attention";
      badge.textContent = "high confidence";
      subtitle.textContent = `Based on ${turns} turns across ${sessions} sessions`;
    } else if (advice.length) {
      card.dataset.state = "attention";
      badge.textContent = "watching";
      subtitle.textContent = `Based on ${turns} local turns`;
    } else {
      card.dataset.state = "healthy";
      badge.textContent = trial?.status === "stable" ? "trial complete" : "well tuned";
      subtitle.textContent = `Based on ${turns} turns across ${sessions} sessions`;
    }

    $("#model-health-metrics").innerHTML = `
      <div class="model-health-metric"><span>Observed turns</span><strong>${turns || "&mdash;"}</strong></div>
      <div class="model-health-metric"><span>Finished</span><strong>${turns ? pct(observed.completion_rate) : "&mdash;"}</strong></div>
      <div class="model-health-metric"><span>Tool success</span><strong>${calls ? pct(1 - (Number(observed.tool_error_rate) || 0)) : "&mdash;"}</strong></div>
      <div class="model-health-metric"><span>Busy context</span><strong>${peakContext && context ? pct(contextRatio) : "&mdash;"}</strong></div>`;

    const compareEl = $("#model-health-compare");
    if (compareEl) {
      const others = Array.isArray(data?.comparison) ? data.comparison : [];
      const currentSpeed = Number(observed.output_tok_s) || 0;
      const currentTool = calls ? 1 - (Number(observed.tool_error_rate) || 0) : null;
      const rows = [];
      if (data?.model) {
        rows.push({
          model: data.model, current: true,
          turns, finished: turns ? pct(observed.completion_rate) : "&mdash;",
          tool: currentTool == null ? "&mdash;" : pct(currentTool),
          speed: currentSpeed ? currentSpeed.toFixed(1) : "&mdash;",
        });
      }
      others.forEach(o => rows.push({
        model: o.model, current: false,
        turns: Number(o.turns) || 0,
        finished: pct(o.completion_rate),
        tool: pct(o.tool_success),
        speed: Number(o.output_tok_s) ? Number(o.output_tok_s).toFixed(1) : "&mdash;",
      }));
      compareEl.hidden = rows.length < 2;
      if (rows.length >= 2) {
        compareEl.innerHTML = `
          <details class="model-health-compare">
            <summary><i class="ph ph-arrows-left-right"></i>Compare with other models</summary>
            <div class="mhc-row mhc-head"><span>Model</span><span>Turns</span><span>Finished</span><span>Tool ok</span><span>tok/s</span></div>
            ${rows.map(r => `
              <div class="mhc-row ${r.current ? "mhc-current" : ""}">
                <span>${esc(r.model)}${r.current ? " <i class='ph ph-asterisk' title='current model'></i>" : ""}</span>
                <b>${r.turns}</b>
                <b>${r.finished}</b>
                <b>${r.tool}</b>
                <b>${r.speed}</b>
              </div>`).join("")}
          </details>`;
      }
    }

    const adviceEl = $("#model-health-advice");
    if (!enabled) {
      adviceEl.textContent = "Existing observations are kept locally. Turn learning back on whenever you want it to continue.";
    } else if (!ready) {
      const turnText = Number(learning.turns_remaining) > 0
        ? `${learning.turns_remaining} more turn${Number(learning.turns_remaining) === 1 ? "" : "s"}` : "enough turns";
      const sessionText = Number(learning.sessions_remaining) > 0
        ? `${learning.sessions_remaining} more session${Number(learning.sessions_remaining) === 1 ? "" : "s"}` : "enough sessions";
      adviceEl.textContent = `Keep using this exact setup normally. The advisor needs ${turnText} and ${sessionText} before changing anything.`;
    } else if (advice.length) {
      adviceEl.innerHTML = `<ul>${advice.map(item => `<li>${esc(item)}</li>`).join("")}</ul>`;
    } else {
      adviceEl.textContent = recommendation
        ? "A measured configuration change is ready below."
        : "No measured reason to change the current settings. Accuretta will keep watching this configuration.";
    }

    const recommendationEl = $("#model-health-recommendation");
    if (recommendationEl) {
      recommendationEl.hidden = !(recommendation || trial || fitRec || fitResult);
      const fitTitle = fitRec?.kind === "fit_down" ? "Context doesn't fit in VRAM"
        : fitRec?.kind === "fit_up" ? "Larger context fits" : "Check that the context fits";
      const fitApply = fitRec?.kind === "fit_check" ? "Run the check" : "Use this context";
      const fitHtml = fitRec ? `
          <div class="model-health-rec-head"><span><i class="ph ph-gauge"></i>${esc(fitTitle)}</span><small>measured</small></div>
          <p>${esc(fitRec.reason || "")}</p>
          <div class="model-health-changes">${modelHealthChanges(fitRec.changes)}</div>
          <small class="model-health-tradeoff">${esc(fitRec.tradeoff || "")}</small>
          <div class="model-health-actions">
            <button type="button" class="btn accent" data-model-health-action="fit_apply" data-recommendation-id="${esc(fitRec.id || "")}">${esc(fitApply)}</button>
            <button type="button" class="btn sm" data-model-health-action="fit_dismiss" data-recommendation-id="${esc(fitRec.id || "")}">Not now</button>
          </div>` : (fitResult ? `<p>${esc(fitResult)}</p>` : "");
      if (fitRec) {
        recommendationEl.innerHTML = fitHtml;
      } else if (recommendation) {
        recommendationEl.innerHTML = `
          <div class="model-health-rec-head"><span><i class="ph ph-sliders-horizontal"></i>Recommended setup</span><small>high confidence</small></div>
          <p>${esc(recommendation.reason || "A better local configuration is ready.")}</p>
          <div class="model-health-changes">${modelHealthChanges(recommendation.changes)}</div>
          <small class="model-health-tradeoff">${esc(recommendation.tradeoff || "")}</small>
          <div class="model-health-actions">
            <button type="button" class="btn accent" data-model-health-action="apply" data-recommendation-id="${esc(recommendation.id || "")}">Use recommended settings</button>
            <button type="button" class="btn sm" data-model-health-action="dismiss" data-recommendation-id="${esc(recommendation.id || "")}">Not now</button>
          </div>`;
      } else if (trial) {
        const measuring = trial.status === "measuring";
        const worse = trial.status === "worse";
        const title = measuring ? "Testing the recommendation" : (worse ? "The trial needs attention" : "Trial complete");
        const detail = measuring
          ? `${trial.progress || 0} of ${trial.target || 10} comparison turns finished.`
          : (trial.result || "The comparison is complete.");
        const trialChanges = modelHealthChanges(trial.changes);
        recommendationEl.innerHTML = `
          <div class="model-health-rec-head"><span><i class="ph ${worse ? "ph-warning" : "ph-gauge"}"></i>${esc(title)}</span><small>${measuring ? "measuring" : esc(trial.status || "complete")}</small></div>
          <p>${esc(detail)}</p>
          ${trialChanges ? `<div class="model-health-changes">${trialChanges}</div>` : ""}
          <div class="model-health-trial-track"><span style="width:${Math.min(100, ((trial.progress || 0) / Math.max(1, trial.target || 10)) * 100)}%"></span></div>
          <div class="model-health-actions">
            ${!measuring && !worse ? '<button type="button" class="btn accent" data-model-health-action="keep">Keep settings</button>' : ""}
            <button type="button" class="btn sm ${worse ? "danger" : ""}" data-model-health-action="undo">Undo recommended settings</button>
          </div>`;
      } else if (fitResult) {
        recommendationEl.innerHTML = `
          <div class="model-health-rec-head"><span><i class="ph ph-gauge"></i>Speed check</span><small>done</small></div>
          ${fitHtml}`;
      }
      recommendationEl.querySelectorAll("[data-model-health-action]").forEach(button => {
        button.addEventListener("click", () => runModelHealthAction(
          button.dataset.modelHealthAction,
          button.dataset.recommendationId || "",
          button,
        ));
      });
    }
    const sw = $("#sw-passive-model-telemetry");
    sw?.classList.toggle("on", enabled);
    sw?.setAttribute("aria-checked", String(enabled));
  }

  async function loadModelHealth() {
    const card = $("#model-health-card");
    if (!card) return;
    try {
      renderModelHealth(await api("/api/model-health"));
    } catch (e) {
      card.dataset.state = "error";
      $("#model-health-title").textContent = "Model health unavailable";
      $("#model-health-subtitle").textContent = "The local counters could not be read";
      $("#model-health-badge").textContent = "offline";
      $("#model-health-advice").textContent = e?.message || "Try reopening Settings.";
    }
  }

  async function reloadModelHealthSettings(result, action) {
    if (!result?.settings) return;
    state.settings = result.settings;
    populateSettingsForm();
    renderStatus();
    renderModelPill();
    if (!result.reload || !result.model_path) return;
    const tid = "model-health-reload";
    state._reloading = true;
    renderStatus();
    renderModelPill();
    toast(action === "undo" ? "restoring the previous model setup…" : "loading the recommended model setup…", "info", 60000, tid);
    try {
      await requestModelLoad(result.model_path);
      await refreshModels();
      toast(action === "undo" ? "previous settings restored"
        : action === "fit_apply" ? "reloaded; the speed check measured it right after loading"
        : "recommended settings applied; the ten-turn check starts now", "ok", 5000, tid);
    } finally {
      state._reloading = false;
      renderStatus();
      renderModelPill();
    }
  }

  async function runModelHealthAction(action, recommendationId = "", button = null) {
    if (button) button.disabled = true;
    try {
      const result = await api("/api/model-health/action", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, recommendation_id: recommendationId }),
      });
      if (!result?.ok) {
        if (result?.error === "recommendation is no longer current") {
          clearModelAdvisorNotifications();
          await loadModelHealth();
          return;
        }
        throw new Error(result?.error || "the setup advisor could not complete that action");
      }
      if (action === "apply" || action === "dismiss") clearModelAdvisorNotifications();
      await reloadModelHealthSettings(result, action);
      if (action === "dismiss" || action === "fit_dismiss") toast("recommendation dismissed for this setup", "info", 2200);
      if (action === "keep") toast("recommended settings kept", "ok", 2200);
      await loadModelHealth();
    } catch (error) {
      toast(`setup advisor: ${error.message || error}`, "err", 5000, "model-health-action");
      if (button) button.disabled = false;
    }
  }

  let _modelAdvisorNotificationId = "";

  function clearModelAdvisorNotifications() {
    const tracked = _toasts.get("model-advisor");
    const rows = new Set();
    if (tracked) rows.add(tracked);
    document.querySelectorAll(".advisor-toast-copy").forEach(copy => {
      const row = copy.closest(".revealer-card.notifications, .toast");
      if (row) rows.add(row);
    });
    rows.forEach(row => {
      clearTimeout(row?._dismissTimer);
      try { row.remove(); } catch {}
    });
    _toasts.delete("model-advisor");
    _syncDeckStack();
  }

  function showModelAdvisorNotification(data) {
    const recommendation = data?.recommendation;
    if (!recommendation?.id || recommendation.id === _modelAdvisorNotificationId) return;
    _modelAdvisorNotificationId = recommendation.id;
    clearModelAdvisorNotifications();
    const changes = Array.isArray(recommendation.changes) ? recommendation.changes : [];
    const summary = changes.slice(0, 3)
      .map(change => `${change.label || change.key} ${modelHealthValue(change.from)} → ${modelHealthValue(change.to)}`)
      .join(" · ") + (changes.length > 3 ? ` · +${changes.length - 3} more` : "");
    const row = toast(
      `<span class="advisor-toast-copy"><strong>A better setup is ready</strong><small>${esc(summary || recommendation.reason || "Measured from normal local use.")}</small></span>` +
      `<button type="button" class="advisor-toast-apply">Use recommended settings</button>` +
      `<button type="button" class="advisor-toast-review">Review</button>` +
      `<button type="button" class="advisor-toast-dismiss" title="Dismiss" aria-label="Dismiss"><i class="ph ph-x"></i></button>`,
      "info", 45000, "model-advisor", true,
    );
    row?.querySelector(".advisor-toast-apply")?.addEventListener("click", event =>
      runModelHealthAction("apply", recommendation.id, event.currentTarget));
    row?.querySelector(".advisor-toast-review")?.addEventListener("click", async () => {
      clearModelAdvisorNotifications();
      await openSettings();
      revealSettingsControl("#model-health-recommendation");
    });
    row?.querySelector(".advisor-toast-dismiss")?.addEventListener("click", clearModelAdvisorNotifications);
    api("/api/model-health/action", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "seen", recommendation_id: recommendation.id }),
    }).catch(() => {});
  }

  async function checkModelAdvisor(options = {}) {
    const data = await api("/api/model-health");
    renderModelHealth(data);
    if (options.notify && data?.notification_needed && !data?.trial) showModelAdvisorNotification(data);
    return data;
  }

  function renderRemoteAccess() {
    const data = state.remoteAccess || {};
    const tail = data.tailscale || {};
    const serve = data.serve || {};
    const card = $("#remote-status-card");
    const title = $("#remote-status-title");
    const detail = $("#remote-status-detail");
    const health = Array.isArray(tail.health) ? tail.health.filter(Boolean) : [];
    if (card) card.dataset.state = tail.ok ? "ok" : "warn";
    if (title) title.textContent = tail.ok
      ? "Tailscale is ready"
      : (tail.state ? `Tailscale: ${tail.state}` : "Tailscale unavailable");
    if (detail) {
      const parts = [];
      if (health.length) parts.push(health[0]);
      parts.push(window.isSecureContext ? "Clipboard has a secure browser context." : "This page is HTTP, so browser clipboard access may be blocked.");
      detail.textContent = parts.join(" ");
    }
    const serveBtn = $("#btn-tailscale-serve");
    if (serveBtn) {
      serveBtn.innerHTML = serve.configured
        ? '<i class="ph ph-check-circle"></i>HTTPS enabled'
        : '<i class="ph ph-lock-key"></i>Enable HTTPS address';
      serveBtn.disabled = !!serve.configured;
    }
    const serveLink = $("#remote-serve-url");
    if (serveLink) {
      serveLink.hidden = !serve.url;
      serveLink.href = serve.url || "#";
      serveLink.textContent = serve.url || "";
    }
    const peerSelect = $("#remote-peer-select");
    if (peerSelect) {
      const prior = peerSelect.value;
      const peers = Array.isArray(data.mac_peers) ? data.mac_peers : [];
      peerSelect.innerHTML = peers.length ? "" : '<option value="">No Mac detected</option>';
      peers.forEach(peer => {
        const option = document.createElement("option");
        option.value = peer.node_id || "";
        option.textContent = `${peer.hostname || peer.dns_name || "Mac"}${peer.online ? " · online" : " · offline"}`;
        peerSelect.appendChild(option);
      });
      if (Array.from(peerSelect.options).some(o => o.value === prior)) peerSelect.value = prior;
    }
    const machineList = $("#remote-machine-list");
    if (machineList) {
      const machines = Array.isArray(data.machines) ? data.machines : [];
      machineList.innerHTML = machines.length ? machines.map(machine => `
        <div class="remote-machine" data-machine="${esc(machine.id)}">
          <strong>${esc(machine.label || "Paired Mac")}</strong>
          <small>${esc(machine.username || "user")}@${esc(machine.hostname || machine.ip || "Mac")} · ${esc(machine.status || "prepared")}</small>
          <span class="remote-machine-actions">
            ${machine.status !== "ready" ? '<button class="btn sm remote-verify" type="button">Verify</button>' : '<span class="hint">ready</span>'}
            <button class="iconbtn remote-remove" type="button" title="Remove pairing"><i class="ph ph-trash"></i></button>
          </span>
        </div>`).join("") : '<span class="hint">No Mac paired yet.</span>';
    }
  }

  let settingsReturnFocus = null;
  // closeSettings lives in another section; it reads the let through this.
  function settingsReturnTarget() { return settingsReturnFocus; }
  // While a settings search drives sections open/closed, the toggle handler
  // must not persist those forced moves as the user's accordion preference.
  async function openSettings() {
    closeSecurityOverview();
    closeFaq();
    closeUsageStats();
    initSettingsSections();
    if (settingsOpenPage) {
      showSettingsPage(settingsOpenPage);
      settingsOpenPage = "";
    }
    $("#drawer-scrim").classList.add("open");
    settingsReturnFocus = document.activeElement;
    $("#settings-drawer").classList.add("open");
    $("#settings-drawer").setAttribute("aria-hidden", "false");
    SETTINGS_PAGE_SHOWN[$("#settings-drawer").dataset.settingsPage]?.();
    $("#btn-close-settings").focus();
    touchedLoadControls.clear();
    populateSettingsForm();
    const health = loadModelHealth();
    await loadModels();
    loadSystemContext();
    loadDetectedVram();
    refreshSandboxStatus();
    loadClientContext().catch(() => {});
    await health;
  }

  function shortcutLabel(chord) {
    if (!chord) return "Not set";
    const names = {
      Ctrl: "Ctrl", Alt: "Alt", Shift: "Shift", Meta: "⌘",
      Space: "Space", Slash: "/", Backslash: "\\", BracketLeft: "[",
      BracketRight: "]", Semicolon: ";", Quote: "'", Comma: ",",
      Period: ".", Minus: "-", Equal: "=",
    };
    return chord.split("+").map(part => {
      if (names[part]) return names[part];
      if (/^Key[A-Z]$/.test(part)) return part.slice(3);
      if (/^Digit[0-9]$/.test(part)) return part.slice(5);
      return part.replace(/^Arrow/, "");
    }).join(" + ");
  }

  function shortcutChordFromEvent(event) {
    const modifierCodes = new Set([
      "ControlLeft", "ControlRight", "AltLeft", "AltRight",
      "ShiftLeft", "ShiftRight", "MetaLeft", "MetaRight",
    ]);
    if (!event.code || modifierCodes.has(event.code)) return "";
    const parts = [];
    if (event.ctrlKey) parts.push("Ctrl");
    if (event.altKey) parts.push("Alt");
    if (event.shiftKey) parts.push("Shift");
    if (event.metaKey) parts.push("Meta");
    parts.push(event.code);
    return parts.join("+");
  }

  // The action whose shortcut is being recorded (moved here from the state block,
  // next to the code that uses it). The reset button elsewhere clears it through
  // setShortcutRecording, since code in another file only sees a copy of a let.
  let shortcutRecording = null;
  function setShortcutRecording(action) { shortcutRecording = action; }

  function renderShortcutSettings() {
    const list = $("#shortcut-list");
    if (!list) return;
    const bindings = effectiveShortcuts();
    const defaults = defaultShortcuts();
    list.innerHTML = Object.entries(SHORTCUT_ACTIONS).map(([action, meta]) => {
      const chord = bindings[action] || "";
      const recording = shortcutRecording === action;
      const changed = action in defaults && chord !== defaults[action];
      return `<div class="shortcut-row">
        <span class="shortcut-icon"><i class="ph ${meta.icon}"></i></span>
        <span class="shortcut-copy"><strong>${esc(meta.label)}</strong><small>${esc(meta.detail)}${changed ? ` · default ${esc(shortcutLabel(defaults[action]))}` : ""}</small></span>
        ${changed ? `<button class="iconbtn shortcut-default" type="button" data-shortcut-default="${action}" title="Restore ${esc(shortcutLabel(defaults[action]))}" aria-label="Restore the default shortcut for ${esc(meta.label)}"><i class="ph ph-arrow-counter-clockwise"></i></button>` : ""}
        <button class="shortcut-capture${recording ? " recording" : ""}" type="button" data-shortcut-action="${action}" aria-label="Set shortcut for ${esc(meta.label)}" title="Click, then press keys. Backspace turns it off.">
          <kbd>${recording ? "Press keys…" : esc(shortcutLabel(chord))}</kbd>
        </button>
      </div>`;
    }).join("");
    list.querySelectorAll("[data-shortcut-action]").forEach(button => {
      button.addEventListener("click", () => {
        shortcutRecording = button.dataset.shortcutAction;
        renderShortcutSettings();
      });
    });
    list.querySelectorAll("[data-shortcut-default]").forEach(button => {
      button.addEventListener("click", () => {
        const next = { ...(state.settings?.keyboard_shortcuts || {}) };
        delete next[button.dataset.shortcutDefault];
        state.settings.keyboard_shortcuts = next;
        renderShortcutSettings();
        persistUiPreference({ keyboard_shortcuts: next });
        wbRefreshShortcutHints();
      });
    });
  }

  function finishShortcutRecording(chord) {
    const action = shortcutRecording;
    if (!action) return;
    const next = { ...(state.settings?.keyboard_shortcuts || {}) };
    if (!chord) {
      // Turning off a built-in default must be remembered; anything else is
      // simply unbound.
      if (action in defaultShortcuts()) next[action] = "";
      else delete next[action];
    } else {
      const conflict = Object.entries(effectiveShortcuts()).find(([other, value]) => other !== action && value === chord);
      if (conflict) {
        shortcutRecording = null;
        renderShortcutSettings();
        toast(`that combination is already used by ${SHORTCUT_ACTIONS[conflict[0]]?.label || "another action"}`, "warn", 3500);
        return;
      }
      next[action] = chord;
    }
    shortcutRecording = null;
    state.settings.keyboard_shortcuts = next;
    renderShortcutSettings();
    persistUiPreference({ keyboard_shortcuts: next });
    wbRefreshShortcutHints();
  }

  async function runShortcutAction(action) {
    if (action === "approve_pending" || action === "deny_pending") {
      const approval = [...state.approvals.values()].at(-1);
      if (!approval) {
        toast("there is no pending approval", "info", 1800);
        return;
      }
      if (action === "approve_pending" && approval.details?.critical) {
        toast("protected actions still require hold-to-approve", "warn", 3200);
        return;
      }
      decideApproval(approval.id, action === "approve_pending" ? "approve" : "deny", false);
      return;
    }
    if (action === "open_settings") {
      closeFaq();
      closeUsageStats();
      openSettings();
    } else if (action === "open_faq") {
      openFaq();
    } else if (action === "open_usage") {
      closeFaq();
      openUsageStats();
    } else if (action === "new_chat") {
      closeFaq();
      closeUsageStats();
      await newChat();
    } else if (action === "focus_composer") {
      closeFaq();
      closeUsageStats();
      closeSettings();
      $("#composer-input")?.focus();
    } else if (action === "stop_generation") {
      if (state.streaming) stopStreaming();
      else toast("nothing is generating", "info", 1500);
    } else {
      wbRunAction(action);
    }
  }

  function handleShortcutKeydown(event) {
    if (shortcutRecording) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === "Escape") {
        shortcutRecording = null;
        renderShortcutSettings();
        return;
      }
      if (event.key === "Backspace" || event.key === "Delete") {
        finishShortcutRecording("");
        return;
      }
      const chord = shortcutChordFromEvent(event);
      if (!chord) return;
      const guarded = event.ctrlKey || event.altKey || event.metaKey || /^F(?:[1-9]|1[0-2])$/.test(event.code);
      if (!guarded) {
        toast("use Ctrl, Alt, Command, or a function key in shortcuts", "warn", 3000);
        return;
      }
      finishShortcutRecording(chord);
      return;
    }

    if (event.repeat) return;
    const chord = shortcutChordFromEvent(event);
    if (!chord) return;
    // macOS users press ⌘ where the defaults say Ctrl.
    const alt = chord.startsWith("Meta+") ? "Ctrl+" + chord.slice(5) : "";
    const bindings = Object.entries(effectiveShortcuts());
    const match = bindings.find(([, value]) => value && value === chord)
      || (alt && bindings.find(([, value]) => value && value === alt));
    if (!match) return;
    // Full-screen pages and open dialogs own their keys; only the always-safe
    // actions run over them.
    if (wbShortcutBlocked(match[0])) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    wbShowKeycap(match[1], SHORTCUT_ACTIONS[match[0]]?.label || "");
    runShortcutAction(match[0]);
  }
  return {
    initSettingsSections, openSettingsAt, renderSettingsProfile, initEmailSettings, wbMcpShown,
    revealSettingsControl, loadModelHealth, checkModelAdvisor, renderRemoteAccess,
    settingsReturnTarget, openSettings, shortcutLabel, setShortcutRecording, renderShortcutSettings,
    handleShortcutKeydown,
  };
};
