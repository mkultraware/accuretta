/* SPDX-License-Identifier: AGPL-3.0-only */
/* utilities: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["utilities"] = ({
  $, api, app, isMobile, refreshContextStats, renderStatus, shortenModelName, state, wbLiveModel,
}) => {
  // Composer notification tabs. Keyed notices replace an older copy.
  const _toasts = new Map();

  function showDeckToast(msg, kind, ms = 3000, key = null, html = false) {
    const deck = $("#revealer-deck");
    if (!deck) return null;

    const heightBefore = _deckHeightBefore(deck);
    const previous = key ? _toasts.get(key) : null;
    const row = previous?.isConnected ? previous : document.createElement("div");
    clearTimeout(row._dismissTimer);
    clearTimeout(row._removeTimer);
    row.className = `revealer-card notifications ${kind}`;
    row.dataset.cardType = "notifications";

    const dotKind = kind === "error" || kind === "err" ? "err"
      : kind === "warn" ? "warn"
      : kind === "ok" ? "ok" : "live";
    const labelMap = { info: "Agent", ok: "Done", warn: "Attention", err: "Notice" };
    const badgeText = labelMap[kind] || "Agent";
    const cleanMsg = html ? String(msg) : esc(String(msg));

    row.innerHTML = `
      <div class="status-tab-primary" style="width:100%;padding:0;">
        <div class="status-tab-left">
          <span class="status-dot ${dotKind}"></span>
          <span class="status-env-badge env-agent">${badgeText}</span>
          <span class="status-task-title" title="${cleanMsg.replace(/<[^>]*>/g, '')}">${cleanMsg}</span>
        </div>
        <div class="status-tab-right">
          <button type="button" class="status-action-btn" data-act="dismiss">Dismiss</button>
        </div>
      </div>
    `;

    const removeRow = () => {
      row.remove();
      if (key && _toasts.get(key) === row) _toasts.delete(key);
      _syncDeckStack();
      if (deck.children.length === 0) deck.innerHTML = "";
    };
    // Fade the notice while the tab closes over it, then take it out.
    const closeRow = () => {
      if (!row.isConnected || row.classList.contains("fade-out")) return;
      const from = _deckHeightBefore(deck);
      row.style.display = "none";
      const to = _deckHeightNow(deck);
      row.style.display = "";
      row.classList.add("fade-out");
      _deckHeightSlide(deck, from, to, removeRow);
    };
    const dismissBtn = row.querySelector('[data-act="dismiss"]');
    if (dismissBtn) {
      dismissBtn.onclick = (e) => {
        e.stopPropagation();
        closeRow();
      };
    }

    // Insert into deck
    const firstDeckCard = [...deck.children].find((child) => !child.classList.contains("notifications"));
    if (firstDeckCard) deck.insertBefore(row, firstDeckCard);
    else deck.appendChild(row);
    _syncDeckStack();
    _deckHeightSlide(deck, heightBefore, _deckHeightNow(deck));

    row._dismissTimer = setTimeout(closeRow, ms);

    if (key) _toasts.set(key, row);
    return row;
  }

  // The tab above the composer changes height when a notice arrives or leaves.
  // Slide between the two heights so the composer's top edge doesn't jump.
  // The deck sits in the composer's inbox, which is hidden while empty and
  // carries the spacing under it, so the inbox is the box that slides.
  function _deckBox(deck) {
    return deck.closest(".composer-inbox") || deck;
  }

  function _deckHeightBefore(deck) {
    const box = _deckBox(deck);
    const height = box.getBoundingClientRect().height;
    box._heightAnim?.cancel();   // runs any pending removal, after measuring
    return height;
  }

  function _deckHeightNow(deck) {
    const box = _deckBox(deck);
    // watchComposerInbox reveals the inbox a moment later; do it now so the
    // height it is about to take can be measured.
    if (box !== deck && deck.children.length) box.classList.add("has-items");
    return box.getBoundingClientRect().height;
  }

  function _deckHeightSlide(deck, from, to, after = null) {
    const box = _deckBox(deck);
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduced || !box.animate || Math.abs(to - from) < 1) { after?.(); return; }
    // Opening from nothing or closing to nothing: ease the gap under the box too.
    const start = { height: `${from}px`, overflow: "hidden" };
    const end = { height: `${to}px`, overflow: "hidden" };
    if (from < 1) start.marginBottom = "0px";
    if (to < 1) {
      end.marginBottom = "0px";
      // An emptied inbox is hidden straight away; hold it open while it closes.
      const shown = getComputedStyle(box).display;
      if (shown !== "none") start.display = end.display = shown;
    }
    const anim = box.animate([start, end], {
      duration: 220, easing: "cubic-bezier(.2, .8, .2, 1)", fill: after ? "forwards" : "none",
    });
    box._heightAnim = anim;
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      if (box._heightAnim === anim) box._heightAnim = null;
      after?.();
      anim.cancel();
    };
    anim.onfinish = finish;
    anim.oncancel = finish;
  }

  // In the unified docked tab, avoid stacked floating card depth layers
  function _syncDeckStack() {
    const deck = $("#revealer-deck");
    if (!deck) return;
    const notes = [...deck.querySelectorAll(".revealer-card.notifications:not(.fade-out)")];
    // Keep only the front notice visible if multiple arrive; older ones hide without depth stacking
    notes.forEach((el, i) => {
      if (i === 0) {
        el.style.display = "";
      } else {
        el.style.display = "none";
      }
    });
  }

  function toast(msg, kind = "info", ms = 3000, key = null, html = false) {
    msg = String(msg ?? "");
    // Only `.toast.err` has an error style; `"error"` was passed at ~12 call
    // sites and silently rendered with the neutral accent dot. Normalize it.
    if (kind === "error") kind = "err";
    if (kind === "err") {
      triggerComposerStatus("error");
    } else if (msg.includes("auto-tuned") || msg.includes("auto-tune")) {
      triggerComposerStatus("autotuned");
    } else if (msg.includes("loaded") || msg.includes("ready")) {
      triggerComposerStatus("loaded");
    }

    // Route ordinary status messages into the narrow tab above the composer.
    const deck = $("#revealer-deck");
    if (deck) {
      const deckRow = showDeckToast(msg, kind, ms, key, html);
      if (deckRow) return deckRow;
    }

    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      // Anchor toasts on the composer-wrap so they pop up from behind the prompt
      // box; pages without a composer fall back to body.
      const anchor = document.querySelector(".composer-wrap") || document.body;
      anchor.appendChild(host);
    }
    if (key && _toasts.has(key)) {
      try { _toasts.get(key).remove(); } catch {}
      _toasts.delete(key);
    }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    
    const iconMap = {
      info: '<i class="ph ph-info toast-ico" style="color:var(--accent)"></i>',
      ok: '<i class="ph ph-check-circle toast-ico" style="color:var(--success)"></i>',
      warn: '<i class="ph ph-warning toast-ico" style="color:var(--warning)"></i>',
      err: '<i class="ph ph-x-circle" style="color:var(--danger)"></i>'
    };
    const iconHtml = iconMap[kind] != null ? iconMap[kind] : iconMap.info;
    const cleanMsg = html ? msg : esc(msg);
    el.innerHTML = `${iconHtml}<span class="toast-text">${cleanMsg}</span>`;

    host.appendChild(el);
    if (key) _toasts.set(key, el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => { try { el.remove(); } catch {} if (key && _toasts.get(key) === el) _toasts.delete(key); }, 250);
    }, ms);
    return el;
  }

  function triggerComposerStatus(status) {
    const comp = document.querySelector(".composer");
    if (!comp) return;
    comp.classList.remove("status-loaded", "status-autotuned", "status-error");
    if (status === "loaded") {
      comp.classList.add("status-loaded");
      setTimeout(() => comp.classList.remove("status-loaded"), 2200);
    } else if (status === "autotuned") {
      comp.classList.add("status-autotuned");
      setTimeout(() => comp.classList.remove("status-autotuned"), 2200);
    } else if (status === "error") {
      comp.classList.add("status-error");
      setTimeout(() => comp.classList.remove("status-error"), 5000);
    }
  }

  async function requestModelLoad(modelPath) {
    const result = await api("/api/models/load", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: modelPath }),
    });
    if (!result?.ok || !result?.ready) {
      throw new Error(result?.error || "llama-server did not report the model ready");
    }
    // The context ring caches the server's context size, so drop it and re-read
    // from the freshly started server.
    state._ctxCapacity = 0;
    await refreshContextStats();
    renderStatus();
    return result;
  }

  const UPDATE_DISMISS_KEY = "accuretta:dismissed-update";
  let activeUpdateVersion = "";
  let updateCheckTimer = null;

  function positionAppUpdateCard() {
    const card = $("#app-update-card");
    if (!card || card.hidden) return;
    let bottom = 76;
    if (isMobile()) {
      bottom = 14;
      if (app.classList.contains("m-tab-chat")) {
        const composer = document.querySelector(".composer-wrap");
        if (composer) {
          bottom = Math.max(14, Math.ceil(window.innerHeight - composer.getBoundingClientRect().top + 12));
        }
      }
    }
    card.style.setProperty("--app-update-bottom", `${bottom}px`);
  }

  function hideAppUpdateCard(persist = false) {
    const card = $("#app-update-card");
    if (!card || card.hidden) return;
    if (persist && activeUpdateVersion) {
      try { localStorage.setItem(UPDATE_DISMISS_KEY, activeUpdateVersion); } catch {}
    }
    card.classList.add("is-leaving");
    card.classList.remove("is-visible");
    setTimeout(() => {
      card.hidden = true;
      card.classList.remove("is-leaving");
    }, 220);
  }

  function showAppUpdateCard(status) {
    const release = status?.release;
    const version = String(release?.version || "").trim();
    if (!status?.available || !version || !release?.url) return;
    try {
      if (localStorage.getItem(UPDATE_DISMISS_KEY) === version) return;
    } catch {}

    activeUpdateVersion = version;
    const card = $("#app-update-card");
    if (!card) return;
    $("#app-update-title").textContent = `Accuretta v${version} available`;
    $("#app-update-summary").textContent = release.summary || "A new Accuretta release is ready.";
    // An installer build updates by running the new installer; a git checkout
    // follows the release page's steps.
    $("#app-update-link").href = (status.install_type === "installer" && release.installer_url) || release.url;
    card.hidden = false;
    card.classList.remove("is-leaving");
    positionAppUpdateCard();
    requestAnimationFrame(() => card.classList.add("is-visible"));
  }

  async function checkForAppUpdate() {
    try {
      showAppUpdateCard(await api("/api/app-update"));
    } catch (error) {
      console.debug("update check skipped:", error);
    }
  }

  function initAppUpdateCheck() {
    $("#app-update-dismiss")?.addEventListener("click", () => hideAppUpdateCard(true));
    window.addEventListener("resize", positionAppUpdateCard, { passive: true });
    setTimeout(checkForAppUpdate, 1400);
    if (updateCheckTimer) clearInterval(updateCheckTimer);
    updateCheckTimer = setInterval(checkForAppUpdate, 12 * 60 * 60 * 1000);
  }

  function compactionRowFor(chatId) {
    const row = state.compactionRows.get(chatId);
    if (row?.isConnected) return row;
    state.compactionRows.delete(chatId);
    return null;
  }

  function removeCompactionRow(chatId, row) {
    if (!row) return;
    clearTimeout(row._safetyTimer);
    clearTimeout(row._removeTimer);
    row.classList.add("is-leaving");
    row._removeTimer = setTimeout(() => {
      if (state.compactionRows.get(chatId) === row) {
        state.compactionRows.delete(chatId);
      }
      try { row.remove(); } catch {}
    }, 260);
  }

  function compactStatsLabel(stats) {
    if (!stats) return "history condensed into the session summary";
    const msgs = Number(stats.folded) || 0;
    let tok = "";
    if (Number.isFinite(stats.folded_tokens) && stats.folded_tokens > 0) {
      const t = stats.folded_tokens;
      tok = ` · ~${t >= 1000 ? (t / 1000).toFixed(1) + "K" : t} tokens`;
    }
    return `compacted · ${msgs} msgs${tok}`;
  }

  function buildFoldDivider(stats, opts) {
    const el = opts && opts.el || document.createElement("div");
    el.className = "compaction-inline-row is-divider";
    if (opts && opts.chatId) el.dataset.chatId = opts.chatId;
    if (opts && opts.persistent !== false) el.dataset.foldDivider = "1";
    el.setAttribute("role", "note");
    el.innerHTML = `<span class="fold-line" aria-hidden="true"></span>` +
      `<span class="fold-label">${esc(compactStatsLabel(stats))}</span>` +
      `<span class="fold-line" aria-hidden="true"></span>`;
    return el;
  }

  // Model-switch divider: same faint line · label · line as the fold divider.
  // Each reply records the model that wrote it (msg.model, the GGUF stem), so
  // the break is derived on render: it goes just before the turn whose reply
  // came from a different model than the reply before it.
  function modelSwitchLabel(model) {
    const short = shortenModelName(model);
    return `switched model · ${short || model}`;
  }

  function buildModelSwitchDivider(model) {
    const el = document.createElement("div");
    el.className = "compaction-inline-row is-divider is-model-switch";
    el.setAttribute("role", "note");
    el.innerHTML = `<span class="fold-line" aria-hidden="true"></span>` +
      `<span class="fold-label"><i class="ph ph-shuffle" aria-hidden="true"></i>${esc(modelSwitchLabel(model))}</span>` +
      `<span class="fold-line" aria-hidden="true"></span>`;
    return el;
  }

  // Map of visible-message index -> new model, for each place a reply's model
  // differs from the previous reply's. The index is the first message after
  // the previous reply (the user turn the new model answered).
  function modelSwitchPoints(messages) {
    const points = new Map();
    let prevModel = "";
    let lastReplyIdx = -1;
    (messages || []).forEach((m, i) => {
      if (m.role !== "assistant") return;
      if (m.model) {
        if (prevModel && m.model !== prevModel) points.set(lastReplyIdx + 1, m.model);
        prevModel = m.model;
      }
      lastReplyIdx = i;
    });
    return points;
  }

  // Stem of the model now loaded, in the same form replies record.
  function liveModelStem() {
    const path = String(state.settings?.model_path || "");
    if (path) return path.split(/[\\/]/).pop().replace(/\.gguf$/i, "").slice(0, 80);
    return wbLiveModel();
  }

  // Right after a model load, show the break at the end of the open chat
  // (before the next message) if its last reply came from another model.
  // Switching back to that model takes it away again. Once the new model
  // replies, the rendered history carries the same divider at the same place.
  function syncLiveModelSwitchDivider() {
    const inner = $("#chat-inner");
    if (!inner) return;
    inner.querySelectorAll(":scope > .is-model-switch.is-live").forEach(el => el.remove());
    if (!state.messages?.length) return;
    let last = "";
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i];
      if (m.role === "assistant" && m.model) { last = m.model; break; }
    }
    const now = liveModelStem();
    if (!last || !now || last === now) return;
    const rows = inner.querySelectorAll(":scope > .bubble-row");
    const lastRow = rows[rows.length - 1];
    if (!lastRow) return;
    const divider = buildModelSwitchDivider(now);
    divider.classList.add("is-live");
    lastRow.after(divider);
  }

  // The chat record also stores intermediate assistant turns and tool results
  // for the bridge's replay; only these render. A reply that a mid-answer update
  // cut short (_steer_partial) is internal for the bridge but shown, since the
  // user watched it being written.
  function isRenderedMessage(m) {
    return !!m && (m.role === "user" || m.role === "assistant"
      || (m.role === "system" && m._note)) && (!m._internal || !!m._steer_partial);
  }

  // Visible-message index of the fold boundary for a raw chat record.
  // summary_through points into the raw message list (internal assistants and
  // tool results included), but only visible user/assistant messages render, so
  // the divider goes after the last visible message before the boundary.
  function foldBoundaryVisibleIdx(chat) {
    if (!chat || !chat.messages) return { idx: -1, stats: null };
    const mark = chat.fold_mark || null;
    const through = mark ? Number(mark.through) : (Number(chat.summary_through) || 0);
    if (!through) return { idx: -1, stats: null };
    let idx = 0;
    for (let i = 0; i < Math.min(through, chat.messages.length); i++) {
      const m = chat.messages[i];
      if ((m.role === "user" || m.role === "assistant") && isRenderedMessage(m)) idx++;
    }
    return { idx, stats: mark };
  }

  // Insert the durable fold divider straight from stored state when there is no
  // live working row to settle (the start event was missed on a reconnect, or
  // the page loaded mid-fold). Mirrors the anchor logic in renderMessages so
  // the divider lands at the same visible boundary.
  function insertFoldDivider(chatId, stats) {
    if (!chatId || chatId !== state.chatId) return null;
    const inner = $("#chat-inner");
    if (!inner || !(state.foldBoundaryIdx > 0)) return null;
    const escId = String(chatId).replace(/["\\]/g, "\\$&");
    if (inner.querySelector(`.compaction-inline-row.is-divider[data-chat-id="${escId}"]`)) return null;
    const start = Math.max(0, Math.min(state.messageWindowStart || 0, state.messages.length));
    if (!(state.foldBoundaryIdx > start && state.foldBoundaryIdx <= state.messages.length)) return null;
    const bubbles = Array.from(inner.querySelectorAll("#chat-inner > .bubble-row"));
    const at = Math.max(0, state.foldBoundaryIdx - start);
    const divider = buildFoldDivider(stats || state.foldMark, { chatId });
    divider.classList.add("is-done");
    if (bubbles[at]) inner.insertBefore(divider, bubbles[at]);
    else inner.appendChild(divider);
    if (isNearBottom()) requestAnimationFrame(() => scrollToBottom(true));
    return divider;
  }

  function ensureCompactionRow(chatId) {
    if (!chatId || chatId !== state.chatId) return null;
    const inner = $("#chat-inner");
    if (!inner) return null;
    let row = compactionRowFor(chatId);
    if (!row) {
      const follow = isNearBottom();
      row = document.createElement("div");
      row.className = "compaction-inline-row is-working";
      row.dataset.chatId = chatId;
      row.setAttribute("role", "status");
      row.setAttribute("aria-live", "polite");
      const dots = Array.from({ length: 9 }, () => "<span></span>").join("");
      row.innerHTML = `<span class="compaction-dot-matrix" aria-hidden="true">${dots}</span><span class="compaction-inline-label shimmer">Compacting..</span>`;

      const liveRow = state.liveTurn?.chatId === chatId
        && state.liveTurn.row?.parentNode === inner
        ? state.liveTurn.row
        : Array.from(inner.querySelectorAll(".bubble-row"))
            .find(candidate => candidate.querySelector(".bubble-meta.streaming"));
      // Under the reply that is streaming, where the reader is looking: above
      // a long reply it sat off screen and the run looked stuck. The finished
      // divider is placed at the real fold point (finishCompactionRow).
      if (liveRow) inner.insertBefore(row, liveRow.nextSibling);
      else inner.appendChild(row);
      row._liveAnchor = liveRow || null;
      state.compactionRows.set(chatId, row);
      if (follow) requestAnimationFrame(() => scrollToBottom(true));
    }
    clearTimeout(row._safetyTimer);
    row._safetyTimer = setTimeout(() => {
      state.compactingChats.delete(chatId);
      removeCompactionRow(chatId, row);
    }, 310000);
    return row;
  }

  function finishCompactionRow(chatId, outcome = "done", stats) {
    state.compactingChats.delete(chatId);
    const row = compactionRowFor(chatId);
    if (!row) {
      // No live working row: the start event was missed (reconnect, late load,
      // or a backgrounded client). Still leave the durable divider so history
      // records that compaction happened here.
      if (outcome === "done") insertFoldDivider(chatId, stats);
      return;
    }
    clearTimeout(row._safetyTimer);
    if (outcome === "done") {
      // Settle into a persistent divider at the fold point (dot matrix hidden);
      // re-renders recreate it from the stored fold_mark.
      const _after = state.chats?.chats?.[chatId];
      if (_after && stats) {
        _after.fold_mark = {
          through: Number(stats.through) || _after.summary_through || 0,
          folded: Number(stats.folded) || 0,
          folded_tokens: stats.folded_tokens,
          summary_chars: stats.summary_chars,
          t: undefined,
        };
      }
      // The working row sat under the live reply so it stayed in view; the
      // settled divider marks the fold, which is just before the reply being
      // written. Move the row there as it turns into the divider.
      state.compactionRows.delete(chatId);
      const follow = isNearBottom();
      const liveRow = row._liveAnchor;
      if (liveRow?.isConnected && liveRow.parentNode === row.parentNode) liveRow.before(row);
      row._liveAnchor = null;
      const div = buildFoldDivider(stats, { el: row, chatId });
      div.classList.add("is-done");
      if (follow) requestAnimationFrame(() => scrollToBottom(true));
      return;
    }
    clearTimeout(row._removeTimer);
    if (outcome === "clear") {
      removeCompactionRow(chatId, row);
      return;
    }
    row.classList.remove("is-working");
    row.classList.add(outcome === "failed" ? "is-failed" : "is-done");
    const label = row.querySelector(".compaction-inline-label");
    if (label) {
      label.classList.remove("shimmer");
      label.textContent = outcome === "failed" ? "Compaction failed" : "Compacted";
    }
    row._removeTimer = setTimeout(
      () => removeCompactionRow(chatId, row), outcome === "failed" ? 3600 : 900);
  }

  const SKILL_SAVE_MIN_FEEDBACK_MS = 650;

  function skillSaveRowKey(chatId, callId = "") {
    return `${chatId}:${callId || "pending"}`;
  }

  function skillSaveRowFor(chatId, callId = "") {
    if (callId) {
      const key = skillSaveRowKey(chatId, callId);
      const exact = state.skillSaveRows.get(key);
      if (exact?.isConnected) return exact;
      state.skillSaveRows.delete(key);
      return null;
    }
    const rows = Array.from(state.skillSaveRows.values())
      .filter(row => row?.isConnected && row.dataset.chatId === chatId);
    return rows[rows.length - 1] || null;
  }

  function removeSkillSaveRow(chatId, row) {
    if (!row) return;
    clearTimeout(row._safetyTimer);
    clearTimeout(row._finishTimer);
    clearTimeout(row._removeTimer);
    row.classList.add("is-leaving");
    row._removeTimer = setTimeout(() => {
      const key = row._skillSaveKey || skillSaveRowKey(chatId, row.dataset.callId || "");
      if (state.skillSaveRows.get(key) === row) state.skillSaveRows.delete(key);
      try { row.remove(); } catch {}
    }, 260);
  }

  function ensureSkillSaveRow(chatId, skill, callId = "") {
    if (!chatId || chatId !== state.chatId) return null;
    const inner = $("#chat-inner");
    if (!inner) return null;
    const labelText = `Saving skill${skill ? ` · ${skill}` : ""}..`;
    let row = skillSaveRowFor(chatId, callId);
    if (!row) {
      const follow = isNearBottom();
      row = document.createElement("div");
      row.dataset.chatId = chatId;
      row.dataset.callId = callId;
      row._skillSaveKey = skillSaveRowKey(chatId, callId);
      row._startedAt = Date.now();
      row.setAttribute("role", "status");
      row.setAttribute("aria-live", "polite");
      const dots = Array.from({ length: 9 }, () => "<span></span>").join("");
      row.innerHTML = `<span class="compaction-dot-matrix" aria-hidden="true">${dots}</span>` +
        `<span class="compaction-inline-label shimmer">${esc(labelText)}</span>`;

      const liveRow = state.liveTurn?.chatId === chatId
        && state.liveTurn.row?.parentNode === inner
        ? state.liveTurn.row
        : Array.from(inner.querySelectorAll(".bubble-row"))
            .find(candidate => candidate.querySelector(".bubble-meta.streaming"));
      if (liveRow) inner.insertBefore(row, liveRow);
      else inner.appendChild(row);
      state.skillSaveRows.set(row._skillSaveKey, row);
      if (follow) requestAnimationFrame(() => scrollToBottom(true));
    }

    if (skill) row.dataset.skill = skill;
    clearTimeout(row._removeTimer);
    row.className = "compaction-inline-row skill-save-inline-row is-working";
    const label = row.querySelector(".compaction-inline-label");
    if (label && !row._finishPending) {
      label.textContent = labelText;
      label.classList.add("shimmer");
    }
    clearTimeout(row._safetyTimer);
    row._safetyTimer = setTimeout(() => removeSkillSaveRow(chatId, row), 310000);
    return row;
  }

  function finishSkillSaveRow(chatId, outcome = "saved", skill = "", callId = "") {
    const row = skillSaveRowFor(chatId, callId);
    if (!row) return;
    if (row._finishPending) return;
    row._finishPending = true;
    if (skill) row.dataset.skill = skill;
    clearTimeout(row._safetyTimer);
    const label = row.querySelector(".compaction-inline-label");
    const savedSkill = skill || row.dataset.skill || "";
    const suffix = savedSkill ? ` · ${savedSkill}` : "";
    const finalLabel = outcome === "failed"
      ? `Skill save failed${suffix}`
      : outcome === "cancelled" ? `Skill save cancelled${suffix}` : `Skill saved${suffix}`;
    if (label) label.textContent = finalLabel;

    const settle = () => {
      row.classList.remove("is-working");
      row.classList.add(outcome === "failed" ? "is-failed" : "is-done");
      if (label) {
        label.classList.remove("shimmer");
        label.textContent = finalLabel;
      }
      row._removeTimer = setTimeout(
        () => removeSkillSaveRow(chatId, row), outcome === "failed" ? 3600 : 1100);
    };

    const elapsed = Date.now() - (row._startedAt || Date.now());
    const remaining = outcome === "saved"
      ? Math.max(0, SKILL_SAVE_MIN_FEEDBACK_MS - elapsed)
      : 0;
    if (remaining) row._finishTimer = setTimeout(settle, remaining);
    else settle();
  }

  function hideToast(key) {
    if (!key || !_toasts.has(key)) return;
    const el = _toasts.get(key);
    _toasts.delete(key);
    try { el.remove(); } catch {}
  }

  const esc = (s) => String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  function isNearBottom() {
    const s = $("#chat-scroll");
    return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
  }
  let chatFollowBottom = true;
  // Code in a ui-*.js file gets a copy of a let, so it sets this one through here.
  function setChatFollowBottom(on) { chatFollowBottom = on; }
  function scrollToBottom(force = false) {
    const s = $("#chat-scroll");
    if (force) chatFollowBottom = true;
    if (chatFollowBottom) {
      s.scrollTop = s.scrollHeight;
    }
  }

  function initChatJumpButton() {
    const scroller = $("#chat-scroll");
    const button = $("#chat-jump-bottom");
    if (!scroller || !button) return;

    let updateFrame = 0;
    const update = () => {
      if (updateFrame) return;
      updateFrame = requestAnimationFrame(() => {
        updateFrame = 0;
        const distance = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
        const visible = distance > 140;
        button.classList.toggle("is-visible", visible);
        button.setAttribute("aria-hidden", visible ? "false" : "true");
        button.tabIndex = visible ? 0 : -1;
      });
    };

    let lastHeight = scroller.scrollHeight;
    let lastViewport = scroller.clientHeight;
    scroller.addEventListener("wheel", event => {
      if (event.deltaY < 0) chatFollowBottom = false;
    }, { passive: true });
    let touchY = null;
    scroller.addEventListener("touchstart", event => { touchY = event.touches[0]?.clientY; }, { passive: true });
    scroller.addEventListener("touchmove", event => {
      const y = event.touches[0]?.clientY;
      if (touchY != null && y > touchY) chatFollowBottom = false;
      touchY = y;
    }, { passive: true });
    scroller.addEventListener("keydown", event => {
      if (event.target.closest("input, textarea, [contenteditable=true]")) return;
      if (["ArrowUp", "PageUp", "Home"].includes(event.key)) chatFollowBottom = false;
    });
    scroller.addEventListener("scroll", () => {
      const resized = lastHeight !== scroller.scrollHeight || lastViewport !== scroller.clientHeight;
      if (isNearBottom()) chatFollowBottom = true;
      else if (!resized) chatFollowBottom = false;
      lastHeight = scroller.scrollHeight;
      lastViewport = scroller.clientHeight;
      update();
    }, { passive: true });
    button.addEventListener("click", () => {
      chatFollowBottom = true;
      scroller.scrollTo({
        top: scroller.scrollHeight,
        behavior: "instant",
      });
    });

    if ("ResizeObserver" in window) {
      scroller._jumpButtonObserver?.disconnect();
      scroller._jumpButtonObserver = new ResizeObserver(() => {
        scrollToBottom();
        update();
      });
      scroller._jumpButtonObserver.observe(scroller);
      scroller._jumpButtonObserver.observe($("#chat-inner"));
    }
    update();
  }

  function relTime(t) {
    const d = Math.floor(Date.now() / 1000) - (t || 0);
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  }

  function humanBytes(n) {
    if (n == null) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  function detectClientOs() {
    const ua = navigator.userAgent || "";
    const platform = navigator.userAgentData?.platform || navigator.platform || "";
    if (/iPhone|iPad|iPod/i.test(ua) || (platform === "MacIntel" && navigator.maxTouchPoints > 1)) return "iOS";
    if (/Mac/i.test(platform) || /Macintosh/i.test(ua)) return "macOS";
    if (/Win/i.test(platform) || /Windows/i.test(ua)) return "Windows";
    if (/Android/i.test(ua)) return "Android";
    if (/Linux/i.test(platform) || /Linux/i.test(ua)) return "Linux";
    return "unknown";
  }

  function currentClientHint() {
    return {
      os: detectClientOs(),
      mobile: isMobile(),
      secure_context: !!window.isSecureContext,
      execution_target: state.executionTarget || "host",
    };
  }

  async function copyText(text) {
    const value = String(text ?? "");
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(value);
        return "clipboard";
      } catch (_) {}
    }
    const area = document.createElement("textarea");
    area.value = value;
    area.readOnly = true;
    area.setAttribute("aria-hidden", "true");
    Object.assign(area.style, {
      position: "fixed", left: "-9999px", top: "0", opacity: "0",
    });
    document.body.appendChild(area);
    area.focus();
    area.select();
    area.setSelectionRange(0, value.length);
    try {
      if (document.execCommand("copy")) return "legacy";
    } catch (_) {
    } finally {
      area.remove();
    }
    try {
      window.prompt("Browser clipboard access is blocked. Copy the text below:", value);
      return "manual";
    } catch (_) {
      throw new Error("Clipboard unavailable");
    }
  }
  return {
    _toasts, _deckHeightBefore, _deckHeightNow, _deckHeightSlide, _syncDeckStack, toast,
    requestModelLoad, positionAppUpdateCard, initAppUpdateCheck, buildFoldDivider,
    buildModelSwitchDivider, modelSwitchPoints, syncLiveModelSwitchDivider, isRenderedMessage,
    foldBoundaryVisibleIdx, ensureCompactionRow, finishCompactionRow, ensureSkillSaveRow,
    finishSkillSaveRow, hideToast, esc, setChatFollowBottom, scrollToBottom, initChatJumpButton,
    relTime, humanBytes, detectClientOs, currentClientHint, copyText,
  };
};
