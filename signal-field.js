/* Accuretta splash signal field — quiet Canvas 2D dot field adapted from the
   ThreeUI "Signal Particles" background (PredictiveArcCanvas variant).
   Two slow travelling sine waves decide which grid points light up; a few
   deterministic points carry the theme accent. Colours are read from the
   active theme's CSS custom properties (--fg / --accent / --fg-muted), so
   the field always matches the theme the user last used. Call
   window.AccuSignalField.refresh() after a theme change.
   Robustness: devicePixelRatio-aware (capped at 2x), resize handling,
   pauses while the tab is hidden, and renders one static frame under
   prefers-reduced-motion. */
(function () {
  "use strict";
  var canvas = document.getElementById("signal-field");
  if (!canvas || !canvas.getContext) return;
  var ctx = canvas.getContext("2d");
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var spacing = 18, dotR = 1.4, W = 0, H = 0, dpr = 1, raf = null;
  var frameMs = 1000 / 30, lastDraw = 0;
  var pointer = { x: -9999, y: -9999, active: false };
  var colors = { base: [9, 9, 11], accent: [0, 102, 255], muted: [82, 82, 91] };

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
    colors.muted = parseColor(cs.getPropertyValue("--fg-muted") || cs.getPropertyValue("--muted"), [82, 82, 91]);
  }

  function rgba(c, a) {
    return "rgba(" + (c[0] | 0) + "," + (c[1] | 0) + "," + (c[2] | 0) + "," + a.toFixed(3) + ")";
  }

  function resize() {
    dpr = Math.min(2, window.devicePixelRatio || 1);
    spacing = window.matchMedia && window.matchMedia("(max-width: 600px)").matches ? 22 : 18;
    W = canvas.width = Math.floor(window.innerWidth * dpr);
    H = canvas.height = Math.floor(window.innerHeight * dpr);
    canvas.style.width = window.innerWidth + "px";
    canvas.style.height = window.innerHeight + "px";
    if (reduced) draw(0);
  }

  function draw(time) {
    ctx.clearRect(0, 0, W, H);
    var s = spacing * dpr;
    var cols = Math.floor(W / s);
    var rows = Math.floor(H / s);
    var ox = (W - cols * s) / 2;
    var oy = (H - rows * s) / 2;
    var pr = 150 * dpr; // pointer influence radius
    for (var i = 0; i <= cols; i++) {
      for (var j = 0; j <= rows; j++) {
        var x = ox + i * s;
        var y = oy + j * s;
        var nx = i * 0.1, ny = j * 0.1;
        var wave1 = Math.sin(nx + time * 0.5) * Math.cos(ny - time * 0.3);
        var wave2 = Math.sin(nx * 0.5 - ny * 0.5 + time * 0.8);
        var value = wave1 + wave2;
        if (pointer.active) {
          var dx = x - pointer.x, dy = y - pointer.y;
          var d2 = dx * dx + dy * dy;
          if (d2 < pr * pr) value += (1 - Math.sqrt(d2) / pr) * 0.9;
        }
        if (value > 0.1) {
          var h = Math.sin(i * 12.34) * Math.cos(j * 56.78);
          var fill;
          if (h > 0.98) fill = rgba(colors.accent, 0.85);
          else if (h < -0.98) fill = rgba(colors.muted, 0.8);
          else {
            var a = Math.min(0.5, (value - 0.1) * 0.7);
            fill = rgba(colors.base, a);
          }
          ctx.beginPath();
          ctx.arc(x, y, dotR * dpr, 0, 6.2832);
          ctx.fillStyle = fill;
          ctx.fill();
        }
      }
    }
  }

  function loop(t) {
    if (!lastDraw || t - lastDraw >= frameMs) {
      draw(t * 0.0012);
      lastDraw = t;
    }
    raf = requestAnimationFrame(loop);
  }

  window.addEventListener("resize", resize);
  if (!reduced) {
    if (!window.matchMedia || window.matchMedia("(pointer: fine)").matches) {
      window.addEventListener("pointermove", function (e) {
        pointer.x = e.clientX * dpr;
        pointer.y = e.clientY * dpr;
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
      if (reduced) draw(0);
    }
  };
})();
