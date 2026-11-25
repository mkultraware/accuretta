/* SPDX-License-Identifier: AGPL-3.0-only */
/* security assessment progress: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["assessment-progress"] = ({
  esc, isFrontendSecretAudit, isPassiveOsint, state,
}) => {
  // The backend owns phase changes. Tool starts update the activity line only;
  // refused calls cannot advance the engagement or imply access that never happened.
  const ATTACK_NODES = [
    { key: "recon", label: "Recon", icon: "ph-binoculars" },
    { key: "validate", label: "Validate", icon: "ph-seal-check" },
    { key: "exploit", label: "Exploit", icon: "ph-flask" },
    { key: "report", label: "Report", icon: "ph-file-text" },
  ];
  // Mirrors bridge.py's _RT_EXPLOIT_TOOL_NAMES. These active primitives drive
  // the attack rail when they run, including recon_auth_spray (recon_-prefixed,
  // but really an attack).
  const RT_EXPLOIT_TOOLS = new Set([
    "http_request", "recon_auth_spray", "jwt_tool", "sql_injection",
    "fuzz", "batch_probe", "tcp_send",
  ]);
  const RT_WORKFLOW_TOOLS = new Set([
    "validate_finding", "record_finding", "sandbox_sqlmap", "rt_generate_report",
  ]);
  function isExploitTool(name) { return RT_EXPLOIT_TOOLS.has(name); }
  function isRedTeamTool(name) {
    return !!name && (name.startsWith("recon_") || name === "encode_decode"
      || name === "scan_js_secrets" || isExploitTool(name) || RT_WORKFLOW_TOOLS.has(name));
  }
  function isAttackRailEnabled(row) {
    return row?._attackRailEnabled === true;
  }
  // Short path helper: strip scheme+host, keep path+query, cap length.
  function arPath(u, cap = 40) {
    u = String(u || "");
    u = u.replace(/^https?:\/\/[^/]+/i, "") || u;
    return u.length > cap ? u.slice(0, cap) + "…" : u;
  }
  function attackActivitySummary(name, args) {
    args = args || {};
    if (name === "validate_finding") {
      return `validating → ${arPath(args.url || args.target, 52) || "finding"}`;
    }
    if (name === "record_finding") {
      return `evidence ledger → ${String(args.title || "finding").slice(0, 52)}`;
    }
    if (name === "sandbox_sqlmap") {
      return `sqlmap verification → ${arPath(args.url, 48) || "target"}`;
    }
    if (name === "rt_generate_report") {
      return "report → assembling evidence and findings";
    }
    if (name === "scan_js_secrets") {
      return `frontend audit → ${arPath(args.url || args.target, 46) || "page"}`;
    }
    if (name === "http_request") {
      const m = (args.method || "GET").toUpperCase();
      return `http_request → ${m} ${arPath(args.url || args.target, 44) || "/"}`;
    }
    if (name === "encode_decode") {
      return `encode_decode → ${args.scheme || "base64"} ${args.operation || "encode"}`;
    }
    if (name === "sql_injection") {
      const p = args.param ? `${args.param} @ ` : "";
      return `sql_injection → ${p}${arPath(args.url) || "?"}`;
    }
    if (name === "fuzz") {
      const p = args.param ? `${args.param} ` : "";
      return `fuzz → ${p}${arPath(args.url) || "FUZZ"}`;
    }
    if (name === "jwt_tool") {
      return `jwt_tool → ${args.operation || "decode"}`;
    }
    if (name === "batch_probe") {
      const n = Array.isArray(args.targets) ? args.targets.length : 0;
      const cls = args.probe || (Array.isArray(args.payloads) ? "custom" : "probe");
      return `batch_probe → ${cls}${n ? ` ×${n}` : ""}`;
    }
    if (name === "tcp_send") {
      const port = args.port ? `:${args.port}` : "";
      return `tcp_send → ${args.host || args.target || ""}${port}`;
    }
    if (name === "recon_auth_spray") {
      const m = args.mode ? `${args.mode} ` : "";
      return `auth_spray → ${m}${arPath(args.url || args.target) || String(args.url || args.target || "")}`;
    }
    if (name && name.startsWith("recon_")) {
      return `${name} → ${args.url || args.target || args.domain || args.host || ""}`;
    }
    return name || "";
  }
  function fmtElapsed(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    const p = (n) => String(n).padStart(2, "0");
    return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
  }
  // Current-action line. Split the "cmd → target" summary so the arrow + target
  // can be tinted separately (matches the mockup), falling back to plain cmd.
  function setAttackActivity(rail, name, args) {
    const el = rail.querySelector(".ar-activity-text");
    if (!el) return;
    const summary = attackActivitySummary(name, args);
    const cut = summary.indexOf(" → ");
    if (cut >= 0) {
      el.innerHTML =
        `<span class="ar-cmd">${esc(summary.slice(0, cut))}</span>` +
        `<span class="ar-arrow">→</span>` +
        `<span class="ar-target">${esc(summary.slice(cut + 3))}</span>`;
    } else {
      el.innerHTML = `<span class="ar-cmd">${esc(summary)}</span>`;
    }
  }
  function ensureAttackRail(row, mission = {}) {
    if (!row || !isAttackRailEnabled(row)) return null;
    let rail = row.querySelector(".attack-rail") || document.querySelector("#revealer-deck .attack-rail");
    if (rail) {
      const target = rail.querySelector(".ar-target-title");
      if (target && mission.target) target.textContent = mission.target;
      return rail;
    }
    const toolStack = row.querySelector(".tool-stack");
    rail = document.createElement("div");
    rail.className = "attack-rail is-running";
    rail.dataset.phase = mission.phase === "exploit" ? "exploit" : "recon";
    rail.dataset.missionPhase = rail.dataset.phase;
    rail.dataset.status = mission.status || "active";
    rail.dataset.running = "0";
    rail.dataset.flagsraw = "0";
    rail.dataset.start = String(Date.now());
    const nodesHtml = ATTACK_NODES.map((n, i) =>
      `<div class="ar-node is-pending" data-i="${i}" data-phase="${n.key}">` +
        `<span class="ar-dot"><i class="ph ${n.icon}"></i><span class="ar-check"><i class="ph ph-check"></i></span></span>` +
        `<span class="ar-label">${n.label}</span>` +
      `</div>` +
      (i < ATTACK_NODES.length - 1 ? `<span class="ar-seg" data-s="${i}"></span>` : "")
    ).join("");
    rail.innerHTML =
      `<div class="ar-head">` +
        `<div class="ar-heading"><span class="ar-kicker">Security assessment</span>` +
        `<span class="ar-target-title">${esc(mission.target || "Authorized engagement")}</span></div>` +
        // Evidence stays hidden until the backend observes a concrete FLAG.
        `<span class="ar-state"><span class="ar-state-dot"></span><span class="ar-state-text">In progress</span></span>` +
      `</div>` +
      `<div class="ar-summary"><strong class="ar-phase">Recon</strong><canvas class="ar-strip" aria-hidden="true"></canvas><span class="ar-elapsed">00:00:00</span></div>` +
      `<div class="ar-activity"><span class="ar-pulse live"></span><span class="ar-activity-text">Preparing scoped assessment</span></div>` +
      `<details class="ar-details"><summary>Assessment details</summary><div class="ar-track">${nodesHtml}</div><div class="ar-foot">` +
        `<span class="ar-meta ar-flags" hidden>Evidence <strong class="ar-flag-count"></strong></span>` +
      `</div><p class="ar-phase-history"></p><p class="ar-coverage-note">Stages show activity, not verified coverage. Report generation closes the engagement.</p></details>`;
    
    const deck = document.getElementById("revealer-deck");
    if (deck) {
      rail.classList.add("revealer-card");
      deck.appendChild(rail);
    } else {
      if (toolStack && toolStack.parentNode) toolStack.parentNode.insertBefore(rail, toolStack);
      else row.appendChild(rail);
    }
    rail._viz = window.AccRtViz?.phases(rail.querySelector(".ar-strip")) || null;
    // Live elapsed clock freezes when the engagement closes or the turn ends,
    // and the phase track settles with it.
    const tick = () => {
      if (!document.body.contains(rail)) { clearInterval(rail._timer); return; }
      const frozen = rail.dataset.status === "closed" || !state.streaming;
      if (!frozen) rail.dataset.elapsed = String(Date.now() - (+rail.dataset.start || Date.now()));
      const el = rail.querySelector(".ar-elapsed");
      if (el) el.textContent = fmtElapsed(+(rail.dataset.elapsed || 0));
      if (frozen) {
        clearInterval(rail._timer);
        rail._viz?.update({ live: false });
      }
    };
    rail._timer = setInterval(tick, 1000);
    tick();
    renderRail(rail);
    return rail;
  }
  function renderRail(rail) {
    const active = Math.max(0, ATTACK_NODES.findIndex(n => n.key === rail.dataset.phase));
    const closed = rail.dataset.status === "closed";
    const history = rail._phaseHistory || (rail._phaseHistory = []);
    const phase = ATTACK_NODES[active]?.label || "Recon";
    if (history.at(-1) !== phase) history.push(phase);
    const historyEl = rail.querySelector(".ar-phase-history");
    if (historyEl) historyEl.textContent = `Observed phases: ${history.join(" → ")}`;
    rail.querySelectorAll(".ar-node").forEach((el) => {
      const i = +el.dataset.i;
      el.classList.toggle("is-active", !closed && i === active);
      el.classList.toggle("is-done", false);
      el.classList.toggle("is-pending", closed || i !== active);
    });
    rail.querySelectorAll(".ar-seg").forEach((el) => {
      const s = +el.dataset.s;
      el.classList.toggle("is-done", false);
      el.classList.toggle("is-active", !closed && active === s);
    });
    // Keep evidence counts hidden at zero.
    const flagsRaw = +(rail.dataset.flagsraw || 0);
    const flagsWrap = rail.querySelector(".ar-flags");
    if (flagsWrap) flagsWrap.hidden = flagsRaw <= 0;
    const fc = rail.querySelector(".ar-flag-count");
    if (fc) fc.textContent = `${flagsRaw} confirmed`;
    // Status reflects actual work, policy holds, and server-side closure.
    const complete = closed;
    const live = rail.querySelector(".ar-pulse")?.classList.contains("live");
    const phaseEl = rail.querySelector(".ar-phase");
    if (phaseEl) phaseEl.textContent = ATTACK_NODES[active]?.label || "Recon";
    const stateEl = rail.querySelector(".ar-state-text");
    if (stateEl) stateEl.textContent = complete ? "Closed"
      : rail.dataset.state === "guarded" ? "Policy guarded"
      : live ? "In progress" : "Engagement active";
    rail.classList.toggle("has-flags", flagsRaw > 0);
    rail.classList.toggle("is-running", live && !complete);
    rail.classList.toggle("is-guarded", rail.dataset.state === "guarded" && !complete);
    rail.classList.toggle("is-complete", complete);
    rail._viz?.update({
      active,
      visited: history.map(label => ATTACK_NODES.findIndex(n => n.label === label)).filter(i => i >= 0),
      closed: complete,
      guarded: rail.dataset.state === "guarded",
      flags: flagsRaw,
      live: !complete && !!state.streaming,
    });
  }
  function attackRailToolStart(row, name, args) {
    // Tool starts update activity. They do not authorize or complete a phase.
    if (!isAttackRailEnabled(row) || !isRedTeamTool(name)
        || isPassiveOsint(row) || isFrontendSecretAudit(row)) return;
    const rail = ensureAttackRail(row);
    if (!rail) return;
    rail.dataset.state = "active";
    rail.dataset.running = String(+(rail.dataset.running || 0) + 1);
    if (name === "validate_finding" || name === "sandbox_sqlmap") rail.dataset.phase = "validate";
    if (name === "rt_generate_report") rail.dataset.phase = "report";
    setAttackActivity(rail, name, args);
    rail.querySelector(".ar-pulse")?.classList.add("live");
    renderRail(rail);
    rail._viz?.probe("out");
  }
  function attackRailToolResult(row, name, result) {
    if (!isAttackRailEnabled(row) || !isRedTeamTool(name) || isFrontendSecretAudit(row)) return;
    const rail = (row && row.querySelector(".attack-rail"))
      || document.querySelector("#revealer-deck .attack-rail");
    if (!rail) return;
    rail.dataset.running = String(Math.max(0, +(rail.dataset.running || 0) - 1));
    if (+rail.dataset.running === 0) rail.querySelector(".ar-pulse")?.classList.remove("live");
    const failed = !!(result && (result.error || result.ok === false));
    // the response travels back down the track; a held call hits the wall
    rail._viz?.probe(result?.scope_blocked ? "blocked" : failed ? "fail" : "back");
    if (result && result.scope_blocked) {
      rail.dataset.state = "guarded";
      const activity = rail.querySelector(".ar-activity-text");
      if (activity) activity.textContent = "Action held by engagement policy";
    } else if (name === "validate_finding" && (!result || result.likely_real !== true)) {
      rail.dataset.phase = rail.dataset.missionPhase || "recon";
    } else if (name === "sandbox_sqlmap" && (!result || result.injectable !== true)) {
      rail.dataset.phase = rail.dataset.missionPhase || "recon";
    } else if (name === "rt_generate_report") {
      if (result && result.mission_status === "closed") {
        attackRailMission(row, { status: "closed", phase: "report", report: result.report });
        return;
      }
      if (failed) rail.dataset.phase = rail.dataset.missionPhase || "recon";
    } else if (isExploitTool(name) && !failed) {
      rail.dataset.phase = "exploit";
      rail.dataset.missionPhase = "exploit";
    }
    renderRail(rail);
  }
  function attackRailMission(row, mission) {
    if (!isAttackRailEnabled(row)) return;
    const rail = ensureAttackRail(row, mission || {});
    if (!rail) return;
    const target = rail.querySelector(".ar-target-title");
    if (target && mission?.target) target.textContent = mission.target;
    if (mission?.phase === "exploit") {
      rail.dataset.phase = "exploit";
      rail.dataset.missionPhase = "exploit";
    }
    if (mission?.status === "closed") {
      rail.dataset.status = "closed";
      rail.dataset.phase = "report";
      rail.dataset.running = "0";
      rail.querySelector(".ar-pulse")?.classList.remove("live");
      const activity = rail.querySelector(".ar-activity-text");
      if (activity) activity.textContent = mission.report ? "Report ready" : "Engagement closed";
      if (rail._timer) clearInterval(rail._timer);
    }
    renderRail(rail);
  }
  function attackRailFinalize(row) {
    if (!isAttackRailEnabled(row)) return;
    const rail = (row && row.querySelector(".attack-rail"))
      || document.querySelector("#revealer-deck .attack-rail");
    if (!rail || rail.dataset.status === "closed") return;
    rail.dataset.running = "0";
    rail.querySelector(".ar-pulse")?.classList.remove("live");
    renderRail(rail);
    rail._viz?.update({ live: false });
  }
  function attackRailBreach(row, stage) {
    if (!isAttackRailEnabled(row)) return;
    const rail = (row && row.querySelector(".attack-rail")) || document.querySelector("#revealer-deck .attack-rail");
    if (!rail) return;
    const raw = Math.max(1, parseInt(stage, 10) || 1);
    rail.dataset.flagsraw = String(Math.max(+(rail.dataset.flagsraw || 0), raw));
    rail.dataset.phase = "exploit";
    rail.dataset.missionPhase = "exploit";
    const activity = rail.querySelector(".ar-activity-text");
    if (activity) activity.textContent = "Confirmed access evidence captured";
    renderRail(rail);
  }
  return {
    isAttackRailEnabled, ensureAttackRail, renderRail, attackRailToolStart, attackRailToolResult,
    attackRailMission, attackRailFinalize, attackRailBreach,
  };
};
