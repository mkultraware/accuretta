/* SPDX-License-Identifier: AGPL-3.0-only */
/* data loading: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["data-loading"] = ({
  $, api, currentClientHint, detectClientOs, esc, isMobile, normalizeTheme, refreshDraftPairs,
  renderModelPill, renderReasoningEffort, renderRemoteAccess, renderStatus, renderWorkspaceIdentity,
  state, stopStreaming, toast,
}) => {
  async function loadSettings() {
    state.settings = await api("/api/settings");
    if (state.settings) {
      const savedMode = String(state.settings.composer_mode || "agent").toLowerCase();
      state.mode = savedMode === "ide" ? "ide" : "agent";
      const savedEffort = String(state.settings.reasoning_effort || "auto").toLowerCase();
      state.reasoningEffort = ["auto", "low", "medium", "high"].includes(savedEffort)
        ? savedEffort : "auto";
      const storedTheme = state.settings.theme || "light";
      const theme = normalizeTheme(storedTheme);
      state.settings.theme = theme;
      localStorage.setItem("accuretta:theme", theme);
      if (theme !== storedTheme) {
        try {
          state.settings = await api("/api/settings", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ theme }),
          });
        } catch (error) {
          state.settings.theme = theme;
          console.warn("theme migration could not be persisted:", error);
        }
      }
    }
  }
  // Every settings write runs through one chain. Toggles auto-save on click,
  // and two concurrent POSTs could resolve out of order, letting a stale
  // response overwrite state.settings. Serializing also keeps the bridge's
  // read-modify-write from interleaving. Callers still await the promise.
  let settingsSaveChain = Promise.resolve();
  function saveSettings(update) {
    const run = async () => {
      const prevModel = state.settings.model;
      // Tagged so the global unhandledrejection handler can tell the user when
      // a caller (most one-click toggles) doesn't handle the failure itself.
      const fail = (message) => Object.assign(new Error(message), { settingsSave: true });
      let saved;
      try {
        saved = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(update),
        });
      } catch (error) {
        throw fail(`the app didn't answer (${error.message || error})`);
      }
      if (!saved || saved.error) throw fail(saved?.error || "Settings could not be saved");
      state.settings = saved;
      // model changed mid-stream → abort so next send uses fresh model cleanly
      if (update.model && update.model !== prevModel && state.streaming) {
        stopStreaming();
      }
      renderStatus();
      renderModelPill();
      return saved;
    };
    settingsSaveChain = settingsSaveChain.catch(() => {}).then(run);
    return settingsSaveChain;
  }

  let preferenceSaveChain = Promise.resolve();
  function persistUiPreference(update) {
    Object.assign(state.settings, update);
    preferenceSaveChain = preferenceSaveChain
      .catch(() => {})
      .then(async () => {
        const saved = await api("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(update),
        });
        Object.assign(state.settings, saved || {});
      })
      .catch(error => {
        toast("could not save preference: " + (error.message || error), "error", 5000);
      });
    return preferenceSaveChain;
  }
  async function loadWorkspace() {
    state.workspace = await api("/api/workspace");
  }
  async function loadChats({ preserveLoaded = true } = {}) {
    const incoming = await api("/api/chats?summary=1");
    if (preserveLoaded && state.chats?.chats) {
      for (const id of incoming.order || []) {
        const loaded = state.chats.chats[id];
        if (Array.isArray(loaded?.messages) && incoming.chats?.[id]) {
          incoming.chats[id].messages = loaded.messages;
          incoming.chats[id]._summary = false;
          incoming.chats[id]._loadedUpdated = loaded._loadedUpdated;
        }
      }
    }
    state.chats = incoming;
  }
  async function loadModels() {
    try {
      const r = await api("/api/models");
      state.modelsDir = r.models_dir || "";
      state.loadedModel = r.loaded_model || "";
      state.missingModel = r.missing_model || "";
      if (state.missingModel && state._missingModelToasted !== state.missingModel) {
        state._missingModelToasted = state.missingModel;
        toast(`${String(state.missingModel).split(/[\\/]/).pop()} was moved or deleted. Pick another model to start the backend.`,
          "warn", 9000, "missing-model");
      }
      state.llamaRunning = !!r.llama_running;
      // Native vision (model booted with --mmproj) drives the model-pill badge and
      // the Settings hint: whether attached images reach the chat model directly or
      // go through the OCR side model.
      state.visionCapable = !!r.vision_capable;
      state.loadedMmproj = r.loaded_mmproj || "";
      state.reasoningCapability = r.reasoning_capability || { supported: false, mode: "none" };
      state.modelsList = Array.isArray(r.models) ? r.models : [];
      state.models = state.modelsList.map(m => m.name).filter(Boolean);
      refreshDraftPairs();
      if (r.error) state.modelsError = r.error;
      else if (!state.modelsDir) state.modelsError = "no models folder set — pick one above.";
      else if (!state.models.length) state.modelsError = "no .gguf files found in " + state.modelsDir;
      else state.modelsError = "";
      renderReasoningEffort();
    } catch (e) {
      state.models = [];
      state.modelsList = [];
      state.reasoningCapability = { supported: false, mode: "none" };
      state.modelsError = "bridge unreachable: " + (e.message || e);
      renderReasoningEffort();
    }
  }

  function renderClientContext() {
    const data = state.remoteAccess || {};
    const machines = Array.isArray(data.machines) ? data.machines : [];
    const select = $("#execution-target-select");
    if (select) {
      const wanted = state.executionTarget || "host";
      select.innerHTML = '<option value="host">Inference PC</option>';
      machines.filter(m => m.status === "ready" && m.enabled !== false).forEach(machine => {
        const option = document.createElement("option");
        option.value = machine.id;
        option.textContent = machine.label || "Paired Mac";
        select.appendChild(option);
      });
      const valid = Array.from(select.options).some(o => o.value === wanted);
      state.executionTarget = valid ? wanted : "host";
      select.value = state.executionTarget;
    }
    const badge = $("#client-context-badge");
    if (badge) {
      const os = detectClientOs();
      const remote = !!data.context?.remote;
      const icon = os === "macOS" ? "ph-laptop" : (isMobile() ? "ph-device-mobile" : "ph-desktop");
      badge.innerHTML = `<i class="ph ${icon}"></i><span>${esc(remote ? `${os} remote` : os)}</span>`;
      badge.dataset.remote = remote ? "1" : "0";
      document.body.classList.toggle("remote-client", remote);
      document.body.classList.toggle("client-macos", os === "macOS");
    }
    renderWorkspaceIdentity();
  }

  async function loadClientContext() {
    const hint = currentClientHint();
    const query = new URLSearchParams({
      os: hint.os,
      mobile: hint.mobile ? "1" : "0",
      secure: hint.secure_context ? "1" : "0",
      target: hint.execution_target,
    });
    const data = await api(`/api/client-context?${query}`);
    state.remoteAccess = data;
    state.clientContext = data.context || null;
    renderClientContext();
    renderRemoteAccess();
    return data;
  }
  return {
    loadSettings, saveSettings, persistUiPreference, loadWorkspace, loadChats, loadModels,
    renderClientContext, loadClientContext,
  };
};
