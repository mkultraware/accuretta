(() => {
  "use strict";

  // ATELIER — flow lines on the blank "New session" chat.
  // A field of near-horizontal streamlines displaced by two slow sine
  // octaves plus a drifting lens that pinches the lines toward a moving
  // focal point (the reference's saddle). Painted once per ~33ms, paused
  // when hidden/off-theme, and reduced-motion users get a single still
  // frame. No interaction, no color: the theme's bone ink at low alpha.

  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  let canvas = null;
  let ctx = null;
  let raf = 0;
  let lastPaint = 0;
  let W = 0, H = 0, dpr = 1;

  function themeActive() {
    return (document.documentElement.dataset.theme || "") === "atelier";
  }

  function boneColor() {
    // The theme's fg token at drawing alpha; re-read per paint so theme
    // palette edits propagate without a reload.
    const raw = (getComputedStyle(document.documentElement).getPropertyValue("--fg") || "#EAE3D4").trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(raw);
    if (!hex) return { r: 234, g: 227, b: 212 };
    const n = parseInt(hex[1], 16);
    return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
  }

  function fit() {
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(1, Math.round(rect.width));
    H = Math.max(1, Math.round(rect.height));
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Displacement field. Kept cheap: two sine octaves + one gaussian lens.
  function displace(x, y, t) {
    const w1 = 12 * Math.sin(x * 0.0072 + y * 0.0053 + t * 0.00042);
    const w2 = 5 * Math.sin(x * 0.0161 - y * 0.0039 - t * 0.00023);
    // Drifting focal point (the saddle). Period: minutes.
    const cx = W * (0.5 + 0.16 * Math.sin(t * 0.000105 + 1.2));
    const cy = H * (0.44 + 0.13 * Math.cos(t * 0.000083));
    const dx = (x - cx) / (W * 0.34);
    const dy = (y - cy) / (H * 0.26);
    const lens = Math.exp(-0.5 * (dx * dx + dy * dy));
    const pinch = (cy - y) * 0.7 * lens;
    return { y: y + w1 + w2 + pinch, alpha: lens };
  }

  function paint(t) {
    const { r, g, b } = boneColor();
    ctx.clearRect(0, 0, W, H);
    const step = 15;                       // vertical distance between lines
    const dx = 9;                          // horizontal sampling distance
    ctx.lineWidth = 1;
    for (let y = step; y < H + step; y += step) {
      // Lines near the lens brighten slightly and tighten — the field has
      // a focal presence without any color.
      ctx.beginPath();
      let began = false;
      for (let x = -8; x <= W + 8; x += dx) {
        const p = displace(x, y, t);
        if (!began) { ctx.moveTo(x, p.y); began = true; }
        else ctx.lineTo(x, p.y);
      }
      ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, 0.085)`;
      ctx.stroke();
    }
  }

  function loop(time) {
    raf = 0;
    if (!canvas || !canvas.isConnected) return;
    if (document.hidden || !themeActive() || !canvas.getClientRects().length) return;
    if (time - lastPaint >= 33) { paint(time); lastPaint = time; }
    raf = requestAnimationFrame(loop);
  }

  function wake() {
    if (!canvas || motion.matches) { if (canvas && motion.matches) paint(performance.now()); return; }
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function mount(target) {
    if (canvas?.isConnected) { canvas.remove(); canvas = null; }
    if (!themeActive()) return;
    canvas = target.querySelector(".flow-lines-canvas");
    if (!canvas) return;
    ctx = canvas.getContext("2d");
    if (!ctx) return;
    fit();
    paint(performance.now());
    wake();
  }

  // Watch for welcome screens; the canvas element ships in renderMessages.
  const chatInner = document.getElementById("chat-inner") || document.body;
  const changes = new MutationObserver(() => {
    const welcome = document.querySelector("#chat-inner .welcome-screen");
    if (welcome) mount(welcome);
    else if (canvas?.isConnected) { canvas.remove(); canvas = null; }
  });
  changes.observe(chatInner, { childList: true, subtree: true });

  new MutationObserver(() => {
    if (!themeActive()) {
      if (canvas?.isConnected) { canvas.remove(); canvas = null; }
      return;
    }
    const welcome = document.querySelector("#chat-inner .welcome-screen");
    if (welcome) mount(welcome);
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // Re-fit only when the drag settles: resizing the backing store is a full
  // canvas reallocation, and doing it on every resize event made the window
  // drag stutter. The stretched frame during the drag is imperceptible.
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    if (!canvas?.isConnected) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { fit(); paint(performance.now()); }, 120);
  });
  document.addEventListener("visibilitychange", wake);
  motion.addEventListener?.("change", () => {
    if (canvas?.isConnected && motion.matches) paint(performance.now());
    wake();
  });

  const first = document.querySelector("#chat-inner .welcome-screen");
  if (first) mount(first);
})();
