/* SPDX-License-Identifier: AGPL-3.0-only */
/* file index (Ctrl+P): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["file-index"] = ({
  $, _fuzzyScore, api, closePalette, effectiveShortcuts, folderLeafName, openSettings,
  refreshPaletteList, revealSettingsControl, shortcutLabel, state, wbFileIcon, wbFileIndex,
  wbFileKind, wbOpenWorkspaceFile, wbRootFor, wbRunAction,
}) => {
  function wbLoadFileIndex(force) {
    if (!force && wbFileIndex.files && Date.now() - wbFileIndex.loadedAt < 60000) return Promise.resolve(wbFileIndex.files);
    if (wbFileIndex.loading) return wbFileIndex.loading;
    wbFileIndex.loading = api("/api/workspace/files").then(r => {
      wbFileIndex.files = (r?.files || []).map(f => {
        const path = String(f.path || "");
        const rel = String(f.rel || f.name || "");
        const tail = path.includes("\\") ? rel.replace(/\//g, "\\") : rel;
        const root = path.toLowerCase().endsWith(tail.toLowerCase())
          ? path.slice(0, path.length - tail.length).replace(/[\\/]+$/, "")
          : wbRootFor(path);
        return { name: String(f.name || rel.split("/").pop()), rel, path, root };
      });
      wbFileIndex.loadedAt = Date.now();
      return wbFileIndex.files;
    }).catch(() => {
      wbFileIndex.files = wbFileIndex.files || [];
      return wbFileIndex.files;
    }).finally(() => {
      wbFileIndex.loading = null;
      if (state.palette.open) refreshPaletteList($("#palette-input")?.value || "");
    });
    return wbFileIndex.loading;
  }
  function wbPaletteFiles(query, limit, bonus) {
    const files = wbFileIndex.files;
    if (!files) return [];
    const q = query.trim();
    const multiRoot = (state.workspace?.folders || []).length > 1;
    const scored = [];
    for (const f of files) {
      let s = 0;
      if (q) {
        // Fuzzy on the file name; the full path only counts as a contiguous
        // match ("src/app"), or long paths match almost any letters.
        const byName = _fuzzyScore(q, f.name);
        const at = f.rel.toLowerCase().indexOf(q.toLowerCase());
        s = Math.max(byName >= 0 ? byName * 2 : -1, at >= 0 ? 60 - Math.min(50, at) : -1);
        if (s < 0) continue;
      }
      scored.push([s, f]);
    }
    scored.sort((a, b) => b[0] - a[0] || a[1].rel.length - b[1].rel.length);
    return scored.slice(0, limit).map(([s, f]) => ({
      kind: "file",
      icon: wbFileIcon(f.name),
      iconClass: `wb-kind-${wbFileKind(f.name)}`,
      label: f.name,
      sub: multiRoot ? `${folderLeafName(f.root)}/${f.rel}` : f.rel,
      score: s + bonus,
      action: () => { closePalette(); wbOpenWorkspaceFile(f.root, f.rel, f.name); },
    }));
  }
  function wbPaletteCommands() {
    const keys = effectiveShortcuts();
    const cmd = (action, icon, label) => ({
      kind: "cmd", icon, label, keys: keys[action] ? shortcutLabel(keys[action]).replace(/ \+ /g, " ") : "",
      action: () => { closePalette(); wbRunAction(action); },
    });
    return [
      cmd("quick_open", "ph-file-magnifying-glass", "Go to file…"),
      cmd("toggle_sidebar", "ph-sidebar-simple", "Toggle sidebar"),
      cmd("toggle_explorer", "ph-files", "Toggle workspace"),
      cmd("toggle_terminal", "ph-terminal-window", "Toggle terminal"),
      cmd("next_tab", "ph-arrow-down", "Next tab"),
      cmd("prev_tab", "ph-arrow-up", "Previous tab"),
      cmd("close_tab", "ph-x-square", "Close tab"),
      { kind: "cmd", icon: "ph-keyboard", label: "Keyboard shortcuts", action: () => { closePalette(); openSettings(); setTimeout(() => revealSettingsControl("#shortcut-list"), 80); } },
    ];
  }
  return { wbLoadFileIndex, wbPaletteFiles, wbPaletteCommands };
};
