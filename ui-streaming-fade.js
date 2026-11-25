/* SPDX-License-Identifier: AGPL-3.0-only */
/* streaming fade: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["streaming-fade"] = ({
  $, STEP_BREAK, STEP_BREAK_RE, STREAM_PACE_PASSTHROUGH, _applySavings, appendAgentLog,
  appendExecutionText, appendSandboxText, appendTerminalText, attackRailBreach, attackRailMission,
  attackRailToolResult, attackRailToolStart, baseSplitMemo, cancelStreamPaint, checkModelAdvisor,
  enhanceCodeBlocks, ensureSkillSaveRow, esc, executionActivity, executionTarget,
  fenceBodyLooksLikeCode, findStableCut, findToolActivity, finishSkillSaveRow, finishToolActivity,
  fmtToolDuration, folderLeafName, injectMobilePreviewCard, isAttackRailEnabled,
  isLikelyLiveWorkingNotes, isMobile, messageQueue, orbPhaseForTool, osintCardToolResult,
  osintCardToolStart, pacedBuf, parseMultiFileBlocks, passiveIntelMission, receiveApproval,
  renderBubble, renderChatList, renderClientContext, renderCostWidget, renderCtxGauge,
  renderMarkdown, renderMessageQueue, renderMessages, renderNetworkChart, renderPlanPanel,
  renderPreview, renderProjectChip, renderRegenerateChip, renderStatus, renderStatusFast,
  renderStoppedTurn, renderTokTotal, renderTopbarContext, renderTurnChanges, renderVersions,
  renderWorkingNotesBlock, renderWorkspace, rtPhaseMarker, scanOpenFenceState, scheduleFinishBatch,
  scheduleStreamPaint, scrollToBottom, secretRailMission, secretRailToolResult, secretRailToolStart,
  setActiveVersion, setGoalModeUI, setToolActivityLabel, shouldPromoteAnswer, shownImagesHtml,
  splitCascade, splitThinking, splitThinkingMemo, startToolActivity, state, streamPaceArrival,
  streamPaceFlush, streamPaceReset, surfaceShell, thinkingLabel, toast, toolLabel,
  toolProgressLabel, toolResultFailed, toolResultLabel, updateThinkLine, updateToolGroupHead,
  wbFinishLiveRow, wsFileUrl,
}) => {
  // New answer text fades in tinted with the theme accent, then settles into
  // the normal text colour, so the last few words always trail a soft accent.
  // The bubble is re-rendered from markdown on every paint, so the fade can't
  // live on the DOM: each paint records when the visible text grew, and the
  // stretches younger than STREAM_FADE_MS are re-wrapped after the paint with a
  // negative animation-delay, which resumes each fade where it left off.
  const STREAM_FADE_MS = 900;
  const STREAM_FADE_SKIP = "pre, svg, .katex, math, button, .stream-code-hint";
  function applyStreamFade(bubble, opts = {}) {
    if (!bubble || !bubble.isConnected) return;
    const now = performance.now();
    const total = bubble.textContent.length;
    const f = bubble._fade || (bubble._fade = { len: 0, marks: [] });
    if (total < f.len) {
      // Text shrank (markdown re-interpreted, or the bubble was reset).
      f.marks = f.marks.filter(m => m.from < total).map(m => ({ ...m, to: Math.min(m.to, total) }));
    } else if (total > f.len && !opts.settle) {
      // settle (the final render) only finishes fades already running; text
      // it adds (code-card chrome, etc.) isn't new output.
      f.marks.push({ from: f.len, to: total, t: now });
    }
    f.len = total;
    f.marks = f.marks.filter(m => now - m.t < STREAM_FADE_MS && m.to > m.from);
    if (!f.marks.length) return;
    const minFrom = f.marks[0].from;
    // Walk text nodes backwards from the end until the oldest fading offset.
    const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT);
    walker.currentNode = bubble;
    let node = walker.lastChild();
    let end = total;
    const todo = [];
    while (node && end > minFrom) {
      const start = end - node.nodeValue.length;
      todo.push([node, start, end]);
      end = start;
      node = walker.previousNode();
    }
    for (const [tn, start, stop] of todo) {
      const parent = tn.parentElement;
      if (!parent || !tn.nodeValue.trim() || parent.closest(STREAM_FADE_SKIP)) continue;
      const pieces = [];
      let pos = start;
      for (const m of f.marks) {
        const a = Math.max(m.from, start), b = Math.min(m.to, stop);
        if (b <= a) continue;
        if (a > pos) pieces.push([pos, a, -1]);
        pieces.push([a, b, now - m.t]);
        pos = b;
      }
      if (!pieces.length) continue;
      if (pos < stop) pieces.push([pos, stop, -1]);
      const text = tn.nodeValue;
      const frag = document.createDocumentFragment();
      for (const [a, b, age] of pieces) {
        const s = text.slice(a - start, b - start);
        if (age < 0) { frag.appendChild(document.createTextNode(s)); continue; }
        const span = document.createElement("span");
        span.className = "stream-fresh";
        span.style.animationDelay = `-${Math.round(age)}ms`;
        span.textContent = s;
        frag.appendChild(span);
      }
      tn.replaceWith(frag);
    }
  }

  function paintStreamDelta(ctx) {
    const { bubble } = ctx;
    const row = ctx.row;
    if (!row || !row.isConnected || !bubble || row._streamClosed) return;
    const newBuf = pacedBuf(ctx);
    // Controlled cadence on top of rAF alignment; remote browsers get a calmer
    // one. A paint that comes too soon runs when the interval ends instead of
    // being dropped: a dropped paint held a burst's tail back until the next
    // delta, so two bursts landed at once.
    // Locally the interval follows what a paint costs (about 4x its time, 16 to
    // 50 ms): cheap paints run every frame, a long answer's heavier markdown
    // backs off to the old 50 ms so painting never piles up.
    const paintNow = performance.now();
    const paintInterval = state.clientContext?.remote ? 90
      : Math.min(50, Math.max(16, (row._paintCost || 4) * 4));
    const sinceLast = paintNow - (row._lastStreamPaint || 0);
    if (row._lastStreamPaint && sinceLast < paintInterval) {
      if (!row._paintRetry) {
        row._paintRetry = setTimeout(() => {
          row._paintRetry = 0;
          scheduleStreamPaint(row, () => paintStreamDelta(ctx));
        }, paintInterval - sinceLast);
      }
      return;
    }
    row._lastStreamPaint = paintNow;

    let { thinking, content } = splitThinkingMemo(row, newBuf);
    if (row._channelThinking) {
      // reasoning rides its own SSE channel; buf is pure answer text
      thinking = "";
    } else if (!thinking && content
        && state.settings.enable_thinking !== false
        && state.reasoningCapability?.supported) {
      // Default-dim presentation: untagged early output shows as working notes and
      // is promoted to the answer once it stops reading like planning prose.
      if (!row._answerPromoted) {
        if (isLikelyLiveWorkingNotes(content)) { thinking = content; content = ""; }
        else if (shouldPromoteAnswer(content)) row._answerPromoted = true;
        else { thinking = content; content = ""; }
      }
    }
    const cascadeRes = splitCascade(content);
    content = cascadeRes.content;

    if (thinking && !row._channelThinking) {
      const thinkContent = row.querySelector(".think-content");
      if (thinkContent) {
        thinkContent._fullText = thinking;
        if (!thinkContent.classList.contains("hidden")) {
          thinkContent.textContent = thinking;
        }
      }
    }
    // Status line: tool running -> its own label; answer streaming ->
    // "Writing response"; reasoning only -> "Thinking…" / "Thinking about <file>".
    const _toolRunning = ctx.toolStack && ctx.toolStack.querySelector(".tcard-row.running");
    if (!_toolRunning) {
      updateThinkLine(row, true, content.trim() ? "Writing response" : thinkingLabel());
    }

    // Live cascade chips render into the parent container regardless of other content
    if (cascadeRes.cascade && cascadeRes.cascade.length > 0) {
      let btns = cascadeRes.cascade.map(text =>
        `<button class="cascade-chip" data-prompt="${esc(text)}"><i class="ph ph-sparkle"></i>${esc(text)}</button>`
      ).join("");
      let container = row.querySelector(".cascade-container");
      if (!container) {
        container = document.createElement("div");
        container.className = "cascade-container";
        bubble.parentNode.appendChild(container);
      }
      container.innerHTML = btns;
    } else {
      let container = row.querySelector(".cascade-container");
      if (container) container.remove();
    }

    // INTERIM narration (before the last tool call) vs live ANSWER (after).
    let interim = "";
    let answer = content;
    const _base = row._answerBufBase || 0;
    if (_base > 0) {
      const before = baseSplitMemo(row, newBuf, _base);
      if (before && before.trim() && content.startsWith(before)) {
        interim = before;
        answer = content.slice(before.length);
      }
    }
    row._interimText = interim;   // reused by the final re-render

    if (content.trim()) {
      // cancel any in-flight collapse — real content arrived
      if (bubble._collapsing) {
        clearTimeout(bubble._collapseTimer);
        bubble._collapsing = false;
        bubble.classList.remove("collapsing");
      }
      bubble.classList.remove("hidden");
      const interimHtml = interim.trim() ? renderWorkingNotesBlock(interim) : "";
      let didRender = false;
      let usedPlainCode = false;

      // Incremental rendering: blocks before the last safe cut are frozen —
      // their HTML is cached and reused verbatim; only the volatile tail
      // re-renders per paint. Prefix mismatch (reclassification / round
      // reset) falls back to a full render automatically.
      let inc = bubble._incRender;
      if (!(inc && inc.src && answer.startsWith(inc.src))) {
        inc = bubble._incRender = { src: "", html: "" };
      }
      const cut = findStableCut(answer, inc.src.length);
      if (cut > inc.src.length) {
        inc.html += renderMarkdown(answer.slice(inc.src.length, cut));
        inc.src = answer.slice(0, cut);
      }
      const tailStart = inc.src.length;

      // Big code in progress: stream the fence body as plain text rows (cheap esc,
      // no highlight or line spans); highlighting happens once at the final render.
      const fence = scanOpenFenceState(answer);
      const fenceLang = (fence.infoStr || "").split(/\s+/)[0].toLowerCase();
      if (fence.inFence && fence.bodyLen > 4000
          && fenceLang !== "tool_code" && fenceLang !== "tool_call" && fenceLang !== "email"
          && fenceBodyLooksLikeCode(answer.slice(fence.bodyStart), fence.infoStr)) {
        usedPlainCode = true;
        const now = Date.now();
        if (now - (bubble._lastPlainAt || 0) >= 250) {
          bubble._lastPlainAt = now;
          const lang = fenceLang || "code";
          const lines = (answer.slice(fence.bodyStart).match(/\n/g) || []).length + 1;
          const kb = (fence.bodyLen / 1024).toFixed(1);
          const head = answer.slice(tailStart, fence.openIdx);
          const headHtml = head ? renderMarkdown(head, { streaming: true }) : "";
          bubble.innerHTML = interimHtml + inc.html + headHtml +
            `<div class="stream-code-hint"><i class="ph ph-code"></i><span>writing <strong>${esc(lang)}</strong> — ${lines} lines, ${kb} KB · syntax colors land when the block closes</span></div>` +
            `<pre class="code-card streaming-plain"><code>${esc(answer.slice(fence.bodyStart))}</code></pre>`;
          didRender = true;
        }
      } else {
        bubble._lastPlainAt = 0;
        // Unclosed fences go to renderMarkdown as-is; the line-anchored extractor
        // keeps code-like bodies as cards and releases prose-like ones to text.
        const tailHtml = answer.length > tailStart
          ? renderMarkdown(answer.slice(tailStart), { streaming: true }) : "";
        bubble.innerHTML = interimHtml + inc.html + tailHtml;
        enhanceCodeBlocks(bubble);
        didRender = true;
      }
      if (didRender) applyStreamFade(bubble);
      // The completion event normally carries the same text as the final
      // streamed paint. Retain this exact snapshot so it can settle metadata
      // without replacing an already-stable prose node and nudging the chat.
      if (didRender) {
        bubble._streamSnapshot = { content, answer, usedPlainCode, openFence: fence.inFence };
      }
      // NB: no updateThinkLine(false) here — the status line stays a live
      // shimmering indicator; the finally block finalizes it once at turn end.
    } else if ((bubble.innerHTML || !bubble.classList.contains("hidden")) && !bubble._collapsing) {
      // Content was demoted into thinking or stripped (partial </tool_call>,
      // reclassification). Fade the bubble out instead of popping it away,
      // and hold autoscroll so the viewport doesn't jump twice.
      bubble._collapsing = true;
      bubble.classList.add("collapsing");
      row._scrollHoldUntil = Date.now() + 420;
      bubble._collapseTimer = setTimeout(() => {
        bubble._collapsing = false;
        bubble.classList.remove("collapsing");
        bubble.innerHTML = "";
        bubble._incRender = null;
        bubble.classList.add("hidden");
      }, 200);
    }
    if (Date.now() >= (row._scrollHoldUntil || 0)) scrollToBottom();
    const cost = performance.now() - paintNow;
    row._paintCost = row._paintCost ? row._paintCost * 0.7 + cost * 0.3 : cost;
  }

  function handleEvent(evt, ctx) {
    const { bubble, toolStack, toolCards, row } = ctx;
    // Anything but plain streaming text sees the whole received buffer first.
    if (row?._pace && !STREAM_PACE_PASSTHROUGH.has(evt.type)) streamPaceFlush(row);
    if (evt.type === "approval_request") {
      // The same approval as the shared stream's approval:new, sent down this
      // turn's own stream, which is live even when the shared one lags (a
      // remote browser could wait minutes for the card).
      if (evt.approval?.id) receiveApproval(evt.approval);
      return;
    }
    if (evt.type === "steering_applied") {
      const queue = messageQueue(evt.chat_id);
      const index = queue.findIndex(item => item.id === evt.id);
      if (index >= 0) queue.splice(index, 1);
      if (state.chatId === evt.chat_id) renderMessageQueue();
      row._appliedUpdates ||= new Set();
      if (!row._appliedUpdates.has(evt.id)) {
        row._appliedUpdates.add(evt.id);
        // The reply this update cut short stays on screen above it, as it will
        // after a reload; the live bubble below then carries on from it.
        if (evt.kept_partial) {
          row.before(renderBubble({ role: "assistant", content: evt.kept_partial, _internal: true,
            _steer_partial: true, t: Date.now() / 1000 }));
        }
        const update = renderBubble({ role: "user", content: evt.text, images: evt.images || [], _steering_id: evt.id, t: Date.now() / 1000 });
        row.before(update);
      }
      ctx.setBuf("");
      streamPaceReset(row);
      row._answerBufBase = 0;
      row._thinkBuf = "";
      clearTimeout(bubble._collapseTimer);
      bubble._collapsing = false;
      bubble._incRender = null;
      bubble.innerHTML = "";
      bubble.classList.add("hidden");
      updateThinkLine(row, true, "Considering your update…");
      return;
    }
    if (evt.type === "steering_deferred") {
      for (const item of messageQueue(evt.chat_id)) {
        if ((evt.ids || []).includes(item.id)) { item.status = "failed"; item.error = "Task stopped before delivery. Retry to send this update."; }
      }
      if (state.chatId === evt.chat_id) renderMessageQueue();
      return;
    }
    if (evt.type === "delta") {
      const newBuf = ctx.getBuf() + evt.content;
      ctx.setBuf(newBuf);
      // Live cost estimate: count every delta (chars/4) here, not inside the render
      // throttle below, or fast streams go mostly uncounted. Delta length rather
      // than buf length, because buf resets between rounds while the estimate
      // accumulates across the turn.
      state._streamOutEstimate += Math.round(evt.content.length / 4);
      // Throttled gauge + cost RENDER during streaming (display only).
      if (!state._lastGaugeUpdate || Date.now() - state._lastGaugeUpdate > 500) {
        renderCtxGauge();
        renderCostWidget();
        state._lastGaugeUpdate = Date.now();
      }
      // Live tok/s in the bubble meta. The start time lives on the agentRow node
      // (ctx is rebuilt every event); the count is approximate (chars/4) until the
      // real stats arrive.
      if (ctx.row) {
        if (!ctx.row._streamStart) ctx.row._streamStart = Date.now();
        const elapsed = (Date.now() - ctx.row._streamStart) / 1000;
        if (elapsed > 0.5 && (!ctx.row._lastTpsUpdate || Date.now() - ctx.row._lastTpsUpdate > 400)) {
          ctx.row._lastTpsUpdate = Date.now();
          const approxTokens = Math.max(1, Math.round(newBuf.length / 4));
          const liveTps = (approxTokens / elapsed).toFixed(1);
          renderStatusFast(liveTps, "streaming");
          const meta = ctx.row.querySelector(".bubble-meta.streaming");
          if (meta) {
            const dots = meta.querySelector(".typing");
            meta.innerHTML = `${liveTps} tok/s · streaming`;
            if (dots) meta.appendChild(dots);
            else {
              const d = document.createElement("span");
              d.className = "typing";
              d.innerHTML = "<span></span><span></span><span></span>";
              meta.appendChild(d);
            }
          }
        }
      }
      // Research progress is shown by the notebook rail; render its answer at completion.
      if (row?._research) return;
      // The pacer reveals the new text over the next frames at the arrival speed
      // (painting stays frame-aligned, coalesced and throttled). The buffer and
      // counters above consume every delta; the final event always does one
      // authoritative full render.
      if (ctx.row) streamPaceArrival(ctx, evt.content.length);
      else scheduleStreamPaint(ctx.row, () => paintStreamDelta(ctx));
    } else if (evt.type === "thinking_start") {
      // Reasoning arrived on its own SSE channel (bridge saw
      // reasoning_content) — no tag parsing, no answer/thinking ambiguity.
      if (ctx.row) {
        ctx.row._channelThinking = true;
        // Each model request in a turn thinks anew; mark where one ends so a
        // harness cut and retry is visible instead of reading as one run-on block.
        if (ctx.row._thinkBuf && evt.round !== ctx.row._thinkRound) {
          ctx.row._thinkBuf += "\n\n[next round]\n\n";
        }
        ctx.row._thinkRound = evt.round;
        updateThinkLine(ctx.row, true, thinkingLabel());
      }
    } else if (evt.type === "thinking_delta") {
      const r = ctx.row;
      if (!r) return;
      r._thinkBuf = (r._thinkBuf || "") + evt.content;
      const tc = r.querySelector(".think-content");
      if (tc) {
        tc._fullText = r._thinkBuf;
        if (!tc.classList.contains("hidden")) tc.textContent = r._thinkBuf;
      }
    } else if (evt.type === "thinking_end") {
      // boundary only — answer deltas follow as plain delta events
    } else if (evt.type === "command_requested") {
      const target = executionTarget(evt);
      const item = executionActivity(row, evt);
      if (item) {
        item.status = "requested";
        item.target = target;
        item.command = evt.command || item.command;
      }
      appendAgentLog(`${target === "sandbox" ? "WSL GUEST" : "HOST"} command requested: ${evt.command || ""}`);
      setToolActivityLabel(toolStack, toolCards, evt, "Waiting for command approval…");
    } else if (evt.type === "subagent_progress") {
      setToolActivityLabel(toolStack, toolCards, { ...evt, name: "spawn_subagent" }, evt.label || "Sub-agents working…");
      if (evt.log) appendAgentLog(evt.log);
    } else if (evt.type === "command_spawned") {
      const liveActivity = findToolActivity(toolStack, toolCards, evt);
      if (liveActivity) {
        liveActivity.dataset.waiting = "false";
        liveActivity.dataset.t0 = String(Date.now());
        liveActivity.dataset.targetLabel = executionTarget(evt) === "sandbox" ? "WSL guest" : "This PC";
        setToolActivityLabel(toolStack, toolCards, evt, evt.name === "run_tests" ? "Running project tests" : "Running command");
      }
      const target = executionTarget(evt);
      const item = executionActivity(row, evt);
      if (item) {
        item.status = "running";
        item.target = target;
        item.command = evt.command || item.command;
        item.t0 = Date.now();
        item.duration = "";
      }
      const heading = target === "sandbox"
        ? "[WSL GUEST · accuretta-sbx · Windows interop off]"
        : `[HOST · ${evt.name === "run_tests" ? "project tests" : "Windows PowerShell"}]`;
      appendExecutionText(target, `\n${heading}\n$ ${evt.command || ""}\n`, false, true);
      appendAgentLog(`${target === "sandbox" ? "WSL GUEST" : "HOST"} command started: ${evt.command || ""}`);
      setToolActivityLabel(toolStack, toolCards, evt,
        evt.name === "run_tests" ? "Running project tests" : "Running command");
    } else if (evt.type === "command_finished") {
      const target = executionTarget(evt);
      const exitCode = evt.exit_code ?? 0;
      const failed = Boolean(evt.error || evt.killed || evt.timed_out || Number(exitCode) !== 0);
      const item = executionActivity(row, evt, false);
      if (item) {
        item.status = failed ? "err" : "ok";
        item.duration = evt.duration_ms != null
          ? (evt.duration_ms < 1000 ? `${evt.duration_ms}ms` : `${(evt.duration_ms / 1000).toFixed(1)}s`)
          : fmtToolDuration(item.t0);
      }
      const suffix = evt.killed ? " (killed)" : evt.timed_out ? " (timed out)" : "";
      appendExecutionText(target, `\nCommand finished with exit code ${exitCode}${suffix}\n`, failed, true);
      appendAgentLog(`${target === "sandbox" ? "WSL GUEST" : "HOST"} command finished: exit code ${exitCode}${suffix}`);
    } else if (evt.type === "command_not_executed") {
      const target = executionTarget(evt);
      const item = executionActivity(row, evt);
      if (item) {
        item.status = "err";
        item.duration = "not run";
      }
      const reason = evt.message || evt.reason || "blocked before execution";
      appendExecutionText(
        target,
        `\n[${target === "sandbox" ? "WSL GUEST" : "HOST"} · NOT EXECUTED]\n$ ${evt.command || ""}\n${reason}\n`,
        true,
        true,
      );
      appendAgentLog(`${target === "sandbox" ? "WSL GUEST" : "HOST"} command was not executed: ${reason}`);
    } else if (evt.type === "tool_start") {
      secretRailToolStart(row, evt.name, evt.arguments);
      attackRailToolStart(row, evt.name, evt.arguments);
      osintCardToolStart(row, evt.name, evt.arguments);
      if (evt.name === "save_skill") {
        const chatId = state.liveTurn?.chatId || state.chatId;
        const skill = typeof evt.arguments?.name === "string" ? evt.arguments.name.trim() : "";
        ensureSkillSaveRow(chatId, skill, String(evt.call_id || ""));
      }
      // Mark the content boundary: everything streamed before this tool call is
      // interim narration (rendered dimmed), everything after is the live answer.
      // Each step's narration ends here, so give it its own paragraph: steps
      // streamed back to back with nothing between them ("…the log.No Java…").
      // STEP_BREAK carries an invisible marker so the final render can tell
      // these breaks from the model's own text.
      if (row) {
        const buf = ctx.getBuf();
        // Only after a step that wrote visible text: a break after pure
        // reasoning would make an empty reply look non-empty.
        const visible = buf ? splitThinking(buf).content.replace(/⁣/g, "").trim() : "";
        if (visible && !/\n\s*\n\s*$/.test(buf)) ctx.setBuf(buf + STEP_BREAK);
        row._answerBufBase = ctx.getBuf().length;
      }
      if (evt.name === "session_start") {
        surfaceShell(evt.arguments?.session_id || evt.arguments?.id || "");
      }
      startToolActivity(toolStack, toolCards, evt);
      const orbPhase = orbPhaseForTool(evt.name);
      if (!["run_powershell", "run_tests", "sandbox_run", "sandbox_nmap", "sandbox_sqlmap"].includes(evt.name)) {
        const lbl = toolLabel(evt.name, evt.arguments);
        appendAgentLog(`Tool started: ${lbl}`);
      }
      
      // Update our activities state
      if (row) {
        if (!row._activities) {
          row._activities = { writes: [], commands: [], mcp: [] };
        }
        const act = row._activities;
        if (evt.name === "write_file" || evt.name === "edit_file") {
          const path = evt.arguments?.path || "file";
          let lines = 0;
          if (evt.name === "write_file" && evt.arguments?.content) {
            lines = evt.arguments.content.split("\n").length;
          }
          let existing = act.writes.find(w => w.path === path);
          if (!existing) {
            act.writes.push({
              path: path,
              added: lines,
              deleted: 0,
              status: "running",
              t0: Date.now()
            });
          } else {
            existing.status = "running";
            if (lines > 0) existing.added = lines;
          }
        } else if (evt.name && evt.name.startsWith("mcp_")) {
          act.mcp.push({
            name: evt.name,
            arguments: evt.arguments,
            status: "running",
            t0: Date.now(),
            duration: ""
          });
        }
      }
      
      // Keep the turn visibly alive. The concrete action sits in the trail;
      // this header remains the honest overall state until the answer starts.
      if (row) updateThinkLine(row, true, "Working…");
      // After the header update, which would otherwise put the orb back on "thinking".
      window.AccurettaOrb?.setState(row, orbPhase);
      // Estimate tokens for the tool call itself — the model generated
      // the tool name + JSON arguments, which aren't in content deltas.
      const argsLen = evt.arguments ? JSON.stringify(evt.arguments).length : 0;
      state._streamOutEstimate += Math.round((evt.name.length + argsLen) / 4);
      renderCostWidget();
      scrollToBottom();
    } else if (evt.type === "tool_stream") {
      const streaming = findToolActivity(toolStack, toolCards, evt);
      if (streaming) streaming.dataset.lastOut = String(Date.now());
      const streamTarget = evt.target || (evt.name && evt.name.startsWith("sandbox_") ? "sandbox"
        : (["run_powershell", "run_tests"].includes(evt.name) ? "host" : ""));
      if (streamTarget === "host") {
        appendTerminalText((evt.text || "") + "\n", false);
      } else if (streamTarget === "sandbox") {
        appendSandboxText((evt.text || "") + "\n", false, false);
      }
    } else if (evt.type === "tool_progress") {
      // A tool call is still streaming its arguments, and nothing else shows until
      // it is complete: name its target and size on the status line.
      const line = row?.querySelector(".think-line");
      if (line && !row.querySelector(".tcard-row.running")) {
        const label = toolProgressLabel(evt);
        line.dataset.progressLabel = label;
        updateThinkLine(row, true, label);
      }
    } else if (evt.type === "heartbeat") {
      // The active activity row already proves a long tool is alive. Keep the
      // overall header stable instead of repeating the same tool in two places.
      if (row?.querySelector(".tcard-row.running")) return;
      const line = row && row.querySelector(".think-line");
      if (line && !line.classList.contains("done")) {
        const span = line.querySelector("span");
        if (span && span.classList.contains("shimmer")) {
          span.textContent = (evt.note || "working…").slice(0, 80);
        }
      }
    } else if (evt.type === "tool_result") {
      const completedActivity = finishToolActivity(toolStack, toolCards, evt);
      // A failed tool scatters the orb until the model's next step moves it on.
      if (toolResultFailed(evt.result)) window.AccurettaOrb?.setState(row, "error");
      secretRailToolResult(row, evt.name, evt.result);
      attackRailToolResult(row, evt.name, evt.result);
      osintCardToolResult(row, evt.name, evt.result);
      // A picture the agent showed appears under the reply right away; the reply
      // keeps the list (msg._images below) for every later redraw.
      if (evt.name === "show_image" && evt.result?.image && row) {
        const html = shownImagesHtml([evt.result.image]);
        const col = row.querySelector(".bubble-col");
        if (html && col) {
          (row._shownImages = row._shownImages || []).push(evt.result.image);
          const holder = document.createElement("div");
          holder.innerHTML = html;
          const box = col.querySelector(":scope > .shown-images");
          if (box) box.append(...holder.firstElementChild.children);
          else col.querySelector(":scope > .bubble")?.after(holder.firstElementChild);
        }
      }
      if (evt.name === "switch_execution_target" && evt.result?.ok && evt.result.execution_target) {
        state.executionTarget = String(evt.result.execution_target);
        localStorage.setItem("accuretta:execution-target", state.executionTarget);
        if (state.remoteAccess?.context) {
          state.remoteAccess.context.execution_target = state.executionTarget;
          state.remoteAccess.context.target_label = evt.result.target_label || "Inference PC";
        }
        renderClientContext();
        toast(`Commands now target ${evt.result.target_label || "the selected device"}`, "info", 2200);
      }
      if (evt.name === "save_skill") {
        const chatId = state.liveTurn?.chatId || state.chatId;
        const failed = toolResultFailed(evt.result);
        const denied = failed && /denied|cancelled/i.test(String(evt.result?.error || ""));
        finishSkillSaveRow(
          chatId,
          denied ? "cancelled" : failed ? "failed" : "saved",
          String(evt.result?.skill || ""),
          String(evt.call_id || ""),
        );
      }
      if (evt.name === "web_image_search" && evt.result && evt.result.results) {
        evt.result.results.forEach(r => {
          if (r.image && r.url) {
            state.imageUrlToSourceMap.set(r.image, r.url);
          }
        });
      }
      const isExecutionTool = ["run_powershell", "run_tests", "sandbox_run", "sandbox_nmap", "sandbox_sqlmap"].includes(evt.name);
      if (!isExecutionTool) {
        const label = toolResultLabel(evt.name, evt.result);
        appendAgentLog(`Tool finished: ${label}`);
      }
      // Mark the matching live activity finished and refresh the deck so it drops
      // out of the strip above the composer. The record stays in _activities for
      // the collapsed turn-end history.
      if (row && row._activities) {
        const act = row._activities;
        const st = toolResultFailed(evt.result) ? "err" : "ok";
        if (evt.name === "write_file" || evt.name === "edit_file") {
          const path = (evt.result && evt.result.path) || "";
          const runningWrites = act.writes.filter(x => x.status === "running");
          const w = runningWrites.reverse().find(x => !path || x.path === path) || runningWrites[0];
          if (w) {
            w.status = st;
            if (evt.result && typeof evt.result.added === "number") w.added = evt.result.added;
            if (evt.result && typeof evt.result.deleted === "number") w.deleted = evt.result.deleted;
          }
        } else if (isExecutionTool) {
          const c = (evt.call_id && act.commands.find(x => x.callId === evt.call_id
              && (x.status === "requested" || x.status === "running")))
            || act.commands.find(x => (x.status === "requested" || x.status === "running") && x.tool === evt.name)
            || act.commands.find(x => x.status === "requested" || x.status === "running");
          if (c) { c.status = st; c.duration = fmtToolDuration(c.t0); }
        } else if (evt.name && evt.name.startsWith("mcp_")) {
          const runningMcp = act.mcp.filter(x => x.status === "running");
          const m = runningMcp.find(x => x.name === evt.name) || runningMcp[0];
          if (m) { m.status = st; m.duration = fmtToolDuration(m.t0); }
        }
      }
      if (completedActivity) {
        const isErr = completedActivity.classList.contains("err");
        if (!isErr && (evt.name === "edit_file" || evt.name === "write_file")) {
          if (evt.result && evt.result.path) {
            state.touchedFiles.add(evt.result.path);
            renderWorkspace();
          }
          const added = (evt.result && evt.result.added) || 0;
          const deleted = (evt.result && evt.result.deleted) || 0;
          if (added > 0 || deleted > 0) {
            const filename = folderLeafName(evt.result.path || "");
            const msg = `<span style="color:#00ff88;font-weight:600;">+${added}</span>, <span style="color:#ff3b30;font-weight:600;">-${deleted}</span> <span style="opacity:0.4;margin:0 4px;">|</span> <span style="font-weight:500;">${esc(filename)}</span>`;
            toast(msg, "ok", 4000, "file-change", true);
          }
          // On mobile: inject a preview card for .html files written by the agent
          const filePath = (evt.result && evt.result.path) || "";
          if (isMobile() && /\.html?$/i.test(filePath)) {
            const wsRoot = (state.workspace && state.workspace.folders && state.workspace.folders[0]) || "";
            if (wsRoot) {
              const rel = filePath.replace(/\\/g, "/").replace(wsRoot.replace(/\\/g, "/"), "").replace(/^\//, "");
              injectMobilePreviewCard({
                filename: folderLeafName(filePath),
                size: evt.result.bytes || 0,
                url: wsFileUrl(wsRoot, rel),
              });
            } else {
              // No workspace root — try blob URL with the content if available
              injectMobilePreviewCard({
                filename: folderLeafName(filePath),
                size: evt.result.bytes || 0,
              });
            }
          }
        }
        // web_search sources show when its row is opened (tcardDetailHtml).
        // network_snapshot: rich bar-chart card, mounted outside the tool group so it
        // stays visible while the group is collapsed (the default).
        if (!isErr && evt.name === "network_snapshot") {
          const chart = renderNetworkChart(evt.result);
          if (chart) {
            const group = toolStack.querySelector(".tool-group");
            if (group && group.parentElement) {
              group.insertAdjacentHTML("afterend", chart);
            } else {
              completedActivity.insertAdjacentHTML("afterend", chart);
            }
          }
        }
        updateToolGroupHead(toolStack);
      }
    } else if (evt.type === "tool_dialect_warning") {
      // Dialect parsing errors: show a distinct system notice so the user knows the
      // tool didn't run, visually separate from the model's prose.
      bubble.classList.remove("hidden");
      bubble.innerHTML += `<div class="dialect-warn"><i class="ph ph-warning"></i><span><strong>Warning:</strong> ${esc(evt.message)}</span></div>`;
    } else if (evt.type === "tools_unavailable") {
      // The model can't do tools (e.g. Gemma): show a persistent notice above the
      // bubble, not inside it (bubble.innerHTML is replaced while streaming). Reuses
      // the .dialect-warn styling.
      let note = row && row.querySelector(".tools-off-note");
      if (!note && bubble && bubble.parentNode) {
        note = document.createElement("div");
        note.className = "dialect-warn tools-off-note";
        bubble.parentNode.insertBefore(note, bubble);
      }
      if (note) {
        note.innerHTML = `<i class="ph ph-warning"></i><span><strong>Tools off:</strong> ${esc(evt.message)}</span>`;
      }
    } else if (evt.type === "context_trimmed") {
      // Automatic trimming/compaction is invisible infrastructure: keep the per-turn
      // diagnostic marker for the tool-group details, but no toasts. Manual compact
      // stays visible through its own button response.
      if (row) {
        row.dataset.dropped = evt.dropped;
        // Re-render tool group head if it exists to pick up the dropped count
        const stack = row.querySelector(".tool-stack");
        if (stack) updateToolGroupHead(stack);
      }
    } else if (evt.type === "version_saved") {
      state.versions.push(evt.version);
      renderVersions();
      setActiveVersion(evt.version.id);
    } else if (evt.type === "savings") {
      // Lifetime token totals pushed by the server after a turn, the source of
      // truth for the "saved vs cloud" card. Set, not added, so the client can't
      // drift or double-count.
      _applySavings(evt);
    } else if (evt.type === "stats") {
      const tok = evt.eval_count;
      const dur = (evt.eval_duration || 0) / 1e9;
      if (Number.isFinite(tok)) {
        state.tokTotal += tok;
        // Clear streaming estimate — real values have arrived
        state._streamOutEstimate = 0;
        state._streamPromptEstimate = 0;
      }
      if (dur > 0 && tok > 0) {
        state.totalGenDuration = (state.totalGenDuration || 0) + dur;
      }
      if (Number.isFinite(tok)) {
        renderTokTotal();
      }
      const tps = dur > 0 ? (tok / dur).toFixed(1) : "—";
      renderStatus(tps, "idle");
      const meta = bubble.parentElement.querySelector(".bubble-meta");
      if (meta) {
        const finished = bubble.closest(".bubble-row")?.dataset.wbFinished || "";
        meta.textContent = `${finished ? `${finished} · ` : ""}${tok} tok · ${tps} tok/s`;
        if (state.streaming && meta.classList.contains("streaming")) {
          const dots = document.createElement("span");
          dots.className = "typing";
          dots.innerHTML = "<span></span><span></span><span></span>";
          meta.appendChild(dots);
        }
      }
      // accumulate prompt tokens for cost widget
      const promptTok = evt.prompt_eval_count;
      if (Number.isFinite(promptTok) && promptTok > 0) {
        state.tokPromptTotal += promptTok;
      }
      // All-time totals come from the server ("savings" event); the client doesn't
      // accumulate them.
      renderCostWidget();
      // Agent turns run several rounds and each emits stats; reset the stream start
      // so the next round's live tok/s starts fresh.
      if (ctx.row) {
        ctx.row._streamStart = null;
        ctx.row._lastTpsUpdate = null;
      }
      // stash for the final message object
      state._lastMsgTokens = tok;
      state._lastMsgPromptTokens = evt.prompt_eval_count;
      // prompt_eval_count is measured truth from llama-server, not an
      // estimate — mark it so the tooltip stops claiming otherwise.
      state._ctxSource = "live";
      if (evt.ctx_breakdown && typeof evt.ctx_breakdown === "object") state._ctxBreakdown = evt.ctx_breakdown;
      // refresh gauge live — prompt_eval_count is the truth from llama-server,
      // and tool-heavy turns can blow past where the char-count estimate sits.
      renderCtxGauge();
    } else if (evt.type === "final") {
      if (row && evt.message._research?.status === "complete"
          && evt.message._research.presentation && !evt.message._failed && !row._notificationCancelled) {
        row._notificationFailed = false;
        row._notificationError = null;
      }
      const full = evt.message.content || "";
      const msg = {
        role: "assistant",
        content: full,
        _research: evt.message._research,
        t: Math.floor(Date.now() / 1000),
        tokens: state._lastMsgTokens || 0,
        prompt_tokens: state._lastMsgPromptTokens || 0,
      };
      if (row?._shownImages?.length) msg._images = row._shownImages.slice(-12);
      // The final message belongs to the chat this turn streamed into, not
      // whichever chat is selected now (state.messages is the selected chat's
      // in-memory copy). It is already persisted server-side, so only graft it into
      // the view when we're still on the streamed chat (or the turn has no live
      // tracking, e.g. regenerate, which always targets the chat on screen).
      if (state.liveTurn && state.liveTurn.chatId !== state.chatId) {
        // User switched chats mid-turn: the bridge persists the final message and
        // loadChats() in the stream's finally refreshes it.
      } else {
      // Fallback when no stats event arrived (some llama-server versions emit no
      // timings/usage): use the streaming char estimate so the cost widget isn't
      // stuck at $0.00.
      if (!state._lastMsgTokens && full.length > 0) {
        const fallbackTok = Math.max(1, Math.round(full.length / 4));
        state.tokTotal += fallbackTok;
        msg.tokens = fallbackTok;
        // Clear streaming estimate since we've committed the fallback
        state._streamOutEstimate = 0;
        state._streamPromptEstimate = 0;
        // All-time is server-owned (the server applies the same char-estimate
        // fallback and emits a "savings" event); don't accumulate it here.
        renderTokTotal();
        renderCostWidget();
      }
      state.messages.push(msg);
      if (msg._research) window.AccurettaResearch?.update(row, msg._research);
      state._lastMsgTokens = 0;
      // Keep this turn's real prompt-token count so the gauge holds true fill
      // between turns; the next turn (or the 2s poll) refreshes it.
      if (msg.prompt_tokens > 0) state._lastMsgPromptTokens = msg.prompt_tokens;
      // update the meta tooltip on the bubble we just rendered
      const rows = [...document.querySelectorAll("#chat-inner .bubble-row")];
      const lastRow = rows.reverse().find(r => r.querySelector(".bubble.agent"));
      if (lastRow) {
        const meta = lastRow.querySelector(".bubble-meta");
        if (meta && msg.tokens) {
          meta.title = `${msg.tokens.toLocaleString()} tokens${msg.prompt_tokens ? ` (prompt: ${msg.prompt_tokens.toLocaleString()})` : ""}`;
        }
        wbFinishLiveRow(lastRow, msg);
        // Final-event re-render: streaming deltas can race or miss a fence boundary
        // and leave the bubble blank for pure-code or thinking-then-fence replies.
        // `full` is authoritative, so render it now.
        const finalBubble = lastRow.querySelector(".bubble.agent");
        if (finalBubble) {
          // The authoritative render supersedes any queued streaming paint: cancel it
          // and drop the incremental cache so nothing re-paints a stale buffer.
          cancelStreamPaint(lastRow);
          finalBubble._incRender = null;
          const { content: finalContent } = splitThinking(full);
          // Keep interim narration dimmed in the final render too, using the string
          // captured during streaming; if it's no longer a clean prefix, nothing is
          // demoted.
          const interimText = lastRow._interimText || "";
          // The step breaks exist only in the live buffer, not in the bridge's
          // text, so match the prefix without them too.
          const interimBare = interimText.replace(STEP_BREAK_RE, "");
          let interimHtml = "";
          let answerText = finalContent;
          if (interimText.trim()) {
            const cut = finalContent.startsWith(interimText) ? interimText.length
              : (interimBare !== interimText && finalContent.startsWith(interimBare)) ? interimBare.length : -1;
            if (cut >= 0) {
              interimHtml = renderWorkingNotesBlock(interimText);
              answerText = finalContent.slice(cut);
            }
          }
          // Decide synchronously (reads live DOM), apply in the finish batch so the
          // bubble rewrite and think-line flip land in one frame. mode: "keep" = live
          // DOM is already correct, "show" = rewrite, "hide" = nothing renderable. Empty
          // finals skip everything (pure tool-call turns leave streaming state alone).
          let mode = "hide";
          let rendered = "";
          let hasFinal = false;
          if (finalContent.trim()) {
            hasFinal = true;
            const snapshot = finalBubble._streamSnapshot;
            const keepLiveProse = snapshot
              && snapshot.content === finalContent
              && snapshot.answer === answerText
              && !snapshot.usedPlainCode
              && !snapshot.openFence
              && !finalBubble.classList.contains("hidden")
              && !!finalBubble.innerHTML.trim()
              && !finalBubble.querySelector("pre.code-card, .code-card-tabs-container");
            if (keepLiveProse) {
              mode = "keep";
            } else {
              rendered = renderMarkdown(answerText);
              if ((rendered && rendered.trim()) || interimHtml) mode = "show";
            }
          }
          if (hasFinal) {
            scheduleFinishBatch(lastRow, () => {
              if (!finalBubble.isConnected) return;
              if (mode === "show") {
                if (finalBubble._collapsing) {
                  clearTimeout(finalBubble._collapseTimer);
                  finalBubble._collapsing = false;
                  finalBubble.classList.remove("collapsing");
                }
                finalBubble.classList.remove("hidden");
                finalBubble.innerHTML = interimHtml + rendered;
                enhanceCodeBlocks(finalBubble);
                // Let the last words finish their fade instead of snapping.
                applyStreamFade(finalBubble, { settle: true });
              } else if (mode === "hide") {
                // All content was tool-call-stripped. Keep the bubble hidden.
                finalBubble.innerHTML = "";
                finalBubble.classList.add("hidden");
              }
              updateThinkLine(lastRow, false);
            });
          }
        }
      }
      // parse companion files emitted alongside the primary html block
      // (```css path=style.css ..., ```js path=script.js ..., etc.)
      const files = parseMultiFileBlocks(full);
      if (Object.keys(files).length) {
        state.currentFiles = files;
        if (state.currentHtml) renderPreview();
      }
      renderCtxGauge();
      renderRegenerateChip();
      setTimeout(() => checkModelAdvisor({ notify: true }).catch(() => {}), 700);
      }
    } else if (evt.type === "research_update") {
      window.AccurettaResearch?.update(row, evt.research);
    } else if (evt.type === "notice") {
      if (!evt.quiet) toast(evt.note || "", "info", 3000, "ctx-notice");
    } else if (evt.type === "project_follow") {
      // The agent started changing files in a folder: the chip follows it.
      const chat = state.chats?.chats?.[evt.chat_id || state.chatId];
      if (chat && evt.project_workspace) {
        chat.project_workspace = evt.project_workspace;
        if (!evt.chat_id || evt.chat_id === state.chatId) {
          renderProjectChip(chat);
          renderTopbarContext(chat);
          const chip = $("#project-chip");
          chip?.classList.remove("just-followed");
          void chip?.offsetWidth;
          chip?.classList.add("just-followed");
        }
        renderChatList();
      }
    } else if (evt.type === "goal_mode") {
      // Bridge echo of the /goal latch on this turn's own stream; apply it
      // unconditionally (chat_id can still be null on a brand-new chat, exactly
      // when the first flourish must show).
      setGoalModeUI(!!evt.active, { goal: evt.goal });
    } else if (evt.type === "breach") {
      if (!isAttackRailEnabled(row)) return;
      // Cyber-range / CTF: a FLAG{...} was captured in a tool response = a
      // confirmed breach. Advance the attack-chain rail and surface a toast.
      attackRailBreach(row, evt.stage);
      // Inline SVG flag — no emoji, ever. Model-supplied fields are esc()'d
      // since toast() renders via innerHTML.
      const breachFlag = `<svg class="toast-ico" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>`;
      toast(`${breachFlag} Breach — stage ${esc(String(evt.stage))}: ${esc(evt.flag)} (via ${esc(evt.via)})`, "ok", 6000, null, true);
    } else if (evt.type === "rt_phase") {
      // Recon -> exploit gate opened. Subtle marker only (see rtPhaseMarker).
      if (isAttackRailEnabled(row)) rtPhaseMarker(row, evt.via);
    } else if (evt.type === "rt_mission") {
      if (evt.engagement === "passive_osint") passiveIntelMission(row, evt);
      else if (evt.engagement === "frontend_secrets") secretRailMission(row, evt);
      else if (evt.engagement === "pentest" && isAttackRailEnabled(row)) {
        if (row) row._rtEngagement = "pentest";
        const deck = document.querySelector("#revealer-deck");
        if (deck) delete deck.dataset.rtEngagement;
        attackRailMission(row, evt);
      }
    } else if (evt.type === "turn_changes") {
      renderTurnChanges(row, evt);
    } else if (evt.type === "plan") {
      renderPlanPanel(evt.steps || []);
    } else if (evt.type === "chat:note") {
      // A GitHub branch event (or other UI note) persisted into the current
      // session: mirror it into the live view, deduped against notes already
      // loaded into state.messages.
      if (evt.chat_id && evt.chat_id !== state.chatId) return;
      if (!evt.content) return;
      if (state.messages.some(m => m._note && m.content === evt.content)) return;
      state.messages.push({
        role: "system", content: evt.content,
        t: Math.floor(Date.now() / 1000), _note: true,
      });
      renderMessages();
    } else if (evt.type === "error") {
      if (row?._stopRequested) {
        renderStoppedTurn(row);
        return;
      }
      if (row) {
        row._notificationFailed = true;
        row._notificationError = evt.error || "The agent stopped with an error.";
      }
      bubble.innerHTML = `<span style="color: var(--danger)">error: ${esc(evt.error)}</span>`;
    }
  }
  return { paintStreamDelta, handleEvent };
};
