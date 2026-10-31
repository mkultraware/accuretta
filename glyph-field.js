/* Accuretta splash — the split A drawn in monospace glyphs over a dot field.
   One canvas covers the window. The A (shape from dot-matrix.js, AccDots.inA)
   is a grid of characters, dense in its body and lighter at its edges, which
   resolves along the notch's diagonal (bottom left to top right) as the
   splash appears and then idles with an occasional re-rolled glyph and a
   slow light sweep. Behind it sits the same quiet dot field as the New
   Session screen (dot-matrix.js AccDots.field): one pool of dots around the
   mark, each twinkling on its own phase and speed, blooming outward from the
   A as it resolves. At ready one ripple leaves the A and the field fades, so
   the hand-off lands on the app's own welcome field.
   API (window.AccuGlyphField):
     setStage(pct)         pct 0..100 (kept for the boot contract)
     ready()               flashes the mark, ripples and fades the field
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
  var CORE = "#@8&%SX", MID = "=+*xo", EDGE = ".:-~";
  var FONT = '"JetBrains Mono", "Cascadia Mono", Consolas, ui-monospace, monospace';
  // Field geometry and timing match the New Session field in dot-matrix.js.
  // The fade is shorter than the ripple: the window swaps to the app as soon
  // as 0.26s after ready, so the ring has to read early.
  var GAP = 18, DOT_R = 1.05, PULSE_MS = 1100, FADE_MS = 650;

  var W = 0, H = 0, dpr = 1, raf = null, lastDraw = 0, t0 = 0;
  var cells = [], rows = 0;
  var colors = { ink: [230, 236, 245], bg: [10, 12, 16], dark: true };
  var pct = 0, readyAt = 0;
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
  function easeOut(k) { return 1 - Math.pow(1 - k, 3); }

  // Same per-dot twinkle as the New Session field: every dot on its own
  // phase and speed, cubed so it rests low and lifts briefly. t is in ms.
  function hash(i, j) {
    var h = Math.sin(i * 127.1 + j * 311.7) * 43758.5453;
    return h - Math.floor(h);
  }
  function twinkle(i, j, t) {
    var ph = hash(i, j) * 6.2832, sp = 0.0006 + hash(j, i) * 0.0011;
    var w = 0.5 + 0.5 * Math.sin(t * sp + ph);
    return w * w * w;
  }

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
    cells = [];
    seed = 7;
    for (var y2 = 0; y2 < rows; y2++) {
      for (var x2 = 0; x2 < COLS; x2++) {
        if (!inside[y2][x2]) continue;
        var t = (x2 + 0.5) / COLS, u = (y2 + 0.5) / rows;
        var n = 0;
        for (var dy = -1; dy <= 1; dy++) for (var dx = -1; dx <= 1; dx++) if ((dx || dy) && at(x2 + dx, y2 + dy)) n++;
        var set = n >= 8 ? CORE : n >= 5 ? MID : EDGE;
        cells.push({ x: x2, y: y2, t: t, u: u, set: set, ch: pick(set), depth: n / 8,
                     k: (1 - u) * 0.5 + t * 0.5, ph: rand() * 6.283 });
      }
    }
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

  // The dot field: a pool centred on the mark that blooms outward as the A
  // resolves. Dots under the A's glyphs are skipped so the mark stays clean.
  // At ready a ring leaves the mark (the New Session ripple) while the whole
  // field fades.
  function drawField(r, sec, now) {
    var cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    var rx = Math.max(r.width * 1.5, W * 0.42), ry = Math.max(r.height * 1.6, H * 0.42);
    var bloom = reduced ? 9 : sec * 0.85;
    var fade = 1, pr = -1, pw = 0, pk = 0;
    if (readyAt) {
      var since = now - readyAt;
      fade = 1 - smooth(since / FADE_MS);
      if (!reduced) {
        pk = Math.min(1, since / PULSE_MS);
        pr = easeOut(pk) * Math.hypot(Math.max(cx, W - cx), Math.max(cy, H - cy));
        pw = 60 + 90 * pk;
      }
    }
    if (fade <= 0.001) return;
    var t = reduced ? 0 : now;
    var ox = (W % GAP) / 2, oy = (H % GAP) / 2;
    for (var y = oy, j = 0; y < H; y += GAP, j++) {
      for (var x = ox, i = 0; x < W; x += GAP, i++) {
        var mt = (x - r.left) / r.width, mu = (y - r.top) / r.height;
        if (mt >= 0 && mt <= 1 && mu >= 0 && mu <= 1 && window.AccDots.inA(mt, mu)) continue;
        var d = Math.hypot((x - cx) / rx, (y - cy) / ry);
        var pool = 1 - smooth((d - 0.18) / 0.82);
        if (pool <= 0) continue;
        var grow = smooth((bloom - d) / 0.35);
        if (grow <= 0) continue;
        var a = (0.13 + 0.3 * twinkle(i, j, t)) * pool * grow;
        var rad = DOT_R, lit = 0;
        if (pr >= 0) {
          var dd = Math.abs(Math.hypot(x - cx, y - cy) - pr);
          if (dd < pw) {
            lit = (1 - dd / pw) * (1 - pk * 0.85);
            a += lit * 0.75;
            rad += lit * 1.5;
          }
        }
        if (lit > 0.04) {
          var hue = 212 + (x / W) * 50 + (y / H) * 30;
          ctx.fillStyle = "hsla(" + hue.toFixed(0) + ",90%,60%," + (a * fade).toFixed(3) + ")";
        } else {
          ctx.fillStyle = rgba(colors.ink, a * fade);
        }
        ctx.beginPath();
        ctx.arc(x, y, rad, 0, 6.2832);
        ctx.fill();
      }
    }
  }

  function draw(now) {
    if (!t0) t0 = now;
    var sec = reduced ? 99 : (now - t0) / 1000;
    var r = mark.getBoundingClientRect();
    var cw = r.width / COLS, ch = r.height / rows;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    drawField(r, sec, now);

    ctx.font = "600 " + (cw * 1.32).toFixed(2) + "px " + FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    var flash = readyAt ? Math.max(0, 1 - (now - readyAt) / 900) : 0;
    // A slow diagonal light sweep every few seconds, the work pill's sheen.
    var sweep = reduced ? -9 : ((sec * 0.28) % 1.6) - 0.3;

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
      var band = Math.exp(-Math.pow((c.k - sweep) / 0.07, 2));
      var light = Math.min(1, band * 0.6 + flash);
      var alpha = (0.58 + 0.4 * c.depth) * smooth(appear) + light * 0.25;
      if (!reduced) alpha *= 0.92 + 0.08 * Math.sin(sec * 2.1 + c.ph);
      // Each glyph sits on a faint tile, so the A reads as a mosaic of
      // type rather than loose characters.
      ctx.fillStyle = glyphColor(c.t, c.u, light, (0.05 + 0.07 * c.depth + light * 0.06) * smooth(appear));
      ctx.fillRect(x - cw / 2 + 0.5, y - ch / 2 + 0.5, cw - 1, ch - 1);
      ctx.fillStyle = glyphColor(c.t, c.u, light, alpha);
      ctx.fillText(appear < 1 && !reduced ? pick(c.set) : c.ch, x, y);
    }
  }

  function loop(now) {
    if (!lastDraw || now - lastDraw >= FRAME_MS) {
      draw(now);
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
    },
    ready: function () {
      if (!readyAt) readyAt = performance.now();
      pct = 100;
      if (reduced) draw(performance.now());
    },
  };
})();
