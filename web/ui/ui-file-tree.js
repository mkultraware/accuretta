/* SPDX-License-Identifier: AGPL-3.0-only */
/* workspace file tree: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["file-tree"] = ({
  $, SOURCE_VIEW_LANGS, SVG_BOOK, SVG_EYE, SVG_PYCHECK, _deckHeightBefore, _deckHeightNow,
  _deckHeightSlide, api, appendSandboxText, appendTerminalText, approvalIconSvg,
  downloadWorkspaceFile, esc, notifyApproval, previewWorkspaceHtml, previewWorkspaceMarkdown,
  previewWorkspaceSource, renderChatList, runPythonCheck, shortPath, state, tcardSync, toast,
  toolLabel, wbFileKind, wbGitCodeFor, wbGitLabel, wbLoadGit, wbOpenWorkspaceFile,
}) => {
  const FILE_ICON = {
    // scripts
    js: "ph-file-js", jsx: "ph-file-js", ts: "ph-file-ts", tsx: "ph-file-ts",
    py: "ph-file-py", rb: "ph-file-code", go: "ph-file-code", rs: "ph-file-rs",
    java: "ph-file-code", c: "ph-file-c", cpp: "ph-file-cpp", h: "ph-file-c",
    cs: "ph-file-cs", php: "ph-file-code", sh: "ph-terminal-window",
    ps1: "ph-terminal-window", bat: "ph-terminal-window", lua: "ph-file-code",
    // web
    html: "ph-file-html", htm: "ph-file-html", css: "ph-file-css",
    scss: "ph-file-css", sass: "ph-file-css", less: "ph-file-css",
    vue: "ph-file-vue", svelte: "ph-file-code",
    // data
    json: "ph-brackets-curly", yaml: "ph-brackets-angle", yml: "ph-brackets-angle",
    xml: "ph-brackets-angle", toml: "ph-brackets-angle", ini: "ph-brackets-angle",
    csv: "ph-table", tsv: "ph-table", sql: "ph-database", db: "ph-database",
    sqlite: "ph-database",
    // docs
    md: "ph-file-md", mdx: "ph-file-md", txt: "ph-file-text", rtf: "ph-file-text",
    pdf: "ph-file-pdf", doc: "ph-file-doc", docx: "ph-file-doc",
    xls: "ph-file-xls", xlsx: "ph-file-xls", ppt: "ph-file-ppt", pptx: "ph-file-ppt",
    // media
    png: "ph-file-image", jpg: "ph-file-image", jpeg: "ph-file-image",
    gif: "ph-file-image", webp: "ph-file-image", svg: "ph-file-svg",
    ico: "ph-file-image", bmp: "ph-file-image", avif: "ph-file-image",
    mp3: "ph-file-audio", wav: "ph-file-audio", flac: "ph-file-audio",
    ogg: "ph-file-audio", m4a: "ph-file-audio",
    mp4: "ph-file-video", mov: "ph-file-video", mkv: "ph-file-video",
    webm: "ph-file-video", avi: "ph-file-video",
    // archives
    zip: "ph-file-zip", rar: "ph-file-zip", "7z": "ph-file-zip",
    tar: "ph-file-zip", gz: "ph-file-zip", bz2: "ph-file-zip",
    // config
    env: "ph-key", lock: "ph-lock-simple", log: "ph-article",
    gitignore: "ph-git-branch", dockerfile: "ph-cube",
  };
  function fileIconFor(name, ext) {
    const lower = (name || "").toLowerCase();
    if (lower === "dockerfile") return "ph-cube";
    if (lower === "makefile") return "ph-hammer";
    if (lower === "license" || lower === "license.md") return "ph-scales";
    if (lower === "readme" || lower === "readme.md") return "ph-book-open-text";
    if (lower.startsWith(".git")) return "ph-git-branch";
    return FILE_ICON[ext] || "ph-file";
  }
  function folderLeafName(path) {
    return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  }
  async function fetchFolderListing(path) {
    try {
      const r = await api(`/api/list-folder?path=${encodeURIComponent(path)}`);
      if (r.error) throw new Error(r.error);
      return r.entries || [];
    } catch (e) {
      return { _error: e.message || String(e) };
    }
  }
  // Inline SVG icons for file actions; the icon font would impose its vertical
  // metrics on the tree row.
  const SVG_LIGHTNING = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
  const SVG_DOWNLOAD = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>';

  function fileExt(name) {
    const m = (name || "").toLowerCase().match(/\.([a-z0-9]+)$/);
    return m ? m[1] : "";
  }
  function isMarkdownFile(name) {
    const e = fileExt(name);
    return e === "md" || e === "markdown" || e === "mdown" || e === "mkd";
  }
  function isSourceViewable(name) {
    return Object.prototype.hasOwnProperty.call(SOURCE_VIEW_LANGS, fileExt(name));
  }

  // relative path from a workspace root → entry.path. `entry.path` is the
  // absolute on-disk path returned by /api/list-folder.
  function relPathFromRoot(root, abs) {
    const r = (root || "").replace(/\\/g, "/").replace(/\/+$/, "");
    const a = (abs  || "").replace(/\\/g, "/");
    if (r && a.toLowerCase().startsWith(r.toLowerCase() + "/")) return a.slice(r.length + 1);
    return a; // fallback — bridge will still validate
  }

  function isPreviewableHtml(name) {
    const n = (name || "").toLowerCase();
    return n.endsWith(".html") || n.endsWith(".htm");
  }
  function isPythonFile(name) {
    return (name || "").toLowerCase().endsWith(".py");
  }

  function renderTreeNode(entry, depth, rootFolder) {
    const node = document.createElement("div");
    node.className = entry.is_dir ? "tree-node tree-dir" : "tree-node tree-file";
    node.style.setProperty("--depth", depth);
    const icon = entry.is_dir ? "ph-folder" : fileIconFor(entry.name, entry.ext);
    const chev = entry.is_dir ? `<i class="ph ph-caret-right tree-chev"></i>` : `<span class="tree-chev-spacer"></span>`;
    // file-type-specific inline action buttons (SVG, not emoji)
    let actions = "";
    if (!entry.is_dir) {
      actions += `<button class="tree-action ws-download" title="Download to this browser">${SVG_DOWNLOAD}</button>`;
      if (isPreviewableHtml(entry.name)) {
        actions += `<button class="tree-action ws-preview-html" title="Preview this HTML in the panel">${SVG_LIGHTNING}</button>`;
      }
      if (isPythonFile(entry.name)) {
        actions += `<button class="tree-action ws-pycheck" title="Check Python syntax">${SVG_PYCHECK}</button>`;
      }
      if (isMarkdownFile(entry.name)) {
        actions += `<button class="tree-action ws-preview-md" title="Render Markdown in the panel">${SVG_BOOK}</button>`;
      } else if (isSourceViewable(entry.name) && !isPythonFile(entry.name)) {
        // .py files already get a syntax-checker that shows highlighted
        // source — no need for a duplicate "view source" button on those.
        actions += `<button class="tree-action ws-preview-source" title="View formatted source in the panel">${SVG_EYE}</button>`;
      }
    }
    const hasBadge = state.touchedFiles.has(entry.path);
    const badgeHtml = hasBadge ? `<span class="ws-badge-m" title="Modified by agent">M</span>` : "";
    const gitCode = wbGitCodeFor(entry.path, entry.is_dir);
    node.dataset.path = entry.path;
    node.dataset.name = entry.name;
    if (gitCode) node.dataset.git = gitCode;
    node.innerHTML = `
      <div class="tree-row" title="${esc(entry.path)}">
        ${chev}
        <i class="ph ${icon} tree-icon" data-kind="${esc(wbFileKind(entry.name, entry.is_dir))}"></i>
        <span class="tree-name">${esc(entry.name)}</span>
        ${badgeHtml}
        ${gitCode && !entry.is_dir ? `<span class="wb-git-code" title="git: ${esc(wbGitLabel(gitCode))}">${esc(gitCode === "??" ? "U" : gitCode.slice(0, 1))}</span>` : ""}
        ${actions ? `<span class="tree-actions">${actions}</span>` : ""}
      </div>
      ${entry.is_dir ? `<div class="tree-children" hidden></div>` : ""}`;
    if (!entry.is_dir) {
      // A plain click on a file opens it in the viewer pane.
      node.querySelector(".tree-row").addEventListener("click", (e) => {
        if (e.target.closest(".tree-action")) return;
        e.stopPropagation();
        wbOpenWorkspaceFile(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
      });
    }
    if (entry.is_dir) {
      const rowEl = node.querySelector(".tree-row");
      const kids = node.querySelector(".tree-children");
      let loaded = false;
      rowEl.addEventListener("click", async (e) => {
        e.stopPropagation();
        const expanded = !kids.hasAttribute("hidden");
        if (expanded) {
          kids.setAttribute("hidden", "");
          node.classList.remove("open");
          return;
        }
        node.classList.add("open");
        kids.removeAttribute("hidden");
        if (!loaded) {
          kids.innerHTML = `<div class="tree-loading" style="--depth:${depth + 1}">loading…</div>`;
          const entries = await fetchFolderListing(entry.path);
          kids.innerHTML = "";
          if (entries._error) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">empty</div>`;
          } else {
            for (const child of entries) kids.appendChild(renderTreeNode(child, depth + 1, rootFolder));
          }
          loaded = true;
        }
      });
    } else {
      // wire file-action buttons; both stop propagation so clicking them
      // doesn't also toggle the row.
      const downloadBtn = node.querySelector(".ws-download");
      if (downloadBtn) {
        downloadBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          downloadWorkspaceFile(rootFolder, relPathFromRoot(rootFolder, entry.path));
        });
      }
      const previewBtn = node.querySelector(".ws-preview-html");
      if (previewBtn) {
        previewBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceHtml(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const pyBtn = node.querySelector(".ws-pycheck");
      if (pyBtn) {
        pyBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          runPythonCheck(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const mdBtn = node.querySelector(".ws-preview-md");
      if (mdBtn) {
        mdBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceMarkdown(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const srcBtn = node.querySelector(".ws-preview-source");
      if (srcBtn) {
        srcBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceSource(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
    }
    return node;
  }

  function renderWorkspace() {
    const wrap = $("#ws-list");
    wrap.innerHTML = "";
    if (!state.workspace.folders.length) {
      wrap.innerHTML = `<div style="padding: 10px 12px; font-size: 11px; color: var(--fg-faint);">no folders. add one to let the agent read/write files.</div>`;
      return;
    }
    for (const f of state.workspace.folders) {
      const wrapper = document.createElement("div");
      wrapper.className = "ws-root";

      const header = document.createElement("div");
      header.className = "ws-folder";
      header.innerHTML = `
        <i class="ph ph-caret-right ws-chev"></i>
        <i class="ph ph-folder"></i>
        <span class="path" title="${esc(f)}">${esc(folderLeafName(f))}</span>
        <button class="rm" title="Remove"><i class="ph ph-x"></i></button>`;
      wrapper.appendChild(header);

      const tree = document.createElement("div");
      tree.className = "ws-tree";
      tree.hidden = true;
      wrapper.appendChild(tree);

      let loaded = false;
      header.querySelector(".rm").addEventListener("click", async (e) => {
        e.stopPropagation();
        const next = state.workspace.folders.filter(x => x !== f);
        await api("/api/workspace", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folders: next }),
        });
        state.workspace.folders = next;
        renderWorkspace();
      });
      header.addEventListener("click", async () => {
        const wasOpen = wrapper.classList.toggle("open");
        tree.hidden = !wasOpen;
        if (wasOpen && !loaded) {
          tree.innerHTML = `<div class="tree-loading" style="--depth:1">loading…</div>`;
          const [entries] = await Promise.all([fetchFolderListing(f), wbLoadGit(f, header)]);
          tree.innerHTML = "";
          if (entries._error) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">empty</div>`;
          } else {
            for (const child of entries) tree.appendChild(renderTreeNode(child, 1, f));
          }
          loaded = true;
        }
      });

      wrap.appendChild(wrapper);
    }
    // The sidebar lists every project folder, so a folder change redraws it.
    if ($("#sidebar")?.classList.contains("sb-v2")) renderChatList();
  }

  async function addWorkspaceFolder() {
    const inp = $("#ws-input");
    const v = inp.value.trim();
    if (!v) return;
    const next = Array.from(new Set([...state.workspace.folders, v]));
    const r = await api("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders: next }),
    });
    state.workspace = r;
    inp.value = "";
    $("#ws-add").classList.add("hidden");
    const wsToggle = $("#btn-ws-add-toggle");
    if (wsToggle) {
      wsToggle.setAttribute("aria-expanded", "false");
      wsToggle.title = "Add folder";
      const icon = wsToggle.querySelector("i");
      if (icon) icon.className = "ph ph-plus";
    }
    renderWorkspace();
  }

  function buildDiffBar(added, deleted) {
    const total = added + deleted;
    if (total === 0) {
      return Array(10).fill('<span class="diff-bar-segment is-gray"></span>').join("");
    }
    const greenSegments = Math.max(0, Math.min(10, Math.round((added / total) * 10)));
    const redSegments = 10 - greenSegments;
    
    let html = "";
    for (let i = 0; i < greenSegments; i++) {
      html += '<span class="diff-bar-segment is-green"></span>';
    }
    for (let i = 0; i < redSegments; i++) {
      html += '<span class="diff-bar-segment is-red"></span>';
    }
    return html;
  }

  function getFileIcon(path) {
    const ext = String(path || "").split(".").pop().toLowerCase();
    switch (ext) {
      case "py": return '<i class="ph ph-file-code" style="color:#ffd43b"></i>';
      case "html": case "htm": return '<i class="ph ph-file-html" style="color:#e34f26"></i>';
      case "css": return '<i class="ph ph-file-css" style="color:#1572b6"></i>';
      case "js": case "jsx": case "ts": case "tsx": return '<i class="ph ph-file-js" style="color:#f7df1e"></i>';
      case "json": return '<i class="ph ph-braces" style="color:#f9a825"></i>';
      default: return '<i class="ph ph-file" style="color:var(--fg-muted)"></i>';
    }
  }

  // Elapsed time since a tool's t0, formatted tight ("820ms" / "3.4s"), so
  // finished commands show their runtime.
  function fmtToolDuration(t0) {
    if (!t0) return "";
    const ms = Date.now() - Number(t0);
    if (ms <= 0) return "";
    return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  function executionTarget(evt) {
    return evt?.target === "sandbox" ? "sandbox" : "host";
  }

  function executionActivity(row, evt, create = true) {
    if (!row) return null;
    if (!row._activities) row._activities = { writes: [], commands: [], mcp: [] };
    const commands = row._activities.commands;
    const callId = evt.call_id || "";
    let item = callId ? commands.find(c => c.callId === callId) : null;
    if (!item) {
      item = commands.find(c => (c.status === "requested" || c.status === "running")
        && c.tool === evt.name && c.command === (evt.command || c.command));
    }
    if (!item && create) {
      item = {
        callId,
        command: evt.command || "",
        tool: evt.name || "command",
        target: executionTarget(evt),
        status: "requested",
        t0: 0,
        duration: "",
      };
      commands.push(item);
    }
    return item;
  }

  function appendExecutionText(target, text, isError = false, activate = true) {
    if (target === "sandbox") appendSandboxText(text, isError, activate);
    else appendTerminalText(text, isError);
  }

  // Kill control for an in-progress command on the live commands card only (not
  // writes/MCP, which have no process). Kills just that command via
  // /api/kill-command; the turn carries on with the command's partial output.
  function killBtnHtml(live) {
    return live ? '<button class="revealer-card-kill" type="button" title="Kill this command"><i class="ph ph-x"></i></button>' : "";
  }

  function killCurrentCommand() {
    const cid = state.chatId;
    if (!cid) return Promise.resolve(false);
    return api("/api/kill-command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: cid }),
    }).then(r => !!r?.ok).catch(() => false);
  }

  // Cards are re-rendered from HTML strings, so the button is handled by one
  // delegated listener. Capture phase, so the click never reaches the card
  // head's collapse toggle.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".revealer-card-kill");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    if (btn.disabled) return;
    btn.disabled = true;
    killCurrentCommand().then(ok => {
      if (!ok) {
        btn.disabled = false;
        toast("no running command to stop", "warn", 2500);
      }
    });
  }, true);

  function buildWritesCardHtml(writes, collapsed, live) {
    if (!writes || !writes.length) return "";
    const totalAdded = writes.reduce((sum, w) => sum + (w.added || 0), 0);
    const totalDeleted = writes.reduce((sum, w) => sum + (w.deleted || 0), 0);
    const fileCount = writes.length;
    
    const bodyHtml = writes.map(w => {
      const filename = w.path.split(/[\/]/).pop();
      const statusIcon = w.status === "running" ? '<i class="ph ph-circle-notch spinning" style="color:var(--accent)"></i>'
        : (w.status === "err" ? '<i class="ph ph-x-circle" style="color:var(--danger)"></i>' : '<i class="ph ph-check-circle" style="color:var(--success)"></i>');
      const diffBar = buildDiffBar(w.added, w.deleted);
      return `
        <div class="revealer-row file-row">
          <span class="file-icon">${getFileIcon(w.path)}</span>
          <span class="file-name" title="${esc(w.path)}">${esc(filename)}</span>
          <span class="file-stats">
            <span class="stat-added">+${w.added}</span>
            <span class="stat-deleted">-${w.deleted}</span>
          </span>
          <div class="diff-bar">
            ${diffBar}
          </div>
          <span class="row-status">${statusIcon}</span>
          <button class="row-action btn-preview-file" data-path="${esc(w.path)}" title="Diff/Preview"><i class="ph ph-eye"></i></button>
        </div>
      `;
    }).join("");
    
    return `
      <div class="revealer-card writes ${collapsed ? 'collapsed' : ''}" data-card-type="writes">
        <div class="revealer-card-head">
          <span class="revealer-card-icon"><i class="ph ph-pencil-simple-line"></i></span>
          <span class="revealer-card-title">Writing files...</span>
          <span class="revealer-card-stats">${fileCount} file${fileCount === 1 ? "" : "s"} <span class="stat-added">+${totalAdded}</span> <span class="stat-deleted">-${totalDeleted}</span></span>
          <span class="grow"></span>
          <button class="revealer-card-toggle" type="button"><i class="ph ph-caret-down"></i></button>
        </div>
        <div class="revealer-card-body">
          ${bodyHtml}
        </div>
      </div>
    `;
  }

  function buildCommandsCardHtml(commands, collapsed, live) {
    if (!commands || !commands.length) return "";
    const count = commands.length;
    const bodyHtml = commands.map(c => {
      const statusIcon = c.status === "running" ? '<i class="ph ph-circle-notch spinning" style="color:var(--accent)"></i>'
        : c.status === "requested" ? '<i class="ph ph-shield" style="color:var(--warning)"></i>'
        : c.status === "err" ? '<i class="ph ph-x-circle" style="color:var(--danger)"></i>'
        : '<i class="ph ph-check-circle" style="color:var(--success)"></i>';
      const shortCmd = c.command.length > 60 ? c.command.slice(0, 57) + "..." : c.command;
      return `
        <div class="revealer-row command-row">
          <span class="row-status">${statusIcon}</span>
          <span class="command-target ${c.target === "sandbox" ? "is-sandbox" : "is-host"}">${c.target === "sandbox" ? "WSL GUEST" : "HOST"}</span>
          <span class="command-text" title="${esc(c.command)}"><code>${esc(shortCmd)}</code></span>
          <span class="grow"></span>
          <span class="row-time">${c.duration || (c.status === "requested" ? "waiting..." : "running...")}</span>
        </div>
      `;
    }).join("");
    
    const hasRunning = commands.some(c => c.status === "running");
    return `
      <div class="revealer-card commands ${collapsed ? 'collapsed' : ''}" data-card-type="commands">
        <div class="revealer-card-head">
          <span class="revealer-card-icon"><i class="ph ph-terminal-window"></i></span>
          <span class="revealer-card-title">${hasRunning ? "Running commands..." : "Command requests"}</span>
          <span class="revealer-card-stats">${count} command${count === 1 ? "" : "s"}</span>
          <span class="grow"></span>
          ${killBtnHtml(live && hasRunning)}
          <button class="revealer-card-toggle" type="button"><i class="ph ph-caret-down"></i></button>
        </div>
        <div class="revealer-card-body">
          ${bodyHtml}
        </div>
      </div>
    `;
  }

  function buildMcpCardHtml(mcp, collapsed, live) {
    if (!mcp || !mcp.length) return "";
    const count = mcp.length;
    let serverName = "MCP";
    if (mcp[0].name.startsWith("mcp_")) {
      const parts = mcp[0].name.split("_");
      if (parts.length > 1) serverName = parts[1];
    }
    
    const bodyHtml = mcp.map(m => {
      const statusIcon = m.status === "running" ? '<i class="ph ph-circle-notch spinning" style="color:var(--accent)"></i>'
        : (m.status === "err" ? '<i class="ph ph-x-circle" style="color:var(--danger)"></i>' : '<i class="ph ph-check-circle" style="color:var(--success)"></i>');
      const cleanName = m.name.replace(/^mcp_[^_]+_/, "");
      return `
        <div class="revealer-row mcp-row">
          <span class="row-status">${statusIcon}</span>
          <span class="mcp-text">${esc(cleanName)}</span>
          <span class="grow"></span>
          <span class="row-time">${m.duration || "running..."}</span>
        </div>
      `;
    }).join("");
    
    return `
      <div class="revealer-card mcp ${collapsed ? 'collapsed' : ''}" data-card-type="mcp">
        <div class="revealer-card-head">
          <span class="revealer-card-icon"><i class="ph ph-nodes"></i></span>
          <span class="revealer-card-title">Using MCP tools...</span>
          <span class="revealer-card-stats">${esc(serverName)} · ${count} tool${count === 1 ? "" : "s"}</span>
          <span class="grow"></span>
          <button class="revealer-card-toggle" type="button"><i class="ph ph-caret-down"></i></button>
        </div>
        <div class="revealer-card-body">
          ${bodyHtml}
        </div>
      </div>
    `;
  }

  function renderPermissionsChecklist(a) {
    const details = a.details || {};
    const kind = details.kind || "command";
    let html = "";
    
    html += `
      <div class="permission-item">
        <span class="permission-item-icon check"><i class="ph ph-check-circle"></i></span>
        <span class="permission-item-text">Read project files</span>
      </div>
    `;
    
    html += `
      <div class="permission-item">
        <span class="permission-item-icon check"><i class="ph ph-check-circle"></i></span>
        <span class="permission-item-text">Search code and documentation</span>
      </div>
    `;

    if (kind === "write_file" || kind === "edit_file") {
      const path = details.path || "file";
      const filename = path.split(/[\/]/).pop();
      const added = details.added || (a.command ? a.command.split("\n").length : 0);
      const deleted = details.deleted || 0;
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
          <span class="permission-item-text">Write to <code>${esc(filename)}</code></span>
          <span class="permission-item-stats">
            <span class="stat-added">+${added} LoC</span> / <span class="stat-deleted">-${deleted} LoC</span>
          </span>
        </div>
      `;
    } else if (kind === "delete") {
      // Show WHICH file/folder is going away
      const path = details.path || a.command || "(unknown path)";
      const isDir = details.dir ? "folder" : "file";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
          <span class="permission-item-text">Delete ${isDir}: <code>${esc(path)}</code></span>
        </div>
      `;
    } else if (kind === "git") {
      // Show the real git command line
      const cmd = a.command || "git ...";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-git-branch"></i></span>
          <span class="permission-item-text">Run: <code>${esc(cmd)}</code></span>
        </div>
      `;
    } else if (kind === "session") {
      const cmd = a.command || "";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-terminal-window"></i></span>
          <span class="permission-item-text">${details.session_id ? "Send input to" : "Start"} a persistent process on the <strong>Windows host</strong>: <code>${esc(cmd)}</code></span>
          <span class="permission-item-badge">HOST</span>
        </div>
        ${details.cwd ? `<div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-folder-open"></i></span>
          <span class="permission-item-text">Working folder: <code>${esc(details.cwd)}</code></span>
        </div>` : ""}
      `;
    } else if (kind === "powershell" || kind === "run_powershell" || kind === "command") {
      const cmd = a.command || "";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
          <span class="permission-item-text">Run on the <strong>Windows host</strong>: <code>${esc(cmd)}</code></span>
          <span class="permission-item-badge">HOST</span>
        </div>
      `;
      if (details.code_preview) {
        html += `
          <div class="permission-item">
            <span class="permission-item-icon warning"><i class="ph ph-code"></i></span>
            <span class="permission-item-text">Python script it runs:
<code>${esc(String(details.code_preview))}</code></span>
          </div>
        `;
      }
    } else if (kind === "test") {
      const cmd = a.command || "";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-terminal-window"></i></span>
          <span class="permission-item-text">Run project code on the <strong>Windows host</strong>: <code>${esc(cmd)}</code></span>
          <span class="permission-item-badge">HOST</span>
        </div>
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-folder-open"></i></span>
          <span class="permission-item-text">Working folder: <code>${esc(details.cwd || "project workspace")}</code></span>
        </div>
      `;
    } else if (kind === "sandbox") {
      const cmd = a.command || "";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-linux-logo"></i></span>
          <span class="permission-item-text">Run in the <strong>accuretta-sbx WSL guest</strong>: <code>${esc(cmd)}</code></span>
          <span class="permission-item-badge">WSL GUEST</span>
        </div>
        ${details.cwd ? `<div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-folder-open"></i></span>
          <span class="permission-item-text">Working folder: <code>${esc(details.cwd)}</code></span>
        </div>` : ""}
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning-octagon"></i></span>
          <span class="permission-item-text">Windows programs are disabled, but <code>/mnt</code> still exposes real host files. Do not use this guest to execute unknown malware.</span>
        </div>
      `;
    } else if (kind === "recon_s3") {
      // Third-party touch (AWS S3, outside the target): make that explicit so
      // the user can decline if probing AWS is out of scope.
      const bl = Array.isArray(details.buckets) ? details.buckets : [];
      const sample = bl.slice(0, 4).join(", ") + (bl.length > 4 ? "…" : "");
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-globe-hemisphere-west"></i></span>
          <span class="permission-item-text">Probe <strong>public AWS S3 buckets</strong> — a third party outside <code>${esc(details.host || "the target")}</code></span>
          <span class="permission-item-badge">read-only</span>
        </div>
        ${bl.length ? `<div class="permission-item">
          <span class="permission-item-icon check"><i class="ph ph-list-magnifying-glass"></i></span>
          <span class="permission-item-text">${bl.length} candidate bucket${bl.length === 1 ? "" : "s"}: <code>${esc(sample)}</code></span>
        </div>` : ""}
      `;
    } else if (kind && (kind.startsWith("desktop.") || kind.startsWith("desktop_"))) {
      const element = details.element || {};
      const windowInfo = details.window || {};
      const target = element.name || element.role || details.title || "desktop control";
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-desktop"></i></span>
          <span class="permission-item-text">${esc(kind.replace(/^desktop[._]/, "").replaceAll("_", " "))}: <code>${esc(target)}</code></span>
          <span class="permission-item-badge">DESKTOP</span>
        </div>
        ${windowInfo.title ? `<div class="permission-item">
          <span class="permission-item-icon check"><i class="ph ph-app-window"></i></span>
          <span class="permission-item-text">Window: <code>${esc(windowInfo.title)}</code></span>
        </div>` : ""}
        ${details.sensitive ? `<div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-password"></i></span>
          <span class="permission-item-text">Sensitive field value is hidden from this approval card.</span>
        </div>` : ""}
      `;
    } else if (kind && kind.startsWith("mcp_")) {
      const m = kind.match(/^mcp_([^_]+)_(.+)$/);
      const server = m ? m[1] : "MCP";
      const tool = m ? m[2] : kind.slice(4);
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
          <span class="permission-item-text">Use MCP tool <code>${esc(tool)}</code></span>
          <span class="permission-item-badge">${esc(server)}</span>
        </div>
      `;
    } else {
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
          <span class="permission-item-text">Execute privileged action: <code>${esc(kind.toUpperCase())}</code></span>
        </div>
      `;
    }
    if (details.red_team_context && details.executes_code
        && String(details.target || "").startsWith("host")) {
      html += `
        <div class="permission-item">
          <span class="permission-item-icon warning"><i class="ph ph-warning-octagon"></i></span>
          <span class="permission-item-text">This runs code on your real PC during a security assessment. Do not approve it for a file recovered from the target.</span>
        </div>
      `;
    }
    return html;
  }

  function reflectApprovalInLiveTurn(approval, decision = "pending") {
    const row = state.liveTurn?.row;
    if (!row || !approval) return;
    if (approval.chat_id && approval.chat_id !== state.liveTurn?.chatId) return;
    window.AccurettaOrb?.setState(row, decision === "pending" ? "waiting" : "working");
    const details = approval.details || {};
    const kind = String(details.kind || "");
    const path = String(details.path || "");
    const candidates = [...row.querySelectorAll(".tcard-row.running")].reverse();
    const line = candidates.find(item => (!kind || item.dataset.name === kind)
      && (!path || !item.dataset.path || item.dataset.path === path)) || candidates[0];
    const label = line?.querySelector(".tcard-label");
    if (label) {
      if (decision === "pending") {
        const action = kind === "edit_file" ? `Edit ${shortPath(path)}`
          : kind === "write_file" ? `Write ${shortPath(path)}`
            : (approval.title || "requested action");
        label.textContent = `Waiting for approval · ${action}`;
      } else if (decision === "deny") {
        label.classList.remove("shimmer");
        label.textContent = `Approval denied · ${approval.title || "action"}`;
      } else if (kind) {
        label.textContent = toolLabel(kind, details);
      }
      line.dataset.waiting = String(decision === "pending");
      tcardSync(line.closest(".tcard"));
    }

    const writes = row._activities?.writes || [];
    const write = [...writes].reverse().find(item => (!path || item.path === path)
      && (item.status === "running" || item.status === "requested"));
    if (write) {
      write.status = decision === "pending" ? "requested" : decision === "deny" ? "err" : "running";
    }
  }

  function renderApprovals() {
    renderChatList();
    const deck = $("#revealer-deck");
    if (!deck) return;
    
    const approvals = [...state.approvals.values()];
    const heightBefore = _deckHeightBefore(deck);
    deck.classList.toggle("has-approval", approvals.length > 0);
    const liveIds = new Set(approvals.map(a => String(a.id)));
    const existing = new Map();
    deck.querySelectorAll(".revealer-card.permissions").forEach(card => {
      if (liveIds.has(card.dataset.approvalId)) existing.set(card.dataset.approvalId, card);
      else card.remove();
    });
    if (!approvals.length) {
      if (deck.children.length === 0) deck.innerHTML = "";
      _deckHeightSlide(deck, heightBefore, _deckHeightNow(deck));
      return;
    }
    const newestIndex = approvals.length - 1;
    approvals.forEach((a, index) => {
      const depth = newestIndex - index;
      const depthClass = depth === 0 ? "approval-front"
        : depth === 1 ? "approval-depth-1"
          : depth === 2 ? "approval-depth-2" : "approval-depth-hidden";
      const prior = existing.get(String(a.id));
      if (prior) {
        // Reconcile in place: never rebuild a card the user is reading, so an
        // expanded card and its scroll position survive polls and pushes.
        prior.classList.remove("approval-front", "approval-depth-1", "approval-depth-2", "approval-depth-hidden");
        prior.classList.add(depthClass);
        const head = prior.querySelector(".revealer-card-head");
        const grow = head?.querySelector(".grow");
        let count = head?.querySelector(".approval-stack-count");
        if (depth === 0 && approvals.length > 1) {
          if (!count && grow) {
            count = document.createElement("span");
            count.className = "approval-stack-count";
            grow.after(count);
          }
          if (count) count.textContent = `${approvals.length} pending`;
        } else if (count) {
          count.remove();
        }
        return;
      }
      const card = document.createElement("div");
      card.className = "revealer-card permissions collapsed " + depthClass;
      card.dataset.cardType = "permissions";
      card.dataset.approvalId = a.id;
      
      const checklistHtml = renderPermissionsChecklist(a);
      // Soft mode says why it stopped; the first reason rides in the header so
      // it reads without expanding the card.
      const softReasons = Array.isArray(a.details?.soft_reasons) ? a.details.soft_reasons.map(String) : [];
      const statsHtml = softReasons.length
        ? `<span class="revealer-card-stats perm-soft-reason" title="${esc(softReasons.join("\n"))}">Asks because it ${esc(softReasons[0])}${softReasons.length > 1 ? ` (+${softReasons.length - 1} more)` : ""}</span>`
        : `<span class="revealer-card-stats">Review and confirm the actions the agent will take.</span>`;
      const reasonsHtml = softReasons.length
        ? `<div class="permissions-soft-reasons">
            <div class="permissions-soft-reasons-title">Why it asked</div>
            ${softReasons.map(r => `<div class="permission-item">
              <span class="permission-item-icon warning"><i class="ph ph-warning"></i></span>
              <span class="permission-item-text">It ${esc(r)}</span>
            </div>`).join("")}
          </div>`
        : "";

      card.innerHTML = `
        <div class="revealer-card-head">
          <span class="revealer-card-icon">${approvalIconSvg(a)}</span>
          <span class="revealer-card-title">${esc(a.title || "Review requested action")}</span>
          ${statsHtml}
          <span class="grow"></span>
          ${depth === 0 && approvals.length > 1 ? `<span class="approval-stack-count">${approvals.length} pending</span>` : ""}
          <div class="perm-head-actions">
            <button class="perm-quick perm-quick-deny" data-act="deny" type="button" title="Deny">Deny</button>
            ${a.details && a.details.critical ? `` : `<button class="perm-quick perm-quick-approve" data-act="approve" type="button" title="Approve">Approve</button>`}
          </div>
          <button class="revealer-card-toggle" type="button"><i class="ph ph-caret-down"></i></button>
        </div>
        <div class="revealer-card-body">
          ${reasonsHtml}
          <div class="permissions-intro">Review the requested actions:</div>
          <div class="permissions-checklist">
            ${checklistHtml}
          </div>
          ${a.details && a.details.critical ? `<div class="permissions-critical"><i class="ph ph-warning-octagon"></i> ${esc(a.details.critical_note || "Protected operation - this can change Windows itself. It never auto-approves: press and HOLD to confirm.")}</div>` : ``}
          ${a.details && a.details.allow_always ? `<div class="permissions-options">
            <label class="permission-option-label">
              <input type="checkbox" id="perm-remember-session">
              <span class="custom-checkbox"></span>
              <span>${esc(a.details.allow_always_label || "Remember this action for this session")}</span>
            </label>
          </div>` : ``}
          <div class="permissions-actions">
            <button class="perm-btn perm-btn-cancel" data-act="deny">Deny</button>
            ${a.details && a.details.critical
              ? `<button class="perm-btn perm-btn-hold" type="button"><span class="hold-fill"></span><span class="hold-label">Hold to approve</span></button>`
              : `<button class="perm-btn perm-btn-continue" data-act="approve">Approve</button>`}
          </div>
        </div>
      `;
      
      const head = card.querySelector(".revealer-card-head");
      head.addEventListener("click", () => card.classList.toggle("collapsed"));
      
      // Critical (protected) approvals: approve is press-and-HOLD (1.5s) so a
      // single mis-click can't push through a registry/System32 change.
      const holdBtn = card.querySelector(".perm-btn-hold");
      if (holdBtn) {
        let holdTimer = null;
        const fill = holdBtn.querySelector(".hold-fill");
        const startHold = (e) => {
          e.stopPropagation();
          e.preventDefault();
          if (holdTimer) return;
          const t0 = Date.now();
          holdBtn.classList.add("holding");
          holdTimer = setInterval(() => {
            const pct = Math.min(100, ((Date.now() - t0) / 1500) * 100);
            if (fill) fill.style.width = pct + "%";
            if (pct >= 100) {
              clearInterval(holdTimer);
              holdTimer = null;
              decideApproval(a.id, "approve", false);
            }
          }, 30);
        };
        const cancelHold = () => {
          if (holdTimer) { clearInterval(holdTimer); holdTimer = null; }
          holdBtn.classList.remove("holding");
          if (fill) fill.style.width = "0%";
        };
        holdBtn.addEventListener("mousedown", startHold);
        holdBtn.addEventListener("touchstart", startHold, { passive: false });
        ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach(ev =>
          holdBtn.addEventListener(ev, cancelHold));
      }
      // Bind both the head quick-actions and the body buttons; stopPropagation
      // keeps a head click from toggling the card's collapse. With "remember this
      // action for this session" ticked, approve with always=true so the bridge
      // session-allows this action kind.
      card.querySelectorAll('[data-act="approve"]').forEach(b => b.addEventListener("click", (e) => {
        e.stopPropagation();
        const remember = !!card.querySelector("#perm-remember-session")?.checked;
        decideApproval(a.id, "approve", remember);
      }));
      card.querySelectorAll('[data-act="deny"]').forEach(b => b.addEventListener("click", (e) => {
        e.stopPropagation();
        decideApproval(a.id, "deny");
      }));

      deck.appendChild(card);
    });
    _deckHeightSlide(deck, heightBefore, _deckHeightNow(deck));
  }

  // A new approval can arrive twice (the turn's own stream and the shared
  // one); only the first draws the card and plays the sound. One already
  // decided here never comes back.
  const decidedApprovals = new Set();
  function receiveApproval(approval) {
    const id = approval.id;
    if (state.approvals.has(id) || decidedApprovals.has(id)) return;
    state.approvals.set(id, approval);
    reflectApprovalInLiveTurn(approval, "pending");
    renderApprovals();
    notifyApproval();
  }

  async function decideApproval(id, decision, always) {
    decidedApprovals.add(id);
    state.approvals.delete(id);
    renderApprovals();
    await fetch("/api/approvals/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, decision, always: !!always }),
    });
  }

  let approvalLoadPromise = null;
  async function loadApprovals({ announceNew = false } = {}) {
    if (approvalLoadPromise) return approvalLoadPromise;
    approvalLoadPromise = (async () => {
      const r = await api("/api/approvals");
      const pending = Array.isArray(r.pending) ? r.pending : [];
      const known = new Set(state.approvals.keys());
      state.approvals.clear();
      for (const approval of pending) state.approvals.set(approval.id, approval);
      renderApprovals();
      if (announceNew) {
        for (const approval of pending) {
          if (known.has(approval.id)) continue;
          reflectApprovalInLiveTurn(approval, "pending");
          notifyApproval();
        }
      }
    })();
    try {
      return await approvalLoadPromise;
    } finally {
      approvalLoadPromise = null;
    }
  }

  // The box above the input (approval cards, notices, queued updates) shows
  // only when something is in it. That was a CSS :has() rule, and Safari
  // doesn't always re-check :has() when children are added later: on a Mac
  // the approval card sat in a hidden box until some other change restyled it.
  // A class set here works the same in every browser.
  function watchComposerInbox() {
    const inbox = document.querySelector(".composer-inbox");
    if (!inbox) return;
    const sync = () => {
      const on = [...inbox.children].some(child => !child.classList.contains("hidden")
        && (child.children.length > 0 || child.textContent.trim() !== ""));
      if (inbox.classList.contains("has-items") !== on) inbox.classList.toggle("has-items", on);
    };
    new MutationObserver(sync).observe(inbox, {
      childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["class"],
    });
    sync();
  }

  let approvalSyncStarted = false;
  function startApprovalSync() {
    if (approvalSyncStarted) return;
    approvalSyncStarted = true;
    watchComposerInbox();
    // SSE `approval:new` / `approval:decided` is the primary path. These
    // catch-ups cover a push missed while the tab was backgrounded or the
    // connection was down: resync the pending list when the tab is visible again.
    window.addEventListener("focus", () => {
      loadApprovals({ announceNew: true }).catch(error => console.warn("approval sync failed", error));
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        loadApprovals({ announceNew: true }).catch(error => console.warn("approval sync failed", error));
      }
    });
  }
  return {
    fileIconFor, folderLeafName, fetchFolderListing, fileExt, relPathFromRoot, renderTreeNode,
    renderWorkspace, addWorkspaceFolder, fmtToolDuration, executionTarget, executionActivity,
    appendExecutionText, buildWritesCardHtml, buildCommandsCardHtml, buildMcpCardHtml,
    reflectApprovalInLiveTurn, renderApprovals, decidedApprovals, receiveApproval, decideApproval,
    loadApprovals, startApprovalSync,
  };
};
