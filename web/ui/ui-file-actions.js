/* SPDX-License-Identifier: AGPL-3.0-only */
/* workspace file actions: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["file-actions"] = ({
  $, SOURCE_VIEW_LANGS, SVG_BOOK, SVG_EYE, SVG_PYCHECK, buildPreviewHtml, clearPreview, esc,
  fileExt, highlightCode, highlightHTML, isMobile, renderMarkdown, splitHighlightedLines, state,
  wbFormatBytes, wbViewerBannerHtml,
}) => {
  // urlsafe-base64 of the workspace root path, used as a path segment so
  // relative asset URLs in served HTML resolve through the same
  // /api/wsfs/<token>/... endpoint.
  function wsRootToken(root) {
    // unicode-safe utf8 → base64, then urlsafe (+→-, /→_) and strip padding
    const utf8 = unescape(encodeURIComponent(root));
    return btoa(utf8).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function wsFileUrl(root, rel) {
    // encode each path segment but keep the slashes as separators
    const encRel = (rel || "").replace(/\\/g, "/").split("/").map(encodeURIComponent).join("/");
    return `/api/wsfs/${wsRootToken(root)}/${encRel}`;
  }
  function downloadWorkspaceFile(root, rel) {
    const link = document.createElement("a");
    link.href = `${wsFileUrl(root, rel)}?download=1`;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  // Stream an existing .html from a workspace folder into the preview iframe via
  // `src` (not srcdoc), so relative asset URLs resolve through
  // /api/wsfs/<token>/..., where the bridge enforces containment to that root.
  function previewWorkspaceHtml(root, rel, displayName) {
    if (!root || !rel) return;
    if (isMobile()) {
      window.open(wsFileUrl(root, rel), "_blank");
      return;
    }
    // make sure preview pane is open
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    // remember BEFORE rendering so view-toggle / refresh handlers can detect mode
    state.workspacePreview = { root, rel, name: displayName || rel };
    state.currentHtml = "";  // ensure model-output flow doesn't fight us
    state.view = "preview";
    document.getElementById("btn-view-preview")?.classList.add("active");
    document.getElementById("btn-view-code")?.classList.remove("active");
    renderWorkspacePreview();
  }

  // Render the current workspace-preview state into the right pane, honoring
  // state.view (Preview / Code) as for model-generated HTML.
  async function renderWorkspacePreview() {
    const wp = state.workspacePreview;
    if (!wp) return;
    document.getElementById("preview-empty")?.classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = wp.name || wp.rel;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `workspace · ${wp.name || wp.rel}`;

    if (state.view === "code") {
      // hide iframe stage, show code-view, fetch source as text
      document.getElementById("preview-stage")?.classList.add("hidden");
      const c = document.getElementById("code-view");
      if (!c) return;
      c.classList.remove("hidden");
      c.textContent = "loading…";
      try {
        const r = await fetch(wsFileUrl(wp.root, wp.rel));
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          c.textContent = `error: ${j.error || r.statusText}`;
          return;
        }
        const txt = await r.text();
        c.innerHTML = highlightHTML(txt);
      } catch (e) {
        c.textContent = `error: ${e.message || e}`;
      }
      return;
    }

    // preview mode — recreate iframe pointing at the path-style endpoint
    document.getElementById("code-view")?.classList.add("hidden");
    const stage = document.getElementById("preview-stage");
    if (stage) stage.classList.remove("hidden");
    const old = document.getElementById("preview-frame");
    const fresh = document.createElement("iframe");
    fresh.id = "preview-frame";
    fresh.className = "preview-frame";
    fresh.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups");
    fresh.src = wsFileUrl(wp.root, wp.rel);
    old?.replaceWith(fresh);
  }

  // Server-side Python syntax check of a workspace .py file (never executed).
  // Shows a pass/fail banner and the source with the error line highlighted in
  // the bottom pane.
  async function runPythonCheck(root, rel, displayName) {
    if (!root || !rel) return;
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    document.getElementById("preview-empty")?.classList.add("hidden");
    document.getElementById("preview-stage")?.classList.add("hidden");
    document.getElementById("code-view")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    const banner = document.getElementById("pycheck-banner");
    const codeEl = document.getElementById("pycheck-code")?.querySelector("code");
    if (!banner || !codeEl) return;

    banner.className = "pycheck-banner pending";
    banner.textContent = `checking ${displayName || rel}…`;
    codeEl.textContent = "";
    document.getElementById("pycheck-pane")?.classList.remove("hidden");

    let res;
    try {
      const r = await fetch("/api/py-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root, path: rel }),
      });
      res = await r.json();
    } catch (e) {
      banner.className = "pycheck-banner err";
      banner.textContent = `request failed: ${e.message || e}`;
      return;
    }
    if (res.error) {
      banner.className = "pycheck-banner err";
      banner.textContent = res.error;
      return;
    }

    // also fetch the actual source to show under the banner
    let srcText = "";
    try {
      const sr = await fetch(wsFileUrl(root, rel));
      if (sr.ok) srcText = await sr.text();
    } catch {}

    const errLine = res.ok ? -1 : Math.max(1, parseInt(res.line || 0, 10));
    // Tokenize the whole source in one pass so multi-line strings keep their
    // coloring, then split on \n so each line gets its own block (line numbers +
    // error highlight). The block-level .pyc-line spans provide the line breaks,
    // so join("") rather than "\n" (which would double-space inside <pre>).
    const fullHtml = highlightCode(srcText, "py");
    const htmlLines = splitHighlightedLines(fullHtml);
    const numbered = htmlLines.map((html, i) => {
      const n = i + 1;
      const isErr = !res.ok && n === errLine;
      const cls = isErr ? "pyc-line err" : "pyc-line";
      return `<span class="${cls}"><span class="pyc-num">${String(n).padStart(4, " ")}</span> ${html}</span>`;
    }).join("");
    codeEl.innerHTML = numbered;

    if (res.ok) {
      banner.className = "pycheck-banner ok";
      banner.innerHTML = `${SVG_PYCHECK} <strong>syntax OK</strong> · ${esc(res.file || displayName || rel)} · ${res.lines} lines`;
    } else {
      const at = res.line ? ` at line ${res.line}${res.col ? `, col ${res.col}` : ""}` : "";
      banner.className = "pycheck-banner err";
      banner.innerHTML = `<strong>SyntaxError</strong>${esc(at)}: ${esc(res.msg || "unknown")} · ${esc(res.file || displayName || rel)}`;
      // scroll to the error line
      requestAnimationFrame(() => {
        const errEl = document.getElementById("pycheck-pane")?.querySelector(".pyc-line.err");
        if (errEl) errEl.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    }
  }

  // Hide every right-pane mode and return the doc-preview pane (lazily created)
  // shared by the markdown renderer and the formatted-source view. It sits next
  // to .pycheck-pane inside .preview-body with the same banner + scrollable body
  // layout.
  function _ensureDocPreviewPane() {
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    document.getElementById("preview-empty")?.classList.add("hidden");
    document.getElementById("preview-stage")?.classList.add("hidden");
    document.getElementById("code-view")?.classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    let pane = document.getElementById("doc-preview-pane");
    if (!pane) {
      pane = document.createElement("div");
      pane.id = "doc-preview-pane";
      pane.className = "doc-preview-pane";
      pane.innerHTML = `
        <div class="doc-preview-banner" id="doc-preview-banner"></div>
        <div class="doc-preview-body" id="doc-preview-body"></div>`;
      const pBody = document.getElementById("preview-body");
      if (pBody) {
        const resizer = document.getElementById("preview-v-resizer");
        pBody.insertBefore(pane, resizer);
      }
    }
    pane.classList.remove("hidden");
    return pane;
  }

  // Render a workspace .md file in the preview pane with the same
  // renderMarkdown() used for chat bubbles. No iframe: the body is injected
  // straight into the pane and scoped via .doc-preview-body.
  async function previewWorkspaceMarkdown(root, rel, displayName) {
    if (!root || !rel) return;
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.remove("doc-source-mode");
    banner.className = "doc-preview-banner pending";
    banner.innerHTML = `${SVG_BOOK} <strong>${esc(displayName || rel)}</strong> · loading…`;
    body.innerHTML = "";
    try {
      const r = await fetch(wsFileUrl(root, rel));
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        banner.className = "doc-preview-banner err";
        banner.textContent = `error: ${j.error || r.statusText}`;
        return;
      }
      const txt = await r.text();
      body.innerHTML = `<div class="doc-md">${renderMarkdown(txt)}</div>`;
      banner.className = "doc-preview-banner ok wb-viewer-banner";
      banner.innerHTML = wbViewerBannerHtml(root, rel, displayName,
        ["markdown", `${txt.split("\n").length} lines`, wbFormatBytes(new Blob([txt]).size)]);
    } catch (e) {
      banner.className = "doc-preview-banner err";
      banner.textContent = `error: ${e.message || e}`;
    }
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `md · ${displayName || rel}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `markdown · ${displayName || rel}`;
  }

  // Render a workspace text/code file as highlighted source in the preview pane,
  // using the chat-fence tokenizer picked by extension (SOURCE_VIEW_LANGS).
  // Plain-text files (.txt, .toml, .ini) render as escaped monospace.
  async function previewWorkspaceSource(root, rel, displayName) {
    if (!root || !rel) return;
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.add("doc-source-mode");
    banner.className = "doc-preview-banner pending";
    banner.innerHTML = `${SVG_EYE} <strong>${esc(displayName || rel)}</strong> · loading…`;
    body.innerHTML = "";
    try {
      const r = await fetch(wsFileUrl(root, rel));
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        banner.className = "doc-preview-banner err";
        banner.textContent = `error: ${j.error || r.statusText}`;
        return;
      }
      const txt = await r.text();
      const lang = SOURCE_VIEW_LANGS[fileExt(displayName || rel)] || "text";
      const fullHtml = highlightCode(txt, lang);
      const htmlLines = splitHighlightedLines(fullHtml);
      const numbered = htmlLines.map((html, i) => {
        const n = i + 1;
        return `<span class="pyc-line"><span class="pyc-num">${String(n).padStart(4, " ")}</span> ${html}</span>`;
      }).join("");
      body.innerHTML = `<pre class="pycheck-code"><code>${numbered}</code></pre>`;
      banner.className = "doc-preview-banner ok wb-viewer-banner";
      banner.innerHTML = wbViewerBannerHtml(root, rel, displayName,
        [lang, `${htmlLines.length} lines`, wbFormatBytes(new Blob([txt]).size)]);
    } catch (e) {
      banner.className = "doc-preview-banner err";
      banner.textContent = `error: ${e.message || e}`;
    }
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `source · ${displayName || rel}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `source · ${displayName || rel}`;
  }

  function renderPreview() {
    if (!state.currentHtml) { clearPreview(); return; }
    $("#preview-empty").classList.add("hidden");
    if (state.view === "preview") {
      $("#code-view").classList.add("hidden");
      $("#preview-stage").classList.remove("hidden");
      // recreate iframe each time we switch back — srcdoc on a hidden iframe
      // can end up blank in some browsers. Cheap and always correct.
      const old = $("#preview-frame");
      const fresh = document.createElement("iframe");
      fresh.id = "preview-frame";
      fresh.className = "preview-frame";
      // Preview scripts receive an opaque origin, separate from the control UI.
      fresh.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups");
      // tabindex makes the iframe element itself focusable, which is what
      // lets contentWindow.focus() actually take effect from the parent.
      fresh.setAttribute("tabindex", "0");
      fresh.srcdoc = buildPreviewHtml();
      // Focus the iframe on hover; otherwise a fresh iframe doesn't receive wheel
      // events until the user clicks inside it.
      fresh.addEventListener("mouseenter", () => {
        try { fresh.contentWindow && fresh.contentWindow.focus(); } catch {}
      });
      // Also focus once the document loads, so the first scroll after a render works
      // without a hover.
      fresh.addEventListener("load", () => {
        try { fresh.contentWindow && fresh.contentWindow.focus(); } catch {}
      });
      old.replaceWith(fresh);
    } else {
      $("#preview-stage").classList.add("hidden");
      const c = $("#code-view");
      c.classList.remove("hidden");
      c.innerHTML = highlightHTML(state.currentHtml);
    }
  }
  return {
    wsFileUrl, downloadWorkspaceFile, previewWorkspaceHtml, renderWorkspacePreview, runPythonCheck,
    _ensureDocPreviewPane, previewWorkspaceMarkdown, previewWorkspaceSource, renderPreview,
  };
};
