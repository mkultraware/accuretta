/* SPDX-License-Identifier: AGPL-3.0-only */
/* mobile preview card: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["mobile-preview"] = ({
  $, api, buildEffortSlider, effortSliderOpened, esc, humanBytes, isCompactViewport, isMobile,
  persistUiPreference, scrollToBottom, send, state, syncEffortSliders, toast, wbSyncModeChip,
}) => {
  // The preview pane is hidden on mobile; inject a tappable artifact card into
  // the chat that opens the HTML in a new tab. Takes a raw HTML string (blob URL)
  // or a wsfs URL for workspace files.
  function injectMobilePreviewCard(opts) {
    if (!isMobile()) return;
    const { filename, size, url, html } = opts || {};
    const name = filename || "index.html";
    const sizeText = size ? humanBytes(size) : "";
    const card = document.createElement("div");
    card.className = "mobile-preview-card";
    card.innerHTML = `
      <div class="mobile-preview-card-icon"><i class="ph ph-browser"></i></div>
      <div class="mobile-preview-card-body">
        <div class="mobile-preview-card-title">${esc(name)}</div>
        <div class="mobile-preview-card-meta">Tap to preview${sizeText ? " · " + sizeText : ""}</div>
      </div>
      <div class="mobile-preview-card-arrow"><i class="ph ph-arrow-square-out"></i></div>`;
    card.addEventListener("click", () => {
      if (url) {
        window.open(url, "_blank");
      } else if (html) {
        const blob = new Blob([html], { type: "text/html" });
        window.open(URL.createObjectURL(blob), "_blank");
      }
    });
    // Insert into the chat flow at the end of the current messages
    const chatInner = $("#chat-inner");
    if (chatInner) {
      chatInner.appendChild(card);
      scrollToBottom(true);
    }
  }

  // Shorten a GGUF filename like "qwen2.5-coder-32b-instruct-q4_k_m.gguf" into a
  // display label like "Qwen2.5 Coder 32B": family name, optional
  // coder/instruct/chat tag and parameter count; quant suffixes, revisions and
  // packaging cruft dropped. Falls back to the raw stem.
  function shortenModelName(filename) {
    if (!filename) return "";
    let stem = String(filename).split(/[\\/]/).pop();
    // strip extension
    stem = stem.replace(/\.gguf$|\.bin$|\.safetensors$/i, "");
    // strip common quant + packaging suffixes (everything from -q?_? onward,
    // -imat, -kquants, -gguf, etc.)
    stem = stem.replace(/[._-](?:q\d[a-z0-9_]*|iq\d[a-z0-9_]*|f16|fp16|f32|bf16)\b.*$/i, "");
    stem = stem.replace(/[._-](?:imat|kquants?|gguf|ggml|hf|fixed|fix|merged|abliterated)\b.*$/i, "");
    // collapse separators to spaces
    let parts = stem.split(/[._\-\s]+/).filter(Boolean);
    // keep at most ~5 segments — anything beyond that is usually metadata.
    // We also re-capitalize each segment so "qwen2.5" → "Qwen2.5", "32b" → "32B".
    parts = parts.slice(0, 5).map(p => {
      // parameter count: "32b" / "8x7b" / "1.5b" → uppercase B
      if (/^\d+(?:\.\d+)?b$/i.test(p) || /^\d+x\d+(?:\.\d+)?b$/i.test(p)) return p.toUpperCase();
      // moe-like "a3b": uppercase
      if (/^a\d+(?:\.\d+)?b$/i.test(p)) return p.toUpperCase();
      // bare numbers stay
      if (/^\d/.test(p)) return p.charAt(0).toUpperCase() + p.slice(1);
      // word: capitalize first letter only
      return p.charAt(0).toUpperCase() + p.slice(1).toLowerCase();
    });
    const out = parts.join(" ").trim();
    return out || stem;
  }

  function renderModelPill() {
    const pill = $("#model-pill");
    const nameEl = pill.querySelector(".model-pill-name") || pill;
    const loadedPath = state.missingModel && !state.loadedModel
      ? "" : (state.loadedModel || state.settings.model_path || state.settings.model || "");
    if (state.missingModel && !state.loadedModel) {
      nameEl.textContent = "select model";
      pill.title = `${String(state.missingModel).split(/[\\/]/).pop()} is no longer on disk — click to pick another model`;
    } else if (loadedPath) {
      const fullName = String(loadedPath).split(/[\\/]/).pop();
      const entry = (state.modelsList || []).find(m => m.path === loadedPath);
      const shortName = entry?.display_name || shortenModelName(fullName);
      nameEl.textContent = state._reloading ? `${shortName} …` : shortName;
      pill.title = state._reloading
        ? `${fullName} — reloading with new settings…`
        : `${fullName} — click to change model`;
    } else if (state.models && state.models.length) {
      nameEl.textContent = "select model";
      pill.title = "Click to pick a model";
    } else {
      nameEl.textContent = "no models";
      pill.title = state.modelsError || "Pick a models folder in Settings";
    }
    // Vision badge: a small eye chip on the pill when the loaded model has its own
    // vision tower (mmproj), meaning images go straight to the chat model.
    let badge = pill.querySelector(".model-pill-vision");
    const wantBadge = !!state.visionCapable && !!loadedPath;
    if (wantBadge && !badge) {
      badge = document.createElement("span");
      badge.className = "model-pill-vision";
      badge.innerHTML = '<i class="ph ph-eye"></i>';
      // Insert before the caret so the order reads name → badge → caret.
      const caret = pill.querySelector(".model-pill-caret");
      if (caret) pill.insertBefore(badge, caret);
      else pill.appendChild(badge);
    } else if (!wantBadge && badge) {
      badge.remove();
      badge = null;
    }
    if (badge) {
      const mm = state.loadedMmproj ? String(state.loadedMmproj).split(/[\\/]/).pop() : "mmproj";
      badge.title = `vision: native — ${mm}`;
    }
  }

  const COMPOSER_MODES = {
    agent: { label: "Agent", icon: "ph-terminal-window" },
    ide: { label: "IDE", icon: "ph-code" },
    research: { label: "Deep Research", icon: "ph-books" },
  };

  function setComposerMode(mode, { announce = false, persist = false } = {}) {
    mode = String(mode || "agent").toLowerCase();
    if (mode === "auto") mode = "agent";
    if (!Object.prototype.hasOwnProperty.call(COMPOSER_MODES, mode)) mode = "agent";
    if (mode === "ide" && isCompactViewport()) {
      if (announce) toast("IDE mode needs a wider window for the preview pane.", "info", 2600);
      mode = "agent";
    }
    state.mode = mode;
    $("#composer-input")?.setAttribute("placeholder", mode === "research" ? "What would you like to research?" : "ask, build, or instruct…");
    const meta = COMPOSER_MODES[mode];
    document.querySelectorAll("#composer-mode-menu .mode-option").forEach(option => {
      const active = option.dataset.composerMode === mode;
      option.classList.toggle("active", active);
      option.setAttribute("aria-checked", String(active));
      option.tabIndex = active ? 0 : -1;
    });
    const value = $("#composer-mode-value");
    const popoverValue = $("#composer-mode-popover-value");
    const icon = $("#composer-mode-icon");
    const pill = $("#composer-mode-pill");
    if (value) value.textContent = meta.label;
    if (popoverValue) popoverValue.textContent = meta.label;
    if (icon) icon.className = `ph ${meta.icon} mode-pill-icon`;
    if (pill) pill.setAttribute("aria-label", `Composer mode: ${meta.label}`);
    wbSyncModeChip();
    if (persist) {
      persistUiPreference({ composer_mode: mode });
      const chat = state.chats?.chats?.[state.chatId];
      if (chat) chat.last_mode = mode;
      if (state.chatId) {
        api(`/api/chats/${encodeURIComponent(state.chatId)}/mode`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        }).catch(error => console.warn("chat mode could not be saved:", error));
      }
    }
  }

  function renderComposerMode() {
    const ideOption = document.querySelector('#composer-mode-menu [data-composer-mode="ide"]');
    if (ideOption) {
      const unavailable = isCompactViewport();
      ideOption.setAttribute("aria-disabled", String(unavailable));
      ideOption.classList.toggle("disabled", unavailable);
      ideOption.title = unavailable ? "IDE mode needs a wider window for the preview pane" : "Build and preview an interface";
    }
    setComposerMode(state.mode);
  }

  function wireComposerMode() {
    const control = $("#composer-mode-control");
    const pill = $("#composer-mode-pill");
    const popover = $("#composer-mode-popover");
    const menu = $("#composer-mode-menu");
    if (!control || !pill || !popover || !menu) return;
    const close = () => {
      popover.classList.remove("open");
      pill.setAttribute("aria-expanded", "false");
    };
    const options = () => [...menu.querySelectorAll(".mode-option")];
    const focusOption = option => {
      if (!option) return;
      options().forEach(item => { item.tabIndex = item === option ? 0 : -1; });
      option.focus({ preventScroll: true });
    };
    const openMenu = focusLast => {
      popover.classList.add("open");
      pill.setAttribute("aria-expanded", "true");
      const items = options();
      focusOption(focusLast ? items[items.length - 1]
        : menu.querySelector(".mode-option.active") || items[0]);
    };
    pill.addEventListener("click", event => {
      event.stopPropagation();
      const open = !popover.classList.contains("open");
      if (open) openMenu(false);
      else close();
    });
    pill.addEventListener("keydown", event => {
      if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
      event.preventDefault();
      openMenu(event.key === 'ArrowUp');
    });
    menu.addEventListener("click", event => {
      const option = event.target.closest(".mode-option");
      if (!option) return;
      if (option.getAttribute("aria-disabled") === "true") {
        toast("IDE mode needs a wider window for the preview pane.", "info", 2600);
        focusOption(option);
        return;
      }
      setComposerMode(option.dataset.composerMode, { announce: true, persist: true });
      close();
      pill.focus({ preventScroll: true });
      if (option.dataset.composerMode === "research") send({ mode: "research" });
    });
    menu.addEventListener("keydown", event => {
      if (event.key === "Tab") {
        setTimeout(close, 0);
        return;
      }
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const items = options();
      const current = Math.max(0, items.indexOf(document.activeElement));
      let next = current;
      if (event.key === "ArrowDown") next = (current + 1) % items.length;
      if (event.key === "ArrowUp") next = (current - 1 + items.length) % items.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = items.length - 1;
      focusOption(items[next]);
    });
    control.addEventListener("focusout", () => {
      setTimeout(() => { if (!control.contains(document.activeElement)) close(); }, 0);
    });
    document.addEventListener("click", event => {
      if (!control.contains(event.target)) close();
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && popover.classList.contains("open")) {
        close();
        pill.focus({ preventScroll: true });
      }
    });
    window.addEventListener("resize", () => {
      renderComposerMode();
      close();
    });
    renderComposerMode();
  }

  const REASONING_EFFORT_KEY = "accuretta:reasoning-effort:v1";
  // Effort names ranked low to high. A model with its own levels (Qwen3.8:
  // low, medium, xhigh) shows exactly those; others get low, medium, high.
  const EFFORT_RANK = { minimal: 1, low: 2, medium: 3, high: 4, xhigh: 5, max: 6 };
  const EFFORT_NAMES = { auto: "Default", minimal: "Minimal", low: "Low", medium: "Medium", high: "High", xhigh: "xHigh", max: "Max" };
  const EFFORT_ICONS = { auto: "ph-magic-wand", minimal: "ph-leaf", low: "ph-leaf", medium: "ph-lightning", high: "ph-fire", xhigh: "ph-fire", max: "ph-fire" };

  function effortLevels() {
    const cap = state.reasoningCapability || {};
    const native = cap.mode === "native_effort" && Array.isArray(cap.native_levels)
      ? cap.native_levels.filter(l => EFFORT_RANK[l]) : [];
    return ["auto", ...(native.length >= 2 ? native : ["low", "medium", "high"])];
  }

  function effortLabel(level) {
    return EFFORT_NAMES[level] || String(level || "");
  }

  // A saved choice the current model lacks maps to its closest level (a tie
  // goes deeper): "high" becomes xHigh on Qwen3.8, "xhigh" becomes High elsewhere.
  function fitEffortLevel(level, levels) {
    level = String(level || "auto").toLowerCase();
    if (level === "deep") level = "high";
    if (levels.includes(level)) return level;
    const rank = EFFORT_RANK[level];
    if (!rank) return "auto";
    let best = "auto";
    let bestKey = null;
    for (const l of levels) {
      if (!EFFORT_RANK[l]) continue;
      const key = [Math.abs(EFFORT_RANK[l] - rank), -EFFORT_RANK[l]];
      if (!bestKey || key[0] < bestKey[0] || (key[0] === bestKey[0] && key[1] < bestKey[1])) {
        best = l;
        bestKey = key;
      }
    }
    return best;
  }

  function renderEffortMenu(levels) {
    const menu = $("#reasoning-effort-menu");
    if (!menu) return;
    const sig = levels.join(",");
    if (menu.dataset.levels === sig) return;
    menu.dataset.levels = sig;
    menu.innerHTML = levels.map(l =>
      `<button type="button" class="effort-option" role="menuitemradio" aria-checked="false" data-level="${esc(l)}">`
      + `<i class="ph ${EFFORT_ICONS[l] || "ph-brain"}" aria-hidden="true"></i><span>${esc(effortLabel(l))}</span>`
      + `<i class="ph ph-check effort-check" aria-hidden="true"></i></button>`).join("");
  }

  function reasoningModelKey() {
    return String(state.loadedModel || state.settings.model_path || state.settings.model || "")
      .split(/[\\/]/).pop().toLowerCase();
  }

  function readReasoningEffortMap() {
    try {
      const data = JSON.parse(localStorage.getItem(REASONING_EFFORT_KEY) || "{}");
      return data && typeof data === "object" ? data : {};
    } catch (_) { return {}; }
  }

  function syncThinkingBudgetControl() {
    const row = $("#thinking-budget-row");
    const input = $("#set-think-budget");
    const hint = $("#thinking-budget-hint");
    if (!row || !input || !hint) return;
    const cap = state.reasoningCapability || {};
    const nativeOverride = cap.mode === "native_effort" && state.reasoningEffort !== "auto";
    input.disabled = nativeOverride;
    row.classList.toggle("is-overridden", nativeOverride);
    if (nativeOverride) {
      const level = effortLabel(state.reasoningEffort) || "Native effort";
      hint.textContent = `${level} uses this model's native effort control. Your saved numeric budget remains available for Default effort and budget-controlled models.`;
    } else if (cap.mode === "native_effort") {
      const names = effortLevels().slice(1).map(effortLabel).join(", ");
      hint.textContent = `Default effort uses this saved target. Choosing ${names} gives control to the model's native effort setting.`;
    } else {
      hint.textContent = "Used for Default effort and models that expose a numeric reasoning budget.";
    }
  }

  function setReasoningEffort(level, persist = true) {
    level = fitEffortLevel(level, effortLevels());
    state.reasoningEffort = level;
    if (persist) {
      const key = reasoningModelKey();
      if (key) {
        const saved = readReasoningEffortMap();
        saved[key] = level;
        try { localStorage.setItem(REASONING_EFFORT_KEY, JSON.stringify(saved)); } catch (_) {}
      }
      persistUiPreference({ reasoning_effort: level });
    }
    const label = effortLabel(level);
    document.querySelectorAll("#reasoning-effort-menu .effort-option").forEach(opt => {
      const active = opt.dataset.level === level;
      opt.classList.toggle("active", active);
      opt.setAttribute("aria-checked", String(active));
    });
    const pillValue = $("#reasoning-effort-value");
    const popValue = $("#reasoning-effort-popover-value");
    if (pillValue) pillValue.textContent = label;
    if (popValue) popValue.textContent = label;
    const pill = $("#reasoning-effort-pill");
    if (pill) pill.setAttribute("aria-label", `Reasoning effort: ${label}`);
    const control = $("#reasoning-effort-control");
    if (control) {
      control.classList.toggle("effort-active", level !== "auto");
      control.classList.remove("is-changing");
      requestAnimationFrame(() => control.classList.add("is-changing"));
      setTimeout(() => control.classList.remove("is-changing"), 260);
    }
    syncThinkingBudgetControl();
    syncEffortSliders();
  }

  function renderReasoningEffort() {
    const control = $("#reasoning-effort-control");
    if (!control) return;
    const cap = state.reasoningCapability || {};
    const supported = !!cap.supported && cap.mode !== "none" && !!reasoningModelKey();
    if (!supported) {
      state.reasoningEffort = "auto";
      control.classList.add("hidden");
      $("#reasoning-effort-popover")?.classList.remove("open");
      $("#reasoning-effort-pill")?.setAttribute("aria-expanded", "false");
      syncThinkingBudgetControl();
      syncEffortSliders();
      return;
    }
    const wasHidden = control.classList.contains("hidden");
    control.classList.remove("hidden");
    control.dataset.mode = cap.mode || "budget";
    renderEffortMenu(effortLevels());
    const saved = readReasoningEffortMap();
    const durable = String(state.settings.reasoning_effort || "").toLowerCase();
    const restored = EFFORT_NAMES[durable]
      ? durable : (saved[reasoningModelKey()] || "auto");
    setReasoningEffort(restored, false);
    const pill = $("#reasoning-effort-pill");
    if (pill) pill.title = cap.description || "Reasoning effort for the next message";
    if (wasHidden) {
      control.classList.add("effort-arrive");
      setTimeout(() => control.classList.remove("effort-arrive"), 420);
    }
  }

  function wireReasoningEffort() {
    const control = $("#reasoning-effort-control");
    const pill = $("#reasoning-effort-pill");
    const popover = $("#reasoning-effort-popover");
    const menu = $("#reasoning-effort-menu");
    if (!control || !pill || !popover || !menu) return;
    // The popover shows the effort slider; the hidden list stays as the
    // record of levels that setReasoningEffort keeps in sync.
    const slider = buildEffortSlider({ isShown: () => popover.classList.contains("open") });
    popover.classList.add("has-slider");
    popover.appendChild(slider.el);
    const close = () => {
      popover.classList.remove("open");
      pill.setAttribute("aria-expanded", "false");
    };
    pill.addEventListener("click", (event) => {
      event.stopPropagation();
      const open = !popover.classList.contains("open");
      popover.classList.toggle("open", open);
      pill.setAttribute("aria-expanded", String(open));
      if (open) {
        effortSliderOpened(slider);
        slider.el.querySelector(".fx-effort-track")?.focus({ preventScroll: true });
      }
    });
    menu.addEventListener("click", (event) => {
      const opt = event.target.closest(".effort-option");
      if (!opt) return;
      setReasoningEffort(opt.dataset.level);
      close();
      pill.focus({ preventScroll: true });
    });
    // Arrow keys walk the menu like a native select list
    menu.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      event.preventDefault();
      const opts = [...menu.querySelectorAll(".effort-option")];
      const cur = opts.indexOf(document.activeElement);
      const next = event.key === "ArrowDown"
        ? Math.min(opts.length - 1, cur + 1)
        : Math.max(0, cur - 1);
      opts[next]?.focus({ preventScroll: true });
    });
    document.addEventListener("click", (event) => {
      if (!control.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && popover.classList.contains("open")) {
        close();
        pill.focus();
      }
    });
    renderReasoningEffort();
  }
  return {
    injectMobilePreviewCard, shortenModelName, renderModelPill, COMPOSER_MODES, setComposerMode,
    wireComposerMode, effortLevels, effortLabel, syncThinkingBudgetControl, setReasoningEffort,
    renderReasoningEffort, wireReasoningEffort,
  };
};
