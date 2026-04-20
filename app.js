/* ============================================================
   Accuretta frontend — single-file app logic.
   No framework. Vanilla JS. SSE for streaming.
   ============================================================ */
(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const api = (p, opts) => fetch(p, opts).then(r => r.json());

  // ---------- state ----------
  const state = {
    chats: { chats: {}, order: [] },
    chatId: null,
    messages: [],
    settings: {},
    workspace: { folders: [] },
    models: [],
    mode: "auto",          // auto | ide | agent
    view: "preview",       // preview | code
    versions: [],
    activeVersion: null,   // vid
    currentHtml: "",
    streaming: false,
    abortCtl: null,
    approvals: new Map(),
    mobileTab: "chat",
    pendingImages: [],  // [{ dataUrl, name }]
  };

  const app = $("#app");
  const isMobile = () => window.matchMedia("(max-width: 600px)").matches;

  // ---------- utilities ----------
  // simple toast system — bottom-right, auto-dismiss. keyed toasts replace each other.
  const _toasts = new Map();
  function toast(msg, kind = "info", ms = 3000, key = null) {
    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      document.body.appendChild(host);
    }
    if (key && _toasts.has(key)) {
      try { _toasts.get(key).remove(); } catch {}
      _toasts.delete(key);
    }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = msg;
    host.appendChild(el);
    if (key) _toasts.set(key, el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => { try { el.remove(); } catch {} if (key && _toasts.get(key) === el) _toasts.delete(key); }, 250);
    }, ms);
    return el;
  }

  const esc = (s) => (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  function scrollToBottom() {
    const s = $("#chat-scroll");
    s.scrollTop = s.scrollHeight;
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

  // ---------- friendly tool call labels ----------
  function shortPath(p) {
    if (!p) return "";
    const s = String(p).replace(/\\/g, "/");
    const parts = s.split("/").filter(Boolean);
    return parts.length <= 2 ? s : "…/" + parts.slice(-2).join("/");
  }
  function toolLabel(name, args) {
    args = args || {};
    switch (name) {
      case "list_directory": return `Looking in ${shortPath(args.path) || "folder"}…`;
      case "read_file":      return `Reading ${shortPath(args.path)}…`;
      case "write_file":     return `Writing ${shortPath(args.path)}…`;
      case "delete_file":    return `Deleting ${shortPath(args.path)}…`;
      case "run_powershell": return `Running command…`;
      case "open_program":   return `Opening ${args.name || args.path || "program"}…`;
      case "web_fetch":      return `Fetching ${args.url || "the web"}…`;
      default:               return `Running ${name || "tool"}…`;
    }
  }
  function toolResultLabel(name, res) {
    res = res || {};
    if (res.error) return `${name}: ${String(res.error).slice(0, 120)}`;
    switch (name) {
      case "list_directory": {
        const n = (res.entries || []).length;
        return `Found ${n} item${n === 1 ? "" : "s"}${res.path ? " in " + shortPath(res.path) : ""}`;
      }
      case "read_file":      return `Read ${shortPath(res.path)}${res.bytes != null ? ` (${res.bytes} bytes)` : ""}`;
      case "write_file":     return `Wrote ${shortPath(res.path)}`;
      case "delete_file":    return `Deleted ${shortPath(res.path)}`;
      case "run_powershell": {
        const out = (res.stdout || "").trim();
        const first = out.split(/\r?\n/)[0] || "(no output)";
        return `Done · ${first.slice(0, 120)}`;
      }
      case "open_program":   return `Opened ${res.name || ""}`;
      case "web_fetch":      return `Fetched ${shortPath(res.url)}`;
      default:               return `${name} complete`;
    }
  }

  // ---------- markdown-lite for chat bubbles ----------
  // Preserves code fences, ignores tool_call tags (rendered as tool cards separately).
  function renderMarkdown(text) {
    if (!text) return "";
    // strip tool_call blocks from visible markdown (shown as cards)
    text = text.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, "");
    text = text.replace(/```tool_call[\s\S]*?```/gi, "");

    // extract code fences
    const fences = [];
    text = text.replace(/```(\w+)?\n?([\s\S]*?)```/g, (_m, lang, code) => {
      fences.push({ lang: lang || "", code });
      return `\x00F${fences.length - 1}\x00`;
    });

    let out = esc(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\n\n/g, "</p><p>")
      .replace(/\n/g, "<br>");
    out = "<p>" + out + "</p>";
    out = out.replace(/\x00F(\d+)\x00/g, (_, i) => {
      const { lang, code } = fences[+i];
      return `<pre data-lang="${esc(lang)}"><code>${esc(code)}</code></pre>`;
    });
    return out;
  }

  // very small HTML syntax highlighter for code view
  function highlightHTML(src) {
    const s = esc(src);
    return s
      .replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="tok-comment">$1</span>')
      .replace(/(&lt;\/?)([a-zA-Z][\w-]*)/g, '$1<span class="tok-tag">$2</span>')
      .replace(/(\s)([a-zA-Z][\w-]*)=(&quot;[^&]*?&quot;)/g, '$1<span class="tok-attr">$2</span>=<span class="tok-str">$3</span>');
  }

  // ---------- bootstrap ----------
  async function boot() {
    // on-device hint
    if (isMobile()) document.body.classList.add("is-mobile");

    await Promise.all([
      loadSettings(),
      loadWorkspace(),
      loadChats(),
      loadModels(),
    ]);

    // pick or create current chat
    if (state.chats.order.length) {
      selectChat(state.chats.order[0]);
    } else {
      await newChat();
    }

    applyTheme(state.settings.theme === "dark");
    renderStatus();
    renderModelPill();
    renderChatList();
    renderWorkspace();

    wireEvents();
    subscribeSSE();
  }

  // ---------- data loading ----------
  async function loadSettings() {
    state.settings = await api("/api/settings");
  }
  async function saveSettings(update) {
    const prevModel = state.settings.model;
    state.settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    });
    // model changed mid-stream → abort so next send uses fresh model cleanly
    if (update.model && update.model !== prevModel && state.streaming) {
      stopStreaming();
    }
    renderStatus();
    renderModelPill();
  }
  async function loadWorkspace() {
    state.workspace = await api("/api/workspace");
  }
  async function loadChats() {
    state.chats = await api("/api/chats");
  }
  async function loadModels() {
    try {
      const r = await api("/api/models");
      state.models = (r.models || []).map(m => m.name || m.model).filter(Boolean);
      state.modelsError = r.error || (state.models.length ? "" : "ollama returned no models — run: ollama pull qwen3:8b");
    } catch (e) {
      state.models = [];
      state.modelsError = "bridge unreachable: " + (e.message || e);
    }
  }

  // ---------- chat ----------
  async function newChat() {
    const c = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "new session" }),
    });
    await loadChats();
    selectChat(c.id);
  }

  function selectChat(id) {
    state.chatId = id;
    const chat = state.chats.chats[id];
    state.messages = chat ? (chat.messages || []).slice() : [];
    $("#chat-title").textContent = chat ? chat.title : "new session";
    renderMessages();
    loadVersions();
    renderChatList();
    if (isMobile()) {
      state.mobileTab = "chat";
      applyMobileTab();
    }
  }

  async function deleteChat(id) {
    if (!confirm("Delete this session and its versions?")) return;
    await fetch(`/api/chats/${id}`, { method: "DELETE" });
    await loadChats();
    if (state.chatId === id) {
      if (state.chats.order.length) selectChat(state.chats.order[0]);
      else await newChat();
    } else {
      renderChatList();
    }
  }

  function renderChatList() {
    const wrap = $("#chatlist");
    wrap.innerHTML = "";
    for (const id of state.chats.order) {
      const c = state.chats.chats[id];
      if (!c) continue;
      const row = document.createElement("div");
      row.className = "chatrow" + (id === state.chatId ? " active" : "");
      row.innerHTML = `
        <i class="ph ph-chat-circle"></i>
        <span class="t">${esc(c.title)}</span>
        <span class="d">${relTime(c.updated)}</span>
        <button class="del" title="Delete"><i class="ph ph-trash"></i></button>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".del")) return;
        selectChat(id);
      });
      row.querySelector(".del").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(id);
      });
      wrap.appendChild(row);
    }
  }

  function renderMessages() {
    const inner = $("#chat-inner");
    inner.innerHTML = "";
    if (!state.messages.length) {
      inner.innerHTML = `
        <div class="bubble-row">
          <div class="avatar"><i class="ph-bold ph-sparkle" style="font-size:12px"></i></div>
          <div class="bubble-col">
            <div class="bubble agent">Welcome to Accuretta. What would you like to do today?</div>
          </div>
        </div>`;
      scrollToBottom();
      return;
    }
    for (const m of state.messages) {
      inner.appendChild(renderBubble(m));
    }
    scrollToBottom();
  }

  function renderBubble(m) {
    const row = document.createElement("div");
    row.className = "bubble-row " + (m.role === "user" ? "user" : "");
    const avatar = m.role === "user"
      ? `<div class="avatar user">me</div>`
      : `<div class="avatar"><i class="ph-bold ph-sparkle" style="font-size:12px"></i></div>`;

    let visible = m.content || "";
    let thoughtChip = "";
    if (m.role === "assistant") {
      const { thinking, content } = splitThinking(visible);
      visible = content;
      if (thinking) {
        thoughtChip = `<div class="think-line done" data-thinking="${esc(thinking)}"><i class="ph ph-check"></i><span>Thought for a moment</span></div>`;
      }
    }

    row.innerHTML = `
      ${avatar}
      <div class="bubble-col">
        ${thoughtChip}
        <div class="bubble ${m.role === "user" ? "user" : "agent"}">${renderMarkdown(visible)}</div>
        <div class="bubble-meta">${m.role === "user" ? "you" : (state.settings.model || "agent")} · ${relTime(m.t)}</div>
      </div>`;
    return row;
  }

  // ---------- image attachments ----------
  function renderImageTray() {
    const tray = $("#image-tray");
    if (!tray) return;
    tray.innerHTML = "";
    if (!state.pendingImages.length) { tray.classList.add("hidden"); return; }
    tray.classList.remove("hidden");
    state.pendingImages.forEach((img, i) => {
      const div = document.createElement("div");
      div.className = "thumb";
      div.innerHTML = `<img src="${img.dataUrl}" alt="${esc(img.name || "image")}"><button class="rm" title="Remove"><i class="ph ph-x"></i></button>`;
      div.querySelector(".rm").addEventListener("click", () => {
        state.pendingImages.splice(i, 1);
        renderImageTray();
      });
      tray.appendChild(div);
    });
  }
  function fileToDataURL(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }
  async function addImageFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      try {
        const dataUrl = await fileToDataURL(f);
        state.pendingImages.push({ dataUrl, name: f.name });
      } catch (e) { console.warn("read failed", e); }
    }
    renderImageTray();
  }

  // ---------- send / stream ----------
  async function send() {
    if (state.streaming) return;
    const ta = $("#composer-input");
    const text = ta.value.trim();
    const images = state.pendingImages.slice();
    if (!text && !images.length) return;
    if (!state.settings.model) {
      alert("Pick a model in Settings first.");
      openSettings();
      return;
    }
    ta.value = "";
    autoResize(ta);
    state.pendingImages = [];
    renderImageTray();

    // show the image count in the user bubble so they know what got sent
    const bubbleText = images.length
      ? (text ? `${text}\n\n📎 ${images.length} image${images.length > 1 ? "s" : ""} attached` : `📎 ${images.length} image${images.length > 1 ? "s" : ""} attached`)
      : text;
    const userMsg = { role: "user", content: bubbleText, t: Math.floor(Date.now() / 1000) };
    state.messages.push(userMsg);
    $("#chat-inner").appendChild(renderBubble(userMsg));
    scrollToBottom();
    renderCtxGauge();

    // placeholder agent bubble
    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    agentRow.innerHTML = `
      <div class="avatar"><i class="ph-bold ph-sparkle" style="font-size:12px"></i></div>
      <div class="bubble-col">
        <div class="think-line" data-label="Thinking"><i class="ph ph-brain"></i><span class="shimmer">Thinking…</span></div>
        <div class="tool-stack" id="tool-stack"></div>
        <div class="bubble agent hidden" id="stream-bubble"></div>
        <div class="bubble-meta">${esc(state.settings.model)} · streaming</div>
      </div>`;
    $("#chat-inner").appendChild(agentRow);
    scrollToBottom();

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);

    try {
      await streamChat(text, agentRow, state.abortCtl.signal, images);
    } catch (e) {
      const b = agentRow.querySelector("#stream-bubble") || agentRow.querySelector(".bubble");
      if (b) {
        if (e.name === "AbortError") b.innerHTML += `<div style="color: var(--fg-faint); font-size:11px; margin-top:6px;">— stopped</div>`;
        else b.innerHTML = `<span style="color: var(--danger)">error: ${esc(e.message)}</span>`;
      }
    } finally {
      state.streaming = false;
      state.abortCtl = null;
      setStreamingUI(false);
      await loadChats();
      renderChatList();
    }
  }

  function setStreamingUI(on) {
    $("#btn-send").classList.toggle("hidden", on);
    $("#btn-stop").classList.toggle("hidden", !on);
    $("#composer-input").disabled = false; // always allow typing next message
  }

  function stopStreaming() {
    if (state.abortCtl) {
      try { state.abortCtl.abort(); } catch {}
    }
  }

  async function streamChat(text, agentRow, signal, images) {
    const bubble = agentRow.querySelector("#stream-bubble");
    const toolStack = agentRow.querySelector("#tool-stack");
    let buf = "";
    const toolCards = new Map();

    // heartbeat: if no delta arrives, rotate through varied status lines
    // so the user sees the model is alive (not frozen). Mix of plain progress
    // and dry one-liners. Never repeats until the pool is exhausted.
    const idlePool = [
      "still working", "thinking it through", "crunching tokens",
      "wrangling the model", "hitting the monitor with a hammer",
      "politely asking the weights", "re-reading the prompt",
      "weighing options", "arguing with itself", "lining up the next move",
      "checking its own math", "rehearsing the reply", "taking the scenic route",
      "compiling thoughts", "sharpening the pencil", "consulting the rubber duck",
      "shaking the dice", "yelling at the GPU",
    ];
    let pool = idlePool.slice();
    let currentIdle = "still working";
    let lastActivity = Date.now();
    let lastRotate = 0;
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
      // rotate phrase every 6 seconds of continuous idleness
      if (Date.now() - lastRotate > 6000) {
        if (!pool.length) pool = idlePool.slice();
        currentIdle = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
        lastRotate = Date.now();
      }
      span.textContent = `${currentIdle}… ${total}s`;
    }, 1000);

    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: state.chatId,
        message: text,
        mode: state.mode,
        images: (images || []).map(x => x.dataUrl),
      }),
      signal,
    });
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
          handleEvent(evt, { bubble, toolStack, toolCards, row: agentRow, getBuf: () => buf, setBuf: v => buf = v });
          markActivity();
          if (evt.type === "chat_end") { ended = true; break; }
        }
      }
    } finally {
      try { await reader.cancel(); } catch {}
      clearInterval(heartbeat);
      if (agentRow) updateThinkLine(agentRow, false);
      // safety net: if the model ran tools or thought for a while but ended
      // without a visible answer, surface what we have so the user isn't
      // staring at nothing. Promote the tail of thinking if it's substantive.
      if (bubble && bubble.classList.contains("hidden")) {
        const { thinking } = splitThinking(buf);
        const hadTools = toolStack && toolStack.children.length > 0;
        bubble.classList.remove("hidden");
        bubble.classList.add("quiet");
        if (thinking && thinking.length > 40) {
          const tail = thinking.length > 900 ? "…" + thinking.slice(-900) : thinking;
          bubble.innerHTML = `<div style="margin-bottom:6px;opacity:0.7;font-size:11px;">(model spent its whole budget thinking — here's the tail)</div><pre style="white-space:pre-wrap;font-family:inherit;margin:0;">${esc(tail)}</pre>`;
        } else {
          bubble.textContent = hadTools
            ? "(model ended turn without a reply — ask it what it found, or try again)"
            : "(no response — try raising Max reply tokens in Settings)";
        }
      }
    }
  }

  // strip reasoning wrappers from several model families so the chat bubble
  // only shows the final answer. Accumulate thinking text into the think line.
  function splitThinking(buf) {
    // tags observed: <think>, <thinking>, <reasoning>, and <|thinking|>…<|/thinking|>.
    // many local models (Qwen/DeepSeek/Nemotron) emit bare </think> with no opening tag,
    // sometimes multiple times between tool rounds. rule: everything up to the LAST closing
    // reasoning tag is thinking; everything after is the visible answer.
    const closeRe = /<\/(?:think|thinking|reasoning)>|<\|\/thinking\|>/gi;
    let lastClose = -1;
    let m;
    while ((m = closeRe.exec(buf)) !== null) lastClose = m.index + m[0].length;

    let thinking = "";
    let content = "";
    if (lastClose >= 0) {
      thinking = buf.slice(0, lastClose);
      content = buf.slice(lastClose);
    } else {
      // no closing tag yet — if an opening tag is present, everything from it is in-flight thinking
      const openIdx = buf.search(/<(?:think|thinking|reasoning)>|<\|thinking\|>/i);
      if (openIdx >= 0) {
        content = buf.slice(0, openIdx);
        thinking = buf.slice(openIdx);
      } else {
        content = buf;
      }
    }
    const stripTags = /<\/?(?:think|thinking|reasoning)>|<\|\/?thinking\|>/gi;
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
    ];
    for (const re of junk) { thinking = thinking.replace(re, ""); content = content.replace(re, ""); }
    return { thinking: thinking.trim(), content };
  }
  function updateThinkLine(row, running, label) {
    const line = row.querySelector(".think-line");
    if (!line) return;
    const span = line.querySelector("span");
    const icon = line.querySelector("i");
    if (!running) {
      line.classList.add("done");
      span.classList.remove("shimmer");
      span.textContent = label || "Thought for a moment";
      icon.className = "ph ph-check";
      return;
    }
    if (label) span.textContent = label;
  }
  function handleEvent(evt, ctx) {
    const { bubble, toolStack, toolCards, row } = ctx;
    if (evt.type === "delta") {
      const newBuf = ctx.getBuf() + evt.content;
      ctx.setBuf(newBuf);
      const { thinking, content } = splitThinking(newBuf);
      if (thinking && ctx.row) {
        // first few words of current thinking snippet, shimmering
        const preview = thinking.split(/\s+/).slice(-12).join(" ");
        updateThinkLine(ctx.row, true, preview || "Thinking…");
      }
      if (content.trim()) {
        bubble.classList.remove("hidden");
        bubble.innerHTML = renderMarkdown(content);
        if (ctx.row) updateThinkLine(ctx.row, false);
      }
      scrollToBottom();
    } else if (evt.type === "tool_start") {
      const card = document.createElement("div");
      card.className = "tool-line running";
      card.innerHTML = `<i class="ph ph-circle-notch"></i><span class="shimmer">${esc(toolLabel(evt.name, evt.arguments))}</span>`;
      card.dataset.name = evt.name || "";
      toolStack.appendChild(card);
      scrollToBottom();
    } else if (evt.type === "tool_result") {
      const cards = Array.from(toolStack.querySelectorAll(".tool-line.running"));
      const card = cards.reverse().find(c => c.dataset.name === evt.name);
      if (card) {
        const isErr = evt.result && evt.result.error;
        card.classList.remove("running");
        card.classList.add(isErr ? "err" : "done");
        const icon = isErr ? "ph-x-circle" : "ph-check";
        const label = toolResultLabel(evt.name, evt.result);
        card.innerHTML = `<i class="ph ${icon}"></i><span>${esc(label)}</span>`;
      }
    } else if (evt.type === "version_saved") {
      state.versions.push(evt.version);
      renderVersions();
      setActiveVersion(evt.version.id);
    } else if (evt.type === "stats") {
      const tok = evt.eval_count;
      const dur = (evt.eval_duration || 0) / 1e9;
      const tps = dur > 0 ? (tok / dur).toFixed(1) : "—";
      const meta = bubble.parentElement.querySelector(".bubble-meta");
      if (meta) meta.textContent = `${state.settings.model} · ${tok} tok · ${tps} tok/s`;
    } else if (evt.type === "final") {
      state.messages.push({
        role: "assistant",
        content: evt.message.content || "",
        t: Math.floor(Date.now() / 1000),
      });
      renderCtxGauge();
    } else if (evt.type === "error") {
      bubble.innerHTML = `<span style="color: var(--danger)">error: ${esc(evt.error)}</span>`;
    }
  }

  // ---------- versions / preview ----------
  async function loadVersions() {
    if (!state.chatId) return;
    try {
      const r = await api(`/api/versions/${state.chatId}`);
      state.versions = r.versions || [];
    } catch { state.versions = []; }
    renderVersions();
    if (state.versions.length) {
      setActiveVersion(state.versions[state.versions.length - 1].id);
    } else {
      clearPreview();
    }
  }

  function renderVersions() {
    const bar = $("#version-bar");
    bar.innerHTML = "";
    if (!state.versions.length) {
      bar.innerHTML = `<span id="versions-empty" style="color:var(--fg-faint)">no versions yet</span><span class="spacer"></span>`;
      return;
    }
    for (const v of state.versions) {
      const chip = document.createElement("button");
      chip.className = "version-chip" + (v.id === state.activeVersion ? " active" : "");
      chip.innerHTML = `<span class="n">v${String(v.n).padStart(2, "0")}</span>${v.label ? `<span style="opacity:.6">· ${esc(v.label).slice(0, 32)}</span>` : ""}`;
      chip.title = `${v.id} · ${humanBytes(v.bytes)} · ${relTime(v.t)}`;
      chip.addEventListener("click", () => setActiveVersion(v.id));
      bar.appendChild(chip);
    }
    const spacer = document.createElement("span");
    spacer.className = "spacer";
    bar.appendChild(spacer);
  }

  async function setActiveVersion(vid) {
    state.activeVersion = vid;
    const resp = await fetch(`/api/versions/${state.chatId}/${vid}`);
    const html = await resp.text();
    state.currentHtml = html;
    const v = state.versions.find(x => x.id === vid);
    $("#preview-url").textContent = vid;
    $("#preview-meta").textContent = v ? `v${String(v.n).padStart(2, "0")} · ${relTime(v.t)}` : "—";
    $("#preview-size").textContent = humanBytes((html || "").length);
    renderPreview();
    renderVersions();
    // auto-open preview pane if collapsed
    if (app.classList.contains("preview-collapsed") && !isMobile()) {
      app.classList.remove("preview-collapsed");
    }
  }

  function clearPreview() {
    state.currentHtml = "";
    state.activeVersion = null;
    $("#preview-url").textContent = "—";
    $("#preview-meta").textContent = "—";
    $("#preview-size").textContent = "—";
    $("#preview-frame").classList.add("hidden");
    $("#code-view").classList.add("hidden");
    $("#preview-empty").classList.remove("hidden");
    renderVersions();
  }

  function injectCspIfNeeded(html) {
    if (state.settings.allow_web_preview !== false) return html;
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'self' data: blob:; style-src 'unsafe-inline' 'self' data:; script-src 'unsafe-inline' 'self' data:; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self';">`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + csp);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + csp + "</head>");
    return csp + html;
  }

  function renderPreview() {
    if (!state.currentHtml) { clearPreview(); return; }
    $("#preview-empty").classList.add("hidden");
    if (state.view === "preview") {
      $("#code-view").classList.add("hidden");
      // recreate iframe each time we switch back — srcdoc on a hidden iframe
      // can end up blank in some browsers. Cheap and always correct.
      const old = $("#preview-frame");
      const fresh = document.createElement("iframe");
      fresh.id = "preview-frame";
      fresh.className = "preview-frame";
      fresh.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups");
      fresh.srcdoc = injectCspIfNeeded(state.currentHtml);
      old.replaceWith(fresh);
    } else {
      $("#preview-frame").classList.add("hidden");
      const c = $("#code-view");
      c.classList.remove("hidden");
      c.innerHTML = highlightHTML(state.currentHtml);
    }
  }

  // ---------- workspace ----------
  // ---- workspace file tree ----
  const FILE_ICON = {
    // scripts
    js: "ph-file-js", jsx: "ph-file-js", ts: "ph-file-ts", tsx: "ph-file-ts",
    py: "ph-file-py", rb: "ph-file-code", go: "ph-file-code", rs: "ph-file-rs",
    java: "ph-file-code", c: "ph-file-c", cpp: "ph-file-cpp", h: "ph-file-c",
    cs: "ph-file-cs", php: "ph-file-code", sh: "ph-terminal-window",
    ps1: "ph-terminal-window", bat: "ph-terminal-window", lua: "ph-file-code",
    // web
    html: "ph-file-html", htm: "ph-file-html", css: "ph-file-css",
    scss: "ph-file-css", sass: "ph-file-css", less: "ph-file-css",
    vue: "ph-file-vue", svelte: "ph-file-code",
    // data
    json: "ph-brackets-curly", yaml: "ph-brackets-angle", yml: "ph-brackets-angle",
    xml: "ph-brackets-angle", toml: "ph-brackets-angle", ini: "ph-brackets-angle",
    csv: "ph-table", tsv: "ph-table", sql: "ph-database", db: "ph-database",
    sqlite: "ph-database",
    // docs
    md: "ph-file-md", mdx: "ph-file-md", txt: "ph-file-text", rtf: "ph-file-text",
    pdf: "ph-file-pdf", doc: "ph-file-doc", docx: "ph-file-doc",
    xls: "ph-file-xls", xlsx: "ph-file-xls", ppt: "ph-file-ppt", pptx: "ph-file-ppt",
    // media
    png: "ph-file-image", jpg: "ph-file-image", jpeg: "ph-file-image",
    gif: "ph-file-image", webp: "ph-file-image", svg: "ph-file-svg",
    ico: "ph-file-image", bmp: "ph-file-image", avif: "ph-file-image",
    mp3: "ph-file-audio", wav: "ph-file-audio", flac: "ph-file-audio",
    ogg: "ph-file-audio", m4a: "ph-file-audio",
    mp4: "ph-file-video", mov: "ph-file-video", mkv: "ph-file-video",
    webm: "ph-file-video", avi: "ph-file-video",
    // archives
    zip: "ph-file-zip", rar: "ph-file-zip", "7z": "ph-file-zip",
    tar: "ph-file-zip", gz: "ph-file-zip", bz2: "ph-file-zip",
    // config
    env: "ph-key", lock: "ph-lock-simple", log: "ph-article",
    gitignore: "ph-git-branch", dockerfile: "ph-cube",
  };
  function fileIconFor(name, ext) {
    const lower = (name || "").toLowerCase();
    if (lower === "dockerfile") return "ph-cube";
    if (lower === "makefile") return "ph-hammer";
    if (lower === "license" || lower === "license.md") return "ph-scales";
    if (lower === "readme" || lower === "readme.md") return "ph-book-open-text";
    if (lower.startsWith(".git")) return "ph-git-branch";
    return FILE_ICON[ext] || "ph-file";
  }
  function folderLeafName(path) {
    return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  }
  async function fetchFolderListing(path) {
    try {
      const r = await api(`/api/list-folder?path=${encodeURIComponent(path)}`);
      if (r.error) throw new Error(r.error);
      return r.entries || [];
    } catch (e) {
      return { _error: e.message || String(e) };
    }
  }
  function renderTreeNode(entry, depth) {
    const node = document.createElement("div");
    node.className = entry.is_dir ? "tree-node tree-dir" : "tree-node tree-file";
    node.style.setProperty("--depth", depth);
    const icon = entry.is_dir ? "ph-folder" : fileIconFor(entry.name, entry.ext);
    const chev = entry.is_dir ? `<i class="ph ph-caret-right tree-chev"></i>` : `<span class="tree-chev-spacer"></span>`;
    node.innerHTML = `
      <div class="tree-row" title="${esc(entry.path)}">
        ${chev}
        <i class="ph ${icon} tree-icon"></i>
        <span class="tree-name">${esc(entry.name)}</span>
      </div>
      ${entry.is_dir ? `<div class="tree-children" hidden></div>` : ""}`;
    if (entry.is_dir) {
      const rowEl = node.querySelector(".tree-row");
      const kids = node.querySelector(".tree-children");
      let loaded = false;
      rowEl.addEventListener("click", async (e) => {
        e.stopPropagation();
        const expanded = !kids.hasAttribute("hidden");
        if (expanded) {
          kids.setAttribute("hidden", "");
          node.classList.remove("open");
          return;
        }
        node.classList.add("open");
        kids.removeAttribute("hidden");
        if (!loaded) {
          kids.innerHTML = `<div class="tree-loading" style="--depth:${depth + 1}">loading…</div>`;
          const entries = await fetchFolderListing(entry.path);
          kids.innerHTML = "";
          if (entries._error) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">empty</div>`;
          } else {
            for (const child of entries) kids.appendChild(renderTreeNode(child, depth + 1));
          }
          loaded = true;
        }
      });
    }
    return node;
  }

  function renderWorkspace() {
    const wrap = $("#ws-list");
    wrap.innerHTML = "";
    if (!state.workspace.folders.length) {
      wrap.innerHTML = `<div style="padding: 10px 12px; font-size: 11px; color: var(--fg-faint);">no folders. add one to let the agent read/write files.</div>`;
      return;
    }
    for (const f of state.workspace.folders) {
      const wrapper = document.createElement("div");
      wrapper.className = "ws-root";

      const header = document.createElement("div");
      header.className = "ws-folder";
      header.innerHTML = `
        <i class="ph ph-caret-right ws-chev"></i>
        <i class="ph ph-folder"></i>
        <span class="path" title="${esc(f)}">${esc(folderLeafName(f))}</span>
        <button class="rm" title="Remove"><i class="ph ph-x"></i></button>`;
      wrapper.appendChild(header);

      const tree = document.createElement("div");
      tree.className = "ws-tree";
      tree.hidden = true;
      wrapper.appendChild(tree);

      let loaded = false;
      header.querySelector(".rm").addEventListener("click", async (e) => {
        e.stopPropagation();
        const next = state.workspace.folders.filter(x => x !== f);
        await api("/api/workspace", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folders: next }),
        });
        state.workspace.folders = next;
        renderWorkspace();
      });
      header.addEventListener("click", async () => {
        const wasOpen = wrapper.classList.toggle("open");
        tree.hidden = !wasOpen;
        if (wasOpen && !loaded) {
          tree.innerHTML = `<div class="tree-loading" style="--depth:1">loading…</div>`;
          const entries = await fetchFolderListing(f);
          tree.innerHTML = "";
          if (entries._error) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">empty</div>`;
          } else {
            for (const child of entries) tree.appendChild(renderTreeNode(child, 1));
          }
          loaded = true;
        }
      });

      wrap.appendChild(wrapper);
    }
  }

  async function addWorkspaceFolder() {
    const inp = $("#ws-input");
    const v = inp.value.trim();
    if (!v) return;
    const next = Array.from(new Set([...state.workspace.folders, v]));
    const r = await api("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders: next }),
    });
    state.workspace = r;
    inp.value = "";
    $("#ws-add").classList.add("hidden");
    renderWorkspace();
  }

  // ---------- approvals ----------
  function renderApprovals() {
    const stack = $("#approval-stack");
    stack.innerHTML = "";
    for (const a of state.approvals.values()) {
      const card = document.createElement("div");
      card.className = "approval";
      const details = a.details || {};
      const tag = details.kind || "command";
      card.innerHTML = `
        <div class="head">
          <i class="ph-bold ph-shield-warning"></i>
          <span class="t">${esc(a.title)}</span>
          <span class="tag">${esc(tag)}</span>
        </div>
        <div class="cmd">${esc(a.command)}</div>
        <div class="actions">
          <button class="btn danger" data-act="deny">Deny</button>
          <button class="btn accent" data-act="approve">Approve</button>
        </div>`;
      card.querySelector('[data-act="approve"]').addEventListener("click", () => decideApproval(a.id, "approve"));
      card.querySelector('[data-act="deny"]').addEventListener("click", () => decideApproval(a.id, "deny"));
      stack.appendChild(card);
    }
  }

  async function decideApproval(id, decision) {
    state.approvals.delete(id);
    renderApprovals();
    await fetch("/api/approvals/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, decision }),
    });
  }

  async function loadApprovals() {
    const r = await api("/api/approvals");
    state.approvals.clear();
    for (const a of r.pending || []) state.approvals.set(a.id, a);
    renderApprovals();
  }

  // ---------- SSE ----------
  function subscribeSSE() {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); } catch { return; }
      if (evt.type === "approval:new") {
        state.approvals.set(evt.approval.id, evt.approval);
        renderApprovals();
      } else if (evt.type === "approval:decided") {
        state.approvals.delete(evt.id);
        renderApprovals();
      } else if (evt.type === "settings:update") {
        loadSettings().then(renderStatus).then(renderModelPill);
      } else if (evt.type === "workspace:update") {
        loadWorkspace().then(renderWorkspace);
      } else if (evt.type === "chat:rename") {
        const c = state.chats && state.chats.chats && state.chats.chats[evt.chat_id];
        if (c) {
          c.title = evt.title;
          renderChatList();
        }
      } else if (evt.type === "desktop:panic") {
        if (evt.on) toast("desktop automation PANICKED — all actions blocked", "warn", 6000, "desktop-panic");
        else toast("desktop automation resumed", "ok", 2000, "desktop-panic");
        refreshDesktopStatus();
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(subscribeSSE, 3000);
    };
  }

  // ---------- settings drawer ----------
  async function openSettings() {
    $("#drawer-scrim").classList.add("open");
    $("#settings-drawer").classList.add("open");
    await loadModels();
    populateSettingsForm();
    const current = $("#set-model").value;
    if (current) loadRecommended(current);
    loadSystemContext();
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
      alert(`save failed: ${e.message || e}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function rescanSystemContext() {
    const btn = $("#btn-sysctx-rescan");
    const ta = $("#set-sysctx");
    if (btn) btn.disabled = true;
    if (ta) ta.value = "scanning…";
    try {
      const r = await api("/api/system-context/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (ta) ta.value = r.md || "";
    } catch (e) {
      if (ta) ta.value = `(rescan failed: ${e.message || e})`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  function closeSettings() {
    $("#drawer-scrim").classList.remove("open");
    $("#settings-drawer").classList.remove("open");
  }
  function populateSettingsForm() {
    const s = state.settings;
    const fill = (id, v) => { const el = $(id); if (el) el.value = v ?? ""; };
    // model selects
    const modelSel = $("#set-model"); modelSel.innerHTML = "";
    for (const name of state.models) {
      const o = document.createElement("option");
      o.value = name; o.textContent = name;
      if (name === s.model) o.selected = true;
      modelSel.appendChild(o);
    }
    if (!state.models.length) {
      const msg = state.modelsError || "no models — run: ollama pull qwen3:8b";
      modelSel.innerHTML = `<option value="">(${msg})</option>`;
    }

    const visionSel = $("#set-vision"); visionSel.innerHTML = "";
    const emptyOpt = document.createElement("option");
    emptyOpt.value = ""; emptyOpt.textContent = "(none)";
    visionSel.appendChild(emptyOpt);
    for (const name of state.models) {
      const o = document.createElement("option");
      o.value = name; o.textContent = name;
      if (name === s.vision_model) o.selected = true;
      visionSel.appendChild(o);
    }

    fill("#set-ctx", s.num_ctx);
    fill("#set-gpu", s.num_gpu);
    fill("#set-batch", s.num_batch);
    fill("#set-thread", s.num_thread);
    fill("#set-predict", s.num_predict);
    fill("#set-keep", s.keep_alive);
    fill("#set-temp", s.temperature);
    fill("#set-topp", s.top_p);
    $("#sw-dark").classList.toggle("on", s.theme === "dark");
    $("#sw-web").classList.toggle("on", s.allow_web_preview !== false);

    // desktop automation
    $("#sw-desktop-enabled")?.classList.toggle("on", !!s.desktop_enabled);
    const al = $("#set-desktop-allowlist");
    if (al) al.value = (s.desktop_app_allowlist || []).join("\n");
    fill("#set-desktop-rate", s.desktop_max_actions_per_minute || 30);
    refreshDesktopStatus();
  }

  async function refreshDesktopStatus() {
    try {
      const r = await api("/api/desktop/status");
      const badge = $("#desktop-deps-badge");
      if (badge) {
        const missing = [];
        if (!r.have_pyautogui) missing.push("pyautogui");
        if (!r.have_pil) missing.push("Pillow");
        if (!r.have_pygetwindow) missing.push("pygetwindow");
        badge.textContent = missing.length
          ? `missing: pip install ${missing.join(" ")}`
          : "all libs installed";
        badge.style.color = missing.length ? "var(--danger)" : "var(--mint-3)";
      }
      const ps = $("#desktop-panic-state");
      if (ps) {
        ps.textContent = r.panic ? "PANIC — all actions blocked" : "ready";
        ps.style.color = r.panic ? "var(--danger)" : "var(--fg-faint)";
      }
    } catch {}
  }
  async function collectAndSaveSettings() {
    const n = (id) => Number($(id).value);
    const payload = {
      model: $("#set-model").value,
      vision_model: $("#set-vision").value,
      num_ctx: n("#set-ctx") || 8192,
      num_gpu: n("#set-gpu"),
      num_batch: n("#set-batch") || 512,
      num_thread: n("#set-thread"),
      num_predict: n("#set-predict"),
      keep_alive: $("#set-keep").value || "30m",
      temperature: n("#set-temp"),
      top_p: n("#set-topp"),
      theme: $("#sw-dark").classList.contains("on") ? "dark" : "light",
      allow_web_preview: $("#sw-web").classList.contains("on"),
      desktop_enabled: $("#sw-desktop-enabled")?.classList.contains("on") || false,
      desktop_app_allowlist: ($("#set-desktop-allowlist")?.value || "")
        .split("\n").map(x => x.trim()).filter(Boolean),
      desktop_max_actions_per_minute: Math.max(1, Math.min(300, n("#set-desktop-rate") || 30)),
    };
    await saveSettings(payload);
    applyTheme(payload.theme === "dark");
    closeSettings();
  }

  let _lastRec = null;
  async function loadRecommended(model) {
    const hint = $("#rec-hint");
    const text = $("#rec-hint-text");
    if (!model) { hint.style.display = "none"; _lastRec = null; return; }
    try {
      const r = await api(`/api/model-info/${encodeURIComponent(model)}`);
      if (r.error) { hint.style.display = "none"; _lastRec = null; return; }
      _lastRec = r;
      const rec = r.recommended || {};
      const gpuLbl = rec.num_gpu === 0 ? "auto" : rec.num_gpu;
      text.innerHTML = `detected: <code>${r.size_b || "?"}B</code> · <code>${r.quant || "?"}</code> · ~<code>${r.est_weights_gb || "?"}GB</code> weights · native ctx <code>${r.native_ctx}</code>. recommended → ctx <code>${rec.num_ctx}</code>, gpu <code>${gpuLbl}</code>, batch <code>${rec.num_batch}</code>.`;
      hint.style.display = "";
    } catch {
      hint.style.display = "none"; _lastRec = null;
    }
  }
  function applyRecommended() {
    if (!_lastRec) return;
    const rec = _lastRec.recommended;
    $("#set-ctx").value = rec.num_ctx;
    $("#set-gpu").value = rec.num_gpu;
    $("#set-batch").value = rec.num_batch;
    $("#set-thread").value = rec.num_thread;
    $("#set-predict").value = rec.num_predict;
    $("#set-keep").value = rec.keep_alive;
    $("#set-temp").value = rec.temperature;
    $("#set-topp").value = rec.top_p;
  }
  // auto-apply: whenever the model changes (settings dropdown or model pill),
  // pull recommended, merge into settings, save. Aim is plug-and-play.
  async function autoTuneForModel(model, opts = {}) {
    if (!model) return;
    try {
      const r = await api(`/api/model-info/${encodeURIComponent(model)}`);
      if (!r || r.error || !r.recommended) return;
      const rec = r.recommended;
      _lastRec = r;
      const payload = {
        model,
        num_ctx: rec.num_ctx,
        num_gpu: rec.num_gpu,
        num_batch: rec.num_batch,
        num_thread: rec.num_thread,
        num_predict: rec.num_predict,
        keep_alive: rec.keep_alive,
        temperature: rec.temperature,
        top_p: rec.top_p,
      };
      await saveSettings(payload);
      if (opts.silent !== true) {
        toast(`tuned for ${model} — ctx ${rec.num_ctx}, batch ${rec.num_batch}`, "ok");
      }
      // warm up ollama so the first reply doesn't freeze
      prewarmModel(model);
    } catch {}
  }
  async function prewarmModel(model) {
    if (!model) return;
    toast(`loading ${model.split("/").pop()}…`, "info", 30000, "prewarm");
    try {
      await api("/api/prewarm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }) });
      toast(`${model.split("/").pop()} ready`, "ok", 2500, "prewarm");
    } catch {
      toast("prewarm failed — will load on first send", "warn", 3000, "prewarm");
    }
  }

  function applyTheme(dark) {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    $("#btn-theme").innerHTML = dark ? '<i class="ph ph-sun"></i>' : '<i class="ph ph-moon"></i>';
  }

  function renderStatus() {
    // status pill was removed; keep function as a no-op shim so callers still work,
    // and update the context gauge since token counts may have changed.
    renderCtxGauge();
  }
  function renderCtxGauge() {
    const arc = $("#ctx-gauge-arc");
    const label = $("#ctx-gauge-label");
    if (!arc || !label) return;
    const capacity = Math.max(1, Number(state.settings.num_ctx) || 8192);
    // rough token estimate: chars/4 across all visible turns
    const chars = (state.messages || []).reduce((a, m) => a + String(m.content || "").length, 0);
    const used = Math.min(capacity, Math.round(chars / 4));
    const pct = Math.min(1, used / capacity);
    const circ = 2 * Math.PI * 13;            // r=13 → 81.68
    arc.setAttribute("stroke-dasharray", circ.toFixed(2));
    arc.setAttribute("stroke-dashoffset", (circ * (1 - pct)).toFixed(2));
    label.textContent = `${Math.round(pct * 100)}%`;
    const gauge = $("#ctx-gauge");
    gauge.classList.toggle("warn", pct >= 0.7 && pct < 0.9);
    gauge.classList.toggle("crit", pct >= 0.9);
    gauge.title = `${used.toLocaleString()} / ${capacity.toLocaleString()} tokens (~${Math.round(pct * 100)}%)`;
  }
  function renderModelPill() {
    const pill = $("#model-pill");
    if (state.settings.model) {
      pill.textContent = state.settings.model;
      pill.title = "Click to change model";
    } else if (state.models.length) {
      pill.textContent = "select model";
      pill.title = "Click to pick a model";
    } else {
      pill.textContent = "no models";
      pill.title = state.modelsError || "Install Ollama and pull a model";
    }
  }

  // ---------- mobile tabs ----------
  function applyMobileTab() {
    $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === state.mobileTab));
    app.classList.remove("m-tab-chat", "m-tab-sessions", "m-tab-approvals", "m-tab-settings");
    if (state.mobileTab === "settings") {
      openSettings();
      state.mobileTab = "chat";
      $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === "chat"));
      app.classList.add("m-tab-chat");
      return;
    }
    app.classList.add("m-tab-" + state.mobileTab);
  }

  // ---------- event wiring ----------
  function autoResize(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(200, ta.scrollHeight) + "px";
  }

  function wireEvents() {
    $("#btn-new-chat").addEventListener("click", newChat);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-close-settings").addEventListener("click", closeSettings);
    $("#drawer-scrim").addEventListener("click", closeSettings);
    const openFaq = () => { $("#faq-scrim").classList.add("open"); $("#faq-modal").classList.add("open"); };
    const closeFaq = () => { $("#faq-scrim").classList.remove("open"); $("#faq-modal").classList.remove("open"); };
    $("#btn-faq")?.addEventListener("click", openFaq);
    $("#btn-close-faq")?.addEventListener("click", closeFaq);
    $("#faq-scrim")?.addEventListener("click", closeFaq);
    $("#btn-save-settings").addEventListener("click", collectAndSaveSettings);
    $("#sw-dark").addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    $("#sw-desktop-enabled")?.addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    $("#btn-desktop-panic")?.addEventListener("click", async () => {
      try {
        await api("/api/desktop/panic", { method: "POST" });
        toast("desktop automation panicked — all actions blocked", "warn", 4000);
        refreshDesktopStatus();
      } catch (e) { toast("panic failed: " + e.message, "error"); }
    });
    $("#btn-desktop-resume")?.addEventListener("click", async () => {
      try {
        await api("/api/desktop/resume", { method: "POST" });
        toast("desktop automation resumed", "ok", 2500);
        refreshDesktopStatus();
      } catch (e) { toast("resume failed: " + e.message, "error"); }
    });
    $("#sw-web").addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    $("#btn-refresh-models").addEventListener("click", async () => {
      const btn = $("#btn-refresh-models");
      btn.disabled = true;
      await loadModels();
      populateSettingsForm();
      renderModelPill();
      btn.disabled = false;
    });
    $("#model-pill").addEventListener("click", openSettings);
    $("#set-model").addEventListener("change", () => {
      const m = $("#set-model").value;
      loadRecommended(m);
      autoTuneForModel(m);           // plug-and-play: apply best settings
    });
    $("#btn-apply-rec").addEventListener("click", applyRecommended);
    $("#btn-sysctx-rescan").addEventListener("click", rescanSystemContext);
    $("#btn-sysctx-save").addEventListener("click", saveSystemContext);
    $("#btn-theme").addEventListener("click", async () => {
      const dark = state.settings.theme !== "dark";
      await saveSettings({ theme: dark ? "dark" : "light" });
      applyTheme(dark);
    });
    $("#btn-send").addEventListener("click", send);
    $("#btn-stop").addEventListener("click", stopStreaming);
    $("#composer-input").addEventListener("input", e => autoResize(e.target));
    $("#composer-input").addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      if (e.shiftKey) return; // newline
      e.preventDefault();
      send();
    });

    // image attach: click button, paste, drop
    $("#btn-attach-image")?.addEventListener("click", () => $("#file-image").click());
    $("#file-image")?.addEventListener("change", async (e) => {
      await addImageFiles(Array.from(e.target.files || []));
      e.target.value = "";
    });
    $("#composer-input").addEventListener("paste", (e) => {
      const items = Array.from(e.clipboardData?.items || []);
      const files = items.filter(i => i.kind === "file" && i.type.startsWith("image/")).map(i => i.getAsFile()).filter(Boolean);
      if (files.length) { e.preventDefault(); addImageFiles(files); }
    });
    const composerEl = document.querySelector(".composer");
    if (composerEl) {
      composerEl.addEventListener("dragover", (e) => { e.preventDefault(); composerEl.classList.add("drag-over"); });
      composerEl.addEventListener("dragleave", () => composerEl.classList.remove("drag-over"));
      composerEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        composerEl.classList.remove("drag-over");
        const files = Array.from(e.dataTransfer?.files || []).filter(f => f.type.startsWith("image/"));
        if (files.length) addImageFiles(files);
      });
    }

    // mode chips
    $$('[data-mode]').forEach(b => {
      b.addEventListener("click", () => {
        state.mode = b.dataset.mode;
        $$('[data-mode]').forEach(x => x.classList.remove("on"));
        b.classList.add("on");
      });
    });

    // preview toggle
    $("#btn-view-preview").addEventListener("click", () => {
      state.view = "preview";
      $("#btn-view-preview").classList.add("active");
      $("#btn-view-code").classList.remove("active");
      renderPreview();
    });
    $("#btn-view-code").addEventListener("click", () => {
      state.view = "code";
      $("#btn-view-code").classList.add("active");
      $("#btn-view-preview").classList.remove("active");
      renderPreview();
    });
    $("#btn-refresh").addEventListener("click", renderPreview);
    $("#btn-open-new").addEventListener("click", () => {
      if (!state.activeVersion) return;
      window.open(`/api/versions/${state.chatId}/${state.activeVersion}`, "_blank");
    });
    $("#btn-close-preview").addEventListener("click", () => app.classList.add("preview-collapsed"));

    // preview pane resize drag
    const resizer = $("#preview-resizer");
    if (resizer) {
      let dragging = false;
      const endDrag = () => {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove("dragging");
        app.classList.remove("resizing");
        document.body.style.userSelect = "";
        try { resizer.releasePointerCapture?.(resizer._pid); } catch {}
        localStorage.setItem("accuretta:preview-w", app.style.getPropertyValue("--preview-w"));
      };
      resizer.addEventListener("pointerdown", (e) => {
        dragging = true;
        resizer._pid = e.pointerId;
        try { resizer.setPointerCapture(e.pointerId); } catch {}
        resizer.classList.add("dragging");
        app.classList.add("resizing");
        document.body.style.userSelect = "none";
        e.preventDefault();
      });
      resizer.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const w = Math.max(280, Math.min(window.innerWidth - 280, window.innerWidth - e.clientX));
        app.style.setProperty("--preview-w", w + "px");
      });
      resizer.addEventListener("pointerup", endDrag);
      resizer.addEventListener("pointercancel", endDrag);
      window.addEventListener("blur", endDrag);
      const saved = localStorage.getItem("accuretta:preview-w");
      if (saved) app.style.setProperty("--preview-w", saved);
    }
    $("#pull-tab").addEventListener("click", () => app.classList.remove("preview-collapsed"));
    $("#btn-toggle-preview").addEventListener("click", () => app.classList.toggle("preview-collapsed"));

    // sidebar toggles
    $("#btn-toggle-sidebar").addEventListener("click", () => app.classList.add("sidebar-collapsed"));
    $("#btn-toggle-sidebar-m").addEventListener("click", () => {
      if (isMobile()) {
        state.mobileTab = "sessions";
        applyMobileTab();
      } else {
        app.classList.toggle("sidebar-collapsed");
      }
    });
    $("#pull-tab-left").addEventListener("click", () => app.classList.remove("sidebar-collapsed"));

    // workspace add
    $("#btn-ws-add-toggle").addEventListener("click", () => {
      $("#ws-add").classList.toggle("hidden");
      $("#ws-input").focus();
    });
    $("#ws-add-btn").addEventListener("click", addWorkspaceFolder);
    $("#ws-browse-btn").addEventListener("click", async () => {
      const btn = $("#ws-browse-btn");
      btn.disabled = true;
      try {
        const r = await api("/api/browse-folder", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
        if (r.path) {
          $("#ws-input").value = r.path;
          await addWorkspaceFolder();
        }
      } finally { btn.disabled = false; }
    });
    $("#ws-input").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); addWorkspaceFolder(); }
    });

    // mobile tabs (legacy bottom bar, still wired for desktop testing)
    $$('.mobile-tab').forEach(t => t.addEventListener("click", () => {
      state.mobileTab = t.dataset.mtab;
      applyMobileTab();
    }));

    // mobile top-right overflow menu
    const mm = $("#mobile-menu");
    const mmScrim = $("#mobile-menu-scrim");
    const mmBtn = $("#btn-mobile-menu");
    const closeMM = () => { mm.classList.remove("open"); mmScrim.classList.remove("open"); };
    const openMM = () => {
      const dark = document.documentElement.getAttribute("data-theme") !== "light";
      const lbl = $("#mm-theme-label");
      if (lbl) lbl.textContent = dark ? "Light mode" : "Dark mode";
      mm.classList.add("open"); mmScrim.classList.add("open");
    };
    mmBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      mm.classList.contains("open") ? closeMM() : openMM();
    });
    mmScrim?.addEventListener("click", closeMM);
    $$(".mm-item").forEach(it => it.addEventListener("click", () => {
      const a = it.dataset.mm;
      closeMM();
      if (a === "theme") { $("#btn-theme").click(); return; }
      if (a === "settings") { openSettings(); return; }
      if (a === "faq") { $("#btn-faq")?.click(); return; }
      if (a === "chat" || a === "sessions" || a === "approvals") {
        state.mobileTab = a;
        applyMobileTab();
      }
    }));

    // responsive
    window.addEventListener("resize", () => {
      document.body.classList.toggle("is-mobile", isMobile());
    });
  }

  // kick off
  loadApprovals();
  boot().catch(e => {
    console.error(e);
    alert("boot error: " + e.message);
  });
})();
