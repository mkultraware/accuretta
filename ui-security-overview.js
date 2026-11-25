/* SPDX-License-Identifier: AGPL-3.0-only */
/* Security Overview: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["security-overview"] = ({
  $, $$, api, autoResize, closeSettings, closeUsageStats, esc, loadChats, selectChat, toast,
}) => {
  let securityOverviewData = null;
  let securityPollTimer = null;
  let pendingWhitelistAlertId = "";
  let securityIntroSeen = false;
  let securityIntroVisual = null;
  let securityIntroStartedAt = 0;
  let securityIntroFinishTimer = null;
  let securityIntroFinishing = false;

  const SECURITY_INTRO_STAGES = [
    { key: "network", match: "network", percent: 10 },
    { key: "system", match: "system", percent: 20 },
    { key: "application", match: "application", percent: 30 },
    { key: "security", match: "security", percent: 40 },
    { key: "persistence", match: "persistence", percent: 50 },
    { key: "processes", match: "process", percent: 58 },
    { key: "commands", match: "command", percent: 66 },
    { key: "actions", match: "action history", percent: 74 },
    { key: "correlate", match: "correlating", percent: 85 },
    { key: "summary", match: "summary", percent: 96 },
  ];
  const SECURITY_STAGE_COUNT = String(SECURITY_INTRO_STAGES.length).padStart(2, "0");

  function securityIntroStage(stage = "") {
    const value = String(stage).toLowerCase();
    const index = SECURITY_INTRO_STAGES.findIndex(item => value.includes(item.match));
    return index >= 0 ? { ...SECURITY_INTRO_STAGES[index], index } : { key: "", index: -1, percent: 4 };
  }

  function updateSecurityIntroClock(elapsedMs) {
    const clock = $("#security-intro-clock");
    if (!clock) return;
    const seconds = Math.max(0, elapsedMs) / 1000;
    const minutes = Math.floor(seconds / 60);
    clock.textContent = `${String(minutes).padStart(2, "0")}:${(seconds % 60).toFixed(1).padStart(4, "0")}`;
  }

  function ensureSecurityIntroVisual() {
    if (securityIntroVisual) return securityIntroVisual;
    const canvas = $("#security-scan-canvas");
    const factory = window.AccurettaSecurityScanField;
    if (!canvas || !factory?.create) return null;
    securityIntroVisual = factory.create(canvas, { onTick: updateSecurityIntroClock });
    return securityIntroVisual;
  }

  function updateSecurityScanIntro(stage = "Preparing local collectors") {
    const current = securityIntroStage(stage);
    if ($("#security-intro-stage")) $("#security-intro-stage").textContent = stage;
    if ($("#security-intro-index")) $("#security-intro-index").textContent = `${String(current.index + 1).padStart(2, "0")} / ${SECURITY_STAGE_COUNT}`;
    if ($("#security-intro-percent")) $("#security-intro-percent").textContent = `${current.percent}%`;
    if ($("#security-intro-progress")) $("#security-intro-progress").style.width = `${current.percent}%`;
    securityIntroVisual?.setProgress(current.percent / 100, current.index);
    $$("[data-security-source]").forEach((node, index) => {
      node.classList.toggle("is-complete", current.index > index);
      node.classList.toggle("is-active", current.index === index);
    });
  }

  function startSecurityScanIntro(stage) {
    if (securityIntroSeen) return;
    const overlay = $("#security-scan-intro");
    if (!overlay) return;
    securityIntroSeen = true;
    securityIntroFinishing = false;
    securityIntroStartedAt = performance.now();
    overlay.hidden = false;
    overlay.classList.remove("is-leaving");
    overlay.dataset.state = "scanning";
    const visual = ensureSecurityIntroVisual();
    visual?.start();
    updateSecurityScanIntro(stage || "Preparing local collectors");
  }

  function hideSecurityScanIntro(immediate = false) {
    const overlay = $("#security-scan-intro");
    if (!overlay || overlay.hidden) {
      securityIntroVisual?.stop();
      return;
    }
    clearTimeout(securityIntroFinishTimer);
    const hide = () => {
      overlay.hidden = true;
      overlay.classList.remove("is-leaving");
      securityIntroVisual?.stop();
    };
    if (immediate || matchMedia("(prefers-reduced-motion: reduce)").matches) hide();
    else {
      overlay.classList.add("is-leaving");
      securityIntroFinishTimer = setTimeout(hide, 430);
    }
  }

  function finishSecurityScanIntro(failed = false) {
    const overlay = $("#security-scan-intro");
    if (!overlay || overlay.hidden || securityIntroFinishing) return;
    securityIntroFinishing = true;
    const wait = Math.max(0, 3200 - (performance.now() - securityIntroStartedAt));
    securityIntroFinishTimer = setTimeout(() => {
      overlay.dataset.state = failed ? "error" : "complete";
      securityIntroVisual?.setState(failed ? "error" : "complete");
      if ($("#security-intro-index")) $("#security-intro-index").textContent = failed ? "SCAN STOPPED" : `${SECURITY_STAGE_COUNT} / ${SECURITY_STAGE_COUNT}`;
      if ($("#security-intro-stage")) $("#security-intro-stage").textContent = failed ? "Scan stopped before completion" : "Local snapshot complete";
      if ($("#security-intro-percent")) $("#security-intro-percent").textContent = failed ? "--" : "100%";
      if ($("#security-intro-progress")) $("#security-intro-progress").style.width = "100%";
      $$("[data-security-source]").forEach(node => {
        node.classList.remove("is-active");
        node.classList.toggle("is-complete", !failed);
      });
      securityIntroFinishTimer = setTimeout(() => hideSecurityScanIntro(), failed ? 1500 : 1150);
    }, wait);
  }

  function securityWhen(epochSeconds) {
    if (!epochSeconds) return "Never scanned";
    try {
      return new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium", timeStyle: "short",
      }).format(new Date(Number(epochSeconds) * 1000));
    } catch {
      return new Date(Number(epochSeconds) * 1000).toLocaleString();
    }
  }

  function securitySourceLabel(value) {
    return String(value || "unknown").replaceAll("_", " ");
  }

  function renderSecurityOverview(data) {
    securityOverviewData = data || {};
    const summary = data?.summary || {};
    const risk = data?.risk || { state: "quiet" };
    const metrics = data?.metrics || {};
    const briefing = $("#security-briefing");
    if (briefing) briefing.dataset.risk = risk.state || "quiet";
    if ($("#security-risk-label")) $("#security-risk-label").textContent = securitySourceLabel(risk.state || "quiet");
    if ($("#security-summary-source")) $("#security-summary-source").textContent = summary.source === "model" ? "Local model analysis" : "Rules summary";
    if ($("#security-summary-headline")) $("#security-summary-headline").textContent = summary.headline || risk.headline || "Security Overview";
    if ($("#security-summary-text")) $("#security-summary-text").textContent = summary.situation || risk.detail || "No current summary is available.";
    if ($("#security-coverage-note")) $("#security-coverage-note").textContent = summary.coverage_note || "Coverage has not been measured.";
    if ($("#security-metric-alerts")) $("#security-metric-alerts").textContent = Number(metrics.open_alerts || 0).toLocaleString();
    if ($("#security-metric-events")) $("#security-metric-events").textContent = Number(metrics.windows_events || 0).toLocaleString();
    if ($("#security-metric-network")) $("#security-metric-network").textContent = Number(metrics.tcp_connections || 0).toLocaleString();
    if ($("#security-metric-coverage")) $("#security-metric-coverage").textContent = `${Number(metrics.coverage_available || 0)}/${Number(metrics.coverage_total || 0)}`;
    if ($("#security-updated-at")) $("#security-updated-at").textContent = data?.generated_at ? `Updated ${securityWhen(data.generated_at)}` : "Never scanned";

    const scanning = data?.scan?.status === "scanning";
    const scanFailed = data?.scan?.status === "error";
    if (scanning && $("#security-page")?.classList.contains("open")) {
      startSecurityScanIntro(data?.scan?.stage);
      updateSecurityScanIntro(data?.scan?.stage || "Preparing local collectors");
    } else if (!$("#security-scan-intro")?.hidden) {
      finishSecurityScanIntro(scanFailed);
    }
    const scanStatus = $("#security-scan-status");
    if (scanStatus) {
      scanStatus.hidden = !scanning && !scanFailed;
      scanStatus.dataset.state = scanFailed ? "error" : "scanning";
      const heading = scanStatus.querySelector("strong");
      if (heading) heading.textContent = scanFailed ? "The last scan stopped" : "Reading this machine";
    }
    if ($("#security-scan-stage")) $("#security-scan-stage").textContent = scanFailed
      ? (data?.scan?.error || "A collector failed unexpectedly.")
      : (data?.scan?.stage || "Preparing local collectors");
    const refresh = $("#btn-refresh-security");
    if (refresh) refresh.disabled = scanning;
    const analyze = $("#btn-security-analyze");
    if (analyze) analyze.disabled = scanning || !data?.generated_at;
    if (scanning) scheduleSecurityPoll();
    else clearSecurityPoll();

    renderSecurityAlerts();
    renderSecurityCoverage(data?.coverage || []);
    renderSecurityTimeline(data?.timeline || []);
    renderSecurityWhitelist(data?.whitelist || []);
    renderSecurityAlertFilters(data?.alert_filters || []);
    renderSecurityWatchlist(data?.watchlist || []);
    renderSecurityAttention(data?.attention || {}, data?.environment || {});
  }

  function renderSecurityAlerts() {
    const list = $("#security-alert-list");
    if (!list) return;
    const showWhitelisted = !!$("#security-show-whitelisted")?.checked;
    const active = Array.isArray(securityOverviewData?.alerts) ? securityOverviewData.alerts : [];
    const hidden = showWhitelisted && Array.isArray(securityOverviewData?.whitelisted_activity)
      ? securityOverviewData.whitelisted_activity.map(item => ({ ...item, _whitelisted: true })) : [];
    const alerts = [...active, ...hidden];
    if (!alerts.length) {
      const scanned = !!securityOverviewData?.generated_at;
      list.innerHTML = `<div class="security-empty"><i class="ph ${scanned ? "ph-shield-check" : "ph-scan"}"></i><strong>${scanned ? "Nothing currently needs attention" : "No scan data yet"}</strong><span>${scanned ? "No open alerts from the latest scan. Quieted items remain available below." : "Run a scan to inspect the current machine state."}</span></div>`;
      return;
    }
    list.innerHTML = alerts.map(alert => `
      <article class="security-alert ${alert._whitelisted ? "is-whitelisted" : ""}" data-alert-id="${esc(alert.id || "")}" data-severity="${esc(alert.severity || "info")}">
        <span class="security-alert-severity">${esc(alert._whitelisted ? "trusted" : alert.severity || "info")}</span>
        <div class="security-alert-copy">
          <h4>${esc(alert.title || "Security alert")}</h4>
          <p>${esc(alert.detail || "No additional detail was recorded.")}</p>
          <span class="security-alert-entity" title="${esc(alert.entity_label || "")}">${esc(alert.entity_label || securitySourceLabel(alert.source))}</span>
        </div>
        <div class="security-alert-actions">
          <button type="button" data-security-action="investigate">Investigate</button>
          ${alert._whitelisted || alert.whitelistable === false ? "" : '<button type="button" data-security-action="whitelist">Whitelist</button>'}
          ${alert.dismissible ? '<button type="button" data-security-action="dismiss" title="Hide these recorded occurrences">Dismiss</button><button type="button" data-security-action="ignore" title="Hide matching failures in future scans. You can restore them below.">Ignore repeats</button>' : ""}
        </div>
      </article>`).join("");
    list.querySelectorAll("[data-security-action]").forEach(button => button.addEventListener("click", event => {
      const row = event.currentTarget.closest("[data-alert-id]");
      const alertId = row?.dataset.alertId || "";
      if (event.currentTarget.dataset.securityAction === "investigate") openSecurityInvestigation(alertId, event.currentTarget);
      else if (["dismiss", "ignore"].includes(event.currentTarget.dataset.securityAction)) quietSecurityAlert(alertId, event.currentTarget.dataset.securityAction, event.currentTarget);
      else openSecurityWhitelist(alertId);
    }));
  }

  async function quietSecurityAlert(alertId, mode, button) {
    button.disabled = true;
    try {
      const result = await api("/api/security/alerts/filter", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_id: alertId, mode }),
      });
      if (!result?.ok) throw new Error(result?.error || "Could not quiet this alert.");
      renderSecurityOverview(result.overview);
      toast(mode === "ignore" ? "Matching errors will stay quiet. Restore them below at any time." : "Recorded errors dismissed. New occurrences can still appear.", "ok", 3000);
    } catch (error) {
      button.disabled = false;
      toast(error.message || String(error), "warn", 3500);
    }
  }

  function renderSecurityAlertFilters(rows) {
    const list = document.getElementById("security-alert-filters");
    if (!list) return;
    const count = document.getElementById("security-filter-count");
    if (count) count.textContent = String(rows.length);
    list.innerHTML = rows.map(row => `<div class="security-whitelist-row"><div class="security-whitelist-main"><strong title="${esc(row.label)}">${esc(row.label)}</strong><span>${row.mode === "ignore" ? "Matching repeats ignored" : "Recorded occurrence dismissed"} · ${esc(securityWhen(row.created_at))}</span></div><button type="button" data-restore-filter="${esc(row.id)}">Restore</button></div>`).join("") || '<div class="security-empty compact"><span>No tool errors have been quieted.</span></div>';
    list.querySelectorAll("[data-restore-filter]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api("/api/security/alerts/restore", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: button.dataset.restoreFilter }),
        });
        if (!result?.ok) throw new Error(result?.error || "Could not restore this alert.");
        renderSecurityOverview(result.overview);
        toast("Filter removed. Matching errors can appear again.", "ok", 2200);
      } catch (error) {
        button.disabled = false;
        toast(error.message || String(error), "warn", 3500);
      }
    }));
  }

  function renderSecurityCoverage(rows) {
    const list = $("#security-coverage-list");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = '<div class="security-empty compact"><span>No collectors have reported yet.</span></div>';
      return;
    }
    list.innerHTML = rows.map(row => `
      <div class="security-coverage-row" data-state="${esc(row.state || "error")}">
        <span class="security-coverage-dot"></span>
        <div class="security-coverage-copy"><strong>${esc(securitySourceLabel(row.source))}</strong><span>${esc(row.note || "")}</span></div>
        <span class="security-coverage-time">${Number(row.duration_ms || 0).toLocaleString()} ms</span>
      </div>`).join("");
  }

  function renderSecurityTimeline(rows) {
    const list = $("#security-timeline");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = '<div class="security-empty compact"><span>No notable timeline entries were returned.</span></div>';
      return;
    }
    list.innerHTML = rows.slice(0, 10).map(row => `
      <div class="security-timeline-row">
        <time>${esc(row.time || "Unknown time")}</time>
        <strong>${esc(row.label || securitySourceLabel(row.source))}</strong>
        <span>${esc(row.detail || "")}</span>
      </div>`).join("");
  }

  function renderSecurityWhitelist(rows) {
    const list = $("#security-whitelist-list");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = '<div class="security-empty compact"><span>No applications have been whitelisted.</span></div>';
      return;
    }
    list.innerHTML = rows.map(row => `
      <div class="security-whitelist-row" data-whitelist-id="${esc(row.id || "")}">
        <div class="security-whitelist-main"><strong title="${esc(row.label || "")}">${esc(row.label || "Trusted application")}</strong><span>Added ${esc(securityWhen(row.created_at))}</span></div>
        <span class="security-whitelist-reason">${esc(row.reason || "No reason recorded")}</span>
        <button type="button">Remove</button>
      </div>`).join("");
    list.querySelectorAll("button").forEach(button => button.addEventListener("click", async event => {
      const id = event.currentTarget.closest("[data-whitelist-id]")?.dataset.whitelistId || "";
      event.currentTarget.disabled = true;
      try {
        const result = await api("/api/security/whitelist/remove", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
        });
        if (!result?.ok) throw new Error(result?.error || "Could not remove whitelist entry.");
        renderSecurityOverview(result.overview);
        toast("Whitelist entry removed.", "ok", 1800);
      } catch (error) {
        event.currentTarget.disabled = false;
        toast(error.message || String(error), "warn", 3500);
      }
    }));
  }

  function renderSecurityWatchCondition(conditions = {}) {
    const when = conditions.when || ["running"];
    const labels = {
      running: "any run",
      network: "network activity",
      new_listener: "new UDP listener",
      unsigned: "unsigned binary",
      outside_hours: "outside hours",
    };
    let text = when.map(w => labels[w] || w).join(" · ");
    if (when.includes("outside_hours") && conditions.hours) {
      text += ` (allowed ${String(Number(conditions.hours.start)).padStart(2, "0")}:00–${String(Number(conditions.hours.end)).padStart(2, "0")}:00)`;
    }
    return text;
  }

  function renderSecurityWatchlist(rows) {
    const list = $("#security-watchlist-list");
    if (!list) return;
    if (!rows.length) {
      list.innerHTML = '<div class="security-empty compact"><span>No processes are being watched.</span></div>';
      return;
    }
    list.innerHTML = rows.map(row => `
      <div class="security-whitelist-row" data-watch-id="${esc(row.id || "")}">
        <div class="security-whitelist-main"><strong title="${esc(row.process_key || "")}">${esc(row.label || row.process_key || "Watched process")}</strong><span>Alerts when: ${esc(renderSecurityWatchCondition(row.conditions))} · added ${esc(securityWhen(row.created_at))}</span></div>
        <button type="button">Stop watching</button>
      </div>`).join("");
    list.querySelectorAll("button").forEach(button => button.addEventListener("click", async event => {
      const id = event.currentTarget.closest("[data-watch-id]")?.dataset.watchId || "";
      event.currentTarget.disabled = true;
      try {
        const result = await api("/api/security/watchlist/remove", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }),
        });
        if (!result?.ok) throw new Error(result?.error || "Could not stop watching this process.");
        renderSecurityOverview(result.overview);
        toast("Stopped watching this process.", "ok", 1800);
      } catch (error) {
        event.currentTarget.disabled = false;
        toast(error.message || String(error), "warn", 3500);
      }
    }));
  }

  function renderSecurityAttention(attention, environment = {}) {
    const popup = $("#security-attention");
    const body = $("#security-attention-body");
    if (!popup || !body) return;
    const alerts = Array.isArray(attention?.alerts) ? attention.alerts : [];
    const elevated = environment?.elevated === true
      ? "Administrator rights: elevated collectors available."
      : "Running without admin — elevated sources (Security log, some services) stay limited.";
    if (!alerts.length) {
      popup.hidden = true;
      body.innerHTML = "";
      return;
    }
    body.innerHTML = alerts.map(alert => `
      <div class="security-attention-item" data-alert-id="${esc(alert.id || "")}" data-severity="${esc(alert.severity || "info")}">
        <span class="security-alert-severity">${esc(alert.severity || "info")}${Number.isFinite(Number(alert.confidence)) ? ` · ${Number(alert.confidence)}% confidence` : ""}</span>
        <div class="security-attention-copy">
          <strong>${esc(alert.title || "Security event")}</strong>
          <p>${esc(alert.detail || "")}</p>
          ${alert.injection ? '<span class="security-attention-tag">possible prompt injection</span>' : ""}
        </div>
        <div class="security-attention-item-actions">
          <button type="button" class="btn sm accent" data-attention-investigate>Ask agent</button>
        </div>
      </div>`).join("") + `<div class="security-attention-env">${esc(elevated)}</div>`;
    body.querySelectorAll("[data-attention-investigate]").forEach(button => button.addEventListener("click", event => {
      const alertId = event.currentTarget.closest("[data-alert-id]")?.dataset.alertId || "";
      popup.hidden = true;
      ackSecurityAttention(true);
      openSecurityInvestigation(alertId, event.currentTarget);
    }));
    popup.hidden = false;
  }

  async function ackSecurityAttention(silent = false) {
    try {
      const result = await api("/api/security/attention/ack", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}),
      });
      if (result?.overview) renderSecurityOverview(result.overview);
      if (!silent) toast("Alert acknowledged. The finding stays in the overview list.", "ok", 2400);
    } catch (_) { /* non-fatal */ }
  }

  let securityWatchProcesses = [];
  let pendingWatchProcess = null;

  function closeSecurityWatchDialog() {
    pendingWatchProcess = null;
    if ($("#security-watch-scrim")) $("#security-watch-scrim").hidden = true;
  }

  function syncSecurityWatchConfirm() {
    const confirm = $("#security-watch-confirm");
    if (!confirm) return;
    const hasProcess = !!pendingWatchProcess;
    const hasAck = !!$("#security-watch-ack")?.checked;
    const hasCondition = ["watch-cond-running", "watch-cond-network", "watch-cond-listener", "watch-cond-unsigned", "watch-cond-hours"]
      .some(id => $(`#${id}`)?.checked);
    confirm.disabled = !(hasProcess && hasAck && hasCondition);
  }

  function renderSecurityWatchOptions() {
    const list = $("#security-watch-options");
    if (!list) return;
    const query = ($("#security-watch-search")?.value || "").trim().toLowerCase();
    const rows = securityWatchProcesses.filter(process =>
      !query || `${process.name} ${process.path}`.toLowerCase().includes(query));
    if (!rows.length) {
      list.innerHTML = `<div class="security-empty compact"><span>${securityWatchProcesses.length ? "No running process matches that search." : "Could not list running processes."}</span></div>`;
      syncSecurityWatchConfirm();
      return;
    }
    list.innerHTML = rows.slice(0, 250).map(process => `
      <button type="button" class="security-watch-option${pendingWatchProcess?.key === `name:${String(process.name).toLowerCase()}` ? " selected" : ""}" data-process-name="${esc(process.name)}" data-process-path="${esc(process.path || "")}">
        <strong>${esc(process.name)}</strong>
        <span>${esc(process.path || "system process")}<em> · pid ${esc(process.pid || "?")}</em></span>
      </button>`).join("");
    list.querySelectorAll("[data-process-name]").forEach(button => button.addEventListener("click", () => {
      const name = button.dataset.processName || "";
      pendingWatchProcess = {
        key: `name:${String(name).toLowerCase()}`,
        label: name,
        path: button.dataset.processPath || "",
      };
      renderSecurityWatchOptions();
      syncSecurityWatchConfirm();
    }));
  }

  async function openSecurityWatchDialog() {
    const scrim = $("#security-watch-scrim");
    if (!scrim) return;
    pendingWatchProcess = null;
    $("#security-watch-search").value = "";
    $("#security-watch-ack").checked = false;
    $("#watch-cond-running").checked = true;
    ["watch-cond-network", "watch-cond-listener", "watch-cond-unsigned", "watch-cond-hours"].forEach(id => {
      const node = $(`#${id}`);
      if (node) node.checked = false;
    });
    scrim.hidden = false;
    syncSecurityWatchConfirm();
    $("#security-watch-options").innerHTML = '<div class="security-empty compact"><span>Loading running processes…</span></div>';
    try {
      const result = await api("/api/security/processes");
      securityWatchProcesses = Array.isArray(result?.processes) ? result.processes : [];
      if (result?.error) toast(`Process list limited: ${result.error}`, "info", 3200);
    } catch (error) {
      securityWatchProcesses = [];
      toast(error.message || String(error), "warn", 3200);
    }
    renderSecurityWatchOptions();
    $("#security-watch-search")?.focus();
  }

  async function submitSecurityWatch() {
    if (!pendingWatchProcess) return;
    const when = [];
    if ($("#watch-cond-running")?.checked) when.push("running");
    if ($("#watch-cond-network")?.checked) when.push("network");
    if ($("#watch-cond-listener")?.checked) when.push("new_listener");
    if ($("#watch-cond-unsigned")?.checked) when.push("unsigned");
    let hours;
    if ($("#watch-cond-hours")?.checked) {
      when.push("outside_hours");
      hours = {
        start: Math.max(0, Math.min(23, Number($("#watch-hours-start")?.value || 0))),
        end: Math.max(0, Math.min(23, Number($("#watch-hours-end")?.value || 0))),
      };
    }
    if (!when.length) return;
    const button = $("#security-watch-confirm");
    if (button) button.disabled = true;
    try {
      const result = await api("/api/security/watchlist", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          process_key: pendingWatchProcess.key,
          label: pendingWatchProcess.label,
          conditions: { when, ...(hours ? { hours } : {}) },
          acknowledge_compute: true,
        }),
      });
      if (!result?.ok) throw new Error(result?.error || "Could not start watching this process.");
      closeSecurityWatchDialog();
      renderSecurityOverview(result.overview);
      toast("Watching this process in the background. Alerts follow the conditions you set.", "ok", 3200);
    } catch (error) {
      toast(error.message || String(error), "warn", 4200);
    } finally {
      syncSecurityWatchConfirm();
    }
  }

  async function loadSecurityOverview() {
    try {
      const data = await api("/api/security/overview");
      if (data?.error) throw new Error(data.error);
      renderSecurityOverview(data);
      return data;
    } catch (error) {
      const status = $("#security-scan-status");
      if (status) {
        status.hidden = false;
        status.querySelector("strong").textContent = "Security Overview unavailable";
        $("#security-scan-stage").textContent = error.message || String(error);
      }
      finishSecurityScanIntro(true);
      return null;
    }
  }

  function clearSecurityPoll() {
    if (securityPollTimer) clearTimeout(securityPollTimer);
    securityPollTimer = null;
  }

  function scheduleSecurityPoll() {
    clearSecurityPoll();
    securityPollTimer = setTimeout(async () => {
      if (!$("#security-page")?.classList.contains("open")) return;
      const data = await loadSecurityOverview();
      if (data?.scan?.status === "scanning") scheduleSecurityPoll();
    }, 1400);
  }

  async function refreshSecurityOverview() {
    const refresh = $("#btn-refresh-security");
    if (refresh) refresh.disabled = true;
    startSecurityScanIntro("Preparing local collectors");
    try {
      const result = await api("/api/security/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (result?.error) throw new Error(result.error);
      await loadSecurityOverview();
      scheduleSecurityPoll();
    } catch (error) {
      if (refresh) refresh.disabled = false;
      finishSecurityScanIntro(true);
      toast(`Security scan: ${error.message || error}`, "warn", 4200);
    }
  }

  async function updateSecurityAnalysis() {
    const button = $("#btn-security-analyze");
    if (button) button.disabled = true;
    try {
      const result = await api("/api/security/summarize", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!result?.ok) throw new Error(result?.error || "Analysis was unavailable.");
      renderSecurityOverview(result.overview);
    } catch (error) {
      toast(error.message || String(error), "info", 4200);
    } finally {
      if (button) button.disabled = false;
    }
  }

  function openSecurityWhitelist(alertId) {
    const alert = [...(securityOverviewData?.alerts || []), ...(securityOverviewData?.whitelisted_activity || [])]
      .find(item => item.id === alertId);
    if (!alert) return;
    pendingWhitelistAlertId = alertId;
    $("#security-whitelist-title").textContent = `Whitelist ${alert.entity_label || "this application"}?`;
    $("#security-whitelist-copy").textContent = "Routine alerts for this identity will be hidden. Materially new behaviour can still bring it back for review.";
    $("#security-whitelist-reason").value = "";
    $("#security-whitelist-scrim").hidden = false;
    setTimeout(() => $("#security-whitelist-reason")?.focus(), 0);
  }

  function closeSecurityWhitelist() {
    pendingWhitelistAlertId = "";
    if ($("#security-whitelist-scrim")) $("#security-whitelist-scrim").hidden = true;
  }

  async function confirmSecurityWhitelist() {
    if (!pendingWhitelistAlertId) return;
    const button = $("#security-whitelist-confirm");
    if (button) button.disabled = true;
    try {
      const result = await api("/api/security/whitelist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_id: pendingWhitelistAlertId, reason: $("#security-whitelist-reason")?.value || "" }),
      });
      if (!result?.ok) throw new Error(result?.error || "Could not whitelist this application.");
      closeSecurityWhitelist();
      renderSecurityOverview(result.overview);
      toast("Application whitelisted. Routine alerts are now quiet.", "ok", 2600);
    } catch (error) {
      toast(error.message || String(error), "warn", 4200);
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function openSecurityInvestigation(alertId, button) {
    if (!alertId || button?.disabled) return;
    if (button) {
      button.disabled = true;
      button.textContent = "Opening...";
    }
    try {
      const chat = await api("/api/chats", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ origin: "security", security_alert_id: alertId }),
      });
      if (!chat?.id) throw new Error(chat?.error || "Could not open an investigation session.");
      closeSecurityOverview();
      await loadChats();
      await selectChat(chat.id);
      const input = $("#composer-input");
      if (input) {
        input.value = "";
        autoResize(input);
        input.focus();
      }
      toast("Investigation session ready. Ask what you want to check.", "ok", 2600);
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.textContent = "Investigate";
      }
      toast(error.message || String(error), "warn", 4200);
    }
  }

  // Too much to show in the settings window, so the overview keeps its own
  // full page; its schedule stays in Settings > Security scans.
  async function openSecurityOverview() {
    const page = $("#security-page");
    if (!page) return;
    closeFaq();
    closeSettings();
    page.classList.add("open");
    page.setAttribute("aria-hidden", "false");
    const data = await loadSecurityOverview();
    const stale = !data?.generated_at || (Date.now() / 1000 - Number(data.generated_at)) > 90;
    if (stale && data?.scan?.status !== "scanning") refreshSecurityOverview();
    // Opening the overview counts as seeing the alert — clear the popup.
    if (data?.attention?.alerts?.length) ackSecurityAttention(true);
  }

  function closeSecurityOverview() {
    const page = $("#security-page");
    if (!page) return;
    clearSecurityPoll();
    hideSecurityScanIntro(true);
    closeSecurityWhitelist();
    page.classList.remove("open");
    page.setAttribute("aria-hidden", "true");
  }

  function filterFaq(query = "") {
    const normalized = String(query).trim().toLowerCase();
    let visibleItems = 0;
    $$("#faq-page .faq-item").forEach(item => {
      const visible = !normalized || item.textContent.toLowerCase().includes(normalized);
      item.hidden = !visible;
      if (visible) visibleItems += 1;
    });
    $$("#faq-page [data-faq-section]").forEach(section => {
      section.hidden = !section.querySelector(".faq-item:not([hidden])");
    });
    const empty = $("#faq-empty");
    if (empty) empty.hidden = visibleItems !== 0;
    const note = $("#faq-results-note");
    if (note) note.textContent = normalized
      ? `${visibleItems} ${visibleItems === 1 ? "answer" : "answers"} found`
      : "";
  }

  function syncFaqNav() {
    const scroller = $(".faq-page-scroll");
    if (!scroller) return;
    const sections = [...$$("#faq-page [data-faq-section]:not([hidden])")];
    if (!sections.length) return;
    const top = scroller.getBoundingClientRect().top + 110;
    let active = sections[0];
    for (const section of sections) {
      if (section.getBoundingClientRect().top <= top) active = section;
    }
    $$("#faq-page [data-faq-nav]").forEach(link => {
      link.classList.toggle("active", link.dataset.faqNav === active.id);
    });
  }

  function openFaq() {
    const page = $("#faq-page");
    if (!page) return;
    closeUsageStats();
    closeSecurityOverview();
    closeSettings();
    page.classList.add("open");
    page.setAttribute("aria-hidden", "false");
    requestAnimationFrame(syncFaqNav);
  }

  // Open the FAQ on one answer: clear any search that would hide it, expand
  // it and bring it into view.
  function openFaqAt(itemId) {
    const search = $("#faq-search");
    if (search?.value) { search.value = ""; filterFaq(""); }
    openFaq();
    const item = document.getElementById(itemId);
    if (!item) return;
    item.open = true;
    requestAnimationFrame(() => item.scrollIntoView({ block: "center" }));
  }

  function closeFaq() {
    const page = $("#faq-page");
    if (!page) return;
    page.classList.remove("open");
    page.setAttribute("aria-hidden", "true");
  }

  async function refreshGitHubStatus() {
    const badge = $("#github-conn-status");
    if (!badge) return;
    try {
      const r = await api("/api/github/status");
      if (r && r.connected && r.login) {
        badge.textContent = `connected as ${r.login}`;
        badge.style.color = "var(--mint-3)";
      } else {
        badge.textContent = "not connected";
        badge.style.color = "";
      }
    } catch {
      badge.textContent = "not connected";
      badge.style.color = "";
    }
  }

  // Collapse the finished work (thinking + tools) under one "Worked for Xs"
  // line, only when tools ran; a pure Q&A turn keeps "Thought for Xs". The
  // think-header caret folds both.
  function collapseWorkBlock(row) {
    if (!row) return;
    const col = row.querySelector(".bubble-col");
    const think = row.querySelector(".think-container");
    const group = col && col.querySelector(".tool-group");
    if (!think || !group) return;
    const title = think.querySelector(".think-title");
    const secs = row._workStart ? Math.max(1, Math.round((Date.now() - row._workStart) / 1000)) : 0;
    if (title && secs > 0) {
      const fmt = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : `${secs}s`;
      // The tool card underneath already says how many tools ran.
      title.textContent = `Worked for ${fmt}`;
      title.classList.remove("shimmer");
    }
    think.classList.add("has-worklog");
    const caret = think.querySelector(".think-caret");
    if (caret) caret.className = `ph ph-caret-${group.classList.contains("collapsed") ? "right" : "down"} think-caret`;
  }
  return {
    hideSecurityScanIntro, renderSecurityAlerts, ackSecurityAttention, closeSecurityWatchDialog,
    syncSecurityWatchConfirm, renderSecurityWatchOptions, openSecurityWatchDialog,
    submitSecurityWatch, loadSecurityOverview, refreshSecurityOverview, updateSecurityAnalysis,
    closeSecurityWhitelist, confirmSecurityWhitelist, openSecurityOverview, closeSecurityOverview,
    filterFaq, syncFaqNav, openFaq, openFaqAt, closeFaq, refreshGitHubStatus, collapseWorkBlock,
  };
};
