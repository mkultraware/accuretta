/* SPDX-License-Identifier: AGPL-3.0-only */
/* chat: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["chat"] = ({
  $, api, app, applyMobileTab, autoResize, closeSecurityOverview, confirmModal, esc,
  foldBoundaryVisibleIdx, isCompactViewport, isMobile, isRenderedMessage, leaveWelcome, loadChats,
  loadVersions, refreshSessionDesktopState, renderBubble, renderChatList, renderCostWidget,
  renderEditBanner, renderMessageQueue, renderMessages, renderSkillPill, renderStatus,
  renderTokTotal, renderWorkspace, renderWorkspaceIdentity, scheduleMessageQueueDrain,
  scrollToBottom, setComposerMode, setGoalModeUI, state, toast, wbNorm, wbOnChatSelected,
  wireOverflow,
}) => {
  async function newChat() {
    closeSecurityOverview();
    // Tag where the session was born (the chat list shows a phone icon for
    // mobile sessions). The bridge persists it; it never changes after creation.
    const origin = isMobile() ? "mobile" : "desktop";
    const c = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "new session", origin }),
    }).catch(() => null);
    if (!c || !c.id) {
      toast(`Couldn't start a new session: ${(c && c.error) || "the app didn't answer"}`, "err", 4000);
      return;
    }
    await loadChats();
    await selectChat(c.id);
    const ta = $("#composer-input");
    ta.value = "";
    autoResize(ta);
  }

  async function refreshContextStats(chatId = state.chatId) {
    if (!chatId) return;
    try {
      const result = await api("/api/ctx-stats?chat_id=" + encodeURIComponent(chatId));
      if (!result || typeof result.prompt_tokens !== "number" || state.chatId !== chatId) return;
      if (Number.isFinite(result.capacity) && result.capacity > 0) state._ctxCapacity = result.capacity;
      // The poll reports the last completed round, while ctx_fill SSE pushes the
      // next round's estimate mid-turn; a stale poll would make the gauge jump
      // backwards. While streaming, never regress.
      if (state.streaming && result.prompt_tokens < Number(state._lastMsgPromptTokens || 0)) {
        renderStatus();
        return;
      }
      state._lastMsgPromptTokens = result.prompt_tokens;
      state._ctxSource = result.source || "";
      renderStatus();
    } catch (_) {}
  }

  function renderTopbarContext(chat) {
    renderWorkspaceIdentity(chat);
    renderProjectChip(chat);
    const chatContext = $("#topbar-chat-context");
    const projectContext = $("#topbar-project-context");
    const branch = $("#topbar-branch");
    const branchName = $("#topbar-branch-name");
    const projectName = $("#topbar-project-name");
    const worktree = chat?.github_worktree || chat?.project_workspace || null;
    const project = String(worktree?.repo || worktree?.name || "").trim();
    const branchValue = String(worktree?.branch || "").trim();

    $("#chat-title").textContent = chat ? chat.title : "new session";
    if (!chatContext || !projectContext) return;

    const showProject = !!project;
    chatContext.hidden = showProject;
    projectContext.hidden = !showProject;
    if (!showProject) return;

    projectName.textContent = project;
    projectContext.title = String(worktree?.path || project);
    branch.hidden = !branchValue;
    branchName.textContent = branchValue;
  }

  function projectPathKey(value) {
    return String(value || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  }

  function projectBaseName(value) {
    return String(value || "").replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
  }

  function projectPointerForChat(chat) {
    const pointer = chat?.github_worktree || chat?.project_workspace || null;
    const path = String(pointer?.path || "").trim();
    if (!path) return null;
    return { path, name: String(pointer.name || pointer.repo || projectBaseName(path)) };
  }

  function renderProjectChip(chat = state.chats?.chats?.[state.chatId]) {
    const chip = $("#project-chip");
    const label = $("#project-chip-label");
    if (!chip || !label) return;
    const pointer = projectPointerForChat(chat);
    label.textContent = pointer ? pointer.name : "No project";
    // A folder picked up from the agent's own work says so on hover.
    const auto = !!chat?.project_workspace?.auto && !chat?.github_worktree;
    chip.title = pointer ? (auto ? `${pointer.path}\nThe agent is working here. Click to choose another folder.` : pointer.path)
      : "Choose the folder this session works on";
    chip.classList.toggle("on", !!pointer);
  }

  function projectCandidates(limit = 6) {
    const seen = new Set();
    const recent = [];
    const folders = [];
    const push = (list, pointer) => {
      if (!pointer) return;
      const key = projectPathKey(pointer.path);
      if (!key || seen.has(key)) return;
      seen.add(key);
      list.push(pointer);
    };
    for (const id of state.chats?.order || []) {
      push(recent, projectPointerForChat(state.chats?.chats?.[id]));
      if (recent.length >= limit) break;
    }
    for (const folder of state.workspace?.folders || []) {
      const path = String(folder || "").trim();
      if (!path) continue;
      push(folders, { path, name: projectBaseName(path) });
    }
    return { recent, folders };
  }

  // Folder browser inside the project menu: a stack of folders the user opened
  // with a row's chevron. Empty = the top list (recent + workspace folders);
  // "" on the stack = this computer's starting points (home, drives).
  const projectBrowse = { stack: [] };
  const PROJECT_BROWSE_ROOTS = "";

  // A project must sit in a workspace folder (that's what scopes the agent's
  // file tools); one picked outside gets added, as Open folder… does.
  function projectInWorkspace(path) {
    const key = projectPathKey(path);
    return !!key && (state.workspace?.folders || []).some(root => {
      const rootKey = projectPathKey(root);
      return rootKey && (key === rootKey || key.startsWith(rootKey + "/"));
    });
  }

  // Tool and cache folders that are never a project.
  const PROJECT_BROWSE_SKIP = new Set([".git", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox", ".idea"]);

  function projectRow(pointer, currentKey, { showPath = true } = {}) {
    const selected = projectPathKey(pointer.path) === currentKey;
    const name = pointer.name || projectBaseName(pointer.path);
    // Home and drives are places to start from, never a project themselves
    // (a whole drive would hand the agent's file tools the whole drive).
    if (pointer.kind === "drive" || pointer.kind === "home") {
      return `<button type="button" class="mm-row" role="menuitem" data-drill-path="${esc(pointer.path)}" title="${esc(pointer.path)}">
        <i class="ph ${pointer.kind === "drive" ? "ph-hard-drives" : "ph-house"}" aria-hidden="true"></i>
        <span class="mm-row-name">${esc(name)}</span>
        ${showPath ? `<span class="mm-row-sub">${esc(pointer.path)}</span>` : ""}
        <i class="ph ph-caret-right project-row-caret" aria-hidden="true"></i>
      </button>`;
    }
    const drill = `<button type="button" class="project-drill" role="menuitem" data-drill-path="${esc(pointer.path)}" title="Show the folders inside ${esc(name)}" aria-label="Show the folders inside ${esc(name)}"><i class="ph ph-caret-right" aria-hidden="true"></i></button>`;
    const icon = selected ? "ph-folder-open" : "ph-folder-simple";
    return `<div class="project-row">
      <button type="button" class="mm-row${selected ? " loaded" : ""}" role="menuitemradio" aria-checked="${selected}" data-project-path="${esc(pointer.path)}" data-project-name="${esc(name)}" title="${esc(pointer.path)}">
        <i class="ph ${icon}" aria-hidden="true"></i>
        <span class="mm-row-name">${esc(name)}</span>
        ${showPath ? `<span class="mm-row-sub">${esc(pointer.path)}</span>` : ""}
      </button>${drill}
    </div>`;
  }

  function renderProjectMenu() {
    const menu = $("#project-menu");
    if (!menu) return;
    if (projectBrowse.stack.length) return renderProjectBrowse(menu);
    const current = projectPointerForChat(state.chats?.chats?.[state.chatId]);
    const currentKey = current ? projectPathKey(current.path) : "";
    const { recent, folders } = projectCandidates();
    const row = (pointer) => projectRow(pointer, currentKey);
    const parts = [];
    if (recent.length) {
      parts.push('<div class="overflow-section-label">Recent</div>');
      parts.push(...recent.map(row));
    }
    if (folders.length) {
      parts.push('<div class="overflow-section-label">Workspace folders</div>');
      parts.push(...folders.map(row));
    }
    parts.push(`<button type="button" class="mm-row" data-drill-path="${PROJECT_BROWSE_ROOTS}" role="menuitem" title="Pick any folder on this computer"><i class="ph ph-hard-drives" aria-hidden="true"></i><span class="mm-row-name">Browse this computer</span><i class="ph ph-caret-right project-row-caret" aria-hidden="true"></i></button>`);
    parts.push('<div class="overflow-section-label">Session</div>');
    if (current) {
      parts.push('<button type="button" class="mm-row" id="project-clear" role="menuitem"><i class="ph ph-x" aria-hidden="true"></i><span class="mm-row-name">No project</span></button>');
    }
    parts.push('<button type="button" class="mm-row" id="project-open-folder" role="menuitem" title="Add a folder from this computer to the workspace"><i class="ph ph-folder-open" aria-hidden="true"></i><span class="mm-row-name">Open folder…</span></button>');
    menu.innerHTML = parts.join("");
    menu._reposition?.();
  }

  // One folder's subfolders: back, "use this folder", then each subfolder
  // (click to use it, chevron to go further in). The roots entry lists the
  // home folder and drives instead.
  async function renderProjectBrowse(menu) {
    const path = projectBrowse.stack[projectBrowse.stack.length - 1];
    const atRoots = path === PROJECT_BROWSE_ROOTS;
    const name = atRoots ? "This computer" : projectBaseName(path);
    const current = projectPointerForChat(state.chats?.chats?.[state.chatId]);
    const currentKey = current ? projectPathKey(current.path) : "";
    const useThis = atRoots ? "" : `<button type="button" class="mm-row${projectPathKey(path) === currentKey ? " loaded" : ""}" role="menuitemradio" aria-checked="${projectPathKey(path) === currentKey}" data-project-path="${esc(path)}" data-project-name="${esc(name)}" title="${esc(path)}">
        <i class="ph ph-check" aria-hidden="true"></i>
        <span class="mm-row-name">Use this folder</span>
      </button>`;
    const head = `<button type="button" class="mm-row project-back" id="project-back" role="menuitem" title="Back">
        <i class="ph ph-caret-left" aria-hidden="true"></i>
        <span class="mm-row-name">${esc(name)}</span>
        ${atRoots ? "" : `<span class="mm-row-sub">${esc(path)}</span>`}
      </button>${useThis}
      <div class="overflow-section-label">${atRoots ? "Start from" : "Folders inside"}</div>`;
    menu.innerHTML = head + '<div class="project-menu-empty">Loading…</div>';
    menu._reposition?.();
    let listing;
    try {
      listing = await api(`/api/list-subfolders?path=${encodeURIComponent(path)}`);
    } catch (e) {
      listing = { error: e.message || String(e), entries: [] };
    }
    // The user may have moved on (back, deeper, closed) while this loaded.
    if (projectBrowse.stack[projectBrowse.stack.length - 1] !== path || !menu.classList.contains("open")) return;
    const dirs = (listing.entries || []).filter(e => !PROJECT_BROWSE_SKIP.has(e.name));
    let body;
    if (listing.error) {
      body = `<div class="project-menu-empty">Couldn't open this folder: ${esc(listing.error)}</div>`;
    } else {
      body = dirs.length
        ? dirs.map(e => projectRow(e, currentKey, { showPath: atRoots && e.kind === "home" })).join("")
        : '<div class="project-menu-empty">No folders inside this one.</div>';
    }
    menu.innerHTML = head + body;
    menu._reposition?.();
  }

  async function setChatProject(pointer) {
    if (!state.chatId) {
      toast("Open a session before choosing a project.", "error");
      return false;
    }
    try {
      const updated = await api(`/api/chats/${encodeURIComponent(state.chatId)}/project`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_workspace: pointer }),
      });
      if (!updated || updated.error) throw new Error((updated && updated.error) || "the bridge refused the project");
      const id = state.chatId;
      state.chats.chats[id] = updated;
      renderProjectChip(updated);
      renderTopbarContext(updated);
      renderChatList();
      return true;
    } catch (error) {
      toast("could not set project: " + (error.message || error), "error");
      return false;
    }
  }

  // The native pickers answer {path, error}. An empty path alone means the user
  // cancelled; an error means the picker never opened, so say why.
  function pickedPath(result, what) {
    if (result?.error) toast(`couldn't open the ${what} picker: ${result.error}`, "error", 6000);
    return String(result?.path || "").trim();
  }

  async function browseForFolder() {
    try {
      const result = await api("/api/browse-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      return pickedPath(result, "folder");
    } catch (error) {
      toast("could not open the folder picker: " + (error.message || error), "error");
      return "";
    }
  }

  async function addFolderToWorkspace(picked) {
    const pickedKey = projectPathKey(picked);
    const inWorkspace = (state.workspace?.folders || []).some(root => {
      const rootKey = projectPathKey(root);
      return pickedKey === rootKey || pickedKey.startsWith(rootKey + "/");
    });
    if (inWorkspace) return true;
    const folders = Array.from(new Set([...(state.workspace?.folders || []), picked]));
    try {
      const workspace = await api("/api/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folders }),
      });
      if (!workspace || workspace.error) throw new Error((workspace && workspace.error) || "the bridge refused the folder");
      state.workspace = workspace;
      renderWorkspace();
      return true;
    } catch (error) {
      toast("could not add the folder to the workspace: " + (error.message || error), "error");
      return false;
    }
  }

  async function pickProjectFolder() {
    const picked = await browseForFolder();
    if (!picked) return;
    if (!(await addFolderToWorkspace(picked))) return;
    await setChatProject({ path: picked, name: projectBaseName(picked) });
  }

  async function addProjectFolder() {
    const picked = await browseForFolder();
    if (!picked) return;
    if (!(await addFolderToWorkspace(picked))) return;
    renderProjectScopeRow();
    toast(`added ${projectBaseName(picked)} to the project list`);
  }

  const PROJECT_SCOPE_NONE = "__none__";

  function projectScopeMatches(chat) {
    const scope = state.projectScope || "";
    if (!scope) return true;
    const pointer = projectPointerForChat(chat);
    if (scope === PROJECT_SCOPE_NONE) return !pointer;
    return !!pointer && projectPathKey(pointer.path) === scope;
  }

  function projectScopeInfo() {
    const scope = state.projectScope || "";
    if (!scope) return { label: "All projects", icon: "ph-folders", scoped: false };
    if (scope === PROJECT_SCOPE_NONE) return { label: "No project", icon: "ph-folder-dashed", scoped: true };
    const { recent, folders } = projectCandidates(50);
    const hit = [...recent, ...folders].find(p => projectPathKey(p.path) === scope);
    return { label: (hit && hit.name) || projectBaseName(scope) || scope, icon: "ph-folder-simple", scoped: true };
  }

  function renderProjectScopeRow() {
    const btn = $("#btn-project-scope");
    const label = $("#project-scope-label");
    const icon = $("#project-scope-icon");
    if (!btn || !label) return;
    const info = projectScopeInfo();
    label.textContent = info.label;
    if (icon) icon.className = `ph ${info.icon}`;
    btn.classList.toggle("scoped", info.scoped);
    btn.title = info.scoped ? `Conversations in ${info.label} — click to change` : "Filter conversations by project";
  }

  function renderProjectScopeMenu() {
    const menu = $("#project-scope-menu");
    if (!menu) return;
    const scope = state.projectScope || "";
    const { recent, folders } = projectCandidates(50);
    const seen = new Set();
    const projects = [];
    for (const pointer of [...recent, ...folders]) {
      const key = projectPathKey(pointer.path);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      projects.push(pointer);
    }
    const row = (value, name, path, icon) => {
      const active = scope === value;
      return `<button type="button" class="mm-row${active ? " loaded" : ""}" role="menuitemradio" aria-checked="${active}" data-scope="${esc(value)}" title="${esc(path || name)}">
        <i class="ph ${icon}" aria-hidden="true"></i>
        <span class="mm-row-name">${esc(name)}</span>
        ${path ? `<span class="mm-row-sub">${esc(path)}</span>` : ""}
        ${active ? '<i class="ph ph-check" aria-hidden="true"></i>' : ""}
      </button>`;
    };
    const parts = [
      row("", "All projects", "", "ph-folders"),
      row(PROJECT_SCOPE_NONE, "No project", "", "ph-folder-dashed"),
    ];
    if (projects.length) {
      parts.push('<div class="overflow-section-label">Projects</div>');
      for (const pointer of projects) {
        parts.push(row(projectPathKey(pointer.path), pointer.name || projectBaseName(pointer.path), pointer.path, "ph-folder-simple"));
      }
    }
    menu.innerHTML = parts.join("");
  }

  function setProjectScope(scope) {
    state.projectScope = scope || "";
    renderProjectScopeRow();
    renderChatList();
  }

  async function clearConversations() {
    const targets = (state.chats?.order || [])
      .map(id => state.chats.chats[id])
      .filter(Boolean)
      .filter(projectScopeMatches);
    if (!targets.length) {
      toast("no conversations to clear here", "error");
      return;
    }
    const info = projectScopeInfo();
    const plural = targets.length === 1 ? "session" : "sessions";
    const ok = await confirmModal({
      title: info.scoped ? `Clear ${info.label}` : "Clear conversations",
      message: `This deletes ${targets.length} ${plural}${info.scoped ? ` in ${info.label}` : ""} and all their saved versions. This can't be undone.`,
      confirmText: "Clear",
      danger: true,
      icon: "ph-trash",
    });
    if (!ok) return;
    const ids = new Set(targets.map(c => c.id));
    for (const id of ids) {
      try {
        await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" });
      } catch (error) {
        // keep going: one failed delete must not strand the rest
      }
      localStorage.removeItem("accuretta:draft:" + id);
      state.messageQueues.delete(id);
    }
    if (state.queueEditId && ids.has(state.queueEditId)) state.queueEditId = null;
    await loadChats();
    if (state.chatId && ids.has(state.chatId)) {
      if (state.chats.order.length) selectChat(state.chats.order[0]);
      else await newChat();
    }
    renderChatList();
  }

  // Removes one project from the sidebar: its sessions are deleted (as Clear
  // conversations does) and, if it is a workspace folder, it leaves the
  // workspace. The folder and its files on disk are never touched.
  async function removeProjectGroup(group) {
    const ids = new Set(group.ids || []);
    const folder = (state.workspace?.folders || []).find(f => wbNorm(f) === wbNorm(group.path || ""));
    const plural = ids.size === 1 ? "session" : "sessions";
    const ok = await confirmModal({
      title: `Remove ${group.label}`,
      message: (ids.size ? `This deletes its ${ids.size} ${plural} and all their saved versions, which can't be undone. ` : "")
        + "The folder and its files stay on disk.",
      confirmText: "Remove",
      danger: true,
      icon: "ph-trash",
    });
    if (!ok) return;
    for (const id of ids) {
      try {
        await fetch(`/api/chats/${encodeURIComponent(id)}`, { method: "DELETE" });
      } catch (error) {
        // keep going: one failed delete must not strand the rest
      }
      localStorage.removeItem("accuretta:draft:" + id);
      state.messageQueues.delete(id);
    }
    if (state.queueEditId && ids.has(state.queueEditId)) state.queueEditId = null;
    if (folder) {
      const next = state.workspace.folders.filter(f => f !== folder);
      const r = await api("/api/workspace", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ folders: next }),
      }).catch(error => ({ error: error.message || String(error) }));
      if (r?.error) toast("couldn't remove the folder from the workspace: " + r.error, "error");
      else state.workspace.folders = next;
    }
    await loadChats();
    if (state.chatId && ids.has(state.chatId)) {
      if (state.chats.order.length) selectChat(state.chats.order[0]);
      else await newChat();
    }
    renderWorkspace();
    renderChatList();
  }

  function initProjectScope() {
    const btn = $("#btn-project-scope");
    const menu = $("#project-scope-menu");
    if (!btn || !menu) return;
    const addBtn = $("#btn-project-add");
    const clearBtn = $("#btn-clear-conversations");
    btn.addEventListener("click", () => {
      const willOpen = !menu.classList.contains("open");
      btn.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) renderProjectScopeMenu();
    });
    menu.addEventListener("click", (event) => {
      const target = event.target.closest("[data-scope]");
      if (!target) return;
      menu.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
      setProjectScope(target.dataset.scope || "");
    });
    if (addBtn) addBtn.addEventListener("click", addProjectFolder);
    if (clearBtn) clearBtn.addEventListener("click", clearConversations);
    wireOverflow("#btn-project-scope", "#project-scope-menu");
  }

  function initProjectPicker() {
    const chip = $("#project-chip");
    const menu = $("#project-menu");
    if (!chip || !menu) return;
    chip.addEventListener("click", () => {
      const willOpen = !menu.classList.contains("open");
      chip.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) {
        projectBrowse.stack = [];         // every open starts at the top list
        renderProjectMenu();
      }
    });
    menu.addEventListener("click", async (event) => {
      // Going into a folder or back out re-renders the menu, which detaches
      // the clicked button; stop here so the outside-click handler doesn't
      // read that as a click outside the menu and close it.
      const nav = event.target.closest("[data-drill-path], #project-back");
      if (nav) {
        event.stopPropagation();
        if (nav.id === "project-back") projectBrowse.stack.pop();
        else projectBrowse.stack.push(nav.dataset.drillPath);
        renderProjectMenu();
        return;
      }
      const target = event.target.closest("[data-project-path], #project-clear, #project-open-folder");
      if (!target) return;
      projectBrowse.stack = [];
      menu.classList.remove("open");
      chip.setAttribute("aria-expanded", "false");
      if (target.id === "project-open-folder") return pickProjectFolder();
      if (target.id === "project-clear") return setChatProject(null);
      const path = target.dataset.projectPath || "";
      // Outside the workspace: add it first, exactly like Open folder… does.
      if (!projectInWorkspace(path) && !(await addFolderToWorkspace(path))) return;
      await setChatProject({ path, name: target.dataset.projectName || projectBaseName(path) });
    });
    wireOverflow("#project-chip", "#project-menu");
  }

  // A pending "open this session" request from the notch's Open-in-main-app
  // button, applied here (and retried on window focus) so the overlay host's
  // window raise lands on the requested conversation.
  function openRequestedChat() {
    const cid = state._pendingOpenChat;
    if (!cid) return;
    if (state.chats?.chats?.[cid]) {
      state._pendingOpenChat = "";
      selectChat(cid);
      return;
    }
    loadChats().then(() => {
      renderChatList();
      if (state._pendingOpenChat === cid && state.chats?.chats?.[cid]) {
        state._pendingOpenChat = "";
        selectChat(cid);
      }
    }).catch(() => {});
  }

  async function selectChat(id) {
    document.querySelector('#revealer-deck .research-rail')?.remove();
    if (state.chatId) localStorage.setItem("accuretta:draft:" + state.chatId, $("#composer-input").value);
    state.chatId = id;
    // A chat switch ends any in-progress edit: the banner belongs to one chat.
    if (state.editingTurn) {
      state.editingTurn = null;
      $("#composer-input")?.classList.remove("is-editing");
      renderEditBanner();
    }
    let chat = state.chats.chats[id];
    // loadChats() keeps a loaded copy's messages across rail refreshes, so a
    // session that moved on elsewhere (a notch turn, another window) would reopen
    // from that stale copy; the summary's `updated` tells. A turn this window is
    // attached to keeps the cached copy: the re-attach below restores its prompt,
    // and a refetched copy would show it twice.
    const attached = !!(state.liveTurn && state.liveTurn.chatId === id && state.streaming);
    const stale = !attached && !!chat && !chat._summary && Array.isArray(chat.messages)
      && chat.updated != null && chat._loadedUpdated !== chat.updated;
    if (!chat || chat._summary || !Array.isArray(chat.messages) || stale) {
      let fresh = null;
      try {
        fresh = await api(`/api/chats/${encodeURIComponent(id)}`);
      } catch (error) {
        // A stale copy still renders; only a missing one is an error.
        if (!stale) {
          if (state.chatId === id) toast("could not load session: " + (error.message || error), "error");
          return;
        }
      }
      if (state.chatId !== id) return;
      if (fresh) {
        chat = fresh;
        chat._summary = false;
        chat._loadedUpdated = chat.updated;
        state.chats.chats[id] = chat;
      }
    }
    state.activeSkill = null;
    if (chat && chat.active_skill) {
      state.activeSkill = { name: chat.active_skill, budget: chat.active_skill_budget || 0, body_chars: 0 };
    }
    renderSkillPill();
    // Reflect this chat's /goal latch on the Access chip (persisted per chat).
    setGoalModeUI(!!(chat && chat.goal_mode), { goal: (chat && chat.goal_text) || "" });
    
    // Scan past messages to populate the image-to-source map
    state.imageUrlToSourceMap.clear();
    if (chat && chat.messages) {
      for (const m of chat.messages) {
        if (m.content) {
          try {
            const parsed = JSON.parse(m.content);
            if (parsed && parsed.results && Array.isArray(parsed.results)) {
              parsed.results.forEach(r => {
                if (r.image && r.url) {
                  state.imageUrlToSourceMap.set(r.image, r.url);
                }
              });
            }
          } catch (_) {}
        }
      }
    }
    
    // Only render visible bubbles (isRenderedMessage).
    state.messages = chat ? (chat.messages || []).filter(isRenderedMessage) : [];
    state.messageWindowStart = Math.max(
      0, state.messages.length - state.messageWindowSize);
    if (chat) {
      const boundary = foldBoundaryVisibleIdx(chat);
      state.foldBoundaryIdx = boundary.idx;
      state.foldMark = boundary.stats;
    } else {
      state.foldBoundaryIdx = -1;
      state.foldMark = null;
    }
    renderTopbarContext(chat);
    // Restore the last-used mode for this chat so the toolbar feels sticky.
    // IDE is unavailable in compact layouts because the preview pane is hidden.
    if (chat && chat.last_mode) {
      setComposerMode(chat.last_mode);
    } else if (isMobile() && state.mode === "ide") {
      setComposerMode("agent");
    } else {
      setComposerMode(state.mode);
    }
    let chatPromptTok = 0;
    let chatOutTok = 0;
    if (chat && chat.messages) {
      for (const m of chat.messages) {
        if (Number.isFinite(m.prompt_tokens)) {
          chatPromptTok += m.prompt_tokens;
        }
        if (Number.isFinite(m.tokens)) {
          chatOutTok += m.tokens;
        }
      }
    }
    state.tokTotal = chatOutTok;
    state.tokPromptTotal = chatPromptTok;
    // Seed the context gauge from the newest message carrying a real
    // prompt-token count, so a freshly loaded chat shows true fill immediately.
    state._lastMsgPromptTokens = 0;
    state._ctxBreakdown = null;
    state._ctxSource = "";
    if (chat && chat.messages) {
      for (let i = chat.messages.length - 1; i >= 0; i--) {
        const pt = chat.messages[i].prompt_tokens;
        if (Number.isFinite(pt) && pt > 0) { state._lastMsgPromptTokens = pt; state._ctxSource = "live"; break; }
      }
    }
    state.totalGenDuration = 0;
    state._streamOutEstimate = 0;
    state._streamPromptEstimate = 0;
    renderTokTotal();
    renderCostWidget();
    refreshSessionDesktopState();
    renderMessages();
    // If this chat has a still-running turn, re-attach the live agent row and the
    // unpersisted prompt row. The stream keeps painting into the same node, so
    // this restores the partial text exactly where the model is; the turn isn't in
    // the persisted chat.messages until it completes.
    if (state.liveTurn && state.liveTurn.chatId === id && state.streaming && state.liveTurn.row) {
      const inner = $("#chat-inner");
      // renderMessages only knows the persisted copy, which lacks the live turn's
      // first message, so it painted the welcome screen. Remove the whole welcome
      // layout (heading, centred composer, recents, dot field), as send() does
      // before appending.
      const welcome = inner.querySelector(".welcome-screen");
      if (welcome) { welcome.remove(); leaveWelcome(); }
      if (state.liveTurn.userMsg && !state.liveTurn.userMsg.invisible && state.messages.indexOf(state.liveTurn.userMsg) === -1) {
        state.messages.push(state.liveTurn.userMsg);
        inner.appendChild(renderBubble(state.liveTurn.userMsg));
      }
      if (!state.liveTurn.row.isConnected) {
        inner.appendChild(state.liveTurn.row);
      }
      if (state.liveTurn.row._research) window.AccurettaResearch?.update(state.liveTurn.row, state.liveTurn.row._research);
      scrollToBottom(true);
    }
    state._versionsExpanded = false;
    loadVersions();
    renderChatList();
    // restore composer draft
    const ta = $("#composer-input");
    ta.value = localStorage.getItem("accuretta:draft:" + id) || "";
    autoResize(ta);
    state.queueEditId = null;
    renderMessageQueue();
    if (!state.streaming) scheduleMessageQueueDrain(id);
    if (isMobile()) {
      state.mobileTab = "chat";
      applyMobileTab();
    } else if (isCompactViewport()) {
      // A notch-born session (bridged takeover) must show its rail: the owner came
      // here to read it, and an auto-collapsed sidebar hides the conversation.
      if (chat.origin === "island") {
        app.classList.remove("sidebar-collapsed");
      } else {
        app.classList.add("sidebar-collapsed");
      }
    }
    // SSE carries live context updates. Fetch once on selection, then use a
    // slow visible/streaming-only fallback in case the event stream reconnects.
    clearInterval(state._ctxPoll);
    refreshContextStats(id);
    state._ctxPoll = setInterval(() => {
      if (!document.hidden && state.streaming) refreshContextStats(state.chatId);
    }, 15000);
    wbOnChatSelected(id);
  }
  return {
    newChat, refreshContextStats, renderTopbarContext, renderProjectChip, pickedPath,
    addProjectFolder, projectScopeMatches, clearConversations, initProjectScope, initProjectPicker,
    openRequestedChat, selectChat, removeProjectGroup,
  };
};
