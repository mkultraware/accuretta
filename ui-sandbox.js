/* SPDX-License-Identifier: AGPL-3.0-only */
/* Sandbox (WSL): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["sandbox"] = ({ $, api, esc }) => {
  // Status and management live here, not in the composer. The Setup Wizard has
  // the guided first run; this mirrors its state for later set up / test /
  // remove, polling only while a provision runs.
  let _sbxPoll = null;
  const SBX_BUSY = ["downloading", "extracting", "importing", "provisioning"];

  function _sbxChip(state, label) {
    const chip = $("#sandbox-chip");
    if (!chip) return;
    chip.dataset.state = state;
    chip.textContent = label;
  }

  async function refreshSandboxStatus() {
    if (!$("#sandbox-chip")) return;
    try {
      const st = await api("/api/sandbox/status");
      renderSandbox(st);
      const p = st.provision || {};
      if (SBX_BUSY.includes(p.status)) {
        if (!_sbxPoll) _sbxPoll = setInterval(refreshSandboxStatus, 800);
      } else if (_sbxPoll) {
        clearInterval(_sbxPoll); _sbxPoll = null;
      }
    } catch (e) {
      _sbxChip("error", "unavailable");
    }
  }

  function renderSandbox(st) {
    const p = st.provision || {};
    const busy = SBX_BUSY.includes(p.status);
    const row = $("#sandbox-progress-row");
    const setupBtn = $("#btn-sandbox-setup");
    const testBtn = $("#btn-sandbox-test");
    const rmBtn = $("#btn-sandbox-remove");

    if (busy) _sbxChip("installing", p.step || "setting up…");
    else if (st.state === "ready") _sbxChip("ready", "ready");
    else if (st.state === "no_wsl") _sbxChip("no_wsl", "WSL not installed");
    else if (st.state === "needs_hardening") _sbxChip("warn", "security update required");
    else if (st.state === "present_unprovisioned") _sbxChip("warn", "needs provisioning");
    else _sbxChip("off", "not set up");

    if (setupBtn) {
      setupBtn.disabled = busy || st.state === "no_wsl";
      setupBtn.dataset.reinstall = st.state === "ready" ? "1" : "";
      setupBtn.innerHTML = st.state === "ready"
        ? '<i class="ph ph-arrow-clockwise"></i>Reinstall'
        : st.state === "needs_hardening"
          ? '<i class="ph ph-shield-check"></i>Apply security update'
          : '<i class="ph ph-cube"></i>Set up WSL guest';
    }
    if (testBtn) testBtn.disabled = busy || st.state !== "ready";
    if (rmBtn) rmBtn.disabled = busy || !st.sandbox_present;

    if (busy) {
      if (row) row.style.display = "";
      const icon = $("#sandbox-card-icon"), title = $("#sandbox-card-title"),
            desc = $("#sandbox-card-desc"), log = $("#sandbox-card-log");
      if (icon) icon.className = "ph ph-spinner-gap pulse-icon spin";
      if (title) title.textContent = p.step || "Setting up…";
      const pct = typeof p.pct === "number" ? p.pct : 0;
      if (desc) desc.innerHTML =
        '<div class="setup-download-container"><div class="setup-download-bar">' +
        '<div class="setup-download-fill" style="width:' + pct + '%;"></div></div>' +
        '<div class="setup-download-meta"><span>' + esc(p.status || "") + '</span><span>' + pct + '%</span></div></div>';
      if (log) {
        if (p.log && p.log.length) { log.style.display = ""; log.textContent = p.log.slice(-8).join("\n"); log.scrollTop = log.scrollHeight; }
        else log.style.display = "none";
      }
    } else if (p.status === "failed" && p.error) {
      if (row) row.style.display = "";
      const icon = $("#sandbox-card-icon"), title = $("#sandbox-card-title"),
            desc = $("#sandbox-card-desc"), log = $("#sandbox-card-log");
      if (icon) icon.className = "ph ph-warning-circle";
      if (title) title.textContent = "Setup failed";
      if (desc) desc.innerHTML = '<span style="color:var(--danger);">' + esc(p.error) + '</span>';
      if (log) log.style.display = "none";
    } else if (row) {
      row.style.display = "none";
    }
  }
  return { refreshSandboxStatus };
};
