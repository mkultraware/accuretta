(() => {
  "use strict";

  const PARTICLE_COUNT = 120;
  const MORPH_DURATION = 560;
  const orbs = new Map();
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const labels = {
    idle: "Agent", thinking: "Thinking", composing: "Writing response", working: "Working",
    searching: "Searching", connecting: "Connecting", planning: "Planning", waiting: "Waiting for you",
  };
  let frame = 0;
  let lastPaint = 0;

  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const lerp = (from, to, amount) => from + (to - from) * amount;
  const ease = (amount) => {
    const t = clamp(amount, 0, 1);
    return t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2;
  };

  function spherePoint(index, time) {
    const latitude = 1 - 2 * (index + 0.5) / PARTICLE_COUNT;
    const radius = Math.sqrt(Math.max(0, 1 - latitude * latitude));
    const angle = index * 2.399963229728653 + time * 0.00018;
    return { x: radius * Math.cos(angle), y: latitude, z: radius * Math.sin(angle), angle, latitude };
  }

  function targetPoint(phase, index, time) {
    const base = spherePoint(index, time);
    const wave = Math.sin(index * 1.618 + time * 0.0011);
    const orbit = base.angle + time * 0.00045;
    let { x, y, z } = base;
    let alpha = 0.75;
    let size = 0.92;
    let links = 0;

    switch (phase) {
      case "thinking": {
        const ripple = 0.88 + 0.12 * Math.sin(base.latitude * 9 + time * 0.0012);
        x *= ripple;
        z *= ripple;
        y += 0.1 * Math.sin(orbit * 2 + base.latitude * 5);
        alpha = 0.92;
        break;
      }
      case "composing": {
        const twist = 0.74 + 0.2 * Math.cos(base.latitude * 7 - time * 0.0016);
        x *= twist;
        z *= 0.8 + 0.16 * Math.sin(base.latitude * 6 + time * 0.0012);
        y *= 0.82 + 0.13 * Math.cos(orbit * 2);
        y += 0.09 * Math.sin(orbit * 3 + time * 0.0013);
        alpha = 0.96;
        size = 1.04;
        break;
      }
      case "working": {
        const breath = 0.82 + 0.14 * Math.sin(time * 0.0024 + index * 0.19);
        x *= breath;
        y *= breath;
        z *= breath;
        alpha = 0.88;
        size = 1.02;
        break;
      }
      case "searching": {
        const ring = 0.58 + 0.34 * Math.sin(base.latitude * Math.PI * 0.5) ** 2;
        x *= ring;
        z *= ring;
        y *= 0.52;
        const tilt = 0.62 + 0.12 * Math.sin(time * 0.0008);
        const tiltedY = y * Math.cos(tilt) - z * Math.sin(tilt);
        z = y * Math.sin(tilt) + z * Math.cos(tilt);
        y = tiltedY;
        alpha = 0.82;
        break;
      }
      case "connecting": {
        const pulse = 0.86 + 0.08 * Math.sin(time * 0.0019 + index * 0.8);
        x *= pulse;
        y *= pulse;
        z *= pulse;
        alpha = 0.94;
        links = 1;
        break;
      }
      case "planning": {
        const band = Math.round(base.latitude * 6) / 6;
        const bandRadius = Math.sqrt(Math.max(0, 1 - band * band));
        x = bandRadius * Math.cos(orbit);
        z = bandRadius * Math.sin(orbit);
        y = band + 0.025 * Math.sin(orbit * 3 + time * 0.001);
        alpha = 0.9;
        break;
      }
      case "waiting": {
        const halo = index / PARTICLE_COUNT * Math.PI * 2 + time * 0.00018;
        const radius = 0.72 + 0.08 * Math.sin(index * 0.37 + time * 0.0008);
        x = Math.cos(halo) * radius;
        y = Math.sin(halo) * radius;
        z = 0.1 * Math.sin(halo * 2);
        alpha = index % 3 === 0 ? 0.85 : 0.34;
        size = index % 3 === 0 ? 1 : 0.7;
        break;
      }
      default: {
        const settle = 0.68 + 0.05 * wave;
        x *= settle;
        y *= settle;
        z *= settle;
        alpha = 0.62;
        size = 0.82;
      }
    }
    return { x, y, z, alpha, size, links };
  }

  function getTargetPose(phase, time) {
    return Array.from({ length: PARTICLE_COUNT }, (_, index) => targetPoint(phase, index, time));
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
    const target = getTargetPose(info.phase, time);
    if (!info.from) return target;
    const amount = ease((time - info.transitionStarted) / MORPH_DURATION);
    const pose = mixPose(info.from, target, amount);
    if (amount >= 1) info.from = null;
    return pose;
  }

  function paint(canvas, info, time) {
    const context = canvas.getContext("2d");
    if (!context) return;
    const size = 40;
    const ratio = Math.min(devicePixelRatio || 1, 2);
    if (canvas.width !== size * ratio) canvas.width = canvas.height = size * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, size, size);
    context.fillStyle = getComputedStyle(canvas).color;
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
        context.globalAlpha = 0.15 * Math.min(source.links, target.links);
        context.beginPath();
        context.moveTo(source.x, source.y);
        context.lineTo(target.x, target.y);
        context.stroke();
      }
    }
    for (const point of points) {
      const depth = (point.z + 1) / 2;
      context.globalAlpha = (0.16 + depth * 0.52) * point.alpha;
      context.beginPath();
      context.arc(point.x, point.y, (0.42 + depth * 0.34) * point.size, 0, Math.PI * 2);
      context.fill();
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
      if (info.phase === "idle" && !info.from) continue;
      active = true;
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
      const info = { visible: false, phase: canvas.dataset.state || "idle", from: null, transitionStarted: 0 };
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
    }
    info.phase = phase;
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
