/* SPDX-License-Identifier: AGPL-3.0-only */
/* foreign live turn + SSE: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["live-events"] = ({
  $, AGENT_AVATAR_HTML, api, appendAgentLog, appendSandboxText, attackRailFinalize,
  collapseWorkBlock, decidedApprovals, ensureCompactionRow, ensureSkillSaveRow, esc,
  finalizeToolGroup, finishCompactionRow, finishSkillSaveRow, foldBoundaryVisibleIdx, handleEvent,
  isRenderedMessage, leaveWelcome, loadChats, loadMemories, loadModels, loadSecurityOverview,
  loadSettings, loadSkills, loadWorkspace, openRequestedChat, osintCardFinalize,
  populateSettingsForm, receiveApproval, reflectApprovalInLiveTurn, refreshDesktopStatus,
  renderApprovals, renderBubble, renderChatList, renderCtxGauge, renderMessages, renderModelPill,
  renderReasoningEffort, renderSkillPill, renderStatus, renderTaskHandoff, renderWorkspace,
  scheduleMessageQueueDrain, scrollToBottom, secretRailFinalize, setGoalModeUI, setStreamingUI,
  state, streamPaceFinish, toast, updateThinkLine, wbDecorateLiveRow,
}) => {
  // A turn can run on another surface while this window is open (the notch
  // overlay's composer). The bridge persists its user message and broadcasts
  // chat_start / tool_start / deltas / final / chat_end on /api/events.
  // Mirroring feeds those events through the same render pipeline as an own turn
  // (handleEvent into a live agent row) for the session on screen; turns in
  // other sessions surface through the rail refresh that accompanies chat_start.
  const FOREIGN_EVENT_TYPES = new Set([
    "steering_applied", "steering_deferred", "delta", "thinking_start",
    "thinking_delta", "thinking_end", "command_requested", "command_spawned",
    "command_finished", "command_not_executed", "tool_start", "tool_stream", "tool_progress",
    "heartbeat", "subagent_progress", "tool_result", "tool_dialect_warning", "tools_unavailable",
    "context_trimmed", "version_saved", "savings", "stats", "final",
    "research_update", "notice", "breach", "rt_phase", "rt_mission",
    "turn_changes", "plan", "chat:note", "error",
  ]);
  // Events that only occur while a turn is running. A window that booted or
  // reconnected mid-turn has no chat_start to catch, so the first of these
  // attaches the mirror; chat_end still ends it. Tighter than FOREIGN_EVENT_TYPES:
  // final/error/savings, version_saved and replayed post-turn events must never
  // open a live row for a finished turn.
  const LIVE_FOREIGN_EVENT_TYPES = new Set([
    "delta", "thinking_start", "thinking_delta", "steering_applied",
    "tool_start", "tool_stream", "tool_progress", "command_requested", "command_spawned",
    "heartbeat",
  ]);
  let foreignTurn = null;   // { chatId, row, ctx, userMsg } while mirroring
  let foreignStarting = false;
  // This page's own turn can end first (Stop aborts its fetch) while the bridge
  // still flushes a few deltas for it onto the bus. Those must not open a mirror
  // of the same turn: it would wait for a chat_end that may already be gone.
  // A new turn from another surface still attaches through its chat_start.
  const OWN_TURN_TAIL_MS = 10000;
  const ownTurnEndedAt = new Map();
  function noteOwnTurnEnded(chatId) {
    if (chatId) ownTurnEndedAt.set(String(chatId), Date.now());
  }

  function maybeAttachForeignTurn(evt) {
    if (foreignTurn || !evt.chat_id) return;
    if (!LIVE_FOREIGN_EVENT_TYPES.has(evt.type)) return;
    const endedAt = ownTurnEndedAt.get(String(evt.chat_id));
    if (endedAt && Date.now() - endedAt < OWN_TURN_TAIL_MS) return;
    beginForeignTurn(evt.chat_id);
  }

  function beginForeignTurn(cid) {
    const id = String(cid || "");
    if (!id || state.streaming || foreignTurn || foreignStarting) return;
    if (id !== String(state.chatId || "")) return;   // only the session on screen
    foreignStarting = true;
    try {
      const agentRow = document.createElement("div");
      agentRow.className = "bubble-row";
      agentRow.innerHTML = `
        ${AGENT_AVATAR_HTML}
        <div class="bubble-col">
          <div class="think-container think-line">
            <div class="think-header" style="cursor: pointer;">
              <i class="ph ph-caret-right think-caret"></i>
              <i class="ph ph-brain think-check-icon"></i>
              <span class="think-title shimmer">Thinking…</span>
            </div>
            <div class="think-content hidden"></div>
          </div>
          <div class="tool-stack" id="tool-stack"></div>
          <div class="bubble agent hidden" id="stream-bubble"></div>
          <div class="bubble-meta streaming">streaming<span class="typing"><span></span><span></span><span></span></span></div>
        </div>`;
      const welcome = document.querySelector("#chat-inner .welcome-screen");
      if (welcome) { leaveWelcome(); $("#chat-inner").innerHTML = ""; }
      $("#chat-inner").appendChild(agentRow);
      window.AccurettaOrb?.setState(agentRow, "thinking");
      scrollToBottom(true);
      const ctx = {
        bubble: agentRow.querySelector("#stream-bubble"),
        toolStack: agentRow.querySelector("#tool-stack"),
        toolCards: new Map(),
        row: agentRow,
        buf: "",
        getBuf() { return ctx.buf; },
        setBuf(v) { ctx.buf = v; },
      };
      agentRow._workStart = Date.now();
      wbDecorateLiveRow(agentRow);
      foreignTurn = { chatId: id, row: agentRow, ctx, userMsg: null };
      state.liveTurn = { chatId: id, row: agentRow, userMsg: null };
      // Same bookkeeping as an own turn: the composer offers a correction,
      // Stop cancels the notch's turn server-side, and a send is queued.
      state.streaming = true;
      state.abortCtl = new AbortController();
      setStreamingUI(true);
      appendAgentLog("Turn started in the notch — mirroring it here.");
    } finally {
      foreignStarting = false;
    }
    // The bridge saves the user turn BEFORE chat_start broadcasts, so the
    // prompt can be fetched and echoed above the live row like a local send.
    // Failure is non-fatal: the reply/error still lands via the events.
    api(`/api/chats/${encodeURIComponent(id)}`).then(chat => {
      if (!foreignTurn || foreignTurn.chatId !== id) return;
      const msgs = Array.isArray(chat?.messages) ? chat.messages : [];
      const u = [...msgs].reverse().find(m => m && m.role === "user" && !m._internal && !m.invisible);
      if (!u) return;
      if (state.messages.some(m => m.role === "user" && m.content === u.content && m.t === u.t)) return;
      foreignTurn.userMsg = u;
      if (state.liveTurn && state.liveTurn.chatId === id) state.liveTurn.userMsg = u;
      if (state.chatId !== id) return;               // user switched away mid-fetch
      state.messages.push(u);
      if (foreignTurn.row.parentNode) foreignTurn.row.parentNode.insertBefore(renderBubble(u), foreignTurn.row);
      scrollToBottom(true);
    }).catch(() => {});
  }

  function handleForeignEvent(evt) {
    if (!foreignTurn || String(evt.chat_id || "") !== foreignTurn.chatId) return false;
    if (!FOREIGN_EVENT_TYPES.has(evt.type)) return false;
    try { handleEvent(evt, foreignTurn.ctx); }
    catch (err) { console.error("mirror event failed on", evt.type, err); }
    return true;
  }

  function finishForeignTurn() {
    const ft = foreignTurn;
    if (!ft) return;
    foreignTurn = null;
    const row = ft.row;
    if (row) {
      streamPaceFinish(row);
      try {
        updateThinkLine(row, false);
        const meta = row.querySelector(".bubble-meta");
        if (meta) {
          meta.classList.remove("streaming");
          meta.querySelectorAll(".typing").forEach(d => d.remove());
        }
        finalizeToolGroup(row);
        collapseWorkBlock(row);
        osintCardFinalize(row);
        secretRailFinalize(row);
        attackRailFinalize(row);
        const revealerDeck = document.querySelector("#revealer-deck");
        if (revealerDeck) delete revealerDeck.dataset.rtEngagement;
        renderTaskHandoff(row, ft.chatId);
      } catch (err) { console.warn("mirror finalize failed", err); }
    }
    state.streaming = false;
    state.abortCtl = null;
    if (state.liveTurn && state.liveTurn.chatId === ft.chatId) state.liveTurn = null;
    setStreamingUI(false, row?._notificationFailed ? "failed" : row?._notificationCancelled ? "stopped" : "completed");
    loadChats().then(renderChatList).catch(() => {});
    scheduleMessageQueueDrain(state.chatId);
    // A turn that ended before its prompt was fetched (or whose row a re-render
    // detached) would leave no answer on screen. The bridge persists the whole
    // turn before chat_end, so rebuild from storage.
    if (state.chatId === ft.chatId && (!ft.userMsg || (row && !row.isConnected))) {
      rebuildActiveChat(ft.chatId);
    }
  }

  async function rebuildActiveChat(chatId) {
    try {
      const c = await api(`/api/chats/${encodeURIComponent(chatId)}`);
      if (state.chatId !== chatId) return;
      state.chats.chats[chatId] = c;
      state.messages = (c.messages || []).filter(isRenderedMessage);
      renderMessages();
    } catch (err) { console.warn("chat rebuild failed", err); }
  }

  // ---------- SSE ----------
  // The bridge's monotonic event-id snapshot across reconnects. A `hello` with a
  // lower snapshot_id than the last one means the bridge restarted (the counter
  // resets on boot), which gets its own toast.
  let _lastSnapshotId = -1;

  let _sseSource = null;
  let _lastSseEventAt = 0;
  let _sseWatchdog = null;

  function subscribeSSE() {
    const handleMessage = (e) => {
      _lastSseEventAt = Date.now();
      let evt;
      try { evt = JSON.parse(e.data); } catch { return; }
      // A mirrored foreign turn owns these events: paint them into its live
      // row exactly like a locally-sent turn. chat_end is its finish line (the
      // bridge persists the whole turn before broadcasting it).
      if (evt.type === "chat_end") {
        if (foreignTurn && String(evt.chat_id || "") === foreignTurn.chatId) {
          finishForeignTurn();
          return;
        }
      } else {
        maybeAttachForeignTurn(evt);
        if (handleForeignEvent(evt)) return;
      }
      if (evt.type === "approval:new") {
        if (evt.approval?.id) receiveApproval(evt.approval);
      } else if (evt.type === "approval:decided") {
        decidedApprovals.add(evt.id);
        const approval = state.approvals.get(evt.id);
        reflectApprovalInLiveTurn(approval, evt.decision || "deny");
        state.approvals.delete(evt.id);
        renderApprovals();
      } else if (evt.type === "settings:update") {
        loadSettings().then(renderStatus).then(renderModelPill);
      } else if (evt.type === "security:update") {
        if ($("#security-page")?.classList.contains("open")) loadSecurityOverview();
      } else if (evt.type === "security:attention") {
        // Notable scheduled-scan finding: refresh regardless of page state so
        // the popup near the security button appears while the user is active.
        loadSecurityOverview();
      } else if (evt.type === "workspace:update") {
        loadWorkspace().then(renderWorkspace);
      } else if (evt.type === "skills:update") {
        loadSkills(true);
      } else if (evt.type === "skill:saving") {
        const chatId = evt.chat_id || state.chatId;
        ensureSkillSaveRow(chatId, evt.skill || "", evt.call_id || "");
        appendAgentLog(`Saving skill: ${evt.skill || "unnamed"}.`);
      } else if (evt.type === "skill:save_finished") {
        const chatId = evt.chat_id || state.chatId;
        finishSkillSaveRow(chatId, evt.outcome || "saved", evt.skill || "", evt.call_id || "");
        appendAgentLog(`Skill save ${evt.outcome || "finished"}: ${evt.skill || "unnamed"}.`);
      } else if (evt.type === "goal_mode") {
        // /goal latch changed (toggle endpoint or a turn). Keep the stored chat
        // flag in sync, and repaint the chip when it's the chat in view.
        const c = state.chats && state.chats.chats && state.chats.chats[evt.chat_id];
        if (c) {
          c.goal_mode = !!evt.active;
          if (evt.goal !== undefined) c.goal_text = evt.goal || "";
        }
        if (!evt.chat_id || evt.chat_id === state.chatId) setGoalModeUI(!!evt.active, { goal: evt.goal });
      } else if (evt.type === "chat:rename") {
        const c = state.chats && state.chats.chats && state.chats.chats[evt.chat_id];
        if (c) {
          c.title = evt.title;
          renderChatList();
        }
      } else if (evt.type === "ui:open-chat") {
        // The notch's "Open in main app" raised this window and asked for a session.
        // A backgrounded WebView can throttle this delivery, so the request is
        // remembered and retried on the focus that the raise causes.
        if (!evt.chat_id) return;
        state._pendingOpenChat = evt.chat_id;
        openRequestedChat();
      } else if (evt.type === "chat_start") {
        // A turn began on another surface (the notch). The bridge persists the chat,
        // title included, before broadcasting, so refreshing the rail shows a new
        // island-born session at once and flips a known one to WORKING without waiting
        // for the 10s poll. Then mirror the turn if that session is on screen.
        loadChats().then(renderChatList).catch(() => {});
        beginForeignTurn(evt.chat_id);
      } else if (evt.type === "ctx_fill") {
        // Per-round assembled-prompt size, pushed before the round streams; the 2s
        // poll only knows the last completed round.
        if (evt.chat_id && evt.chat_id !== state.chatId) return;
        if (typeof evt.prompt_tokens === "number") {
          // Same rule as the poll: while streaming, a stale ctx_fill (e.g. an SSE replay
          // of a previous round) must not pull the gauge backwards. A genuine shrink
          // (compaction) arrives with the next round's stats.
          if (state.streaming && evt.prompt_tokens < Number(state._lastMsgPromptTokens || 0)) {
            return;
          }
          state._lastMsgPromptTokens = evt.prompt_tokens;
          state._ctxSource = evt.source || "estimate";
          if (evt.breakdown && typeof evt.breakdown === "object") state._ctxBreakdown = evt.breakdown;
          if (Number.isFinite(evt.capacity) && evt.capacity > 0) state._ctxCapacity = evt.capacity;
          renderCtxGauge();
        }
      } else if (evt.type === "summary_folding") {
        const chatId = evt.chat_id || state.chatId;
        if (!chatId) return;
        state.compactingChats.add(chatId);
        if (chatId === state.chatId) ensureCompactionRow(chatId);
        appendAgentLog("Context compaction started — condensing older turns into the session summary.");
      } else if (evt.type === "summary_folded") {
        window._lastCompactionAt = Date.now();
        const chatId = evt.chat_id || state.chatId;
        // A previous live divider for this chat is stale once the boundary
        // moved forward (multi-fold turns); drop it before the new one lands.
        if (state.chats && state.chats.chats && state.chats.chats[chatId]) {
          const raw = state.chats.chats[chatId];
          raw.fold_mark = raw.fold_mark || {};
          raw.fold_mark.folded = Number(evt.folded) || raw.fold_mark.folded || 0;
          raw.fold_mark.folded_tokens = evt.folded_tokens;
          raw.fold_mark.summary_chars = evt.summary_chars;
          if (typeof evt.through === "number") {
            raw.fold_mark.through = evt.through;
            raw.summary_through = evt.through;
          }
          if (chatId === state.chatId) {
            const boundary = foldBoundaryVisibleIdx(raw);
            state.foldBoundaryIdx = boundary.idx;
            state.foldMark = boundary.stats;
          }
        }
        const inner = $("#chat-inner");
        if (inner) {
          const escId = String(chatId).replace(/["\\]/g, "\\$&");
          const stale = inner.querySelector(`.compaction-inline-row.is-divider[data-chat-id="${escId}"]`);
          if (stale) try { stale.remove(); } catch {}
        }
        finishCompactionRow(chatId, "done", evt);
      } else if (evt.type === "skill:loaded") {
        const chatId = evt.chat_id || state.chatId;
        if (state.chats && state.chats.chats && state.chats.chats[chatId]) {
          const raw = state.chats.chats[chatId];
          raw.active_skill = evt.skill;
          raw.active_skill_budget = evt.budget || 0;
        }
        if (chatId === state.chatId) {
          state.activeSkill = { name: evt.skill, budget: evt.budget || 0, body_chars: evt.body_chars || 0 };
          renderSkillPill();
        }
        appendAgentLog(`Skill loaded: ${evt.skill}${evt.replaced ? ` (replaced ${evt.replaced})` : ""}.`);
      } else if (evt.type === "skill:cleared") {
        const chatId = evt.chat_id || state.chatId;
        if (state.chats && state.chats.chats && state.chats.chats[chatId]) {
          delete state.chats.chats[chatId].active_skill;
          delete state.chats.chats[chatId].active_skill_budget;
        }
        if (chatId === state.chatId) {
          state.activeSkill = null;
          renderSkillPill();
        }
        appendAgentLog("Active skill cleared.");
      } else if (evt.type === "summary_fold_failed") {
        const chatId = evt.chat_id || state.chatId;
        finishCompactionRow(chatId, "failed");
        if (chatId && chatId !== state.chatId) return;
        toast("Compaction could not produce a valid summary, so this response was stopped before older context could be discarded. Saved messages and completed actions are intact. Use Compact to try again now, wait three minutes and retry, or continue in a new session.", "warn", 11000, "summary-fold-fail");
      } else if (evt.type === "summary_fold_finished") {
        // No state change (nothing foldable / no_change): the working row
        // must not settle into a divider — just clear it.
        finishCompactionRow(evt.chat_id || state.chatId, "clear");
      } else if (evt.type === "desktop:panic") {
        if (evt.on) toast("desktop automation PANICKED — all actions blocked", "warn", 6000, "desktop-panic");
        else toast("desktop automation resumed", "ok", 2000, "desktop-panic");
        refreshDesktopStatus();
      } else if (evt.type === "memories:update") {
        if ($("#settings-drawer")?.classList.contains("open")) loadMemories();
      } else if (evt.type === "reasoning_capability") {
        state.reasoningCapability = evt.capability || { supported: false, mode: "none" };
        renderReasoningEffort();
      } else if (evt.type === "models:update") {
        // A load swaps per-model settings (tune flags, sampling profile), so re-read
        // them; a later Settings save must not post the previous model's sampling.
        Promise.all([
          loadModels(),
          api("/api/settings").then(s => { if (s) state.settings = s; }).catch(() => {}),
        ]).then(() => {
          if ($("#settings-drawer")?.classList.contains("open")) populateSettingsForm();
          renderModelPill();
        });
      } else if (evt.type === "hello") {
        // New connection. A snapshot_id that went backwards means the bridge
        // restarted; say so distinctly from a normal reconnect.
        const snap = Number(evt.snapshot_id || 0);
        if (_lastSnapshotId > 0 && snap < _lastSnapshotId) {
          toast("Bridge restarted — reconnected.", "info", 3500, "sse-hello");
        } else if (evt.replayed_from && _lastSnapshotId > 0) {
          // Reconnect WITHOUT a bridge restart — just say "back online".
          toast("Reconnected.", "ok", 1800, "sse-hello");
        }
        _lastSnapshotId = snap;
      } else if (evt.type === "events:gap") {
        // The disconnect outlasted the bridge's 256-event ring buffer, so some events
        // are gone; tell the user to reload if things look stale.
        const lost = (evt.lost_to || 0) - (evt.lost_from || 0) + 1;
        toast(
          `Connection dropped for too long — ${lost} event${lost === 1 ? "" : "s"} missed. ` +
          `Reload the page if anything looks half-finished.`,
          "warn", 8000, "sse-gap"
        );
      } else if (evt.type === "llama:watchdog_restart") {
        // llama-server crashed; bridge is auto-restarting. Backoff is in
        // evt.delay seconds; attempt N of 3.
        const att = evt.attempt || 1;
        const max = 3;
        const sec = Math.round(evt.delay || 2);
        toast(
          `llama-server crashed — auto-restart attempt ${att}/${max} in ${sec}s…`,
          "warn", 4500, "llama-watchdog"
        );
      } else if (evt.type === "llama:watchdog_restored") {
        toast(
          `llama-server back up${evt.pid ? ` (pid ${evt.pid})` : ""} — keep going.`,
          "ok", 3000, "llama-watchdog"
        );
        // Refresh model pill / status since the loaded model is back.
        try { loadModels().then(renderModelPill); } catch {}
      } else if (evt.type === "llama:watchdog_stuck") {
        // Circuit breaker tripped (3 crashes in 60s): the bridge stopped
        // auto-restarting. Long-lived toast with a clear next step.
        toast(
          (evt.message || "llama-server keeps crashing.") +
          " Auto-restart suspended. Open Settings → Models and pick a different model or lower num_ctx.",
          "err", 60000, "llama-watchdog"
        );
        try { loadModels().then(renderModelPill); } catch {}
      } else if (evt.type === "llama:auto_tuned") {
        // Sent at the start of /api/models/load (the tuner runs before the spawn).
        // If this window started that reload, its "reloading model" toast covers it;
        // an "auto-tuned" toast on top would read as done while the server spawns.
        if (state._reloading) return;
        // Headline with the key numbers; the tuner notes sit behind an expandable
        // "why these settings?" block.
        const nm = (evt.model || "").split(/[\\/]/).pop() || "model";
        const parts = [];
        if (evt.ctx) parts.push(`ctx ${Number(evt.ctx).toLocaleString()}`);
        if (evt.n_cpu_moe) parts.push(`n-cpu-moe ${evt.n_cpu_moe}`);
        if (evt.kv_cache_type) {
          const kvv = evt.kv_cache_type_v && evt.kv_cache_type_v !== evt.kv_cache_type
            ? `/${String(evt.kv_cache_type_v).toUpperCase()}` : "";
          parts.push(`${String(evt.kv_cache_type).toUpperCase()}${kvv} KV`);
        }
        let html = `auto-tuned <b>${esc(nm)}</b>` +
          (parts.length ? ` — ${esc(parts.join(" · "))}` : "");
        if (evt.quant_downshift) html += ` · ⚠ ${esc(evt.quant_downshift)}`;
        const notes = String(evt.notes || "").trim();
        if (notes) {
          const lines = notes.split(/(?<=\.)\s+/).filter(Boolean).map(esc).join("<br>");
          html += `<details class="toast-details"><summary>why these settings?</summary>` +
                  `<div class="toast-details-body">${lines}</div></details>`;
        }
        toast(html, "ok", 30000, "auto-tuned", true);
      } else if (evt.type === "llama:auto_degraded") {
        // Watchdog self-heal: the tuner progressively degraded settings so a
        // crashing model can still boot.
        toast(
          `${esc(evt.message || "model unstable")} — auto-tune adjusted settings so it can boot.`,
          "warn", 15000, "llama-watchdog"
        );
      } else if (evt.type === "sandbox:stream") {
        const line = evt.line || "";
        const isError = /\b(err(or)?|failed|failure|cannot|no such|not found|traceback|abort(ed)?|fatal|out of memory|oom|segmentation|assert(ion)?|invalid|exception|denied)\b/i.test(line);
        appendSandboxText(line + "\n", isError, false);
      }
    };

    const connect = () => {
      _lastSseEventAt = Date.now();
      try { _sseSource?.close(); } catch {}
      const es = new EventSource("/api/events");
      _sseSource = es;
      es.onmessage = handleMessage;
      es.onerror = () => {
        es.close();
        if (_sseSource === es) _sseSource = null;
        setTimeout(connect, 3000);
      };
    };
    connect();

    if (!_sseWatchdog) {
      _sseWatchdog = setInterval(() => {
        if (document.visibilityState !== "visible") return;
        // A backgrounded WebView's stream can be severed silently: the bridge keeps
        // the socket open, so EventSource never errors or reconnects. Heartbeats reset
        // this timer; rebuild the stream when it has been quiet far longer than one
        // heartbeat interval.
        if (Date.now() - _lastSseEventAt > 45000) connect();
      }, 10000);
    }
    window.addEventListener("online", () => {
      if (Date.now() - _lastSseEventAt > 45000) connect();
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible" && Date.now() - _lastSseEventAt > 45000) connect();
    });
  }
  return { noteOwnTurnEnded, subscribeSSE };
};
