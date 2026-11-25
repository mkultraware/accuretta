/* SPDX-License-Identifier: AGPL-3.0-only */
/* preview: screenshot / export / review-UI: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["preview-tools"] = ({
  $, api, autoResize, buildPreviewHtml, confirmModal, renderImageTray, state, toast,
}) => {
  function safeSlug(s, fallback) {
    const t = (s || "").toString().toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
    return t || fallback;
  }

  function currentProjectBase() {
    const v = state.versions.find(x => x.id === state.activeVersion);
    const title = (state.chats?.find?.(x => x.id === state.chatId)?.title) || "";
    return safeSlug(title || (v ? `version-${v.n}` : "preview"), "preview");
  }

  async function captureIframePng({ scale = 1 } = {}) {
    const frame = $("#preview-frame");
    if (!frame || (!state.currentHtml && !state.workspacePreview)) return null;
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join("");
        const source = frame.contentWindow;
        const timer = setTimeout(() => finish(new Error("Preview capture timed out")), 15000);
        function finish(error, image) {
          clearTimeout(timer);
          window.removeEventListener("message", receive);
          if (error) reject(error); else resolve(image);
        }
        function receive(event) {
          const data = event.data;
          if (event.source !== source || event.origin !== "null" || data?.__acc !== "capture-result" || data.id !== id) return;
          if (typeof data.image !== "string" || !data.image.startsWith("data:image/png;base64,") || data.image.length > 32 * 1024 * 1024) {
            finish(new Error(String(data.error || "Invalid preview image").slice(0, 300)));
          } else finish(null, data.image);
        }
        window.addEventListener("message", receive);
        source.postMessage({ __acc: "capture", id, scale }, "*");
      });
      const picture = new Image();
      picture.src = dataUrl;
      await picture.decode();
      if (picture.width > 8192 || picture.height > 8192) throw new Error("Preview image is too large");
      const canvas = document.createElement("canvas");
      canvas.width = picture.width; canvas.height = picture.height;
      canvas.getContext("2d").drawImage(picture, 0, 0);
      return canvas;
    } catch (error) {
      toast(`Screenshot failed: ${error.message}`, "err", 4000);
      return null;
    }
  }

  async function screenshotPreview() {
    const canvas = await captureIframePng();
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${currentProjectBase()}-${Date.now()}.png`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Screenshot saved", "ok", 2000, "ss");
    }, "image/png");
  }

  async function reviewUiAttach() {
    const canvas = await captureIframePng();
    if (!canvas) return;
    const dataUrl = canvas.toDataURL("image/png");
    state.pendingImages.push({ dataUrl, name: `ui-${Date.now()}.png` });
    renderImageTray();
    const ta = $("#composer-input");
    if (ta) {
      const existing = ta.value.trim();
      if (!/review this ui/i.test(existing)) {
        ta.value = existing ? `${existing}\n\nReview this UI — note what feels off and suggest concrete fixes.`
                            : "Review this UI — note what feels off and suggest concrete fixes.";
      }
      autoResize(ta);
      ta.focus();
    }
    toast("Preview attached — press Send to have the model review it.", "ok", 3000, "review");
  }

  async function saveSnapshot() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    const base = currentProjectBase();
    const html = buildPreviewHtml();  // persist what the user actually sees
    const resp = await fetch("/api/snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: base, html }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      toast(`Snapshot failed: ${data.error || resp.status}`, "err", 3500);
      return;
    }
    toast(`Saved: ${data.name}`, "ok", 2600, "snap");
  }

  // Save the current preview HTML to a workspace folder by POSTing it to the
  // bridge (which validates the root), without asking the agent to regenerate
  // it.
  async function saveToWorkspace() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    // 1. Get the current workspace folders.
    let folders = [];
    try {
      const ws = await api("/api/workspace");
      folders = (ws && ws.folders) || [];
    } catch {
      toast("Couldn't load workspace folders.", "err", 3000);
      return;
    }
    if (!folders.length) {
      toast("No workspace folders configured. Add one in the Workspace panel first.", "warn", 4000);
      return;
    }
    // 2. Pick a root. Single folder = use it; multiple = prompt with a
    //    numbered list (kept dead simple — no modal infra needed).
    let root;
    if (folders.length === 1) {
      root = folders[0];
    } else {
      const list = folders.map((f, i) => `${i + 1}. ${f}`).join("\n");
      const pick = window.prompt(`Save to which workspace folder?\n\n${list}\n\nEnter 1-${folders.length}:`, "1");
      if (pick == null) return;
      const idx = parseInt(pick, 10) - 1;
      if (!(idx >= 0 && idx < folders.length)) {
        toast("Invalid choice.", "warn", 2200);
        return;
      }
      root = folders[idx];
    }
    // 3. Filename — default to project slug + .html.
    const defaultName = `${currentProjectBase()}.html`;
    const filename = window.prompt(`Save as (in ${root}):`, defaultName);
    if (filename == null || !filename.trim()) return;
    const html = buildPreviewHtml();
    // 4. POST. Handle 409 (file exists) by re-asking with overwrite=true.
    const send = async (overwrite) => fetch("/api/save-to-workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, filename: filename.trim(), html, overwrite }),
    });
    let resp = await send(false);
    if (resp.status === 409) {
      const ok = await confirmModal({
        title: "Overwrite file",
        message: `"${filename.trim()}" already exists in ${root}. Overwrite it?`,
        confirmText: "Overwrite",
        icon: "ph-warning-circle",
      });
      if (!ok) return;
      resp = await send(true);
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      toast(`Save failed: ${data.error || resp.status}`, "err", 3500);
      return;
    }
    toast(`Saved to ${data.path}`, "ok", 3000, "ws-save");
  }

  function toggleConsolePane(force) {
    const want = typeof force === "boolean" ? force : !state.consoleOpen;
    state.consoleOpen = want;
    const pane = $("#console-pane");
    if (!pane) return;
    pane.classList.toggle("hidden", !want);
  }

  async function exportProjectZip() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    const base = currentProjectBase();
    const files = state.currentFiles || {};
    const hasCompanions = Object.keys(files).length > 0;

    // single-file path: just download the html
    if (!hasCompanions) {
      const blob = new Blob([state.currentHtml], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${base}.html`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Downloaded HTML", "ok", 2000, "exp");
      return;
    }

    if (typeof window.JSZip !== "function") {
      toast("Zip library hasn't loaded yet — try again in a second.", "warn", 2500);
      return;
    }

    const zip = new window.JSZip();
    // if the model also emitted its own index.html path, prefer that verbatim;
    // otherwise write state.currentHtml as index.html
    if (!files["index.html"]) zip.file("index.html", state.currentHtml);
    for (const [path, body] of Object.entries(files)) zip.file(path, body);
    const blob = await zip.generateAsync({ type: "blob" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${base}.zip`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${Object.keys(files).length + (files["index.html"] ? 0 : 1)} files`, "ok", 2400, "exp");
  }
  return {
    captureIframePng, screenshotPreview, reviewUiAttach, saveSnapshot, saveToWorkspace,
    toggleConsolePane, exportProjectZip,
  };
};
