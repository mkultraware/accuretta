/* SPDX-License-Identifier: AGPL-3.0-only */
/* git: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["wb-git"] = ({
  $, api, esc, folderLeafName, state, wb, wbNorm, wbRenderContextRow,
}) => {
  function wbGitEntryFor(path) {
    const target = wbNorm(path);
    let best = null;
    for (const [root, info] of wb.git) {
      if ((target === root || target.startsWith(root + "/")) && (!best || root.length > best[0].length)) best = [root, info];
    }
    return best ? best[1] : null;
  }
  function wbGitCodeFor(path, isDir) {
    const info = wbGitEntryFor(path);
    if (!info?.repo) return "";
    const key = wbNorm(path);
    if (isDir) return info.dirs.has(key) ? "M" : "";
    return info.files.get(key) || "";
  }
  function wbGitLabel(code) {
    if (code === "??") return "untracked";
    return { A: "added", M: "modified", D: "deleted", R: "renamed", U: "conflict" }[String(code).replace(/\s/g, "").slice(0, 1)] || "changed";
  }
  async function wbLoadGit(root, headerEl) {
    if (!root) return null;
    let info = null;
    try {
      const r = await api(`/api/workspace/git?path=${encodeURIComponent(root)}`);
      if (r?.repo) {
        const files = new Map();
        const dirs = new Set();
        const rootKey = wbNorm(root);
        for (const [p, code] of Object.entries(r.files || {})) {
          const key = wbNorm(p);
          files.set(key, code);
          let dir = key.slice(0, key.lastIndexOf("/"));
          while (dir && dir.length > rootKey.length && dir.startsWith(rootKey)) {
            dirs.add(dir);
            dir = dir.slice(0, dir.lastIndexOf("/"));
          }
        }
        info = { repo: true, branch: r.branch || "", files, dirs, added: r.added || 0, deleted: r.deleted || 0 };
        wb.git.set(wbNorm(root), info);
      }
    } catch {}
    if (headerEl) {
      headerEl.querySelector(".wb-ws-git")?.remove();
      if (info) {
        const span = document.createElement("span");
        span.className = "wb-ws-git";
        span.title = `${info.branch || "detached"} · ${info.files.size} changed file${info.files.size === 1 ? "" : "s"}`;
        span.innerHTML = (info.branch ? `<i class="ph ph-git-branch" aria-hidden="true"></i>${esc(info.branch)}` : "")
          + (info.added || info.deleted ? ` <b class="wb-add">+${info.added}</b> <b class="wb-del">−${info.deleted}</b>` : "");
        headerEl.querySelector(".path")?.after(span);
      }
    }
    return info;
  }
  function wbChatFolder(chat = state.chats?.chats?.[state.chatId]) {
    return chat?.github_worktree?.path || chat?.project_workspace?.path || "";
  }
  async function wbRefreshGitForChat() {
    const folder = wbChatFolder() || state.workspace?.folders?.[0] || "";
    const chip = $("#wb-chip-diff");
    if (!folder) { if (chip) chip.hidden = true; wbRenderContextRow(); return; }
    const info = await wbLoadGit(folder);
    if (chip) {
      const show = !!(info && (info.added || info.deleted));
      chip.hidden = !show;
      if (show) {
        chip.innerHTML = `<b class="wb-add">+${info.added}</b><b class="wb-del">−${info.deleted}</b>`;
        chip.title = `${info.files.size} changed file${info.files.size === 1 ? "" : "s"} in ${folderLeafName(folder)}${info.branch ? ` on ${info.branch}` : ""} · open the explorer`;
        chip.dataset.folder = folder;
      }
    }
    wbRenderContextRow(info);
  }
  return { wbGitEntryFor, wbGitCodeFor, wbGitLabel, wbLoadGit, wbChatFolder, wbRefreshGitForChat };
};
