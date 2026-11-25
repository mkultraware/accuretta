/* SPDX-License-Identifier: AGPL-3.0-only */
/* attachments: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["attachments"] = ({
  $, appendAgentLog, autoResize, captureIframePng, currentClientHint, esc, hideMentionMenu,
  humanBytes, openSettings, send, state, toast, withMentionRefs,
}) => {
  // Images and files wait above the input as tiles: a thumbnail, or a colored
  // file-type icon over the name. Files upload when the message is sent; while
  // they do, each tile draws its progress round its edge and counts up in the
  // corner, one file after another, and can't be removed.
  const ATTACH_TYPES = [
    [/^pdf$/, "ph-file-pdf", "red"],
    [/^(docx?|odt|rtf|pages)$/, "ph-file-doc", "blue"],
    [/^(txt|log)$/, "ph-file-txt", "gray"],
    [/^(md|markdown)$/, "ph-file-md", "gray"],
    [/^(xlsx?|ods|numbers)$/, "ph-file-xls", "green"],
    [/^(csv|tsv)$/, "ph-file-csv", "green"],
    [/^(pptx?|odp|key)$/, "ph-file-ppt", "orange"],
    [/^(html?|css|[cm]?jsx?|tsx?|py|sql|c|cc|cpp|h|hpp|rs|go|java|kt|swift|rb|php|sh|ps1|bat|json|ya?ml|toml|xml|ini)$/, "ph-file-code", "teal"],
    [/^(zip|7z|rar|tar|gz|tgz|bz2|xz)$/, "ph-file-zip", "amber"],
    [/^(mp3|wav|flac|ogg|m4a|aac)$/, "ph-file-audio", "pink"],
    [/^(mp4|mov|mkv|webm|avi|m4v)$/, "ph-file-video", "blue"],
    [/^(png|jpe?g|gif|webp|bmp|tiff?|heic|ico|svg)$/, "ph-file-image", "violet"],
  ];
  function attachType(name) {
    const ext = (String(name).match(/\.([a-z0-9]+)$/i) || [])[1]?.toLowerCase() || "";
    const hit = ATTACH_TYPES.find(([re]) => re.test(ext));
    return hit ? { icon: hit[1], tint: hit[2] } : { icon: "ph-file", tint: "gray" };
  }
  function attachTile(kind, index, name, body, title) {
    const tile = document.createElement("div");
    tile.className = "wb-att";
    tile.dataset.kind = kind;
    tile.dataset.index = String(index);
    tile.title = title;
    tile.innerHTML = body
      + `<span class="wb-att-pct" aria-hidden="true"></span>`
      + `<svg class="wb-att-ring" aria-hidden="true" focusable="false"><rect class="wb-att-ring-track"></rect><rect class="wb-att-ring-arc" pathLength="100"></rect></svg>`
      + `<button type="button" class="wb-att-x" aria-label="Remove ${esc(name)}" title="Remove"><i class="ph ph-x" aria-hidden="true"></i></button>`;
    return tile;
  }
  function renderImageTray() {
    const tray = $("#image-tray");
    if (!tray) return;
    tray.innerHTML = "";
    if (!state.pendingImages.length && !state.pendingFiles.length) { tray.classList.add("hidden"); return; }
    tray.classList.remove("hidden");
    tray.classList.toggle("uploading", !!state.uploadingFiles);
    state.pendingImages.forEach((img, i) => {
      const name = img.name || "image";
      tray.appendChild(attachTile("image", i, name, `<img class="wb-att-thumb" src="${img.dataUrl}" alt="${esc(name)}">`, name));
    });
    state.pendingFiles.forEach((file, i) => {
      const { icon, tint } = attachType(file.name);
      tray.appendChild(attachTile("file", i, file.name,
        `<i class="ph-fill ${icon} wb-att-icon" data-tint="${tint}" aria-hidden="true"></i><span class="wb-att-name">${esc(file.name)}</span>`,
        `${file.name} · ${humanBytes(file.size)}`));
    });
  }
  // Progress for the i-th pending file: 0..1 while it uploads, 1 once landed.
  function setAttachProgress(i, fraction) {
    const tile = $(`#image-tray .wb-att[data-kind="file"][data-index="${i}"]`);
    if (!tile) return;
    const pct = Math.max(0, Math.min(100, Math.round(fraction * 100)));
    tile.dataset.state = pct >= 100 ? "done" : "uploading";
    tile.querySelector(".wb-att-pct").textContent = pct >= 100 ? "" : `${pct}%`;
    tile.querySelector(".wb-att-ring-arc").style.strokeDasharray = `${pct} 100`;
  }
  function fileToDataURL(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }

  // Vision models spend tokens in proportion to pixels and every agentic round
  // replays the stored image, so send a bounded copy: 1568px on the long edge.
  // PNG stays PNG so screenshots and fine print stay legible; photos become q90
  // JPEG. Smaller images pass through untouched.
  const IMAGE_SEND_MAX_EDGE = 1568;

  function loadImageForShrink(file) {
    if (typeof createImageBitmap === "function") {
      return createImageBitmap(file, { imageOrientation: "from-image" })
        .catch(() => createImageBitmap(file));
    }
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = (e) => { URL.revokeObjectURL(url); reject(e); };
      img.src = url;
    });
  }

  async function shrinkImageFile(file) {
    if (/image\/(gif|svg\+xml)/i.test(file.type || "")) return fileToDataURL(file);
    let source;
    try { source = await loadImageForShrink(file); }
    catch (_) { return fileToDataURL(file); }
    const width = source.width || source.naturalWidth || 0;
    const height = source.height || source.naturalHeight || 0;
    if (!width || !height || Math.max(width, height) <= IMAGE_SEND_MAX_EDGE) {
      source.close?.();
      return fileToDataURL(file);
    }
    const scale = IMAGE_SEND_MAX_EDGE / Math.max(width, height);
    const targetW = Math.max(1, Math.round(width * scale));
    const targetH = Math.max(1, Math.round(height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = targetW;
    canvas.height = targetH;
    const ctx = canvas.getContext("2d");
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    const jpeg = /image\/jpe?g/i.test(file.type || "");
    if (jpeg) { ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, targetW, targetH); }
    ctx.drawImage(source, 0, 0, targetW, targetH);
    source.close?.();
    return jpeg ? canvas.toDataURL("image/jpeg", 0.9) : canvas.toDataURL("image/png");
  }

  async function addImageFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      try {
        const dataUrl = await shrinkImageFile(f);
        state.pendingImages.push({ dataUrl, name: f.name });
      } catch (e) { console.warn("read failed", e); }
    }
    renderImageTray();
  }

  function addClientFiles(files) {
    const maxBytes = Math.max(1, Number(state.settings.remote_file_max_mb || 100)) * 1024 * 1024;
    for (const file of files) {
      if (!file || !file.name) continue;
      if (file.size > maxBytes) {
        toast(`${file.name} exceeds the ${Math.round(maxBytes / 1024 / 1024)} MB limit`, "warn", 3500);
        continue;
      }
      if (state.pendingFiles.length >= 20) {
        toast("Twenty files at once is plenty. The browser is not a moving van.", "warn", 3000);
        break;
      }
      state.pendingFiles.push(file);
    }
    renderImageTray();
  }

  function messageQueue(chatId = state.chatId, create = false) {
    if (!chatId) return [];
    if (!state.messageQueues.has(chatId) && create) state.messageQueues.set(chatId, []);
    return state.messageQueues.get(chatId) || [];
  }

  function queuedMessageId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  }

  function renderMessageQueue() {
    const host = $("#message-queue");
    if (!host) return;
    const queue = messageQueue();
    host.classList.toggle("hidden", !queue.length);
    host.innerHTML = queue.length ? `<div class="message-queue-head"><span><i class="ph ph-paper-plane-tilt" aria-hidden="true"></i>Your updates <span class="queue-count">${queue.length}</span></span><small>Waiting to be applied</small></div><div class="message-queue-list">${queue.map(item => {
      const label = item.status === "sending" ? "Sending…" : item.status === "pending" ? "Queued" : item.status === "failed" ? "Not delivered" : "Queued";
      return `<article class="queued-message" data-status="${esc(item.status)}" data-queue-id="${esc(item.id)}"><div class="queued-message-body"><div class="queued-message-text">${esc(item.text || "Attached files")}</div>${item.status === "failed" && item.error ? `<div class="queued-message-meta">${esc(item.error)}</div>` : ""}</div><span class="task-update-status">${label}</span>${item.status === "failed" ? '<div class="queued-message-actions"><button type="button" data-update-action="retry">Retry</button><button type="button" data-update-action="remove">Remove</button></div>' : ""}</article>`;
    }).join("")}</div>` : "";
    host.querySelectorAll("[data-update-action]").forEach(button => button.addEventListener("click", async () => {
      const item = queue.find(entry => entry.id === button.closest("[data-queue-id]").dataset.queueId);
      if (!item) return;
      if (button.dataset.updateAction === "remove") {
        const receipt = await taskUpdateRequest("/api/chat/steer-status", { chat_id: item.chatId, id: item.id }).catch(() => null);
        if (!receipt) { toast("Could not check delivery. Try again before removing this update.", "warn", 3500); return; }
        if (receipt.status === "pending") { item.status = "pending"; renderMessageQueue(); return; }
        queue.splice(queue.indexOf(item), 1); renderMessageQueue();
      } else if (state.streaming && state.liveTurn?.chatId === item.chatId) {
        const receipt = await taskUpdateRequest("/api/chat/steer-status", { chat_id: item.chatId, id: item.id }).catch(() => null);
        if (!receipt) { toast("Could not check delivery. Please try again.", "warn", 3000); return; }
        if (receipt.status === "applied") { queue.splice(queue.indexOf(item), 1); renderMessageQueue(); return; }
        if (receipt.status === "deferred") item.id = queuedMessageId();
        await submitTaskUpdate(item);
      } else {
        const receipt = await taskUpdateRequest("/api/chat/steer-status", { chat_id: item.chatId, id: item.id }).catch(() => null);
        if (!receipt) { toast("Could not check delivery. Please try again.", "warn", 3000); return; }
        if (receipt.status === "applied") { queue.splice(queue.indexOf(item), 1); renderMessageQueue(); return; }
        if (receipt.status === "pending") { item.status = "pending"; renderMessageQueue(); return; }
        item.status = "queued"; scheduleMessageQueueDrain(item.chatId);
      }
    }));
  }

  async function taskUpdateRequest(path, payload) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json();
    if (!response.ok && result.status !== "inactive") throw new Error(result.error || "Could not deliver the update");
    return result;
  }

  async function submitTaskUpdate(item) {
    item.status = "sending";
    if (state.chatId === item.chatId) renderMessageQueue();
    try {
      if (item.files.length && !item.uploadedFiles) item.uploadedFiles = await uploadClientFiles(item.files);
      if (!item.wireText) {
        const fileBlock = item.uploadedFiles?.length ? "\n\n[Files uploaded into the workspace]\n" + item.uploadedFiles.map(file => `${file.name}: ${file.path}`).join("\n") : "";
        item.wireText = withMentionRefs(item.text) + fileBlock;
      }
      const result = await taskUpdateRequest("/api/chat/steer", {
        chat_id: item.chatId, id: item.id, text: item.wireText,
        images: item.images.map(image => typeof image === "string" ? image : image.dataUrl),
      });
      const queue = messageQueue(item.chatId);
      if (!queue.includes(item)) return;
      if (result.status === "applied") queue.splice(queue.indexOf(item), 1);
      else if (result.status === "inactive") item.status = "queued";
      else if (result.status === "deferred") { item.status = "failed"; item.error = "The task ended before this update was delivered."; }
      else item.status = "pending";
    } catch (error) {
      item.status = "failed";
      item.error = error.message || "Could not confirm delivery";
    }
    if (state.chatId === item.chatId) renderMessageQueue();
    if (!state.streaming) scheduleMessageQueueDrain(item.chatId);
  }

  async function enqueueMessage(opts = {}) {
    const fromComposer = opts.prompt === undefined;
    const ta = $("#composer-input");
    const chatId = state.chatId;
    const text = (opts.researchId ? String(opts.prompt || "").trim() : opts.researchBrief?.topic)
      || (fromComposer ? ta.value.trim() : String(opts.prompt || "").trim());
    const images = Array.isArray(opts.images) ? opts.images.slice() : (fromComposer ? state.pendingImages.slice() : []);
    const files = Array.isArray(opts.files) ? opts.files.slice() : (fromComposer ? state.pendingFiles.slice() : []);
    if (!text && !images.length && !files.length) return false;
    if (!chatId) return false;
    if (!state.settings.model) {
      toast("Pick a model in Settings first.", "warn", 3200, "no-model");
      openSettings();
      return false;
    }

    if (fromComposer) {
      ta.value = "";
      autoResize(ta);
      hideMentionMenu();
      localStorage.removeItem("accuretta:draft:" + chatId);
      state.pendingImages = [];
      state.pendingFiles = [];
      renderImageTray();
    }

    if (text && /\breview this ui\b/i.test(text) && !images.length && state.currentHtml) {
      const canvas = await captureIframePng();
      if (canvas) images.push({ dataUrl: canvas.toDataURL("image/png"), name: `ui-${Date.now()}.png` });
    }

    const item = {
      id: queuedMessageId(),
      chatId,
      text,
      images,
      files,
      mode: opts.mode || state.mode,
      reasoningEffort: opts.reasoningEffort || state.reasoningEffort || "auto",
      clientContext: opts.clientContext || currentClientHint(),
      invisible: !!opts.invisible,
      mission: opts.mission || null,
      researchBrief: opts.researchBrief || null,
      researchId: opts.researchId || null,
      createdAt: Date.now(),
      status: "queued",
    };
    messageQueue(chatId, true).push(item);
    if (state.chatId === chatId) renderMessageQueue();
    if (state.streaming && state.liveTurn?.chatId === chatId && !item.mission && !item.researchBrief) {
      await submitTaskUpdate(item);
      return true;
    }
    appendAgentLog("Message will be sent when the current task finishes.");
    if (state.chatId === chatId && !state.streaming && !state.queueDispatching) scheduleMessageQueueDrain(chatId);
    return true;
  }

  function scheduleMessageQueueDrain(chatId = state.chatId) {
    clearTimeout(state.queueDrainTimer);
    state.queueDrainTimer = setTimeout(() => drainMessageQueue(chatId), 80);
  }

  async function drainMessageQueue(chatId = state.chatId) {
    if (!chatId || state.chatId !== chatId || state.streaming || state.queueDispatching || state.queueEditId) return;
    const queue = messageQueue(chatId);
    if (!queue.length) return;
    for (const update of [...queue].filter(item => item.status === "pending")) {
      try {
        const receipt = await taskUpdateRequest("/api/chat/steer-status", { chat_id: chatId, id: update.id });
        if (receipt.status === "applied") queue.splice(queue.indexOf(update), 1);
        else if (receipt.status !== "pending") { update.status = "failed"; update.error = "The task ended before delivery was confirmed. Retry to send again."; }
      } catch (error) { update.status = "failed"; update.error = "Could not confirm delivery. Retry to check."; }
    }
    renderMessageQueue();
    if (!queue.length || queue[0].status !== "queued" || state.streaming || state.queueDispatching) return;
    const item = queue.shift();
    state.queueEditId = null;
    state.queueDispatching = true;
    renderMessageQueue();
    let accepted = false;
    try {
      accepted = await send({
        prompt: item.wireText || item.text,
        preparedPrompt: !!item.wireText,
        images: item.images,
        files: item.uploadedFiles ? [] : item.files,
        mode: item.mode,
        reasoningEffort: item.reasoningEffort,
        researchBrief: item.researchBrief,
        researchId: item.researchId,
        clientContext: item.clientContext,
        invisible: item.invisible,
        mission: item.mission,
        fromQueue: true,
      }) !== false;
    } finally {
      state.queueDispatching = false;
      if (!accepted) {
        queue.unshift(item);
        renderMessageQueue();
      } else if (queue.length) {
        scheduleMessageQueueDrain(chatId);
      }
    }
  }

  // XMLHttpRequest rather than fetch: it reports upload progress. It also
  // skips bridge-client.js's fetch wrapper, so it sends the token itself.
  function uploadClientFile(file, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/client-files/upload");
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.setRequestHeader("X-File-Name", encodeURIComponent(file.name));
      xhr.setRequestHeader("X-Accuretta-Token", document.querySelector('meta[name="accuretta-request-token"]')?.content || "");
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && e.total) onProgress?.(e.loaded / e.total); };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch {}
        if (xhr.status < 200 || xhr.status >= 300 || data.error) reject(new Error(data.error || `upload failed (${xhr.status})`));
        else resolve(data);
      };
      xhr.onerror = () => reject(new Error("upload failed (connection lost)"));
      xhr.send(file);
    });
  }
  // One file after another; onProgress(i, fraction) follows each.
  async function uploadClientFiles(files, onProgress) {
    const uploaded = [];
    for (let i = 0; i < files.length; i++) {
      onProgress?.(i, 0);
      uploaded.push(await uploadClientFile(files[i], (fraction) => onProgress?.(i, Math.min(fraction, 0.99))));
      onProgress?.(i, 1);
    }
    return uploaded;
  }
  return {
    renderImageTray, setAttachProgress, addImageFiles, addClientFiles, messageQueue,
    renderMessageQueue, enqueueMessage, scheduleMessageQueueDrain, uploadClientFiles,
  };
};
