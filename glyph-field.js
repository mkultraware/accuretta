/* Accuretta splash — the split A drawn in monospace glyphs.
   One canvas covers the window. The A (shape from dot-matrix.js, AccDots.inA)
   is a grid of characters, dense in its body and lighter at its edges, which
   resolves along the notch's diagonal (bottom left to top right) as the
   splash appears and then idles with an occasional re-rolled glyph and a
   slow light sweep. A faint halo of dashes sits around it and sparse dust
   across the window. A small section tag frames the part of the A that
   stands for the current boot stage (bridge, engine, workspace) and glides
   to the next one as boot moves on; at ready it frames the whole mark.
   The corner HUD (#hud-tl / #hud-tr) prints real boot telemetry.
   API (window.AccuGlyphField):
     setStage(pct)         pct 0..100; picks the stage and updates the HUD
     ready()               flashes the mark and frames all of it
   Robustness: devicePixelRatio-aware (capped at 2x), resize handling, pauses
   while hidden, one still, fully drawn frame under reduced motion. */
(function () {
  "use strict";
  var canvas = document.getElementById("glyph-field");
  var mark = document.getElementById("glyph-mark");
  if (!canvas || !canvas.getContext || !mark || !window.AccDots || !window.AccDots.inA) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var FRAME_MS = 1000 / 30, COLS = 38;
  var CORE = "#@8&%SX", MID = "=+*xo", EDGE = ".:-~", GHOST = ".-";
  var FONT = '"JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace';
  // Each boot stage frames one part of the A: (t0, u0, t1, u1) in 0..1 of
  // the mark. Bridge = the slab's foot, engine = its apex, workspace = the
  // small right stroke. Ready frames the whole mark.
  var STAGES = [
    { tag: "BRIDGE 01", box: [0.03, 0.6, 0.36, 0.98] },
    { tag: "ENGINE 02", box: [0.44, 0.0, 0.76, 0.34] },
    { tag: "WORKSPACE 03", box: [0.52, 0.47, 0.99, 0.97] },
  ];
  var ALL = { tag: "READY", box: [0, 0, 1, 1] };

  var W = 0, H = 0, dpr = 1, raf = null, lastDraw = 0, t0 = 0, frame = 0;
  var cells = [], ghosts = [], dust = [], rows = 0;
  var colors = { ink: [230, 236, 245], bg: [10, 12, 16], dark: true };
  var stage = 0, pct = 0, readyAt = 0;
  var box = STAGES[0].box.slice(), boxTarget = STAGES[0];
  var seed = 7;
  function rand() { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; }
  function pick(set) { return set[(rand() * set.length) | 0]; }

  function parseColor(str, fallback) {
    var m = /^#([0-9a-f]{6})$/i.exec(String(str || "").trim());
    if (!m) return fallback;
    var n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  function readColors() {
    var cs = getComputedStyle(document.documentElement);
    colors.ink = parseColor(cs.getPropertyValue("--fg"), colors.ink);
    colors.bg = parseColor(cs.getPropertyValue("--bg"), colors.bg);
    var b = colors.bg;
    colors.dark = (0.2126 * b[0] + 0.7152 * b[1] + 0.0722 * b[2]) / 255 < 0.5;
  }
  function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + Math.max(0, Math.min(1, a)).toFixed(3) + ")"; }
  function smooth(k) { k = Math.max(0, Math.min(1, k)); return k * k * (3 - 2 * k); }

  // The grid is rebuilt on resize: the A is sampled once per cell, and each
  // cell's depth (how many of its neighbours are also inside) picks how dense
  // its glyph is, so the outline reads lighter than the body.
  function build() {
    var r = mark.getBoundingClientRect();
    var cw = r.width / COLS, ch = cw * 1.3;
    rows = Math.max(8, Math.round(r.height / ch));
    var inside = [];
    for (var y = 0; y < rows; y++) {
      inside.push([]);
      for (var x = 0; x < COLS; x++) inside[y].push(window.AccDots.inA((x + 0.5) / COLS, (y + 0.5) / rows));
    }
    var at = function (x, y) { return y >= 0 && y < rows && x >= 0 && x < COLS && inside[y][x]; };
    cells = []; ghosts = [];
    seed = 7;
    for (var y2 = 0; y2 < rows; y2++) {
      for (var x2 = 0; x2 < COLS; x2++) {
        var t = (x2 + 0.5) / COLS, u = (y2 + 0.5) / rows;
        if (inside[y2][x2]) {
          var n = 0;
          for (var dy = -1; dy <= 1; dy++) for (var dx = -1; dx <= 1; dx++) if ((dx || dy) && at(x2 + dx, y2 + dy)) n++;
          var set = n >= 8 ? CORE : n >= 5 ? MID : EDGE;
          cells.push({ x: x2, y: y2, t: t, u: u, set: set, ch: pick(set), depth: n / 8,
                       k: (1 - u) * 0.5 + t * 0.5, ph: rand() * 6.283 });
        } else {
          // Halo: a sparse ring of dim dashes just outside the outline.
          var near = 0;
          for (var dy2 = -2; dy2 <= 2; dy2++) for (var dx2 = -2; dx2 <= 2; dx2++) if (at(x2 + dx2, y2 + dy2)) near++;
          if (near > 0 && rand() < 0.35) ghosts.push({ x: x2, y: y2, ch: pick(GHOST), ph: rand() * 6.283, k: (1 - u) * 0.5 + t * 0.5 });
        }
      }
    }
    // Two short dashed rules above and below the mark, as in a specimen plate.
    var mid = (COLS / 2) | 0;
    for (var i = -5; i <= 5; i++) {
      ghosts.push({ x: mid + i - 6, y: -2, ch: "-", ph: rand() * 6.283, k: 0.2, rule: true });
      ghosts.push({ x: mid + i + 4, y: rows + 2, ch: "-", ph: rand() * 6.283, k: 0.9, rule: true });
    }
    dust = [];
    for (var d = 0; d < 150; d++) dust.push([rand(), rand(), rand() * 6.283, 0.6 + rand() * 0.9]);
    var hud = document.getElementById("hud-grid");
    if (hud) hud.textContent = "GRID " + COLS + "×" + rows;
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth; H = window.innerHeight;
    canvas.width = Math.floor(W * dpr); canvas.height = Math.floor(H * dpr);
    canvas.style.width = W + "px"; canvas.style.height = H + "px";
    build();
    if (reduced) draw(performance.now());
  }

  function glyphColor(t, u, light, alpha) {
    var hue = 212 + t * 50 + u * 30;
    var l = colors.dark ? 70 + light * 18 : 44 - light * 10;
    return "hsla(" + hue.toFixed(0) + ",85%," + l.toFixed(0) + "%," + Math.max(0, Math.min(1, alpha)).toFixed(3) + ")";
  }

  function draw(now) {
    if (!t0) t0 = now;
    var sec = reduced ? 99 : (now - t0) / 1000;
    var r = mark.getBoundingClientRect();
    var cw = r.width / COLS, ch = r.height / rows;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // dust
    for (var d = 0; d < dust.length; d++) {
      var p = dust[d], px = p[0] * W, py = p[1] * H;
      var tw = reduced ? 0.6 : 0.5 + 0.5 * Math.sin(sec * 0.7 + p[2]);
      ctx.fillStyle = rgba(colors.ink, (colors.dark ? 0.16 : 0.12) * tw * Math.min(1, sec / 1.2));
      ctx.fillRect(px, py, p[3], p[3]);
    }

    ctx.font = "600 " + (cw * 1.32).toFixed(2) + "px " + FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var flash = readyAt ? Math.max(0, 1 - (now - readyAt) / 900) : 0;
    // A slow diagonal light sweep every few seconds, the work pill's sheen.
    var sweep = reduced ? -9 : ((sec * 0.28) % 1.6) - 0.3;
    var bx = box;

    for (var g = 0; g < ghosts.length; g++) {
      var q = ghosts[g];
      var gin = smooth((sec - 0.2 - q.k * 0.9) / 0.5);
      if (gin <= 0) continue;
      var ga = (q.rule ? 0.2 : 0.11) * (0.6 + 0.4 * Math.sin(sec * 1.3 + q.ph)) * gin;
      ctx.fillStyle = rgba(colors.ink, ga * (colors.dark ? 1 : 0.8));
      ctx.fillText(q.ch, r.left + (q.x + 0.5) * cw, r.top + (q.y + 0.5) * ch);
    }

    for (var i = 0; i < cells.length; i++) {
      var c = cells[i];
      var appear = (sec - 0.25 - c.k * 0.95) / 0.35;
      if (appear <= -0.6) continue;
      var x = r.left + (c.x + 0.5) * cw, y = r.top + (c.y + 0.5) * ch;
      if (appear < 0) {
        // not resolved yet: a dim scramble running ahead of the reveal
        ctx.fillStyle = glyphColor(c.t, c.u, 0, 0.18 * (1 + appear / 0.6));
        ctx.fillText(pick(CORE + MID), x, y);
        continue;
      }
      if (!reduced && rand() < 0.004) c.ch = pick(c.set);
      var inBox = c.t >= bx[0] && c.t <= bx[2] && c.u >= bx[1] && c.u <= bx[3];
      var band = Math.exp(-Math.pow((c.k - sweep) / 0.07, 2));
      var light = Math.min(1, (inBox ? 0.45 : 0) + band * 0.6 + flash);
      var alpha = (0.58 + 0.4 * c.depth) * smooth(appear) + light * 0.25;
      if (!reduced) alpha *= 0.92 + 0.08 * Math.sin(sec * 2.1 + c.ph);
      // Each glyph sits on a faint tile, so the A reads as a mosaic of
      // type rather than loose characters.
      ctx.fillStyle = glyphColor(c.t, c.u, light, (0.05 + 0.07 * c.depth + light * 0.06) * smooth(appear));
      ctx.fillRect(x - cw / 2 + 0.5, y - ch / 2 + 0.5, cw - 1, ch - 1);
      ctx.fillStyle = glyphColor(c.t, c.u, light, alpha);
      ctx.fillText(appear < 1 && !reduced ? pick(c.set) : c.ch, x, y);
    }

    drawTag(r, sec, now);
  }

  // The section tag: a hairline frame around the current stage's part of the
  // A with a small solid label on its top edge. It eases toward its target.
  function drawTag(r, sec, now) {
    var show = smooth((sec - 1.1) / 0.5);
    if (show <= 0) return;
    var tb = boxTarget.box;
    for (var i = 0; i < 4; i++) box[i] += (tb[i] - box[i]) * (reduced ? 1 : 0.12);
    var pad = 6;
    var x0 = r.left + box[0] * r.width - pad, y0 = r.top + box[1] * r.height - pad;
    var x1 = r.left + box[2] * r.width + pad, y1 = r.top + box[3] * r.height + pad;
    ctx.strokeStyle = rgba(colors.ink, 0.32 * show);
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(x0) + 0.5, Math.round(y0) + 0.5, Math.round(x1 - x0), Math.round(y1 - y0));
    ctx.font = "600 9.5px " + FONT;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    var text = boxTarget.tag, tw = ctx.measureText(text).width;
    var lx = Math.round(x0), ly = Math.round(y0) - 15;
    ctx.fillStyle = rgba(colors.ink, 0.88 * show);
    ctx.fillRect(lx, ly, Math.ceil(tw + 10), 15);
    ctx.fillStyle = rgba(colors.bg, show);
    ctx.fillText(text, lx + 5, ly + 8);
  }

  function hud(now) {
    var el = document.getElementById("hud-clock");
    if (!el) return;
    var secs = t0 ? (now - t0) / 1000 : 0;
    var f = String(frame).padStart(4, "0");
    el.textContent = "F " + f + "  " + secs.toFixed(2).padStart(5, "0") + "S";
    var st = document.getElementById("hud-stage");
    if (st) st.textContent = (readyAt ? "READY" : "STAGE 0" + (stage + 1) + "/03") + "  " + Math.round(pct) + "%";
  }

  function loop(now) {
    if (!lastDraw || now - lastDraw >= FRAME_MS) {
      frame++;
      draw(now);
      hud(now);
      lastDraw = now;
    }
    raf = requestAnimationFrame(loop);
  }

  window.addEventListener("resize", resize);
  readColors();
  resize();
  if (reduced) {
    draw(performance.now());
  } else {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (raf) cancelAnimationFrame(raf); raf = null; }
      else if (!raf) { lastDraw = 0; raf = requestAnimationFrame(loop); }
    });
    raf = requestAnimationFrame(loop);
  }

  window.AccuGlyphField = {
    setStage: function (p) {
      if (typeof p === "number") pct = Math.max(0, Math.min(100, p));
      if (readyAt) return;
      stage = pct >= 100 ? 2 : pct >= 55 ? 1 : 0;
      boxTarget = STAGES[stage];
      if (reduced) { draw(performance.now()); hud(performance.now()); }
    },
    ready: function () {
      if (!readyAt) readyAt = performance.now();
      pct = 100;
      boxTarget = ALL;
      if (reduced) { draw(performance.now()); hud(performance.now()); }
    },
  };
})();
