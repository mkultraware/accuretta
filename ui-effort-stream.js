/* SPDX-License-Identifier: AGPL-3.0-only */
/* effort slider + top-level stream: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["effort-stream"] = ({
  $, activateTerminalTab, api, effortLabel, effortLevels, loadModels, loadSettings,
  offerLlamaUpdate, offerMayaInstall, populateSettingsForm, renderModelPill, renderStatus, requestModelLoad,
  saveSettings, setReasoningEffort, state, syncLiveModelSwitchDivider, toast, toggleConsolePane,
}) => {
  // One stop per level, in the same order and names as the effort menu
  // (Default, then the model's own levels). Clicking slides the thumb to a
  // stop; dragging follows the pointer and snaps on release. The highest
  // level turns the track dark and runs a dot-matrix stream toward the thumb.
  const effortSliders = new Set();
  let effortDotsRaf = 0;
  const effortMotion = matchMedia("(prefers-reduced-motion: reduce)");

  function buildEffortSlider(opts = {}) {
    const el = document.createElement("div");
    el.className = "fx-effort";
    el.innerHTML = `<div class="fx-effort-head"><span class="fx-effort-title">Effort</span>`
      + `<span class="fx-effort-level" aria-hidden="true"></span></div>`
      + `<div class="fx-effort-ends" aria-hidden="true"><span>Faster</span><span>Smarter</span></div>`
      + `<div class="fx-effort-track" role="slider" tabindex="0" aria-label="Reasoning effort">`
      + `<canvas class="fx-effort-dots" aria-hidden="true"></canvas>`
      + `<span class="fx-effort-fill" aria-hidden="true"></span>`
      + `<span class="fx-effort-ticks" aria-hidden="true"></span>`
      + `<span class="fx-effort-thumb" aria-hidden="true"></span></div>`;
    const slider = { el, isShown: opts.isShown || (() => el.isConnected), idx: -1, sig: "", levels: [] };
    const track = el.querySelector(".fx-effort-track");
    const posOf = (clientX) => {
      const r = track.getBoundingClientRect();
      const tw = el.querySelector(".fx-effort-thumb").offsetWidth || 22;
      return Math.max(0, Math.min(1, (clientX - r.left - tw / 2) / Math.max(1, r.width - tw)));
    };
    const commit = (idx) => {
      const level = slider.levels[idx];
      if (level && level !== state.reasoningEffort) setReasoningEffort(level);
      else syncEffortSlider(slider);
    };
    let drag = null;
    track.addEventListener("pointerdown", (e) => {
      if (e.button !== 0 || slider.levels.length < 2) return;
      e.preventDefault();
      track.focus({ preventScroll: true });
      track.setPointerCapture(e.pointerId);
      drag = { id: e.pointerId, x: e.clientX, moved: false };
      // A press slides the thumb to the nearest stop; moving then drags it.
      const n = slider.levels.length - 1;
      showEffortStop(slider, Math.round(posOf(e.clientX) * n), null);
    });
    track.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      if (!drag.moved && Math.abs(e.clientX - drag.x) < 3) return;
      drag.moved = true;
      track.classList.add("is-dragging");
      const p = posOf(e.clientX);
      showEffortStop(slider, Math.round(p * (slider.levels.length - 1)), p);
    });
    const release = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      track.classList.remove("is-dragging");
      commit(slider.shownIdx);
    };
    track.addEventListener("pointerup", release);
    track.addEventListener("pointercancel", release);
    track.addEventListener("keydown", (e) => {
      const n = slider.levels.length - 1;
      const step = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
      let next = null;
      if (step) next = Math.max(0, Math.min(n, slider.idx + step));
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = n;
      if (next === null) return;
      e.preventDefault();
      e.stopPropagation();
      commit(next);
    });
    // The caller mounts it, then syncs it; the stream loop drops it once it
    // leaves the page.
    effortSliders.add(slider);
    return slider;
  }

  // Put the thumb on stop `idx`, or at `pos` (0..1) while dragging, and
  // update the label, ticks and the top-level state to match.
  function showEffortStop(slider, idx, pos) {
    const { el, levels } = slider;
    const n = Math.max(1, levels.length - 1);
    const track = el.querySelector(".fx-effort-track");
    track.style.setProperty("--pos", String(pos ?? idx / n));
    const changed = slider.shownIdx !== idx;
    slider.shownIdx = idx;
    const level = levels[idx];
    const label = effortLabel(level);
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", String(n));
    track.setAttribute("aria-valuenow", String(idx));
    track.setAttribute("aria-valuetext", label);
    const isMax = levels.length > 1 && idx === levels.length - 1;
    if (track.classList.contains("is-max") !== isMax) {
      track.classList.toggle("is-max", isMax);
      if (isMax) effortLaunch(el.querySelector(".fx-effort-dots"));
      kickEffortDots();
    }
    el.querySelectorAll(".fx-effort-tick").forEach((t, i) => t.classList.toggle("is-under", i <= idx));
    const out = el.querySelector(".fx-effort-level");
    if (!changed && out.textContent) return;
    // The new level name comes in out of a short blur, as in the reference.
    // The text is set first so it is right even if the animation never runs.
    const first = !out.textContent;
    out.textContent = label;
    if (first || effortMotion.matches || !out.animate) return;
    out.getAnimations().forEach(a => a.cancel());
    out.animate([{ opacity: 0, filter: "blur(3px)" }, { opacity: 1, filter: "blur(0)" }],
      { duration: 200, easing: "ease-out" });
  }

  function syncEffortSlider(slider) {
    const { el } = slider;
    if (!el.isConnected) return;
    const ctl = $("#reasoning-effort-control");
    const on = !!ctl && !ctl.classList.contains("hidden");
    el.hidden = !on;
    if (!on) return;
    const levels = effortLevels();
    const sig = levels.join(",");
    if (sig !== slider.sig) {
      slider.sig = sig;
      slider.levels = levels;
      const n = Math.max(1, levels.length - 1);
      el.querySelector(".fx-effort-ticks").innerHTML = levels
        .map((_, i) => `<i class="fx-effort-tick" style="--k:${i / n}"></i>`).join("");
    }
    let idx = levels.indexOf(state.reasoningEffort);
    if (idx < 0) idx = 0;
    slider.idx = idx;
    showEffortStop(slider, idx, null);
    kickEffortDots();
  }

  function syncEffortSliders() {
    for (const s of [...effortSliders]) syncEffortSlider(s);
  }

  // A panel holding a slider just opened: sync it, and if it sits on the top
  // level, ignite the jet so it arrives with the panel.
  function effortSliderOpened(slider) {
    if (!slider) return;
    syncEffortSlider(slider);
    const track = slider.el.querySelector(".fx-effort-track");
    if (track?.classList.contains("is-max")) effortLaunch(slider.el.querySelector(".fx-effort-dots"));
    kickEffortDots();
  }

  // ---- top-level stream ----
  // The thumb is an engine and the track shows its exhaust, drawn on a dot
  // grid: a solid jet that leaves the thumb white-hot, runs vivid blue and
  // tapers to a point about two thirds of the way left. Its edges ripple like
  // a flame and the ripples flow away from the thumb. Sparse white specks
  // streak left past it. Reaching the level ignites the jet: it grows out of
  // the thumb. Every light sits on a grid dot, like the house dot matrix.
  const effortStreams = new WeakMap();

  function effortHash(i, j) {
    const h = Math.sin(i * 127.1 + j * 311.7) * 43758.5453;
    return h - Math.floor(h);
  }

  // Smooth wobble from two sines; cheap and has no visible repeat.
  function effortWave(z, seed) {
    return .62 * Math.sin(z + seed) + .38 * Math.sin(z * 2.27 + seed * 1.9 + 1.3);
  }

  function effortStream(canvas, w, rows) {
    let st = effortStreams.get(canvas);
    if (st && st.rows === rows && st.w === w) return st;
    st = { rows, w, last: 0, launch: st?.launch || 0, specks: [] };
    for (let n = 0; n < Math.round(w / 26); n++) st.specks.push(effortSpawn(st, w, rows, Math.random() * w));
    effortStreams.set(canvas, st);
    return st;
  }

  // A speck enters from the thumb side and streaks left.
  function effortSpawn(st, w, rows, x) {
    return {
      x: x ?? w + Math.random() * w * .4,
      row: Math.floor(Math.random() * rows),
      v: 260 + Math.random() * 520,
      len: 1 + Math.floor(Math.random() * 4),
      b: .55 + Math.random() * .45,
    };
  }

  // Re-ignite the jet (called when the top level is reached).
  function effortLaunch(canvas) {
    const st = effortStreams.get(canvas);
    if (st) st.launch = performance.now();
    else effortStreams.set(canvas, { rows: -1, w: -1, launch: performance.now() });
  }

  function drawEffortDots(canvas, now) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const P = 3;
    const cols = Math.max(2, Math.floor((w - 3) / P) + 1);
    const rows = Math.max(1, Math.floor((h - 3) / P) + 1);
    const ox = (w - (cols - 1) * P) / 2, oy = (h - (rows - 1) * P) / 2;
    const st = effortStream(canvas, w, rows);
    const still = effortMotion.matches;
    const dt = still || !st.last ? 0 : Math.min(.05, (now - st.last) / 1000);
    st.last = now;
    const t = still ? 1.7 : now / 1000;
    const mid = (rows - 1) / 2 || 1;

    // Ignition: the jet grows out of the thumb, overshooting a touch.
    let reach = 1;
    if (st.launch && !still) {
      const k = Math.min(1, (now - st.launch) / 520);
      reach = 1 + 2.2 * Math.pow(k - 1, 3) + 1.2 * Math.pow(k - 1, 2);
      if (k >= 1) st.launch = 0;
    }
    // How far the jet reaches from the thumb (0..1 of the track), breathing.
    const L = Math.max(.001, (.74 + .04 * Math.sin(t * 3.1)) * Math.max(0, reach));

    // Specks: move left, stamp a short trail behind them (toward the thumb).
    const speck = new Float32Array(cols * rows);
    for (let n = 0; n < st.specks.length; n++) {
      const p = st.specks[n];
      p.x -= p.v * dt;
      if (p.x + p.len * P < 0) { st.specks[n] = effortSpawn(st, w, rows); continue; }
      const head = Math.round((p.x - ox) / P);
      for (let k = 0; k <= p.len; k++) {
        const c = head + k;
        if (c < 0 || c >= cols) continue;
        const f = 1 - k / (p.len + 1);
        speck[p.row * cols + c] = Math.max(speck[p.row * cols + c], p.b * f);
      }
    }

    for (let c = 0; c < cols; c++) {
      const u = 1 - c / (cols - 1);            // 0 at the thumb, 1 at the far end
      const along = Math.max(0, 1 - u / L);    // 1 at the thumb, 0 at the tip
      // Flame edges: separate ripples top and bottom, flowing away from the thumb.
      const flow = u * 6 - t * 8;
      const taper = Math.pow(along, .6);
      const top = taper * (.84 + .3 * effortWave(flow, 0.3) * (1 - along * .6));
      const bot = taper * (.84 + .3 * effortWave(flow * 1.13, 4.1) * (1 - along * .6));
      // Inner shimmer, also flowing away from the thumb.
      const shimmer = .9 + .1 * Math.sin(u * 34 - t * 26);
      for (let j = 0; j < rows; j++) {
        const yc = (j - (rows - 1) / 2) / mid;  // -1 top .. 1 bottom
        const edge = yc < 0 ? top : bot;
        const ay = Math.abs(yc);
        let jet = 0, heat = 0;
        if (edge > .02 && ay < edge) {
          const depth = 1 - ay / edge;          // 1 on the axis, 0 at the edge
          const soft = Math.min(1, depth / .14);
          jet = soft * (.7 + .3 * along) * shimmer;
          heat = Math.pow(depth, .6) * Math.pow(along, .7);
        }
        const sp = speck[j * cols + c];
        const x = ox + c * P, y = oy + j * P;
        if (jet > .04) {
          // White-hot core near the thumb, vivid blue body, deep blue rim.
          const hh = Math.min(1, heat * 1.15 + sp * .4);
          const light = 54 + 42 * Math.pow(hh, 1.6);
          const sat = 98 - 20 * Math.pow(hh, 2);
          ctx.fillStyle = `hsla(${(220 - 6 * hh).toFixed(0)},${sat.toFixed(0)}%,${light.toFixed(0)}%,${(.72 + .28 * jet).toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(x, y, .85 + .55 * jet, 0, Math.PI * 2);
          ctx.fill();
        } else if (sp > .05) {
          ctx.fillStyle = `rgba(236,241,255,${(.35 + .65 * sp).toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(x, y, .6 + .55 * sp, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.fillStyle = `rgba(205,212,228,${(.1 + .05 * effortHash(c, j)).toFixed(3)})`;
          ctx.beginPath();
          ctx.arc(x, y, .55, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
  }

  function effortDotsTick(now) {
    effortDotsRaf = 0;
    if (document.hidden) return;
    let live = false;
    for (const s of [...effortSliders]) {
      if (!s.el.isConnected) { effortSliders.delete(s); continue; }
      const track = s.el.querySelector(".fx-effort-track");
      if (s.el.hidden || !track.classList.contains("is-max") || !s.isShown()) continue;
      drawEffortDots(s.el.querySelector(".fx-effort-dots"), now);
      live = true;
    }
    if (live && !effortMotion.matches) effortDotsRaf = requestAnimationFrame(effortDotsTick);
  }

  function kickEffortDots() {
    if (!effortDotsRaf) effortDotsRaf = requestAnimationFrame(effortDotsTick);
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) kickEffortDots(); });

  // Reload the model list, then re-mirror it everywhere it shows (settings
  // dropdown, pill, dropdown menu). Module scope so loadModelByPath and
  // autoRetuneOnBoot can call it.
  async function refreshModels() {
    await loadModels();
    populateSettingsForm();
    renderModelPill();
  }

  // Shared model-load flow for the settings <select> and the model-pill
  // dropdown. The tune happens server-side in /api/models/load: a known model
  // gets its remembered config back, a new one is tuned fresh. Tuning here
  // first used to run with the previous model's ctx as the grow-only floor and
  // save the result while model_path still pointed at the previous model, so
  // every switch rewrote both models' configs (and the model advisor, which
  // learns per exact config, started over). loadSettings() below mirrors the
  // restored values into the settings form. Returns true on success.
  async function loadModelByPath(modelPath, opts = {}) {
    if (!modelPath) return false;
    // Warn instead of trying: a llama.cpp without this architecture can only
    // fail, and the attempt would first stop the running model.
    const entry = (state.modelsList || []).find(m => m.path === modelPath);
    if (entry && entry.llama_supported === false) {
      const name = entry.display_name || modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, "");
      if (entry.needs_maya) {
        offerMayaInstall(name, modelPath);
        return false;
      }
      offerLlamaUpdate(name, entry.architecture);
      return false;
    }
    const hint = opts.hint || null;
    const prev = hint?.textContent;
    if (hint) hint.textContent = "loading model into llama-server...";
    // Surface the backend output so the model-load progress is visible live.
    try { toggleConsolePane(true); activateTerminalTab("backend"); } catch {}
    state._reloading = true;
    renderStatus();
    renderModelPill();
    // Loading indicator in the revealer deck: a keyed card with the deck's live
    // dot. The completion/failure toasts reuse the "model-load" key, so the card
    // morphs into the result; the long duration covers the whole blocking load.
    const loadingName = modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, "");
    toast(`Loading ${loadingName} into llama-server…`, "info", 600000, "model-load");
    try {
      // Persist just the model selection; the load restores or tunes its flags.
      const persistPayload = {
        model_path: modelPath,
        model: modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, ""),
      };
      await saveSettings(persistPayload);
      await requestModelLoad(modelPath);
      await loadSettings();
      await refreshModels();
      try { syncLiveModelSwitchDivider(); } catch {}
      const ctx = Number(state.settings?.num_ctx || 0);
      toast(ctx ? `model loaded (ctx ${ctx.toLocaleString()})` : "model loaded", "ok", 2500, "model-load");
      return true;
    } catch (e) {
      if (hint) hint.textContent = prev || "";
      toast("load failed: " + (e.message || e), "error", 6000, "model-load");
      return false;
    } finally {
      state._reloading = false;
      renderStatus();
      renderModelPill();
    }
  }

  // Validated DFlash/DSpark pairs per model path, used for the model-picker
  // badge. Fetched once after the model list loads; the bridge caches the
  // GGUF fingerprints, so repeat calls are cheap.
  async function refreshDraftPairs() {
    try {
      const r = await api("/api/models/draft-pairs");
      state.draftPairs = (r && r.pairs) || {};
    } catch (_) {
      state.draftPairs = {};
    }
  }
  return {
    buildEffortSlider, syncEffortSliders, effortSliderOpened, refreshModels, loadModelByPath,
    refreshDraftPairs,
  };
};
