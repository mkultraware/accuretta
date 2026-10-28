(() => {
  "use strict";

  // Accuretta dot matrix — the house visual. Two pieces share one look:
  //   AccDots.buildA(svg)  the split-A logo as a grid of dots, same polygons
  //                        and blue→violet ramp as the notch's matrix A.
  //   AccDots.field        a quiet dot field behind the empty chat (welcome
  //                        screen + composer) that ripples once when a new
  //                        session starts, then fades out.
  // Reduced-motion users get still frames and no ripple.

  const motion = matchMedia("(prefers-reduced-motion: reduce)");

  // ---- matrix A --------------------------------------------------------------
  // Artwork space and polygons match notch-wire.js (MX_*), so the A here and
  // the notch's A are the same shape on a coarser grid.
  const A_X0 = 150, A_Y0 = 285, A_W = 980, A_H = 725;
  const A_BIG = [[165, 990], [625, 315], [868, 298], [500, 878], [470, 888]];
  const A_SMALL = [[672, 648], [885, 645], [1112, 975], [862, 940]];

  function inPoly(x, y, p) {
    let c = false;
    for (let i = 0, j = p.length - 1; i < p.length; j = i++) {
      const [xi, yi] = p[i], [xj, yj] = p[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
    return c;
  }

  // Only the A's own dots are drawn — no surrounding grid.
  // opts.lo = [min, max] resting opacity for lit dots (twinkle floor).
  // opts.mono = flat currentColor dots, no twinkle (a static logo).
  function buildA(svg, cols = 16, opts = {}) {
    const [loMin, loMax] = opts.lo || [.35, .65];
    const ns = "http://www.w3.org/2000/svg";
    const cell = A_W / cols;
    const rows = Math.floor(A_H / cell);
    const offY = (A_H - rows * cell) / 2;
    svg.setAttribute("viewBox", `0 0 ${A_W} ${A_H}`);
    svg.classList.add("dm-a");
    svg.textContent = "";
    for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
      const x = A_X0 + (c + .5) * cell, y = A_Y0 + offY + (r + .5) * cell;
      if (!(inPoly(x, y, A_BIG) || inPoly(x, y, A_SMALL))) continue;
      const t = (x - A_X0) / A_W, u = (y - A_Y0) / A_H;
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("cx", (x - A_X0).toFixed(1));
      dot.setAttribute("cy", (y - A_Y0).toFixed(1));
      dot.setAttribute("r", (cell * .36).toFixed(1));
      if (opts.mono) {
        dot.setAttribute("fill", "currentColor");
      } else {
        // Lightness rides on --dm-l so dark themes can lift the whole A.
        const hue = 212 + t * 50 + u * 30, shade = 2 - (1 - Math.abs(u - .65)) * 10;
        const p = 1.6 + Math.random() * 2.4;
        dot.style.fill = `hsl(${hue.toFixed(0)} 90% calc(var(--dm-l, 60%) + ${shade.toFixed(1)}%))`;
        dot.setAttribute("class", "on");
        dot.style.setProperty("--p", p.toFixed(2) + "s");
        dot.style.setProperty("--d", (-Math.random() * p).toFixed(2) + "s");
        dot.style.setProperty("--lo", (loMin + Math.random() * (loMax - loMin)).toFixed(2));
      }
      svg.appendChild(dot);
    }
    return svg;
  }

  // ---- welcome field -----------------------------------------------------------
  const GAP = 18;              // px between dot centres
  const DOT_R = 1.05;          // resting radius
  const FRAME_MS = 1000 / 30;  // idle drift is slow; 30fps is plenty
  const PULSE_MS = 1100;
  const FADE_MS = 520;

  let canvas = null, ctx = null, host = null;
  let W = 0, H = 0, dpr = 1;
  let raf = 0, lastPaint = 0;
  let wanted = false;          // welcome screen is up
  let alpha = 0;               // global fade, 0..1
  let fadeFrom = 0, fadeTo = 0, fadeStart = 0;
  let pulse = null;            // { x, y, t0, max }
  let ink = { r: 120, g: 120, b: 130 };

  function readInk() {
    const raw = (getComputedStyle(document.documentElement).getPropertyValue("--fg") || "").trim();
    const hex = /^#([0-9a-f]{6})$/i.exec(raw);
    if (hex) {
      const n = parseInt(hex[1], 16);
      ink = { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
      return;
    }
    const rgb = /rgba?\(\s*(\d+)[ ,]+(\d+)[ ,]+(\d+)/i.exec(raw);
    if (rgb) ink = { r: +rgb[1], g: +rgb[2], b: +rgb[3] };
  }

  function ensureCanvas() {
    if (canvas && canvas.isConnected) return true;
    host = document.querySelector(".center .chat");
    if (!host) return false;
    canvas = document.createElement("canvas");
    canvas.className = "dot-field";
    canvas.setAttribute("aria-hidden", "true");
    host.prepend(canvas);
    ctx = canvas.getContext("2d");
    fit();
    return true;
  }

  function fit() {
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = Math.max(1, Math.round(rect.width));
    H = Math.max(1, Math.round(rect.height));
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // Idle motion: every dot twinkles on its own phase and speed, like the
  // notch's matrix A — the light never moves as one block, so no bands or
  // blobs drift across the pool. Returns 0..1, mostly near rest.
  function hash(i, j) {
    const h = Math.sin(i * 127.1 + j * 311.7) * 43758.5453;
    return h - Math.floor(h);
  }
  function twinkle(i, j, t) {
    const ph = hash(i, j) * 6.2832, sp = .0006 + hash(j, i) * .0011;
    const w = .5 + .5 * Math.sin(t * sp + ph);
    return w * w * w;
  }

  function smooth(e0, e1, v) {
    const k = Math.max(0, Math.min(1, (v - e0) / (e1 - e0)));
    return k * k * (3 - 2 * k);
  }

  // The pool centres on the composer (mid-pane on the welcome screen) and is
  // sized to it, so the dots frame the prompt instead of tiling the pane.
  function focusPoint() {
    const comp = document.querySelector(".composer-wrap > .composer");
    const box = canvas.getBoundingClientRect();
    if (comp) {
      const r = comp.getBoundingClientRect();
      if (r.width) {
        return { x: r.left + r.width / 2 - box.left, y: r.top + r.height / 2 - box.top - 40,
                 rx: Math.max(r.width * .78, 320), ry: Math.max(H * .42, 260) };
      }
    }
    return { x: W / 2, y: H * .5, rx: W * .42, ry: H * .42 };
  }

  function easeOut(k) { return 1 - Math.pow(1 - k, 3); }

  function paint(now) {
    ctx.clearRect(0, 0, W, H);
    if (alpha <= 0.001) return;
    const still = motion.matches;
    const t = still ? 0 : now;
    const f = focusPoint();
    let pr = -1, pw = 0, pk = 0;
    if (pulse) {
      pk = Math.min(1, (now - pulse.t0) / PULSE_MS);
      pr = easeOut(pk) * pulse.max;
      pw = 60 + 90 * pk;
    }
    const ox = (W % GAP) / 2, oy = (H % GAP) / 2;
    for (let y = oy, j = 0; y < H; y += GAP, j++) {
      for (let x = ox, i = 0; x < W; x += GAP, i++) {
        const d = Math.hypot((x - f.x) / f.rx, (y - f.y) / f.ry);
        const pool = 1 - smooth(.18, 1, d);
        if (pool <= 0 && pr < 0) continue;
        let a = (.13 + .3 * twinkle(i, j, t)) * pool;
        let r = DOT_R;
        let lit = 0;
        if (pr >= 0) {
          const dd = Math.abs(Math.hypot(x - pulse.x, y - pulse.y) - pr);
          if (dd < pw) {
            lit = (1 - dd / pw) * (1 - pk * .85);
            a += lit * .75;
            r += lit * 1.5;
          }
        }
        if (lit > .04) {
          const u = x / W, v = y / H;
          const hue = 212 + u * 50 + v * 30;
          ctx.fillStyle = `hsla(${hue.toFixed(0)},90%,60%,${(a * alpha).toFixed(3)})`;
        } else {
          ctx.fillStyle = `rgba(${ink.r},${ink.g},${ink.b},${(a * alpha).toFixed(3)})`;
        }
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    if (pulse && pk >= 1) pulse = null;
  }

  function tick(now) {
    raf = 0;
    if (!canvas || !canvas.isConnected || document.hidden) return;
    if (fadeStart) {
      const k = Math.min(1, (now - fadeStart) / FADE_MS);
      alpha = fadeFrom + (fadeTo - fadeFrom) * k;
      if (k >= 1) fadeStart = 0;
    }
    // Hold full opacity while a ripple is still travelling, then fade.
    if (!wanted && !pulse && !fadeStart && alpha > 0) startFade(0, now);
    const busy = fadeStart || pulse;
    if (busy || now - lastPaint >= FRAME_MS) {
      lastPaint = now;
      paint(now);
    }
    const done = !wanted && !pulse && !fadeStart && alpha <= 0.001;
    if (done) { canvas.classList.remove("on"); return; }
    if (motion.matches && !busy) return; // still frame, nothing moving
    raf = requestAnimationFrame(tick);
  }

  function kick() {
    if (!raf) raf = requestAnimationFrame(tick);
  }

  function startFade(to, now = performance.now()) {
    fadeFrom = alpha;
    fadeTo = to;
    fadeStart = now;
  }

  const field = {
    show() {
      if (!ensureCanvas()) return;
      readInk();
      fit();
      wanted = true;
      canvas.classList.add("on");
      if (alpha < 1) startFade(1);
      kick();
    },
    hide() {
      wanted = false;
      kick();
    },
    // Ripple out from `origin` (an element or {x, y} in client px). Called
    // when a message or suggestion starts the session; the field fades once
    // the ring has crossed the pane.
    pulse(origin) {
      if (!canvas || !canvas.isConnected || alpha <= 0.001) return;
      const now = performance.now();
      if (pulse) return; // one ripple per session start
      if (motion.matches) { field.hide(); return; }
      const box = canvas.getBoundingClientRect();
      let cx = W / 2, cy = H * .72;
      if (origin && typeof origin.getBoundingClientRect === "function") {
        const r = origin.getBoundingClientRect();
        cx = r.left + r.width / 2 - box.left;
        cy = r.top + r.height / 2 - box.top;
      } else if (origin && Number.isFinite(origin.x)) {
        cx = origin.x - box.left;
        cy = origin.y - box.top;
      }
      const max = Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy)) + 40;
      pulse = { x: cx, y: cy, t0: now, max };
      kick();
    },
  };

  window.addEventListener("resize", () => { if (canvas && wanted) { fit(); kick(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && canvas) kick(); });
  // Theme switches only change the ink; the field stays up while the
  // welcome screen is.
  new MutationObserver(() => {
    if (!canvas) return;
    readInk();
    kick();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  // Is (t, u), as 0..1 across the A's artwork box, inside the A? Lets the
  // splash's glyph field draw the same shape without a second copy of it.
  function inA(t, u) {
    const x = A_X0 + t * A_W, y = A_Y0 + u * A_H;
    return inPoly(x, y, A_BIG) || inPoly(x, y, A_SMALL);
  }

  window.AccDots = { buildA, field, inA, A_ASPECT: A_W / A_H };

  // Static chrome: the preview's empty state.
  const previewA = document.getElementById("preview-empty-a");
  if (previewA) buildA(previewA, 16, { mono: true });
})();
