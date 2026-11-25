/* SPDX-License-Identifier: AGPL-3.0-only */
/* VRAM auto-tune: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["vram-tune"] = ({
  $, LOAD_TIME_KEYS, api, renderModelPill, renderStatus, requestModelLoad, saveSettings, state,
  toast,
}) => {
  // Last detected GPU info, so the auto-tune notes can quote it.
  const _vramState = { detected: null };

  // Best-effort pre-select of the closest tier in the dropdown.
  function _pickClosestVramTier(gb) {
    const sel = $("#set-vram-tier");
    if (!sel) return;
    const opts = Array.from(sel.options).map(o => Number(o.value)).filter(v => v > 0);
    if (!opts.length) return;
    // Pick the largest tier that is <= detected, falling back to the smallest.
    let pick = opts[0];
    for (const v of opts) if (v <= gb && v >= pick) pick = v;
    if (gb >= Math.max(...opts)) pick = Math.max(...opts);
    sel.value = String(pick);
  }

  async function loadDetectedVram() {
    const hint = $("#vram-detected-hint");
    if (!hint) return;
    hint.textContent = "detecting GPU...";
    try {
      const r = await api("/api/llama/detect-vram");
      _vramState.detected = r;
      const gb = Number(r?.gb || 0);
      if (gb > 0) {
        const name = r.name ? ` ${r.name}` : "";
        // gb is free VRAM (other processes' allocations excluded); show it as such
        // and match the tier picker against free, not total.
        const total = Number(r.total_gb || 0);
        const size = total > 0 ? `${gb.toFixed(1)} GB free of ${total.toFixed(1)} GB` : `${gb.toFixed(1)} GB`;
        hint.textContent = `detected: ${size}${name} (via ${r.source || "nvidia-smi"})`;
        // If the user hasn't picked a tier yet (it's still 0 = Manual), nudge to
        // the closest detected tier so the Suggest button is one click away.
        const sel = $("#set-vram-tier");
        if (sel && Number(sel.value) === 0) _pickClosestVramTier(gb);
      } else {
        hint.textContent = "no NVIDIA GPU detected — pick a VRAM tier manually if you want a suggestion";
      }
    } catch (e) {
      hint.textContent = `vram detect failed: ${e.message || e}`;
    }
  }

  // Shared auto-tune application. The caller passes its saved num_ctx as
  // min_ctx to /api/llama/auto-tune and the backend returns a (ctx, offload)
  // combo that fits (grow-only is the tuner's job). Apply it as returned: save
  // every tuner key through the settings-save path and mirror the values into
  // the Settings drawer. Returns the applied update, or null when nothing
  // changed.
  async function applyAutoTune(result, opts = {}) {
    const sug = result || {};
    const update = {};
    for (const k of ["num_ctx", "num_gpu", "num_batch", "n_ubatch", "n_cpu_moe", "num_thread",
                     "spec_draft_n_max", "moe_cache_mib"]) {
      if (sug[k] != null) update[k] = Number(sug[k]);
    }
    if (sug.kv_cache_type) update.kv_cache_type = String(sug.kv_cache_type);
    if (sug.kv_cache_type_v != null) update.kv_cache_type_v = String(sug.kv_cache_type_v);
    const manualSpec = state.settings?.spec_strategy_source === "manual";
    if (sug.spec_strategy && !manualSpec) {
      update.spec_strategy = String(sug.spec_strategy);
      update.spec_strategy_source = "auto";
    }
    if (sug.flash_attn != null) update.flash_attn = !!sug.flash_attn;
    // Keep the legacy flag in sync with the strategy, as collectAndSaveSettings
    // does, so a stale enable_speculative=false can't outlive the tuner's pick.
    if (update.spec_strategy) update.enable_speculative = update.spec_strategy !== "off";
    if (!Object.keys(update).length) return null;
    // Mirror into the Settings drawer so the visible fields match what's saved.
    const setVal = (id, v) => { const el = $(id); if (el != null && v != null) el.value = String(v); };
    const setSwitch = (id, v) => { const el = $(id); if (!el || v == null) return; el.classList.toggle("on", !!v); };
    setVal("#set-ctx", update.num_ctx);
    setVal("#set-gpu", update.num_gpu);
    setVal("#set-batch", update.num_batch);
    const kv = $("#set-kv"); if (kv && update.kv_cache_type) kv.value = update.kv_cache_type;
    const kvv = $("#set-kvv"); if (kvv && update.kv_cache_type_v != null) kvv.value = update.kv_cache_type_v;
    setVal("#set-ncmoe", update.n_cpu_moe);
    setVal("#set-ubatch", update.n_ubatch);
    setVal("#set-thread", update.num_thread);
    setSwitch("#sw-flash", update.flash_attn);
    if (update.spec_strategy) setVal("#set-spec-strategy", update.spec_strategy);
    // Skip the save when every value already matches — keeps boot idempotent.
    const cur = state.settings || {};
    const changed = Object.entries(update).some(([k, v]) =>
      typeof v === "boolean" ? !!cur[k] !== v : String(cur[k] ?? "") !== String(v));
    if (!changed) return null;
    await saveSettings(update);
    if (opts.toastPrefix) {
      toast(`${opts.toastPrefix} (ctx ${Number(update.num_ctx || 0).toLocaleString()}, n_cpu_moe ${update.n_cpu_moe ?? 0})`, "ok", 4000);
    }
    return update;
  }

  // Tuner result: a few plain lines up top, the full reasoning behind a toggle.
  function renderAutoTuneNotes(el, headLines, sug, status) {
    if (!el) return;
    el.textContent = "";
    const head = document.createElement("div");
    head.className = "autotune-head";
    head.textContent = headLines.filter(line => line && !line.startsWith(">>")).join(" · ");
    el.appendChild(head);
    if (sug.quant_downshift) {
      const warn = document.createElement("div");
      warn.className = "autotune-warn";
      warn.textContent = sug.quant_downshift;
      el.appendChild(warn);
    }
    const summary = Array.isArray(sug.summary) ? sug.summary : [];
    if (summary.length) {
      const ul = document.createElement("ul");
      ul.className = "autotune-summary";
      for (const line of summary) {
        const li = document.createElement("li");
        li.textContent = line;
        ul.appendChild(li);
      }
      el.appendChild(ul);
    }
    if (status) {
      const st = document.createElement("div");
      st.className = "autotune-status";
      st.textContent = status;
      el.appendChild(st);
    }
    const why = String(sug.notes || "").trim();
    if (why) {
      const det = document.createElement("details");
      det.className = "autotune-why";
      const sm = document.createElement("summary");
      sm.textContent = "Why these settings";
      det.appendChild(sm);
      const body = document.createElement("div");
      body.className = "autotune-why-body";
      for (const line of why.split(/(?<=\.)\s+(?=[A-Za-z])/)) {
        const p = document.createElement("div");
        p.textContent = line;
        body.appendChild(p);
      }
      det.appendChild(body);
      el.appendChild(det);
    }
  }

  async function runAutoTune() {
    const btn = $("#btn-autotune");
    const notes = $("#autotune-notes");
    const sel = $("#set-vram-tier");
    const modelPath = ($("#set-model")?.value || "").trim();
    const tier = Number(sel?.value || 0);
    if (!modelPath) {
      toast("pick a model first — auto-tune needs to know its size", "warn", 3000);
      return;
    }
    if (!tier) {
      toast("pick a VRAM tier (or leave on Manual to skip auto-tune)", "warn", 3000);
      return;
    }
    // The tuner reads the saved floor and K/V cache choices, so persist edits first.
    const tunerPrefs = {};
    const floorField = Number($("#set-min-tok-s")?.value || 0);
    if (floorField > 0 && floorField !== Number(state.settings?.min_decode_tok_s ?? 45)) {
      tunerPrefs.min_decode_tok_s = Math.min(500, Math.max(1, floorField));
    }
    const kvK = $("#set-autotune-kv")?.value || "auto";
    const kvV = $("#set-autotune-kv-v")?.value || "";
    if (kvK !== (state.settings?.autotune_kv_cache || "auto")) tunerPrefs.autotune_kv_cache = kvK;
    if (kvV !== (state.settings?.autotune_kv_cache_v || "")) tunerPrefs.autotune_kv_cache_v = kvV;
    if (Object.keys(tunerPrefs).length) await saveSettings(tunerPrefs);
    if (btn) btn.disabled = true;
    if (notes) notes.textContent = "thinking...";
    try {
      // min_ctx = the ctx we already have: grow-only is the tuner's job, so
      // the returned (ctx, offload) combo always comes from the same solve.
      const curCtx = Number($("#set-ctx")?.value || state.settings.num_ctx || 0) || 0;
      const r = await api("/api/llama/auto-tune", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_path: modelPath, vram_gb: tier, min_ctx: curCtx }),
      });
      const sug = r?.suggested || {};
      // Apply the returned combo verbatim (shared helper: saves every tuner
      // key + mirrors into the drawer fields below).
      const applied = await applyAutoTune(sug);

      // Build a short, friendly notes blob from the server reply + model meta.
      const lines = [];
      const m = r?.model || {};
      const head = [];
      if (m.name) head.push(m.name);
      if (m.quant) head.push(m.quant);
      if (m.size_gb) head.push(`${Number(m.size_gb).toFixed(1)} GB on disk`);
      if (m.is_moe) {
        const tag = m.active_params_b
          ? `MoE ${m.total_params_b || "?"}B-A${m.active_params_b}B`
          : "MoE";
        head.push(tag);
      }
      if (head.length) lines.push(head.join(" · "));
      const v = `target VRAM: ${Number(r?.vram_gb || tier).toFixed(0)} GB${r?.vram_name ? ` (${r.vram_name})` : ""}`;
      lines.push(v);

      // Quant-downshift banner: highest-leverage user-facing recommendation.
      // Rendered with a leading marker so it stands out in the plain-text panel.
      if (sug.quant_downshift) {
        lines.push("");
        lines.push(`>> ${sug.quant_downshift}`);
        lines.push("");
      }
      // Saving here already persisted the tune, so the drawer's Save would see no
      // change and never restart the server; reload now when the tuned model is the
      // running one.
      const baseName = p => String(p || "").split(/[\\/]/).pop();
      const changedLoadKeys = applied ? Object.keys(applied).filter(k => LOAD_TIME_KEYS.includes(k)) : [];
      const reload = changedLoadKeys.length > 0 && state.llamaRunning
        && baseName(state.loadedModel) && baseName(state.loadedModel) === baseName(modelPath);
      renderAutoTuneNotes(notes, lines, sug,
        reload ? "Reloading the model with these settings…"
          : applied ? "Saved — takes effect the next time this model loads."
            : "Already using these settings.");
      if (reload) {
        const tid = "reload-llama";
        toast(`reloading model (${changedLoadKeys.join(", ")})…`, "info", 60000, tid);
        state._reloading = true;
        renderStatus();
        renderModelPill();
        try {
          await requestModelLoad(modelPath);
          toast("model reloaded with the tuned settings", "ok", 3000, tid);
          renderAutoTuneNotes(notes, lines, sug, "Applied — the model is running with these settings.");
        } catch (e) {
          toast("reload failed: " + (e.message || e), "error", 6000, tid);
          renderAutoTuneNotes(notes, lines, sug, "Saved, but the reload failed — load the model again to apply.");
        } finally {
          state._reloading = false;
          renderStatus();
          renderModelPill();
        }
      } else {
        const toastMsg = sug.quant_downshift
          ? "auto-tune applied — quant downshift suggested"
          : "auto-tune applied";
        toast(toastMsg, sug.quant_downshift ? "warn" : "ok", 4000);
      }
    } catch (e) {
      if (notes) notes.textContent = `auto-tune failed: ${e.message || e}`;
      toast("auto-tune failed: " + (e.message || e), "error", 5000);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadSystemContext() {
    const ta = $("#set-sysctx");
    const path = $("#sysctx-path");
    if (!ta) return;
    ta.value = "loading…";
    try {
      const r = await api("/api/system-context");
      ta.value = r.md || "";
      if (path) path.textContent = r.path || "";
    } catch (e) {
      ta.value = `(failed: ${e.message || e})`;
    }
  }
  async function saveSystemContext() {
    const ta = $("#set-sysctx");
    if (!ta) return;
    const btn = $("#btn-sysctx-save");
    if (btn) btn.disabled = true;
    try {
      await api("/api/system-context", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ md: ta.value }) });
    } catch (e) {
      toast("Save failed: " + (e.message || e), "err", 4000, "sysctx-save");
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  return { loadDetectedVram, applyAutoTune, runAutoTune, loadSystemContext, saveSystemContext };
};
