/* SPDX-License-Identifier: AGPL-3.0-only */
/* sidebar: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["sidebar"] = ({
  $, SB_SECTIONS_KEY, THEME_CYCLE, addProjectFolder, clearConversations, esc, fetchFolderListing,
  folderLeafName, openFaq, openSecurityOverview, openSettings, openSettingsAt, renderChatList,
  renderTreeNode, state, stepTheme, wb, wbChatGroup, wbNorm, wbStore,
}) => {
  // Profile on top, Quick search, New session, Security overview, Customize,
  // a Sessions / Files switch over the list (projects holding their sessions,
  // or each project folder's file tree), then the theme
  // toggle, Support, Settings and today's usage. The old controls keep their
  // handlers (the new rows press them); what the new layout drops is hidden.
  function renderSidebarProfile() {
    const avatar = $("#sb-avatar");
    const nameEl = $("#sb-name");
    if (!avatar || !nameEl) return;
    const url = state.settings?.profile_avatar || "";
    const name = String(state.settings?.profile_name || "").trim();
    // Before a name or picture is set, the app's own mark stands in.
    avatar.innerHTML = url ? `<img src="${esc(url)}" alt="">`
      : name ? esc(name[0].toUpperCase())
      : `<img src="/assets/brand/logo-mark-${sbIsDark() ? "dark" : "light"}.png" alt="">`;
    avatar.classList.toggle("is-mark", !url && !name);
    nameEl.textContent = name || "Accuretta";
    const version = $("#brand-v")?.textContent || "";
    $("#sb-profile").title = `${name || "Accuretta"}${version ? ` · Accuretta ${version}` : ""} · Profile settings`;
  }

  function sbIsDark() {
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(bg)?.[1];
    if (!hex) return /dark|opal|dim/.test(document.documentElement.dataset.theme || "");
    const [r, g, b] = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16));
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) < 128;
  }
  function syncSidebarTheme() {
    const theme = document.documentElement.dataset.theme || "";
    const label = [...($("#set-theme")?.options || [])].find(o => o.value === theme)?.textContent || theme;
    const name = $("#sb-theme-label");
    if (name) {
      name.textContent = label;
      const at = THEME_CYCLE.indexOf(theme) + 1;
      name.closest("button").title = `Theme: ${label}${at ? ` (${at} of ${THEME_CYCLE.length})` : ""}. Click for the next one.`;
    }
    renderSidebarProfile();
  }

  function renderSidebarUsage() {
    const line = $("#sb-usage-line");
    if (!line) return;
    const n = Number(wb.status?.tokens_today) || 0;
    const tokens = n >= 1000 ? `${(n / 1000).toFixed(n >= 100000 ? 0 : 1)}K` : String(n);
    const saved = $("#cost-session")?.textContent?.trim() || "$0.00";
    const text = `${tokens} tokens · ${saved} saved`;
    if (line.textContent !== text) line.textContent = text;
  }

  // The list under the menu has two views that never mix: Sessions (projects
  // and their conversations) and Files (each project folder's file tree).
  const SB_VIEW_KEY = "accuretta:sb:view";
  let sbFilesStarted = false;
  function sbView() {
    return wbStore.get(SB_VIEW_KEY, "sessions") === "files" ? "files" : "sessions";
  }
  function sbSyncView() {
    const sidebar = $("#sidebar");
    if (!sidebar) return;
    const view = sbView();
    sidebar.dataset.view = view;
    sidebar.querySelectorAll(".sb-views [data-view]").forEach(btn => {
      const on = btn.dataset.view === view;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", String(on));
      btn.tabIndex = on ? 0 : -1;
    });
  }
  function sbSetView(view, openPath = "") {
    wbStore.set(SB_VIEW_KEY, view === "files" ? "files" : "sessions");
    if (view === "files") {
      if (openPath) sbOpenFiles.add(openPath);
      sbFilesStarted = false;
      sbRenderFilesView(true);
      if (openPath) {
        const target = [...document.querySelectorAll("#sb-files-view .sb-root")].find(el => el.dataset.path === openPath);
        target?.scrollIntoView({ block: "nearest" });
      }
    }
    sbSyncView();
  }
  // Every folder that can hold files: projects with sessions first (newest
  // first, as in the Sessions view), then project folders with none yet.
  function sbProjectRoots() {
    const roots = new Map();
    const add = (path, label) => {
      const key = path ? wbNorm(path) : "";
      if (key && !roots.has(key)) roots.set(key, { path, label: label || folderLeafName(path) });
    };
    const chats = state.chats?.chats || {};
    [...(state.chats?.order || [])].filter(id => chats[id])
      .sort((a, b) => (chats[b].updated || 0) - (chats[a].updated || 0))
      .forEach(id => { const g = wbChatGroup(chats[id]); add(g.path, g.label); });
    for (const folder of state.workspace?.folders || []) add(folder);
    return [...roots.values()];
  }
  function sbRenderFilesView(force = false) {
    const host = $("#sb-files-view");
    if (!host) return;
    const roots = sbProjectRoots();
    // Arriving with nothing open: start with the open session's project, or
    // the only project there is. Once per visit, so closing it sticks.
    if (sbView() === "files" && !sbFilesStarted && roots.length) {
      sbFilesStarted = true;
      if (!sbOpenFiles.size) {
        const current = wbChatGroup(state.chats?.chats?.[state.chatId] || {}).path;
        const start = roots.find(r => current && wbNorm(r.path) === wbNorm(current)) || (roots.length === 1 ? roots[0] : null);
        if (start) { sbOpenFiles.add(start.path); force = true; }
      }
    }
    // The session list re-renders on every chat event; the trees only need
    // rebuilding when the set of project folders changes.
    const signature = roots.map(r => `${r.path}\t${r.label}`).join("\n");
    if (!force && host._signature === signature) return;
    host._signature = signature;
    host.innerHTML = `<div class="sb-section-head"><span class="sb-section-label">Project files</span>`
      + `<button type="button" class="iconbtn sb-section-tool" data-act="refresh" title="Reload the file lists" aria-label="Reload the file lists"><i class="ph ph-arrow-clockwise" aria-hidden="true"></i></button>`
      + `<button type="button" class="iconbtn sb-section-tool" data-act="add" title="Add a project folder" aria-label="Add a project folder"><i class="ph ph-plus" aria-hidden="true"></i></button></div>`;
    host.querySelector('[data-act="add"]').addEventListener("click", addProjectFolder);
    host.querySelector('[data-act="refresh"]').addEventListener("click", () => {
      sbFileTrees.clear();
      sbRenderFilesView(true);
    });
    if (!roots.length) {
      const empty = document.createElement("button");
      empty.type = "button";
      empty.className = "sb-section-empty";
      empty.innerHTML = `<i class="ph ph-plus" aria-hidden="true"></i><span>Add a project folder</span>`;
      empty.addEventListener("click", addProjectFolder);
      host.appendChild(empty);
      return;
    }
    for (const root of roots) host.appendChild(sbFilesRoot(root));
  }

  // One project folder in the Files view: a row that opens its file tree.
  // Open trees survive re-rendering; closing one drops it, so reopening reads
  // the folder again.
  const sbOpenFiles = new Set();
  const sbFileTrees = new Map();
  function sbFilesRoot({ path, label }) {
    const wrap = document.createElement("div");
    wrap.className = "sb-root";
    wrap.dataset.path = path;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "sb-root-head";
    btn.title = path;
    btn.innerHTML = `<i class="ph ph-folder-simple sb-root-icon is-closed" aria-hidden="true"></i><i class="ph ph-folder-open sb-root-icon is-open" aria-hidden="true"></i>`
      + `<span class="sb-root-name">${esc(label)}</span>`;
    wrap.appendChild(btn);
    const show = async () => {
      let tree = sbFileTrees.get(path);
      if (!tree) {
        tree = document.createElement("div");
        tree.className = "ws-tree sb-files-tree";
        tree.innerHTML = `<div class="tree-loading" style="--depth:0">loading…</div>`;
        sbFileTrees.set(path, tree);
        const entries = await fetchFolderListing(path);
        tree.innerHTML = "";
        if (entries._error) tree.innerHTML = `<div class="tree-empty" style="--depth:0">${esc(entries._error)}</div>`;
        else if (!entries.length) tree.innerHTML = `<div class="tree-empty" style="--depth:0">empty</div>`;
        else for (const child of entries) tree.appendChild(renderTreeNode(child, 0, path));
      }
      if (sbOpenFiles.has(path)) wrap.appendChild(tree);
    };
    const sync = () => {
      const open = sbOpenFiles.has(path);
      wrap.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", String(open));
    };
    btn.addEventListener("click", () => {
      if (sbOpenFiles.has(path)) {
        sbOpenFiles.delete(path);
        sbFileTrees.get(path)?.remove();
        sbFileTrees.delete(path);
      } else {
        sbOpenFiles.add(path);
        show();
      }
      sync();
    });
    sync();
    if (sbOpenFiles.has(path)) show();
    return wrap;
  }

  function sbListSections(wrap) {
    const folded = new Set(wbStore.get(SB_SECTIONS_KEY, []));
    const make = (key, label, tool) => {
      const box = document.createElement("section");
      box.className = "sb-section" + (folded.has(key) ? " collapsed" : "");
      box.dataset.section = key;
      box.innerHTML = `<div class="sb-section-head"><button type="button" class="sb-section-toggle" aria-expanded="${!folded.has(key)}">`
        + `<span>${label}</span><i class="ph ph-caret-down" aria-hidden="true"></i></button>${tool}</div><div class="sb-section-body"></div>`;
      const toggle = box.querySelector(".sb-section-toggle");
      toggle.addEventListener("click", () => {
        const set = new Set(wbStore.get(SB_SECTIONS_KEY, []));
        const fold = box.classList.toggle("collapsed");
        if (fold) set.add(key); else set.delete(key);
        wbStore.set(SB_SECTIONS_KEY, [...set]);
        toggle.setAttribute("aria-expanded", String(!fold));
      });
      wrap.appendChild(box);
      return box;
    };
    const projects = make("projects", "Projects", `<button type="button" class="iconbtn sb-section-tool" title="Add a project folder" aria-label="Add a project folder"><i class="ph ph-plus" aria-hidden="true"></i></button>`);
    const chats = make("chats", "Chats", `<button type="button" class="iconbtn sb-section-tool" title="Clear conversations" aria-label="Clear conversations"><i class="ph ph-trash" aria-hidden="true"></i></button>`);
    projects.querySelector(".sb-section-tool").addEventListener("click", addProjectFolder);
    chats.querySelector(".sb-section-tool").addEventListener("click", clearConversations);
    return { projects: projects.querySelector(".sb-section-body"), chats: chats.querySelector(".sb-section-body") };
  }
  function sbFillEmptySections(sections) {
    if (!sections.projects.children.length) {
      sections.projects.innerHTML = `<button type="button" class="sb-section-empty"><i class="ph ph-plus" aria-hidden="true"></i><span>Add a project folder</span></button>`;
      sections.projects.firstChild.addEventListener("click", addProjectFolder);
    }
    if (!sections.chats.children.length) sections.chats.innerHTML = `<div class="sb-section-empty">No chats yet</div>`;
  }
  function sbRevealSection(key) {
    const set = new Set(wbStore.get(SB_SECTIONS_KEY, []));
    if (!set.delete(key)) return;
    wbStore.set(SB_SECTIONS_KEY, [...set]);
    const box = document.querySelector(`#chatlist .sb-section[data-section="${key}"]`);
    box?.classList.remove("collapsed");
    box?.querySelector(".sb-section-toggle")?.setAttribute("aria-expanded", "true");
  }

  function wbInitSidebar() {
    const sidebar = $("#sidebar");
    if (!sidebar || sidebar.classList.contains("sb-v2")) return;
    sidebar.classList.add("sb-v2");

    const profile = document.createElement("button");
    profile.type = "button";
    profile.id = "sb-profile";
    profile.className = "sb-profile";
    profile.innerHTML = `<span class="sb-avatar" id="sb-avatar" aria-hidden="true"></span><span class="sb-name" id="sb-name"></span>`
      + `<i class="ph ph-caret-up-down" aria-hidden="true"></i>`;
    profile.addEventListener("click", () => openSettingsAt("profile"));
    sidebar.querySelector(".brand-row")?.prepend(profile);

    const nav = sidebar.querySelector(".workspace-nav");
    const search = $("#btn-search-sessions");
    if (nav && search) {
      const label = search.querySelector("span");
      if (label) label.textContent = "Quick search";
      nav.prepend(search);
      nav.insertAdjacentHTML("beforeend",
        `<button type="button" class="workspace-nav-action" id="sb-security" title="Security overview"><i class="ph ph-lightning" aria-hidden="true"></i><span>Security overview</span></button>`
        + `<button type="button" class="workspace-nav-action" id="sb-customize" title="Customize"><i class="ph ph-sliders-horizontal" aria-hidden="true"></i><span>Customize</span></button>`);
      $("#sb-customize").addEventListener("click", () => openSettingsAt("skills"));
      $("#sb-security").addEventListener("click", openSecurityOverview);
    }

    const bottom = document.createElement("div");
    bottom.className = "sb-bottom";
    bottom.innerHTML = `<div class="sb-theme" role="group" aria-label="Theme">`
      + `<button type="button" class="sb-theme-step" data-step="-1" title="Previous theme" aria-label="Previous theme"><i class="ph ph-caret-left" aria-hidden="true"></i></button>`
      + `<button type="button" class="sb-theme-name" data-step="1"><span class="sb-theme-swatch" aria-hidden="true"></span><span id="sb-theme-label">Theme</span></button>`
      + `<button type="button" class="sb-theme-step" data-step="1" title="Next theme" aria-label="Next theme"><i class="ph ph-caret-right" aria-hidden="true"></i></button></div>`
      + `<button type="button" class="sb-link" id="sb-support" title="Support"><i class="ph ph-headset" aria-hidden="true"></i><span>Support</span></button>`
      + `<div class="sb-link-row"><button type="button" class="sb-link" id="sb-settings" title="Settings"><i class="ph ph-gear-six" aria-hidden="true"></i><span>Settings</span></button></div>`
      + `<button type="button" class="sb-usage" id="sb-usage" title="Usage stats">`
      + `<span class="sb-usage-icon" aria-hidden="true"><i class="ph ph-chart-bar"></i></span>`
      + `<span class="sb-usage-text"><b>Today</b><small id="sb-usage-line">0 tokens</small></span>`
      + `<span class="sb-usage-btn">Usage</span></button>`;
    // Below the scrolling list, so it stays at the bottom of the sidebar.
    sidebar.querySelector(".sidebar-scroll")?.after(bottom);
    // Steps through every theme, both ways; the dot and name show the current one.
    bottom.querySelectorAll(".sb-theme [data-step]").forEach(btn =>
      btn.addEventListener("click", () => stepTheme(Number(btn.dataset.step))));
    $("#sb-support").addEventListener("click", openFaq);
    $("#sb-settings").addEventListener("click", () => openSettings());
    $("#sb-usage").addEventListener("click", () => openSettingsAt("usage", "stats"));
    // In a browser the power button sits beside Settings (the desktop app's
    // window X opens the same dialog).
    const power = $("#btn-shutdown-side");
    if (power) bottom.querySelector(".sb-link-row").appendChild(power);

    new MutationObserver(syncSidebarTheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style"] });
    const cost = $("#cost-session");
    if (cost) new MutationObserver(renderSidebarUsage).observe(cost, { childList: true, characterData: true, subtree: true });
    syncSidebarTheme();
    renderSidebarUsage();
    // Sessions / Files switch above the list, and the Files view beside it.
    const views = document.createElement("div");
    views.className = "sb-views";
    views.setAttribute("role", "tablist");
    views.setAttribute("aria-label", "Sidebar view");
    views.innerHTML = `<button type="button" role="tab" data-view="sessions">Sessions</button>`
      + `<button type="button" role="tab" data-view="files">Files</button>`;
    views.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-view]");
      if (btn) sbSetView(btn.dataset.view);
    });
    views.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      sbSetView(sbView() === "files" ? "sessions" : "files");
      views.querySelector(".active")?.focus();
    });
    sidebar.querySelector(".sidebar-scroll")?.before(views);
    const filesView = document.createElement("div");
    filesView.id = "sb-files-view";
    filesView.className = "sb-files-view";
    $("#chatlist")?.after(filesView);
    if (sbView() === "files") sbSetView("files"); else sbSyncView();

    // The list may have drawn before this layout existed; redraw it in sections.
    if (state.chats) renderChatList();
  }
  return {
    renderSidebarProfile, renderSidebarUsage, sbSetView, sbRenderFilesView, sbListSections,
    sbFillEmptySections, sbRevealSection, wbInitSidebar,
  };
};
