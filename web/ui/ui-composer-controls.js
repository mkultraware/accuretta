/* SPDX-License-Identifier: AGPL-3.0-only */
/* composer: four controls: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["composer-controls"] = ({
  $, COMPOSER_MODES, activateSkill, clearActiveSkill, esc, isCompactViewport, loadSkills, state,
  stopStreaming,
}) => {
  // "+" (attachments, modes, build toggles, playbooks, skills), Access, the
  // model group (model, effort) and one send / stop / steer button. The
  // original controls stay in the DOM with their own handlers and the new ones
  // drive them, so behavior (goal mode, approvals, queueing, recon gates,
  // Deep Research briefs) is unchanged.
  function wbInsertAtCaret(text) {
    const input = $("#composer-input");
    if (!input) return;
    input.focus();
    const value = input.value;
    const pos = input.selectionStart ?? value.length;
    const before = value.slice(0, pos);
    const lead = before && !/\s$/.test(before) ? " " : "";
    input.value = `${before}${lead}${text}${value.slice(pos)}`;
    const caret = pos + lead.length + text.length;
    input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }
  function wbCloseAddMenu() {
    $("#wb-add-menu")?.classList.remove("open");
    $("#wb-add")?.setAttribute("aria-expanded", "false");
  }
  const WB_MODE_NOTES = { agent: "Works with your tools", ide: "Build with a live preview", research: "Investigate and present" };
  // Short notes for the playbook rows; the full description stays the tooltip.
  const WB_PLAYBOOK_NOTES = {
    "quick-netscan-mothership": "This machine's connections",
    "quick-passive-osint": "Public sources only",
    "quick-recon-target": "Scoped DNS, TLS, HTTP, ports",
    "quick-frontend-secrets": "Exposed keys in JavaScript",
    "quick-gain-access": "Recon, validate, report",
    "quick-access-control": "Two accounts, who sees what",
    "quick-health-check": "Read-only host triage",
    "quick-persistence-hunt": "Read-only startup sweep",
  };
  const WB_PLAYBOOK_TILES = { network: "blue", "red team": "red", "blue team": "teal" };
  // Playbooks (the quick-* launchers) come straight from the original overflow
  // menu, sections and all, so a new prompt added there shows up here too.
  function wbPlaybooks() {
    const out = [];
    let section = "";
    for (const el of $("#toolbar-overflow-menu")?.children || []) {
      if (el.classList.contains("overflow-section-label")) { section = el.textContent.trim(); continue; }
      if (!el.id || !el.id.startsWith("quick-")) continue;
      const icon = [...(el.querySelector("i.ph")?.classList || [])].find(c => c.startsWith("ph-")) || "ph-lightning";
      // The label is the button's own text; a "beta" marker inside it is carried
      // separately so the row can draw it as a badge.
      const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join("");
      out.push({ id: el.id, section, icon, label: text.trim().replace(/\s*\(read-only\)$/i, ""), title: el.title || "",
        beta: !!el.querySelector(".beta-badge") });
    }
    return out;
  }
  function wbMenuRow({ act = "", sub = "", back = "", icon, tile = "", label, note = "", role = "menuitem", checked = null, disabled = false, title = "", beta = false }) {
    const attrs = [`type="button"`, `class="wb-menu-item${checked ? " on" : ""}${back ? " wb-menu-back" : ""}"`, `role="${role}"`];
    if (checked !== null) attrs.push(`aria-checked="${checked}"`);
    if (disabled) attrs.push(`aria-disabled="true"`);
    if (act) attrs.push(`data-wb-add="${esc(act)}"`);
    if (sub) attrs.push(`data-wb-sub="${esc(sub)}"`, `aria-haspopup="menu"`);
    if (back) attrs.push(`data-wb-back="${esc(back)}"`);
    if (title) attrs.push(`title="${esc(title)}"`);
    const glyph = tile
      ? `<span class="wb-tile" data-tile="${esc(tile)}"><i class="ph ${icon}" aria-hidden="true"></i></span>`
      : `<i class="ph ${icon}" aria-hidden="true"></i>`;
    const trail = sub ? `<i class="ph ph-caret-right wb-menu-caret" aria-hidden="true"></i>`
      : checked !== null ? `<i class="ph ph-check wb-menu-check" aria-hidden="true"></i>` : "";
    return `<button ${attrs.join(" ")}>${glyph}<span class="wb-menu-text"><b>${esc(label)}${beta ? `<span class="beta-badge">beta</span>` : ""}</b>`
      + `${note ? `<small>${esc(note)}</small>` : ""}</span>${trail}</button>`;
  }
  // The menu drills in instead of opening a second floating layer: Playbooks
  // and Skills replace the main list, with a back row on top.
  function wbRenderAddMenu(view = "main") {
    const menu = $("#wb-add-menu");
    if (!menu) return;
    menu.dataset.view = view;
    if (view === "playbooks") {
      let html = wbMenuRow({ back: "playbooks", icon: "ph-caret-left", label: "Playbooks" });
      let section = null;
      for (const p of wbPlaybooks()) {
        if (p.section !== section) {
          section = p.section;
          if (section) html += `<div class="wb-menu-label">${esc(section)}</div>`;
        }
        html += wbMenuRow({ act: `quick:${p.id}`, icon: p.icon, tile: WB_PLAYBOOK_TILES[section.toLowerCase()] || "gray",
          label: p.label, note: WB_PLAYBOOK_NOTES[p.id] || "", title: p.title, beta: p.beta });
      }
      menu.innerHTML = html;
      return;
    }
    if (view === "skills") {
      const active = state.activeSkill?.name || "";
      let html = wbMenuRow({ back: "skills", icon: "ph-caret-left", label: "Skills" });
      if (active) html += wbMenuRow({ act: "skill-clear", icon: "ph-x-circle", label: `Unload ${active}` });
      if (!state.skills) html += `<div class="wb-menu-empty">Loading skills…</div>`;
      else if (!state.skills.length) html += `<div class="wb-menu-empty">No skills yet. Add them in the skills folder.</div>`;
      else html += state.skills.map(s => wbMenuRow({ act: `skill:${s.name}`, icon: "ph-book-open-text", tile: "violet",
        label: s.name, note: String(s.description || "").split(/(?<=\.)\s/)[0], title: s.description || "",
        role: "menuitemradio", checked: s.name === active })).join("");
      menu.innerHTML = html;
      return;
    }
    const tw = $("#toggle-tailwind");
    const mf = $("#toggle-multifile");
    const mode = state.mode || "agent";
    const narrow = isCompactViewport();
    let html = `<div class="wb-menu-label">Add</div>`
      + wbMenuRow({ act: "file", icon: "ph-paperclip", label: "Files", note: "Upload to the workspace" })
      + wbMenuRow({ act: "image", icon: "ph-image", label: "Image", note: state.visionCapable ? "The model sees it" : "Described by the vision model" })
      + wbMenuRow({ act: "mention", icon: "ph-at", label: "Workspace file", note: "Reference with @" })
      + `<div class="wb-menu-label">Mode</div>`
      + Object.entries(COMPOSER_MODES).map(([key, meta]) => wbMenuRow({ act: `mode:${key}`, icon: meta.icon, label: meta.label,
        note: WB_MODE_NOTES[key] || "", role: "menuitemradio", checked: mode === key, disabled: key === "ide" && narrow,
        title: key === "ide" && narrow ? "IDE mode needs a wider window for the preview pane" : "" })).join("");
    if (mode === "ide" && (tw || mf)) {
      html += `<div class="wb-menu-label">Build</div>`
        + (tw ? wbMenuRow({ act: "tailwind", icon: "ph-wind", label: "Tailwind CDN", role: "menuitemcheckbox", checked: tw.classList.contains("on") }) : "")
        + (mf ? wbMenuRow({ act: "multifile", icon: "ph-folder-simple", label: "Multi-file output", role: "menuitemcheckbox", checked: mf.classList.contains("on") }) : "");
    }
    html += `<div class="wb-menu-sep"></div>`
      + (wbPlaybooks().length ? wbMenuRow({ sub: "playbooks", icon: "ph-strategy", tile: "coral", label: "Playbooks", note: "Guided security jobs" }) : "")
      + wbMenuRow({ sub: "skills", icon: "ph-book-open-text", tile: "violet", label: "Skills",
        note: state.activeSkill ? `${state.activeSkill.name} is loaded` : "Load a procedure" });
    menu.innerHTML = html;
  }
  function wbAddMenuView(view, focusSub = "") {
    const menu = $("#wb-add-menu");
    if (!menu) return;
    wbRenderAddMenu(view);
    wbPositionAddMenu();
    const target = focusSub ? menu.querySelector(`[data-wb-sub="${focusSub}"]`)
      : menu.querySelector(".wb-menu-item:not(.wb-menu-back)") || menu.querySelector(".wb-menu-item");
    target?.focus({ preventScroll: true });
    if (view === "skills" && (!state.skills || Date.now() - (state.skillsAt || 0) > 20000)) {
      loadSkills().then(() => { if (menu.dataset.view === "skills" && menu.classList.contains("open")) wbAddMenuView("skills"); });
    }
  }
  function wbPositionAddMenu() {
    const btn = $("#wb-add");
    const menu = $("#wb-add-menu");
    if (!btn || !menu) return;
    const r = btn.getBoundingClientRect();
    // Open on the roomier side and scroll inside it, so a long list (Skills)
    // never flips over the composer.
    menu.style.maxHeight = "";
    const above = r.top - 16;
    const below = window.innerHeight - r.bottom - 16;
    const up = menu.offsetHeight <= above || above >= below;
    menu.style.maxHeight = `min(520px, ${Math.max(160, Math.round(up ? above : below))}px)`;
    const mh = menu.offsetHeight || 300;
    const mw = menu.offsetWidth || 260;
    const top = up ? r.top - mh - 8 : r.bottom + 8;
    let left = Math.min(r.left, window.innerWidth - mw - 8);
    menu.style.top = `${Math.max(8, Math.round(top))}px`;
    menu.style.left = `${Math.max(8, Math.round(left))}px`;
  }
  function wbInitAddMenu() {
    const tools = $(".composer-tools");
    if (!tools || $("#wb-add")) return;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.id = "wb-add";
    btn.className = "wb-add";
    btn.title = "Add files, switch mode, run a playbook or load a skill";
    btn.setAttribute("aria-label", "Add");
    btn.setAttribute("aria-haspopup", "menu");
    btn.setAttribute("aria-expanded", "false");
    btn.innerHTML = '<i class="ph ph-plus" aria-hidden="true"></i>';
    tools.prepend(btn);
    const menu = document.createElement("div");
    menu.id = "wb-add-menu";
    menu.className = "wb-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", "Add");
    document.body.appendChild(menu);
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu.classList.contains("open")) return wbCloseAddMenu();
      menu.classList.add("open");
      btn.setAttribute("aria-expanded", "true");
      wbAddMenuView("main");
      requestAnimationFrame(wbPositionAddMenu);
    });
    menu.addEventListener("click", (e) => {
      e.stopPropagation();
      const item = e.target.closest(".wb-menu-item");
      if (!item) return;
      if (item.dataset.wbSub) return wbAddMenuView(item.dataset.wbSub);
      if (item.dataset.wbBack) return wbAddMenuView("main", item.dataset.wbBack);
      const act = item.dataset.wbAdd || "";
      if (act === "tailwind" || act === "multifile") {
        // Toggles stay open so both can be flipped in one visit.
        document.getElementById(act === "tailwind" ? "toggle-tailwind" : "toggle-multifile")?.click();
        setTimeout(() => { if (menu.classList.contains("open")) wbRenderAddMenu("main"); }, 250);
        return;
      }
      wbCloseAddMenu();
      if (act.startsWith("mode:")) {
        // The original option runs the mode switch: the IDE width check, saving
        // the choice, and the Deep Research brief.
        document.querySelector(`#composer-mode-menu [data-composer-mode="${act.slice(5)}"]`)?.click();
        if (act !== "mode:research") $("#composer-input")?.focus();
      } else if (act.startsWith("quick:")) {
        document.getElementById(act.slice(6))?.click();
      } else if (act.startsWith("skill:")) {
        const skill = (state.skills || []).find(s => s.name === act.slice(6));
        if (skill && skill.name !== state.activeSkill?.name) activateSkill(skill);
      } else if (act === "skill-clear") clearActiveSkill();
      else if (act === "image") $("#btn-attach-image")?.click();
      else if (act === "file") $("#btn-attach-file")?.click();
      else if (act === "mention") wbInsertAtCaret("@");
    });
    menu.addEventListener("keydown", (e) => {
      const items = [...menu.querySelectorAll(".wb-menu-item")];
      const at = items.indexOf(document.activeElement);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        const next = e.key === "ArrowDown" ? (at + 1) % items.length : (at - 1 + items.length) % items.length;
        items[next]?.focus({ preventScroll: true });
      } else if (e.key === "ArrowRight" && document.activeElement?.dataset.wbSub) {
        e.preventDefault();
        wbAddMenuView(document.activeElement.dataset.wbSub);
      } else if (e.key === "ArrowLeft" && menu.dataset.view !== "main") {
        e.preventDefault();
        wbAddMenuView("main", menu.dataset.view);
      } else if (e.key === "Escape") {
        e.preventDefault();
        wbCloseAddMenu();
        btn.focus({ preventScroll: true });
      }
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && !btn.contains(e.target)) wbCloseAddMenu();
    });
    window.addEventListener("resize", () => { if (menu.classList.contains("open")) wbPositionAddMenu(); });
  }

  // Mode chip: Agent is the default and shows nothing; IDE and Deep Research
  // show as a chip after the "+" whose × goes back to Agent.
  function wbSyncModeChip() {
    const chip = $("#wb-mode-chip");
    if (!chip) return;
    const mode = state.mode || "agent";
    const meta = COMPOSER_MODES[mode];
    chip.hidden = mode === "agent" || !meta;
    if (chip.hidden) return;
    chip.querySelector(".wb-mode-chip-icon").className = `ph ${meta.icon} wb-mode-chip-icon`;
    chip.querySelector(".wb-mode-chip-label").textContent = meta.label;
    const x = chip.querySelector(".wb-mode-chip-x");
    x.setAttribute("aria-label", `Leave ${meta.label} and go back to Agent`);
    x.title = "Back to Agent";
  }
  function wbInitModeChip() {
    const add = $("#wb-add");
    if (!add || $("#wb-mode-chip")) return;
    const chip = document.createElement("span");
    chip.id = "wb-mode-chip";
    chip.className = "wb-mode-chip";
    chip.hidden = true;
    chip.innerHTML = '<i class="ph wb-mode-chip-icon" aria-hidden="true"></i><span class="wb-mode-chip-label"></span>'
      + '<button type="button" class="wb-mode-chip-x"><i class="ph ph-x" aria-hidden="true"></i></button>';
    add.after(chip);
    chip.querySelector(".wb-mode-chip-x").addEventListener("click", (e) => {
      e.stopPropagation();
      document.querySelector('#composer-mode-menu [data-composer-mode="agent"]')?.click();
      $("#composer-input")?.focus();
    });
    wbSyncModeChip();
  }

  // Model group: model and effort sit side by side; the effort pill opens
  // the effort slider and the model menu lists models only. The context ring
  // lives at the right end of the strip above the box, with its percentage.
  function wbInitModelGroup() {
    const pill = $("#model-pill");
    if (!pill || pill.closest(".wb-model-group")) return;
    const group = document.createElement("div");
    group.className = "wb-model-group";
    pill.before(group);
    const ring = $("#ctx-gauge");
    const strip = $(".composer-projects");
    const effort = $("#reasoning-effort-control");
    if (ring && strip) strip.appendChild(ring);
    else if (ring) group.appendChild(ring);
    group.appendChild(pill);
    if (effort) group.appendChild(effort);
  }
  // One action button: dim send when there is nothing to send, send, stop
  // while a turn runs, and a split Steer | stop when you type mid-turn.
  function wbComposerHasContent() {
    const text = ($("#composer-input")?.value || "").trim();
    const tray = $("#image-tray");
    return !!text || !!(tray && !tray.classList.contains("hidden") && tray.children.length);
  }
  function wbSyncAction() {
    const act = $("#wb-act");
    if (!act) return;
    const busy = !!state.streaming;
    const has = wbComposerHasContent();
    const next = busy ? (has ? "steer" : "stop") : (has ? "send" : "empty");
    if (act.dataset.state !== next) act.dataset.state = next;
    const send = $("#btn-send");
    if (send) {
      const label = next === "steer" ? "Steer: send this as a correction (Enter)"
        : next === "empty" ? "Type a message to send" : "Send (Enter)";
      if (send.title !== label) send.title = label;
    }
    const stop = $("#btn-stop");
    if (stop && stop.title !== "Stop (Esc)") stop.title = "Stop (Esc)";
  }
  function wbInitAction() {
    const send = $("#btn-send");
    const stop = $("#btn-stop");
    if (!send || !stop || send.closest(".wb-act")) return;
    const act = document.createElement("div");
    act.id = "wb-act";
    act.className = "wb-act";
    send.before(act);
    act.append(send, stop);
    if (!send.querySelector(".wb-steer-label")) {
      send.insertAdjacentHTML("beforeend",
        '<i class="ph-bold ph-arrow-bend-up-right wb-steer-icon" aria-hidden="true"></i><b class="wb-steer-label">Steer</b>');
    }
    // While a turn runs, the outline and the arc are both SVG strokes with the
    // same geometry, so they coincide (a CSS border snaps to whole pixels and
    // drifted half a pixel from the arc at the corners).
    if (!stop.querySelector(".wb-ring")) {
      stop.insertAdjacentHTML("beforeend",
        '<svg class="wb-ring" aria-hidden="true" focusable="false"><rect class="wb-ring-track"></rect><rect class="wb-ring-arc" pathLength="100"></rect></svg>');
    }
    const input = $("#composer-input");
    input?.addEventListener("input", wbSyncAction);
    input?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) setTimeout(wbSyncAction, 30);
      // Esc stops a running turn, unless a picker or menu is using Esc.
      if (e.key === "Escape" && state.streaming && !e.defaultPrevented
          && $("#mention-menu")?.classList.contains("hidden") !== false) {
        e.preventDefault();
        stopStreaming();
      }
    });
    send.addEventListener("click", () => setTimeout(wbSyncAction, 30));
    const tray = $("#image-tray");
    if (tray) new MutationObserver(wbSyncAction).observe(tray, { attributes: true, childList: true });
    wbSyncAction();
  }
  function wbInitComposer() {
    document.body.classList.add("wb-composer");
    wbInitAddMenu();
    wbInitModeChip();
    wbInitModelGroup();
    wbInitAction();
  }
  return { wbSyncModeChip, wbSyncAction, wbInitComposer };
};
