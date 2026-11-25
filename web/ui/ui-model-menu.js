/* SPDX-License-Identifier: AGPL-3.0-only */
/* model menu: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["model-menu"] = ({
  $, esc, isMobile, loadModelByPath, shortenModelName, state,
}) => {
  // A rail of model families beside the list. Files of one model fold into a
  // row whose "N versions" chip lists them by bit level and size. Effort
  // lives in its own pill and slider, not here. Names, groups, families,
  // logos and bit levels come from /api/models (model_display_info in
  // bridge.py); a family without a logo shows its first letter.
  const mp = { family: "", query: "", open: new Set() };

  function wbModelGroups() {
    const groups = new Map();
    for (const m of state.modelsList || []) {
      const key = m.group || String(m.name || "").toLowerCase();
      if (!groups.has(key)) {
        groups.set(key, { key, name: m.display_name || shortenModelName(m.name), family: m.family || "other",
          familyLabel: m.family_label || "Other", familyLogo: m.family_logo || "", files: [] });
      }
      groups.get(key).files.push(m);
    }
    for (const g of groups.values()) g.files.sort((a, b) => (a.size || 0) - (b.size || 0));
    return [...groups.values()];
  }
  const wbLoadedPath = () => state.loadedModel || state.settings.model_path || "";
  const wbFileLoaded = (m) => m.path === wbLoadedPath() || !!m.loaded;
  const wbGigs = (bytes) => (bytes ? `${(bytes / 1073741824).toFixed(1)} GB` : "");
  const wbBitsLabel = (m) => [m.bits ? `${m.bits}-bit` : "", wbGigs(m.size)].filter(Boolean).join(" · ");
  // Five bars of precision: 2-bit lights one, 8-bit and up lights all five.
  function wbBars(bits) {
    const level = bits >= 8 ? 5 : bits >= 5 ? 4 : bits >= 4 ? 3 : bits >= 3 ? 2 : bits ? 1 : 0;
    return `<span class="wb-mp-bars" data-level="${level}" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i></span>`;
  }
  function wbFamilyGlyph(logo, label) {
    return /^[\w-]+$/.test(logo || "")
      ? `<span class="wb-mp-logo" style="--wb-logo:url('/assets/vendor/lobe-icons/${logo}.svg')" aria-hidden="true"></span>`
      : `<span class="wb-mp-letter" aria-hidden="true">${esc((label || "?").charAt(0).toUpperCase())}</span>`;
  }
  function wbDraftBadge(m) {
    const pair = state.draftPairs && state.draftPairs[m.path];
    return pair ? `<i class="ph-fill ph-lightning wb-mp-draft" title="DFlash draft paired: ${esc(pair.draft)}" aria-hidden="true"></i>` : "";
  }
  // The installed llama.cpp doesn't know this model's architecture (from
  // /api/models: llama_supported === false), so loading it can only fail.
  const wbTooOldText = (m) => m.needs_maya
    ? "Needs the Maya engine: this file is made for it, and llama.cpp can't run it correctly"
    : `Needs a newer llama.cpp: this build doesn't support ${m.architecture || "its architecture"}`;
  function wbTooOldBadge(m) {
    return m.llama_supported === false
      ? `<i class="ph ph-warning-circle wb-mp-old" title="${esc(wbTooOldText(m))}" role="img" aria-label="${esc(wbTooOldText(m))}"></i>` : "";
  }
  function wbModelGroupHtml(g, isLoaded) {
    const multi = g.files.length > 1;
    const single = g.files[0];
    const open = multi && mp.open.has(g.key);
    let html = `<div class="wb-mp-row${isLoaded ? " loaded" : ""}" data-mp-group="${esc(g.key)}">`
      + `<button type="button" class="wb-mp-pick" role="option" aria-selected="${isLoaded}"`
      + (multi ? ` data-mp-versions aria-expanded="${open}" title="${esc(g.name)}: ${g.files.length} files"`
        : ` data-mp-path="${esc(single.path)}" title="${esc(single.name)}"`)
      + `><span class="wb-mp-name">${esc(g.name)}</span>${multi ? "" : wbDraftBadge(single) + wbTooOldBadge(single)}</button>`
      + (multi
        ? `<button type="button" class="wb-mp-chip" data-mp-versions aria-expanded="${open}">${g.files.length} versions<i class="ph ph-caret-${open ? "down" : "right"}" aria-hidden="true"></i></button>`
        : `<span class="wb-mp-meta">${esc(wbBitsLabel(single))}</span>`)
      + `<span class="wb-mp-radio" aria-hidden="true"></span></div>`;
    if (open) {
      html += `<div class="wb-mp-versions" role="group" aria-label="${esc(g.name)} versions">`
        + g.files.map(m => {
          const on = wbFileLoaded(m);
          const tag = [...(m.quant_tags || []), m.quant || ""].filter(Boolean).join(" ");
          return `<button type="button" class="wb-mp-ver${on ? " loaded" : ""}" data-mp-path="${esc(m.path)}" role="option" aria-selected="${on}" title="${esc(m.name)}">`
            + `${wbBars(m.bits)}<span class="wb-mp-ver-name">${esc(wbBitsLabel(m))}</span>${wbDraftBadge(m)}${wbTooOldBadge(m)}`
            + `<span class="wb-mp-tag">${esc(tag)}</span><i class="ph ph-check wb-mp-check" aria-hidden="true"></i></button>`;
        }).join("") + `</div>`;
    }
    return html;
  }
  function wbRenderModelList() {
    const menu = $("#model-pill-menu");
    const shell = menu?.querySelector(".wb-mp");
    if (!shell) return;
    const groups = wbModelGroups();
    const families = new Map();
    for (const g of groups) {
      const f = families.get(g.family) || { key: g.family, label: g.familyLabel, logo: g.familyLogo, count: 0 };
      f.count += 1;
      families.set(g.family, f);
    }
    const fams = [...families.values()].sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
    const loaded = groups.find(g => g.files.some(wbFileLoaded));
    if (!families.has(mp.family)) mp.family = loaded?.family || fams[0]?.key || "";
    const q = mp.query.trim().toLowerCase();
    shell.querySelector(".wb-mp-rail").innerHTML = fams.map(f => {
      const on = !q && f.key === mp.family;
      return `<button type="button" class="wb-mp-fam${on ? " on" : ""}" data-mp-family="${esc(f.key)}" aria-pressed="${on}"`
        + ` aria-label="${esc(f.label)}" title="${esc(f.label)} · ${f.count}">${wbFamilyGlyph(f.logo, f.label)}</button>`;
    }).join("");
    const list = shell.querySelector(".wb-mp-list");
    const shown = q
      ? groups.filter(g => g.name.toLowerCase().includes(q) || g.files.some(m => String(m.name).toLowerCase().includes(q)))
      : groups.filter(g => g.family === mp.family);
    list.innerHTML = shown.map(g => wbModelGroupHtml(g, g === loaded)).join("")
      || `<div class="wb-mp-empty">${q ? "No models match that search" : "No models in this family"}</div>`;
  }
  function renderModelMenu() {
    const menu = $("#model-pill-menu");
    if (!menu) return;
    if (!(state.modelsList || []).length) {
      menu.innerHTML = `<div class="model-pill-menu-empty">${esc(state.modelsError || "pick a models folder in Settings")}</div>`;
      return;
    }
    if (!menu.querySelector(".wb-mp")) {
      menu.innerHTML = `<div class="wb-mp"><nav class="wb-mp-rail" aria-label="Model families"></nav><div class="wb-mp-main">`
        + `<div class="wb-mp-head"><span class="wb-mp-title">Models</span><label class="wb-mp-search">`
        + `<input type="search" placeholder="Quick search" aria-label="Search models" autocomplete="off" spellcheck="false">`
        + `<i class="ph ph-magnifying-glass" aria-hidden="true"></i></label></div>`
        + `<div class="wb-mp-list" role="listbox" aria-label="Models"></div></div></div>`;
      menu.querySelector(".wb-mp-search input").addEventListener("input", (e) => {
        mp.query = e.target.value;
        wbRenderModelList();
      });
    }
    wbRenderModelList();
  }

  // Model-pill dropdown toggle. Same positioning as wireOverflow (reparent to
  // <body> to escape composer-level backdrop-filter / transform, JS-position on
  // open), but right-aligned to the pill in the bottom-right corner.
  function wireModelMenu() {
    const btn = $("#model-pill");
    const menu = $("#model-pill-menu");
    if (!btn || !menu) return;
    if (menu.parentNode !== document.body) document.body.appendChild(menu);
    function positionMenu() {
      const r = btn.getBoundingClientRect();
      const margin = 8;
      const vw = window.innerWidth, vh = window.innerHeight;
      // Open on the roomier side and let the list scroll, so expanding a
      // model's versions never flips the menu over the composer.
      menu.style.maxHeight = "";
      const above = r.top - 6 - margin;
      const below = vh - r.bottom - 6 - margin;
      const up = menu.offsetHeight <= above || above >= below;
      menu.style.maxHeight = `min(460px, ${Math.max(160, Math.round(up ? above : below))}px)`;
      const mw = menu.offsetWidth || 240;
      const mh = menu.offsetHeight || 200;
      const top = up ? r.top - mh - 6 : r.bottom + 6;
      let left = r.right - mw;
      if (left + mw > vw - margin) left = vw - mw - margin;
      if (left < margin) left = margin;
      menu.style.top = `${Math.round(Math.max(margin, top))}px`;
      menu.style.left = `${Math.round(left)}px`;
    }
    const close = () => {
      menu.classList.remove("open");
      btn.classList.remove("open");
    };
    // Re-render the list in place and put focus back on the same control.
    const rerender = (focusSel) => {
      wbRenderModelList();
      positionMenu();
      if (focusSel) menu.querySelector(focusSel)?.focus({ preventScroll: true });
    };
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !menu.classList.contains("open");
      if (willOpen) {
        // Open on the loaded model's family, its versions showing.
        mp.query = "";
        mp.family = "";
        const loaded = wbModelGroups().find(g => g.files.some(wbFileLoaded));
        if (loaded && loaded.files.length > 1) mp.open.add(loaded.key);
        renderModelMenu();
        const input = menu.querySelector(".wb-mp-search input");
        if (input) input.value = "";
        menu.classList.add("open");
        btn.classList.add("open");
        positionMenu();
        requestAnimationFrame(positionMenu);
        if (input && !isMobile()) input.focus({ preventScroll: true });
      } else {
        close();
      }
    });
    menu.addEventListener("click", async (e) => {
      // The list re-renders under the click, so the target may already be
      // detached by the time the document handler would see it.
      e.stopPropagation();
      const t = e.target;
      const fam = t.closest("[data-mp-family]");
      if (fam) {
        mp.family = fam.dataset.mpFamily;
        mp.query = "";
        const input = menu.querySelector(".wb-mp-search input");
        if (input) input.value = "";
        return rerender(`[data-mp-family="${CSS.escape(mp.family)}"]`);
      }
      const row = t.closest("[data-mp-group]");
      const versions = t.closest("[data-mp-versions]") || (row && !t.closest("[data-mp-path]") && row.querySelector("[data-mp-versions]"));
      if (versions && row) {
        const key = row.dataset.mpGroup;
        if (mp.open.has(key)) mp.open.delete(key);
        else mp.open.add(key);
        return rerender(`[data-mp-group="${CSS.escape(key)}"] .wb-mp-pick`);
      }
      const pick = t.closest("[data-mp-path]") || row?.querySelector("[data-mp-path]");
      if (!pick) return;
      close();
      if (pick.dataset.mpPath === wbLoadedPath()) return;
      await loadModelByPath(pick.dataset.mpPath);
    });
    menu.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        const items = [...menu.querySelectorAll(".wb-mp-list button")];
        if (!items.length) return;
        e.preventDefault();
        const at = items.indexOf(document.activeElement);
        const step = e.key === "ArrowDown" ? 1 : -1;
        const next = at < 0 ? (step > 0 ? 0 : items.length - 1) : (at + step + items.length) % items.length;
        items[next].focus({ preventScroll: false });
      }
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && menu.classList.contains("open")) {
        close();
        btn.focus({ preventScroll: true });
      }
    });
    window.addEventListener("resize", () => { if (menu.classList.contains("open")) positionMenu(); });
    window.addEventListener("scroll", () => { if (menu.classList.contains("open")) positionMenu(); }, true);
  }
  return { wireModelMenu };
};
