/* Accuretta splash field — a halftone dot field in the notch's language.
   Every dot sits on one grid and is sized AND lit by the light falling on
   it: a soft pool around the lockup that widens as boot progresses, two slow
   lights wandering through it, and the theme accent spilling onto the dots
   nearest the matrix A. The field reaches nothing well before the window's
   edges, so it dissolves into the background with no line where it stops,
   and a soft round halo clears it behind the lockup so the A and the text
   always sit on clean ground. At 100% a wave ripples out from the A and a horizon of
   grains rises from the bottom: the send-off into the app.
   API (window.AccuSignalField):
     setProgress(p)  0..1, widens the lit pool
     ready()         plays the send-off
     refresh()       re-reads --fg / --bg / --accent after a theme change
   Robustness: devicePixelRatio-aware (capped at 2x), resize handling, pauses
   while hidden, one still frame under reduced motion. */
(function () {
  "use strict";
  var canvas = document.getElementById("signal-field");
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var GAP = 11, FADE_MS = 900, frameMs = 1000 / 30;
  var VIOLET = [142, 126, 255];
  var W = 0, H = 0, dpr = 1, raf = null, lastDraw = 0, startedAt = 0;
  var colors = { ink: [9, 9, 11], accent: [0, 102, 255], dark: false };
  var progress = 0, shownProgress = 0, readyAt = 0, grains = null;
  // haloed: the whole lockup (A, wordmark, status, stage bar) and the footer
  var HALO = ".lockup, .foot";

  function parseColor(str, fallback) {
    if (!str) return fallback;
    str = String(str).trim();
    var m;
    if ((m = str.match(/^#([0-9a-f]{3,8})$/i))) {
      var h = m[1];
      if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
      return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
    }
    if ((m = str.match(/^rgba?\(([^)]+)\)$/i))) {
      var p = m[1].split(",").map(function (s) { return parseFloat(s); });
      if (p.length >= 3 && p.slice(0, 3).every(Number.isFinite)) return [p[0], p[1], p[2]];
    }
    return fallback;
  }

  function readThemeColors() {
    var cs = getComputedStyle(document.documentElement);
    colors.ink = parseColor(cs.getPropertyValue("--fg"), [9, 9, 11]);
    colors.accent = parseColor(cs.getPropertyValue("--accent"), [0, 102, 255]);
    var bg = parseColor(cs.getPropertyValue("--bg"), [250, 250, 251]);
    colors.dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255 < 0.5;
    // The spill is the accent leaning toward the A's own blue-violet.
    colors.spill = mix(colors.accent, VIOLET, 0.4);
  }

  function mix(a, b, k) { return [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]; }
  function rgba(c, a) { return "rgba(" + (c[0] | 0) + "," + (c[1] | 0) + "," + (c[2] | 0) + "," + Math.max(0, a).toFixed(3) + ")"; }
  function smooth(e0, e1, v) { var k = Math.max(0, Math.min(1, (v - e0) / (e1 - e0))); return k * k * (3 - 2 * k); }
  function easeOut(k) { return 1 - Math.pow(1 - Math.max(0, Math.min(1, k)), 3); }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    grains = null;
    if (reduced) draw(performance.now(), 1);
  }

  function markCenter() {
    var el = document.getElementById("mx") || document.querySelector(".mark");
    if (!el) return { x: W / 2, y: H * 0.42 };
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }

  // The send-off: grains packed toward the rim of a great arc, rising.
  function makeGrains() {
    var s = 11, rand = function () { s = (s * 16807) % 2147483647; return (s - 1) / 2147483646; };
    var g = [];
    for (var i = 0; i < 2200; i++) g.push([rand() * Math.PI, 1 - Math.pow(rand(), 2.4) * 0.22, rand() * 6.28, 0.5 + rand() * 1.4]);
    return g;
  }

  function draw(now, fade) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    var t = reduced ? 0 : now / 1000;
    shownProgress += (progress - shownProgress) * (reduced ? 1 : 0.08);
    var a = markCenter();
    var grow = 0.55 + 0.45 * easeOut(shownProgress);
    var fx = W / 2, fy = a.y + 40;
    var rx = Math.max(Math.min(W * 0.46, 720), 280) * grow, ry = Math.max(H * 0.46, 260) * grow;
    var l1x = fx + rx * 0.55 * Math.sin(t * 0.11), l1y = fy + ry * 0.4 * Math.sin(t * 0.07 + 1);
    var l2x = fx + rx * 0.5 * Math.cos(t * 0.09 + 2), l2y = fy + ry * 0.45 * Math.cos(t * 0.13);
    var age = readyAt ? now - readyAt : 0, wave = readyAt && !reduced && age < 1400;
    var waveR = age * 0.9, waveFade = 1 - age / 1400;
    var inkGain = colors.dark ? 1 : 0.75;
    var ox = (W % GAP) / 2, oy = (H % GAP) / 2;
    for (var y = oy; y < H; y += GAP) {
      for (var x = ox; x < W; x += GAP) {
        var d = Math.hypot((x - fx) / rx, (y - fy) / ry);
        var pool = 1 - smooth(0.15, 1, d);
        if (pool <= 0) continue;
        var da = Math.hypot(x - a.x, y - a.y);
        var spill = Math.exp(-(da * da) / (120 * 120));
        var I = 0.14 + Math.exp(-((x - l1x) * (x - l1x) + (y - l1y) * (y - l1y)) / 52000) * 0.45
                     + Math.exp(-((x - l2x) * (x - l2x) + (y - l2y) * (y - l2y)) / 40000) * 0.32 + spill * 0.55;
        if (wave) { var dw = (da - waveR) / 46; I += Math.exp(-dw * dw) * 0.9 * waveFade; }
        I = Math.min(1, I * pool * fade);
        if (I < 0.04) continue;
        ctx.fillStyle = rgba(mix(colors.ink, colors.spill, Math.min(1, spill * 1.3)), (0.05 + 0.5 * I) * inkGain);
        ctx.beginPath();
        ctx.arc(x, y, 0.45 + 1.3 * I, 0, 6.2832);
        ctx.fill();
      }
    }
    if (readyAt) drawHorizon(now, age, inkGain);
    // The lockup sits on clean ground: one soft round halo clears the field
    // behind the A, the wordmark, the status and the stage bar. (Per-element
    // rectangles read as a box on an even field.)
    ctx.globalCompositeOperation = "destination-out";
    var els = document.querySelectorAll(HALO);
    for (var i = 0; i < els.length; i++) {
      var r = els[i].getBoundingClientRect();
      if (r.width < 1) continue;
      var hx = r.left + r.width / 2, hy = r.top + r.height / 2;
      var hr = Math.max(r.width, r.height) * 0.62 + 40;
      ctx.save();
      ctx.translate(hx, hy);
      ctx.scale(1, Math.min(1, (r.height * 0.62 + 40) / hr));
      var g = ctx.createRadialGradient(0, 0, 0, 0, 0, hr);
      g.addColorStop(0, "rgba(0,0,0,.96)");
      g.addColorStop(0.55, "rgba(0,0,0,.85)");
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = g;
      ctx.fillRect(-hr, -hr, hr * 2, hr * 2);
      ctx.restore();
    }
    ctx.globalCompositeOperation = "source-over";
  }

  function drawHorizon(now, age, inkGain) {
    if (!grains) grains = makeGrains();
    var R = Math.min(W * 0.62, 900), rise = (1 - easeOut(age / 1100)) * 70;
    var cx = W / 2, cy = H + R * 0.36 + rise;
    var col = mix(colors.ink, colors.spill, 0.5), spin = reduced ? 0 : now / 1000 * 0.015;
    var show = Math.min(1, age / 500);
    for (var i = 0; i < grains.length; i++) {
      var g = grains[i], ang = Math.PI + ((g[0] + spin) % Math.PI);
      var x = cx + Math.cos(ang) * R * g[1], y = cy + Math.sin(ang) * R * g[1] * 0.5;
      if (y > H || x < 0 || x > W) continue;
      var rim = (g[1] - 0.78) / 0.22, tw = 0.6 + 0.4 * Math.sin(now / 1000 * 1.7 + g[2]);
      // fade toward the window's sides so the arc never meets an edge
      var side = smooth(0, W * 0.18, Math.min(x, W - x));
      ctx.fillStyle = rgba(col, (0.08 + 0.6 * rim) * tw * show * side * inkGain);
      ctx.fillRect(x, y, g[3], g[3]);
    }
  }

  function loop(now) {
    if (!startedAt) startedAt = now;
    if (!lastDraw || now - lastDraw >= frameMs) {
      draw(now, Math.min(1, (now - startedAt) / FADE_MS));
      lastDraw = now;
    }
    raf = requestAnimationFrame(loop);
  }

  window.addEventListener("resize", resize);
  if (!reduced) {
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (raf) cancelAnimationFrame(raf); raf = null; }
      else if (!raf) { lastDraw = 0; raf = requestAnimationFrame(loop); }
    });
    raf = requestAnimationFrame(loop);
  }
  readThemeColors();
  resize();

  window.AccuSignalField = {
    setProgress: function (p) {
      progress = Math.max(0, Math.min(1, +p || 0));
      if (reduced) draw(performance.now(), 1);
    },
    ready: function () {
      if (!readyAt) readyAt = performance.now();
      progress = 1;
      if (reduced) draw(performance.now(), 1);
    },
    refresh: function () {
      readThemeColors();
      if (reduced) draw(performance.now(), 1);
    }
  };
})();
