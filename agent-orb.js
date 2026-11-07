(() => {
  "use strict";

  const PARTICLE_COUNT = 120;
  const MORPH_DURATION = 560;
  // A state that lasts this long eases into its next shape, so a long think
  // or a long command doesn't loop the same motion for minutes.
  const VARIANT_MS = 8000;
  const VARIANT_MORPH = 900;
  const orbs = new Map();
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const labels = {
    idle: "Agent", thinking: "Thinking", composing: "Writing response", working: "Working",
    reading: "Reading", running: "Running a command", searching: "Searching",
    connecting: "Connecting", planning: "Planning", waiting: "Waiting for you",
    error: "Hit an error", done: "Saved",
  };
  let frame = 0;
  let lastPaint = 0;

  const TAU = Math.PI * 2;
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const lerp = (from, to, amount) => from + (to - from) * amount;
  const ease = (amount) => {
    const t = clamp(amount, 0, 1);
    return t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2;
  };
  const rotY = (p, a) => {
    const c = Math.cos(a), s = Math.sin(a);
    return { x: p.x * c + p.z * s, y: p.y, z: -p.x * s + p.z * c };
  };
  const rotX = (p, a) => {
    const c = Math.cos(a), s = Math.sin(a);
    return { x: p.x, y: p.y * c - p.z * s, z: p.y * s + p.z * c };
  };
  const pt = (p, alpha, size, links = 0) => ({ x: p.x, y: p.y, z: p.z, alpha, size, links });
  const scaled = (p, s) => ({ x: p.x * s, y: p.y * s, z: p.z * s });

  function spherePoint(index, time) {
    const latitude = 1 - 2 * (index + 0.5) / PARTICLE_COUNT;
    const radius = Math.sqrt(Math.max(0, 1 - latitude * latitude));
    const angle = index * 2.399963229728653 + time * 0.00018;
    return { x: radius * Math.cos(angle), y: latitude, z: radius * Math.sin(angle), angle, latitude };
  }

  const CUBE_EDGES = [[0, 1], [1, 3], [3, 2], [2, 0], [4, 5], [5, 7], [7, 6], [6, 4], [0, 4], [1, 5], [2, 6], [3, 7]];
  const CUBE_CORNERS = Array.from({ length: 8 }, (_, k) => ({ x: k & 1 ? 0.5 : -0.5, y: k & 2 ? 0.5 : -0.5, z: k & 4 ? 0.5 : -0.5 }));

  // Point t (0..1) along the "done" check mark.
  function checkPoint(t) {
    if (t < 0.38) {
      const u = t / 0.38;
      return { x: lerp(-0.55, -0.12, u), y: lerp(0.02, 0.48, u) };
    }
    const u = (t - 0.38) / 0.62;
    return { x: lerp(-0.12, 0.62, u), y: lerp(0.48, -0.52, u) };
  }

  // Every shape places the same 120 dots, so any shape can morph into any other.
  const SHAPES = {
    idle(i, time) {
      const base = spherePoint(i, time);
      return pt(scaled(base, 0.68 + 0.05 * Math.sin(i * 1.618 + time * 0.0011)), 0.62, 0.82);
    },
    ripple(i, time) {                       // thinking
      const base = spherePoint(i, time);
      const orbit = base.angle + time * 0.00045;
      const ripple = 0.84 + 0.17 * Math.sin(base.latitude * 9 + time * 0.0014);
      return pt({ x: base.x * ripple, y: base.y + 0.14 * Math.sin(orbit * 2 + base.latitude * 5), z: base.z * ripple }, 0.92, 0.94);
    },
    nebula(i, time) {                       // thinking, alternate
      const k = 1 + (i % 5) * 0.15;
      const p = {
        x: 0.64 * Math.sin(i * 1.7 + time * 0.0005 * k) * Math.cos(i * 0.9 + time * 0.0003),
        y: 0.64 * Math.cos(i * 2.3 + time * 0.0004 * k),
        z: 0.64 * Math.sin(i * 0.7 + time * 0.00035 * k),
      };
      return pt(p, 0.6 + 0.35 * ((i * 7) % 10) / 10, 0.9 + ((i * 3) % 5) * 0.06);
    },
    twist(i, time) {                        // writing
      const base = spherePoint(i, time);
      const orbit = base.angle + time * 0.00045;
      return pt({
        x: base.x * (0.7 + 0.26 * Math.cos(base.latitude * 7 - time * 0.0018)),
        y: base.y * (0.8 + 0.17 * Math.cos(orbit * 2)) + 0.13 * Math.sin(orbit * 3 + time * 0.0015),
        z: base.z * (0.78 + 0.2 * Math.sin(base.latitude * 6 + time * 0.0014)),
      }, 0.96, 1.04);
    },
    breathe(i, time) {                      // working
      const base = spherePoint(i, time);
      return pt(scaled(base, 0.8 + 0.19 * Math.sin(time * 0.0026 + i * 0.19)), 0.9, 1.02);
    },
    scanline(i, time) {                     // reading
      const base = spherePoint(i, time * 2);
      const glow = Math.exp(-((base.y * 0.8 - Math.sin(time * 0.0016)) ** 2) * 30);
      const s = 0.74 + 0.2 * glow;
      return pt({ x: base.x * s, y: base.y * 0.8, z: base.z * s }, 0.55 + 0.45 * glow, 0.88 + 0.45 * glow);
    },
    cube(i, time) {                         // running a command
      const a = CUBE_CORNERS[CUBE_EDGES[Math.floor(i / 10)][0]];
      const b = CUBE_CORNERS[CUBE_EDGES[Math.floor(i / 10)][1]];
      const u = (i % 10 + 0.5) / 10;
      let p = { x: lerp(a.x, b.x, u) * 1.08, y: lerp(a.y, b.y, u) * 1.08, z: lerp(a.z, b.z, u) * 1.08 };
      p = rotX(rotY(p, time * 0.0008), 0.55 + 0.15 * Math.sin(time * 0.0006));
      return pt(p, 0.95, 1.08);
    },
    ring(i, time) {                         // searching
      const base = spherePoint(i, time);
      const ring = 0.58 + 0.34 * Math.sin(base.latitude * Math.PI * 0.5) ** 2;
      const x = base.x * ring, z = base.z * ring, y = base.y * 0.52;
      const tilt = 0.62 + 0.2 * Math.sin(time * 0.001);
      return pt({ x, y: y * Math.cos(tilt) - z * Math.sin(tilt), z: y * Math.sin(tilt) + z * Math.cos(tilt) }, 0.86, 0.94);
    },
    radar(i, time) {                        // searching, alternate
      const r = Math.sqrt((i + 0.5) / PARTICLE_COUNT) * 0.86;
      const a = i * 2.399963229728653;
      const behind = ((a - time * 0.0025) % TAU + TAU) % TAU;
      const glow = Math.exp(-behind * 1.6);
      return pt(rotX({ x: r * Math.cos(a), y: 0, z: r * Math.sin(a) }, 1.05), 0.4 + 0.6 * glow, 0.85 + 0.5 * glow);
    },
    network(i, time) {                      // connecting
      const base = spherePoint(i, time);
      return pt(scaled(base, 0.84 + 0.12 * Math.sin(time * 0.0021 + i * 0.8)), 0.94, 0.94, 1);
    },
    globe(i, time) {                        // connecting, alternate
      const spin = time * 0.0006;
      let lat, lon;
      if (i < 66) {                         // three meridian circles, 22 dots each
        const m = Math.floor(i / 22), u = (i % 22) / 22 * TAU;
        lat = Math.asin(Math.sin(u));
        lon = m / 3 * Math.PI + (Math.cos(u) < 0 ? Math.PI : 0) + spin;
      } else {                              // equator (22) and two parallels (16 each)
        const q = i - 66, row = q < 22 ? 1 : q < 38 ? 0 : 2;
        const k = row === 1 ? q : row === 0 ? q - 22 : q - 38;
        lat = (row - 1) * 0.6;
        lon = k / (row === 1 ? 22 : 16) * TAU + spin;
      }
      const p = { x: 0.74 * Math.cos(lat) * Math.cos(lon), y: 0.74 * Math.sin(lat), z: 0.74 * Math.cos(lat) * Math.sin(lon) };
      return pt(rotX(p, 0.35), 0.9, 0.98);
    },
    bands(i, time) {                        // planning
      const base = spherePoint(i, time);
      const orbit = base.angle + time * 0.0006;
      const band = Math.round(base.latitude * 6) / 6;
      const r = Math.sqrt(Math.max(0, 1 - band * band));
      return pt({ x: r * Math.cos(orbit), y: band + 0.05 * Math.sin(orbit * 3 + time * 0.0013), z: r * Math.sin(orbit) }, 0.9, 0.94);
    },
    checklist(i, time) {                    // planning, alternate
      const layer = Math.floor(i / 40), k = i % 40;
      const a = k / 40 * TAU + time * 0.0008 * (layer % 2 ? 1 : -1);
      const on = layer === Math.floor(time / 900) % 3;
      return pt(rotX({ x: 0.62 * Math.cos(a), y: (layer - 1) * 0.48, z: 0.62 * Math.sin(a) }, 0.3), on ? 1 : 0.55, on ? 1.15 : 0.9);
    },
    halo(i, time) {                         // waiting
      const a = i / PARTICLE_COUNT * TAU + time * 0.00024;
      const r = 0.72 + 0.12 * Math.sin(i * 0.37 + time * 0.001);
      return pt({ x: Math.cos(a) * r, y: Math.sin(a) * r, z: 0.1 * Math.sin(a * 2) }, i % 3 === 0 ? 0.9 : 0.42, i % 3 === 0 ? 1 : 0.74);
    },
    heartbeat(i, time) {                    // waiting, alternate
      const ph = (time % 1500) / 1500;
      const beat = Math.exp(-(((ph - 0.1) / 0.045) ** 2)) + 0.6 * Math.exp(-(((ph - 0.3) / 0.05) ** 2));
      return pt(scaled(spherePoint(i, time), 0.62 + 0.16 * beat), 0.7 + 0.3 * beat, 0.9 + 0.25 * beat);
    },
    scatter(i, time) {                      // error
      const base = spherePoint(i, 0);
      const out = 0.5 + 0.5 * Math.sin(time * 0.002);
      const spread = 0.55 + out * (0.25 + (Math.sin(i * 12.9898) * 0.5 + 0.5) * 0.35);
      return pt(scaled(base, spread), 0.5 + 0.4 * (1 - out), 0.92);
    },
    check(i) {                              // done
      const p = checkPoint(i / PARTICLE_COUNT);
      return pt({ x: p.x + Math.sin(i * 12.9898) * 0.02, y: p.y + Math.cos(i * 7.233) * 0.02, z: 0.3 }, 1, 1);
    },
  };

  // Each state's shapes, in the order a long stretch cycles through them.
  const PHASE_SHAPES = {
    idle: ["idle"], thinking: ["ripple", "nebula"], composing: ["twist"], working: ["breathe"],
    reading: ["scanline", "breathe"], running: ["cube", "breathe"], searching: ["ring", "radar"],
    connecting: ["network", "globe"], planning: ["bands", "checklist"], waiting: ["halo", "heartbeat"],
    error: ["scatter"], done: ["check"],
  };

  function getTargetPose(info, time) {
    const list = PHASE_SHAPES[info.phase] || PHASE_SHAPES.idle;
    const shape = SHAPES[list[(info.variant || 0) % list.length]];
    return Array.from({ length: PARTICLE_COUNT }, (_, index) => shape(index, time));
  }

  function mixPose(from, to, amount) {
    if (!from) return to;
    return to.map((point, index) => {
      const source = from[index] || point;
      return {
        x: lerp(source.x, point.x, amount), y: lerp(source.y, point.y, amount), z: lerp(source.z, point.z, amount),
        alpha: lerp(source.alpha, point.alpha, amount), size: lerp(source.size, point.size, amount), links: lerp(source.links, point.links, amount),
      };
    });
  }

  function visiblePose(info, time) {
    const target = getTargetPose(info, time);
    if (!info.from) return target;
    const amount = ease((time - info.transitionStarted) / (info.morphMs || MORPH_DURATION));
    const pose = mixPose(info.from, target, amount);
    if (amount >= 1) info.from = null;
    return pose;
  }

  function checkStroke() {
    const pts = [];
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const p = checkPoint(i / (PARTICLE_COUNT - 1));
      pts.push({ x: 20 + p.x * 15, y: 20 + p.y * 15 });
    }
    return pts;
  }

  function paint(canvas, info, time) {
    const context = canvas.getContext("2d");
    if (!context) return;
    const size = 40;
    const css = canvas.clientWidth || size;
    const ratio = Math.min(devicePixelRatio || 1, 2) * (css / size);
    const px = Math.max(1, Math.round(size * ratio));
    if (canvas.width !== px) canvas.width = canvas.height = px;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size, size);
    context.fillStyle = getComputedStyle(canvas).color;
    if (info.phase === "done" && !info.from) {
      context.strokeStyle = context.fillStyle;
      context.globalAlpha = 1;
      context.lineWidth = 1.7;
      context.lineCap = "round";
      context.lineJoin = "round";
      context.beginPath();
      for (const p of checkStroke()) context.lineTo(p.x, p.y);
      context.stroke();
      context.globalAlpha = 1;
      return;
    }
    // While morphing into "done", the particles converge onto the check path
    // while the stroke draws itself underneath them — the dots dissolve into
    // the line instead of snapping from cloud to vector at the end.
    const morphing = info.phase === "done" && info.from;
    let drawAmount = 0;
    if (morphing) {
      const k = clamp((time - info.transitionStarted) / (info.morphMs || MORPH_DURATION), 0, 1);
      drawAmount = ease(clamp((k - 0.22) / 0.78, 0, 1));
    }
    const points = visiblePose(info, time).map(point => ({ ...point, x: 20 + point.x * 15, y: 20 + point.y * 15 }))
      .sort((a, b) => a.z - b.z);

    context.strokeStyle = context.fillStyle;
    context.lineWidth = 0.45;
    for (let i = 0; i < points.length; i += 4) {
      const source = points[i];
      if (source.links < 0.02 || source.z < -0.15) continue;
      for (let j = i + 4; j < points.length; j += 4) {
        const target = points[j];
        if (target.z < -0.15 || Math.hypot(source.x - target.x, source.y - target.y) > 8.5) continue;
        context.globalAlpha = 0.2 * Math.min(source.links, target.links);
        context.beginPath();
        context.moveTo(source.x, source.y);
        context.lineTo(target.x, target.y);
        context.stroke();
      }
    }
    for (const point of points) {
      const depth = (point.z + 1) / 2;
      let alpha = (0.16 + depth * 0.52) * point.alpha;
      if (morphing) alpha *= 1 - 0.82 * drawAmount;   // dots hand off to the stroke
      context.globalAlpha = clamp(alpha, 0, 1);
      context.beginPath();
      context.arc(point.x, point.y, (0.42 + depth * 0.34) * point.size, 0, Math.PI * 2);
      context.fill();
    }
    if (morphing && drawAmount > 0) {
      const stroke = checkStroke();
      let length = 0;
      for (let i = 1; i < stroke.length; i++) length += Math.hypot(stroke[i].x - stroke[i - 1].x, stroke[i].y - stroke[i - 1].y);
      context.strokeStyle = context.fillStyle;
      context.lineWidth = 1.7;
      context.lineCap = "round";
      context.lineJoin = "round";
      context.setLineDash([length, length]);
      context.lineDashOffset = length * (1 - drawAmount);
      context.globalAlpha = 0.35 + 0.65 * drawAmount;
      context.beginPath();
      for (const p of stroke) context.lineTo(p.x, p.y);
      context.stroke();
      context.setLineDash([]);
    }
    context.globalAlpha = 1;
  }

  function tick(time) {
    frame = 0;
    if (document.hidden || motion.matches) return;
    let active = false;
    for (const [canvas, info] of orbs) {
      if (!canvas.isConnected) { observer.unobserve(canvas); orbs.delete(canvas); continue; }
      if (!info.visible || !canvas.getClientRects().length) continue;
      // Settled states don't repaint frame-by-frame; the theme observer
      // repaints them on palette changes.
      if ((info.phase === "idle" || info.phase === "done") && !info.from) continue;
      active = true;
      const shapes = PHASE_SHAPES[info.phase] || [];
      if (shapes.length > 1 && time - info.variantSince > VARIANT_MS) {
        info.from = visiblePose(info, time);
        info.transitionStarted = time;
        info.morphMs = VARIANT_MORPH;
        info.variant = (info.variant || 0) + 1;
        info.variantSince = time;
      }
      if (time - lastPaint > 32) paint(canvas, info, time);
    }
    if (time - lastPaint > 32) lastPaint = time;
    if (active) frame = requestAnimationFrame(tick);
  }

  function wake() {
    if (!frame && !document.hidden && !motion.matches) frame = requestAnimationFrame(tick);
  }

  const observer = new IntersectionObserver(entries => {
    for (const entry of entries) {
      const info = orbs.get(entry.target);
      if (info) info.visible = entry.isIntersecting;
    }
    wake();
  });

  function attach(root) {
    const canvases = root.matches?.("canvas.agent-orb") ? [root] : [...(root.querySelectorAll?.("canvas.agent-orb") || [])];
    for (const canvas of canvases) {
      if (orbs.has(canvas)) continue;
      const phase = Object.hasOwn(labels, canvas.dataset.state || "") ? canvas.dataset.state : "idle";
      const info = {
        visible: false, phase, from: null, transitionStarted: 0,
        morphMs: MORPH_DURATION, variant: 0, variantSince: performance.now(),
      };
      orbs.set(canvas, info);
      observer.observe(canvas);
      paint(canvas, info, 0);
    }
  }

  function setState(row, phase) {
    const canvas = row?.querySelector("canvas.agent-orb");
    if (!canvas) return;
    phase = Object.hasOwn(labels, phase) ? phase : "idle";
    if (!orbs.has(canvas)) attach(canvas);
    const info = orbs.get(canvas);
    if (!info || info.phase === phase) return;
    const time = performance.now();
    if (motion.matches) info.from = null;
    else {
      info.from = visiblePose(info, time);
      info.transitionStarted = time;
      info.morphMs = MORPH_DURATION;
    }
    info.phase = phase;
    info.variant = 0;
    info.variantSince = time;
    canvas.dataset.state = phase;
    canvas.parentElement.title = labels[phase];
    canvas.parentElement.setAttribute("aria-label", labels[phase]);
    paint(canvas, info, time);
    wake();
  }

  const changes = new MutationObserver(records => {
    for (const record of records) for (const node of record.addedNodes) if (node.nodeType === 1) attach(node);
    for (const canvas of orbs.keys()) if (!canvas.isConnected) { observer.unobserve(canvas); orbs.delete(canvas); }
    wake();
  });
  changes.observe(document.getElementById("chat-inner") || document.body, { childList: true, subtree: true });
  new MutationObserver(() => {
    const time = performance.now();
    for (const [canvas, info] of orbs) paint(canvas, info, time);
    wake();
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style"] });
  document.addEventListener("visibilitychange", wake);
  motion.addEventListener("change", () => {
    const time = performance.now();
    for (const [canvas, info] of orbs) {
      if (motion.matches) info.from = null;
      paint(canvas, info, time);
    }
    wake();
  });
  attach(document);
  window.AccurettaOrb = { setState };
})();
