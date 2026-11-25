/* SPDX-License-Identifier: AGPL-3.0-only */
/* speculative draft picker (DFlash / DSpark): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["draft-picker"] = ({
  $, LOAD_TIME_KEYS, api, controlFeedback, drawUsageMark, esc, loadMemories, loadModels,
  normalizedMmprojMode, reflectIdeToggles, refreshGitHubStatus, renderModelPill,
  renderShortcutSettings, renderStatus, requestModelLoad, saveSettings, startBackendLogPoll,
  startShellPoll, state, stopBackendLogPoll, stopShellPoll, syncMmprojModeUi,
  syncThinkingBudgetControl, toast, touchedLoadControls, wbSyncJumps,
}) => {
  // The bridge validates a draft against the model's tokenizer and base-model
  // metadata. This picker lists every draft-named GGUF on disk, marks validated
  // pairs, and keeps the text field as the source of truth for saving.
  const SPEC_DRAFT_OK_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>';
  const SPEC_DRAFT_WARN_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.6 19.5h18.8L12 3z"/><path d="M12 9.5v4.2M12 17.2h.01"/></svg>';

  function setSpecDraftStatus(kind, text) {
    const el = $("#spec-draft-status");
    if (!el) return;
    el.classList.toggle("is-match", kind === "match");
    el.classList.toggle("is-warn", kind === "warn");
    el.innerHTML = (kind === "match" ? SPEC_DRAFT_OK_SVG
      : kind === "warn" ? SPEC_DRAFT_WARN_SVG : "") + esc(text || "");
  }

  function specStrategyWantsDraft() {
    const value = ($("#set-spec-strategy")?.value || "").toLowerCase();
    return value === "dflash" || value === "dspark";
  }

  function syncSpecDraftSelection() {
    const sel = $("#set-spec-draft-select");
    const input = $("#set-spec-draft-model");
    if (!sel || !input) return;
    const path = (input.value || "").trim();
    const match = Array.from(sel.options).find(o => o.value && o.value === path);
    sel.value = match ? path : "";
  }

  // The strategy a draft file runs as: maya's helper is DSpark; a draft GGUF
  // says what it is in its architecture. "" when it can't be told.
  function specDraftKind(path) {
    const data = state._specDrafts || {};
    if (data.engine === "maya") return "dspark";
    const want = path || data.paired || "";
    const d = (data.drafts || []).find(x => x.path === want);
    const arch = String(d?.architecture || "").toLowerCase();
    return arch.includes("dspark") ? "dspark" : arch.includes("dflash") ? "dflash" : "";
  }

  function renderSpecDraftStatus(data) {
    if ((state.settings?.spec_draft_mode || "auto") === "off" && !specStrategyWantsDraft()) {
      setSpecDraftStatus("", "decoder unloaded — choose a draft below to turn it back on");
      return;
    }
    if (!specStrategyWantsDraft()) { setSpecDraftStatus("", ""); return; }
    const input = ($("#set-spec-draft-model")?.value || "").trim();
    const selected = data && data.selected_match;
    const paired = data && data.paired_match;
    if (input && selected && selected.path === input) {
      if (selected.matched) {
        setSpecDraftStatus("match", `matches this model — ${(selected.reasons || []).join(" · ")}`);
      } else {
        setSpecDraftStatus("warn", `not validated for this model — ${(selected.reasons || []).join(" · ")}`);
      }
      return;
    }
    if (paired && paired.matched) {
      const name = (paired.path || "").split(/[\\/]/).pop();
      setSpecDraftStatus("match", `${name} detected — ${(paired.reasons || []).join(" · ")}`);
      return;
    }
    const drafts = (data && data.drafts) || [];
    if (drafts.length) {
      setSpecDraftStatus("warn", "draft files found, but none validate for this model");
    } else {
      setSpecDraftStatus("warn", "no matching draft found beside this model");
    }
  }

  let _specDraftRequest = 0;
  async function refreshSpecDrafts() {
    const sel = $("#set-spec-draft-select");
    if (!sel) return;
    const modelPath = ($("#set-model")?.value || state.loadedModel || state.settings?.model_path || "").trim();
    const input = $("#set-spec-draft-model");
    const request = ++_specDraftRequest;
    if (!modelPath) {
      sel.innerHTML = '<option value="">Auto — select a model first</option>';
      setSpecDraftStatus("", "");
      return;
    }
    try {
      const chosen = (input?.value || "").trim();
      const query = `/api/models/spec-drafts?model_path=${encodeURIComponent(modelPath)}`
        + (chosen ? `&draft_path=${encodeURIComponent(chosen)}` : "");
      const data = await api(query);
      if (request !== _specDraftRequest) return;
      if (!data || data.error || !Array.isArray(data.drafts)) {
        // Endpoint missing (bridge not restarted yet) or failing: keep the saved path
        // selectable and say "could not check" rather than "no match".
        const chosenPath = (input?.value || "").trim();
        sel.innerHTML = "";
        const unavailable = document.createElement("option");
        unavailable.value = "";
        unavailable.textContent = "Auto — draft list unavailable";
        sel.appendChild(unavailable);
        if (chosenPath) {
          const option = document.createElement("option");
          option.value = chosenPath;
          option.textContent = `${chosenPath.split(/[\\/]/).pop()} — manual`;
          sel.appendChild(option);
        }
        syncSpecDraftSelection();
        setSpecDraftStatus("warn", "draft list unavailable — restart the bridge to enable validation");
        return;
      }
      const drafts = data.drafts || [];
      sel.innerHTML = "";
      const auto = document.createElement("option");
      auto.value = "";
      auto.textContent = data.paired
        ? `Auto — ${String(data.paired).split(/[\\/]/).pop()}`
        : "Auto — no matching draft found";
      sel.appendChild(auto);
      for (const d of drafts) {
        const option = document.createElement("option");
        option.value = d.path;
        const gb = d.size ? ` · ${(d.size / 1e9).toFixed(2)} GB` : "";
        option.textContent = `${d.name}${d.matched ? " — matches" : " — not validated"}${gb}`;
        sel.appendChild(option);
      }
      if (chosen && !drafts.some(d => d.path === chosen)) {
        const option = document.createElement("option");
        option.value = chosen;
        option.textContent = `${chosen.split(/[\\/]/).pop()} — manual`;
        sel.appendChild(option);
      }
      state._specDrafts = data;
      syncSpecDraftSelection();
      renderSpecDraftStatus(data);
    } catch (error) {
      if (request !== _specDraftRequest) return;
      console.warn("spec draft lookup failed", error);
      setSpecDraftStatus("warn", "could not load draft list");
    }
  }

  function populateSettingsForm() {
    $("#set-offline-mode").checked = !!state.settings.offline_mode;
    $("#offline-status").textContent = !!state.settings.offline_mode !== !!state.settings.offline_active
      ? "Saved. Quit and reopen Accuretta to apply this change."
      : state.settings.offline_active ? "Offline mode is active." : "Online features are available.";
    const s = state.settings;
    const fill = (id, v) => { const el = $(id); if (el) el.value = v ?? ""; };

    // models folder + dropdown
    const dirInput = $("#set-models-dir");
    if (dirInput) dirInput.value = state.modelsDir || s.models_dir || "";
    const modelSel = $("#set-model");
    if (modelSel) {
      modelSel.innerHTML = "";
      const list = state.modelsList || [];
      const loaded = state.loadedModel || s.model_path || "";
      if (!list.length) {
        const msg = state.modelsError || "pick a models folder above";
        modelSel.innerHTML = `<option value="">(${esc(msg)})</option>`;
      } else {
        for (const m of list) {
          const o = document.createElement("option");
          o.value = m.path; o.textContent = m.name + (m.size_gb ? `  (${m.size_gb} GB)` : "")
            + (m.needs_maya ? "  · needs the Maya engine"
              : m.llama_supported === false ? "  · needs a newer llama.cpp" : "");
          if (m.path === loaded || m.loaded) o.selected = true;
          modelSel.appendChild(o);
        }
      }
    }
    const hint = $("#set-model-hint");
    if (hint) {
      if (state._reloading) {
        hint.textContent = "reloading — new flags applying to llama-server…";
      } else if (state.llamaRunning && state.loadedModel) {
        const name = state.loadedModel.split(/[\\/]/).pop();
        hint.textContent = `loaded: ${name}`;
      } else if (state.modelsDir) {
        hint.textContent = `${(state.modelsList || []).length} model(s) in ${state.modelsDir}`;
      } else {
        hint.textContent = "selecting a model loads it into llama-server.";
      }
    }
    const visionSel = $("#set-vision");
    if (visionSel) {
      visionSel.innerHTML = "";
      const emptyOpt = document.createElement("option");
      emptyOpt.value = ""; emptyOpt.textContent = "(none)";
      visionSel.appendChild(emptyOpt);
      for (const m of (state.modelsList || [])) {
        const o = document.createElement("option");
        o.value = m.path; o.textContent = m.name;
        if (m.path === s.vision_model) o.selected = true;
        visionSel.appendChild(o);
      }
    }
    // Projector state is explicit: off must remain off across reloads rather
    // than falling through to sibling auto-detection.
    const mmMode = $("#set-mmproj-mode");
    if (mmMode) mmMode.value = normalizedMmprojMode(s);
    const mmInp = $("#set-mmproj");
    if (mmInp) mmInp.value = s.mmproj_path || "";
    const mmDev = $("#set-mmproj-device");
    if (mmDev) mmDev.value = s.mmproj_device === "gpu" ? "gpu" : "cpu";
    syncMmprojModeUi();

    fill("#set-ctx", s.num_ctx);
    fill("#set-gpu", s.num_gpu);
    fill("#set-batch", s.num_batch);
    fill("#set-thread", s.num_thread);
    fill("#set-predict", s.num_predict);
    fill("#set-max-output", s.max_output_tokens);
    const kvSel = $("#set-kv");
    if (kvSel) kvSel.value = s.kv_cache_type || "q8_0";
    const kvVSel = $("#set-kvv");
    if (kvVSel) kvVSel.value = s.kv_cache_type_v || "";
    fill("#set-vram-reserve", s.vram_reserve_gb ?? 0.25);
    fill("#set-ram-reserve", s.safety_reserve_gb ?? 1.0);
    fill("#set-min-tok-s", s.min_decode_tok_s ?? 45);
    // VRAM tier picker (auto-tune persistence — 0 = Manual)
    const vramSel = $("#set-vram-tier");
    if (vramSel) vramSel.value = String(s.vram_tier_gb ?? 0);
    const tuneKvK = $("#set-autotune-kv");
    if (tuneKvK) tuneKvK.value = s.autotune_kv_cache || "auto";
    const tuneKvV = $("#set-autotune-kv-v");
    if (tuneKvV) tuneKvV.value = s.autotune_kv_cache_v || "";
    // advanced llama-server flags
    fill("#set-ncmoe", s.n_cpu_moe ?? 0);
    fill("#set-ubatch", s.n_ubatch ?? 0);
    fill("#set-parallel", s.n_parallel ?? 1);
    $("#sw-flash")?.classList.toggle("on", s.flash_attn !== false);
    // spec_strategy, falling back to enable_speculative for settings.json files
    // older than the MTP option. Mirrors the bridge's launch-time rule: an
    // explicit legacy `enable_speculative=false` wins even if spec_strategy came
    // from DEFAULTS during the merge, so the UI shows what was actually launched.
    const specSel = $("#set-spec-strategy");
    if (specSel) {
      let strat = (s.spec_strategy || "").trim().toLowerCase();
      if (s.enable_speculative === false) {
        strat = "off";
      } else if (!["off", "ngram-mod", "draft-mtp", "dflash", "dspark"].includes(strat)) {
        strat = "ngram-mod";
      }
      specSel.value = strat;
    }
    $("#sw-nowarmup")?.classList.toggle("on", !!s.no_warmup);
    $("#sw-metrics")?.classList.toggle("on", !!s.enable_metrics);
    const xa = $("#set-extra-args");
    if (xa) xa.value = s.llama_extra_args || "";
    const draftModel = $("#set-spec-draft-model");
    if (draftModel) draftModel.value = s.spec_draft_model || "";
    syncSpecDraftSelection();
    refreshSpecDrafts();
    fill("#set-temp", s.temperature);
    fill("#set-topp", s.top_p);
    fill("#set-topk", s.top_k ?? 40);
    fill("#set-minp", s.min_p ?? 0.05);
    fill("#set-repeat", s.repeat_penalty ?? 1.1);
    fill("#set-presence", s.presence_penalty ?? 0);
    fill("#set-frequency", s.frequency_penalty ?? 0);
    $("#sw-thinking")?.classList.toggle("on", s.enable_thinking !== false);
    fill("#set-think-budget", s.thinking_budget ?? 2048);
    syncThinkingBudgetControl();
    const reasoningCap = $("#set-reasoning-capability");
    if (reasoningCap) reasoningCap.value = s.reasoning_capability_override || "auto";
    const verifyMode = $("#set-verification-mode");
    if (verifyMode) verifyMode.value = s.verification_mode || "auto";
    fill("#set-subagent-tasks", s.subagent_max_tasks ?? 5);
    fill("#set-subagent-turns", s.subagent_max_turns ?? 20);
    const themeSel = $("#set-theme");
    if (themeSel) themeSel.value = normalizeTheme(s.theme || "light");
    fillAppearanceColors();
    $("#sw-web").classList.toggle("on", s.allow_web_preview !== false);

    // IDE toggles mirror back into the composer chips
    reflectIdeToggles();

    // desktop automation
    $("#sw-desktop-enabled")?.classList.toggle("on", !!s.desktop_enabled);
    $("#sw-sound-notifications")?.classList.toggle("on", s.sound_notifications !== false);
    $("#sw-notch-enabled")?.classList.toggle("on", !!s.notch_enabled);
    $("#sw-red-team-enabled")?.classList.toggle("on", !!s.red_team_enabled);
    $("#sw-analysis-tools-enabled")?.classList.toggle("on", !!s.analysis_tools_enabled);
    fill("#set-ghidra-heap", s.ghidra_max_heap_gb ?? 0);
    fill("#set-ghidra-idle", s.ghidra_idle_minutes ?? 5);
    $("#sw-passive-model-telemetry")?.classList.toggle("on", s.passive_model_telemetry !== false);
    $("#sw-passive-model-telemetry")?.setAttribute(
      "aria-checked", String(s.passive_model_telemetry !== false));
    $("#sw-rt-force-exploit")?.classList.toggle("on", !!s.rt_force_exploit);
    $("#sw-rt-spoof-xff")?.classList.toggle("on", !!s.rt_spoof_xff);
    $("#sw-rt-jitter")?.classList.toggle("on", s.rt_jitter !== false);
    fill("#set-rt-proxy", s.rt_proxy || "");
    fill("#set-rt-evidence-days", s.rt_evidence_retention_days ?? 30);
    fill("#set-shodan-key", s.shodan_api_key || "");
    $("#sw-discord-enabled")?.classList.toggle("on", !!s.discord_enabled);
    fill("#set-discord-token", s.discord_bot_token || "");
    fill("#set-discord-owner", s.discord_owner_id || "");
    $("#sw-security-scheduled")?.classList.toggle("on", s.security_scheduled !== false);
    fill("#set-security-interval", s.security_interval_minutes || 30);
    if ($("#set-security-threshold")) $("#set-security-threshold").value = s.security_notify_threshold || "medium";
    $("#sw-security-discord")?.classList.toggle("on", s.security_discord_notify !== false);
    $("#sw-remote-enabled")?.classList.toggle("on", s.remote_access_enabled !== false);
    fill("#set-remote-timeout", s.remote_command_timeout || 120);
    fill("#set-remote-file-mb", s.remote_file_max_mb || 100);
    const al = $("#set-desktop-allowlist");
    if (al) al.value = (s.desktop_app_allowlist || []).join("\n");
    fill("#set-desktop-rate", s.desktop_max_actions_per_minute || 30);
    refreshDesktopStatus();
    refreshGitHubStatus();
    // memories panel
    loadMemories();
    renderShortcutSettings();
  }

  async function refreshDesktopStatus() {
    try {
      const r = await api("/api/desktop/status");
      const badge = $("#desktop-deps-badge");
      if (badge) {
        const missing = [];
        if (!r.have_uiautomation) missing.push("uiautomation");
        if (!r.have_pygetwindow) missing.push("pygetwindow");
        badge.textContent = r.blind_control_ready
          ? `blind control ready · ${r.vision_ready ? "vision active" : "vision optional"}`
          : `blind control unavailable${missing.length ? ` · pip install ${missing.join(" ")}` : ""}`;
        badge.style.color = r.blind_control_ready ? "var(--mint-3)" : "var(--danger)";
      }
      const ps = $("#desktop-panic-state");
      if (ps) {
        ps.textContent = r.panic ? "PANIC — all actions blocked" : "ready";
        ps.style.color = r.panic ? "var(--danger)" : "var(--fg-faint)";
      }
    } catch {}
  }
  // Drawer controls that feed load-time keys, and every payload key each one
  // owns. Save only sends (and only reloads for) the ones touched since the
  // drawer opened. Comparing the whole form against state.settings would reload
  // on every save: keys the form never sends (spec_draft_n_max, moe_cache_mib)
  // always look changed, and a background auto-tune that moved num_ctx makes the
  // stale form value look like an edit.
  const LOAD_CONTROL_KEYS = Object.freeze({
    "set-model": ["model_path", "model"],
    "set-vision": ["vision_model"],
    "set-ctx": ["num_ctx"],
    "set-gpu": ["num_gpu"],
    "set-batch": ["num_batch"],
    "set-thread": ["num_thread"],
    "set-kv": ["kv_cache_type"],
    "set-kvv": ["kv_cache_type_v"],
    "set-ncmoe": ["n_cpu_moe"],
    "set-ubatch": ["n_ubatch"],
    "set-parallel": ["n_parallel"],
    "sw-flash": ["flash_attn"],
    "set-spec-strategy": ["spec_strategy", "spec_strategy_source", "enable_speculative", "spec_draft_mode"],
    "set-spec-draft-model": ["spec_draft_model", "spec_draft_mode"],
    "set-spec-draft-select": ["spec_draft_model", "spec_draft_mode"],
    "sw-nowarmup": ["no_warmup"],
    "sw-metrics": ["enable_metrics"],
    "set-extra-args": ["llama_extra_args"],
    "set-mmproj-mode": ["mmproj_mode", "mmproj_auto", "mmproj_path"],
    "set-mmproj": ["mmproj_mode", "mmproj_auto", "mmproj_path"],
    "set-mmproj-device": ["mmproj_device"],
  });
  const LOAD_CONTROL_SWITCHES = new Set(["sw-flash", "sw-nowarmup", "sw-metrics"]);
  function markLoadControlTouched(id) {
    if (LOAD_CONTROL_KEYS[id]) touchedLoadControls.add(id);
  }
  function wireLoadControlTracking() {
    const drawer = $("#settings-drawer");
    if (!drawer) return;
    // Only real user events count: populateSettingsForm and the auto-tune
    // mirror set .value directly, which fires nothing.
    const onEdit = (e) => {
      if (!e.isTrusted) return;
      const id = e.target?.id;
      if (id && !LOAD_CONTROL_SWITCHES.has(id)) markLoadControlTouched(id);
    };
    drawer.addEventListener("input", onEdit, true);
    drawer.addEventListener("change", onEdit, true);
    drawer.addEventListener("click", (e) => {
      if (!e.isTrusted) return;
      const sw = e.target?.closest?.("[id]");
      if (sw && LOAD_CONTROL_SWITCHES.has(sw.id)) markLoadControlTouched(sw.id);
    }, true);
  }

  async function collectAndSaveSettings() {
    const n = (id) => Number($(id).value);
    const modelPath = $("#set-model").value || "";
    const mmprojMode = $("#set-mmproj-mode")?.value || "off";
    const payload = {
      model_path: modelPath,
      model: modelPath ? modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, "") : "",
      models_dir: ($("#set-models-dir")?.value || "").trim(),
      vision_model: $("#set-vision").value,
      num_ctx: n("#set-ctx") || 8192,
      num_gpu: n("#set-gpu"),
      num_batch: n("#set-batch") || 512,
      num_thread: n("#set-thread"),
      num_predict: n("#set-predict"),
      max_output_tokens: n("#set-max-output") || 8192,
      kv_cache_type: $("#set-kv")?.value || "q8_0",
      kv_cache_type_v: $("#set-kvv")?.value || "",
      vram_reserve_gb: Math.max(0, Number($("#set-vram-reserve")?.value || 0.25)),
      safety_reserve_gb: Math.max(0, Number($("#set-ram-reserve")?.value || 1.0)),
      min_decode_tok_s: Math.min(500, Math.max(1, Number($("#set-min-tok-s")?.value || 45))),
      n_cpu_moe: Math.max(0, n("#set-ncmoe") || 0),
      n_ubatch: Math.max(0, n("#set-ubatch") || 0),
      n_parallel: Math.max(1, n("#set-parallel") || 1),
      flash_attn: $("#sw-flash")?.classList.contains("on") !== false,
      // Write both spec_strategy and the legacy enable_speculative so anything still
      // reading the old field (the launch-time legacy rule) agrees with the pick.
      spec_strategy: ($("#set-spec-strategy")?.value || "ngram-mod"),
      spec_strategy_source: "manual",
      spec_draft_model: ($("#set-spec-draft-model")?.value || "").trim(),
      // Explicit draft intent re-arms auto-pairing; otherwise the saved mode stands,
      // so an unloaded decoder stays unloaded across saves.
      spec_draft_mode: ((($("#set-spec-draft-model")?.value || "").trim()
        || (($("#set-spec-strategy")?.value || "").toLowerCase() === "dflash")
        || (($("#set-spec-strategy")?.value || "").toLowerCase() === "dspark"))
        ? "auto" : (state.settings?.spec_draft_mode || "auto")),
      enable_speculative: ($("#set-spec-strategy")?.value || "ngram-mod") !== "off",
      no_warmup: !!$("#sw-nowarmup")?.classList.contains("on"),
      enable_metrics: !!$("#sw-metrics")?.classList.contains("on"),
      llama_extra_args: ($("#set-extra-args")?.value || "").trim(),
      mmproj_mode: mmprojMode,
      mmproj_auto: mmprojMode === "auto",
      mmproj_path: mmprojMode === "manual" ? ($("#set-mmproj")?.value || "").trim() : "",
      mmproj_device: $("#set-mmproj-device")?.value === "gpu" ? "gpu" : "cpu",
      vram_tier_gb: Math.max(0, Number($("#set-vram-tier")?.value || 0)),
      autotune_kv_cache: $("#set-autotune-kv")?.value || "auto",
      autotune_kv_cache_v: $("#set-autotune-kv-v")?.value || "",
      temperature: n("#set-temp"),
      top_p: n("#set-topp"),
      top_k: n("#set-topk"),
      min_p: n("#set-minp"),
      repeat_penalty: n("#set-repeat"),
      presence_penalty: n("#set-presence"),
      frequency_penalty: n("#set-frequency"),
      enable_thinking: $("#sw-thinking")?.classList.contains("on") !== false,
      thinking_budget: n("#set-think-budget"),
      reasoning_capability_override: $("#set-reasoning-capability")?.value || "auto",
      verification_mode: $("#set-verification-mode")?.value || "auto",
      subagent_max_tasks: n("#set-subagent-tasks"),
      subagent_max_turns: n("#set-subagent-turns"),
      theme: ($("#set-theme")?.value || "light"),
      custom_palettes: appearanceDraft(),
      allow_web_preview: $("#sw-web").classList.contains("on"),
      desktop_enabled: $("#sw-desktop-enabled")?.classList.contains("on") || false,
      sound_notifications: $("#sw-sound-notifications")?.classList.contains("on") ?? true,
      notch_enabled: $("#sw-notch-enabled")?.classList.contains("on") || false,
      red_team_enabled: $("#sw-red-team-enabled")?.classList.contains("on") || false,
      analysis_tools_enabled: $("#sw-analysis-tools-enabled")?.classList.contains("on") || false,
      ghidra_max_heap_gb: Math.min(64, Math.max(0, Number($("#set-ghidra-heap")?.value || 0))),
      ghidra_idle_minutes: Math.min(120, Math.max(1, Number($("#set-ghidra-idle")?.value || 5))),
      passive_model_telemetry: $("#sw-passive-model-telemetry")
        ? $("#sw-passive-model-telemetry").classList.contains("on")
        : true,
      rt_force_exploit: $("#sw-rt-force-exploit")?.classList.contains("on") || false,
      rt_proxy: ($("#set-rt-proxy")?.value || "").trim(),
      rt_spoof_xff: $("#sw-rt-spoof-xff")?.classList.contains("on") || false,
      rt_jitter: $("#sw-rt-jitter") ? $("#sw-rt-jitter").classList.contains("on") : true,
      rt_evidence_retention_days: Math.max(0, Math.min(3650, n("#set-rt-evidence-days") || 0)),
      shodan_api_key: ($("#set-shodan-key")?.value || "").trim(),
      discord_enabled: $("#sw-discord-enabled")?.classList.contains("on") || false,
      discord_bot_token: ($("#set-discord-token")?.value || "").trim(),
      discord_owner_id: ($("#set-discord-owner")?.value || "").trim(),
      security_scheduled: $("#sw-security-scheduled")?.classList.contains("on") !== false,
      security_interval_minutes: Math.max(5, Math.min(720, n("#set-security-interval") || 30)),
      security_notify_threshold: ($("#set-security-threshold")?.value || "medium"),
      security_discord_notify: $("#sw-security-discord")?.classList.contains("on") !== false,
      remote_access_enabled: $("#sw-remote-enabled")?.classList.contains("on") !== false,
      remote_command_timeout: Math.max(10, Math.min(3600, n("#set-remote-timeout") || 120)),
      remote_file_max_mb: Math.max(1, Math.min(1024, n("#set-remote-file-mb") || 100)),
      desktop_app_allowlist: ($("#set-desktop-allowlist")?.value || "")
        .split("\n").map(x => x.trim()).filter(Boolean),
      desktop_max_actions_per_minute: Math.max(1, Math.min(300, n("#set-desktop-rate") || 30)),
      use_tailwind_cdn: !!state.settings.use_tailwind_cdn,
      ide_multifile: !!state.settings.ide_multifile,
    };

    // Drop load-time keys whose controls the user never touched, so an
    // untouched form can't overwrite the saved/auto-tuned value or trigger
    // a reload. Then reload only for touched keys whose value really moved.
    const touchedKeys = new Set();
    for (const id of touchedLoadControls) for (const k of LOAD_CONTROL_KEYS[id]) touchedKeys.add(k);
    for (const keys of Object.values(LOAD_CONTROL_KEYS)) {
      for (const k of keys) if (!touchedKeys.has(k)) delete payload[k];
    }
    const prev = state.settings || {};
    const changedLoadKeys = LOAD_TIME_KEYS.filter(k =>
      k in payload && String(prev[k] ?? "") !== String(payload[k] ?? ""));
    const reloadPath = payload.model_path || prev.model_path || "";

    await saveSettings(payload);
    touchedLoadControls.clear();
    applyTheme(payload.theme || "light");
    if (String(prev.reasoning_capability_override || "auto") !== payload.reasoning_capability_override) {
      await loadModels();
    }

    if (changedLoadKeys.length && reloadPath) {
      const tid = "reload-llama";
      toast(`reloading model (${changedLoadKeys.join(", ")})…`, "info", 60000, tid);
      // Flip every "loaded" indicator to "reloading" while the server spawns (a 16
      // GB model can take minutes).
      state._reloading = true;
      const rname = String(reloadPath).split(/[\\/]/).pop();
      const hint = $("#set-model-hint");
      if (hint) hint.textContent = `reloading ${rname} (${changedLoadKeys.join(", ")})…`;
      renderStatus();
      renderModelPill();
      try {
        await requestModelLoad(reloadPath);
        toast("model reloaded with new settings", "ok", 2500, tid);
      } catch (e) {
        toast("reload failed: " + (e.message || e), "error", 6000, tid);
        if (hint) hint.textContent = `reload failed — ${rname}`;
      } finally {
        state._reloading = false;
        renderStatus();
        renderModelPill();
      }
    }
    controlFeedback($("#btn-save-settings"));
  }

  // THEME_CYCLE is the source of truth for the selector and cycle controls.
  // Retired theme names map to a supported theme so old settings never leave the
  // document in a theme with no CSS.
  const THEME_MIGRATIONS = Object.freeze({
    neobrutalism: "amaranth",
    "neobrutalism-dark": "amaranth",
    kinetic: "aperture",
    // Retired dark themes fold into Opal, the one that replaced them.
    aurora: "opal",
    nebula: "opal",
    operator: "opal",
    velvet: "opal",
  });
  const THEME_CYCLE = ["dark", "dim", "retro", "opal", "showcase", "neumorphic", "amaranth", "aperture", "aperture-dark", "atelier", "soft", "pastel", "cartograph", "folio", "light"];
  const THEME_ICONS = {
    dark:              "ph ph-moon",
    dim:               "ph ph-moon-stars",
    retro:             "ph ph-sun-horizon",
    opal:              "ph ph-diamond",
    showcase:          "ph ph-frame-corners",
    neumorphic:        "ph ph-drop-half",
    amaranth:          "ph ph-circle-half",
    aperture:          "ph ph-aperture",
    "aperture-dark":   "ph ph-moon",
    atelier:           "ph ph-pen-nib",
    soft:              "ph ph-cloud",
    pastel:            "ph ph-flower-tulip",
    cartograph:        "ph ph-compass",
    folio:             "ph ph-book-open",
    light:             "ph ph-sun",
  };
  function appearanceDraft() {
    const palettes = { ...(state.settings.custom_palettes || {}) };
    if (document.getElementById('appearance-preview')?.dataset.dirty === 'true') {
      palettes[$('#set-theme').value] = Object.fromEntries(['background', 'surface', 'accent'].map(key => [key, $('#set-color-' + key).value]));
    }
    return palettes;
  }
  function fillAppearanceColors() {
    const css = getComputedStyle(document.documentElement);
    for (const [key, token] of [['background', '--bg'], ['surface', '--bg-raised'], ['accent', '--accent']]) {
      const input = $('#set-color-' + key);
      if (input) {
        const probe = document.createElement('span');
        probe.style.color = css.getPropertyValue(token).trim();
        document.body.appendChild(probe);
        const channels = getComputedStyle(probe).color.match(/\d+/g);
        input.value = '#' + channels.slice(0, 3).map(value => Number(value).toString(16).padStart(2, '0')).join('');
        probe.remove();
      }
    }
    previewAppearanceColors();
    $('#appearance-preview').dataset.dirty = 'false';
  }
  function previewAppearanceColors() {
    const colors = Object.fromEntries(['background', 'surface', 'accent'].map(key => [key, $('#set-color-' + key).value]));
    const preview = $('#appearance-preview');
    const tokens = window.AccurettaAppearance.tokens(colors);
    Object.entries(tokens).forEach(([key, value]) => preview.style.setProperty(key, value));
    preview.style.setProperty('--surface-ink', window.AccurettaAppearance.ink(colors.surface));
    preview.dataset.dirty = 'true';
    $('#appearance-color-hint').textContent = 'Preview only until saved. Text adapts automatically.';
  }
  function normalizeTheme(theme) {
    return THEME_MIGRATIONS[theme] || theme;
  }
  function nextTheme(cur, step = 1) {
    cur = normalizeTheme(cur);
    const n = THEME_CYCLE.length;
    const idx = THEME_CYCLE.indexOf(cur);
    return THEME_CYCLE[((idx + step) % n + n) % n];
  }
  // Every theme control steps through THEME_CYCLE. The theme shows at once and
  // saves behind it, so quick clicks keep stepping instead of repeating one.
  async function stepTheme(step = 1) {
    const next = nextTheme(document.documentElement.dataset.theme || state.settings.theme || "light", step);
    applyTheme(next);
    try { await saveSettings({ theme: next }); }
    catch (error) { toast(`Theme changed here, but it couldn't be saved: ${error.message || error}`, "warn", 3500); }
  }
  // applyTheme takes a theme string from THEME_CYCLE; a boolean from older
  // callers is coerced (true -> "dark", false -> "light").
  function applyTheme(theme) {
    if (theme === true) theme = "dark";
    else if (theme === false) theme = "light";
    theme = normalizeTheme(theme);
    if (!THEME_CYCLE.includes(theme)) theme = "light";
    document.documentElement.dataset.theme = theme;
    window.AccurettaAppearance.apply(theme, state.settings.custom_palettes);
    localStorage.setItem("accuretta:theme", theme);
    const iconClass = THEME_ICONS[theme] || THEME_ICONS.light;
    const topBtn = $("#btn-theme");
    if (topBtn) topBtn.innerHTML = `<i class="${iconClass}"></i>`;
    // Keep the sidebar-foot mirror in sync. Only the <i>'s class is swapped so its
    // inline font-size stays.
    const sideIcon = document.getElementById("btn-theme-side-icon");
    if (sideIcon) sideIcon.className = iconClass;
    if ($("#usage-page")?.classList.contains("open")) drawUsageMark(false);
  }

  function activateTerminalTab(tabId) {
    const tabs = document.querySelectorAll(".term-tab");
    const panes = document.querySelectorAll(".term-tab-pane");
    
    tabs.forEach(tab => {
      const active = tab.dataset.tab === tabId;
      tab.classList.toggle("active", active);
    });
    
    panes.forEach(pane => {
      const active = pane.id === `term-pane-${tabId}`;
      pane.classList.toggle("hidden", !active);
    });
    if (tabId === "backend") startBackendLogPoll();
    else stopBackendLogPoll();
    if (tabId === "shell") startShellPoll();
    else stopShellPoll();
    updateTerminalClearButton(tabId);
    wbSyncJumps();
  }

  function updateTerminalClearButton(tabId) {
    const button = document.getElementById("btn-term-clear");
    if (!button) return;
    const labels = {
      terminal: "Host output",
      sandbox: "WSL Guest output",
      backend: "Backend output",
      shell: "Shell output",
      agentlog: "Agent log",
    };
    const clearable = new Set(["terminal", "sandbox", "agentlog"]).has(tabId);
    const label = labels[tabId] || "active console output";
    button.disabled = !clearable;
    button.title = clearable ? `Clear ${label}` : `${label} is live and cannot be cleared here`;
    button.setAttribute("aria-label", button.title);
  }
  return {
    specStrategyWantsDraft, syncSpecDraftSelection, specDraftKind, refreshSpecDrafts,
    populateSettingsForm, refreshDesktopStatus, markLoadControlTouched, wireLoadControlTracking,
    collectAndSaveSettings, THEME_CYCLE, appearanceDraft, fillAppearanceColors,
    previewAppearanceColors, normalizeTheme, nextTheme, stepTheme, applyTheme, activateTerminalTab,
  };
};
