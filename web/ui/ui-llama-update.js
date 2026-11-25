/* SPDX-License-Identifier: AGPL-3.0-only */
/* Updating llama.cpp: the row in Settings > Model, and the offer on a "needs a newer
   llama.cpp" warning. The bridge downloads, tests and switches (/api/llama/update)
   and puts the old build back when the model doesn't start on the new one. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["llama-update"] = ({ api, confirmModal, refreshModels, toast }) => {
  const RUNNING = new Set(["checking", "downloading", "extracting", "testing", "waiting", "switching"]);
  const KIND_LABELS = { CUDA: "CUDA (NVIDIA)", VULKAN: "Vulkan", CPU: "CPU" };
  let last = null;
  let pollTimer = 0;
  let checkedOnce = false;
  const byId = (id) => document.getElementById(id);

  function progressText(job) {
    switch (job.status) {
      case "checking": return "Looking up the newest llama.cpp…";
      case "downloading": {
        const pct = job.bytes_total ? Math.floor((100 * (job.bytes_written || 0)) / job.bytes_total) : 0;
        return `Downloading ${job.release || "llama.cpp"}… ${pct}%${job.speed ? ` · ${job.speed}` : ""}`;
      }
      case "extracting": return "Unpacking…";
      case "testing": return "Testing the new build…";
      case "waiting": return "Waiting for the current reply to finish…";
      case "switching": return "Restarting your model on the new build…";
      default: return "";
    }
  }

  function render(st) {
    last = st;
    const build = byId("llama-build");
    const btn = byId("btn-llama-update");
    const hint = byId("llama-update-hint");
    if (!build || !btn || !hint) return;
    const job = st.job || {};
    const running = RUNNING.has(job.status);
    const kind = KIND_LABELS[st.kind] || st.kind || "";
    build.textContent = st.path
      ? `${st.build ? `b${st.build}` : "unknown build"}${kind ? ` · ${kind}` : ""}`
      : "not found";
    build.title = st.path || "";
    btn.disabled = running || !st.supported || st.offline || !st.path;
    btn.querySelector("span").textContent = running ? "Updating…"
      : st.available ? `Update to ${st.latest}` : "Check for update";
    let text = "";
    if (running) text = progressText(job);
    else if (!st.supported) text = "Updating from the app works on Windows.";
    else if (st.offline) text = "Offline mode is on.";
    else if (!st.path) text = "No llama.cpp found. The setup downloads one.";
    else if (["done", "failed", "up_to_date"].includes(job.status) && job.message) text = job.message;
    else if (st.check_error) text = `Couldn't check for updates: ${st.check_error}`;
    else if (st.available) text = `${st.latest} is out. Updating tests it first and keeps your current build if your model doesn't start on it.`;
    else if (st.latest) text = "This is the newest llama.cpp.";
    hint.textContent = text;
    hint.classList.toggle("err", job.status === "failed" && !running);
  }

  async function refresh(check = false) {
    try {
      const st = await api(`/api/llama/update${check ? "?check=1" : ""}`);
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
        toast(msg || "llama.cpp updated.", "ok", 8000, "llama-update");
        try { await refreshModels(); } catch {}
      } else if (status === "failed") {
        toast(msg || "The llama.cpp update failed.", "err", 15000, "llama-update");
      } else if (status === "up_to_date") {
        toast(msg, "info", 5000, "llama-update");
      }
    }, 1000);
  }

  async function update() {
    let st = await refresh(true);
    if (!st) return;
    if (RUNNING.has(st.job?.status)) { watchJob(); return; }
    if (!st.available) {
      toast(st.check_error ? `Couldn't check for updates: ${st.check_error}`
        : st.latest ? `llama.cpp is up to date (${st.latest}).` : "Couldn't find a newer llama.cpp.",
      st.check_error ? "warn" : "info", 5000, "llama-update");
      return;
    }
    const size = st.download_mb ? ` (about ${Math.round(st.download_mb)} MB)` : "";
    const ok = await confirmModal({
      title: `Update llama.cpp to ${st.latest}?`,
      message: `This downloads the official ${st.latest} build${size}, tests it, and restarts your model on it `
        + "once any reply in progress has finished. If your model doesn't start on it, your current build comes back.",
      confirmText: "Update",
      icon: "ph-arrow-circle-up",
    });
    if (!ok) return;
    const res = await api("/api/llama/update", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).catch(() => null);
    if (!res?.ok) {
      toast(res?.error || "Couldn't start the llama.cpp update.", "warn", 6000, "llama-update");
      return;
    }
    toast("Updating llama.cpp…", "info", 4000, "llama-update");
    watchJob();
  }

  // A model the build can't load: say so, with a button that updates.
  function offerLlamaUpdate(name, arch) {
    const row = toast(`${name} needs a newer llama.cpp: your build doesn't support its architecture `
      + `(${arch}).`, "warn", 20000, "model-load");
    const right = row?.querySelector?.(".status-tab-right");
    if (right && !right.querySelector("[data-llama-update]")) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "status-action-btn";
      btn.dataset.llamaUpdate = "";
      btn.textContent = "Update llama.cpp";
      btn.addEventListener("click", () => update());
      right.prepend(btn);
    }
  }

  const btn = byId("btn-llama-update");
  if (btn) {
    btn.addEventListener("click", async () => {
      if (last?.available) return update();
      btn.disabled = true;
      btn.querySelector("span").textContent = "Checking…";
      const st = await refresh(true);
      if (!st) { btn.disabled = false; btn.querySelector("span").textContent = "Check for update"; }
    });
  }
  // Fill the row when Settings shows it: the first time with a look at GitHub
  // (cached by the bridge for ten minutes), after that from what's known.
  const row = byId("llama-update-row");
  if (row && "IntersectionObserver" in window) {
    new IntersectionObserver((entries) => {
      if (!entries.some(e => e.isIntersecting)) return;
      refresh(!checkedOnce).then(st => { if (RUNNING.has(st?.job?.status)) watchJob(); });
      checkedOnce = true;
    }).observe(row);
  }

  return { offerLlamaUpdate };
};
