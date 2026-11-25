/* SPDX-License-Identifier: AGPL-3.0-only */
/* GitHub Activity: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["github-activity"] = ({
  $, API_RATE_NOTE, CLOUD_PRICING, _LOGO_PURPLE_D, _LOGO_SLAB_D, api, autoResize, copyText, esc,
  loadChats, openSettings, openSettingsAt, renderCostWidget, renderUsageProfile, selectChat, send,
  state, toast,
}) => {
  let ghRepos = [];
  let ghRepoCursor = "";
  let ghRepoHasNext = false;
  let ghRepoLoading = false;

  function ghRepoSkeletonMarkup(count = 3, extraClass = "") {
    return Array.from({ length: count }, () => `
      <div class="gh-repo-card gh-repo-skeleton ${extraClass}" aria-hidden="true">
        <div class="gh-repo-info">
          <span class="gh-skeleton gh-skeleton-name"></span>
          <span class="gh-skeleton gh-skeleton-star"></span>
        </div>
        <span class="gh-skeleton gh-skeleton-button"></span>
      </div>`).join("");
  }

  function ghActivitySkeletonMarkup() {
    const cells = Array.from({ length: 126 }, () => `<i class="gh-skeleton gh-skeleton-cell"></i>`).join("");
    return `
      <div class="gh-activity-skeleton" aria-label="Loading GitHub activity" aria-busy="true">
        <div class="gh-graph-wrap gh-graph-skeleton">
          <span class="gh-skeleton gh-skeleton-title"></span>
          <div class="gh-skeleton-grid">${cells}</div>
        </div>
        <div class="gh-repos-titlebar">
          <span class="gh-skeleton gh-skeleton-heading"></span>
          <span class="gh-skeleton gh-skeleton-new"></span>
        </div>
        <div class="gh-repos">${ghRepoSkeletonMarkup(3)}</div>
      </div>`;
  }

  function ghActivityOpen() {
    openSettingsAt("github");
  }

  function ghActivityClose() {
    const scrim = $("#github-activity-scrim"), modal = $("#github-activity-modal");
    if (!scrim || !modal) return;
    scrim.classList.remove("open");
    modal.classList.remove("open");
    modal.setAttribute("aria-hidden", "true");
  }

  async function loadGitHubActivity() {
    const body = $("#gh-activity-body");
    if (!body) return;
    body.innerHTML = ghActivitySkeletonMarkup();
    let data;
    try {
      data = await api("/api/github/activity");
    } catch (e) {
      body.innerHTML = `<div class="gh-error"><i class="ph ph-warning-circle"></i><p>could not reach the bridge: ${esc(e.message || e)}</p></div>`;
      return;
    }
    if (!data || !data.connected) {
      const msg = (data && data.error) ? esc(String(data.error)) : "not connected";
      body.innerHTML = `
        <div class="gh-error">
          <i class="ph ph-plugs-connected"></i>
          <p>GitHub is ${msg}. Connect a fine-grained PAT in Settings → GitHub integration.</p>
          <button class="btn accent sm" id="gh-open-settings"><i class="ph ph-gear-six"></i>Open Settings</button>
        </div>`;
      const btn = $("#gh-open-settings");
      if (btn) btn.addEventListener("click", () => { ghActivityClose(); openSettings(); });
      return;
    }
    const loginEl = $("#gh-activity-login");
    if (loginEl) {
      loginEl.innerHTML = data.login
        ? (data.avatar_url
            ? `<img class="gh-login-avatar" src="${esc(data.avatar_url)}" alt="">`
            : `<i class="ph ph-github-logo"></i>`)
          + `<span>${esc(data.login)}</span>`
        : "";
    }
    const total = Number(data.total_contributions || 0);
    const nonzeroDays = (data.weeks || []).reduce(
      (n, w) => n + (w || []).filter(d => (d && d.count > 0)).length, 0);
    body.innerHTML = `
      <div class="gh-graph-wrap">
        <div class="gh-graph-head">
          <span class="gh-graph-title">${total.toLocaleString()} contributions in the last year</span>
        </div>
        <div class="gh-graph" id="gh-graph"></div>
        <div class="gh-legend"><span>Less</span><span class="gh-legend-cells"><i class="gh-cell lv0"></i><i class="gh-cell lv1"></i><i class="gh-cell lv2"></i><i class="gh-cell lv3"></i><i class="gh-cell lv4"></i></span><span>More</span></div>
        ${total === 0 || nonzeroDays < 7 ? `
        <div class="gh-empty-hint"><i class="ph ph-git-commit"></i><span>no counted contributions yet — GitHub only counts commits that reach the <b>default branch</b> (or a merged PR) and are authored by an email <b>verified on your account</b>. unmerged branch work, forks, and unrecognized author emails don't count — add your commit email at github.com/settings/emails and the dots will light up.</span></div>` : ""}
      </div>
      <div class="gh-repos-titlebar">
        <div class="gh-repos-head">Recent repositories</div>
        <button class="btn sm gh-new-project" id="gh-new-project"><i class="ph ph-plus"></i>New project</button>
      </div>
      <div class="gh-repos" id="gh-repos"></div>`;
    renderGhGraph(data.weeks || []);
    ghRepos = Array.isArray(data.repos) ? data.repos : [];
    ghRepoCursor = String((data.repos_page && data.repos_page.cursor) || "");
    ghRepoHasNext = !!(data.repos_page && data.repos_page.has_next);
    renderGhRepos();
    const newProjectBtn = $("#gh-new-project");
    if (newProjectBtn) newProjectBtn.addEventListener("click", openNewProjectModal);
  }

  function ghMonthName(iso) {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleString(undefined, { month: "short" });
  }

  function renderGhGraph(weeks) {
    const wrap = document.getElementById("gh-graph");
    if (!wrap) return;
    if (!weeks.length) {
      wrap.innerHTML = `<span class="hint">no contribution data yet — make some commits!</span>`;
      return;
    }
    // Month labels across the top (label spans the weeks it covers).
    const months = [];
    let lastMonth = "";
    weeks.forEach((w, wi) => {
      const first = w && w[0] && w[0].date;
      if (!first) return;
      const m = first.slice(0, 7);
      if (m !== lastMonth) {
        months.push({ label: ghMonthName(first), count: 1, wi });
        lastMonth = m;
      } else if (months.length) {
        months[months.length - 1].count++;
      }
    });
    let monthRow = "";
    for (const ml of months) {
      monthRow += `<span class="gh-month" style="grid-column:${ml.wi + 1} / span ${ml.count}">${ml.label}</span>`;
    }
    // Day labels sit at fixed rows: Mon(1), Wed(3), Fri(5) like GitHub's grid.
    const days = [
      { label: "Mon", row: 1 }, { label: "Wed", row: 3 }, { label: "Fri", row: 5 },
    ];
    let dayHtml = "";
    for (let r = 0; r < 7; r++) {
      const d = days.find(x => x.row === r);
      dayHtml += `<span class="gh-day" style="grid-row:${r + 1}">${d ? d.label : ""}</span>`;
    }
    // Cells flow column-first over a 7-row grid; the first partial week is
    // padded at the top so columns align exactly like GitHub's calendar.
    const cells = [];
    weeks.forEach((w, wi) => {
      if (wi === 0 && w.length < 7) {
        for (let p = 0; p < 7 - w.length; p++) cells.push(`<div class="gh-cell gh-cell-empty"></div>`);
      }
      for (const d of w) {
        cells.push(`<div class="gh-cell lv${d.level}" data-date="${esc(d.date)}" title="${esc(d.date)} — ${d.count} contribution${d.count === 1 ? "" : "s"}" style="--gd:${(Math.random() * 650).toFixed(0)}ms"></div>`);
      }
    });
    wrap.innerHTML = `
      <div class="gh-months" style="--gh-week-count:${weeks.length}">${monthRow}</div>
      <div class="gh-grid">
        <div class="gh-days">${dayHtml}</div>
        <div class="gh-cells">${cells.join("")}</div>
      </div>`;
    // Brief "glitter" entrance: every dot starts empty, flashes the theme accent
    // once (staggered via --gd), then fades to its static intensity. The safety
    // sweep below settles any dot whose animationend never fired (hidden tab,
    // reduced motion, canceled animation).
    const all = wrap.querySelectorAll(".gh-cell:not(.gh-cell-empty)");
    all.forEach(c => c.classList.add("gh-glitter"));
    let done = 0;
    all.forEach(c => {
      c.addEventListener("animationend", () => {
        c.classList.remove("gh-glitter");
        if (++done >= all.length) wrap.classList.add("gh-glitter-done");
      }, { once: true });
    });
    setTimeout(() => {
      all.forEach(c => c.classList.remove("gh-glitter"));
      wrap.classList.add("gh-glitter-done");
    }, 1500);
  }

  function renderGhRepos() {
    const wrap = document.getElementById("gh-repos");
    if (!wrap) return;
    const cards = ghRepos.length ? ghRepos.map(r => `
      <div class="gh-repo-card">
        <div class="gh-repo-info">
          <span class="gh-repo-name" title="${esc(r.name)}">${esc(r.name)}</span>
          <span class="gh-repo-stars" title="stargazers"><i class="ph ph-star"></i>${Number(r.stars || 0).toLocaleString()}</span>
        </div>
        <button class="btn sm accent" data-gh-work="${esc(r.name)}" data-gh-owner="${esc(r.owner || "")}" title="Create a new branch on GitHub and check it out in the workspace"><i class="ph ph-git-branch"></i>Work on this repo</button>
      </div>`).join("") : `<div class="gh-repo-empty"><i class="ph ph-folder-simple"></i><span>No repositories available to this token yet.</span></div>`;
    const more = ghRepoHasNext ? `
      <div class="gh-load-more-wrap">
        <button class="btn sm" id="gh-load-more" ${ghRepoLoading ? "disabled" : ""}>Load more</button>
      </div>` : "";
    wrap.innerHTML = cards + more;
    wrap.querySelectorAll("[data-gh-work]").forEach(btn => {
      btn.addEventListener("click", () => workOnRepo(btn.dataset.ghOwner, btn.dataset.ghWork, btn));
    });
    const loadMore = document.getElementById("gh-load-more");
    if (loadMore) loadMore.addEventListener("click", loadMoreGhRepos);
  }

  async function loadMoreGhRepos() {
    if (ghRepoLoading || !ghRepoHasNext) return;
    ghRepoLoading = true;
    const wrap = document.getElementById("gh-repos");
    const controls = wrap && wrap.querySelector(".gh-load-more-wrap");
    if (controls) controls.outerHTML = `<div class="gh-more-skeletons" aria-label="Loading more repositories" aria-busy="true">${ghRepoSkeletonMarkup(3)}</div>`;
    try {
      const data = await api(`/api/github/repos?limit=12&after=${encodeURIComponent(ghRepoCursor)}`);
      if (!data || !data.connected) throw new Error((data && data.error) || "GitHub could not load more repositories");
      const seen = new Set(ghRepos.map(r => `${r.owner}/${r.name}`.toLowerCase()));
      for (const repo of data.repos || []) {
        const key = `${repo.owner}/${repo.name}`.toLowerCase();
        if (!seen.has(key)) { seen.add(key); ghRepos.push(repo); }
      }
      ghRepoCursor = String((data.page_info && data.page_info.cursor) || "");
      ghRepoHasNext = !!(data.page_info && data.page_info.has_next);
    } catch (e) {
      toast("could not load more repositories: " + (e.message || e), "err", 4200);
    } finally {
      ghRepoLoading = false;
      renderGhRepos();
    }
  }

  function openNewProjectModal() {
    const roots = ((state.workspace && state.workspace.folders) || []).filter(Boolean);
    const scrim = document.createElement("div");
    scrim.className = "modal-scrim gh-project-scrim";
    const modal = document.createElement("aside");
    modal.className = "modal gh-project-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-labelledby", "gh-project-title");
    const rootOptions = roots.map(root => `<option value="${esc(root)}">${esc(root)}</option>`).join("");
    modal.innerHTML = `
      <div class="modal-head">
        <i class="ph ph-folder-simple-plus"></i>
        <h3 id="gh-project-title">New project</h3>
        <button class="iconbtn" data-project-cancel aria-label="Cancel"><i class="ph ph-x"></i></button>
      </div>
      <form class="modal-body gh-project-form" id="gh-project-form">
        <div class="gh-project-fields">
          <label class="gh-project-field">
            <span>Project name</span>
            <input class="prompt-modal-input" id="gh-project-name" required maxlength="100" placeholder="my-new-project" autocomplete="off" spellcheck="false">
          </label>
          <label class="gh-project-field">
            <span>What should the model build? <small>optional</small></span>
            <textarea class="prompt-modal-input gh-project-brief" id="gh-project-brief" rows="4" maxlength="4000" placeholder="A small dashboard that…"></textarea>
            <small class="gh-project-help">The new project session starts with this brief.</small>
          </label>
          <label class="gh-project-field">
            <span>Save in</span>
            <select class="prompt-modal-input" id="gh-project-root" ${roots.length ? "" : "disabled"}>${rootOptions || `<option>No workspace folder configured</option>`}</select>
          </label>
          <div class="gh-project-publish-row">
            <label class="gh-project-check"><input type="checkbox" id="gh-project-publish" checked><span>Publish an empty repository to GitHub</span></label>
            <select class="prompt-modal-input gh-project-visibility" id="gh-project-visibility" aria-label="Repository visibility">
              <option value="private">Private</option>
              <option value="public">Public</option>
            </select>
          </div>
          <div class="gh-project-permission"><i class="ph ph-info"></i><span>Publishing needs <b>Administration: Read and write</b> on the fine-grained token. Local creation still works without it.</span></div>
          <div class="prompt-modal-err" id="gh-project-error"></div>
          <div class="confirm-actions">
            <button class="btn" type="button" data-project-cancel>Cancel</button>
            <button class="btn accent" type="submit" ${roots.length ? "" : "disabled"}><i class="ph ph-plus"></i>Create project</button>
          </div>
        </div>
        <div class="gh-project-progress" hidden aria-live="polite" aria-busy="true">
          <div class="gh-project-progress-icon"><span class="gh-skeleton gh-skeleton-project-square"></span></div>
          <strong>Creating project</strong>
          <span class="gh-project-progress-copy">Preparing the folder and Git repository…</span>
          <div class="gh-project-progress-lines">
            <span class="gh-skeleton"></span><span class="gh-skeleton"></span><span class="gh-skeleton"></span>
          </div>
        </div>
      </form>`;
    document.body.appendChild(scrim);
    document.body.appendChild(modal);
    requestAnimationFrame(() => { scrim.classList.add("open"); modal.classList.add("open"); });

    const form = modal.querySelector("#gh-project-form");
    const nameInput = modal.querySelector("#gh-project-name");
    const briefInput = modal.querySelector("#gh-project-brief");
    const rootInput = modal.querySelector("#gh-project-root");
    const publishInput = modal.querySelector("#gh-project-publish");
    const visibilityInput = modal.querySelector("#gh-project-visibility");
    const errorEl = modal.querySelector("#gh-project-error");
    let busy = false;
    let closed = false;
    let createdProject = null;

    const finish = () => {
      if (closed || busy) return;
      closed = true;
      document.removeEventListener("keydown", onKey);
      scrim.classList.remove("open");
      modal.classList.remove("open");
      setTimeout(() => { scrim.remove(); modal.remove(); }, 220);
    };
    const onKey = (event) => {
      if (event.key === "Escape" && !busy) { event.preventDefault(); finish(); }
    };
    const syncPublish = () => {
      visibilityInput.disabled = !publishInput.checked;
      visibilityInput.closest(".gh-project-publish-row").classList.toggle("is-local", !publishInput.checked);
    };
    publishInput.addEventListener("change", syncPublish);
    syncPublish();
    document.addEventListener("keydown", onKey);
    scrim.addEventListener("click", finish);
    modal.querySelectorAll("[data-project-cancel]").forEach(button => button.addEventListener("click", finish));
    setTimeout(() => nameInput && nameInput.focus(), 120);

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (busy) return;
      const name = (nameInput.value || "").trim();
      if (!/^[A-Za-z0-9_.-]{1,100}$/.test(name) || name === "." || name === ".." || name.endsWith(".")) {
        errorEl.textContent = "Use letters, digits, dots, underscores and hyphens. Maximum 100 characters.";
        errorEl.style.display = "block";
        nameInput.focus();
        return;
      }
      busy = true;
      errorEl.style.display = "none";
      modal.querySelector(".gh-project-fields").hidden = true;
      modal.querySelector(".gh-project-progress").hidden = false;
      try {
        const result = await api("/api/github/project", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name,
            brief: briefInput.value.trim(),
            workspace_root: rootInput.value,
            publish: publishInput.checked,
            private: visibilityInput.value !== "public",
          }),
        });
        if (!result || !result.ok) throw new Error((result && result.error) || "project creation failed");
        createdProject = result;
        busy = false;
        finish();
        ghActivityClose();
        await openProjectSession(result, briefInput.value.trim());
        if (result.publish_error) {
          toast(result.publish_error, "warn", 6500);
        } else {
          toast(result.published ? `Project ${name} created and published.` : `Local project ${name} created.`, "ok", 3800);
        }
      } catch (error) {
        if (closed && createdProject) {
          toast(`Project created at ${createdProject.path}, but its session could not open: ${error.message || error}`, "err", 6500);
          return;
        }
        busy = false;
        modal.querySelector(".gh-project-progress").hidden = true;
        modal.querySelector(".gh-project-fields").hidden = false;
        errorEl.textContent = error.message || String(error);
        errorEl.style.display = "block";
        nameInput.focus();
      }
    });
  }

  async function openProjectSession(project, brief) {
    const c = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: project.name,
        origin: "project",
        project_workspace: {
          name: project.name,
          path: project.path,
          branch: project.branch || "main",
          brief,
          published: !!project.published,
          owner: project.owner || "",
        },
      }),
    });
    if (!c || !c.id) throw new Error("could not create the project session");
    const publication = project.published
      ? (project.remote_ready
          ? `Published ${project.owner || "GitHub"}/${project.repo || project.name} and connected it as origin.`
          : `Published ${project.owner || "GitHub"}/${project.repo || project.name}; the local origin still needs to be connected.`)
      : "Created as a local Git repository.";
    await api("/api/chat/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: c.id, text: `Created project \`${project.name}\` at ${project.path}. ${publication}` }),
    }).catch(() => {});
    await loadChats();
    await selectChat(c.id);
    const ta = $("#composer-input");
    if (brief && state.settings.model) {
      await send({ prompt: brief });
    } else if (ta) {
      ta.value = brief || "";
      autoResize(ta);
    }
  }

  // Promise-based input dialog (the branch-name prompt for "Work on this repo"),
  // modeled on confirmModal. Resolves with the trimmed string, or null on
  // cancel / Escape / scrim click.
  function promptModal(opts = {}) {
    const {
      title = "Enter a value",
      message = "",
      placeholder = "",
      confirmText = "OK",
      cancelText = "Cancel",
      validate = null,
    } = opts;
    return new Promise((resolve) => {
      const scrim = document.createElement("div");
      scrim.className = "modal-scrim";
      const modal = document.createElement("aside");
      modal.className = "modal prompt-modal";
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      modal.innerHTML = `
        <div class="modal-head">
          <i class="ph ph-git-branch"></i>
          <h3>${esc(title)}</h3>
          <button class="iconbtn" data-act="cancel" aria-label="Cancel"><i class="ph ph-x"></i></button>
        </div>
        <div class="modal-body">
          ${message ? `<p>${esc(message)}</p>` : ""}
          <input type="text" class="prompt-modal-input" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false">
          <div class="prompt-modal-err" data-err></div>
          <div class="confirm-actions">
            <button class="btn" data-act="cancel">${esc(cancelText)}</button>
            <button class="btn accent" data-act="confirm">${esc(confirmText)}</button>
          </div>
        </div>`;
      document.body.appendChild(scrim);
      document.body.appendChild(modal);
      requestAnimationFrame(() => { scrim.classList.add("open"); modal.classList.add("open"); });
      const input = modal.querySelector(".prompt-modal-input");
      const errEl = modal.querySelector("[data-err]");
      const confirmBtn = modal.querySelector('[data-act="confirm"]');
      setTimeout(() => { if (input) input.focus(); }, 120);
      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKey);
        scrim.classList.remove("open");
        modal.classList.remove("open");
        setTimeout(() => { scrim.remove(); modal.remove(); }, 220);
        resolve(result);
      };
      const submit = () => {
        const v = (input && input.value || "").trim();
        if (validate) {
          const errMsg = validate(v);
          if (errMsg) {
            if (errEl) { errEl.textContent = errMsg; errEl.style.display = "block"; }
            if (input) input.focus();
            return;
          }
        }
        finish(v || null);
      };
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); finish(null); }
        else if (e.key === "Enter") { e.preventDefault(); submit(); }
      };
      document.addEventListener("keydown", onKey);
      scrim.addEventListener("click", () => finish(null));
      modal.querySelectorAll('[data-act="cancel"]').forEach(b => b.addEventListener("click", () => finish(null)));
      confirmBtn.addEventListener("click", submit);
    });
  }

  async function workOnRepo(owner, repo, btn) {
    if (!owner || !repo) return;
    const name = await promptModal({
      title: `Work on ${repo}`,
      message: `Create a new branch on ${owner}/${repo}, then check it out in the workspace.`,
      placeholder: "branch name, e.g. feature/my-change",
      confirmText: "Create & checkout",
      validate: (v) => /^[A-Za-z0-9._/-]{1,256}$/.test(v)
        ? "" : "use letters, digits, dots, underscores, slashes and hyphens (max 256 chars)",
    });
    if (!name) return;
    const oldHtml = btn ? btn.innerHTML : "";
    if (btn) {
      btn.disabled = true;
      btn.classList.add("gh-action-loading");
      btn.innerHTML = `<span class="gh-skeleton gh-action-skeleton" aria-label="Creating branch"></span>`;
    }
    try {
      const r = await api("/api/github/branch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, repo, branch: name }),
      });
      if (!r || !r.ok) throw new Error((r && r.error) || "branch creation failed");
      // checkout worked — get the dashboard out of the way so the new
      // branch session is visible immediately (kept open on failure).
      ghActivityClose();
      await openBranchSession(r, name);
      toast(`branch \`${name}\` created and checked out${r.cloned ? " — cloned into the workspace" : ""} — new session opened`, "ok", 3500);
    } catch (e) {
      toast("branch creation failed: " + (e.message || e), "err", 4500);
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove("gh-action-loading"); btn.innerHTML = oldHtml; }
    }
  }

  // Open a fresh session for the branch work with the branch event as its
  // first note; the chat row then shows a git-branch glyph instead of the glow
  // dot.
  async function openBranchSession(r, branch) {
    const text = `Created branch \`${branch}\` on ${r.owner}/${r.repo} (from \`${r.default_branch || "default"}\`) and checked it out${r.cloned ? " — repo was cloned into the workspace" : ""} at ${r.repo_dir || ""}.`;
    const c = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: `work on ${r.repo}`,
        origin: "github",
        github_worktree: {
          owner: r.owner,
          repo: r.repo,
          branch,
          path: r.repo_dir,
        },
      }),
    });
    if (!c || !c.id) throw new Error("could not create the session");
    // Persist the note first so it is already there when we switch to the
    // session (the broadcast is ignored while the old chat is still current).
    await api("/api/chat/note", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: c.id, text }),
    }).catch(() => {});
    await loadChats();
    selectChat(c.id);
    const ta = $("#composer-input");
    if (ta) { ta.value = ""; autoResize(ta); }
  }

  let usageMarkFrame = 0;

  function usageCompactNumber(value) {
    const n = Number(value) || 0;
    if (n >= 1e9) return `${(n / 1e9).toFixed(n >= 1e10 ? 0 : 1)}B`;
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e4 ? 0 : 1)}K`;
    return n.toLocaleString();
  }

  function usageDuration(ms) {
    const seconds = Math.max(0, Math.round((Number(ms) || 0) / 1000));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    if (hours) return `${hours}h ${minutes}m`;
    if (minutes) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
  }

  function usageModelName(name) {
    return String(name || "Unknown model").replace(/\.gguf$/i, "");
  }

  function drawUsageMark(animate = true) {
    const canvas = $("#usage-mark");
    if (!canvas || typeof Path2D === "undefined") return;
    cancelAnimationFrame(usageMarkFrame);
    const ctx = canvas.getContext("2d");
    const width = canvas.width, height = canvas.height;
    const mainPath = new Path2D(_LOGO_SLAB_D);
    const accentPath = new Path2D(_LOGO_PURPLE_D);
    const scale = 1.06, ox = (width - 348 * scale) / 2, oy = 28;
    const targets = [];
    for (let y = 8; y <= 276; y += 9) {
      for (let x = 8; x <= 340; x += 9) {
        const accent = ctx.isPointInPath(accentPath, x, y);
        if (accent || ctx.isPointInPath(mainPath, x, y)) {
          targets.push({ x: ox + x * scale, y: oy + y * scale, accent });
        }
      }
    }
    const starts = targets.map((_, index) => {
      const columns = 29;
      const mixed = (index * 83) % targets.length;
      return {
        x: 28 + (mixed % columns) * ((width - 56) / (columns - 1)),
        y: 28 + Math.floor(mixed / columns) * 14,
      };
    });
    const style = getComputedStyle(document.documentElement);
    const main = style.getPropertyValue("--fg").trim() || "#e9e9ed";
    const accent = style.getPropertyValue("--accent").trim() || "#7867ff";
    const muted = style.getPropertyValue("--border").trim() || "#34343b";
    const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const started = performance.now();
    const duration = 1600;
    const paint = now => {
      const raw = animate && !reduced ? Math.min(1, (now - started) / duration) : 1;
      const ease = 1 - Math.pow(1 - raw, 4);
      ctx.clearRect(0, 0, width, height);
      targets.forEach((target, index) => {
        const start = starts[index];
        const local = Math.max(0, Math.min(1, (ease - (index % 17) * 0.0025) / 0.96));
        const x = start.x + (target.x - start.x) * local;
        const y = start.y + (target.y - start.y) * local;
        ctx.globalAlpha = 0.18 + local * 0.82;
        ctx.fillStyle = local < 0.7 ? muted : (target.accent ? accent : main);
        ctx.beginPath();
        ctx.arc(x, y, 2.45, 0, Math.PI * 2);
        ctx.fill();
      });
      ctx.globalAlpha = 1;
      if (raw < 1) usageMarkFrame = requestAnimationFrame(paint);
    };
    usageMarkFrame = requestAnimationFrame(paint);
  }

  let usageHeatmapMetric = "turns";

  function renderUsageHeatmap(activity) {
    const days = Array.isArray(activity?.days) ? activity.days : [];
    if (!days.length) return '<section class="usage-panel"><p class="usage-empty">Activity history is unavailable. Restart the bridge to load the calendar.</p></section>';
    const first = new Date(`${days[0].day}T12:00:00`);
    const padding = (first.getDay() + 6) % 7;
    const columns = Math.ceil((padding + days.length) / 7);
    const peak = Math.max(1, ...days.map(day => Number(day[usageHeatmapMetric]) || 0));
    const activeDays = days.filter(day => day.turns > 0).length;
    let streak = 0;
    let end = days.length - 1;
    if (!days[end].turns) end--;
    for (let i = end; i >= 0 && days[i].turns > 0; i--) streak++;
    const months = [];
    let lastMonth = "";
    const cells = days.map((day, index) => {
      const date = new Date(`${day.day}T12:00:00`);
      const col = Math.floor((padding + index) / 7) + 1;
      const month = `${date.getFullYear()}-${date.getMonth()}`;
      if (month !== lastMonth) {
        if (!months.length || col - months.at(-1).col >= 3) months.push({ col, text: date.toLocaleDateString(undefined, { month: "short" }) });
        lastMonth = month;
      }
      const value = Number(day[usageHeatmapMetric]) || 0;
      const level = value ? Math.max(1, Math.ceil(Math.sqrt(value / peak) * 4)) : 0;
      const detail = `${date.toLocaleDateString(undefined, { dateStyle: "medium" })} · ${Number(day.turns).toLocaleString()} turns · ${usageCompactNumber(day.tokens)} tokens · ${usageDuration(day.elapsed_ms)}`;
      return `<button type="button" class="usage-day" data-level="${level}" data-day-index="${index}" data-detail="${esc(detail)}" style="grid-column:${col};grid-row:${(padding + index) % 7 + 1}" tabindex="${index === days.length - 1 ? 0 : -1}" aria-label="${esc(detail)}" title="${esc(detail)}"></button>`;
    }).join("")
      // Outline the slots before the first day and after today, so the
      // partial first and last weeks don't leave the rows ragged.
      + Array.from({ length: columns * 7 }, (_, slot) => slot).filter(slot => slot < padding || slot >= padding + days.length)
        .map(slot => `<i class="usage-day is-outside" aria-hidden="true" style="grid-column:${Math.floor(slot / 7) + 1};grid-row:${slot % 7 + 1}"></i>`).join("");
    return `<section class="usage-panel usage-calendar-panel">
      <div class="usage-section-head"><div><span class="usage-eyebrow">Past year</span><h2>Your activity</h2></div><div class="usage-calendar-switch" role="group" aria-label="Activity measure"><button type="button" data-activity-metric="turns" aria-pressed="${usageHeatmapMetric === "turns"}">Turns</button><button type="button" data-activity-metric="tokens" aria-pressed="${usageHeatmapMetric === "tokens"}">Tokens</button></div></div>
      <div class="usage-calendar-summary"><span><strong>${activeDays}</strong> active days</span><span><strong>${streak}</strong> day current streak</span><span>${usageCompactNumber(days.reduce((sum, day) => sum + day.turns, 0))} recorded turns</span></div>
      <div class="usage-calendar-scroll"><div class="usage-calendar" style="--weeks:${columns}"><div class="usage-months">${months.map(month => `<span style="grid-column:${month.col}/span 3">${esc(month.text)}</span>`).join("")}</div><div class="usage-weekdays" aria-hidden="true"><span>Mon</span><span>Wed</span><span>Fri</span></div><div class="usage-days" role="group" aria-label="Daily activity. Use arrow keys to explore days.">${cells}</div></div></div>
      <div class="usage-calendar-footer"><span id="usage-day-detail" aria-live="polite">Select a day to see its activity.</span><div class="usage-calendar-legend" aria-label="Activity intensity, less to more"><span>Less</span>${[0, 1, 2, 3, 4].map(level => `<i class="usage-day" data-level="${level}"></i>`).join("")}<span>More</span></div></div>
      <p class="usage-footnote">Recorded model turns in this computer’s local time. Blank days have no recorded activity.</p>
    </section>`;
  }

  function wireUsageHeatmap(data) {
    const panel = document.querySelector(".usage-calendar-panel");
    if (!panel) return;
    panel.querySelectorAll("[data-activity-metric]").forEach(button => button.addEventListener("click", () => {
      usageHeatmapMetric = button.dataset.activityMetric;
      renderUsageStats(data);
      document.querySelector(`[data-activity-metric="${usageHeatmapMetric}"]`)?.focus();
    }));
    const cells = [...panel.querySelectorAll("button.usage-day")];
    function describe(cell) {
      panel.querySelector("#usage-day-detail").textContent = cell.dataset.detail;
      cells.forEach(item => { item.tabIndex = item === cell ? 0 : -1; item.classList.toggle("is-selected", item === cell); });
    }
    const days = panel.querySelector(".usage-days");
    days?.addEventListener("pointerleave", () => {
      panel.querySelector("#usage-day-detail").textContent = (days.querySelector(".is-selected") || cells.at(-1))?.dataset.detail || "";
    });
    for (const cell of cells) {
      cell.addEventListener("pointerenter", () => { panel.querySelector("#usage-day-detail").textContent = cell.dataset.detail; });
      cell.addEventListener("focus", () => describe(cell));
      cell.addEventListener("click", () => describe(cell));
      cell.addEventListener("keydown", event => {
        const index = Number(cell.dataset.dayIndex);
        const delta = { ArrowLeft: -7, ArrowRight: 7, ArrowUp: -1, ArrowDown: 1 }[event.key];
        if (delta === undefined && event.key !== "Home" && event.key !== "End") return;
        event.preventDefault();
        const next = event.key === "Home" ? 0 : event.key === "End" ? cells.length - 1 : Math.max(0, Math.min(cells.length - 1, index + delta));
        cells[next]?.focus();
      });
    }
    if (cells.length) panel.querySelector("#usage-day-detail").textContent = cells.at(-1).dataset.detail;
    const scroller = panel.querySelector(".usage-calendar-scroll");
    if (scroller) scroller.scrollLeft = scroller.scrollWidth;
  }

  function renderUsageStats(data) {
    const content = $("#usage-content");
    if (!content) return;
    state._usageData = data;
    renderUsageProfile();
    const totals = data.totals || {};
    const compression = data.compression || {};
    const repos = data.repos || {};
    const interventions = data.interventions || {};
    const longest = data.longest;
    const models = Array.isArray(data.models) ? data.models : [];
    const ranked = models.filter(model => model.ranked);
    const best = ranked[0] || null;
    const attention = ranked.length > 1 ? ranked[ranked.length - 1] : null;
    const provider = CLOUD_PRICING[state.costProvider] || CLOUD_PRICING.openai;
    const inputTokens = Number(totals.input_tokens ?? data.savings?.tok_in) || 0;
    const outputTokens = Number(totals.output_tokens ?? data.savings?.tok_out) || 0;
    const tokenTotal = document.getElementById("usage-token-total");
    if (tokenTotal) tokenTotal.textContent = usageCompactNumber(inputTokens + outputTokens);
    const inputCost = inputTokens / 1e6 * provider.input;
    const outputCost = outputTokens / 1e6 * provider.output;
    const money = value => `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
    const since = totals.since ? new Date(totals.since * 1000) : null;
    $("#usage-range").textContent = since
      ? `Recorded since ${since.toLocaleDateString(undefined, { month: "long", year: "numeric" })}`
      : "Local counters";
    $("#usage-privacy").textContent = data.privacy || "Aggregate counters stay on this machine.";
    let rankedPosition = 0;
    const modelRows = models.slice(0, 10).map(model => {
      const score = Math.round((Number(model.reliability) || 0) * 100);
      const rank = model.ranked ? String(++rankedPosition).padStart(2, "0") : "--";
      const tag = best && model.model === best.model ? '<span class="usage-model-tag best"><svg class="usage-model-star" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.6l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.5l-5.9 3.1 1.2-6.5L2.5 9.5l6.6-.9z"/></svg>Best observed</span>'
        : attention && model.model === attention.model ? '<span class="usage-model-tag watch">Watch</span>' : "";
      return `<div class="usage-model-row">
        <span class="usage-model-rank">${rank}</span>
        <div class="usage-model-main"><strong title="${esc(model.model)}">${esc(usageModelName(model.model))}</strong><span>${model.turns.toLocaleString()} ${model.turns === 1 ? "turn" : "turns"} · ${esc(model.confidence.replace("_", " "))} confidence</span></div>
        <span class="usage-model-tag-slot">${tag}</span>
        <div class="usage-model-meter"><span style="width:${score}%"></span></div>
        <strong class="usage-model-score">${score}</strong>
        <dl><div><dt>Finished</dt><dd>${Math.round(model.completion_rate * 100)}%</dd></div><div><dt>Tool success</dt><dd>${model.tool_success == null ? "n/a" : `${Math.round(model.tool_success * 100)}%`}</dd></div><div><dt>Output speed</dt><dd>${Number(model.output_tok_s || 0).toFixed(1)} tok/s</dd></div></dl>
      </div>`;
    }).join("");
    const providerOptions = Object.entries(CLOUD_PRICING).map(([key, item]) => `<option value="${key}"${key === state.costProvider ? " selected" : ""}>${esc(item.label)}</option>`).join("");
    const retryRecoveries = Number(interventions.context_retry || 0)
      + Number(interventions.server_retry || 0)
      + Number(interventions.empty_reply_retry || 0);
    const errors = data.errors || {};
    const errCounts = errors.counts || {};
    const errTotal = Number(errors.total || 0);
    const errTime = (t) => {
      try {
        const d = new Date(Number(t) * 1000);
        return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;
      } catch { return ""; }
    };
    const errRows = (Array.isArray(errors.recent) ? errors.recent : []).map((entry) => {
      const n = Number(entry.count || 1);
      return `<div><span>${esc(errTime(entry.t))} · ${esc(entry.type || "Error")} · ${esc(entry.location || "unknown")}${n > 1 ? ` · ×${n}` : ""}</span><button type="button" class="btn sm" data-error-copy="${esc(entry.id || "")}">Copy</button></div>`;
    }).join("");
    content.innerHTML = `
      <section class="usage-kpis">
        <article><i class="ph ph-chat-centered-text"></i><span>Model turns</span><strong>${usageCompactNumber(totals.turns)}</strong><small>${usageCompactNumber(inputTokens + outputTokens)} tokens processed</small></article>
        <article><i class="ph ph-timer"></i><span>Autonomous time</span><strong>${usageDuration(totals.elapsed_ms)}</strong><small>Across recorded model turns</small></article>
        <article><i class="ph ph-git-branch"></i><span>Repos worked on</span><strong>${Number(repos.unique || 0).toLocaleString()}</strong><small>${Number(repos.sessions || 0).toLocaleString()} branch sessions</small></article>
        <article><i class="ph ph-arrows-in-line-vertical"></i><span>Successful compactions</span><strong>${Number(compression.successful || 0).toLocaleString()}</strong><small>${usageCompactNumber(compression.folded_tokens)} tokens folded</small></article>
      </section>

      ${renderUsageHeatmap(data.activity)}

      <section class="usage-two-col">
        <article class="usage-panel usage-saving-panel">
          <div class="usage-section-head"><div><span class="usage-eyebrow">Cloud equivalent</span><h2>Estimated savings</h2></div><select id="usage-provider" aria-label="Cloud price comparison">${providerOptions}</select></div>
          <div class="usage-money">${money(inputCost + outputCost)}<sup aria-hidden="true">*</sup></div>
          <div class="usage-breakdown"><div><span>Input · ${usageCompactNumber(inputTokens)}</span><strong>${money(inputCost)}</strong></div><div><span>Output · ${usageCompactNumber(outputTokens)}</span><strong>${money(outputCost)}</strong></div><div><span>Compared with</span><strong>${esc(provider.label)}</strong></div></div>
          <p class="usage-rate-note"><span aria-hidden="true">*</span> ${esc(API_RATE_NOTE)}</p>
        </article>
        <article class="usage-panel usage-longest-panel">
          <div class="usage-section-head"><div><span class="usage-eyebrow">Longest recorded turn</span><h2>${longest ? usageDuration(longest.elapsed_ms) : "No data yet"}</h2></div><i class="ph ph-path"></i></div>
          <strong>${esc(longest?.title || "Run a task to start measuring")}</strong>
          <span>${longest ? `${esc(usageModelName(longest.model))} · ${esc(longest.day || "date unavailable")}` : ""}</span>
          ${longest && !longest.chat_available ? '<small>The original chat was deleted, so its title is no longer available.</small>' : ""}
        </article>
      </section>

      <section class="usage-panel usage-model-panel">
        <div class="usage-section-head"><div><span class="usage-eyebrow">On this computer</span><h2>Model performance</h2></div><span class="usage-method">Score = tool success + finished turns + speed + track record</span></div>
        <div class="usage-model-list">${modelRows || '<div class="usage-empty">No recorded usage for models currently on this computer.</div>'}</div>
        <p class="usage-footnote">Only models with a verified local file appear here. Ranking starts after five turns; short runs are weighted toward the fleet average so proven workhorses outrank lucky streaks. Historical activity remains in your totals.</p>
      </section>

      <section class="usage-panel usage-interventions-panel">
        <div class="usage-section-head"><div><span class="usage-eyebrow">Harness self-corrections</span><h2>${Number(interventions.total || 0).toLocaleString()} quiet interventions</h2></div><i class="ph ph-wrench"></i></div>
        <div class="usage-breakdown">
          <div><span>Automatic retries</span><strong>${retryRecoveries.toLocaleString()}</strong></div>
          <div><span>Tool-call repairs</span><strong>${Number(interventions.tool_call_repair || 0).toLocaleString()}</strong></div>
          <div><span>Reasoning guards</span><strong>${Number(interventions.reasoning_guard || 0).toLocaleString()}</strong></div>
          <div><span>Plan continuations</span><strong>${Number(interventions.auto_continue || 0).toLocaleString()}</strong></div>
          <div><span>Loop guards</span><strong>${Number(interventions.loop_guard || 0).toLocaleString()}</strong></div>
        </div>
        <p class="usage-footnote">These recoveries stay out of the notification feed. Failures that need your attention still surface normally.</p>
      </section>

      <section class="usage-panel usage-errors-panel">
        <div class="usage-section-head"><div><span class="usage-eyebrow">Local only · never uploaded</span><h2>${errTotal ? `${errTotal.toLocaleString()} app errors` : "No errors recorded"}</h2></div><i class="ph ph-warning-circle"></i></div>
        <div class="usage-breakdown">
          <div><span>Bridge errors</span><strong>${Number(errCounts.bridge || 0).toLocaleString()}</strong></div>
          <div><span>Interface errors</span><strong>${Number(errCounts.interface || 0).toLocaleString()}</strong></div>
          <div><span>Backend faults</span><strong>${Number(errCounts.backend || 0).toLocaleString()}</strong></div>
        </div>
        ${errRows ? `<div class="usage-breakdown">${errRows}</div>` : '<div class="usage-empty">No entries yet.</div>'}
        <p class="usage-footnote">Only error types and app locations are kept — never prompts, commands, or file contents. Nothing leaves this machine. <button type="button" class="btn sm" id="usage-errors-clear">Clear history</button></p>
      </section>

      <section class="usage-two-col">
        <article class="usage-panel usage-compression-panel">
          <div class="usage-section-head"><div><span class="usage-eyebrow">Context compression</span><h2>${usageCompactNumber(compression.folded_messages)} messages folded</h2></div><i class="ph ph-arrows-in-line-vertical"></i></div>
          <div class="usage-breakdown"><div><span>Successful folds</span><strong>${Number(compression.successful || 0).toLocaleString()}</strong></div><div><span>Skipped safely</span><strong>${Number(compression.skipped || 0).toLocaleString()}</strong></div><div><span>Failed</span><strong>${Number(compression.failed || 0).toLocaleString()}</strong></div></div>
        </article>
        <article class="usage-panel usage-repos-panel">
          <div class="usage-section-head"><div><span class="usage-eyebrow">GitHub work</span><h2>${Number(repos.unique || 0).toLocaleString()} repositories</h2></div><i class="ph ph-git-branch"></i></div>
          <div class="usage-repo-list">${(repos.names || []).map(name => `<span><i class="ph ph-git-branch"></i>${esc(name)}</span>`).join("") || '<span class="usage-empty">No repo sessions recorded yet.</span>'}</div>
        </article>
      </section>`;
    wireUsageHeatmap(data);
    $("#usage-provider")?.addEventListener("change", event => {
      state.costProvider = event.target.value;
      try { localStorage.setItem("accuretta:cost-provider", state.costProvider); } catch {}
      renderCostWidget();
      renderUsageStats(data);
    });
    content.querySelectorAll("[data-error-copy]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const r = await api("/api/errors/report", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: btn.dataset.errorCopy }),
          });
          if (r && r.report) {
            await copyText(r.report);
            toast("Report copied", "ok", 2500);
          } else {
            toast((r && r.error) || "Copy failed", "error", 3000);
          }
        } catch {
          toast("Copy failed", "error", 3000);
        }
      });
    });
    $("#usage-errors-clear")?.addEventListener("click", async () => {
      try {
        await api("/api/errors/clear", { method: "POST" });
        renderUsageStats(await api("/api/usage-stats"));
      } catch {
        toast("Clear failed", "error", 3000);
      }
    });
  }

  async function loadUsageStats() {
    const content = $("#usage-content");
    if (content) content.innerHTML = '<div class="usage-loading"><i class="ph ph-circle-notch"></i><span>Reading local stats</span></div>';
    try {
      renderUsageStats(await api("/api/usage-stats"));
    } catch (error) {
      if (content) content.innerHTML = `<div class="usage-error"><i class="ph ph-warning-circle"></i><strong>Usage stats are unavailable</strong><span>${esc(error.message || error)}</span></div>`;
    }
  }

  function openUsageStats() {
    openSettingsAt("usage", "stats");
  }

  function closeUsageStats() {
    const page = $("#usage-page");
    if (!page) return;
    cancelAnimationFrame(usageMarkFrame);
    page.classList.remove("open");
    page.setAttribute("aria-hidden", "true");
  }
  return {
    ghActivityOpen, ghActivityClose, loadGitHubActivity, usageCompactNumber, usageDuration,
    drawUsageMark, loadUsageStats, openUsageStats, closeUsageStats,
  };
};
