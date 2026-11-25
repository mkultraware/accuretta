/* SPDX-License-Identifier: AGPL-3.0-only */
/* viewer: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["wb-viewer"] = ({
  WB_IMAGE_EXT, _ensureDocPreviewPane, esc, folderLeafName, previewWorkspaceHtml,
  previewWorkspaceMarkdown, previewWorkspaceSource, state, wbFileIcon, wbFileKind, wbFormatBytes,
  wsFileUrl,
}) => {
  function wbOpenWorkspaceFile(root, rel, name) {
    if (!root || !rel) return;
    const display = name || rel.split(/[\\/]/).pop();
    const ext = display.toLowerCase().split(".").pop();
    if (["html", "htm"].includes(ext)) return previewWorkspaceHtml(root, rel, display);
    if (["md", "mdx"].includes(ext)) return previewWorkspaceMarkdown(root, rel, display);
    if (WB_IMAGE_EXT.has(ext)) return wbPreviewImage(root, rel, display);
    return previewWorkspaceSource(root, rel, display);
  }
  function wbViewerBannerHtml(root, rel, name, parts) {
    const segs = String(rel || name || "").split(/[\\/]/).filter(Boolean);
    const file = segs.pop() || name || "";
    const crumbs = [folderLeafName(root || ""), ...segs].filter(Boolean)
      .map(s => `<span>${esc(s)}</span><i class="ph ph-caret-right" aria-hidden="true"></i>`).join("");
    return `<span class="wb-crumbs"><i class="ph ${wbFileIcon(file)}" data-kind="${wbFileKind(file)}" aria-hidden="true"></i>${crumbs}<strong>${esc(file)}</strong></span>`
      + `<span class="wb-viewer-meta">${(parts || []).filter(Boolean).map(esc).join(" · ")}</span>`;
  }
  async function wbPreviewImage(root, rel, name) {
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.remove("doc-source-mode");
    banner.className = "doc-preview-banner pending wb-viewer-banner";
    banner.innerHTML = wbViewerBannerHtml(root, rel, name, ["loading…"]);
    body.innerHTML = "";
    try {
      const r = await fetch(wsFileUrl(root, rel));
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
      const blob = await r.blob();
      const img = new Image();
      img.alt = name;
      img.className = "wb-image";
      img.onload = () => {
        banner.className = "doc-preview-banner ok wb-viewer-banner";
        banner.innerHTML = wbViewerBannerHtml(root, rel, name,
          [name.split(".").pop().toUpperCase(), img.naturalWidth ? `${img.naturalWidth} × ${img.naturalHeight}` : "", wbFormatBytes(blob.size)]);
      };
      img.src = URL.createObjectURL(blob);
      const view = document.createElement("div");
      view.className = "wb-image-view fit";
      view.title = "Click to switch between fit and actual size";
      view.appendChild(img);
      view.addEventListener("click", () => view.classList.toggle("fit"));
      body.appendChild(view);
    } catch (e) {
      banner.className = "doc-preview-banner err";
      banner.textContent = `error: ${e.message || e}`;
    }
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `image · ${name}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `image · ${name}`;
  }

  // Pictures the agent showed with show_image: a card each under its reply, the
  // same while it streams and after the chat is reopened (the reply keeps them
  // as m._images). Only show_image's own /api/chat-media/ names are drawn.
  const SHOWN_IMAGE_URL = /^\/api\/chat-media\/[0-9a-f]{24}\.(?:png|jpg|gif|webp)$/;
  function shownImageHtml(img) {
    if (!img || !SHOWN_IMAGE_URL.test(String(img.url || ""))) return "";
    const cap = esc(img.caption || "Picture");
    const size = img.width > 0 && img.height > 0 ? ` width="${Number(img.width)}" height="${Number(img.height)}"` : "";
    return `<figure class="shown-image"><figcaption><i class="ph ph-image" aria-hidden="true"></i><span>${cap}</span></figcaption>`
      + `<button type="button" class="shown-image-view" data-url="${img.url}" data-caption="${cap}" title="Open full size">`
      + `<img src="${img.url}" alt="${cap}" loading="lazy"${size}></button></figure>`;
  }
  function shownImagesHtml(images) {
    const cards = (Array.isArray(images) ? images : []).map(shownImageHtml).filter(Boolean);
    return cards.length ? `<div class="shown-images">${cards.join("")}</div>` : "";
  }
  // Full size in the same viewer as workspace images: fit, or actual size on click.
  function wbPreviewShownImage(url, caption) {
    if (!SHOWN_IMAGE_URL.test(String(url || ""))) return;
    const name = caption || "Picture";
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.remove("doc-source-mode");
    const head = (parts) => `<span class="wb-crumbs"><i class="ph ph-image" aria-hidden="true"></i><strong>${esc(name)}</strong></span>`
      + `<span class="wb-viewer-meta">${parts.filter(Boolean).map(esc).join(" · ")}</span>`;
    banner.className = "doc-preview-banner ok wb-viewer-banner";
    banner.innerHTML = head([]);
    body.innerHTML = "";
    const img = new Image();
    img.alt = name;
    img.className = "wb-image";
    img.onload = () => { banner.innerHTML = head([img.naturalWidth ? `${img.naturalWidth} × ${img.naturalHeight}` : ""]); };
    img.onerror = () => { banner.className = "doc-preview-banner err"; banner.textContent = "This picture is no longer stored."; };
    img.src = url;
    const view = document.createElement("div");
    view.className = "wb-image-view fit";
    view.title = "Click to switch between fit and actual size";
    view.appendChild(img);
    view.addEventListener("click", () => view.classList.toggle("fit"));
    body.appendChild(view);
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `image · ${name}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `image · ${name}`;
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest?.(".shown-image-view");
    if (btn) wbPreviewShownImage(btn.dataset.url, btn.dataset.caption);
  });
  return { wbOpenWorkspaceFile, wbViewerBannerHtml, shownImagesHtml };
};
