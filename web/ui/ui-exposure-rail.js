/* SPDX-License-Identifier: AGPL-3.0-only */
/* frontend exposure rail: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["exposure-rail"] = ({ esc }) => {
  // A frontend audit is one bounded read-only pass, not a four-stage attack.
  // Its tape (rt-visuals.js) streams the site's JavaScript past a read head
  // while scanning, then settles into a durable result that stays with the
  // completed answer.
  function secretRail(row) {
    return (row && row.querySelector(".secret-rail"))
      || document.querySelector("#revealer-deck .secret-rail");
  }
  function isFrontendSecretAudit(row) {
    return row?._rtEngagement === "frontend_secrets"
      || document.querySelector("#revealer-deck")?.dataset.rtEngagement === "frontend_secrets";
  }
  function ensureSecretRail(row, mission = {}) {
    if (!row) return null;
    let rail = secretRail(row);
    if (rail) {
      const target = rail.querySelector(".sr-target");
      if (target && mission.target) target.textContent = mission.target;
      return rail;
    }
    const deck = document.querySelector("#revealer-deck");
    const toolStack = row.querySelector(".tool-stack");
    rail = document.createElement("div");
    rail.className = "secret-rail is-idle";
    rail.dataset.outcome = "pending";
    rail.setAttribute("role", "status");
    rail.setAttribute("aria-live", "polite");
    rail.innerHTML =
      `<span class="sr-sigil" aria-hidden="true">` +
        `<svg viewBox="0 0 32 32" focusable="false">` +
          `<rect class="sr-icon-frame" x="3.5" y="4.5" width="25" height="23" rx="5"></rect>` +
          `<path class="sr-icon-header" d="M4 11h24"></path>` +
          `<circle class="sr-icon-dot" cx="8" cy="7.8" r="1"></circle>` +
          `<circle class="sr-icon-dot" cx="11.5" cy="7.8" r="1"></circle>` +
          `<path class="sr-icon-code" d="m12.5 16.5-3 3 3 3m7-6 3 3-3 3m-2-7-2.2 9"></path>` +
        `</svg>` +
      `</span>` +
      `<div class="sr-body">` +
        `<div class="sr-title"><span class="sr-kicker">Frontend attack surface</span>` +
          `<strong class="sr-target">${esc(mission.target || "Authorized page")}</strong></div>` +
        `<div class="sr-spectrum" aria-hidden="true"><canvas class="sr-tape"></canvas></div>` +
        `<div class="sr-detail"><span class="sr-live-dot"></span>` +
          `<span class="sr-activity">Ready to inspect bundles, chunks and source maps</span></div>` +
      `</div>` +
      `<div class="sr-readout"><span class="sr-state">Ready</span>` +
        `<strong class="sr-result">One-pass audit</strong>` +
        `<small class="sr-meta">JavaScript · lazy chunks · maps</small></div>`;
    if (deck) {
      rail.classList.add("revealer-card");
      deck.appendChild(rail);
    } else if (toolStack?.parentNode) {
      toolStack.parentNode.insertBefore(rail, toolStack);
    } else {
      row.appendChild(rail);
    }
    rail._viz = window.AccRtViz?.tape(rail.querySelector(".sr-tape")) || null;
    return rail;
  }
  function secretRailToolStart(row, name) {
    if (name !== "scan_js_secrets" || !isFrontendSecretAudit(row)) return;
    const rail = ensureSecretRail(row);
    if (!rail) return;
    rail.dataset.outcome = "running";
    rail.classList.remove("is-idle", "is-complete", "is-error", "is-stopped", "has-findings", "has-candidates");
    rail.classList.add("is-running");
    const stateEl = rail.querySelector(".sr-state");
    const resultEl = rail.querySelector(".sr-result");
    const activity = rail.querySelector(".sr-activity");
    if (stateEl) stateEl.textContent = "Live analysis";
    if (resultEl) resultEl.textContent = "Scanning";
    if (activity) activity.textContent = "Inspecting bundles, lazy chunks and source maps";
    rail._viz?.set("running");
  }
  function secretRailToolResult(row, name, result) {
    if (name !== "scan_js_secrets" || !isFrontendSecretAudit(row)) return;
    const rail = ensureSecretRail(row);
    if (!rail) return;
    const failed = !result || !!result.error;
    const verified = Math.max(0, Number(result?.verified_structure_count || 0));
    const high = Math.max(0, Number(result?.high_confidence_count || 0));
    const confirmed = verified + high;
    const candidates = result?.candidates_included
      ? Math.max(0, Number(result?.candidate_count || 0)) : 0;
    const files = Math.max(0, Number(result?.files_scanned || 0));
    const publicIds = Math.max(0, Number(result?.public_identifier_count || 0));
    rail.dataset.outcome = failed ? "error" : "complete";
    rail.classList.remove("is-idle", "is-running", "is-stopped");
    rail.classList.toggle("is-error", failed);
    rail.classList.toggle("is-complete", !failed);
    rail.classList.toggle("has-findings", confirmed > 0);
    rail.classList.toggle("has-candidates", confirmed === 0 && candidates > 0);
    const stateEl = rail.querySelector(".sr-state");
    const resultEl = rail.querySelector(".sr-result");
    const activity = rail.querySelector(".sr-activity");
    const meta = rail.querySelector(".sr-meta");
    if (failed) {
      if (stateEl) stateEl.textContent = "Incomplete";
      if (resultEl) resultEl.textContent = "Scan failed";
      if (activity) activity.textContent = "The audit stopped before a reliable result was produced";
      if (meta) meta.textContent = "Review the error below";
      rail._viz?.set("error");
      return;
    }
    // findings stay marked on the tape: confirmed in danger, candidates in warning
    rail._viz?.set("complete", {
      marks: confirmed || candidates,
      tone: confirmed ? "danger" : candidates ? "warning" : "success",
    });
    if (stateEl) stateEl.textContent = confirmed || candidates ? "Review ready" : "Complete";
    if (resultEl) resultEl.textContent = confirmed
      ? `${confirmed} confirmed`
      : candidates ? `${candidates} candidate${candidates === 1 ? "" : "s"}` : "Clear";
    if (activity) activity.textContent = confirmed
      ? `Exact values ready for reporting · ${files} file${files === 1 ? "" : "s"} inspected`
      : candidates
        ? `No confirmed exposures · ${candidates} candidate${candidates === 1 ? "" : "s"} separated`
        : `No high-confidence exposures · ${files} file${files === 1 ? "" : "s"} inspected`;
    const endpoints = Math.max(0, Number(result?.endpoint_count || 0));
    const recovered = Math.max(0, Number(result?.source_map_reconstruction?.recovered_file_count || 0));
    const surfaceBits = [];
    if (endpoints) surfaceBits.push(`${endpoints} endpoint${endpoints === 1 ? "" : "s"} mapped`);
    if (recovered) surfaceBits.push(`${recovered} source file${recovered === 1 ? "" : "s"} recovered`);
    const baseMeta = publicIds
      ? `${publicIds} public identifier${publicIds === 1 ? "" : "s"} kept as context`
      : "No provider validation performed";
    if (meta) meta.textContent = surfaceBits.length
      ? `${baseMeta} · ${surfaceBits.join(" · ")}`
      : baseMeta;
  }
  function secretRailMission(row, mission) {
    if (!row || mission?.engagement !== "frontend_secrets") return;
    row._rtEngagement = "frontend_secrets";
    const deck = document.querySelector("#revealer-deck");
    if (deck) deck.dataset.rtEngagement = "frontend_secrets";
    const rail = ensureSecretRail(row, mission);
    if (!rail || mission.status !== "closed" || rail.dataset.outcome !== "pending") return;
    rail.dataset.outcome = "stopped";
    rail.classList.remove("is-idle", "is-running");
    rail.classList.add("is-stopped");
    const stateEl = rail.querySelector(".sr-state");
    const resultEl = rail.querySelector(".sr-result");
    const activity = rail.querySelector(".sr-activity");
    if (stateEl) stateEl.textContent = "Stopped";
    if (resultEl) resultEl.textContent = "No scan result";
    if (activity) activity.textContent = "The one-pass audit ended before the scanner returned";
    rail._viz?.set("stopped");
  }
  function secretRailFinalize(row) {
    const rail = secretRail(row);
    if (!rail || !rail.classList.contains("is-running")) return;
    rail.dataset.outcome = "stopped";
    rail.classList.remove("is-running");
    rail.classList.add("is-stopped");
    const stateEl = rail.querySelector(".sr-state");
    const resultEl = rail.querySelector(".sr-result");
    const activity = rail.querySelector(".sr-activity");
    if (stateEl) stateEl.textContent = "Stopped";
    if (resultEl) resultEl.textContent = "No result";
    if (activity) activity.textContent = "The audit ended before the scanner completed";
    rail._viz?.set("stopped");
  }
  return {
    isFrontendSecretAudit, secretRailToolStart, secretRailToolResult, secretRailMission,
    secretRailFinalize,
  };
};
