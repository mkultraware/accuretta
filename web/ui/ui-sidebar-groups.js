/* SPDX-License-Identifier: AGPL-3.0-only */
/* sidebar groups: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["sidebar-groups"] = ({
  $, WB_GROUPS_KEY, folderLeafName, missionCategory, renderChatList, sbRevealSection, state, wbNorm,
  wbStore,
}) => {
  function wbChatGroup(c) {
    const cat = missionCategory(c);
    if (cat) return { key: `mission:${cat.key}`, label: cat.label, icon: cat.icon };
    if (c.github_worktree) {
      const name = String(c.github_worktree.repo || c.github_worktree.name || "GitHub").trim();
      return { key: `gh:${wbNorm(c.github_worktree.path || name)}`, label: name, icon: "ph-git-branch", path: c.github_worktree.path || "" };
    }
    if (c.project_workspace) {
      const name = String(c.project_workspace.name || folderLeafName(c.project_workspace.path || "") || "Project").trim();
      return { key: `proj:${wbNorm(c.project_workspace.path || name)}`, label: name, icon: "ph-folder-simple", path: c.project_workspace.path || "" };
    }
    return { key: "chats", label: "Chats", icon: "ph-chats-circle" };
  }
  // Collapse is the user's call, so the open session doesn't pin its group open;
  // switching to a session elsewhere (a tab, the palette, a new chat) opens that
  // one group so the selection is visible.
  function wbRevealChatGroup(id) {
    const c = state.chats?.chats?.[id];
    if (!c || state.projectScope) return;
    const key = wbChatGroup(c).key;
    sbRevealSection(key === "chats" ? "chats" : "projects");
    const set = wbCollapsedGroups();
    if (!set.delete(key)) return;
    wbSaveCollapsedGroups(set);
    const box = [...document.querySelectorAll("#chatlist .wb-group")].find(g => g.dataset.group === key);
    if (box) {
      box.classList.remove("collapsed");
      box.querySelector(".wb-group-head")?.setAttribute("aria-expanded", "true");
      const caret = box.querySelector(".wb-group-caret");
      if (caret) caret.className = "ph ph-caret-down wb-group-caret";
    }
    wbSyncCollapseAll();
  }
  function wbCollapsedGroups() { return new Set(wbStore.get(WB_GROUPS_KEY, [])); }
  function wbSaveCollapsedGroups(set) { wbStore.set(WB_GROUPS_KEY, [...set]); }
  function wbSyncCollapseAll() {
    const btn = $("#btn-wb-collapse-groups");
    if (!btn) return;
    const groups = [...document.querySelectorAll("#chatlist .wb-group")];
    const allCollapsed = groups.length > 0 && groups.every(g => g.classList.contains("collapsed"));
    btn.hidden = groups.length < 2;
    btn.title = allCollapsed ? "Expand all groups" : "Collapse all groups";
    btn.setAttribute("aria-label", btn.title);
    btn.querySelector("i").className = `ph ${allCollapsed ? "ph-arrows-out-line-vertical" : "ph-arrows-in-line-vertical"}`;
  }
  function wbToggleAllGroups() {
    const groups = [...document.querySelectorAll("#chatlist .wb-group")];
    const collapse = !groups.every(g => g.classList.contains("collapsed"));
    wbSaveCollapsedGroups(collapse ? new Set(groups.map(g => g.dataset.group)) : new Set());
    renderChatList();
  }
  // One "Sessions" header holds the project filter, add-project, collapse all
  // and clear.
  function wbBuildSessionsHeader() {
    const clear = $("#btn-clear-conversations");
    const head = clear?.closest(".section-head");
    if (!head || head.classList.contains("wb-sessions-head")) return;
    const add = $("#btn-project-add");
    const projectHead = add?.closest(".section-head");
    const scope = $(".project-scope");
    head.classList.add("wb-sessions-head");
    const label = head.querySelector(":scope > span:not(.grow)");
    if (label) label.textContent = "Sessions";
    const collapseBtn = document.createElement("button");
    collapseBtn.type = "button";
    collapseBtn.className = "iconbtn";
    collapseBtn.id = "btn-wb-collapse-groups";
    collapseBtn.innerHTML = '<i class="ph ph-arrows-in-line-vertical"></i>';
    collapseBtn.addEventListener("click", wbToggleAllGroups);
    const tools = [scope, add, collapseBtn].filter(Boolean);
    clear.before(...tools);
    if (projectHead && projectHead !== head) projectHead.remove();
    wbSyncCollapseAll();
  }
  return {
    wbChatGroup, wbRevealChatGroup, wbCollapsedGroups, wbSaveCollapsedGroups, wbSyncCollapseAll,
    wbBuildSessionsHeader,
  };
};
