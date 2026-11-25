/* SPDX-License-Identifier: AGPL-3.0-only */
/* llama.cpp backend log (Backend terminal tab): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["backend-log"] = ({ colorizeBackendLine }) => {
  let _backendLogTimer = null;
  async function refreshBackendLog() {
    const pre = document.getElementById("backend-log-pre");
    const codeEl = pre?.querySelector("code");
    if (!codeEl) return;
    let data;
    try {
      data = await (await fetch("/api/llama-log?tail=500")).json();
    } catch { return; }
    const status = document.getElementById("backend-status");
    const statusText = document.getElementById("backend-status-text");
    const modelEl = document.getElementById("backend-model");
    if (status) status.className = "backend-status " + (data.running ? "is-running" : "is-stopped");
    if (statusText) statusText.textContent = data.running ? "running" : "stopped";
    if (modelEl) modelEl.textContent = data.model ? data.model.split(/[\\/]/).pop() : "";
    const lines = data.lines || [];
    // Skip the DOM rewrite when nothing changed, so we don't fight the scroll.
    const sig = lines.length + "|" + (lines[lines.length - 1] || "");
    if (codeEl.dataset.sig === sig) return;
    const pane = pre.closest(".term-tab-pane");
    const atBottom = pane ? (pane.scrollHeight - pane.scrollTop - pane.clientHeight < 48) : true;
    // Drop leading blank lines — llama.cpp's stdout often starts with empties,
    // which would otherwise render as an empty gap under the status bar.
    let _s = 0;
    while (_s < lines.length && !String(lines[_s]).trim()) _s++;
    const shown = lines.slice(_s);
    codeEl.innerHTML = shown.length
      ? shown.map(colorizeBackendLine).join("")
      : "[system] waiting for backend output…";
    codeEl.dataset.sig = sig;
    if (pane && atBottom) pane.scrollTop = pane.scrollHeight;
  }
  function startBackendLogPoll() {
    refreshBackendLog();
    if (_backendLogTimer) return;
    _backendLogTimer = setInterval(refreshBackendLog, 800);
  }
  function stopBackendLogPoll() {
    if (_backendLogTimer) { clearInterval(_backendLogTimer); _backendLogTimer = null; }
  }
  return { startBackendLogPoll, stopBackendLogPoll };
};
