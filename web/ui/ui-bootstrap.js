/* SPDX-License-Identifier: AGPL-3.0-only */
/* bootstrap: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["bootstrap"] = ({
  $, api, applyAutoTune, applyTheme, checkModelAdvisor, initAppUpdateCheck, initCostWidget,
  initWorkbench, initWorkflowUI, isMobile, loadChats, loadClientContext, loadModels, loadSettings,
  loadWorkspace, newChat, openRequestedChat, refreshModels, renderChatList, renderModelPill,
  renderStatus, renderWorkspace, requestModelLoad, selectChat, startApprovalSync, state,
  subscribeSSE, syncCompactShell, toast, wireEvents,
}) => {
  async function boot() {
    // on-device hint
    if (isMobile()) document.body.classList.add("is-mobile");
    syncCompactShell();

    await Promise.all([
      loadSettings(),
      loadWorkspace(),
      loadChats(),
      loadModels(),
    ]);

    // Pick or create the current chat. A ?chat= deep link (the notch's "Open in
    // main app") wins over the most recent chat when that chat exists.
    const deepChatId = new URLSearchParams(location.search).get("chat");
    if (deepChatId && state.chats.order.includes(deepChatId)) {
      await selectChat(deepChatId);
    } else if (state.chats.order.length) {
      await selectChat(state.chats.order[0]);
    } else {
      await newChat();
    }

    applyTheme(state.settings.theme || "light");
    renderStatus();
    renderModelPill();
    renderChatList();
    renderWorkspace();
    reflectIdeToggles();

    wireEvents();
    initWorkflowUI();
    initWorkbench();
    subscribeSSE();
    startApprovalSync();
    initCostWidget();
    initAppUpdateCheck();
    loadClientContext().catch(() => {});
    setTimeout(() => checkModelAdvisor({ notify: true }).catch(() => {}), 1200);

    // Belt-and-braces chat sync: an SSE reconnect or missed chat_start could hide
    // notch-born sessions until reload, so refresh the rail on any focus or
    // visibility return (the 10s poll can't fire while backgrounded).
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && !state.streaming) {
        loadChats().then(renderChatList).catch(() => {});
      }
    });
    window.addEventListener("focus", () => {
      if (!state.streaming) loadChats().then(renderChatList).catch(() => {});
      openRequestedChat();
    });

    // Background self-correct: re-tune for the loaded model on every boot so
    // settings from older tuner runs heal themselves. Grow-only is enforced
    // server-side via min_ctx. Failures only log to the console.
    autoRetuneOnBoot().catch(e => console.warn("boot auto-retune skipped:", e));
  }

  async function autoRetuneOnBoot() {
    const s = state.settings || {};
    const modelPath = s.model_path || "";
    const tier = Number(s.vram_tier_gb || 0) || 0;
    if (!modelPath || !tier) return;  // nothing to tune for
    // Wait for the model to finish loading. Mid-load, free VRAM and the running
    // instance's share are read at different moments, so the tuner saw a full
    // card and moved every expert layer to RAM (2026-11-21, Inkling at 256k).
    let ready = false;
    for (let i = 0; i < 80 && !ready; i++) {
      try { ready = !!(await api("/api/workbench/status"))?.llama_up; } catch {}
      if (!ready) await new Promise(res => setTimeout(res, 3000));
    }
    if (!ready) return;  // never came up: nothing measured to heal against
    const cur = state.settings || s;
    const curCtx = Number(cur.num_ctx || 0) || 0;
    let r;
    try {
      r = await api("/api/llama/auto-tune", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ model_path: modelPath, vram_gb: tier, min_ctx: curCtx, background: true }),
      });
    } catch (e) {
      throw e;  // bubbled to caller's .catch
    }
    const sug = r?.suggested || {};
    // A background re-tune only heals upward. A plan llama.cpp says won't load,
    // or one that moves more of the model off the GPU than the setup that just
    // booted, comes from a bad measurement, not a better fit: leave it.
    if (sug.does_not_fit) return;
    const curMoe = Number(cur.n_cpu_moe || 0) || 0;
    const curGpu = Number(cur.num_gpu || 99) || 99;
    if (Number(sug.n_cpu_moe || 0) > curMoe) return;
    if (sug.num_gpu != null && Number(sug.num_gpu) < Math.min(curGpu, 99)) return;
    // The tuner enforces grow-only (min_ctx above), so apply the returned (ctx,
    // offload) combo as-is.
    const update = await applyAutoTune(sug);
    if (!update) return;  // already tuned, nothing to do
    // If llama-server is running, restart it so the tuned flags apply now;
    // otherwise the next /api/models/load picks them up.
    if (state.llamaRunning && modelPath) {
      try {
        await requestModelLoad(modelPath);
        await refreshModels();
      } catch (e) { console.warn("boot reload failed:", e); }
    }
    const newCtx = Number(update.num_ctx || 0) || 0;
    if (newCtx > curCtx) {
      toast(`auto-tune grew context: ${curCtx.toLocaleString()} → ${newCtx.toLocaleString()}`, "ok", 4000);
    }
  }

  function reflectIdeToggles() {
    const tw = $("#toggle-tailwind");
    if (tw) tw.classList.toggle("on", !!state.settings.use_tailwind_cdn);
    const mf = $("#toggle-multifile");
    if (mf) mf.classList.toggle("on", !!state.settings.ide_multifile);
  }
  return { boot, reflectIdeToggles };
};
