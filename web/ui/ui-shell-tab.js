/* SPDX-License-Identifier: AGPL-3.0-only */
/* shared interactive shell (Shell tab): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["shell-tab"] = ({
  $, activateTerminalTab, esc, state, toggleConsolePane, wbRenderStatusbar,
}) => {
  let _shellTimer = null;
  let _shellActive = "";   // active session id being viewed
  let _shellOffset = 0;    // this viewer's read cursor (absolute) for that session
  function selectShellSession(id) {
    if (id === _shellActive) return;
    _shellActive = id;
    _shellOffset = 0;
    const code = document.getElementById("shell-out")?.querySelector("code");
    if (code) code.textContent = "";
  }
  async function refreshShellSessions() {
    let data;
    try { data = await (await fetch("/api/session/list")).json(); } catch { return; }
    const sessions = data.sessions || [];
    const sel = document.getElementById("shell-session");
    if (!sel) return;
    sel.innerHTML = sessions.length
      ? sessions.map(s => `<option value="${esc(s.id)}">${esc(s.id)} · ${esc((s.command || "").slice(0, 44))}${s.alive ? "" : " (exited)"}</option>`).join("")
      : `<option value="">no sessions</option>`;
    if (_shellActive && sessions.some(s => s.id === _shellActive)) sel.value = _shellActive;
    else if (sessions.length) { selectShellSession(sessions[sessions.length - 1].id); sel.value = _shellActive; }
  }
  async function refreshShellOutput() {
    if (!_shellActive) return;
    let data;
    try { data = await (await fetch(`/api/session/read?id=${encodeURIComponent(_shellActive)}&since=${_shellOffset}`)).json(); } catch { return; }
    const status = document.getElementById("shell-status");
    if (status) {
      status.className = "shell-status" + (data.alive ? " alive" : "");
      status.innerHTML = `<span class="dot"></span>${data.alive ? "running" : ("exited" + (data.exit_code != null ? ` (${data.exit_code})` : ""))}`;
    }
    const out = document.getElementById("shell-out");
    const code = out?.querySelector("code");
    if (!code || typeof data.offset !== "number") return;
    if (data.output) {
      const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 60;
      if (_shellOffset === 0) code.textContent = "";   // first paint drops the placeholder
      code.insertAdjacentText("beforeend", data.output);
      if (atBottom) out.scrollTop = out.scrollHeight;
    }
    _shellOffset = data.offset;
  }
  function startShellPoll() {
    refreshShellSessions();
    refreshShellOutput();
    if (_shellTimer) return;
    _shellTimer = setInterval(() => { refreshShellSessions(); refreshShellOutput(); }, 800);
  }
  function stopShellPoll() {
    if (_shellTimer) { clearInterval(_shellTimer); _shellTimer = null; }
  }
  async function shellSend(text) {
    if (!_shellActive) return;
    try { await fetch("/api/session/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: _shellActive, input: text }) }); } catch {}
    setTimeout(refreshShellOutput, 150);
  }
  async function shellKill() {
    if (!_shellActive) return;
    try { await fetch("/api/session/stop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: _shellActive }) }); } catch {}
    setTimeout(() => { refreshShellSessions(); refreshShellOutput(); }, 200);
  }
  // Called when the agent opens/uses a session — surface the Shell tab live.
  function surfaceShell(sessionId) {
    try { toggleConsolePane(true); } catch {}
    if (sessionId) { selectShellSession(sessionId); const sel = document.getElementById("shell-session"); if (sel) sel.value = sessionId; }
    activateTerminalTab("shell");
  }
  // Approximate llama.cpp's terminal colouring on its piped (colourless) output:
  // errors red, warnings amber, ready/success green, and the "component:" prefix
  // that leads most llama.cpp log lines tinted so the stream stays scannable.
  function colorizeBackendLine(line) {
    const safe = esc(line);
    if (/\b(err(or)?|failed|failure|cannot|no such|not found|traceback|abort(ed)?|fatal|out of memory|oom|segmentation|assert(ion)?|invalid|exception|denied)\b/i.test(line))
      return `<span class="bl-line bl-err">${safe}</span>`;
    if (/\b(warn(ing)?|deprecated|unsupported|fallback|skipp?ing|retry|retrying)\b/i.test(line))
      return `<span class="bl-line bl-warn">${safe}</span>`;
    // llama.cpp prints "model loaded" once the weights are in memory, before KV
    // cache init and slot setup; paint it amber and say what is still pending.
    if (/\bmodel loaded\b/i.test(line) && !/(server is listening|all slots)/i.test(line)) {
      return `<span class="bl-line bl-warn">${safe} <span class="bl-key">— weights in; server still initializing (KV cache / slots)…</span></span>`;
    }
    if (/(server is listening|all slots (are )?idle|slot released|starting the main loop)/i.test(line)
        || (/\b(GET|POST|HEAD|PUT|DELETE)\b/.test(line) && /\b(200|201|204)\b/.test(line)))
      return `<span class="bl-line bl-ok">${safe}</span>`;
    const m = line.match(/^([a-z][a-z0-9 _.\-]{1,38}?):(?:\s|$)/i);
    if (m)
      return `<span class="bl-line"><span class="bl-key">${esc(m[1])}:</span>${esc(line.slice(m[1].length + 1))}</span>`;
    return `<span class="bl-line">${safe}</span>`;
  }

  // A command can stream thousands of lines a second (a search that hits binary
  // files, a big log). Writing each line on its own, then reading the pane's
  // height, forced a layout per line, and the console never shrank, so every
  // line cost more than the last and the chat froze for seconds. Lines are now
  // queued and written once per frame, and the console keeps only its newest
  // TERM_KEEP_CHARS characters.
  const TERM_KEEP_CHARS = 200000;
  let _termQueue = [];
  let _termQueuedChars = 0;
  let _termDropped = 0;
  let _termFlushPending = false;
  function appendTerminalText(text, isError) {
    const s = String(text ?? "");
    _termQueue.push([s, !!isError]);
    _termQueuedChars += s.length;
    // A burst bigger than the console keeps would be trimmed right away: drop
    // its oldest lines before they reach the page.
    while (_termQueue.length > 1 && _termQueuedChars - _termQueue[0][0].length > TERM_KEEP_CHARS) {
      _termQueuedChars -= _termQueue.shift()[0].length;
      _termDropped++;
    }
    if (_termFlushPending) return;
    _termFlushPending = true;
    // requestAnimationFrame stalls while the window is hidden; the timer keeps
    // output flowing then.
    requestAnimationFrame(flushTerminalText);
    setTimeout(flushTerminalText, 120);
  }

  function flushTerminalText() {
    if (!_termFlushPending) return;
    _termFlushPending = false;
    const items = _termQueue;
    const dropped = _termDropped;
    _termQueue = [];
    _termQueuedChars = 0;
    _termDropped = 0;
    const consolePre = document.getElementById("term-console-pre");
    const codeEl = consolePre?.querySelector("code");
    if (!codeEl || !items.length) return;
    const cursor = consolePre.querySelector(".term-cursor");
    const pane = consolePre.closest('.term-tab-pane');
    // Follow the latest output only when the user is already at the bottom (or
    // the pane was hidden) — if they've scrolled up to read history, leave them.
    const stick = !pane || pane.classList.contains("hidden")
      || (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60);
    let html = "";
    for (const [t, err] of items) html += err ? `<span class="term-err">${esc(t)}</span>` : esc(t);
    const box = cursor?.parentNode || codeEl;
    if (cursor) cursor.insertAdjacentHTML("beforebegin", html);
    else codeEl.insertAdjacentHTML("beforeend", html);
    trimTerminal(box, cursor, dropped > 0);
    activateTerminalTab("terminal");
    if (pane && stick) pane.scrollTop = pane.scrollHeight;
  }

  // Drop the oldest output so the console holds about TERM_KEEP_CHARS, and say
  // at its top that earlier output is not shown.
  function trimTerminal(box, cursor, dropped) {
    let total = box.textContent.length;
    const over = total > TERM_KEEP_CHARS * 1.25;
    if (!over && !dropped) return;
    let mark = box.querySelector(":scope > .term-trimmed");
    if (over) {
      let node = box.firstChild;
      while (node && total > TERM_KEEP_CHARS) {
        const next = node.nextSibling;
        if (node !== mark && node !== cursor && !(cursor && node.contains?.(cursor))) {
          const len = node.textContent.length;
          if (node.nodeType === Node.TEXT_NODE && total - len < TERM_KEEP_CHARS) {
            node.textContent = node.textContent.slice(total - TERM_KEEP_CHARS);   // keep its newest part
            break;
          }
          total -= len;
          node.remove();
        }
        node = next;
      }
    }
    if (!mark) {
      mark = document.createElement("span");
      mark.className = "term-err term-trimmed";
      mark.textContent = "[earlier output not shown]\n";
      box.insertBefore(mark, box.firstChild);
    }
  }

  function appendSandboxText(text, isError = false, activate = false) {
    const sandboxPre = document.getElementById("sandbox-log-pre");
    if (!sandboxPre) return;
    const codeEl = sandboxPre.querySelector("code") || sandboxPre;
    const pane = sandboxPre.closest(".term-tab-pane");
    const stick = !pane || pane.classList.contains("hidden")
      || (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60);
    const safeText = isError
      ? `<span class="term-err">${esc(text)}</span>`
      : esc(text);
    codeEl.insertAdjacentHTML("beforeend", safeText);
    if (activate) activateTerminalTab("sandbox");
    if (pane && stick) pane.scrollTop = pane.scrollHeight;
  }

  function appendAgentLog(msg) {
    const agentLogPre = document.getElementById("term-agent-log-pre");
    if (!agentLogPre) return;
    const codeEl = agentLogPre.querySelector("code") || agentLogPre;
    const pane = agentLogPre.closest('.term-tab-pane');
    // Auto-follow only when already at the bottom, so scrolling up to review
    // the agent's history isn't yanked back down by the next log line.
    const stick = !pane || pane.classList.contains("hidden")
      || (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60);

    const timestamp = new Date().toLocaleTimeString();
    const safeMsg = esc(msg);
    codeEl.insertAdjacentHTML("beforeend", `[${timestamp}] ${safeMsg}<br>`);
    if (pane && stick) pane.scrollTop = pane.scrollHeight;
  }

  function ensureStatusDOM(statusLine) {
    if (!statusLine) return null;
    if (statusLine.dataset.flatBuilt === "1") {
      return {
        model: statusLine.querySelector('[data-s="model"]'),
        ctxLimit: statusLine.querySelector('[data-s="ctx-limit"]'),
        ctxWrap: statusLine.querySelector('[data-s="ctx"]'),
        ctxPct: statusLine.querySelector('[data-s="ctx-pct"]'),
        speed: statusLine.querySelector('[data-s="speed"]'),
        stateWrap: statusLine.querySelector('[data-s="state"]'),
        stateText: statusLine.querySelector('[data-s="state-text"]'),
      };
    }
    statusLine.dataset.flatBuilt = "1";
    statusLine.innerHTML = `
      <div class="status-item"><i class="ph ph-cpu"></i><span data-s="model"></span></div>
      <span class="status-dot">·</span>
      <div class="status-item"><i class="ph ph-database"></i><span data-s="ctx-limit"></span></div>
      <span class="status-dot">·</span>
      <div class="status-item status-ctx" data-s="ctx" role="button" tabindex="0" aria-label="Context usage: details and Compact"><i class="ph ph-gauge"></i><span data-s="ctx-pct"></span></div>
      <span class="status-dot">·</span>
      <div class="status-item"><i class="ph ph-lightning"></i><span data-s="speed"></span></div>
      <span class="status-dot">·</span>
      <div class="status-item status-state" data-s="state">
        <svg class="heartbeat-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>
        <span data-s="state-text"></span>
      </div>
    `;
    return ensureStatusDOM(statusLine);
  }
  function statusSpeedText(speed, isStreaming) {
    if (isStreaming) {
      if (speed && speed > 0) return `${Number(speed).toFixed(1)} tok/s`;
      if (state._lastTps && state._lastTps > 0) return `${Number(state._lastTps).toFixed(1)} tok/s`;
      return "- tok/s";
    }
    if (state.totalGenDuration && state.totalGenDuration > 0 && state.tokTotal && state.tokTotal > 0) {
      return `${(state.tokTotal / state.totalGenDuration).toFixed(1)} tok/s`;
    }
    return "- tok/s";
  }
  function renderStatus(speed, stateStr, opts) {
    wbRenderStatusbar(speed, stateStr);
    const statusLine = document.getElementById("status-line");
    if (!statusLine) { renderCtxGauge(); return; }
    const parts = ensureStatusDOM(statusLine);
    if (!parts) return;
    const isStreaming = !!state.streaming || stateStr === "streaming";
    const statusText = state._reloading
      ? "reloading…"
      : (stateStr || (isStreaming ? "streaming" : "idle"));
    if (parts.model) {
      const modelName = (state.missingModel && !state.loadedModel)
        ? "model file missing" : (state.settings.model || "no model loaded");
      if (parts.model.textContent !== modelName) parts.model.textContent = modelName;
    }
    if (!opts || !opts.skipCtx) {
      const ctxUse = computeCtxUsage();
      const ctxLimit = ctxUse.capacity >= 1024 ? Math.round(ctxUse.capacity / 1024) + "k" : ctxUse.capacity;
      const ctxPct = Math.round(ctxUse.pct * 100);
      const ctxCls = ctxUse.pct >= 0.9 ? "is-crit" : (ctxUse.pct >= 0.7 ? "is-warn" : "");
      const ctxTitle = `${ctxUse.used.toLocaleString()} / ${ctxUse.capacity.toLocaleString()} tokens of context used (~${ctxPct}%)`;
      if (parts.ctxLimit && parts.ctxLimit.textContent !== `${ctxLimit} ctx`) parts.ctxLimit.textContent = `${ctxLimit} ctx`;
      if (parts.ctxPct && parts.ctxPct.textContent !== `${ctxPct}%`) parts.ctxPct.textContent = `${ctxPct}%`;
      if (parts.ctxWrap) {
        parts.ctxWrap.classList.toggle("is-warn", ctxCls === "is-warn");
        parts.ctxWrap.classList.toggle("is-crit", ctxCls === "is-crit");
        if (parts.ctxWrap.title !== ctxTitle) parts.ctxWrap.title = ctxTitle;
      }
      renderCtxGauge(ctxUse);
    }
    const speedText = statusSpeedText(speed, isStreaming);
    if (parts.speed && parts.speed.textContent !== speedText) parts.speed.textContent = speedText;
    if (parts.stateText && parts.stateText.textContent !== statusText) parts.stateText.textContent = statusText;
    if (parts.stateWrap) parts.stateWrap.classList.toggle("is-streaming", isStreaming);
  }
  function renderStatusFast(speed, stateStr) {
    renderStatus(speed, stateStr, { skipCtx: true });
  }
  // Shared context-window usage math for the sidebar gauge and the status bar,
  // so the two never disagree. Memoized: the live path keys on server tokens,
  // the estimate path on message count + tail lengths + pending images
  // (state.messages doesn't grow per delta, so streaming ticks hit the cache).
  // Estimates are approximate (chars/3); the next live prompt_eval_count
  // corrects drift.
  let _ctxCache = { key: "", value: null };
  function computeCtxUsage() {
    // Prefer the live server ctx from /api/ctx-stats; settings.num_ctx can differ
    // from how llama-server was launched.
    const capacity = Math.max(1, Number(state._ctxCapacity) || Number(state.settings.num_ctx) || 32768);
    // Prefer llama-server's reported prompt-token count from the latest turn: the
    // visible-bubble char count misses tool calls, tool results and intermediate
    // rounds, under-reporting tool-heavy work by 10x or more.
    const livePromptTokens = Number(state._lastMsgPromptTokens || 0);
    const sourceKey = String(state._ctxSource || "");
    if (livePromptTokens > 0) {
      const key = `live|${capacity}|${livePromptTokens}|${sourceKey}`;
      if (_ctxCache.key === key && _ctxCache.value) return _ctxCache.value;
      const used = Math.min(capacity, livePromptTokens);
      // _ctxSource is set by the poll / chat-load seed: "live" = real
      // prompt_eval_count from a completed turn, "tokenize" = exact tokenizer
      // count of the assembled prompt before the first turn has run.
      const source = state._ctxSource === "tokenize"
        ? "estimated next prompt"
        : state._ctxSource === "estimate"
          ? "estimated prompt usage"
          : "last measured prompt";
      const pct = Math.min(1, used / capacity);
      const out = { used, capacity, pct, source };
      _ctxCache = { key, value: out };
      return out;
    } else {
      const msgs = state.messages || [];
      const pending = (state.pendingImages || []).length;
      const n = msgs.length;
      let tail = "";
      for (let i = Math.max(0, n - 2); i < n; i++) {
        const m = msgs[i] || {};
        const c = m.content;
        tail += `|${m.role === "tool" ? "t" : "m"}${typeof c === "string" ? c.length : String(c || "").length}`;
      }
      const key = `est|${capacity}|${n}|${pending}|${tail}|${sourceKey}`;
      if (_ctxCache.key === key && _ctxCache.value) return _ctxCache.value;
      const systemPromptChars = 2500;
      const msgChars = msgs.reduce((a, m) => {
        const content = String(m.content || "");
        const multiplier = m.role === "tool" ? 1.5 : 1.0;
        return a + (content.length * multiplier);
      }, 0);
      const imageOverhead = pending * 500;
      const totalChars = systemPromptChars + msgChars + imageOverhead;
      const used = Math.min(capacity, Math.round(totalChars / 3.0));
      const source = "char-count estimate (no live data yet)";
      const pct = Math.min(1, used / capacity);
      const out = { used, capacity, pct, source };
      _ctxCache = { key, value: out };
      return out;
    }
  }

  function renderCtxGauge(pre) {
    const arc = $("#ctx-gauge-arc");
    const label = $("#ctx-gauge-label");
    if (!arc || !label) return;
    const { used, capacity, pct, source } = pre || computeCtxUsage();
    const circ = 2 * Math.PI * 13;
    const pctText = `${Math.round(pct * 100)}%`;
    const offset = (circ * (1 - pct)).toFixed(2);
    if (arc.getAttribute("stroke-dashoffset") !== offset) {
      arc.setAttribute("stroke-dasharray", circ.toFixed(2));
      arc.setAttribute("stroke-dashoffset", offset);
    }
    if (label.textContent !== pctText) label.textContent = pctText;
    const statusContext = document.querySelector(".status-ctx");
    if (statusContext) {
      const title = `${used.toLocaleString()} / ${capacity.toLocaleString()} tokens (${source})`;
      if (statusContext.title !== title) statusContext.title = title;
      statusContext.classList.toggle("is-warn", pct >= 0.7 && pct < 0.9);
      statusContext.classList.toggle("is-crit", pct >= 0.9);
      const value = statusContext.querySelector('[data-s="ctx-pct"]') || statusContext.querySelector("span");
      if (value && value.textContent !== pctText) value.textContent = pctText;
    }
    const gauge = $("#ctx-gauge");
    if (!gauge) return;
    gauge.classList.toggle("warn", pct >= 0.7 && pct < 0.9);
    gauge.classList.toggle("crit", pct >= 0.9);
    const aria = `Context memory: ${pctText} used`;
    if (gauge.getAttribute("aria-label") !== aria) gauge.setAttribute("aria-label", aria);
    // No native title: the hover card below carries the breakdown, and a
    // native tooltip would double up behind it.
    if (gauge.hasAttribute("title")) gauge.removeAttribute("title");
  }
  return {
    selectShellSession, refreshShellOutput, startShellPoll, stopShellPoll, shellSend, shellKill,
    surfaceShell, colorizeBackendLine, appendTerminalText, appendSandboxText, appendAgentLog,
    statusSpeedText, renderStatus, renderStatusFast, computeCtxUsage, renderCtxGauge,
  };
};
