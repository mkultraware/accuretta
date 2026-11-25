/* SPDX-License-Identifier: AGPL-3.0-only */
/* task-progress panel: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["task-progress"] = ({ esc, isMobile }) => {
  // A checklist driven by the `plan` event that the update_plan tool emits.

  /** Keep #plan-panel anchored in the chat column (never the preview). The
   *  topbar Plan button toggles its visibility now, so it no longer moves into
   *  the previewer. */
  function _reparentPlan() {
    const panel = document.getElementById("plan-panel");
    if (!panel) return;
    const chatColumn = document.querySelector(".center");
    if (chatColumn && panel.parentElement !== chatColumn) {
      panel.style.left = "";
      panel.style.top = "";
      panel.style.right = "";
      chatColumn.appendChild(panel);
    }
    panel.classList.add("plan-in-chat");
  }

  // Reflect the plan panel's presence/visibility on the topbar toggle button.
  function setPlanBtn(active) {
    const btn = document.getElementById("btn-toggle-plan");
    if (!btn) return;
    btn.classList.toggle("active", !!active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  }

  function makePanelDraggable(panel) {
    const head = panel.querySelector(".plan-head");
    if (!head) return;
    head.style.cursor = "grab";
    
    let isDragging = false;
    let startX, startY;
    let initialLeft, initialTop;
    
    head.addEventListener("mousedown", (e) => {
      if (e.target.closest("button") || e.target.closest("i")) return;
      isDragging = true;
      head.style.cursor = "grabbing";
      
      startX = e.clientX;
      startY = e.clientY;
      initialLeft = panel.offsetLeft;
      initialTop = panel.offsetTop;
      
      e.preventDefault();
      
      const onMouseMove = (moveEvent) => {
        if (!isDragging) return;
        const dx = moveEvent.clientX - startX;
        const dy = moveEvent.clientY - startY;
        panel.style.left = `${initialLeft + dx}px`;
        panel.style.top = `${initialTop + dy}px`;
        panel.style.right = "auto";
      };
      
      const onMouseUp = () => {
        isDragging = false;
        head.style.cursor = "grab";
        document.removeEventListener("mousemove", onMouseMove);
        document.removeEventListener("mouseup", onMouseUp);
      };
      
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
    });
    
    head.addEventListener("touchstart", (e) => {
      if (e.target.closest("button") || e.target.closest("i")) return;
      const touch = e.touches[0];
      isDragging = true;
      
      startX = touch.clientX;
      startY = touch.clientY;
      initialLeft = panel.offsetLeft;
      initialTop = panel.offsetTop;
      
      const onTouchMove = (moveEvent) => {
        if (!isDragging) return;
        const touchMove = moveEvent.touches[0];
        const dx = touchMove.clientX - startX;
        const dy = touchMove.clientY - startY;
        panel.style.left = `${initialLeft + dx}px`;
        panel.style.top = `${initialTop + dy}px`;
        panel.style.right = "auto";
      };
      
      const onTouchEnd = () => {
        isDragging = false;
        document.removeEventListener("touchmove", onTouchMove);
        document.removeEventListener("touchend", onTouchEnd);
      };
      
      document.addEventListener("touchmove", onTouchMove, { passive: false });
      document.addEventListener("touchend", onTouchEnd);
    });
  }

  // "n/N" where n rolls: the old digit slides up and out as the new one rises in.
  function _setPlanCount(panel, done, total) {
    const num = panel.querySelector(".plan-count-num");
    const tot = panel.querySelector(".plan-count-tot");
    if (!num || !tot) return;
    tot.textContent = `/${total}`;
    const cur = num.querySelector(".pcn:not(.pcn-out)");
    if (cur && cur.textContent === String(done)) return;
    const next = document.createElement("span");
    next.className = cur ? "pcn pcn-in" : "pcn";
    next.textContent = String(done);
    if (cur) {
      cur.classList.add("pcn-out");
      setTimeout(() => cur.remove(), 420);
    }
    num.appendChild(next);
  }

  function renderPlanPanel(steps) {
    let panel = document.getElementById("plan-panel");
    if (!steps || !steps.length) { if (panel) panel.remove(); setPlanBtn(false); return; }
    if (!panel) {
      panel = document.createElement("div");
      panel.id = "plan-panel";
      panel.innerHTML = `
        <div class="plan-head">
          <span class="plan-status" aria-hidden="true"><span class="plan-spin"></span><i class="ph-fill ph-check-circle"></i></span>
          <span class="plan-title">Plan</span>
          <span class="plan-count" aria-live="polite"><span class="plan-count-num"></span><span class="plan-count-tot"></span></span>
          <button class="plan-min" type="button" title="Collapse"><i class="ph ph-caret-up"></i></button>
          <button class="plan-close" type="button" title="Hide"><i class="ph ph-x"></i></button>
        </div>
        <div class="plan-body"></div>`;
      (document.querySelector(".center") || document.body).appendChild(panel);
      panel.classList.add("plan-in-chat");

      panel.querySelector(".plan-min").addEventListener("click", () => panel.classList.toggle("collapsed"));
      // Close hides (not removes) so the topbar Plan button can bring it back
      // within the same turn.
      panel.querySelector(".plan-close").addEventListener("click", () => { panel.classList.add("plan-hidden"); setPlanBtn(false); });
      makePanelDraggable(panel);
      // On a phone the floating checklist would smother the screen — start it
      // collapsed to a compact "Plan N/N" chip the user can tap open.
      if (typeof isMobile === "function" && isMobile()) panel.classList.add("collapsed");
      setPlanBtn(true);   // auto-pop on first plan of the turn
    }
    const done = steps.filter(s => s.status === "done").length;
    _setPlanCount(panel, done, steps.length);
    panel.classList.toggle("plan-complete", done === steps.length);
    // Rows are updated in place while the step list is the same, so a step
    // changing state animates (icon swap, strike drawn across) instead of the
    // whole list being replaced.
    const body = panel.querySelector(".plan-body");
    const titles = steps.map(s => String(s.title || ""));
    const sameList = body._titles && body._titles.length === titles.length
      && body._titles.every((t, i) => t === titles[i]);
    if (!sameList) {
      body.innerHTML = titles.map(t =>
        `<div class="plan-step"><span class="plan-ico" aria-hidden="true">` +
        `<i class="ph ph-circle"></i><i class="ph ph-arrow-circle-right"></i><i class="ph-fill ph-check-circle"></i>` +
        `</span><span class="plan-text"><span class="plan-label">${esc(t)}</span></span></div>`).join("");
      body._titles = titles;
    }
    const rows = body.children;
    steps.forEach((s, i) => {
      const st = s.status === "done" ? "done" : (s.status === "active" ? "active" : "pending");
      if (rows[i] && !rows[i].classList.contains(`is-${st}`)) rows[i].className = `plan-step is-${st}`;
    });
    // Ensure it's in the right container after an update (e.g. if preview was
    // toggled between plan events).
    _reparentPlan();
  }
  return { setPlanBtn, renderPlanPanel };
};
