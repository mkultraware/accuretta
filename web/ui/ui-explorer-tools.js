/* SPDX-License-Identifier: AGPL-3.0-only */
/* explorer tools: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["explorer-tools"] = () => {
  function wbApplyTreeFilter(q) {
    const needle = String(q || "").trim().toLowerCase();
    const nodes = [...document.querySelectorAll("#ws-list .tree-node")];
    nodes.forEach(n => n.classList.remove("wb-filtered"));
    if (!needle) return;
    // Deepest first, so a folder stays visible when any loaded child matches.
    nodes.reverse().forEach(n => {
      const nameHit = (n.dataset.name || "").toLowerCase().includes(needle);
      const childHit = !!n.querySelector(":scope > .tree-children > .tree-node:not(.wb-filtered)");
      n.classList.toggle("wb-filtered", !nameHit && !childHit);
    });
  }
  function wbCollapseAllFolders() {
    document.querySelectorAll("#ws-list .tree-node.open").forEach(n => {
      n.classList.remove("open");
      n.querySelector(":scope > .tree-children")?.setAttribute("hidden", "");
    });
    document.querySelectorAll("#ws-list .ws-root.open").forEach(r => {
      r.classList.remove("open");
      const tree = r.querySelector(".ws-tree");
      if (tree) tree.hidden = true;
    });
  }
  return { wbApplyTreeFilter, wbCollapseAllFolders };
};
