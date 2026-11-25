/* SPDX-License-Identifier: AGPL-3.0-only */
/* Installing the Maya engine: the row in Settings > Model (only on PCs that can run
   it), and the offer on a "made for the Maya engine" warning, which loads the model
   once Maya is in. The bridge downloads, checks and switches (/api/maya/install). */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["maya-install"] = ({ api, confirmModal, loadModelByPath, refreshModels, toast }) => {
  const RUNNING = new Set(["checking", "downloading", "extracting", "testing", "waiting", "switching"]);
  let last = null;
  let pollTimer = 0;
  let checkedOnce = false;
  let loadAfter = "";
  const byId = (id) => document.getElementById(id);

  function progressText(job) {
    switch (job.status) {
      case "checking": return "Looking up the newest Maya…";
      case "downloading": {
        const pct = job.bytes_total ? Math.floor((100 * (job.bytes_written || 0)) / job.bytes_total) : 0;
        return `Downloading Maya ${job.release || ""}… ${pct}%${job.speed ? ` · ${job.speed}` : ""}`;
      }
      case "extracting": return "Unpacking…";
      case "testing": return "Checking every file and starting the engine once…";
      case "waiting": return "Waiting for the current reply to finish…";
      case "switching": return "Restarting your model on the new Maya…";
      default: return "";
    }
  }

  function render(st) {
    last = st;
    const row = byId("maya-install-row");
    const state = byId("maya-state");
    const btn = byId("btn-maya-install");
    const hint = byId("maya-install-hint");
    if (!row || !state || !btn || !hint) return;
    const inst = st.installed || {};
    row.hidden = !(st.supported && (st.eligible || inst.kind));
    const job = st.job || {};
    const running = RUNNING.has(job.status);
    state.textContent = inst.kind === "package" ? `Maya ${inst.version}`
      : inst.kind === "checkout" ? "your own build" : "not installed";
    state.title = inst.root || "";
    btn.hidden = inst.kind === "checkout";
    btn.disabled = running || !st.eligible || st.offline;
    const size = st.download_mb ? ` (${st.download_mb} MB)` : "";
    btn.querySelector("span").textContent = running ? "Installing…"
      : !inst.kind ? `Install${size}`
      : st.available ? `Update to ${st.latest}` : "Check for update";
    let text = "";
    if (running) text = progressText(job);
    else if (["done", "failed", "up_to_date"].includes(job.status) && job.message) text = job.message;
    else if (inst.kind === "checkout") text = `Using the Maya you built in ${inst.root}.`;
    else if (!st.eligible) text = st.reason || "";
    else if (st.offline) text = "Offline mode is on.";
    else if (st.check_error && !inst.kind) text = `Couldn't look up Maya: ${st.check_error}`;
    else if (!inst.kind) text = "Maya runs Inkling 2 Mini about 2.5 times faster than llama.cpp. Installing takes one click.";
    else if (st.available) text = `Maya ${st.latest} is out. Updating checks it first and keeps the current one if your model doesn't start on it.`;
    else if (st.latest) text = "This is the newest Maya.";
    hint.textContent = text;
    hint.classList.toggle("err", job.status === "failed" && !running);
  }

  async function refresh(check = false) {
    try {
      const st = await api(`/api/maya/install${check ? "?check=1" : ""}`);
      if (st && typeof st === "object") render(st);
      return st;
    } catch {
      return null;
    }
  }

  function watchJob() {
    if (pollTimer) return;
    pollTimer = setInterval(async () => {
      const st = await refresh(false);
      const status = st?.job?.status;
      if (!st || RUNNING.has(status)) return;
      clearInterval(pollTimer);
      pollTimer = 0;
      const msg = st.job.message || "";
      if (status === "done") {
        toast(msg || "Maya installed.", "ok", 8000, "maya-install");
        try { await refreshModels(); } catch {}
        const path = loadAfter;
        loadAfter = "";
        if (path) loadModelByPath(path);
      } else if (status === "failed") {
        toast(msg || "Installing Maya failed.", "err", 15000, "maya-install");
      } else if (status === "up_to_date") {
        toast(msg, "info", 5000, "maya-install");
      }
    }, 1000);
  }

  async function install(thenLoad = "") {
    const st = await refresh(true);
    if (!st) return;
    if (RUNNING.has(st.job?.status)) { watchJob(); return; }
    const inst = st.installed || {};
    if (!st.eligible) { toast(st.reason || "This PC can't run Maya.", "warn", 9000, "maya-install"); return; }
    if (inst.kind && !st.available) {
      toast(inst.kind === "checkout" ? "This PC uses its own Maya build."
        : st.latest ? `Maya is up to date (${st.latest}).` : `Couldn't look up Maya: ${st.check_error || "no package found"}`,
      "info", 6000, "maya-install");
      return;
    }
    if (!st.latest) {
      toast(`Couldn't find a Maya to install: ${st.check_error || "none has been published yet"}.`, "warn", 9000, "maya-install");
      return;
    }
    const size = st.download_mb ? ` (about ${st.download_mb} MB)` : "";
    const ok = await confirmModal({
      title: inst.kind ? `Update Maya to ${st.latest}?` : "Install the Maya engine?",
      message: (inst.kind
        ? `This downloads Maya ${st.latest}${size}, checks it, and restarts your Inkling model on it once any reply in progress has finished. If your model doesn't start on it, the current version comes back.`
        : `This downloads the ready-made Maya engine${size} from Accuretta's releases, checks it and sets it up. Inkling 2 models made for Maya then run on it, about 2.5 times faster than llama.cpp.`),
      confirmText: inst.kind ? "Update" : "Install",
      icon: "ph-download-simple",
    });
    if (!ok) return;
    const res = await api("/api/maya/install", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).catch(() => null);
    if (!res?.ok) {
      toast(res?.error || "Couldn't start installing Maya.", "warn", 8000, "maya-install");
      return;
    }
    loadAfter = thenLoad || "";
    toast(inst.kind ? "Updating Maya…" : "Installing Maya…", "info", 4000, "maya-install");
    watchJob();
  }

  // A model made for Maya while Maya isn't here: say so, with a button that
  // installs it and then loads the model.
  async function offerMayaInstall(name, modelPath) {
    const st = last || await refresh(false);
    const can = st && st.supported && st.eligible;
    const row = toast(`${name} is made for the Maya engine, which isn't installed. llama.cpp can't run `
      + `this file correctly.${can ? "" : st?.reason ? ` ${st.reason}` : ""}`, "warn", 20000, "model-load");
    const right = can ? row?.querySelector?.(".status-tab-right") : null;
    if (right && !right.querySelector("[data-maya-install]")) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "status-action-btn";
      btn.dataset.mayaInstall = "";
      btn.textContent = "Install Maya";
      btn.addEventListener("click", () => install(modelPath));
      right.prepend(btn);
    }
  }

  const btn = byId("btn-maya-install");
  if (btn) {
    btn.addEventListener("click", async () => {
      const inst = last?.installed || {};
      if (!inst.kind || last?.available) return install();
      btn.disabled = true;
      btn.querySelector("span").textContent = "Checking…";
      const st = await refresh(true);
      if (!st) { btn.disabled = false; btn.querySelector("span").textContent = "Check for update"; }
    });
  }
  // Fill the row when Settings shows it (the first time with a look at the
  // releases, cached by the bridge for ten minutes).
  const row = byId("maya-install-row");
  if (row) {
    refresh(false);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver((entries) => {
        if (!entries.some(e => e.isIntersecting)) return;
        refresh(!checkedOnce).then(st => { if (RUNNING.has(st?.job?.status)) watchJob(); });
        checkedOnce = true;
      }).observe(row);
    }
  }

  return { offerMayaInstall };
};
