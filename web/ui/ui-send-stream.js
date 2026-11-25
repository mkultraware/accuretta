/* SPDX-License-Identifier: AGPL-3.0-only */
/* send / stream: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["send-stream"] = ({
  $, AGENT_AVATAR_HTML, THINK_CLOSE_RE, appendAgentLog, attackRailFinalize, autoResize,
  captureIframePng, collapseWorkBlock, currentClientHint, enqueueMessage, esc, finalizeToolGroup,
  handleEvent, hideMentionMenu, hideToast, leaveWelcome, loadChats, noteOwnTurnEnded,
  notifyCompletion, notifyFailure, openSettings, osintCardFinalize, renderBubble, renderChatList,
  renderCtxGauge, renderImageTray, renderMarkdown, renderPlanPanel, renderStatus, renderTaskHandoff,
  scheduleMessageQueueDrain, scrollToBottom, secretRailFinalize, sendEditTurn, setAttachProgress,
  setComposerMode, state, streamPaceFinish, toast, uploadClientFiles, wbDecorateLiveRow,
  wbSyncAction, withMentionRefs,
}) => {
  function stoppedTurnMarkup() {
    return `<div class="turn-stopped" role="status">` +
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><rect x="8.5" y="8.5" width="7" height="7" rx="1.25"/></svg>` +
      `<strong>Stopped</strong><small>Turn ended before completion</small>` +
    `</div>`;
  }

  function renderStoppedTurn(row) {
    if (!row) return;
    streamPaceFinish(row);   // show what arrived, then no more streaming paints
    row._notificationCancelled = true;
    row.classList.add("was-stopped");
    const col = row.querySelector(".bubble-col");
    const bubble = row.querySelector("#stream-bubble") || row.querySelector(".bubble.agent");
    if (!col || !bubble) return;
    const hasVisibleReply = !bubble.classList.contains("hidden") && !!bubble.textContent.trim();
    if (!hasVisibleReply) {
      bubble.innerHTML = "";
      bubble.classList.add("hidden");
    }
    col.querySelector(".turn-stopped")?.remove();
    const marker = document.createElement("div");
    marker.innerHTML = stoppedTurnMarkup();
    const meta = col.querySelector(".bubble-meta");
    col.insertBefore(marker.firstElementChild, meta || null);

    const think = row.querySelector(".think-container");
    const title = think?.querySelector(".think-title");
    const icon = think?.querySelector(".think-check-icon");
    const seconds = row._workStart ? Math.max(1, Math.round((Date.now() - row._workStart) / 1000)) : 0;
    if (title) title.textContent = seconds ? `Stopped after ${seconds}s` : "Stopped";
    if (icon) icon.className = "ph ph-stop-circle think-check-icon done";
  }

  document.addEventListener('accuretta:resume-research', event => {
    const notebook = event.detail;
    if (!notebook || notebook.chat_id !== state.chatId) return;
    setComposerMode('research', { persist: true });
    send({ mode: 'research', researchId: notebook.id, researchBrief: notebook.brief,
           prompt: 'Continue the saved research and prepare the presentation from the collected evidence.' });
  });

  // The Access chip wears the icon of its level, taken from that level's row
  // in the Access menu; goal mode shows a flag.
  function setPermsChipIcon(level) {
    const icon = document.querySelector("#btn-perms > i.ph");
    if (!icon) return;
    const row = level === "goal" ? null
      : document.querySelector(`#perms-menu .perms-option[data-perm-mode="${level}"] > i.ph`);
    const glyph = row ? [...row.classList].find(c => c.startsWith("ph-")) : "ph-flag-checkered";
    if (glyph) icon.className = `ph ${glyph}`;
  }

  // /goal hands-free latch: reflect it on the Access chip ("Goal", pulsing) and
  // play a one-shot flourish on the composer. Turning off restores the
  // permission-mode label. The flourish only fires on the off->on transition,
  // unless `flourish` is forced.
  function setGoalModeUI(active, { flourish = false, goal } = {}) {
    const on = !!active;
    const was = !!state.goalMode;
    if (!on) state.goalText = "";
    else if (goal !== undefined) state.goalText = goal || "";
    document.body.classList.toggle("goal-mode-active", on);
    const permsBtn = document.querySelector("#btn-perms");
    if (permsBtn) {
      const lbl = permsBtn.querySelector(".perms-chip-label");
      if (on) {
        if (!permsBtn.dataset.goal && lbl) permsBtn._goalPrevLabel = lbl.textContent;
        permsBtn.dataset.goal = "1";
        if (lbl) lbl.textContent = "Goal";
        permsBtn.classList.add("on");
        permsBtn.title = "Goal mode — this chat runs hands-free. Click to turn off. Deletions and destructive actions still ask.";
        setPermsChipIcon("goal");
      } else if (permsBtn.dataset.goal) {
        delete permsBtn.dataset.goal;
        if (lbl && permsBtn._goalPrevLabel != null) lbl.textContent = permsBtn._goalPrevLabel;
        const mode = (state.settings && state.settings.approval_mode) ||
          ((state.settings && state.settings.auto_approve_write) ? "medium" : "hard");
        permsBtn.classList.toggle("on", mode !== "hard");
        permsBtn.title = "Permissions · " + (mode.charAt(0).toUpperCase() + mode.slice(1));
        setPermsChipIcon(mode);
      }
    }
    if (on && (!was || flourish)) {
      const comp = document.querySelector(".composer");
      if (comp) {
        comp.classList.remove("goal-activating");
        void comp.offsetWidth;          // restart the keyframes
        comp.classList.add("goal-activating");
        setTimeout(() => comp.classList.remove("goal-activating"), 1200);
      }
    }
    state.goalMode = on;
    renderGoalRow();
  }

  // While /goal runs, its task sits as a row inside the composer box, above
  // the input, with a button that ends it.
  function renderGoalRow() {
    const row = $("#composer-goal");
    if (!row) return;
    const on = !!state.goalMode;
    row.classList.toggle("hidden", !on);
    if (!on) { row.innerHTML = ""; return; }
    const text = state.goalText || "Hands-free until you turn it off";
    row.innerHTML = `<i class="ph ph-target" aria-hidden="true"></i><span class="composer-goal-label">Goal</span>`
      + `<span class="composer-goal-text" title="${esc(text)}">${esc(text)}</span>`
      + `<button type="button" class="composer-goal-off" title="End the goal" aria-label="End the goal"><i class="ph ph-x" aria-hidden="true"></i></button>`;
    row.querySelector(".composer-goal-off").addEventListener("click", turnGoalOff);
  }
  function turnGoalOff() {
    if (state.chatId) {
      fetch("/api/chat/goal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: state.chatId, active: false }),
      }).catch(() => {});
    }
    setGoalModeUI(false);
    toast("Goal mode off — back to your standing permissions.", "info", 3200, "goal-toggle");
  }

  async function send(opts = {}) {
    // The tray is already on its way up; a second Enter would upload it twice.
    if (state.uploadingFiles && opts.prompt === undefined && !opts.fromQueue) return false;
    // Edit & resend: only composer-originated sends become edits; queued messages
    // and suggestion-card prompts must not replace an edited turn.
    if (state.editingTurn && !opts.fromEdit && opts.prompt === undefined && !opts.fromQueue) {
      return sendEditTurn(opts);
    }
    if (opts.mission && !opts.mode) opts = { ...opts, mode: "agent" };
    if ((opts.mode || state.mode) === "research" && !opts.researchBrief) {
      const researchBrief = await window.AccurettaResearch.requestBrief(opts.prompt ?? $("#composer-input").value);
      if (!researchBrief) return false;
      opts = { ...opts, researchBrief };
    }
    if (state.streaming || (state.queueDispatching && !opts.fromQueue)) {
      if (opts.fromQueue) return false;
      return enqueueMessage(opts);
    }
    const ta = $("#composer-input");
    const fromComposer = opts.prompt === undefined;
    let text = fromComposer ? ta.value.trim() : String(opts.prompt || "").trim();
    if (opts.researchBrief && !opts.researchId) text = opts.researchBrief.topic;
    const images = Array.isArray(opts.images) ? opts.images.slice() : (fromComposer ? state.pendingImages.slice() : []);
    const files = Array.isArray(opts.files) ? opts.files.slice() : (fromComposer ? state.pendingFiles.slice() : []);

    // "review this UI" auto-captures the preview iframe and attaches it so the
    // vision model sees it, unless the user already attached something.
    if (text && /\breview this ui\b/i.test(text) && !images.length && state.currentHtml) {
      const canvas = await captureIframePng();
      if (canvas) {
        const dataUrl = canvas.toDataURL("image/png");
        images.push({ dataUrl, name: `ui-${Date.now()}.png` });
      }
    }

    if (!text && !images.length && !files.length) return false;
    // /goal: a bare "/goal" (or "/goal off|stop|end|done|cancel") toggles the
    // hands-free latch without starting a turn. "/goal <task>" runs the task and
    // the bridge latches goal mode on for the chat (non-destructive actions
    // auto-approve) until "/goal off".
    const goalMatch = /^\s*\/goal\b[ \t]*([\s\S]*)$/i.exec(text);
    if (goalMatch) {
      const rest = (goalMatch[1] || "").trim();
      const isOff = /^(off|stop|end|done|cancel)$/i.test(rest);
      const isBareOn = rest === "" || /^(on|start)$/i.test(rest);
      if ((isOff || isBareOn) && !images.length && !files.length) {
        if (!state.chatId) {
          if (isBareOn) toast("Start a hands-free run with “/goal <task>” — e.g. “/goal build and test the parser”.", "info", 4800, "goal-hint");
          if (fromComposer) { ta.value = ""; autoResize(ta); }
          return false;
        }
        const active = !isOff;
        try {
          await fetch("/api/chat/goal", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ chat_id: state.chatId, active }),
          });
        } catch (e) {}
        setGoalModeUI(active, { flourish: active });
        toast(active
          ? "Goal mode on — this chat runs hands-free. Deletions and destructive actions still ask."
          : "Goal mode off — back to your standing permissions.",
          active ? "warn" : "info", 4200, "goal-toggle");
        if (fromComposer) {
          ta.value = "";
          autoResize(ta);
          if (state.chatId) localStorage.removeItem("accuretta:draft:" + state.chatId);
        }
        return false;
      }
      // "/goal <task>" continues as a normal turn below; the bridge latches it.
    }
    // A new request starts fresh: drop any prior turn's plan panel. The model
    // re-emits update_plan if this task is multi-step.
    renderPlanPanel([]);
    if (!state.settings.model) {
      toast("Pick a model in Settings first.", "warn", 3200, "no-model");
      openSettings();
      return false;
    }
    let uploadedFiles = [];
    if (files.length) {
      // From the tray the tiles show the progress; other sends keep the toast.
      const onTray = fromComposer && files.length === state.pendingFiles.length
        && files.every((file, i) => file === state.pendingFiles[i]);
      // The tiles stay in place (re-rendering would replay their fade-in);
      // a failed upload redraws them without the half-drawn rings.
      state.uploadingFiles = true;
      if (onTray) $("#image-tray")?.classList.add("uploading");
      else toast(`uploading ${files.length} file${files.length === 1 ? "" : "s"}…`, "info", 60000, "client-upload");
      try {
        uploadedFiles = await uploadClientFiles(files, onTray ? setAttachProgress : null);
      } catch (e) {
        toast(`File upload failed: ${e.message || e}`, "err", 4500);
        if (onTray) renderImageTray();
        return false;
      } finally {
        state.uploadingFiles = false;
        hideToast("client-upload");
        $("#image-tray")?.classList.remove("uploading");
      }
      const fileBlock = [
        "[Files uploaded from the active browser client into the inference PC workspace]",
        ...uploadedFiles.map(f => `- ${f.name}: ${f.path}`),
      ].join("\n");
      text = text ? `${text}\n\n${fileBlock}` : fileBlock;
    }
    if (fromComposer) {
      ta.value = "";
      autoResize(ta);
      hideMentionMenu();
      if (state.chatId) localStorage.removeItem("accuretta:draft:" + state.chatId);
      state.pendingImages = [];
      state.pendingFiles = [];
      renderImageTray();
    }

    // Note the attachment counts in the user bubble as plain text (no emoji: this
    // is stored content).
    const notes = [];
    if (images.length) notes.push(`${images.length} image${images.length > 1 ? "s" : ""} attached`);
    if (uploadedFiles.length) notes.push(`${uploadedFiles.length} file${uploadedFiles.length > 1 ? "s" : ""} uploaded`);
    const bubbleText = notes.length ? `${text}\n\n${notes.join(" · ")}` : text;
    const userMsg = { role: "user", content: bubbleText, t: Math.floor(Date.now() / 1000) };
    if (opts.invisible) {
      userMsg.invisible = true;
    }
    state.messages.push(userMsg);

    // Clear welcome screen visually if we are about to append the agent stream bubble
    const welcome = document.querySelector("#chat-inner .welcome-screen");
    if (welcome) {
      // First message of the session: ripple the dot field out from the
      // composer (a quick-start chip already started its own ripple).
      window.AccDots?.field.pulse(document.querySelector(".composer-wrap > .composer"));
      leaveWelcome();
      $("#chat-inner").innerHTML = "";
    }

    if (!opts.invisible) {
      $("#chat-inner").appendChild(renderBubble(userMsg));
    }
    scrollToBottom(true);
    renderCtxGauge();

    // placeholder agent bubble
    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    // Security presentation is opt-in per turn. Tool names alone are not
    // enough: ordinary code work can legitimately use overlapping tools.
    agentRow._attackRailEnabled = opts?.mission?.engagement === "pentest";
    agentRow._rtEngagement = opts?.mission?.engagement || "";
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
    $("#chat-inner").appendChild(agentRow);
    window.AccurettaOrb?.setState(agentRow, "thinking");
    scrollToBottom(true);

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);
    // The live agent row and its unpersisted prompt exist only in this closure and
    // the DOM until the turn completes. Track the DOM node so a chat switch can
    // re-attach it; deltas keep painting into it, so every partial token is
    // restored without replay.
    state.liveTurn = { chatId: state.chatId, row: agentRow, userMsg };

    try {
      await streamChat(opts.preparedPrompt ? text : withMentionRefs(text), agentRow, state.abortCtl.signal, images, opts);
    } catch (e) {
      if (e.beforeStream && images.length && fromComposer) {
        state.pendingImages = [...images, ...state.pendingImages];
        renderImageTray();
      }
      const b = agentRow.querySelector("#stream-bubble") || agentRow.querySelector(".bubble");
      if (b) {
        if (e.name === "AbortError") {
          renderStoppedTurn(agentRow);
        } else {
          agentRow._notificationFailed = true;
          agentRow._notificationError = e.message;
          b.innerHTML = `<span style="color: var(--danger)">error: ${esc(e.message)}</span>`;
        }
      }
    } finally {
      const finishedChatId = state.liveTurn?.chatId || state.chatId;
      streamPaceFinish(agentRow);
      noteOwnTurnEnded(finishedChatId);
      window.AccurettaResearch?.finish(agentRow);
      state.streaming = false;
      state.abortCtl = null;
      renderTaskHandoff(agentRow, finishedChatId);
      state.liveTurn = null;   // turn committed; partial no longer needs restoring
      setStreamingUI(false, agentRow._notificationCancelled ? "stopped" : agentRow._notificationFailed ? "failed" : "completed");
      await loadChats();
      const finishedChat = state.chats.chats[finishedChatId];
      if (finishedChat) {
        delete finishedChat.messages;
        finishedChat._summary = true;
      }
      renderChatList();
      if (agentRow._notificationFailed) notifyFailure(agentRow._notificationError, agentRow._workStart ? Date.now() - agentRow._workStart : 0);
      else if (!agentRow._notificationCancelled) notifyCompletion(agentRow._workStart ? Date.now() - agentRow._workStart : 0);
      scheduleMessageQueueDrain(state.chatId);
    }
    return true;
  }

  function setStreamingUI(on, outcome = "completed") {
    renderChatList();
    $("#revealer-deck")?.classList.toggle("is-streaming", on);
    const sendButton = $("#btn-send");
    sendButton.classList.remove("hidden");
    sendButton.classList.remove("queue-mode");
    sendButton.title = on ? "Send a correction. The agent receives it between actions." : "Send (⌘⏎)";
    sendButton.setAttribute("aria-label", on ? "Update current task" : "Send message");
    const sendIcon = sendButton.querySelector("i");
    if (sendIcon) sendIcon.className = "ph-bold ph-arrow-up";
    $("#btn-stop").classList.toggle("hidden", !on);
    $("#composer-input").disabled = false; // always allow typing next message
    $("#composer-input").placeholder = on ? "Add a correction or more context…" : "ask, build, or instruct…";
    const comp = document.querySelector(".composer");
    if (comp) comp.classList.toggle("status-thinking", on);
    renderStatus(0, on ? "streaming" : "idle");
    appendAgentLog(on ? "Agent streaming started." : `Agent streaming ${outcome}.`);
    wbSyncAction();
  }

  function stopStreaming() {
    // tell the bridge to force-close the llama-server socket first — otherwise
    // generation keeps running server-side until it hits its own limit.
    const cid = state.chatId;
    if (state.liveTurn?.row) state.liveTurn.row._stopRequested = true;
    if (cid) {
      try {
        fetch("/api/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: cid }),
          keepalive: true,
        }).catch(() => {});
      } catch {}
    }
    if (state.abortCtl) {
      try { state.abortCtl.abort(); } catch {}
    }
  }

  async function streamChat(text, agentRow, signal, images, opts) {
    const regenerate = !!(opts && opts.regenerate);
    agentRow._workStart = Date.now();   // spans the whole turn for "Worked for Xs"
    wbDecorateLiveRow(agentRow);
    const bubble = agentRow.querySelector("#stream-bubble");
    const toolStack = agentRow.querySelector("#tool-stack");
    // Cleared each turn; the empty-reply branch sets it true so the retry
    // button pulses for attention until a real reply lands.
    state.attentionRetry = false;
    let buf = "";
    const toolCards = new Map();

    // A quiet heartbeat proves the stream is alive without inventing claims
    // about what the model is doing internally.
    let lastActivity = Date.now();
    const started = lastActivity;
    const markActivity = () => { lastActivity = Date.now(); };
    const heartbeat = setInterval(() => {
      const line = agentRow.querySelector(".think-line");
      if (!line || line.classList.contains("done")) return;
      const span = line.querySelector("span");
      if (!span || !span.classList.contains("shimmer")) return;
      const idle = Math.floor((Date.now() - lastActivity) / 1000);
      const total = Math.floor((Date.now() - started) / 1000);
      if (idle < 3) return;
      const phase = line.dataset.progressLabel
        || (line.dataset.phase === "writing" ? "Still writing response" : "Still thinking");
      span.textContent = `${phase} · ${total}s`;
    }, 250);

     const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: state.chatId,
        message: text,
        mode: opts?.mode || state.mode,
        images: (images || []).map(x => x.dataUrl),
        regenerate,
        invisible: !!(opts && opts.invisible),
        mission: (opts && opts.mission) || undefined,
        research_brief: opts?.researchBrief || undefined,
        research_id: opts?.researchId || undefined,
        reasoning_effort: opts?.reasoningEffort || state.reasoningEffort || "auto",
        client_context: opts?.clientContext || currentClientHint(),
      }),
       signal,
     });
    if (!resp.ok) {
      let detail = "";
      try {
        const payload = await resp.json();
        detail = payload && payload.error ? String(payload.error) : "";
      } catch (e) {}
      const error = new Error(detail || `request failed (${resp.status})`);
      error.beforeStream = true;
      throw error;
    }
    if (!resp.body) throw new Error("no response body");

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let carry = "";
    let ended = false;

    try {
      while (!ended) {
        const { value, done } = await reader.read();
        if (done) break;
        carry += dec.decode(value, { stream: true });
        const chunks = carry.split(/\n\n/);
        carry = chunks.pop();
        for (const chunk of chunks) {
          const line = chunk.split("\n").find(l => l.startsWith("data: "));
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          try {
            handleEvent(evt, { bubble, toolStack, toolCards, row: agentRow, getBuf: () => buf, setBuf: v => buf = v });
          } catch (err) {
            // One bad event must not tear down the read loop; later events carry stats
            // and the final message.
            console.error("handleEvent failed on", evt && evt.type, err);
          }
          markActivity();
          if (evt.type === "chat_end") { ended = true; break; }
        }
      }
    } finally {
      try { await reader.cancel(); } catch {}
      clearInterval(heartbeat);
      if (agentRow) {
        updateThinkLine(agentRow, false);
        const meta = agentRow.querySelector(".bubble-meta");
        if (meta) {
          meta.classList.remove("streaming");
          meta.querySelectorAll(".typing").forEach(d => d.remove());
        }
        // Move tool strip below the bubble + apply faded "done-pill" styling
        // so it looks like a footnote, not part of the answer.
        finalizeToolGroup(agentRow);
        // Fold the whole work unit (thinking + tools) under one "Worked for Xs".
        collapseWorkBlock(agentRow);
        // Settle the OSINT recon card (stop the live pulse, show the summary).
        osintCardFinalize(agentRow);
        secretRailFinalize(agentRow);
        attackRailFinalize(agentRow);
        const revealerDeck = document.querySelector("#revealer-deck");
        if (revealerDeck) delete revealerDeck.dataset.rtEngagement;
      }
      // Safety net: the model ran tools or thought for a while but ended without a
      // visible answer. Show what we have, promoting the thinking tail if it is
      // substantive.
      if (!agentRow?._stopRequested && !signal?.aborted && bubble && bubble.classList.contains("hidden")) {
        const { thinking, content } = splitThinking(buf);
        const cascadeRes = splitCascade(content);
        const hasCascade = cascadeRes.cascade && cascadeRes.cascade.length > 0;
        const hadTools = toolStack && toolStack.children.length > 0;
        
        if (!hasCascade) {
          bubble.classList.remove("hidden");
          bubble.classList.add("quiet");
          // Reply came back empty/incomplete — flag so the retry button pulses.
          state.attentionRetry = true;
          // All empty-state branches render as an info icon + italic text (.bubble.quiet
          // in CSS). Two shapes need different handling:
          //   A) The buffer ends inside a closed think block: the model put its whole
          //      reply in the tags and closed them as its last tokens. The "thinking"
          //      is the answer, so promote all of it.
          //   B) No closing tag: a genuine runaway (budget cut or reasoning loop).
          //      Keep the "spent its budget" wall with the reasoning tail.
          if (!hadTools && thinking && thinking.length > 40) {
            if (THINK_CLOSE_RE.test((buf || "").replace(/\s+$/, "").slice(-200))) {
              bubble.classList.add("quiet");
              state.attentionRetry = false;   // there IS a readable reply — nothing to retry
              bubble.innerHTML =
                `<i class="quiet-icon ph ph-info"></i>` +
                `<div class="quiet-text">` +
                  renderMarkdown("> The model closed its reply inside its **thinking block** — the full text is below (the collapsed block above holds the same).\n\n" + thinking) +
                `</div>`;
            } else {
              const tail = thinking.length > 900 ? "…" + thinking.slice(-900) : thinking;
              bubble.innerHTML =
                `<i class="quiet-icon ph ph-info"></i>` +
                `<div class="quiet-text">` +
                  `<div style="margin-bottom:6px;opacity:0.85;font-size:12px;">model spent its whole budget thinking — here's the tail</div>` +
                  `<pre style="white-space:pre-wrap;font-family:inherit;margin:0;font-style:normal;">${esc(tail)}</pre>` +
                `</div>`;
            }
        } else {
          let msg = "No response — the model may have crashed or hit a context limit. Check the backend console for errors.";
          if (hadTools) {
            msg = "model ended turn without a reply — ask it what it found, or try again";
          } else if (state.settings.num_predict > 0 && state.settings.num_predict < 50) {
            msg = `No response — Max reply tokens is set very low (${state.settings.num_predict}). Try raising it in Settings.`;
          } else if (state.settings.num_predict === 0) {
            msg = "No response — Max reply tokens is set to 0. Try raising it in Settings.";
          }
          bubble.innerHTML =
            `<i class="quiet-icon ph ph-info"></i>` +
            `<span class="quiet-text">${esc(msg)}</span>`;
          }
        }
      }
    }
  }


  // strip reasoning wrappers from several model families so the chat bubble
  // only shows the final answer. Accumulate thinking text into the think line.
  function splitThinking(buf) {
    // Tags seen: <think>, <thinking>, <reasoning>, <|thinking|>...<|/thinking|> and
    // bracketed [thought], [thinking], [reasoning], [scratchpad]. Many local models
    // (Qwen/DeepSeek/Nemotron) emit a bare </think> with no opener, sometimes
    // several times between tool rounds.
    //
    // Boundary rule: a closing tag followed by an answer always wins. Chats saved
    // by older bridge builds separate reasoning with a bare marker line instead,
    // the answer glued straight after, sometimes with a closing tag after it:
    //     <think>planning...\n responseHere is the answer...
    //     <think>planning...\n responseHere is the answer...</think>
    // The marker counts only after a reasoning opener and only when no answer
    // follows the last closing tag, so an answer line like `ResponseEntity<T>`
    // never splits the reply.
    //
    // Marker lookahead: the char after "response" must start the answer (a
    // letter, digit or tool-call block). A space or punctuation means ordinary
    // prose ("the response was..."), and a trailing "s" at a word boundary is the
    // plural; both are excluded.
    const firstOpen = buf.search(/<(?:think|thinking|reasoning)>|<\|thinking\|>|\[(?:thought|thinking|reasoning|scratchpad)\]/i);
    const markerRe = /(?:\n\s*|\s+)response(?=[A-Za-z0-9<])(?![sS](?:\b|$))/gi;
    let lastMarker = -1;
    let lastMarkerEnd = -1;
    let mm;
    while (firstOpen >= 0 && (mm = markerRe.exec(buf)) !== null) {
      if (mm.index < firstOpen) continue;
      lastMarker = mm.index;
      lastMarkerEnd = mm.index + mm[0].length;
    }

    // A template may stream the boundary as its own line and send the answer in
    // the next delta; recognize that pending state right away, or the scratchpad
    // flashes as answer markdown.
    if (lastMarker < 0 && firstOpen >= 0) {
      const pendingMarker = /(?:^|\n)[ \t]*response[ \t]*$/i.exec(buf);
      if (pendingMarker && pendingMarker.index > firstOpen) {
        lastMarker = pendingMarker.index;
        lastMarkerEnd = buf.length;
      }
    }

    const closeRe = /<\/(?:think|thinking|reasoning)>|<\|\/thinking\|>|\[\/(?:thought|thinking|reasoning|scratchpad)\]/gi;
    let lastClose = -1;
    let m;
    while ((m = closeRe.exec(buf)) !== null) lastClose = m.index + m[0].length;
    const answerAfterClose = lastClose >= 0 && buf.slice(lastClose).trim() !== "";

    let thinking = "";
    let content = "";
    if (lastMarker >= 0 && !answerAfterClose) {
      thinking = buf.slice(0, lastMarker);
      content = buf.slice(lastMarkerEnd);
    } else if (lastClose >= 0) {
      thinking = buf.slice(0, lastClose);
      content = buf.slice(lastClose);
    } else {
      // no closing tag yet — if an opening tag is present, everything from it is in-flight thinking
      const openIdx = buf.search(/<(?:think|thinking|reasoning)>|<\|thinking\|>|\[(?:thought|thinking|reasoning|scratchpad)\]/i);
      if (openIdx >= 0) {
        content = buf.slice(0, openIdx);
        thinking = buf.slice(openIdx);
        
        // Implicit close: if the model forgot to close the think tag but started a
        // native tool call, treat the tool call opener as the end of the thinking block.
        let implicitCloseIdx = thinking.search(/<\/?tool_call>|<\|tool_call>|<call:[a-zA-Z0-9_\-]+>|\[TOOL_CALLS\]|```tool_call/i);
        if (implicitCloseIdx > 0) {
          content += thinking.slice(implicitCloseIdx);
          thinking = thinking.slice(0, implicitCloseIdx);
        } else {
          // Some Qwen3.x builds drop the closing tag and glue the answer after the
          // " response" marker ("<think>...\n responsehey there"): treat the marker as
          // the end of thinking and drop it.
          const respM = thinking.match(/\n\s*response/);
          if (respM && respM.index > 0) {
            content += thinking.slice(respM.index + respM[0].length);
            thinking = thinking.slice(0, respM.index);
          }
        }
      } else {
        content = buf;
      }
    }
    const stripTags = /<\/?(?:think|thinking|reasoning)>|<\|\/?thinking\|>|\[\/?(?:thought|thinking|reasoning|scratchpad)\]/gi;
    thinking = thinking.replace(stripTags, "").trim();
    content = content.replace(stripTags, "");
    // strip model-specific content delimiters that leak into output:
    //   GLM 4.x: ◁begin_of_box▷ … ◁end_of_box▷  and the <|…|> variants
    //   Command-R: <|START_OF_TURN_TOKEN|> etc.
    //   generic: <|im_start|>assistant / <|im_end|>, <|eot_id|>, [INST] wrappers
    const junk = [
      /◁\|?begin_of_box\|?▷/gi,
      /◁\|?end_of_box\|?▷/gi,
      /<\|?begin_of_box\|?>/gi,
      /<\|?end_of_box\|?>/gi,
      /<\|im_start\|>(?:assistant|user|system)?/gi,
      /<\|im_end\|>/gi,
      /<\|eot_id\|>/gi,
      /<\|start_header_id\|>[\s\S]*?<\|end_header_id\|>/gi,
      /<\|(?:START|END)_OF_TURN_TOKEN\|>/gi,
      /<\|begin_of_text\|>/gi,
      /<\|end_of_text\|>/gi,
      /\[\/?INST\]/gi,
      /<s>|<\/s>/gi,
      // Orphan tool_call tags: the bridge only strips matched
      // <tool_call>...</tool_call> pairs, and Gemma and some tunes leave a lone
      // "</tool_call>" or "</" after their last real call.
      /<\/?tool_call>/gi,
      /<\/?function(?:=\w+)?>/gi,
      // Quote-token delimiters: Gemma 4's tool-call dialect uses <|"|> as a string
      // delimiter. The bridge parses valid <|tool_call> blocks before this filter,
      // so anything here is an orphan or partial emit.
      /<\|"\|>/g,
      /<\|'\|>/g,
      // <|tool_call> tags are not stripped here: the naked body (NAME{...}) would
      // leak because renderMarkdown could no longer find the block bounds.
      // renderMarkdown strips the whole block instead.
    ];
    for (const re of junk) { thinking = thinking.replace(re, ""); content = content.replace(re, ""); }
    // Trailing partial tag when the stream cuts mid-tag (`<`, `</`, `</to`,
    // `</tool_call`, `</think` ...) plus whitespace before it. Scoped to known tag
    // names so a legitimate trailing `<` in prose survives.
    content = content.replace(/\s*<\/?(?:tool_call|tool|call|think|thinking|reasoning|function|im_start|im_end)\w*\s*$/i, "");
    content = content.replace(/\s*\[\/?(?:thought|thinking|reasoning|scratchpad)\w*\s*$/i, "");
    content = content.replace(/\s*<\/\s*$/, "");  // bare "</" with nothing after
    content = content.replace(/\s*\[\/?\s*$/, ""); // bare "[" or "[/" with nothing after
    return { thinking: thinking.trim(), content };
  }

  // Some reasoning templates stream an untagged scratchpad and reveal the
  // boundary later with a standalone `response` marker, so keep obvious planning
  // prose in the thought surface while live. Presentation only: the final event
  // uses splitThinking alone, so a direct answer that happens to match is never
  // lost.
  function isLikelyLiveWorkingNotes(text) {
    const sample = String(text || "").trimStart().slice(0, 1800);
    if (sample.length < 24) return false;
    const startsLikePlanning = /^(?:hmm\b|okay\b|ok\b|we need\b|i need\b|let me\b|let's\b|now (?:i|we|let)\b|need to\b|the user\b|user wants\b|i should\b|i'll\b)/i.test(sample);
    const planningHits = sample.match(
      /\b(?:let me|we need|i need|need to|let's|i'll|i should|careful|plan(?:ning)?|first i|then i)\b/gi
    ) || [];
    return startsLikePlanning || planningHits.length >= 3;
  }

  function splitCascade(buf) {
    let cascade = null;
    let content = buf;
    
    // Match <cascade>, \<cascade\>, or &lt;cascade&gt;
    const cascadeRe = /(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*?)(?:<|&lt;|\\<)\/cascade(?:>|&gt;|\\>)/i;
    const match = cascadeRe.exec(buf);
    
    if (match) {
      try {
        // Unescape entities and replace smart quotes
        let jsonStr = match[1].trim()
            .replace(/['‘’]/g, '"')
            .replace(/&quot;/g, '"');
        cascade = JSON.parse(jsonStr);
        if (!Array.isArray(cascade)) cascade = null;
      } catch (e) {
        cascade = null;
      }
      content = buf.replace(cascadeRe, "").trim();
    } else {
      // Hide partial tags while streaming (handles <, \<, and &lt;)
      content = content.replace(/(?:<|&lt;|\\<)cascade[\s\S]*$/i, "").trim();
      // Catch even smaller partials like `<cas` at the absolute end of the stream
      content = content.replace(/(?:<|&lt;|\\<)c(?:a(?:s(?:c(?:a(?:d(?:e)?)?)?)?)?)?$/i, "").trim();
    }
    
    return { cascade, content };
  }
  // Status label for the pure-reasoning phase. Echoing the user message back
  // reads as parroting, so anchor on a named file ("Thinking about
  // BlogPost.tsx") or stay generic ("Thinking..."); the action phases carry the
  // detail.
  function thinkingLabel() {
    const msgs = state.messages || [];
    let req = "";
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "user") {
        let t = msgs[i].content;
        if (Array.isArray(t)) t = t.map(p => (p && p.type === "text") ? p.text : "").join(" ");
        req = String(t || "");
        break;
      }
    }
    const file = req.match(/[\w./\\-]+\.(?:tsx?|jsx?|mjs|cjs|py|css|scss|html?|json|md|ya?ml|rs|go|java|rb|php|sql|sh|ps1|toml|cpp|cs)\b/i);
    if (file) return `Thinking about ${file[0].split(/[\\/]/).pop()}`;
    return "Thinking…";
  }

  function toolProgressLabel(evt) {
    const name = String(evt.name || "tool");
    const file = String(evt.path || "").split(/[\\/]/).pop();
    const verb = name.includes("edit") ? "Editing" : /write|save/.test(name) ? "Writing" : "Preparing";
    const lines = Number(evt.lines) || 0;
    return `${verb} ${file || name.replaceAll("_", " ")} · ${lines} line${lines === 1 ? "" : "s"}`;
  }

  // Which orb shape a tool call shows. Order matters: web_search is a search,
  // remote_read_file is a connection, git_diff is reading.
  function orbPhaseForTool(name) {
    name = String(name || "");
    if (name === "update_plan") return "planning";
    if (/search|find|grep|project_map/.test(name)) return "searching";
    if (/web|http|fetch|remote|mcp_|connect|recon_|tcp_send|cors_probe/.test(name)) return "connecting";
    if (/^(read_|list_directory|file_inspect|strings_dump|binary_inspect|disasm_at|ghidra_analyze|decompile_apk|load_skill|parse_|analyze_|git_(status|log|diff|show))/.test(name)) return "reading";
    if (/^(run_|sandbox_|session_|git_)|shell|check_syntax/.test(name)) return "running";
    return "working";
  }

  function updateThinkLine(row, running, label) {
    const writing = /^Writing\b/.test(label || "");
    window.AccurettaOrb?.setState(row, running ? (writing ? "composing" : "thinking") : "idle");
    const container = row.querySelector(".think-container");
    if (!container) return;
    const span = container.querySelector(".think-title");
    const icon = container.querySelector(".think-check-icon");
    // A tool_progress label stays until any other status replaces it.
    if (!running || label !== container.dataset.progressLabel) delete container.dataset.progressLabel;

    if (running) {
      if (!container._thinkStart) container._thinkStart = Date.now();
      if (!container._phaseStart) container._phaseStart = Date.now();
      container.dataset.phase = writing ? "writing" : "thinking";
      // Re-entering activity (a new thinking phase, or a tool after some answer
      // text): clear the finished look so the label shimmers again.
      container.classList.remove("done");
      if (span) span.classList.add("shimmer");
      if (label && span) span.textContent = label;
      if (icon) icon.className = "ph ph-brain think-check-icon";
      return;
    }
    // Close the current phase and retain accumulated time if reasoning resumes
    // after a tool result.
    container.classList.add("done");
    if (span) span.classList.remove("shimmer");
    if (container._phaseStart) {
      container._thinkElapsed = (container._thinkElapsed || 0) + (Date.now() - container._phaseStart);
      container._phaseStart = 0;
    }
    delete container.dataset.phase;
    const elapsed = container._thinkElapsed ? Math.max(1, Math.round(container._thinkElapsed / 1000)) : 0;
    const fmt = elapsed >= 60
      ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
      : `${elapsed}s`;
    const finalLabel = elapsed > 0
      ? `Thought for ${fmt}`
      : (label || "Thought for a moment");
    if (span) span.textContent = finalLabel;
    if (icon) icon.className = "ph ph-check-circle think-check-icon done";
  }
  return {
    stoppedTurnMarkup, renderStoppedTurn, setPermsChipIcon, setGoalModeUI, turnGoalOff, send,
    setStreamingUI, stopStreaming, streamChat, splitThinking, isLikelyLiveWorkingNotes,
    splitCascade, thinkingLabel, toolProgressLabel, orbPhaseForTool, updateThinkLine,
  };
};
