/* SPDX-License-Identifier: AGPL-3.0-only */
/* event wiring: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["event-wiring"] = ({ esc, state }) => {
  function autoResize(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(200, ta.scrollHeight) + "px";
    if (ta && ta.id === "composer-input") updateComposerMirror();
  }

  // ===== @/# mention: @ references workspace files, # loads a skill =====
  // "@" picks a workspace file; it renders as an accent chip (via a mirror layer
  // behind the textarea) in the box and in chat, and on send the model is given
  // the resolved path.
  const _MENTION_RE = /(^|[\s(])@([\w./-]+)/g;

  async function loadWorkspaceFiles(force) {
    if (!force && state._wsFiles && Date.now() - (state._wsFilesAt || 0) < 8000) return state._wsFiles;
    try {
      const r = await (await fetch("/api/workspace/files")).json();
      state._wsFiles = r.files || [];
      state._wsFilesAt = Date.now();
    } catch { state._wsFiles = state._wsFiles || []; }
    return state._wsFiles;
  }

  // Resolve a raw @token (maybe with trailing punctuation) to a workspace file.
  function resolveMentionToken(token) {
    const files = state._wsFiles || [];
    if (!files.length || !token) return null;
    let t = token;
    for (let i = 0; i < 4 && t.length; i++) {
      const tl = t.toLowerCase();
      const f = files.find(x => x.name.toLowerCase() === tl) || files.find(x => x.rel.toLowerCase() === tl);
      if (f) return { file: f, matched: t };
      if (/[^A-Za-z0-9]$/.test(t)) t = t.slice(0, -1); else break;
    }
    return null;
  }

  function renderComposerMentions(text) {
    let out = "", last = 0, m;
    _MENTION_RE.lastIndex = 0;
    while ((m = _MENTION_RE.exec(text)) !== null) {
      const at = m.index + m[1].length;   // index of '@'
      out += esc(text.slice(last, at));
      const r = resolveMentionToken(m[2]);
      if (r) { out += `<span class="composer-chip">@${esc(r.matched)}</span>`; last = at + 1 + r.matched.length; }
      else { out += "@"; last = at + 1; }
    }
    out += esc(text.slice(last));
    return out.replace(/\n/g, "<br>");
  }

  // Copy the textarea's computed text-layout styles onto the mirror so both lay
  // out identically, instead of hand-matching CSS.
  function syncComposerMirrorStyle() {
    const ta = document.getElementById("composer-input");
    const mirror = document.getElementById("composer-mirror");
    if (!ta || !mirror) return;
    const cs = getComputedStyle(ta);
    ["fontFamily", "fontSize", "fontWeight", "fontStyle", "fontVariant",
     "fontStretch", "fontKerning", "fontFeatureSettings", "letterSpacing",
     "wordSpacing", "lineHeight", "textTransform", "textIndent", "tabSize",
     "paddingTop", "paddingRight", "paddingBottom", "paddingLeft"].forEach(p => {
      try { mirror.style[p] = cs[p]; } catch {}
    });
  }
  function updateComposerMirror() {
    const ta = document.getElementById("composer-input");
    const mirror = document.getElementById("composer-mirror");
    if (!ta || !mirror) return;
    mirror.innerHTML = renderComposerMentions(ta.value);
    mirror.scrollTop = ta.scrollTop;
    mirror.scrollLeft = ta.scrollLeft;
  }
  return {
    autoResize, _MENTION_RE, loadWorkspaceFiles, resolveMentionToken, syncComposerMirrorStyle,
    updateComposerMirror,
  };
};
