/* Accuretta splash dot field — the same quiet pool as the app's welcome
   screen (dot-matrix.js), so the splash hands off to the app without a
   change of language. A grid of dots sits in an elliptical pool around the
   lockup and fades out well before the edges; every dot twinkles on its own
   phase and speed, so the light never moves as one block. A few fixed dots
   carry the theme accent, and dots near the pointer brighten.
   Colours come from the active theme's --fg / --accent. Call
   window.AccuSignalField.refresh() after a theme change.
   Robustness: devicePixelRatio-aware (capped at 2x), resize handling,
   pauses while the tab is hidden, one still frame under reduced motion. */
(function () {
  "use strict";
  var canvas = document.getElementById("signal-field");
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var GAP = 18, DOT_R = 1.1, FADE_MS = 700, frameMs = 1000 / 30;
  var W = 0, H = 0, dpr = 1, raf = null, lastDraw = 0, startedAt = 0;
  var pointer = { x: -9999, y: -9999, active: false };
  var colors = { base: [9, 9, 11], accent: [0, 102, 255] };

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
    colors.base = parseColor(cs.getPropertyValue("--fg"), [9, 9, 11]);
    colors.accent = parseColor(cs.getPropertyValue("--accent"), [0, 102, 255]);
  }

  function rgba(c, a) {
    return "rgba(" + (c[0] | 0) + "," + (c[1] | 0) + "," + (c[2] | 0) + "," + a.toFixed(3) + ")";
  }

  function hash(i, j) {
    var h = Math.sin(i * 127.1 + j * 311.7) * 43758.5453;
    return h - Math.floor(h);
  }

  // 0..1, resting near zero with brief independent flares.
  function twinkle(i, j, t) {
    var ph = hash(i, j) * 6.2832, sp = 0.0006 + hash(j, i) * 0.0011;
    var w = 0.5 + 0.5 * Math.sin(t * sp + ph);
    return w * w * w;
  }

  function smooth(e0, e1, v) {
    var k = Math.max(0, Math.min(1, (v - e0) / (e1 - e0)));
    return k * k * (3 - 2 * k);
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = Math.floor(W * dpr);
    canvas.height = Math.floor(H * dpr);
    canvas.style.width = W + "px";
    canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    if (reduced) draw(0, 1);
  }

  function draw(t, fade) {
    ctx.clearRect(0, 0, W, H);
    var fx = W / 2, fy = H * 0.47;
    var rx = Math.max(Math.min(W * 0.42, 640), 260), ry = Math.max(H * 0.42, 240);
    var pr = 150;
    var ox = (W % GAP) / 2, oy = (H % GAP) / 2;
    for (var y = oy, j = 0; y < H; y += GAP, j++) {
      for (var x = ox, i = 0; x < W; x += GAP, i++) {
        var d = Math.hypot((x - fx) / rx, (y - fy) / ry);
        var pool = 1 - smooth(0.2, 1, d);
        var near = 0;
        if (pointer.active) {
          var pd = Math.hypot(x - pointer.x, y - pointer.y);
          if (pd < pr) near = 1 - pd / pr;
        }
        if (pool <= 0 && near <= 0) continue;
        var tw = twinkle(i, j, t);
        var accent = hash(i + 7, j + 3) > 0.985;
        var a = ((accent ? 0.2 : 0.11) + (accent ? 0.5 : 0.3) * tw) * pool + near * 0.35;
        ctx.fillStyle = rgba(accent ? colors.accent : colors.base, Math.min(0.85, a) * fade);
        ctx.beginPath();
        ctx.arc(x, y, DOT_R + near * 0.6, 0, 6.2832);
        ctx.fill();
      }
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
    if (!window.matchMedia || window.matchMedia("(pointer: fine)").matches) {
      window.addEventListener("pointermove", function (e) {
        pointer.x = e.clientX;
        pointer.y = e.clientY;
        pointer.active = true;
      }, { passive: true });
      document.addEventListener("mouseleave", function () { pointer.active = false; });
    }
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) { if (raf) cancelAnimationFrame(raf); raf = null; }
      else if (!raf) { lastDraw = 0; raf = requestAnimationFrame(loop); }
    });
    raf = requestAnimationFrame(loop);
  }
  readThemeColors();
  resize();

  window.AccuSignalField = {
    refresh: function () {
      readThemeColors();
      if (reduced) draw(0, 1);
    }
  };
})();
