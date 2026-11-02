/* Red-team instruments: small dot-matrix canvases that live inside the
   engagement cards without changing their size.
     tape(canvas)        frontend audit: the site's JavaScript streams past a
                         read head, raw bytes on the right, decoded on the left
     phases(canvas)      assessment: four uneven segments, probe packets out
                         and responses back on every tool call
     bucket(canvas, key) passive intel: a 7x3 matrix that fills with records
   One shared loop runs only while an instrument is moving; each settles to a
   still frame when its work is done, and redraws when its card is resized or
   moved (deck to chat). Colors come from the card's theme tokens.
   window.AccRtViz is the API. */
(function () {
  "use strict";

  const reduced = !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const smooth = t => { t = clamp(t, 0, 1); return t * t * (3 - 2 * t); };
  const easeOut = t => 1 - Math.pow(1 - clamp(t, 0, 1), 3);
  const easeIn = t => Math.pow(clamp(t, 0, 1), 3);
  const easeInOut = t => { t = clamp(t, 0, 1); return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  // lands with a small overshoot, for dots arriving
  const spring = t => t <= 0 ? 0 : t >= 1.4 ? 1 : 1 - Math.exp(-6.5 * t) * Math.cos(9.5 * t);
  function hash(a, b = 0) {
    const v = Math.sin(a * 127.1 + b * 311.7) * 43758.5453;
    return v - Math.floor(v);
  }

  // ---- colors: theme tokens resolved in the card's own context ----
  let swatch = null;
  function toRgb(value) {
    // a 1px canvas reads any CSS color syntax (rgb, color(), oklab) as bytes
    if (!swatch) swatch = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
    swatch.clearRect(0, 0, 1, 1);
    swatch.fillStyle = "#808080";
    swatch.fillStyle = value;
    swatch.fillRect(0, 0, 1, 1);
    const d = swatch.getImageData(0, 0, 1, 1).data;
    return [d[0], d[1], d[2]];
  }
  function readPalette(canvas, names) {
    const host = canvas.parentElement || document.body;
    const probe = document.createElement("i");
    probe.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none";
    host.appendChild(probe);
    const out = {};
    for (const [key, expr] of Object.entries(names)) {
      probe.style.color = "";
      probe.style.color = expr;
      out[key] = toRgb(getComputedStyle(probe).color);
    }
    probe.remove();
    return out;
  }
  const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${clamp(a, 0, 1).toFixed(3)})`;

  // ---- the shared loop ----
  const all = new Set();
  const live = new Set();
  let raf = 0;
  function schedule() {
    if (!raf && !document.hidden) raf = requestAnimationFrame(loop);
  }
  function loop(now) {
    raf = 0;
    for (const inst of [...live]) {
      let more = false;
      try { more = inst.paint(now); } catch (e) { console.error("rt-visuals paint failed", e); }
      if (!more) live.delete(inst);
    }
    if (live.size) schedule();
  }
  document.addEventListener("visibilitychange", () => { if (live.size) schedule(); });
  new MutationObserver(() => {
    for (const inst of [...all]) {
      if (!inst.canvas.isConnected) { all.delete(inst); continue; }
      inst.palette = null;
      inst.wake();
    }
  }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

  class Instrument {
    constructor(canvas, colors) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.colors = colors;
      this.palette = null;
      this.w = 0;
      this.h = 0;
      this.dpr = 1;
      this.last = 0;
      all.add(this);
      if (typeof ResizeObserver === "function") {
        this.resizer = new ResizeObserver(() => this.wake());
        this.resizer.observe(canvas);
      }
    }
    wake() {
      live.add(this);
      schedule();
    }
    // time since a moment, jumping to the end state under reduced motion
    age(now, since) { return reduced ? 1e6 : now - since; }
    paint(now) {
      if (!this.canvas.isConnected) return false;
      const rect = this.canvas.getBoundingClientRect();
      const w = Math.round(rect.width);
      const h = Math.round(rect.height);
      if (w < 2 || h < 2) return false;
      const dpr = Math.min(2, window.devicePixelRatio || 1);
      if (w !== this.w || h !== this.h || dpr !== this.dpr) {
        this.w = w;
        this.h = h;
        this.dpr = dpr;
        this.canvas.width = Math.round(w * dpr);
        this.canvas.height = Math.round(h * dpr);
      }
      if (!this.palette) this.palette = readPalette(this.canvas, this.colors());
      // clamped both ways: a long stall must not jump, and a timestamp from
      // another clock must never run the smoothing backwards
      const dt = this.last ? clamp(now - this.last, 0, 64) : 16;
      this.last = now;
      const ctx = this.ctx;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      const more = this.draw(ctx, now, dt) && !reduced;
      if (!more) this.last = 0;
      return more;
    }
    dot(x, y, r, color, alpha) {
      if (alpha <= 0.004 || r <= 0.05) return;
      const ctx = this.ctx;
      ctx.fillStyle = rgba(color, alpha);
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }
    glow(x, y, radius, color, alpha) {
      if (alpha <= 0.004) return;
      const ctx = this.ctx;
      const g = ctx.createRadialGradient(x, y, 0, x, y, radius);
      g.addColorStop(0, rgba(color, alpha));
      g.addColorStop(1, rgba(color, 0));
      ctx.fillStyle = g;
      ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
    }
  }

  // ---- tape: the frontend audit ----
  // Columns of dots are the site's JavaScript. Every 8 columns hold one word
  // (3 to 7 columns of code, then a gap); some words are string literals, and
  // the odd gap is a bundle boundary drawn as sprocket marks, where the tape
  // pauses as if fetching the next chunk. Right of the head is raw, left of it
  // decoded. The tape is bursty, slow to start and quick to stop.
  const HEAD = 0.62;
  class Tape extends Instrument {
    constructor(canvas) {
      super(canvas, () => ({
        fg: "var(--fg)", faint: "var(--fg-faint)", accent: "var(--accent)",
        tone: "var(--sr-tone, var(--accent))", success: "var(--success)",
        warning: "var(--warning)", danger: "var(--danger)",
      }));
      this.state = "idle";
      this.since = performance.now();
      this.offset = Math.floor(Math.random() * 6000);
      this.speed = 0;
      this.pauseUntil = 0;
      this.pausedAt = -1;
      this.crossedAt = -1;
      this.chunkAt = -1e6;
      this.marks = 0;
      this.tone = "success";
      this.wake();
    }
    set(state, info = {}) {
      if (state === this.state && state !== "complete") return;
      this.state = state;
      this.since = performance.now();
      this.marks = clamp(Math.round(info.marks || 0), 0, 6);
      this.tone = info.tone || "success";
      this.palette = null;
      this.wake();
    }
    draw(ctx, now, dt) {
      const { w, h } = this;
      const p = this.palette;
      const rows = h >= 18 ? 5 : 4;
      const sy = h / rows;
      const sx = 3.6;
      const r0 = Math.min(sx, sy) * 0.29;
      const age = this.age(now, this.since);
      const state = this.state;
      const running = state === "running";
      const final = state === "complete" ? (this.tone === "danger" ? p.danger : this.tone === "warning" ? p.warning : p.success) : p.tone;

      // speed: a swell of two slow waves, held at zero during a chunk pause
      let target = 0;
      if (running) {
        const swell = 0.5 + 0.5 * Math.sin(now * 0.0013) * Math.sin(now * 0.00071 + 1.7);
        target = now < this.pauseUntil ? 0 : 30 + 64 * swell;
      }
      const k = 1 - Math.exp(-dt / (target > this.speed ? 280 : 110));
      this.speed += (target - this.speed) * k;
      if (!running && age > 520) this.speed = 0;
      this.offset += this.speed * dt / 1000;

      // where reading stands: parked while idle, sweeping to the end on completion
      let hx = w * HEAD;
      let headAlpha = 1;
      if (state === "idle") headAlpha = 0.35;
      if (state === "complete") hx = w * HEAD + (w * (1 - HEAD) + 8) * easeInOut((age - 480) / 640);
      if (state === "stopped") headAlpha = 1 - smooth(age / 420);
      if (state === "error") headAlpha = 1 - smooth((age - 220) / 320);
      const readTo = state === "idle" ? -1 : hx;
      const shake = state === "error" ? Math.sin(age * 0.09) * 2.2 * Math.exp(-age / 130) : 0;
      const quiet = state === "stopped" ? smooth(age / 420) * 0.65 : 0;

      // the clear sweep after a clean completion
      const sweepT = (age - 1180) / 760;
      const sweepX = -24 + (w + 48) * easeInOut(sweepT);
      const clean = state === "complete" && this.marks === 0;

      // findings: the decoded words with the highest pick score, in view
      const first = Math.floor(this.offset / sx);
      const shift = this.offset - first * sx;
      const cols = Math.ceil(w / sx) + 1;
      const picks = new Map();
      if (state === "complete" && this.marks) {
        const seen = [];
        for (let i = 0; i < cols; i += 1) {
          const kk = first + i;
          const block = Math.floor(kk / 8);
          if (kk - block * 8 !== 0) continue;
          const x = i * sx - shift + sx / 2;
          const len = 3 + Math.floor(hash(block, 1) * 5);
          if (x < w * 0.08 || x + len * sx > w * 0.96) continue;
          seen.push([hash(block, 9), block]);
        }
        seen.sort((a, b) => b[0] - a[0]);
        seen.slice(0, this.marks).sort((a, b) => a[1] - b[1]).forEach(([, block], i) => picks.set(block, i));
      }

      for (let i = 0; i < cols; i += 1) {
        const kk = first + i;
        const x = i * sx - shift + sx / 2 + shake;
        const block = Math.floor(kk / 8);
        const pos = kk - block * 8;
        const len = 3 + Math.floor(hash(block, 1) * 5);
        const inWord = pos < len;
        const boundary = pos === 7 && hash(block, 2) < 0.11;
        const literal = hash(block, 3) < 0.24;
        const glance = hash(block, 4) < 0.06;
        const decoded = x < readTo;
        const d = x - hx;
        const lens = headAlpha > 0.05 && state !== "idle" ? Math.max(0, 1 - Math.abs(d) / 10) * headAlpha : 0;
        const edge = smooth(x / (w * 0.16)) * smooth((w - x) / 10);

        // chunk boundaries: pause just before the head, pulse as they cross
        if (boundary && running) {
          if (d > 0 && d <= 4 && this.pausedAt !== kk) {
            this.pausedAt = kk;
            this.pauseUntil = now + 160 + hash(kk, 4) * 420;
          }
          if (d <= 0 && this.crossedAt !== kk && this.pausedAt === kk) {
            this.crossedAt = kk;
            this.chunkAt = now;
          }
        }

        const pick = picks.get(block);
        const pickT = pick === undefined ? -1 : (age - 1180 - pick * 120) / 320;
        const glanceF = running && glance && d < 0 && d > -34 ? 1 + d / 34 : 0;
        for (let r = 0; r < rows; r += 1) {
          const y = sy * (r + 0.5);
          const bit = inWord && hash(kk, r + 7) < 0.6;
          let color;
          let alpha;
          let rad = r0;
          if (decoded) {
            if (bit) {
              color = literal ? mix(p.fg, p.accent, 0.72) : p.fg;
              alpha = literal ? 0.64 : 0.46;
            } else {
              color = p.faint;
              alpha = 0.1;
              rad = r0 * 0.55;
            }
          } else {
            const shimmer = running ? hash(kk, r + Math.floor(now / 140)) : 0.5;
            color = p.faint;
            alpha = (bit ? 0.3 : 0.1) * (0.75 + 0.5 * shimmer);
            rad = bit ? r0 * 0.9 : r0 * 0.55;
          }
          if (glanceF > 0 && bit) {
            color = mix(color, p.tone, glanceF);
            alpha = Math.max(alpha, 0.95 * glanceF);
          }
          if (lens > 0 && (bit || !decoded)) {
            rad *= 1 + 0.9 * lens;
            alpha = Math.min(1, alpha + 0.5 * lens);
            color = mix(color, p.tone, 0.5 * lens);
          }
          if (state === "error" && !decoded) color = mix(color, p.danger, 0.55);
          if (clean && decoded && bit) {
            const band = Math.exp(-Math.pow((x - sweepX) / 16, 2));
            const past = x < sweepX ? 0.2 : 0;
            color = mix(color, p.success, Math.min(1, band * 0.9 + past));
            alpha = Math.min(1, alpha + band * 0.45);
            rad *= 1 + band * 0.5;
          }
          if (pickT > 0 && bit) {
            color = final;
            alpha = 1;
            rad = r0 * (1.15 * spring(pickT) + 0.05);
          }
          if (quiet) {
            color = mix(color, p.faint, quiet);
            alpha *= 1 - quiet * 0.35;
          }
          this.dot(x, y, rad, color, alpha * edge);
        }

        // brackets: around a glanced word briefly, around findings for good
        const bracketAlpha = Math.max(glanceF * 0.8, pickT > 0 ? smooth(pickT * 1.6) : 0);
        if (bracketAlpha > 0.01 && (pos === 0 || pos === len - 1)) {
          const bx = pos === 0 ? x - sx * 0.62 : x + sx * 0.62;
          const dir = pos === 0 ? 1 : -1;
          ctx.fillStyle = rgba(pickT > 0 ? final : p.tone, bracketAlpha * edge);
          ctx.fillRect(bx - 0.5, 0.5, 1, h - 1);
          ctx.fillRect(dir > 0 ? bx - 0.5 : bx - 1.5, 0.5, 2, 1);
          ctx.fillRect(dir > 0 ? bx - 0.5 : bx - 1.5, h - 1.5, 2, 1);
          if (pickT > 0 && pos === 0) this.glow(x + (len - 1) * sx / 2, h / 2, len * sx, final, 0.16 * smooth(pickT));
        }
        if (boundary) {
          ctx.fillStyle = rgba(p.faint, 0.6 * edge * (1 - quiet * 0.5));
          ctx.fillRect(x - 0.5, 0, 1, 2);
          ctx.fillRect(x - 0.5, h - 2, 1, 2);
        }
      }

      // the read head: a hairline with a notch on top, glowing harder as a
      // chunk boundary goes through
      if (headAlpha > 0.01 && hx < w + 4) {
        const color = state === "error" ? p.danger : state === "complete" ? final : p.tone;
        const boost = 1 + 1.6 * Math.exp(-(now - this.chunkAt) / 260);
        if (state !== "idle") this.glow(hx, h / 2, 16, color, 0.22 * boost * headAlpha);
        ctx.fillStyle = rgba(color, 0.9 * headAlpha);
        ctx.fillRect(Math.round(hx) - 0.5, 0, 1, h);
        ctx.beginPath();
        ctx.moveTo(hx - 2.6, 0);
        ctx.lineTo(hx + 2.6, 0);
        ctx.lineTo(hx, 2.8);
        ctx.closePath();
        ctx.fill();
      }

      if (running) return true;
      if (state === "complete") return age < 1180 + (this.marks ? this.marks * 120 + 700 : 820);
      if (state === "stopped") return age < 640;
      if (state === "error") return age < 640;
      return false;
    }
  }

  // ---- phases: the assessment track ----
  // Recon, Validate, Exploit and Report as uneven runs of dots. While the turn
  // is live a carrier glides through the active segment with a fast attack and
  // a slow release. Each tool call fires a probe packet down the segment and
  // its result comes back as a return packet; a blocked call hits a wall.
  const WEIGHTS = [1.35, 0.9, 1.15, 0.6];
  class Phases extends Instrument {
    constructor(canvas) {
      super(canvas, () => ({
        fg: "var(--fg)", faint: "var(--fg-faint)", muted: "var(--fg-muted)",
        accent: "var(--accent)", success: "var(--success)", warning: "var(--warning)",
        danger: "var(--danger)",
      }));
      this.active = 0;
      this.visited = new Set([0]);
      this.closed = false;
      this.closedAt = 0;
      this.guarded = false;
      this.flags = 0;
      this.flagAt = -1e6;
      this.isLive = true;
      this.packets = [];
      this.t0 = performance.now();
      this.wake();
    }
    update(next) {
      const now = performance.now();
      if (next.active !== undefined && next.active !== this.active) {
        this.active = next.active;
        this.t0 = now;
      }
      if (next.visited) this.visited = new Set(next.visited);
      if (next.closed && !this.closed) this.closedAt = now;
      if (next.closed !== undefined) this.closed = !!next.closed;
      if (next.guarded !== undefined) this.guarded = !!next.guarded;
      if (next.flags !== undefined && next.flags > this.flags) this.flagAt = now;
      if (next.flags !== undefined) this.flags = next.flags;
      if (next.live !== undefined) this.isLive = !!next.live;
      this.wake();
    }
    probe(kind) {
      if (this.closed) return;
      this.packets.push({ kind, born: performance.now(), seg: this.active });
      if (this.packets.length > 8) this.packets.shift();
      this.wake();
    }
    segments() {
      const gap = 10;
      const total = WEIGHTS.reduce((a, b) => a + b, 0);
      const usable = this.w - gap * (WEIGHTS.length - 1);
      let x = 0;
      return WEIGHTS.map(weight => {
        const x0 = x;
        const x1 = x + usable * weight / total;
        x = x1 + gap;
        return [x0, x1];
      });
    }
    draw(ctx, now) {
      const { w, h } = this;
      const p = this.palette;
      const sx = 4;
      const rows = 2;
      const sy = h / rows;
      const r0 = Math.min(sx, sy) * 0.24;
      const segs = this.segments();
      const closeT = this.closed ? this.age(now, this.closedAt) / 900 : -1;
      const waveX = -12 + (w + 24) * easeInOut(closeT);
      const cycle = ((now - this.t0) % 2600) / 2600;
      const carrierX = cycle < 0.72 ? 1 - Math.pow(1 - cycle / 0.72, 4) : 1;
      const carrierA = this.isLive && !this.closed ? (cycle < 0.72 ? 1 : 1 - smooth((cycle - 0.72) / 0.28)) : 0;
      const live = this.isLive && !this.closed;
      const activeColor = this.guarded ? p.warning : p.accent;

      // where each packet is right now
      const packets = [];
      this.packets = this.packets.filter(pk => {
        const [x0, x1] = segs[pk.seg] || segs[0];
        const t = (now - pk.born) / 1000;
        const span = x1 - x0;
        let px;
        let dir;
        let fade = 1;
        let color;
        if (pk.kind === "out") {
          px = x0 + t * 170;
          dir = 1;
          color = mix(p.accent, p.fg, 0.35);
          if (px > x1) fade = 1 - (px - x1) / 40;
          px = Math.min(px, x1);
        } else if (pk.kind === "blocked") {
          const wall = x0 + span * 0.72;
          px = Math.min(wall, x0 + t * 190);
          dir = 1;
          color = p.warning;
          const at = (wall - x0) / 190;
          if (t > at) fade = 1 - (t - at) / 0.45;
        } else {
          px = x1 - t * 210;
          dir = -1;
          color = pk.kind === "fail" ? p.danger : mix(p.accent, p.success, 0.45);
          if (px < x0) fade = 1 - (x0 - px) / 40;
          px = Math.max(px, x0);
        }
        if (fade <= 0) return false;
        packets.push({ px, dir, fade, color, kind: pk.kind, wall: x0 + span * 0.72, t });
        return true;
      });

      // flag marks: fixed dots in the exploit segment, one per confirmed piece
      const flagDots = new Set();
      if (this.flags) {
        const [x0, x1] = segs[2];
        const n = Math.max(1, Math.floor((x1 - x0) / sx));
        for (let f = 0; f < Math.min(4, this.flags); f += 1) {
          flagDots.add(Math.floor(hash(f, 5) * n) * rows + (f % rows));
        }
      }

      const blocked = packets.find(pk => pk.kind === "blocked" && pk.px >= pk.wall - 0.5);
      segs.forEach(([x0, x1], s) => {
        const visited = this.visited.has(s);
        const active = s === this.active && !this.closed;
        const n = Math.max(1, Math.floor((x1 - x0) / sx));
        const pad = (x1 - x0 - (n - 1) * sx) / 2;
        for (let c = 0; c < n; c += 1) {
          const x = x0 + pad + c * sx;
          for (let r = 0; r < rows; r += 1) {
            const y = sy * (r + 0.5);
            let color = p.faint;
            let alpha = 0.24;
            let rad = r0;
            if (visited && !active) { color = p.muted; alpha = 0.42; }
            if (active) {
              color = activeColor;
              alpha = 0.32;
              const band = Math.exp(-Math.pow((x - (x0 + (x1 - x0) * carrierX)) / 10, 2)) * carrierA;
              alpha += band * 0.5;
              rad *= 1 + band * 0.45;
            }
            for (const pk of packets) {
              const dd = (pk.px - x) * pk.dir;
              const g = dd >= 0 ? Math.exp(-dd / 13) : Math.exp(dd * 0.7);
              if (g < 0.02) continue;
              color = mix(color, pk.color, g * pk.fade);
              alpha = Math.min(1, alpha + g * pk.fade * 0.75);
              rad *= 1 + g * pk.fade * 0.6;
            }
            if (blocked && active) {
              const near = Math.exp(-Math.abs(x - blocked.wall) / 6) * blocked.fade;
              color = mix(color, p.warning, near);
              alpha = Math.min(1, alpha + near * 0.6);
            }
            const flag = s === 2 && flagDots.has(c * rows + r);
            if (flag) {
              color = p.danger;
              alpha = 1;
              rad = r0 * 1.5;
              const ring = this.age(now, this.flagAt) / 700;
              if (ring < 1) {
                ctx.strokeStyle = rgba(p.danger, 0.7 * (1 - ring));
                ctx.lineWidth = 1;
                ctx.beginPath();
                ctx.arc(x, y, 1.5 + ring * 8, 0, Math.PI * 2);
                ctx.stroke();
              }
            }
            if (this.closed && !flag) {
              // closing: a wave hands every visited segment over to the done colors
              const doneColor = visited || s === 3 ? mix(p.accent, p.success, 0.6) : p.faint;
              const doneAlpha = visited || s === 3 ? (s === 3 ? 0.72 : 0.5) : 0.24;
              if (x < waveX) { color = doneColor; alpha = doneAlpha; rad = r0; }
              const band = Math.exp(-Math.pow((x - waveX) / 12, 2)) * (closeT < 1.05 ? 1 : 0);
              color = mix(color, p.success, band);
              alpha = Math.min(1, alpha + band * 0.6);
              rad *= 1 + band * 0.5;
            }
            this.dot(x, y, rad, color, alpha);
          }
        }
        if (s < segs.length - 1) {
          // a small tick between segments, brighter after the active one
          const tx = x1 + 5;
          ctx.fillStyle = rgba(s === this.active && live ? activeColor : p.faint, s === this.active && live ? 0.7 : 0.45);
          ctx.fillRect(Math.round(tx) - 0.5, h * 0.2, 1, h * 0.6);
        }
      });

      if (live || packets.length) return true;
      if (this.closed && closeT < 1.1) return true;
      return this.age(now, this.flagAt) < 760;
    }
  }

  // ---- bucket: one passive source ----
  // A 7x3 matrix. Records fill it on a log scale, in a scattered order, each
  // dot landing with a small spring. While its source is queried a beam sweeps
  // out fast and back slow, and the empty dots twinkle.
  class Bucket extends Instrument {
    constructor(canvas, key) {
      super(canvas, () => ({ fg: "var(--fg)", faint: "var(--fg-faint)", accent: "var(--accent)" }));
      const seed = Array.from(String(key || "")).reduce((sum, ch) => sum + ch.charCodeAt(0), 0);
      this.order = Array.from({ length: 21 }, (_, i) => i).sort((a, b) => hash(a, seed) - hash(b, seed));
      this.rank = new Array(21);
      this.order.forEach((cell, rank) => { this.rank[cell] = rank; });
      this.filled = 0;
      this.born = [];
      this.active = false;
      this.activeSince = 0;
      this.checked = false;
      this.wake();
    }
    setActive(on) {
      if (on && !this.active) this.activeSince = performance.now();
      this.active = !!on;
      this.wake();
    }
    setCount(n) {
      const now = performance.now();
      const target = n > 0 ? Math.min(21, 3 + Math.round(Math.log2(n) * 3)) : 0;
      for (let rank = this.filled; rank < target; rank += 1) this.born[rank] = now + (rank - this.filled) * 45;
      this.filled = Math.max(this.filled, target);
      this.checked = true;
      this.wake();
    }
    draw(ctx, now) {
      const { w, h } = this;
      const p = this.palette;
      const cols = 7;
      const rows = 3;
      const sx = w / cols;
      const sy = h / rows;
      const r0 = Math.min(sx, sy) * 0.27;
      const cycle = ((now - this.activeSince) % 1800) / 1800;
      const pos = cycle < 0.6 ? easeOut(cycle / 0.6) : 1 - easeIn((cycle - 0.6) / 0.4);
      const beamX = -3 + (w + 6) * pos;
      let settling = false;
      for (let r = 0; r < rows; r += 1) {
        for (let c = 0; c < cols; c += 1) {
          const cell = r * cols + c;
          const rank = this.rank[cell];
          const x = sx * (c + 0.5);
          const y = sy * (r + 0.5);
          if (rank < this.filled) {
            const t = this.age(now, this.born[rank]) / 320;
            if (t < 1.4) settling = true;
            if (t <= 0) {
              this.dot(x, y, r0 * 0.6, p.faint, 0.2);
              continue;
            }
            const flash = Math.max(0, 1 - t * 1.6);
            this.dot(x, y, r0 * (spring(t) + flash * 0.25), mix(p.accent, p.fg, flash * 0.6), 0.9);
            continue;
          }
          let alpha = this.checked ? 0.26 : 0.16;
          let color = p.faint;
          let rad = r0 * 0.62;
          if (this.active) {
            const tw = 0.5 + 0.5 * Math.sin(now * 0.004 + hash(cell, 3) * 6.283);
            alpha += 0.28 * tw * tw * tw;
            const beam = Math.exp(-Math.pow((x - beamX) / 4, 2));
            color = mix(color, p.accent, beam);
            alpha = Math.min(1, alpha + beam * 0.6);
            rad *= 1 + beam * 0.5;
          }
          this.dot(x, y, rad, color, alpha);
        }
      }
      return this.active || settling;
    }
  }

  window.AccRtViz = {
    tape: canvas => canvas ? new Tape(canvas) : null,
    phases: canvas => canvas ? new Phases(canvas) : null,
    bucket: (canvas, key) => canvas ? new Bucket(canvas, key) : null,
  };
})();
