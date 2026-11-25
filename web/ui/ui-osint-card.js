/* SPDX-License-Identifier: AGPL-3.0-only */
/* OSINT recon card (passive intel gathering): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["osint-card"] = ({ esc }) => {
  // The calm counterpart to the attack-chain rail. OSINT accumulates
  // intelligence into buckets rather than marching toward "breach", so this is a
  // grid of categories that fill with counts. Theme accent, no red. Only passive
  // recon/OSINT tools summon it; active exploitation stays with the attack rail.
  const OSINT_CATS = [
    { key: "registry", label: "Registry", tools: ["recon_rdap"] },
    { key: "dns", label: "DNS", tools: ["recon_dns"] },
    { key: "certs", label: "Certificates", tools: ["recon_subdomains"] },
    { key: "records", label: "Public record",
      tools: ["recon_web_archive", "recon_shodan", "recon_cve_match", "web_search"] },
  ];
  function osintCatForTool(name) {
    if (!name) return null;
    if (name.startsWith("mcp_osint")) return "records";
    for (const c of OSINT_CATS) if (c.tools.includes(name)) return c.key;
    return null;
  }
  // ensureOsintCard appends the card to #revealer-deck, so result/finalize
  // callers must look there too, not only inside `row`.
  function osintCard(row) {
    return (row && row.querySelector(".osint-card")) || document.querySelector("#revealer-deck .osint-card");
  }
  // Best-effort "how much did this source return" — biggest array or a count-ish
  // number in the result. Falls back to 1 (one source queried, nothing counted).
  function osintFindingCount(res) {
    if (!res || typeof res !== "object" || res.error) return 0;
    let best = 0;
    for (const k of ["count", "total", "found", "matches", "hits", "records"]) {
      if (typeof res[k] === "number") best = Math.max(best, res[k]);
    }
    for (const v of Object.values(res)) if (Array.isArray(v)) best = Math.max(best, v.length);
    return best > 0 ? best : 1;
  }
  function isPassiveOsint(row) {
    return row?._rtEngagement === "passive_osint"
      || document.querySelector("#revealer-deck")?.dataset.rtEngagement === "passive_osint";
  }
  function ensureOsintCard(row, mission = {}) {
    if (!row) return null;
    let card = row.querySelector(".osint-card") || document.querySelector("#revealer-deck .osint-card");
    if (card) {
      const target = card.querySelector(".oc-target-title");
      if (target && mission.target) target.textContent = mission.target;
      return card;
    }
    const toolStack = row.querySelector(".tool-stack");
    card = document.createElement("div");
    card.className = "osint-card is-running";
    card.dataset.total = "0";
    const tiles = OSINT_CATS.map(c =>
      `<div class="oc-cat is-empty" data-cat="${c.key}">` +
        `<span class="oc-source-dot"></span>` +
        `<span class="oc-cat-body"><span class="oc-cat-label">${c.label}</span>` +
        `<span class="oc-cat-count">Waiting</span></span>` +
        `<canvas class="oc-matrix" aria-hidden="true"></canvas>` +
      `</div>`
    ).join("");
    card.innerHTML =
      `<div class="oc-head">` +
        `<div class="oc-heading"><span class="oc-kicker">Passive intelligence</span>` +
        `<span class="oc-target-title">${esc(mission.target || "Public-source collection")}</span></div>` +
        `<span class="oc-boundary"><i class="ph ph-eye-slash"></i>No direct target traffic</span>` +
      `</div>` +
      `<div class="oc-grid">${tiles}</div>` +
      `<div class="oc-foot"><span class="oc-live"><span class="oc-live-dot live"></span><span class="oc-live-text">Collecting</span></span>` +
        `<span class="oc-activity">Preparing public sources</span>` +
        `<span class="oc-total"><span class="oc-total-n">0</span> records</span></div>`;
    
    const deck = document.getElementById("revealer-deck");
    if (deck) {
      card.classList.add("revealer-card");
      deck.appendChild(card);
    } else {
      if (toolStack && toolStack.parentNode) toolStack.parentNode.insertBefore(card, toolStack);
      else row.appendChild(card);
    }
    // one dot matrix per source, filling as its records come in
    card._viz = {};
    card.querySelectorAll(".oc-cat").forEach(tile => {
      card._viz[tile.dataset.cat] = window.AccRtViz?.bucket(tile.querySelector(".oc-matrix"), tile.dataset.cat) || null;
    });
    return card;
  }
  // Which sub-mode a recon tool is running, so the activity line says what is
  // happening (a zone transfer reads differently from a lookup). Mirrors the
  // recon tools' modes/params in bridge.py.
  function reconModeLabel(name, args) {
    args = args || {};
    if (args.mode) return " · " + String(args.mode);
    if (name === "recon_injection_probe" && Array.isArray(args.checks) && args.checks.length) {
      return " · " + args.checks.join("+").slice(0, 24);
    }
    if (name === "recon_check_exposure" && (args.check_s3 || args.buckets)) return " · s3";
    if (name === "recon_dns" && args.loud) return " · axfr";
    return "";
  }
  function osintCardToolStart(row, name, args) {
    if (!isPassiveOsint(row)) return;
    const cat = osintCatForTool(name);
    if (!cat) return;
    const card = ensureOsintCard(row);
    if (!card) return;
    card.classList.add("is-running");
    card.querySelector(".oc-live-dot")?.classList.add("live");
    const liveText = card.querySelector(".oc-live-text");
    if (liveText) liveText.textContent = "Collecting";
    card.querySelectorAll(".oc-cat").forEach(t => t.classList.toggle("is-active", t.dataset.cat === cat));
    Object.entries(card._viz || {}).forEach(([key, viz]) => viz?.setActive(key === cat));
    const act = card.querySelector(".oc-activity");
    if (act) {
      const tgt = String((args && (args.url || args.target || args.domain || args.host || args.query)) || "").replace(/^https?:\/\//, "");
      const base = name.startsWith("mcp_osint") ? name.replace(/^mcp_osint_?/, "osint · ") : name;
      const clean = base + reconModeLabel(name, args);
      act.textContent = tgt ? `${clean} · ${tgt.slice(0, 40)}` : clean;
    }
  }
  function osintCardToolResult(row, name, res) {
    const cat = osintCatForTool(name);
    if (!cat) return;
    const card = osintCard(row);
    if (!card) return;
    const tile = card.querySelector(`.oc-cat[data-cat="${cat}"]`);
    if (!tile) return;
    const add = (res && res.error) ? 0 : osintFindingCount(res);
    const next = parseInt(tile.dataset.n || "0", 10) + add;
    tile.dataset.n = String(next);
    const countEl = tile.querySelector(".oc-cat-count");
    if (countEl) countEl.textContent = add > 0 ? `${next} found` : "Checked";
    tile.classList.remove("is-empty", "is-active");
    card._viz?.[cat]?.setActive(false);
    card._viz?.[cat]?.setCount(next);
    if (next > 0) tile.classList.add("has-data");
    if (add > 0) { tile.classList.remove("just-hit"); void tile.offsetWidth; tile.classList.add("just-hit"); }
    const total = parseInt(card.dataset.total || "0", 10) + add;
    card.dataset.total = String(total);
    const tn = card.querySelector(".oc-total-n");
    if (tn) tn.textContent = String(total);
  }
  function osintCardFinalize(row) {
    const card = osintCard(row);
    if (!card) return;
    card.classList.remove("is-running");
    card.classList.add("is-done");
    card.querySelector(".oc-live-dot")?.classList.remove("live");
    const liveText = card.querySelector(".oc-live-text");
    if (liveText) liveText.textContent = "Complete";
    card.querySelectorAll(".oc-cat.is-active").forEach(t => t.classList.remove("is-active"));
    Object.values(card._viz || {}).forEach(viz => viz?.setActive(false));
    const act = card.querySelector(".oc-activity");
    if (act) act.textContent = parseInt(card.dataset.total || "0", 10) > 0 ? "Public-source review complete" : "No public records returned";
  }

  function passiveIntelMission(row, mission) {
    if (!row || mission?.engagement !== "passive_osint") return;
    row._rtEngagement = "passive_osint";
    const deck = document.querySelector("#revealer-deck");
    if (deck) deck.dataset.rtEngagement = "passive_osint";
    const card = ensureOsintCard(row, mission);
    if (card && mission.status === "closed") osintCardFinalize(row);
  }
  return {
    isPassiveOsint, osintCardToolStart, osintCardToolResult, osintCardFinalize, passiveIntelMission,
  };
};
