/* SPDX-License-Identifier: AGPL-3.0-only */
/* command palette (⌘K): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["command-palette"] = ({
  $, AGENT_AVATAR_HTML, THINK_CLOSE_RE, app, applyTheme, autoResize, buildFoldDivider,
  buildModelSwitchDivider, copyText, deleteChat, ensureCompactionRow, esc, exportProjectZip,
  folderLeafName, highlightMentionsInBubble, loadChats, modelSwitchPoints, newChat, nextTheme,
  noteOwnTurnEnded, notifyCompletion, notifyFailure, openSettings, projectScopeMatches, relTime,
  removeProjectGroup, renderMarkdown, renderPlanPanel, renderStoppedTurn, renderTaskHandoff,
  renderTurnChanges, revealSettingsControl, saveSettings, saveSnapshot, sbFillEmptySections,
  sbListSections, sbRenderFilesView, sbSetView, scheduleMessageQueueDrain, screenshotPreview,
  scrollToBottom, selectChat, send, setChatFollowBottom, setStreamingUI, shownImagesHtml,
  splitCascade, splitThinking, state, stoppedTurnMarkup, streamChat, streamPaceFinish,
  stripMentionRefs, toast, wbChatGroup, wbClock, wbCollapsedGroups, wbDecorateFileRefs, wbDuration,
  wbFileIndex, wbLoadFileIndex, wbMcpOpen, wbModelShort, wbNorm, wbPaletteCommands, wbPaletteFiles,
  wbRenderTabs, wbSaveCollapsedGroups, wbShortTime, wbSyncCollapseAll,
}) => {
  function openPalette(mode) {
    // "files" = Ctrl+P quick open; anything else (including the click event
    // the toolbar buttons pass) is the full palette.
    state.palette.mode = mode === "files" ? "files" : "all";
    state.palette.open = true;
    state.palette.idx = 0;
    const scrim = $("#palette-scrim");
    const pal = $("#palette");
    if (scrim) { scrim.classList.remove("hidden"); scrim.classList.add("open"); }
    if (pal) { pal.classList.remove("hidden"); pal.classList.add("open"); pal.dataset.mode = state.palette.mode; }
    const inp = $("#palette-input");
    if (inp) {
      inp.value = "";
      inp.placeholder = state.palette.mode === "files"
        ? "Go to a file in your workspace…"
        : "Search sessions, files and commands — > commands · @ files";
      if (state.palette.mode === "files" || !wbFileIndex.files) wbLoadFileIndex();
      refreshPaletteList("");
      setTimeout(() => inp.focus(), 0);
    }
  }
  function closePalette() {
    state.palette.open = false;
    clearTimeout(paletteSearchTimer);
    paletteSearchController?.abort();
    paletteSearchRevision++;
    const scrim = $("#palette-scrim");
    const pal = $("#palette");
    if (scrim) { scrim.classList.add("hidden"); scrim.classList.remove("open"); }
    if (pal) { pal.classList.add("hidden"); pal.classList.remove("open"); }
  }
  function _fuzzyScore(query, text) {
    if (!query) return 0;
    const q = query.toLowerCase();
    const t = (text || "").toLowerCase();
    if (t.includes(q)) return 100 - t.indexOf(q);
    // cheap subsequence scoring: every char of q must appear in order
    let i = 0, score = 0, last = -1;
    for (let j = 0; j < t.length && i < q.length; j++) {
      if (t[j] === q[i]) { score += 5 - Math.min(4, j - last - 1); last = j; i++; }
    }
    return i === q.length ? score : -1;
  }
  let paletteSearchTimer = null;
  let paletteSearchController = null;
  let paletteSearchRevision = 0;

  async function openConversationMatch(match) {
    closePalette();
    if (state.chats.chats[match.chat_id]) state.chats.chats[match.chat_id]._summary = true;
    await selectChat(match.chat_id);
    if (state.chatId !== match.chat_id) return;
    const index = match.visible_index;
    if (index == null) { toast("This match has no visible conversation message.", "info", 3000); return; }
    state.messageWindowStart = Math.max(0, index - 8);
    renderMessages();
    setChatFollowBottom(false);
    const row = document.querySelector(`[data-message-index="${index}"]`);
    if (!row) return;
    if (match.internal) {
      const details = document.createElement("details");
      details.className = "conversation-search-source";
      details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = match.role === "tool" ? "Matching tool output" : "Matching intermediate response";
      const content = document.createElement("p");
      content.textContent = match.excerpt;
      details.append(summary, content);
      (row.querySelector(".bubble-col") || row).appendChild(details);
    }
    row.classList.add("conversation-search-hit");
    requestAnimationFrame(() => row.scrollIntoView({ block: "center", behavior: "instant" }));
    setTimeout(() => row.classList.remove("conversation-search-hit"), 5000);
  }

  function refreshPaletteList(query, contentMatches = null, searchError = "") {
    // Prefixes narrow the palette like the composer does: ">" commands,
    // "@" files. Sessions are the default content.
    // Ctrl+P opens it already scoped to files.
    let scope = state.palette.mode === "files" ? "files" : "all";
    const fullQuery = query;
    if (query.startsWith(">")) { scope = "commands"; query = query.slice(1).trimStart(); }
    else if (query.startsWith("@")) { scope = "files"; query = query.slice(1).trimStart(); }
    const wantCommands = scope === "all" || scope === "commands";
    const wantSessions = scope === "all" || scope === "sessions";
    const wantFiles = scope === "all" || scope === "files";
    if (contentMatches === null) {
      clearTimeout(paletteSearchTimer);
      paletteSearchController?.abort();
      const revision = ++paletteSearchRevision;
      if (wantSessions && query.trim().length >= 2) {
        paletteSearchTimer = setTimeout(async () => {
          const controller = new AbortController();
          paletteSearchController = controller;
          try {
            const response = await fetch(`/api/chat-search?q=${encodeURIComponent(query)}`, { signal: controller.signal });
            if (!response.ok) throw new Error("Conversation search is unavailable");
            const result = await response.json();
            if (revision === paletteSearchRevision && state.palette.open) refreshPaletteList(fullQuery, result.results || []);
          } catch (error) {
            if (error.name !== "AbortError" && revision === paletteSearchRevision && state.palette.open) refreshPaletteList(fullQuery, [], error.message);
          }
        }, 180);
      }
    }
    const list = $("#palette-list");
    list.innerHTML = "";
    const items = [];
    // built-in commands always appear first
    const commands = [
      { kind: "cmd", icon: "ph-plus", label: "New session", action: () => { closePalette(); newChat(); } },
      { kind: "cmd", icon: "ph-gear-six", label: "Open Settings", action: () => { closePalette(); openSettings(); } },
      { kind: "cmd", icon: "ph-plugs", label: "MCP servers", action: () => { closePalette(); wbMcpOpen(); } },
      { kind: "cmd", icon: "ph-brain", label: "Open Long-term memory", action: () => { closePalette(); openSettings(); setTimeout(() => revealSettingsControl("#btn-mem-refresh"), 80); } },
      { kind: "cmd", icon: "ph-arrow-counter-clockwise", label: "Regenerate last reply", action: () => { closePalette(); regenerateLast(); } },
      { kind: "cmd", icon: "ph-moon", label: "Cycle theme (dark / dim / retro / opal / showcase / neumorphic / amaranth / aperture / aperture-dark / atelier / soft / pastel / cartograph / folio / light)", action: async () => { closePalette(); const next = nextTheme(state.settings.theme || "light"); await saveSettings({ theme: next }); applyTheme(next); } },
      { kind: "cmd", icon: "ph-browser", label: "Toggle preview pane", action: () => { closePalette(); app.classList.toggle("preview-collapsed"); } },
      { kind: "cmd", icon: "ph-camera", label: "Screenshot preview", action: () => { closePalette(); screenshotPreview(); } },
      { kind: "cmd", icon: "ph-package", label: "Export project", action: () => { closePalette(); exportProjectZip(); } },
      { kind: "cmd", icon: "ph-floppy-disk", label: "Save snapshot", action: () => { closePalette(); saveSnapshot(); } },
      ...wbPaletteCommands(),
    ];
    if (wantCommands) for (const c of commands) {
      const s = query ? _fuzzyScore(query, c.label) : 0;
      if (query && s < 0) continue;
      items.push({ ...c, score: s + 50 });
    }
    // then sessions
    if (wantSessions) for (const id of state.chats.order) {
      const c = state.chats.chats[id];
      if (!c) continue;
      const label = c.title || "(untitled)";
      const s = query ? _fuzzyScore(query, label) : 0;
      if (query && s < 0) continue;
      items.push({
        kind: "session",
        icon: "ph-chat-circle",
        label,
        sub: id === state.chatId ? "current" : relTime(c.updated || c.created),
        action: () => { closePalette(); selectChat(id); },
        score: s,
      });
    }
    // workspace files: everything in files mode, a few strong hits otherwise
    if (wantFiles && (scope === "files" || query.trim())) {
      items.push(...wbPaletteFiles(query, scope === "files" ? 60 : 6, scope === "files" ? 0 : 30));
    }
    for (const match of contentMatches || []) {
      items.push({ kind: match.role === "tool" ? "tool output" : "message", icon: "ph-magnifying-glass",
        label: match.title, sub: match.excerpt, score: 20,
        action: () => openConversationMatch(match) });
    }
    items.sort((a, b) => b.score - a.score);
    state.palette.items = items;
    state.palette.idx = 0;
    items.forEach((it, i) => {
      const el = document.createElement("div");
      el.className = "palette-item" + (i === 0 ? " sel" : "");
      el.innerHTML = `
        <i class="ph ${it.icon}${it.iconClass ? " " + it.iconClass : ""}"></i>
        <div class="pi-main">
          <div class="pi-label">${esc(it.label)}</div>
          ${it.sub ? `<div class="pi-sub">${esc(it.sub)}</div>` : ""}
        </div>
        ${it.keys ? `<span class="pi-kind pi-keys"><kbd>${esc(it.keys)}</kbd></span>` : `<span class="pi-kind">${esc(it.kind)}</span>`}`;
      el.addEventListener("click", it.action);
      list.appendChild(el);
    });
    if (!items.length) {
      const indexing = wantFiles && scope === "files" && !wbFileIndex.files;
      const noFolders = wantFiles && scope === "files" && wbFileIndex.files && !state.workspace?.folders?.length;
      list.innerHTML = `<div class="palette-empty">${indexing ? "Indexing workspace files…"
        : noFolders ? "Add a folder under Explorer to search its files."
        : contentMatches === null && wantSessions && query.trim().length >= 2 ? "Searching conversations…"
        : searchError ? esc(searchError) : "No matches."}</div>`;
    }
  }
  function paletteMove(delta) {
    const items = state.palette.items;
    if (!items.length) return;
    state.palette.idx = (state.palette.idx + delta + items.length) % items.length;
    const rows = document.querySelectorAll("#palette-list .palette-item");
    rows.forEach((r, i) => r.classList.toggle("sel", i === state.palette.idx));
    const r = rows[state.palette.idx];
    if (r) r.scrollIntoView({ block: "nearest" });
  }
  function paletteCommit() {
    const it = state.palette.items[state.palette.idx];
    if (it) it.action();
  }

  function chatDateBucket(t) {
    const now = new Date();
    const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const day = 86400000;
    const ts = (t || 0) * 1000;
    if (ts >= startToday) return "Today";
    if (ts >= startToday - day) return "Yesterday";
    if (ts >= startToday - 7 * day) return "Previous 7 days";
    if (ts >= startToday - 30 * day) return "Previous 30 days";
    return "Older";
  }

  // Engagement metadata from the mission record decides the sidebar category,
  // never the auto-derived "Authorized ..." title. Unknown engagement slugs
  // still get their own category.
  const MISSION_CATEGORIES = {
    pentest: { label: "Red Team", icon: "ph-shield-chevron" },
    access_control: { label: "Access Control", icon: "ph-user-switch" },
    bug_bounty: { label: "Bug Bounty", icon: "ph-bug" },
    frontend_secrets: { label: "Frontend Secrets", icon: "ph-file-js" },
    recon: { label: "Target Recon", icon: "ph-crosshair" },
    passive_osint: { label: "Passive OSINT", icon: "ph-binoculars" },
  };

  function missionCategory(chat) {
    const mission = chat?.mission;
    if (!mission || typeof mission !== "object") return null;
    const key = String(mission.engagement || "").trim().toLowerCase();
    if (!key) return null;
    const known = MISSION_CATEGORIES[key];
    if (known) return { key, label: known.label, icon: known.icon };
    const label = key.replace(/[_-]+/g, " ").replace(/\b\w/g, ch => ch.toUpperCase());
    return { key, label, icon: "ph-shield-star" };
  }

  function renderChatList() {
    const wrap = $("#chatlist");
    wrap.innerHTML = "";
    const seen = new Set();
    let rendered = 0;
    const ordered = [...(state.chats?.order || [])].sort((a, b) =>
      ((state.chats.chats?.[b]?.updated || 0) - (state.chats.chats?.[a]?.updated || 0)))
      .filter(id => {
        if (seen.has(id)) return false;
        seen.add(id);
        const c = state.chats.chats[id];
        return !!c && projectScopeMatches(c);
      });
    // "All projects": sessions sit in collapsible project groups (missions
    // and plain conversations get their own). A scoped project keeps the
    // flat, date-bucketed list.
    let sections = null;
    if (!state.projectScope && (ordered.length || state.workspace?.folders?.length)) {
      const groups = new Map();
      for (const id of ordered) {
        const c = state.chats.chats[id];
        const g = wbChatGroup(c);
        if (!groups.has(g.key)) groups.set(g.key, { ...g, ids: [] });
        groups.get(g.key).ids.push(id);
      }
      // Every project folder shows, sessions or not, and carries its path
      // for the jump to the Files view.
      for (const folder of state.workspace?.folders || []) {
        const key = `proj:${wbNorm(folder)}`;
        if (!groups.has(key)) groups.set(key, { key, label: folderLeafName(folder), icon: "ph-folder-simple", ids: [] });
        if (!groups.get(key).path) groups.get(key).path = folder;
      }
      const collapsed = wbCollapsedGroups();
      // Projects (and missions) by recency; sessions without a project last.
      const ordered_groups = [...groups.values()].sort((x, y) => (x.key === "chats") - (y.key === "chats"));
      // The new sidebar folds the list into two sections: Projects (folders,
      // repos, missions) and Chats (sessions without a project, no group head).
      if ($("#sidebar")?.classList.contains("sb-v2")) sections = sbListSections(wrap);
      for (const g of ordered_groups) {
        if (sections && g.key === "chats") {
          for (const id of g.ids) { sections.chats.appendChild(wbChatRow(id, state.chats.chats[id], true)); rendered++; }
          continue;
        }
        const isCollapsed = collapsed.has(g.key);
        const box = document.createElement("div");
        box.className = "wb-group" + (isCollapsed ? " collapsed" : "");
        box.dataset.group = g.key;
        const head = document.createElement("button");
        head.type = "button";
        head.className = "wb-group-head";
        head.title = g.key === "chats" ? "Sessions without a project folder" : g.label;
        head.setAttribute("aria-expanded", String(!isCollapsed));
        const folderIcons = g.path ? `<i class="ph ph-folder-simple wb-group-icon is-closed" aria-hidden="true"></i><i class="ph ph-folder-open wb-group-icon is-open" aria-hidden="true"></i>`
          : `<i class="ph ${g.icon || "ph-chats-circle"} wb-group-icon" aria-hidden="true"></i>`;
        head.innerHTML = `<i class="ph ph-caret-${isCollapsed ? "right" : "down"} wb-group-caret" aria-hidden="true"></i>${folderIcons}`
          + `<span class="wb-group-name">${esc(g.label)}</span>`
          + `<span class="wb-group-count">${g.ids.length}</span>`;
        head.addEventListener("click", () => {
          const set = wbCollapsedGroups();
          if (box.classList.toggle("collapsed")) set.add(g.key); else set.delete(g.key);
          wbSaveCollapsedGroups(set);
          const open = !box.classList.contains("collapsed");
          head.setAttribute("aria-expanded", String(open));
          head.querySelector(".wb-group-caret").className = `ph ph-caret-${open ? "down" : "right"} wb-group-caret`;
          wbSyncCollapseAll();
        });
        const body = document.createElement("div");
        body.className = "wb-group-body";
        for (const id of g.ids) { body.appendChild(wbChatRow(id, state.chats.chats[id], true)); rendered++; }
        if (!g.ids.length) body.innerHTML = `<div class="sb-group-empty">No sessions yet</div>`;
        box.append(head, body);
        // Files live in the sidebar's Files view, never among the sessions;
        // this jumps there with the project's folder open.
        if (sections && g.path) {
          const files = document.createElement("button");
          files.type = "button";
          files.className = "iconbtn sb-group-files";
          files.title = "Browse this project's files";
          files.setAttribute("aria-label", `Browse files in ${g.label}`);
          files.innerHTML = `<i class="ph ph-files" aria-hidden="true"></i>`;
          files.addEventListener("click", () => sbSetView("files", g.path));
          box.appendChild(files);
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "iconbtn sb-group-remove";
          remove.title = "Remove this project and its sessions (files stay on disk)";
          remove.setAttribute("aria-label", `Remove ${g.label}`);
          remove.innerHTML = `<i class="ph ph-trash" aria-hidden="true"></i>`;
          remove.addEventListener("click", () => removeProjectGroup(g));
          box.appendChild(remove);
        }
        (sections ? sections.projects : wrap).appendChild(box);
      }
      if (sections) sbFillEmptySections(sections);
    } else {
      let lastHeadKey = "";
      for (const id of ordered) {
        const c = state.chats.chats[id];
        const missionCat = missionCategory(c);
        const headKey = missionCat ? `mission:${missionCat.key}` : `date:${chatDateBucket(c.updated)}`;
        if (headKey !== lastHeadKey) {
          lastHeadKey = headKey;
          const head = document.createElement("div");
          if (missionCat) {
            head.className = "chatlist-mission";
            head.innerHTML = `<i class="ph ${missionCat.icon}" aria-hidden="true"></i><span>${esc(missionCat.label)}</span>`;
          } else {
            head.className = "chatlist-date";
            head.textContent = headKey.slice(5);
          }
          wrap.appendChild(head);
        }
        wrap.appendChild(wbChatRow(id, c, false));
        rendered++;
      }
    }
    if (!rendered && !sections) {
      const empty = document.createElement("div");
      empty.className = "chatlist-empty";
      empty.textContent = state.projectScope
        ? "No conversations in this project yet."
        : "No conversations yet.";
      wrap.appendChild(empty);
    }
    wbRenderTabs();
    wbSyncCollapseAll();
    sbRenderFilesView();
  }

  // One line per session: the title, and a single status slot on the right
  // (live state, plan progress, or the age when nothing is going on).
  function wbChatRow(id, c, grouped) {
    const row = document.createElement("div");
    const isActive = id === state.chatId;
    row.className = "chatrow wb-row" + (isActive ? " active" : "");
    const needsYou = [...state.approvals.values()].some(a => a.chat_id === id && a.status === "pending");
    const sessionState = needsYou ? "needs-you" : state.liveTurn?.chatId === id && state.streaming ? "working" : (c.task_state || "ready");
    const sessionLabels = { "needs-you": "needs you", working: "working", finished: "", interrupted: "interrupted", stopped: "stopped", failed: "failed", ready: "" };
    row.dataset.taskState = sessionState;
    const isGitHubChat = c.origin === "github" || !!c.github_worktree;
    const isProjectChat = c.origin === "project" || !!c.project_workspace;
    if (isGitHubChat) row.classList.add("gh-branch");
    if (isProjectChat) row.classList.add("project-session");
    const projectName = isGitHubChat
      ? String(c.github_worktree?.repo || c.github_worktree?.name || "").trim()
      : isProjectChat ? String(c.project_workspace?.name || c.project_workspace?.repo || "").trim() : "";
    const label = sessionLabels[sessionState];
    let meta;
    if (sessionState === "working") {
      meta = `<span class="wb-row-meta" data-state="working" aria-label="working"><i class="ph ph-circle-notch wb-spin" aria-hidden="true"></i></span>`;
    } else if (label) {
      meta = `<span class="wb-row-meta" data-state="${esc(sessionState)}">${esc(label)}</span>`;
    } else if (Array.isArray(c.plan) && c.plan.length) {
      const done = c.plan.filter(step => step && step.status === "done").length;
      meta = `<span class="wb-row-meta" data-state="${done === c.plan.length ? "plan-done" : "plan"}" title="${done} of ${c.plan.length} plan steps done">${done === c.plan.length ? '<i class="ph ph-check" aria-hidden="true"></i>' : ""}${done}/${c.plan.length}</span>`;
    } else {
      meta = `<span class="wb-row-meta">${esc(wbShortTime(c.updated))}</span>`;
    }
    row.title = [c.title || "new session", !grouped && projectName ? projectName : "",
      c.last_model ? wbModelShort(c.last_model) : "", relTime(c.updated)].filter(Boolean).join(" · ");
    row.innerHTML = `
      <span class="t">${esc(c.title || "new session")}</span>
      ${!grouped && projectName ? `<span class="wb-row-project">${esc(projectName)}</span>` : ""}
      ${meta}
      <button class="del" title="Delete"><i class="ph ph-trash"></i></button>`;
    row.addEventListener("click", (e) => {
      if (e.target.closest(".del")) return;
      selectChat(id);
    });
    row.querySelector(".del").addEventListener("click", (e) => {
      e.stopPropagation();
      deleteChat(id);
    });
    return row;
  }

  function renderMessages() {
    const inner = $("#chat-inner");
    inner.innerHTML = "";
    if (!state.messages.length) {
      // Name the project when the session has one; the dot-matrix A and the
      // dot field stay exactly as they are.
      const welcomeChat = state.chats?.chats?.[state.chatId];
      const welcomeProject = String(welcomeChat?.github_worktree?.repo || welcomeChat?.project_workspace?.name || "").trim();
      inner.innerHTML = `
        <div class="welcome-screen welcome-v2">
          <div class="wv-head">
            <svg class="wv-a" aria-hidden="true"></svg>
            <h1 class="wv-title">${welcomeProject ? `What should we work on in <span class="wv-project">${esc(welcomeProject)}</span>?` : "What are we building?"}</h1>
          </div>
        </div>`;
      const a = inner.querySelector(".wv-a");
      if (a && window.AccDots) window.AccDots.buildA(a, 16, { lo: [.6, .85] });
      enterWelcome();
      initWelcomeScreen();
      window.AccDots?.field.show();
      const chatScroll = $("#chat-scroll");
      if (chatScroll) chatScroll.scrollTop = 0;
      return;
    }
    leaveWelcome();
    const start = Math.max(0, Math.min(
      state.messageWindowStart || 0, state.messages.length));
    if (start > 0) {
      const history = document.createElement("div");
      history.className = "history-window";
      history.innerHTML = `<button type="button" class="btn sm history-window-more"><i class="ph ph-clock-counter-clockwise"></i>Load ${Math.min(state.messageWindowSize, start)} earlier messages</button><span>${start} older messages are sleeping, not deleted</span>`;
      history.querySelector("button").addEventListener("click", () => {
        const scroller = $(".chat-scroll");
        const beforeHeight = scroller ? scroller.scrollHeight : 0;
        const beforeTop = scroller ? scroller.scrollTop : 0;
        state.messageWindowStart = Math.max(
          0, state.messageWindowStart - state.messageWindowSize);
        renderMessages();
        if (scroller) scroller.scrollTop = beforeTop + (scroller.scrollHeight - beforeHeight);
      });
      inner.appendChild(history);
    }
    const modelSwitches = modelSwitchPoints(state.messages);
    for (const [offset, m] of state.messages.slice(start).entries()) {
      if (modelSwitches.has(start + offset)) {
        inner.appendChild(buildModelSwitchDivider(modelSwitches.get(start + offset)));
      }
      if (m.invisible) continue;
      const row = renderBubble(m);
      row.dataset.messageIndex = String(start + offset);
      inner.appendChild(row);
    }
    // Persistent fold divider at the point where the rolling summary absorbed the
    // older turns.
    if (state.foldBoundaryIdx > start && state.foldBoundaryIdx <= state.messages.length) {
      const bubbles = Array.from(
        inner.querySelectorAll("#chat-inner > .bubble-row"));
      const at = Math.max(0, state.foldBoundaryIdx - start);
      const anchor = bubbles[at];
      if (anchor) {
        inner.insertBefore(buildFoldDivider(state.foldMark), anchor);
      } else {
        inner.appendChild(buildFoldDivider(state.foldMark));
      }
    }
    const recoveries = state.chats?.chats?.[state.chatId]?.undo_turns || [];
    if (recoveries.length) {
      const recovery = document.createElement("details");
      recovery.className = "saved-recovery";
      const summary = document.createElement("summary");
      summary.textContent = `Task results (${recoveries.length} recent tasks)`;
      recovery.appendChild(summary);
      for (const changes of recoveries) {
        const row = document.createElement("div");
        row.className = "bubble-row";
        row.innerHTML = '<div class="bubble-col"></div>';
        recovery.appendChild(row);
        renderTurnChanges(row, changes);
      }
      inner.appendChild(recovery);
    }
    if (state.compactingChats.has(state.chatId)) ensureCompactionRow(state.chatId);
    renderRegenerateChip();
    scrollToBottom(true);
  }

  // Empty-session layout: the composer lifts to the middle of the pane
  // (.chat.is-welcome) with the heading above it and recent sessions plus
  // quick starts below it, in their own row under the composer.
  const WELCOME_STARTS = [
    { label: "Build a page", icon: "ph-layout", prompt: "Design a landing page for my product using HTML, CSS and JS." },
    { label: "Fix a bug", icon: "ph-bug", prompt: "Help me find and fix a bug in my project." },
    { label: "Explain this repo", icon: "ph-tree-structure", prompt: "Map this project and explain how it is structured." },
  ];
  function welcomeRecents(limit = 3) {
    const chats = state.chats?.chats || {};
    return [...(state.chats?.order || [])]
      .filter(id => id !== state.chatId && chats[id] && (chats[id].title || "new session") !== "new session")
      .sort((a, b) => (chats[b].updated || 0) - (chats[a].updated || 0))
      .slice(0, limit)
      .map(id => {
        const c = chats[id];
        const project = String(c.github_worktree?.repo || c.project_workspace?.name || "").trim();
        const icon = c.github_worktree ? "ph-git-branch" : (c.project_workspace ? "ph-folder-simple" : "ph-chat-circle");
        return { id, title: c.title, icon, project, when: relTime(c.updated) };
      });
  }
  function enterWelcome() {
    const chat = document.querySelector(".center .chat");
    if (!chat) return;
    chat.classList.add("is-welcome");
    let below = document.getElementById("welcome-below");
    if (!below) {
      below = document.createElement("div");
      below.id = "welcome-below";
      below.className = "wv-below";
      chat.appendChild(below);
    }
    syncWelcomeBelowWidth();
    const recents = welcomeRecents();
    const row = (attrs, icon, title, meta) =>
      `<button type="button" class="wv-row" ${attrs}><i class="ph ${icon}" aria-hidden="true"></i>` +
      `<span class="wv-row-t">${esc(title)}</span>${meta ? `<span class="wv-row-m">${esc(meta)}</span>` : ""}</button>`;
    below.innerHTML =
      (recents.length
        ? `<section class="wv-col"><div class="wv-label">Recent</div>` +
          recents.map(r => row(`data-chat="${esc(r.id)}"`, r.icon, r.title, r.project || r.when)).join("") +
          `</section>`
        : "") +
      `<section class="wv-col"><div class="wv-label">Start something</div>` +
      WELCOME_STARTS.map((w, i) => row(`data-start="${i}"`, w.icon, w.label, "")).join("") +
      `</section>`;
  }
  // The composer's max width moves with the sidebar/preview state; keep the
  // recents + quick starts flush with its edges.
  let _welcomeRO = null;
  function syncWelcomeBelowWidth() {
    const comp = document.querySelector(".composer-wrap > .composer");
    const below = document.getElementById("welcome-below");
    if (!comp || !below) return;
    below.style.maxWidth = comp.offsetWidth + "px";
    if (!_welcomeRO && "ResizeObserver" in window) {
      _welcomeRO = new ResizeObserver(() => {
        const b = document.getElementById("welcome-below");
        if (b) b.style.maxWidth = comp.offsetWidth + "px";
      });
      _welcomeRO.observe(comp);
    }
  }
  // Drops the welcome layout; the composer settles back to the bottom.
  function leaveWelcome() {
    window.AccDots?.field.hide();
    document.querySelector(".center .chat")?.classList.remove("is-welcome");
    document.getElementById("welcome-below")?.remove();
  }

  function initWelcomeScreen() {
    const below = document.getElementById("welcome-below");
    if (!below) return;
    below.addEventListener("click", (ev) => {
      const item = ev.target.closest(".wv-row");
      if (!item) return;
      if (item.dataset.chat) { selectChat(item.dataset.chat); return; }
      const start = WELCOME_STARTS[Number(item.dataset.start)];
      if (!start) return;
      // Ripple from the row, then send once the ring is under way.
      window.AccDots?.field.pulse(item);
      item.classList.add("is-picked");
      setTimeout(() => send({ prompt: start.prompt, invisible: true }), 280);
    });
  }

  function renderBubble(m) {
    if (m.role === "user" && m._steering_id) {
      const row = document.createElement("div");
      row.className = "bubble-row task-update-message";
      row.dataset.updateId = m._steering_id;
      row.innerHTML = `
        <article class="task-update-card" aria-label="Task update received by agent">
          <div class="task-update-heading">
            <svg class="task-update-icon" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 3v6a4 4 0 0 0 4 4h8m-4-4 4 4-4 4"/></svg>
            <span>Your update</span>
            <span class="task-update-status"><i class="ph ph-check" aria-hidden="true"></i>Received by agent</span>
          </div>
          <div class="task-update-text"></div>
        </article>`;
      row.querySelector(".task-update-text").textContent = stripMentionRefs(m.content || "");
      if (m.images?.length) {
        const attachments = document.createElement("div");
        attachments.className = "task-update-attachments";
        attachments.textContent = `${m.images.length} attached ${m.images.length === 1 ? "image" : "images"}`;
        row.querySelector(".task-update-card").appendChild(attachments);
      }
      return row;
    }
    if (m.role === "system" && m._note) {
      const row = document.createElement("div");
      row.className = "bubble-row github-event-row";
      row.innerHTML = `
        <div class="github-event-bubble">
          <svg class="github-event-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/></svg>
          <div class="github-event-text">${esc(m.content || "")}</div>
        </div>`;
      return row;
    }
    const row = document.createElement("div");
    row.className = "bubble-row " + (m.role === "user" ? "user" : "");
    const avatar = m.role === "user"
      ? `<div class="avatar user">me</div>`
      : AGENT_AVATAR_HTML;

    let visible = stripMentionRefs(m.content || "");
    let thoughtChip = "";
    let thinkingText = "";
    let cascadeChips = "";
    let stoppedMarker = "";
    if (m.role === "assistant") {
      const { thinking, content } = splitThinking(visible);
      thinkingText = thinking;
      // Promote a reply whose entire text sits inside the thinking tags (the
      // closing tag is the last content): splitThinking would demote everything and
      // the bubble would render empty. Render the think text as the visible answer;
      // the chip above stays as the collapsible copy.
      if (m._stopped) {
        visible = content;
        stoppedMarker = stoppedTurnMarkup();
      } else if (!content.trim() && thinking &&
          THINK_CLOSE_RE.test((m.content || "").replace(/\s+$/, "").slice(-200))) {
        visible = "> The model closed its reply inside its **thinking block**. Full text follows.\n\n" + thinking;
      } else if (!content.trim() && thinking) {
        visible = "> The model stopped during reasoning before producing a final answer. Nothing was written. Use Retry to run the turn again.";
      } else {
        visible = content;
      }
      if (thinking) {
        thoughtChip = `
          <div class="think-container done">
            <div class="think-header" style="cursor: pointer;">
              <i class="ph ph-caret-right think-caret"></i>
              <i class="ph ${m._stopped ? "ph-stop-circle" : "ph-check-circle"} think-check-icon done"></i>
              <span class="think-title">${m._stopped ? "Stopped" : "Thought for a moment"}</span>
            </div>
            <div class="think-content hidden"></div>
          </div>`;
      }
      const cascadeRes = splitCascade(visible);
      visible = cascadeRes.content;
      if (cascadeRes.cascade && cascadeRes.cascade.length > 0) {
        let btns = cascadeRes.cascade.map(text => 
          `<button class="cascade-chip" data-prompt="${esc(text)}"><i class="ph ph-sparkle"></i>${esc(text)}</button>`
        ).join("");
        cascadeChips = `<div class="cascade-container">${btns}</div>`;
      }
    }

    // Per-message meta: time plus the measured generation size when the turn
    // recorded one.
    const tokTipParts = [];
    if (m.tokens) tokTipParts.push(`${m.tokens.toLocaleString()} reply tokens`);
    if (m.prompt_tokens) tokTipParts.push(`${m.prompt_tokens.toLocaleString()} prompt tokens`);
    const tokTip = tokTipParts.length ? ` title="${tokTipParts.join(" · ")}"` : "";
    const tokLabel = m.tokens
      ? ` · ${m.tokens >= 1000 ? `${(m.tokens / 1000).toFixed(1)}k` : m.tokens} tok`
      : "";
    const isAgent = m.role === "assistant";
    const metaText = m.role === "user"
      ? `you · ${relTime(m.t)}`
      : `${m._worked_s ? `Finished ${wbDuration(m._worked_s)} · ` : ""}${wbClock(m.t)}`;
    row.innerHTML = `
      ${avatar}
      <div class="bubble-col">
        ${thoughtChip}
        <div class="bubble ${m.role === "user" ? "user" : "agent"}${m._stopped && !visible.trim() ? " hidden" : ""}">${renderMarkdown(visible)}</div>
        ${m.role === "assistant" ? shownImagesHtml(m._images) : ""}
        ${cascadeChips}
        ${stoppedMarker}
        <div class="bubble-meta"${tokTip}>${metaText}${tokLabel}</div>
      </div>`;
    const savedThinkContent = row.querySelector(".think-content");
    if (savedThinkContent) savedThinkContent._fullText = thinkingText;
    
    // Single copy action under every bubble, in the same .bubble-actions row as
    // the assistant's regenerate strip. renderRegenerateChip rebuilds the last
    // assistant bubble's row with regen + copy, so it is skipped here; other
    // assistant and all user bubbles keep this row. (Cascade clicks use event
    // delegation on #chat-inner.)
    const isLastAssistant = m.role === "assistant" &&
      state.messages.length > 0 &&
      state.messages[state.messages.length - 1] === m;
    if (!isLastAssistant) {
      const actions = document.createElement("div");
      actions.className = "bubble-actions";
      actions.innerHTML = `<button type="button" class="bubble-action" data-act="copy" title="Copy"><i class="ph ph-copy"></i></button>`;
      const copyBtn = actions.querySelector('[data-act="copy"]');
      copyBtn.addEventListener("click", async () => {
        try {
          await copyText(m.content || "");
          const icon = copyBtn.querySelector("i");
          copyBtn.classList.add("copied");
          icon.classList.remove("ph-copy");
          icon.classList.add("ph-check");
          setTimeout(() => {
            copyBtn.classList.remove("copied");
            icon.classList.remove("ph-check");
            icon.classList.add("ph-copy");
          }, 1200);
        } catch {
          toast("Clipboard blocked", "warn", 2000);
        }
      });
      // Edit & resend on user turns. Only the last user turn is editable: editing an
      // older one would silently discard everything the agent did after it.
      if (m.role === "user") {
        const lastUserIdx = state.messages.reduce((acc, mm, i) => mm.role === "user" ? i : acc, -1);
        const editable = state.messages[lastUserIdx] === m && !state.streaming;
        const editBtn = document.createElement("button");
        editBtn.type = "button";
        editBtn.className = "bubble-action";
        editBtn.dataset.act = "edit";
        editBtn.title = editable ? "Edit this message and resend" : "Only your latest message can be edited";
        editBtn.innerHTML = '<i class="ph ph-pencil-simple-line"></i>';
        editBtn.disabled = !editable;
        editBtn.addEventListener("click", () => {
          if (!editable) return;
          startEditTurn(lastUserIdx);
        });
        actions.appendChild(editBtn);
      }
      row.querySelector(".bubble-col").appendChild(actions);
    }
    highlightMentionsInBubble(row.querySelector(".bubble"));
    enhanceCodeBlocks(row);
    if (isAgent) wbDecorateFileRefs(row.querySelector(".bubble"));
    if (m._research) window.AccurettaResearch?.mount(row, m._research);
    return row;
  }

  // Text of a code card for the delegated click handlers. Line-span bodies are
  // re-joined with \n (the spans are display:block, so textContent has no
  // separators).
  function getCodeText(pre) {
    const codeEl = pre.querySelector("code");
    if (codeEl) {
      const lines = codeEl.querySelectorAll(".code-line");
      if (lines.length) {
        return Array.from(lines).map(l => l.textContent).join("\n");
      }
      return codeEl.textContent || "";
    }
    return pre.textContent || "";
  }

  function enhanceCodeBlocks(root) {
    const pres = root.querySelectorAll("pre");
    pres.forEach(pre => {
      if (pre.dataset.enhanced === "1") return;
      pre.dataset.enhanced = "1";
      pre.classList.add("code-block");
      // Clicks (copy / preview / open-file / legacy copy) go through one delegated
      // listener (installCodeCardActions); streaming paints rebuild these nodes, so
      // per-node listeners would be re-wired on every paint.
      if (!pre.querySelector(".cc-copy") && !pre.closest(".code-card-tabs-container")) {
        const btn = document.createElement("button");
        btn.className = "copy-code";
        btn.type = "button";
        btn.innerHTML = '<i class="ph ph-copy"></i>';
        btn.title = "Copy";
        pre.appendChild(btn);
      }
    });

    // Diff cards aren't <pre>, so the loop above skips them. Their copy button
    // copies the raw diff stashed in .diff-raw (delegated, see
    // installCodeCardActions).
    root.querySelectorAll(".diff-card").forEach(card => {
      if (card.dataset.enhanced === "1") return;
      card.dataset.enhanced = "1";
    });

    // Code-only bubbles: when a reply is essentially one code card, drop the
    // bubble's padding and background so the card is the surface. `root` itself
    // is included because the live/final render passes the bubble element
    // (querySelectorAll only sees descendants), while reload passes the row.
    const _agentBubbles = Array.from(root.querySelectorAll(".bubble.agent"));
    if (root.matches && root.matches(".bubble.agent")) _agentBubbles.push(root);
    _agentBubbles.forEach(bubble => {
      const meaningful = Array.from(bubble.children).filter(el => {
        if (el.nodeType !== 1) return false;
        if (el.tagName === "BR") return false;
        if (!(el.textContent || "").trim() && !el.matches?.("pre")) return false;
        return true;
      });
      if (meaningful.length === 1 && meaningful[0].matches?.("pre.code-card")) {
        bubble.classList.add("bubble-code-only");
      } else {
        bubble.classList.remove("bubble-code-only");
      }
    });
  }

  // Regenerate the latest assistant reply: re-send with regenerate:true; the
  // backend pops trailing assistant messages and replays the last user message.
  async function regenerateLast() {
    if (state.streaming) return;
    if (!state.messages.some(m => m.role === "assistant")) {
      toast("Nothing to regenerate yet.", "warn", 2200);
      return;
    }
    // Keep the previous reply for the Compare affordance before dropping it.
    const prevAssistant = [...state.messages].reverse().find(m => m.role === "assistant");
    state._comparePair = prevAssistant && (prevAssistant.content || "").trim()
      ? { before: prevAssistant.content, beforeLabel: "Previous reply" } : null;
    // drop the last assistant bubble visually before re-streaming
    while (state.messages.length && state.messages[state.messages.length - 1].role === "assistant") {
      state.messages.pop();
    }
    renderMessages();

    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    agentRow._attackRailEnabled = false;
    agentRow._rtEngagement = "";
    agentRow.innerHTML = `
      ${AGENT_AVATAR_HTML}
      <div class="bubble-col">
        <div class="think-container think-line">
          <div class="think-header" style="cursor: pointer;">
            <i class="ph ph-caret-right think-caret"></i>
            <i class="ph ph-brain think-check-icon"></i>
            <span class="think-title shimmer">Regenerating…</span>
          </div>
          <div class="think-content hidden"></div>
        </div>
        <div class="tool-stack" id="tool-stack"></div>
        <div class="bubble agent hidden" id="stream-bubble"></div>
        <div class="bubble-meta streaming">streaming<span class="typing"><span></span><span></span><span></span></span></div>
      </div>`;
    $("#chat-inner").appendChild(agentRow);
    window.AccurettaOrb?.setState(agentRow, "thinking");
    scrollToBottom(true);

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);
    // Same switch-away protection as send(): keep the live row reachable so
    // selecting this chat mid-regenerate restores the partial.
    state.liveTurn = { chatId: state.chatId, row: agentRow, userMsg: null };
    try {
      await streamChat("", agentRow, state.abortCtl.signal, [], { regenerate: true });
    } catch (e) {
      if (e.name === "AbortError") {
        renderStoppedTurn(agentRow);
      } else {
        agentRow._notificationFailed = true;
        agentRow._notificationError = e.message;
        toast("regenerate failed: " + e.message, "err");
      }
    } finally {
      streamPaceFinish(agentRow);
      noteOwnTurnEnded(state.liveTurn?.chatId || state.chatId);
      state.streaming = false;
      state.abortCtl = null;
      renderTaskHandoff(agentRow, state.liveTurn?.chatId || state.chatId);
      state.liveTurn = null;
      setStreamingUI(false, agentRow._notificationCancelled ? "stopped" : agentRow._notificationFailed ? "failed" : "completed");
      renderRegenerateChip();
      attachCompareChip(agentRow);
      if (agentRow._notificationFailed) notifyFailure(agentRow._notificationError, agentRow._workStart ? Date.now() - agentRow._workStart : 0);
      scheduleMessageQueueDrain(state.chatId);
    }
  }

  // ===== Edit & resend + Compare (client-side turn surgery) =====
  // Editing only targets the last user turn; the bridge's regenerate path
  // swaps the stored message content, so no new turn is appended and no
  // history beyond the edit survives.
  function startEditTurn(index) {
    if (state.streaming) return;
    const m = state.messages[index];
    if (!m || m.role !== "user") return;
    state.editingTurn = { index };
    const ta = $("#composer-input");
    if (ta) {
      ta.value = m.content || "";
      ta.classList.add("is-editing");
      autoResize(ta);
      ta.focus();
    }
    renderEditBanner();
  }

  function cancelEditTurn() {
    state.editingTurn = null;
    const ta = $("#composer-input");
    if (ta) ta.classList.remove("is-editing");
    renderEditBanner();
  }

  function renderEditBanner() {
    const wrap = document.querySelector(".composer-wrap");
    if (!wrap) return;
    let banner = wrap.querySelector(".bubble-editing-banner");
    if (!state.editingTurn) {
      banner?.remove();
      return;
    }
    if (!banner) {
      banner = document.createElement("div");
      banner.className = "bubble-editing-banner";
      banner.setAttribute("role", "status");
      // Inside the box with the notices, right after them.
      const deck = document.getElementById("revealer-deck");
      if (deck) deck.after(banner);
      else wrap.prepend(banner);
    }
    banner.innerHTML = `<strong>Editing your last message</strong><span>— sending replaces the turn and everything after it.</span><button type="button" class="bubble-editing-cancel" title="Cancel edit"><i class="ph ph-x"></i></button>`;
    banner.querySelector(".bubble-editing-cancel").addEventListener("click", cancelEditTurn);
  }

  async function sendEditTurn(opts = {}) {
    if (state.streaming) return false;
    const edit = state.editingTurn;
    if (!edit) return false;
    const ta = $("#composer-input");
    const text = (ta?.value || "").trim();
    if (!text) return false;
    if (!state.settings.model) {
      toast("Pick a model in Settings first.", "warn", 3200, "no-model");
      openSettings();
      return false;
    }
    const m = state.messages[edit.index];
    if (!m || m.role !== "user") { cancelEditTurn(); return false; }
    // Files attached while editing are ignored — the replaced turn keeps its
    // original attachments, and a mid-edit upload would silently vanish.
    if (state.pendingFiles?.length) {
      toast("Files can't be attached while editing — send the edit first, then attach files.", "warn", 4200);
    }
    if ((opts.mode || state.mode) === "research") {
      toast("Editing is disabled in Deep Research mode.", "warn", 3600);
      return false;
    }
    // Capture the reply this edit replaces so Compare can show both sides.
    const prevAssistant = state.messages.slice(edit.index + 1).findLast?.(mm => mm.role === "assistant")
      ?? [...state.messages.slice(edit.index + 1)].reverse().find(mm => mm.role === "assistant");
    state._comparePair = prevAssistant && (prevAssistant.content || "").trim()
      ? { before: prevAssistant.content, beforeLabel: "Reply to the original message" } : null;
    state.editingTurn = null;
    if (ta) { ta.value = ""; ta.classList.remove("is-editing"); autoResize(ta); }
    renderEditBanner();
    try { localStorage.removeItem("accuretta:draft:" + state.chatId); } catch (_) {}
    // Visual truncation: drop everything after the edited turn, then re-add it
    // with the new content. The bridge mirrors this server-side.
    state.messages = state.messages.slice(0, edit.index);
    const userMsg = { role: "user", content: text, t: Math.floor(Date.now() / 1000) };
    state.messages.push(userMsg);
    renderPlanPanel([]);
    renderMessages();

    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    agentRow._attackRailEnabled = opts?.mission?.engagement === "pentest";
    agentRow._rtEngagement = opts?.mission?.engagement || "";
    agentRow.innerHTML = `
      ${AGENT_AVATAR_HTML}
      <div class="bubble-col">
        <div class="think-container think-line">
          <div class="think-header" style="cursor: pointer;">
            <i class="ph ph-caret-right think-caret"></i>
            <i class="ph ph-brain think-check-icon"></i>
            <span class="think-title shimmer">Resending your edit…</span>
          </div>
          <div class="think-content hidden"></div>
        </div>
        <div class="tool-stack" id="tool-stack"></div>
        <div class="bubble agent hidden" id="stream-bubble"></div>
        <div class="bubble-meta streaming">streaming<span class="typing"><span></span><span></span><span></span></span></div>
      </div>`;
    $("#chat-inner").appendChild(agentRow);
    window.AccurettaOrb?.setState(agentRow, "thinking");
    scrollToBottom(true);

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);
    state.liveTurn = { chatId: state.chatId, row: agentRow, userMsg };
    try {
      await streamChat(text, agentRow, state.abortCtl.signal, [], { regenerate: true });
    } catch (e) {
      if (e.name === "AbortError") {
        renderStoppedTurn(agentRow);
      } else {
        agentRow._notificationFailed = true;
        agentRow._notificationError = e.message;
        toast("edit resend failed: " + e.message, "err");
      }
    } finally {
      const finishedChatId = state.liveTurn?.chatId || state.chatId;
      streamPaceFinish(agentRow);
      noteOwnTurnEnded(finishedChatId);
      window.AccurettaResearch?.finish(agentRow);
      state.streaming = false;
      state.abortCtl = null;
      renderTaskHandoff(agentRow, finishedChatId);
      state.liveTurn = null;
      setStreamingUI(false, agentRow._notificationCancelled ? "stopped" : agentRow._notificationFailed ? "failed" : "completed");
      await loadChats();
      const finishedChat = state.chats.chats[finishedChatId];
      if (finishedChat) {
        delete finishedChat.messages;
        finishedChat._summary = true;
      }
      renderChatList();
      renderRegenerateChip();
      attachCompareChip(agentRow);
      if (agentRow._notificationFailed) notifyFailure(agentRow._notificationError, agentRow._workStart ? Date.now() - agentRow._workStart : 0);
      else if (!agentRow._notificationCancelled) notifyCompletion(agentRow._workStart ? Date.now() - agentRow._workStart : 0);
      scheduleMessageQueueDrain(finishedChatId);
    }
    return true;
  }

  // Attach the Compare affordance to a finished reply when a previous
  // version was captured (regenerate or edit-resend).
  function attachCompareChip(agentRow) {
    const pair = state._comparePair;
    state._comparePair = null;
    if (!pair || !agentRow?.isConnected) return;
    const bubble = agentRow.querySelector("#stream-bubble, .bubble.agent");
    if (!bubble || bubble.classList.contains("hidden") || !(bubble.textContent || "").trim()) return;
    const actions = agentRow.querySelector(".bubble-actions");
    if (!actions || actions.querySelector('[data-act="compare"]')) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "bubble-action";
    btn.dataset.act = "compare";
    btn.title = "Compare with the previous reply";
    btn.setAttribute("aria-label", "Compare with the previous reply");
    btn.innerHTML = '<i class="ph ph-scales"></i>';
    btn.addEventListener("click", () => openCompareDialog({
      ...pair,
      after: state.messages.length ? state.messages[state.messages.length - 1].content : "",
      afterLabel: "New reply",
    }));
    actions.appendChild(btn);
  }

  function openCompareDialog(pair) {
    if (!pair) return;
    const dialog = document.createElement("dialog");
    dialog.className = "compare-dialog";
    dialog.setAttribute("aria-label", "Compare replies");
    dialog.innerHTML = `
      <header class="dw-head">
        <div class="dw-title">
          <h2>Compare replies</h2>
          <p class="dw-sub">Both versions were produced by the model you had loaded. Nothing was sent anywhere.</p>
        </div>
        <div class="dw-head-actions">
          <button type="button" class="btn sm compare-close">Close</button>
        </div>
      </header>
      <div class="dw-body">
        <section class="compare-pane">
          <div class="compare-pane-head"><i class="ph ph-arrow-counter-clockwise" aria-hidden="true"></i><span>${esc(pair.beforeLabel || "Before")}</span></div>
          <div class="compare-pane-body">${renderMarkdown(pair.before || "*(empty reply)*")}</div>
        </section>
        <section class="compare-pane">
          <div class="compare-pane-head"><i class="ph ph-arrow-clockwise" aria-hidden="true"></i><span>${esc(pair.afterLabel || "After")}</span></div>
          <div class="compare-pane-body">${renderMarkdown(pair.after || "*(empty reply)*")}</div>
        </section>
      </div>`;
    document.body.appendChild(dialog);
    dialog.querySelector(".compare-close").onclick = () => dialog.close();
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.showModal();
  }

  // Show an action row (regenerate + copy) under the last assistant bubble.
  // Other bubbles already carry a copy-only .bubble-actions row from
  // renderBubble, so: (a) remove any prior regen chip (found by its regen
  // button, not any .bubble-actions), (b) drop the last assistant's copy-only
  // row, and (c) install the regen + copy chip there.
  function renderRegenerateChip() {
    document.querySelectorAll('.bubble-actions:has([data-act="regen"])').forEach(el => el.remove());
    const rows = [...document.querySelectorAll("#chat-inner .bubble-row")];
    const lastAssistant = rows.reverse().find(r => r.querySelector(".bubble.agent"));
    if (!lastAssistant) return;
    const col = lastAssistant.querySelector(".bubble-col");
    if (!col) return;
    const meta = col.querySelector(".bubble-meta");
    if (!meta) return;
    // strip any existing copy-only row on this bubble so we don't end up
    // with two action rows stacked under the last assistant message.
    col.querySelectorAll(".bubble-actions").forEach(el => el.remove());
    const bubble = col.querySelector(".bubble.agent");
    const actions = document.createElement("div");
    actions.className = "bubble-actions";
    actions.innerHTML = `
      <button type="button" class="bubble-action" data-act="regen" title="Regenerate"><i class="ph ph-arrow-counter-clockwise"></i></button>
      <button type="button" class="bubble-action" data-act="copy" title="Copy"><i class="ph ph-copy"></i></button>
    `;
    const regenBtn = actions.querySelector('[data-act="regen"]');
    regenBtn.addEventListener("click", regenerateLast);
    // If the last reply came back empty, pulse the retry button so the user
    // knows what to click. Cleared on the next turn (see streamChat).
    if (state.attentionRetry) regenBtn.classList.add("attention");
    actions.querySelector('[data-act="copy"]').addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const text = bubble?.innerText || "";
      try {
        await copyText(text);
        const icon = btn.querySelector("i");
        if (icon) {
          icon.classList.remove("ph-copy");
          icon.classList.add("ph-check");
          btn.classList.add("copied");
          setTimeout(() => {
            icon.classList.remove("ph-check");
            icon.classList.add("ph-copy");
            btn.classList.remove("copied");
          }, 1400);
        }
      } catch {
        toast("copy failed", "err", 2000);
      }
    });
    meta.after(actions);
  }
  return {
    openPalette, closePalette, _fuzzyScore, refreshPaletteList, paletteMove, paletteCommit,
    missionCategory, renderChatList, renderMessages, leaveWelcome, renderBubble, getCodeText,
    enhanceCodeBlocks, renderEditBanner, sendEditTurn, renderRegenerateChip,
  };
};
