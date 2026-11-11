(() => {
  "use strict";

  // SHOWCASE — a live ASCII field on the empty "New session" screen.
  // The plate above the composer is an ink slab; this draws a slow field of
  // characters on it, coloured from cobalt through magenta to ember, densest
  // where the field is strongest. The same field keeps going past the plate's
  // edge as faint ink characters, over the sidebar and the top row, thinning
  // with distance. It blooms a little under the pointer.
  // When a chat starts, the characters dissolve cell by cell and the plate
  // fades where it stood; while a chat is open nothing is drawn or running.
  //
  // Built to stay smooth: every character is drawn once, per colour, into an
  // off-screen sheet, and a frame only copies small tiles from that sheet
  // (no text is shaped while it runs). It repaints on every display frame,
  // clears only the area it used, and re-measures the page a few times a
  // second rather than every frame. Paused when hidden or off-theme; reduced
  // motion gets a still.

  const THEME = "showcase";
  const RAMP = " .:-=+*#%@";
  const CELL_W = 9, CELL_H = 15;
  const FONT_PX = 12;
  const FACE = '"Fragment Mono", ui-monospace, "Cascadia Mono", Consolas, monospace';
  const MIN_FRAME_MS = 14;    // repaint at the display's rate, capped near 60
  const MEASURE_EVERY = 6;    // frames between page measurements
  const REACH = 200;          // px the field carries past the plate
  const RADIUS = 26;          // plate corner, matches showcase.css
  const INK = "11, 15, 19";
  const INK_ALPHA = [0.07, 0.11, 0.16, 0.22];
  const GLOW = [0.5, 0.68, 0.85, 1];
  const STOPS = [
    [0.00, 31, 59, 255], [0.28, 112, 66, 255], [0.50, 255, 64, 140],
    [0.70, 255, 92, 34], [0.86, 255, 184, 66], [1.00, 255, 244, 220],
  ];
  const SHADES = 20;          // rows 0..19 of the sheet; row 20 is ink

  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const wide = matchMedia("(min-width: 601px)");

  let canvas = null, ctx = null, sheet = null, raf = 0, last = 0, frame = 0;
  let W = 0, H = 0, dpr = 1, cols = 0, rows = 0, seed = null, tileW = 0, tileH = 0;
  let presence = 0, target = 0;
  let plate = null, keep = [], headSeen = false;
  let dirty = null;
  const pointer = { x: -1e5, y: -1e5 };
  const colours = [];
  const lit = GLOW.map(() => []);
  const haze = INK_ALPHA.map(() => []);

  function buildColours() {
    for (let i = 0; i < SHADES; i++) {
      const k = i / (SHADES - 1);
      let a = STOPS[0], b = STOPS[STOPS.length - 1];
      for (let s = 0; s < STOPS.length - 1; s++) {
        if (k >= STOPS[s][0] && k <= STOPS[s + 1][0]) { a = STOPS[s]; b = STOPS[s + 1]; break; }
      }
      const f = (k - a[0]) / Math.max(1e-6, b[0] - a[0]);
      const mix = n => Math.round(a[n] + (b[n] - a[n]) * f);
      colours.push(`rgb(${mix(1)}, ${mix(2)}, ${mix(3)})`);
    }
    colours.push(`rgb(${INK})`);
  }

  const smooth = (lo, hi, v) => {
    const k = Math.min(1, Math.max(0, (v - lo) / (hi - lo)));
    return k * k * (3 - 2 * k);
  };

  function themeActive() {
    return (document.documentElement.dataset.theme || "") === THEME && wide.matches;
  }
  function plateEl() {
    return themeActive() ? document.querySelector(".center .chat.is-welcome .wv-head") : null;
  }

  function ensureCanvas() {
    if (canvas) return true;
    const app = document.getElementById("app");
    if (!app) return false;
    canvas = document.createElement("canvas");
    canvas.id = "showcase-field";
    canvas.setAttribute("aria-hidden", "true");
    Object.assign(canvas.style, {
      position: "fixed", inset: "0", width: "100vw", height: "100vh",
      zIndex: "2", pointerEvents: "none", display: "none",
    });
    app.appendChild(canvas);
    ctx = canvas.getContext("2d");
    return true;
  }

  // Every character in every colour, drawn once.
  function buildSheet() {
    tileW = Math.ceil(CELL_W * dpr);
    tileH = Math.ceil(CELL_H * dpr);
    sheet = document.createElement("canvas");
    sheet.width = tileW * RAMP.length;
    sheet.height = tileH * colours.length;
    const s = sheet.getContext("2d");
    s.font = `${FONT_PX * dpr}px ${FACE}`;
    s.textAlign = "center";
    s.textBaseline = "middle";
    for (let row = 0; row < colours.length; row++) {
      s.fillStyle = colours[row];
      for (let g = 1; g < RAMP.length; g++) {
        s.fillText(RAMP[g], (g + 0.5) * tileW, (row + 0.5) * tileH);
      }
    }
  }

  function fit() {
    if (!canvas) return;
    // Characters stay sharp at 1.5x; going higher only costs fill.
    dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    cols = Math.ceil(W / CELL_W); rows = Math.ceil(H / CELL_H);
    // One fixed random number per cell: the order cells appear and vanish in.
    seed = new Float32Array(cols * rows);
    let s = 0x9E3779B9;
    for (let i = 0; i < seed.length; i++) {
      s ^= s << 13; s ^= s >>> 17; s ^= s << 5;
      seed[i] = ((s >>> 0) % 10000) / 10000;
    }
    buildSheet();
    dirty = null;
    frame = 0;
  }

  function rectOf(el, grow = 0) {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return { l: r.left - grow, t: r.top - grow, r: r.right + grow, b: r.bottom + grow };
  }

  function measure() {
    const head = plateEl();
    headSeen = !!head;
    if (!head) { keep = []; return; }
    plate = rectOf(head) || plate;
    // Keep clear of what has to be read or typed into.
    keep = [
      rectOf(document.querySelector(".center .composer-wrap > .composer-projects"), 4),
      rectOf(document.querySelector(".center .composer-wrap > .composer"), 10),
      rectOf(document.querySelector(".center .chat.is-welcome > .wv-below"), 6),
    ].filter(Boolean);
  }

  // Inside the plate, allowing for its rounded corners.
  function inPlate(x, y) {
    const p = plate;
    if (x < p.l || x > p.r || y < p.t || y > p.b) return false;
    const cx = x < p.l + RADIUS ? p.l + RADIUS : (x > p.r - RADIUS ? p.r - RADIUS : x);
    const cy = y < p.t + RADIUS ? p.t + RADIUS : (y > p.b - RADIUS ? p.b - RADIUS : y);
    return (x - cx) * (x - cx) + (y - cy) * (y - cy) <= RADIUS * RADIUS;
  }

  function clearDirty() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    if (dirty) ctx.clearRect(dirty.x, dirty.y, dirty.w, dirty.h);
    else ctx.clearRect(0, 0, canvas.width, canvas.height);
  }

  function paint(now) {
    raf = 0;
    const dt = Math.min(80, last ? now - last : 16);
    last = now;
    presence = target > presence ? Math.min(1, presence + dt / 950) : Math.max(0, presence - dt / 620);
    if (motion.matches) presence = target;

    // The plate moves while it scales in, so measure more often then.
    if (frame % (presence < 1 && target === 1 ? 2 : MEASURE_EVERY) === 0) measure();
    frame++;

    if ((presence <= 0 && target === 0) || !plate) {
      dirty = null;
      clearDirty();
      canvas.style.display = "none";
      if (!headSeen) plate = null;
      return;
    }
    canvas.style.display = "";

    const e = presence * presence * (3 - 2 * presence);
    const t = motion.matches ? 12 : now / 1000;
    const p = plate, pw = p.r - p.l, ph = p.b - p.t;
    const span = REACH * 1.6;
    clearDirty();
    dirty = {
      x: Math.max(0, Math.floor((p.l - span) * dpr) - 2), y: Math.max(0, Math.floor((p.t - span) * dpr) - 2),
      w: Math.ceil((pw + span * 2) * dpr) + 4, h: Math.ceil((ph + span * 2) * dpr) + 4,
    };

    // The plate has gone from the page (a chat started): hold its place in
    // ink while the characters leave.
    if (!headSeen) {
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalAlpha = e;
      ctx.fillStyle = `rgb(${INK})`;
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(p.l, p.t, pw, ph, RADIUS); else ctx.rect(p.l, p.t, pw, ph);
      ctx.fill();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
    }

    for (const list of lit) list.length = 0;
    for (const list of haze) list.length = 0;
    const j0 = Math.max(0, Math.floor((p.t - span) / CELL_H));
    const j1 = Math.min(rows, Math.ceil((p.b + span) / CELL_H));
    const i0 = Math.max(0, Math.floor((p.l - span) / CELL_W));
    const i1 = Math.min(cols, Math.ceil((p.r + span) / CELL_W));
    const top = RAMP.length - 1;
    const gate = e * 1.12 - 0.06;
    const fresh = e * 1.12 - 0.14;
    const reach2 = REACH * REACH;
    const flick = Math.floor(now / 70);

    for (let j = j0; j < j1; j++) {
      const y = (j + 0.5) * CELL_H;
      const py = Math.round(y * dpr - tileH / 2);
      for (let i = i0; i < i1; i++) {
        // Cells arrive and leave in their own random order.
        const order = seed[j * cols + i];
        if (order > gate) continue;
        const x = (i + 0.5) * CELL_W;
        const inside = inPlate(x, y);

        let env = 1;
        if (!inside) {
          const dx = x < p.l ? p.l - x : (x > p.r ? x - p.r : 0);
          const dy = y < p.t ? p.t - y : (y > p.b ? y - p.b : 0);
          const d2 = dx * dx + dy * dy;
          if (d2 > reach2 * 2.3) continue;
          env = Math.exp(-d2 / reach2);
          let blocked = false;
          for (let k = 0; k < keep.length; k++) {
            const r = keep[k];
            if (x > r.l && x < r.r && y > r.t && y < r.b) { blocked = true; break; }
          }
          if (blocked) continue;
        }

        const a = Math.sin(x * 0.0105 + t * 0.21 + Math.sin(y * 0.0125 - t * 0.16) * 1.7);
        const b = Math.sin(y * 0.0165 - t * 0.18 + Math.sin(x * 0.0085 + t * 0.12) * 1.9);
        const c = Math.sin((x * 0.6 + y) * 0.0062 + t * 0.075);
        const v = 0.5 + 0.5 * (a * 0.5 + b * 0.32 + c * 0.18);
        const qx = x - pointer.x, qy = y - pointer.y, q2 = qx * qx + qy * qy;
        const bloom = q2 < 67600 ? Math.exp(-q2 / 11025) : 0;
        const px = Math.round(x * dpr - tileW / 2);

        if (inside) {
          // Strong in the upper right, quiet in the lower left where the
          // heading is set.
          const u = (x - p.l) / pw, w = (y - p.t) / ph;
          const glow = smooth(0.42, 1.12, u * 0.92 + (1 - w) * 0.6);
          let d = smooth(0.28, 0.86, v) * (0.05 + 0.95 * glow) + bloom * 0.45 * (0.25 + 0.75 * glow);
          if (d < 0.08) continue;
          if (d > 1) d = 1;
          // A character fades up before it gives way to the next denser one,
          // so the field shifts instead of popping.
          const level = d * (top - 0.001);
          let glyph = 1 + Math.floor(level);
          const part = level - Math.floor(level);
          // The newest cells flicker through other characters as they settle.
          if (order > fresh) glyph = 1 + ((i * 7 + j * 13 + flick) % top);
          const hue = 0.5 + 0.5 * Math.sin(x * 0.0046 - t * 0.12 + Math.sin(y * 0.0061 + t * 0.09) * 1.4);
          const shade = Math.min(SHADES - 1, Math.max(0, Math.floor((hue * 0.72 + d * 0.36) * (SHADES - 1))));
          lit[Math.min(GLOW.length - 1, Math.floor(part * GLOW.length))].push(glyph * tileW, shade * tileH, px, py);
        } else {
          let d = (smooth(0.56, 0.95, v) + bloom * 0.5) * env;
          if (d < 0.1) continue;
          if (d > 1) d = 1;
          const glyph = 1 + Math.floor(d * (top - 0.001));
          haze[Math.min(INK_ALPHA.length - 1, Math.floor(d * INK_ALPHA.length))].push(glyph * tileW, SHADES * tileH, px, py);
        }
      }
    }

    for (let s = 0; s < haze.length; s++) {
      const list = haze[s];
      if (!list.length) continue;
      ctx.globalAlpha = INK_ALPHA[s];
      for (let n = 0; n < list.length; n += 4) {
        ctx.drawImage(sheet, list[n], list[n + 1], tileW, tileH, list[n + 2], list[n + 3], tileW, tileH);
      }
    }
    for (let s = 0; s < lit.length; s++) {
      const list = lit[s];
      if (!list.length) continue;
      ctx.globalAlpha = GLOW[s];
      for (let n = 0; n < list.length; n += 4) {
        ctx.drawImage(sheet, list[n], list[n + 1], tileW, tileH, list[n + 2], list[n + 3], tileW, tileH);
      }
    }
    ctx.globalAlpha = 1;

    // A still frame is enough once it has settled under reduced motion.
    if (motion.matches && presence === target) return;
    schedule();
  }

  function schedule() {
    if (raf || document.hidden) return;
    raf = requestAnimationFrame(now => {
      if (now - last < MIN_FRAME_MS) { raf = 0; schedule(); return; }
      paint(now);
    });
  }

  function sync() {
    const want = !!plateEl();
    target = want ? 1 : 0;
    if (!want && presence <= 0) return;
    if (!ensureCanvas()) return;
    if (!seed || W !== window.innerWidth || H !== window.innerHeight) fit();
    if (!themeActive()) presence = 0;
    frame = 0;
    schedule();
  }

  function start() {
    buildColours();
    const chat = document.querySelector(".center .chat");
    if (chat) new MutationObserver(sync).observe(chat, { attributes: true, attributeFilter: ["class"] });
    new MutationObserver(sync).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    window.addEventListener("resize", () => { if (canvas) { fit(); sync(); } });
    window.addEventListener("pointermove", ev => { pointer.x = ev.clientX; pointer.y = ev.clientY; }, { passive: true });
    document.addEventListener("pointerleave", () => { pointer.x = pointer.y = -1e5; });
    document.addEventListener("visibilitychange", () => { last = 0; sync(); });
    motion.addEventListener?.("change", sync);
    wide.addEventListener?.("change", sync);
    // Draw with the theme's own mono face once it is ready.
    const face = `${FONT_PX}px ${FACE}`;
    (document.fonts?.load ? document.fonts.load(face).catch(() => {}) : Promise.resolve())
      .then(() => { if (canvas) buildSheet(); sync(); });
    sync();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
