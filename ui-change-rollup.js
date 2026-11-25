/* SPDX-License-Identifier: AGPL-3.0-only */
/* per-turn change rollup + one-click Undo: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["change-rollup"] = ({
  $, api, app, autoResize, copyText, esc, humanBytes, initSettingsSections, loadChats,
  previewWorkspaceHtml, previewWorkspaceMarkdown, previewWorkspaceSource, renderChatList,
  selectChat, state, toast,
}) => {
  // Rendered from the bridge's `turn_changes` event. Undo reverts every file the
  // turn wrote via /api/undo (the backend keeps a pre-write snapshot journal).
  //
  // Deepest directory shared by every changed path (case-insensitive for
  // Windows drive letters); "" when there is no meaningful common ancestor
  // (e.g. files across workspace roots).
  function _commonDirOf(paths) {
    const ps = (paths || []).filter(Boolean);
    if (!ps.length) return "";
    if (ps.length === 1) return ps[0].replace(/[\\/][^\\/]*$/, "");
    let prefix = ps[0];
    for (const q of ps.slice(1)) {
      let i = 0;
      while (i < prefix.length && i < q.length && prefix[i].toLowerCase() === q[i].toLowerCase()) i++;
      prefix = prefix.slice(0, i);
      if (!prefix) break;
    }
    const cut = Math.max(prefix.lastIndexOf("\\"), prefix.lastIndexOf("/"));
    return cut > 0 ? prefix.slice(0, cut) : "";
  }

  function taskFileLocation(file) {
    const path = String(file.path || "").replace(/\\/g, "/");
    for (const folder of [...(state.workspace?.folders || [])].sort((a, b) => b.length - a.length)) {
      const root = folder.replace(/\\/g, "/").replace(/\/$/, "");
      const insensitive = /^[a-z]:\//i.test(root);
      if ((insensitive ? path.toLowerCase() : path).startsWith((insensitive ? root.toLowerCase() : root) + "/")) {
        return { root: folder, rel: path.slice(root.length + 1) };
      }
    }
    return null;
  }

  function taskFileAction(file) {
    if (file.removed || !taskFileLocation(file)) return "";
    if (file.text_diff && /\.html?$/i.test(file.name)) return "Open preview";
    if (file.text_diff && /\.md$/i.test(file.name)) return "View document";
    return file.text_diff ? "Open file" : "Show in folder";
  }

  async function openTaskFile(file) {
    const location = taskFileLocation(file);
    if (!location) throw new Error("This file is outside the current workspace");
    const { root, rel } = location;
    if (file.text_diff && /\.html?$/i.test(file.name)) previewWorkspaceHtml(root, rel, file.name);
    else if (file.text_diff && /\.md$/i.test(file.name)) await previewWorkspaceMarkdown(root, rel, file.name);
    else if (file.text_diff) await previewWorkspaceSource(root, rel, file.name);
    else {
      const path = file.path.replace(/[\\/][^\\/]+$/, "");
      const result = await api("/api/open-folder", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
      if (result.error) throw new Error(result.error);
    }
  }

  function renderWorkspaceIdentity(chat = state.chats?.chats?.[state.chatId]) {
    const host = $("#topbar-chat-context")?.parentElement;
    if (!host) return;
    let label = host.querySelector(".workflow-location");
    if (!label) { label = document.createElement("div"); label.className = "workflow-location"; host.appendChild(label); }
    const folder = chat?.github_worktree?.path || chat?.project_workspace?.path || state.workspace?.folders?.[0];
    const device = $("#execution-target-select")?.selectedOptions[0]?.textContent || "Inference PC";
    const name = folder ? String(folder).replace(/[\\/]+$/, "").split(/[\\/]/).pop() : "No workspace selected";
    label.textContent = `${name} · ${device}`;
    label.title = `${folder || "No workspace selected"} · Actions run on ${device}`;
  }

  function initWorkflowUI() {
    initSettingsSections();
    $("#btn-task-details")?.addEventListener("click", () => showTaskDetails());
    const model = $('#settings-body [data-settings-section="model"] .settings-section-content');
    const first = $("#set-gpu")?.closest(".form-row");
    if (model && first?.parentElement === model) {
      const details = document.createElement("details"); details.className = "workflow-advanced";
      details.innerHTML = "<summary>Advanced model settings</summary>";
      let node = first;
      while (node) { const next = node.nextElementSibling; details.appendChild(node); node = next; }
      model.appendChild(details);
    }
    const appearance = $('#settings-body [data-settings-section="appearance"] .settings-section-content');
    if (appearance) {
      const row = document.createElement("div"); row.className = "form-row";
      row.innerHTML = '<label for="activity-detail">Activity detail</label><select id="activity-detail"><option value="concise">Concise</option><option value="detailed">Detailed</option></select>';
      const select = row.querySelector("select");
      select.value = localStorage.getItem("accuretta:activity-detail") === "detailed" ? "detailed" : "concise";
      app.dataset.activityDetail = select.value;
      select.onchange = () => { app.dataset.activityDetail=select.value; localStorage.setItem("accuretta:activity-detail",select.value); };
      appearance.prepend(row);
    }
    renderWorkspaceIdentity();
    let refreshing = false;
    setInterval(async () => {
      if (refreshing || document.visibilityState !== "visible") return;
      refreshing = true;
      try { await loadChats(); renderChatList(); } catch (_) {} finally { refreshing = false; }
    }, 10000);
  }

  function controlFeedback(button, message = "Saved") {
    if (!button) return;
    if (button._feedbackOriginal === undefined) button._feedbackOriginal = button.innerHTML;
    clearTimeout(button._feedbackTimer);
    button.innerHTML = `<i class="ph ph-check" aria-hidden="true"></i><span>${esc(message)}</span>`;
    // Restart the pop even on a rapid second save (remove → reflow → add).
    button.classList.remove("control-saved");
    void button.offsetWidth;
    button.classList.add("control-saved");
    button._feedbackTimer = setTimeout(() => {
      if (button.isConnected) {
        button.innerHTML = button._feedbackOriginal;
        button.classList.remove("control-saved");
      }
      delete button._feedbackOriginal;
    }, 1800);
  }

  function workflowDialog(title) {
    const dialog = document.createElement("dialog");
    dialog.className = "workflow-dialog";
    dialog.setAttribute("aria-label", title);
    dialog.innerHTML = `<header><h2>${esc(title)}</h2><button class="btn sm" type="button">Close</button></header><div class="workflow-body"></div>`;
    dialog.querySelector("header button").onclick = () => dialog.close();
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    document.body.appendChild(dialog);
    dialog.showModal();
    return dialog;
  }

  async function showTaskDetails(chatId = state.chatId) {
    if (!chatId) return;
    const dialog = workflowDialog("Task details");
    const body = dialog.querySelector(".workflow-body");
    body.innerHTML = '<p role="status">Loading task…</p>';
    try {
      const chat = await api(`/api/chats/${encodeURIComponent(chatId)}`);
      if (chat.error) throw new Error(chat.error);
      if (!dialog.isConnected) return;
      const turns = [...(chat.undo_turns || [])].reverse();
      const latest = turns[0];
      const checks = latest?.verification || { status: "unchecked", checks: [] };
      const debt = chat.verification_debt || [];
      const outcome = {working:"Working", "needs-you":"Needs you", interrupted:"Interrupted", stopped:"Stopped", failed:"Failed", finished:"Finished", ready:"Ready"}[chat.task_state] || "Saved task";
      body.innerHTML = `<p class="workflow-kicker">${esc(outcome)} · ${esc(chat.title || "Session")}</p>
        <div class="workflow-sections">
          <section><h3>Changes</h3><p>${latest ? `${latest.files.length} file(s) in the latest recorded task` : "No recorded file edits."}</p><div class="workflow-turns"></div></section>
          <section><h3>Checks</h3><p>${esc({passed:"Recorded checks passed",failed:"Recorded checks failed",partial:"Some edits were checked",unchecked:"No checks recorded for file edits"}[checks.status] || "No checks recorded")}</p><ul>${(checks.checks || []).map(c => `<li>${esc(c.label)}: ${esc(c.status)}${c.detail ? `<p>${esc(c.detail)}</p>` : ""}</li>`).join("")}</ul></section>
          <section><h3>Needs attention</h3>${debt.length ? `<ul>${debt.map(d => `<li>${esc(d.path || d.file || d.reason || JSON.stringify(d))}</li>`).join("")}</ul>` : '<p>No outstanding verification items recorded. This is not a guarantee that everything was tested.</p>'}</section>
        </div><div class="workflow-actions"></div>`;
      const turnList = body.querySelector(".workflow-turns");
      turns.forEach((turn, i) => {
        const button = document.createElement("button"); button.type = "button"; button.className = "workflow-file-button";
        button.textContent = `${i === 0 ? "Latest task" : `Earlier task ${i}`} · ${turn.files.length} file(s)${turn.undone ? " · undone" : ""}`;
        button.onclick = () => { dialog.close(); reviewTaskChanges(turn, chatId); };
        turnList.appendChild(button);
      });
      const actions = body.querySelector(".workflow-actions");
      if (chat.mission) {
        const button = document.createElement("button"); button.type = "button"; button.className = "btn sm"; button.textContent = "Investigation";
        button.onclick = () => { dialog.close(); showInvestigation(chatId); }; actions.appendChild(button);
      }
      if (["interrupted", "stopped", "failed"].includes(chat.task_state)) {
        const resume = document.createElement("button"); resume.type = "button"; resume.className = "btn sm"; resume.textContent = "Prepare to resume";
        resume.onclick = async () => {
          dialog.close();
          if (state.chatId !== chatId) await selectChat(chatId);
          const input = $("#composer-input");
          if (!input.value.trim()) input.value = "Continue the interrupted task. Inspect the saved results first and avoid repeating completed actions.";
          localStorage.setItem("accuretta:draft:" + chatId, input.value); autoResize(input); input.focus();
        };
        actions.appendChild(resume);
      }
    } catch (error) { body.textContent = error.message || "Could not load task details"; }
  }

  async function showInvestigation(chatId) {
    const dialog = workflowDialog("Investigation");
    const body = dialog.querySelector(".workflow-body");
    body.innerHTML = '<p role="status">Loading saved evidence…</p>';
    const read = async (extra = {}) => {
      const data = await api("/api/investigation-review", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({chat_id:chatId,...extra}) });
      if (data.error) throw new Error(data.error); return data;
    };
    try {
      const data = await read(); if (!dialog.isConnected) return;
      const mission = data.mission || {};
      body.innerHTML = `<section><h3>Scope</h3><p>${esc(mission.target || "No target recorded")}</p><pre>${esc(typeof mission.scope === "string" ? mission.scope : JSON.stringify(mission.scope || {}, null, 2))}</pre><p>${esc(mission.objective || "")}</p></section>
        <section><h3>Findings</h3>${data.findings.length ? data.findings.map(f => `<details><summary>${esc(f.title || f.claim || f.id)} · ${esc(f.status || "unreviewed")}</summary><p>${esc(f.description || f.impact || "")}</p><div class="finding-evidence" data-ids="${esc(JSON.stringify(f.evidence_ids || []))}"></div></details>`).join("") : "<p>No recorded findings.</p>"}</section>
        <section><h3>Untested and limited checks</h3><p>Only explicitly recorded checks are listed. Missing entries do not mean tested.</p>${(data.planned_checks || []).filter(p => p.state !== "observed" || p.limitations).map(p => `<p><strong>${esc(p.feature || p.target)}</strong> · ${esc(p.state)}<br>${esc(p.limitations || "")}</p>`).join("") || "<p>No untested checks have been recorded.</p>"}</section>
        <section><h3>Evidence</h3><p>${data.total} saved observations. Observations do not establish full vulnerability coverage.</p><div class="evidence-list"></div><button class="btn sm evidence-more" type="button" hidden>Load more</button></section>`;
      const evidenceButton = (id, label) => {
        const button = document.createElement("button"); button.type="button"; button.className="workflow-file-button"; button.textContent=label;
        button.onclick=async()=>{
          const detail=workflowDialog("Evidence"); const content=detail.querySelector(".workflow-body"); content.textContent="Loading…";
          try { const result=await read({evidence_id:id}); const pre=document.createElement("pre"); pre.textContent=JSON.stringify(result.evidence,null,2); content.replaceChildren(pre); }
          catch(error){content.textContent=error.message;}
        }; return button;
      };
      body.querySelectorAll(".finding-evidence").forEach(host => JSON.parse(host.dataset.ids).forEach(id=>host.appendChild(evidenceButton(id,`Evidence ${id.slice(0,8)}`))));
      const list=body.querySelector(".evidence-list"), more=body.querySelector(".evidence-more");
      let offset=data.next_offset;
      const append=(page)=>{ (page.observations || []).forEach(r=>list.appendChild(evidenceButton(r.id,`${r.target || r.tool || r.id} · ${r.state || "recorded"}`))); offset=page.next_offset; more.hidden=offset == null; };
      append(data);
      more.onclick=async()=>{more.disabled=true;try{append(await read({offset}));}catch(error){more.textContent=error.message;}finally{more.disabled=false;}};
    } catch(error) {body.textContent=error.message || "Could not load investigation";}
  }

  function renderTaskHandoff(row, chatId) {
    if (!row || row.querySelector(".task-handoff")) return;
    const button = document.createElement("button"); button.type="button"; button.className="task-handoff";
    button.textContent = row._notificationCancelled ? "Stopped · Review saved work" : row._notificationFailed ? "Task failed · Review saved work" : "Task details · Changes and checks";
    button.onclick=()=>showTaskDetails(chatId);
    (row.querySelector(".bubble-col") || row).appendChild(button);
  }

  async function reviewTaskChanges(evt, chatId) {
    // Diff review workspace: a persistent two-pane reviewer for this task's
    // recorded edits. Left rail = changed files with per-file verdicts;
    // right = hunk-annotated unified diff with line numbers and hunk nav.
    const dialog = document.createElement("dialog");
    dialog.className = "task-review-dialog diff-workspace";
    dialog.setAttribute("aria-label", "Review task changes");
    dialog.innerHTML = `
      <header class="dw-head">
        <div class="dw-title">
          <h2>Review changes</h2>
          <p class="dw-sub">Loading recorded changes…</p>
        </div>
        <div class="dw-head-actions">
          <button type="button" class="btn sm dw-copy" title="Copy the diff for the selected file"><i class="ph ph-copy"></i><span>Copy diff</span></button>
          <button type="button" class="btn sm task-review-close">Close</button>
        </div>
      </header>
      <div class="dw-body">
        <aside class="dw-files" role="listbox" aria-label="Changed files" tabindex="0"></aside>
        <section class="dw-detail">
          <div class="dw-filebar">
            <span class="dw-filebar-name">—</span>
            <span class="grow"></span>
            <div class="dw-hunknav" hidden>
              <button type="button" class="dw-hunk-prev" title="Previous change group" aria-label="Previous change group"><i class="ph ph-caret-up"></i></button>
              <span class="dw-hunkpos">0/0</span>
              <button type="button" class="dw-hunk-next" title="Next change group" aria-label="Next change group"><i class="ph ph-caret-down"></i></button>
            </div>
            <button type="button" class="btn sm dw-open" hidden></button>
            <button type="button" class="btn sm dw-undo" hidden>Undo file</button>
          </div>
          <p class="dw-status" role="status">Loading recorded changes…</p>
          <div class="dw-diff" aria-label="Before and after diff"></div>
        </section>
      </div>`;
    document.body.appendChild(dialog);
    dialog.querySelector(".task-review-close").onclick = () => dialog.close();
    dialog.addEventListener("close", () => dialog.remove(), { once: true });
    dialog.showModal();

    const status = dialog.querySelector(".dw-status");
    const sub = dialog.querySelector(".dw-sub");
    const filesRail = dialog.querySelector(".dw-files");
    const diffHost = dialog.querySelector(".dw-diff");
    const nameEl = dialog.querySelector(".dw-filebar-name");
    const openBtn = dialog.querySelector(".dw-open");
    const undoBtn = dialog.querySelector(".dw-undo");
    const hunkNav = dialog.querySelector(".dw-hunknav");
    const hunkPrev = dialog.querySelector(".dw-hunk-prev");
    const hunkNext = dialog.querySelector(".dw-hunk-next");
    const hunkPos = dialog.querySelector(".dw-hunkpos");
    const copyBtn = dialog.querySelector(".dw-copy");

    const evtFiles = Array.isArray(evt?.files) ? evt.files : [];
    let request = 0;
    let selected = 0;
    let fileCount = 0;
    let hunks = [];
    let hunkIdx = -1;
    let currentFile = null;
    let rawDiffText = "";

    const read = async (fileIndex) => {
      const result = await api("/api/task-review", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
        turn_id: evt.turn_id, chat_id: chatId, ...(fileIndex === undefined ? {} : { file_index: fileIndex }),
      }) });
      if (result.error) throw new Error(result.error);
      return result;
    };

    // ---------- unified diff renderer ----------
    // difflib.unified_diff with n=3: two file headers, then @@ hunks with
    // context / -old / +new lines. We parse instead of pattern-guessing so
    // line numbers stay exact even around hunks that share context.
    function renderDiff(lines) {
      diffHost.replaceChildren();
      hunks = []; hunkIdx = -1; rawDiffText = "";
      let oldNo = 0, newNo = 0;
      let oldStart = 0, newStart = 0;
      const fileHead = [];
      for (const line of lines) {
        if (line.startsWith("---") || line.startsWith("+++")) { fileHead.push(line); continue; }
        const hunk = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
        if (hunk) {
          oldStart = parseInt(hunk[1], 10); newStart = parseInt(hunk[3], 10);
          oldNo = oldStart; newNo = newStart;
          const row = document.createElement("div");
          row.className = "dwl dwl-hunk";
          const c = document.createElement("span"); c.className = "dwl-c"; c.textContent = line;
          row.appendChild(c);
          diffHost.appendChild(row);
          hunks.push(row);
          continue;
        }
        const mark = line[0];
        const body = line.slice(1);
        const row = document.createElement("div");
        const g1 = document.createElement("span"); g1.className = "dwl-g dwl-old";
        const g2 = document.createElement("span"); g2.className = "dwl-g dwl-new";
        const m = document.createElement("span"); m.className = "dwl-m";
        const c = document.createElement("span"); c.className = "dwl-c"; c.textContent = body;
        if (mark === "+") { row.className = "dwl dwl-add"; m.textContent = "+"; g1.textContent = ""; g2.textContent = String(newNo++); }
        else if (mark === "-") { row.className = "dwl dwl-del"; m.textContent = "−"; g1.textContent = String(oldNo++); g2.textContent = ""; }
        else { row.className = "dwl dwl-ctx"; m.textContent = " "; g1.textContent = String(oldNo++); g2.textContent = String(newNo++); c.textContent = line; }
        row.append(g1, g2, m, c);
        diffHost.appendChild(row);
      }
      if (fileHead.length) {
        const headRow = document.createElement("div");
        headRow.className = "dwl dwl-filehead";
        const c = document.createElement("span"); c.className = "dwl-c";
        c.textContent = fileHead.map(h => h.replace(/^---\s+|^(\+\+\+)\s+/, "")).join("  →  ");
        headRow.appendChild(c);
        diffHost.prepend(headRow);
      }
      // hunk nav visibility
      hunkNav.hidden = hunks.length < 2;
      if (hunks.length) setHunk(0, "auto");
      else { hunkPos.textContent = "0/0"; }
    }

    function setHunk(index, align) {
      if (!hunks.length) return;
      hunkIdx = Math.max(0, Math.min(hunks.length - 1, index));
      hunks[hunkIdx].scrollIntoView({ block: "start", behavior: align === "smooth" ? "smooth" : "auto" });
      hunkPos.textContent = `${hunkIdx + 1}/${hunks.length}`;
      hunkPrev.disabled = hunkIdx === 0;
      hunkNext.disabled = hunkIdx === hunks.length - 1;
    }
    hunkPrev.addEventListener("click", () => setHunk(hunkIdx - 1, "smooth"));
    hunkNext.addEventListener("click", () => setHunk(hunkIdx + 1, "smooth"));
    diffHost.addEventListener("keydown", (e) => {
      if (e.key === "[" ) { e.preventDefault(); setHunk(hunkIdx - 1, "smooth"); }
      if (e.key === "]" ) { e.preventDefault(); setHunk(hunkIdx + 1, "smooth"); }
    });

    // ---------- file rail ----------
    function renderRail(record) {
      const files = record.files || [];
      fileCount = files.length;
      filesRail.replaceChildren();
      files.forEach((file, index) => {
        const meta = evtFiles[index] || {};
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "dw-file";
        btn.setAttribute("role", "option");
        btn.dataset.index = String(index);
        const tags = [
          file.created ? '<span class="tc-tag tc-new">new</span>' : "",
          file.removed ? '<span class="tc-tag tc-del-tag">deleted</span>' : "",
          meta.undone || evt.undone ? '<span class="tc-tag">undone</span>' : "",
        ].filter(Boolean).join("");
        const stats = file.text_diff === false
          ? "File changed"
          : `<span class="tc-add">+${file.added | 0}</span> <span class="tc-del">−${file.deleted | 0}</span>`;
        btn.innerHTML = `
          <span class="dw-file-row">
            <i class="ph ${file.created ? "ph-file-plus" : file.removed ? "ph-file-x" : "ph-file-text"}" aria-hidden="true"></i>
            <span class="dw-file-name">${esc(file.name)}</span>
            ${tags}
            ${file.changed_since ? '<span class="dw-stale-dot" title="The workspace file changed after this task"></span>' : ""}
          </span>
          <span class="dw-dir">${esc(shortDir(file.path, file.name))}</span>
          <span class="dw-file-foot"><span class="dw-file-stats">${stats}</span></span>`;
        btn.addEventListener("click", () => selectFile(index));
        filesRail.appendChild(btn);
      });
      const totalAdd = files.reduce((s, f) => s + (f.added | 0), 0);
      const totalDel = files.reduce((s, f) => s + (f.deleted | 0), 0);
      sub.innerHTML = `${files.length} file${files.length === 1 ? "" : "s"} · <span class="dw-stat-add">+${totalAdd}</span> <span class="dw-stat-del">−${totalDel}</span> · from this task`;
      syncRailSelection();
    }
    function shortDir(path, name) {
      const dir = String(path || "");
      const cut = dir.slice(0, Math.max(0, dir.length - name.length - 1));
      return cut.replace(/^.*[\\/](?=[^\\/]*$)/, "").slice(0, 60) || dir.slice(0, 60) || "workspace";
    }
    function syncRailSelection() {
      filesRail.querySelectorAll(".dw-file").forEach(btn => {
        const index = Number(btn.dataset.index);
        btn.setAttribute("aria-selected", String(index === selected));
        const meta = evtFiles[index] || {};
        btn.classList.toggle("is-undone", !!meta.undone || !!evt.undone);
      });
    }
    filesRail.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      e.preventDefault();
      selectFile(selected + (e.key === "ArrowDown" ? 1 : -1));
    });

    function selectFile(index) {
      selected = Math.max(0, Math.min(fileCount - 1, index));
      syncRailSelection();
      const id = ++request;
      diffHost.replaceChildren();
      openBtn.hidden = true; undoBtn.hidden = true;
      status.textContent = "Loading recorded changes…";
      nameEl.textContent = "…";
      read(selected).then((file) => {
        if (id !== request || !dialog.isConnected) return;
        renderFile(file);
      }).catch((error) => {
        if (id === request) status.textContent = error.message || "Could not load recorded changes";
      });
    }

    function renderFile(file) {
      currentFile = file;
      nameEl.textContent = file.path || file.name;
      const notes = [file.created ? "Created" : file.removed ? "Deleted" : "Modified"];
      if (file.before_size != null && file.after_size != null) notes.push(`${humanBytes(file.before_size)} → ${humanBytes(file.after_size)}`);
      if (file.changed_since) notes.push("The workspace file changed after this task — this diff is the saved record.");
      if (file.notice) notes.push(file.notice);
      if (file.truncated) notes.push("Large diff: only the first portion is shown.");
      status.textContent = notes.join(" · ");

      const meta = evtFiles[selected] || {};
      const restorable = !evt.undone && !meta.undone && !file.changed_since && file.restorable !== false;
      undoBtn.hidden = !restorable || !evt.turn_id;
      undoBtn.onclick = async () => {
        if (undoBtn.dataset.confirm !== "yes") {
          undoBtn.dataset.confirm = "yes";
          undoBtn.textContent = "Confirm undo";
          status.textContent = `Restore ${file.name} to its state before this task? Later edits are protected.`;
          return;
        }
        undoBtn.disabled = true;
        try {
          const result = await api("/api/undo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ turn_id: evt.turn_id, chat_id: chatId, file_index: selected }) });
          if (!result.ok) throw new Error(result.error || "Could not undo this file");
          if (evt.files?.[selected]) { evt.files[selected].undone = true; evt.files[selected].restorable = false; }
          status.textContent = "File restored. Other files were left unchanged.";
          undoBtn.hidden = true;
          const railBtn = filesRail.querySelector(`.dw-file[data-index="${selected}"]`);
          if (railBtn) {
            const tag = document.createElement("span");
            tag.className = "tc-tag";
            tag.textContent = "undone";
            railBtn.querySelector(".dw-file-row").appendChild(tag);
          }
          syncRailSelection();
          await loadChats();
          renderChatList();
        } catch (error) {
          status.textContent = error.message;
        } finally {
          undoBtn.disabled = false;
          delete undoBtn.dataset.confirm;
          undoBtn.textContent = "Undo file";
        }
      };

      if (Array.isArray(file.diff)) {
        renderDiff(file.diff);
        rawDiffText = (file.diff || []).join("\n");
      } else {
        rawDiffText = "";
        diffHost.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "dw-empty";
        empty.innerHTML = `<i class="ph ph-file-x" aria-hidden="true"></i><div class="h">No text diff for this file</div><div class="s">Binary, oversized, or unreadable content keeps no recorded text.</div>`;
        diffHost.appendChild(empty);
      }

      const action = taskFileAction(file);
      openBtn.hidden = !action;
      openBtn.textContent = action === "Open file" ? "Open current file" : action;
      openBtn.onclick = async () => { try { await openTaskFile(file); dialog.close(); } catch (error) { status.textContent = error.message; } };
    }

    copyBtn.addEventListener("click", async () => {
      try {
        await copyText(rawDiffText || "");
        toast("Diff copied", "ok", 1600);
      } catch { toast("Clipboard blocked", "warn", 2000); }
    });

    try {
      const record = await read();
      if (!dialog.isConnected) return;
      renderRail(record);
      if (record.files.length) await selectFile(0);
      else {
        status.textContent = "No recorded file changes.";
        diffHost.replaceChildren();
        const empty = document.createElement("div");
        empty.className = "dw-empty";
        empty.innerHTML = `<i class="ph ph-file" aria-hidden="true"></i><div class="h">No recorded file changes</div><div class="s">This task only ran commands, reads, or checks.</div>`;
        diffHost.appendChild(empty);
        sub.textContent = "0 files · from this task";
      }
    } catch (error) { status.textContent = error.message || "Could not load recorded changes"; }
  }
  return {
    _commonDirOf, taskFileAction, openTaskFile, renderWorkspaceIdentity, initWorkflowUI,
    controlFeedback, renderTaskHandoff, reviewTaskChanges,
  };
};
