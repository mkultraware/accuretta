/* SPDX-License-Identifier: AGPL-3.0-only */
/* terminal jump-to-bottom: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["terminal-jump"] = ({ $ }) => {
  // Every terminal-style pane (Host, WSL Guest, Backend, Shell, Agent log)
  // gets a round arrow once you scroll up to read; output that lands while you
  // are up there lights it with the accent. The arrow lives beside the
  // scroller, never inside it, so its class flips can't feed the observer.
  function wbInitJumps() {
    // Kept on the hoisted function: activateTerminalTab can run before the
    // workbench state object exists.
    if (wbSyncJumps.list) return;
    wbSyncJumps.list = [];
    const body = $(".term-tabs-body");
    const targets = [...document.querySelectorAll(".term-tab-pane")]
      .filter(p => p.id !== "term-pane-shell").map(p => [p, body]);
    targets.push([$("#shell-out"), $(".shell-pane")]);
    const smooth = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
    for (const [scroller, host] of targets) {
      if (!scroller || !host) continue;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "wb-jump";
      btn.dataset.for = scroller.id;
      btn.title = "Jump to latest output";
      btn.setAttribute("aria-label", "Jump to latest output");
      btn.tabIndex = -1;
      btn.innerHTML = '<i class="ph ph-arrow-down" aria-hidden="true"></i>';
      host.appendChild(btn);
      const entry = { scroller, btn, raf: 0 };
      btn.addEventListener("click", () => {
        // Long logs: snap most of the way, then glide the last screenful.
        const near = scroller.scrollHeight - scroller.clientHeight * 2;
        if (scroller.scrollTop < near) scroller.scrollTop = near;
        scroller.scrollTo({ top: scroller.scrollHeight, behavior: smooth() });
        btn.classList.remove("fresh");
      });
      const queue = (grew) => {
        if (grew && wbJumpAway(scroller)) entry.grew = true;
        if (entry.raf) return;
        entry.raf = requestAnimationFrame(() => { entry.raf = 0; wbSyncJump(entry); });
      };
      scroller.addEventListener("scroll", () => queue(false), { passive: true });
      new MutationObserver(() => queue(true)).observe(scroller, { childList: true, characterData: true, subtree: true });
      wbSyncJumps.list.push(entry);
    }
    window.addEventListener("resize", wbSyncJumps);
    wbSyncJumps();
  }
  function wbJumpAway(scroller) {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 80;
  }
  function wbSyncJump(entry) {
    const { scroller, btn } = entry;
    const away = wbJumpAway(scroller);
    const show = away && !scroller.closest(".term-tab-pane.hidden");
    if (btn.classList.contains("show") !== show) {
      btn.classList.toggle("show", show);
      btn.tabIndex = show ? 0 : -1;
    }
    const fresh = away && (entry.grew || btn.classList.contains("fresh"));
    entry.grew = false;
    if (btn.classList.contains("fresh") !== fresh) btn.classList.toggle("fresh", fresh);
  }
  function wbSyncJumps() { (wbSyncJumps.list || []).forEach(wbSyncJump); }
  return { wbInitJumps, wbSyncJumps };
};
