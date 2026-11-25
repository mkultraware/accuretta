/* SPDX-License-Identifier: AGPL-3.0-only */
/* file references in replies: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["file-refs"] = ({
  relPathFromRoot, state, toast, wbChatFolder, wbFileIcon, wbFileKind, wbLoadFileIndex, wbNorm,
  wbOpenWorkspaceFile,
}) => {
  const WB_FILE_REF_RE = /^(?:[A-Za-z]:[\\/]|~?[\\/]|\.{1,2}[\\/])?(?:[\w.@-]+[\\/])*[\w@-][\w.@-]*\.(?:html?|css|scss|js|mjs|cjs|jsx|ts|tsx|py|md|mdx|json|txt|ya?ml|toml|ini|cfg|sh|ps1|bat|cmd|rs|go|java|kt|swift|c|cpp|h|hpp|cs|rb|php|sql|xml|svg|png|jpe?g|gif|webp|lua|vue|svelte|csv|log)$/i;
  function wbDecorateFileRefs(bubble) {
    if (!bubble || !state.workspace?.folders?.length) return;
    bubble.querySelectorAll("code").forEach(code => {
      if (code.closest("pre") || code.classList.contains("wb-fref")) return;
      const text = (code.textContent || "").trim();
      if (!text || text.length > 220 || !WB_FILE_REF_RE.test(text)) return;
      const name = text.split(/[\\/]/).pop();
      code.classList.add("wb-fref");
      code.dataset.ref = text;
      code.title = `Open ${text}`;
      code.setAttribute("role", "button");
      code.tabIndex = 0;
      // Keep the path inside one inline <span> so streaming-fade chunks don't
      // become separate flex items inside the inline-flex <code> chip.
      const label = document.createElement("span");
      while (code.firstChild) label.appendChild(code.firstChild);
      code.appendChild(label);
      code.insertAdjacentHTML("afterbegin", `<i class="ph ${wbFileIcon(name)}" data-kind="${wbFileKind(name)}" aria-hidden="true"></i>`);
    });
  }
  function wbRootFor(absPath) {
    const target = wbNorm(absPath);
    return (state.workspace?.folders || [])
      .filter(f => target === wbNorm(f) || target.startsWith(wbNorm(f) + "/"))
      .sort((a, b) => b.length - a.length)[0] || "";
  }
  async function wbOpenRef(ref) {
    const clean = String(ref || "").trim();
    if (/^[A-Za-z]:[\\/]|^[\\/]/.test(clean)) {
      const root = wbRootFor(clean);
      if (root) return wbOpenWorkspaceFile(root, relPathFromRoot(root, clean), clean.split(/[\\/]/).pop());
    }
    const files = await wbLoadFileIndex();
    const want = clean.replace(/\\/g, "/").replace(/^\.\//, "").toLowerCase();
    const projectRoot = wbNorm(wbChatFolder());
    const matches = (files || []).filter(f => {
      const rel = f.rel.toLowerCase();
      return rel === want || rel.endsWith("/" + want) || (!want.includes("/") && f.name.toLowerCase() === want);
    });
    matches.sort((a, b) => (wbNorm(a.root) === projectRoot ? -1 : 0) - (wbNorm(b.root) === projectRoot ? -1 : 0)
      || a.rel.length - b.rel.length);
    const hit = matches[0];
    if (!hit) { toast(`Couldn't find ${clean} in your workspace folders`, "warn", 3000); return; }
    wbOpenWorkspaceFile(hit.root, hit.rel, hit.name);
  }
  return { wbDecorateFileRefs, wbRootFor, wbOpenRef };
};
