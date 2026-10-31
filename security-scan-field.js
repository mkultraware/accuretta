(function () {
  "use strict";

  // Security scan intro: a dot-matrix map of the machine. Each collector owns
  // a district of the lattice; a read head walks the active district and the
  // dots it passes settle into a texture shaped like that source's data (log
  // lines, traffic bars, a process table). A loupe rides the head, correlation
  // routes join notable cells across districts, and a wave from the
  // bottom-left corner closes the scan.

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const easeOutCubic = t => 1 - Math.pow(1 - t, 3);

  function hash(a, b, seed = 0) {
    const value = Math.sin(a * 127.1 + b * 311.7 + seed * 74.7) * 43758.5453;
    return value - Math.floor(value);
  }

  // Scan order matches SECURITY_INTRO_STAGES in app.js. rect is [x0, y0, x1, y1]
  // on a 24 x 16 unit grid, tiled unevenly on purpose; order is the head's
  // raster direction; kind is what the district looks like once read.
  const DISTRICTS = [
    { key: "network", label: "Network", rect: [0, 0, 10, 6], order: "cols", kind: "bars" },
    { key: "system", label: "System", rect: [10, 0, 15, 3], order: "rows", kind: "log" },
    { key: "application", label: "Application", rect: [15, 0, 24, 3], order: "rows", kind: "log" },
    { key: "security", label: "Security", rect: [10, 3, 24, 6], order: "rows", kind: "log" },
    { key: "persistence", label: "Persistence", rect: [0, 6, 6, 16], order: "cols", kind: "sparse" },
    { key: "processes", label: "Processes", rect: [6, 6, 17, 11], order: "rows", kind: "table" },
    { key: "commands", label: "Commands", rect: [17, 6, 24, 13], order: "rows", kind: "prompt" },
    { key: "actions", label: "Actions", rect: [6, 11, 12, 16], order: "cols", kind: "prompt" },
    { key: "correlate", label: "Correlate", rect: [12, 11, 17, 16], order: "rows", kind: "bits" },
    { key: "summary", label: "Summary", rect: [17, 13, 24, 16], order: "rows", kind: "dither" },
  ];
  // Correlation routes, as pairs of district indices (anchor to anchor).
  const LINKS = [[3, 4], [5, 0], [6, 5], [7, 3], [1, 2], [8, 9]];
  const CELL_FLOATS = 12;

  function parseColor(value, fallback) {
    const text = String(value || "").trim();
    let match = text.match(/^#([\da-f]{3}|[\da-f]{6})$/i);
    if (match) {
      let hex = match[1];
      if (hex.length === 3) hex = hex.split("").map(char => char + char).join("");
      return [
        parseInt(hex.slice(0, 2), 16) / 255,
        parseInt(hex.slice(2, 4), 16) / 255,
        parseInt(hex.slice(4, 6), 16) / 255,
      ];
    }
    match = text.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/i);
    if (match) return [Number(match[1]) / 255, Number(match[2]) / 255, Number(match[3]) / 255];
    match = text.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/i);
    if (match) return [Number(match[1]), Number(match[2]), Number(match[3])];
    return fallback.slice();
  }

  // Custom properties come back as written (often var() or color-mix()), so
  // resolve each through a probe element's computed color.
  function readPalette(canvas) {
    const host = canvas.closest(".security-scan-intro") || document.body;
    const probe = document.createElement("span");
    probe.style.cssText = "position:absolute;visibility:hidden;pointer-events:none";
    host.appendChild(probe);
    const resolve = (name, fallback) => {
      probe.style.color = `var(${name})`;
      return parseColor(getComputedStyle(probe).color, fallback);
    };
    const palette = {
      primary: resolve("--scan-cool", [0.42, 0.38, 1]),
      hot: resolve("--scan-hot", [0.94, 0.94, 0.97]),
      quiet: resolve("--fg-muted", [0.46, 0.46, 0.52]),
      success: resolve("--success", [0.39, 0.76, 0.59]),
      danger: resolve("--danger", [1, 0.34, 0.4]),
    };
    probe.remove();
    return palette;
  }

  // ---- district textures: what each source's data looks like once read ----
  function districtLevels(kind, w, h, seed) {
    const levels = new Float32Array(w * h);
    const set = (lc, lr, value) => { if (lc >= 0 && lc < w && lr >= 0 && lr < h) levels[lr * w + lc] = value; };
    const words = (lr, start, end, dim) => {
      let pos = start;
      while (pos < end) {
        const length = 2 + Math.floor(hash(pos, lr, seed + 7) * 5);
        const level = dim + hash(lr, pos, seed + 11) * (1 - dim);
        for (let i = 0; i < length && pos + i < end; i += 1) set(pos + i, lr, level);
        pos += length + 1;
      }
    };
    if (kind === "bars") {
      // traffic: a wandering histogram with the odd burst, brightest at each crest
      let load = 0.3 + hash(0, 1, seed) * 0.3;
      for (let lc = 0; lc < w; lc += 1) {
        load = clamp(load + (hash(lc, 3, seed) - 0.5) * 0.42 + (0.48 - load) * 0.22, 0.1, 0.85);
        const burst = hash(lc, 5, seed) > 0.88 ? 0.3 : 0;
        const height = Math.max(1, Math.round(h * clamp(load + burst, 0.08, 1)));
        for (let i = 0; i < height; i += 1) set(lc, h - 1 - i, i === height - 1 ? 1 : 0.42 + 0.3 * i / height);
      }
    } else if (kind === "log") {
      // event records: lines of words, with the odd blank line between runs
      for (let lr = 0; lr < h; lr += 1) {
        if (hash(lr, 9, seed) < 0.16) continue;
        words(lr, 0, Math.round(w * (0.38 + 0.62 * hash(lr, 5, seed))), 0.4);
      }
    } else if (kind === "prompt") {
      // shell history: a bright prompt mark, then the command, some indented
      for (let lr = 0; lr < h; lr += 1) {
        if (hash(lr, 13, seed) < 0.12) continue;
        const indent = hash(lr, 17, seed) > 0.68 ? 2 : 0;
        set(indent, lr, 1);
        words(lr, indent + 2, Math.round(w * (0.3 + 0.62 * hash(lr, 19, seed))), 0.35);
      }
    } else if (kind === "sparse") {
      // autoruns and scheduled tasks: few entries, each one worth a look
      for (let lr = 0; lr < h; lr += 1) {
        for (let lc = 0; lc < w; lc += 1) {
          const value = hash(lc, lr, seed + 40);
          if (value > 0.83) set(lc, lr, value > 0.95 ? 1 : 0.62);
        }
      }
    } else if (kind === "table") {
      // process list: fixed columns (pid, name, user, state) on every row
      const fields = [1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1, 0, 1, 0];
      for (let lr = 0; lr < h; lr += 1) {
        for (let lc = 0; lc < w; lc += 1) {
          if (!fields[lc % fields.length] || hash(lc, lr, seed + 23) < 0.14) continue;
          set(lc, lr, lc % fields.length < 3 ? 0.95 : 0.38 + 0.4 * hash(lc, lr, seed));
        }
      }
    } else if (kind === "dither") {
      // the summary: an ordered dither of a diagonal ramp
      const bayer = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
      for (let lr = 0; lr < h; lr += 1) {
        for (let lc = 0; lc < w; lc += 1) {
          const ramp = 1 - (lc / Math.max(1, w - 1)) * 0.75 - (lr / Math.max(1, h - 1)) * 0.25;
          if ((bayer[(lr % 4) * 4 + (lc % 4)] + 0.5) / 16 < ramp) set(lc, lr, 0.5 + ramp * 0.5);
        }
      }
    } else {
      for (let lr = 0; lr < h; lr += 1) {
        for (let lc = 0; lc < w; lc += 1) {
          const value = hash(lc, lr, seed + 31);
          if (value > 0.52) set(lc, lr, 0.35 + value * 0.65);
        }
      }
    }
    return levels;
  }

  function districtCell(district, k) {
    const { w, h } = district;
    let lc;
    let lr;
    if (district.order === "rows") {
      lr = Math.floor(k / w);
      const i = k % w;
      lc = lr % 2 ? w - 1 - i : i;
    } else {
      lc = Math.floor(k / h);
      const i = k % h;
      lr = lc % 2 ? h - 1 - i : i;
    }
    return [district.x0 + lc, district.y0 + lr];
  }

  function districtIndex(district, lc, lr) {
    const { w, h } = district;
    return district.order === "rows"
      ? lr * w + (lr % 2 ? w - 1 - lc : lc)
      : lc * h + (lc % 2 ? h - 1 - lr : lr);
  }

  // Lays the lattice into the box, cuts it into districts and bakes one
  // vertex per cell: position, district, read order, texture, anchors, routes.
  function buildMap(box) {
    const spacing = box.spacing;
    const cols = Math.max(18, Math.floor((box.right - box.left) / spacing));
    const rows = Math.max(12, Math.floor((box.bottom - box.top) / spacing));
    const originX = box.left + (box.right - box.left - cols * spacing) / 2 + spacing / 2;
    const originY = box.top + (box.bottom - box.top - rows * spacing) / 2 + spacing / 2;
    const snapX = unit => Math.round(unit / 24 * cols);
    const snapY = unit => Math.round(unit / 16 * rows);
    const districts = DISTRICTS.map((def, index) => {
      const x0 = snapX(def.rect[0]);
      const y0 = snapY(def.rect[1]);
      const x1 = snapX(def.rect[2]);
      const y1 = snapY(def.rect[3]);
      const w = Math.max(1, x1 - x0 - (x1 < cols ? 1 : 0));
      const h = Math.max(1, y1 - y0 - (y1 < rows ? 1 : 0));
      const district = { ...def, index, x0, y0, x1, y1, w, h, count: w * h, lineLength: def.order === "rows" ? w : h };
      district.levels = districtLevels(def.kind, w, h, index * 13 + 5);
      // the anchor: the brightest-hashing lit cell away from the edges
      let best = -1;
      for (let lr = 0; lr < h; lr += 1) {
        for (let lc = 0; lc < w; lc += 1) {
          if (!district.levels[lr * w + lc]) continue;
          const edge = (lc === 0 || lr === 0 || lc === w - 1 || lr === h - 1) && w > 2 && h > 2;
          const score = hash(lc, lr, index + 99) - (edge ? 1 : 0);
          if (score > best) {
            best = score;
            district.anchor = { lc, lr, c: x0 + lc, r: y0 + lr };
          }
        }
      }
      if (!district.anchor) district.anchor = { lc: 0, lr: 0, c: x0, r: y0 };
      return district;
    });

    const count = cols * rows;
    const data = new Float32Array(count * CELL_FLOATS);
    const at = (c, r) => (r * cols + c) * CELL_FLOATS;
    for (const district of districts) {
      for (let r = district.y0; r < district.y1; r += 1) {
        for (let c = district.x0; c < district.x1; c += 1) {
          const offset = at(c, r);
          const lc = c - district.x0;
          const lr = r - district.y0;
          const gutter = lc >= district.w || lr >= district.h;
          const index = gutter ? 0 : districtIndex(district, lc, lr);
          // the lattice springs in from the top-right corner, unevenly
          const dx = (cols - 1 - c) / cols;
          const dy = r / rows;
          data[offset] = originX + c * spacing;
          data[offset + 1] = originY + r * spacing;
          data[offset + 2] = district.index;
          data[offset + 3] = gutter ? -1 : (index + 0.5) / district.count;
          data[offset + 4] = gutter ? -1 : district.order === "rows" ? lr : lc;
          data[offset + 5] = hash(c, r, 3);
          data[offset + 6] = gutter ? 1 : 0;
          data[offset + 7] = gutter ? 0 : district.levels[lr * district.w + lc];
          data[offset + 8] = !gutter && district.anchor.lc === lc && district.anchor.lr === lr ? 1 : 0;
          data[offset + 9] = -1;
          data[offset + 10] = 0;
          data[offset + 11] = 0.08 + Math.sqrt(dx * dx * 0.8 + dy * dy) * 0.82 + hash(r, c, 8) * 0.26;
        }
      }
    }

    // routes: across from the first anchor, then up or down to the second
    LINKS.forEach(([from, to], linkId) => {
      const a = districts[from].anchor;
      const b = districts[to].anchor;
      const path = [];
      const stepX = Math.sign(b.c - a.c) || 1;
      for (let c = a.c; c !== b.c + stepX; c += stepX) path.push([c, a.r]);
      const stepY = Math.sign(b.r - a.r) || 1;
      for (let r = a.r + stepY; b.r !== a.r && r !== b.r + stepY; r += stepY) path.push([b.c, r]);
      path.forEach(([c, r], i) => {
        if (c < 0 || r < 0 || c >= cols || r >= rows) return;
        const offset = at(c, r);
        if (data[offset + 9] >= 0) return;
        data[offset + 9] = linkId;
        data[offset + 10] = path.length > 1 ? i / (path.length - 1) : 1;
      });
    });

    const cellCenter = (c, r) => [originX + c * spacing, originY + r * spacing];
    return {
      spacing, cols, rows, originX, originY, districts, data, count, cellCenter,
      width: cols * spacing,
      height: rows * spacing,
      // where the head of a district is at progress p, as a smooth point
      head(index, progress) {
        const district = districts[index];
        const f = clamp(progress, 0, 1) * district.count - 0.5;
        const k0 = clamp(Math.floor(f), 0, district.count - 1);
        const k1 = Math.min(k0 + 1, district.count - 1);
        const t = clamp(f - k0, 0, 1);
        const [c0, r0] = districtCell(district, k0);
        const [c1, r1] = districtCell(district, k1);
        const [x0, y0] = cellCenter(c0, r0);
        const [x1, y1] = cellCenter(c1, r1);
        const x = x0 + (x1 - x0) * t;
        const y = y0 + (y1 - y0) * t;
        // the middle of the line being read, for things that should glide
        // along the scan rather than zigzag with the head
        const mid = cellCenter(district.x0 + (district.w - 1) / 2, district.y0 + (district.h - 1) / 2);
        const lineX = district.order === "rows" ? mid[0] : x;
        const lineY = district.order === "rows" ? y : mid[1];
        return { x, y, lineX, lineY, c: c0, r: r0 };
      },
    };
  }

  // ---- WebGL: one point per cell; the vertex shader does all the shading ----
  const VERTEX_SHADER = `
    precision highp float;
    attribute vec2 aPos;
    attribute vec4 aA;   // district, read order, line, hash
    attribute vec4 aB;   // gutter, level, anchor, link id
    attribute vec2 aC;   // position along the link, intro delay
    uniform vec2 uResolution;
    uniform float uDpr;
    uniform float uTime;
    uniform float uP[10];
    uniform float uHeadLine[10];
    uniform float uCount[10];
    uniform float uActive;
    uniform float uLinkP[6];
    uniform float uFinish;
    uniform float uState;
    uniform vec3 uLens;
    uniform float uLensR;
    uniform float uSpacing;
    uniform vec2 uWaveOrigin;
    uniform float uWaveSpan;
    uniform mediump float uGlowPass;
    uniform vec3 uPrimary;
    uniform vec3 uHot;
    uniform vec3 uQuiet;
    uniform vec3 uSuccess;
    uniform vec3 uDanger;
    varying vec3 vColor;
    varying float vAlpha;
    varying float vRing;
    varying float vSize;

    float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

    void main() {
      int district = int(aA.x + 0.5);
      float gutter = aB.x;
      float level = aB.y;
      float lit = step(0.01, level);
      float t = uTime;

      // intro: each dot springs in (overshoot, then settle); a beat later the
      // gutters drop out and the lattice reads as districts
      float age = t - aC.y;
      float appear = age <= 0.0 ? 0.0 : 1.0 - exp(-6.5 * age) * cos(11.5 * age);
      float split = smoothstep(1.25, 1.85, t);

      float progress = uP[district];
      float count = max(1.0, uCount[district]);
      float active = 1.0 - step(0.5, abs(float(district) - uActive));
      float headVis = step(0.0005, progress) * (1.0 - smoothstep(0.985, 1.0, progress));
      float behind = (progress - aA.y) * count;
      float read = step(0.0, behind) * (1.0 - gutter);
      float trail = read * exp(-behind / 4.5) * headVis;
      float lead = (1.0 - read) * (1.0 - gutter) * exp(behind * 1.4) * headVis;
      float line = active * headVis * (1.0 - gutter) * (1.0 - step(0.5, abs(aA.z - uHeadLine[district])));

      float link = 0.0;
      float pulse = 0.0;
      if (aB.w > -0.5) {
        float reach = uLinkP[int(aB.w + 0.5)];
        float drawn = step(0.001, reach);
        link = step(aC.x, reach) * drawn;
        float tip = exp(-pow((aC.x - reach) * 9.0, 2.0)) * drawn * (1.0 - step(0.999, reach));
        float travel = exp(-pow((aC.x - fract(t * 0.42 + aB.w * 0.377)) * 11.0, 2.0)) * link;
        pulse = max(tip, travel);
      }

      // two slow waves at odd angles and rates, so the idle field never repeats
      float idle = 0.5 + 0.5 * sin(aPos.x * 0.011 + t * 0.63 + sin(aPos.y * 0.017 - t * 0.41) * 1.8);
      float glint = step(0.9968, hash(aPos + floor(t * 2.3)));

      float alpha = (0.22 + idle * 0.1 + active * 0.08) * (1.0 - gutter * split);
      float size = uSpacing * (0.21 + idle * 0.04);
      vec3 color = uQuiet;
      float energy = 0.0;
      if (read > 0.5) {
        if (lit > 0.5) {
          alpha = mix(0.42 + level * 0.45, 0.55 + level * 0.45, active);
          color = mix(uQuiet, uHot, 0.3 + level * 0.5);
          color = mix(color, uPrimary, active * 0.5);
          size = uSpacing * (0.3 + level * 0.15);
        } else {
          alpha = 0.08;
          size = uSpacing * 0.13;
        }
      }
      // the head: a bright tip and a short comet, lit cells or not
      energy = max(energy, trail * (0.7 + lit * 0.3));
      energy = max(energy, lead * 0.5);
      size = max(size, uSpacing * (0.26 + 0.34 * trail + 0.2 * lead));
      float tip = read * (1.0 - step(1.0, behind)) * headVis;
      size = max(size, uSpacing * 0.66 * tip);
      // the line being read glows faintly ahead of the head
      alpha += line * (1.0 - read) * 0.2;
      size *= 1.0 + line * (1.0 - read) * 0.18;

      float ring = 0.0;
      if (aB.z > 0.5 && read > 0.5) {
        float flash = exp(-behind / 10.0);
        ring = 1.0;
        size = uSpacing * (0.66 + flash * 0.55);
        alpha = 0.92;
        color = mix(uHot, uPrimary, 0.3);
        energy = max(energy, 0.28 + flash * 0.6);
      }
      if (link > 0.0 || pulse > 0.0) {
        alpha = max(alpha, link * 0.62);
        size = max(size, uSpacing * 0.25);
        color = mix(color, mix(uPrimary, uHot, 0.35), link);
        energy = max(energy, pulse);
      }
      // each dot flashes as it lands, so the entrance sweeps as a bright front
      float pop = age > 0.0 ? exp(-age * 4.5) : 0.0;
      energy = max(energy, pop * 0.6);
      energy = clamp(max(energy, glint * 0.55 * (1.0 - read) * (1.0 - gutter)), 0.0, 1.0);
      color = mix(color, mix(uPrimary, uHot, 0.6), energy);
      alpha = mix(alpha, 1.0, energy);
      size *= 1.0 + energy * 0.75;

      vec2 pos = aPos;
      if (uState > 0.5) {
        // complete: a wave from the bottom-left corner turns the map green
        float dist = length(aPos - uWaveOrigin) / uWaveSpan;
        float front = uFinish * 1.3;
        float past = smoothstep(dist - 0.04, dist + 0.04, front);
        float band = exp(-pow((dist - front) * 7.5, 2.0));
        color = mix(color, uSuccess, past * 0.72 + band * 0.28);
        color = mix(color, uHot, band * 0.45);
        alpha = min(1.0, alpha + band * 0.55 * (1.0 - gutter * 0.5));
        size *= 1.0 + band * 0.95;
        energy = max(energy, band);
      } else if (uState < -0.5) {
        // stopped: the unread lattice tears sideways for a moment, then sits red
        float row = floor(aPos.y / uSpacing);
        float frame = floor(t * 22.0);
        float tear = exp(-uFinish * 3.5) * step(0.7, hash(vec2(row, frame)));
        pos.x += (hash(vec2(row, frame + 3.0)) - 0.5) * uSpacing * 3.0 * tear;
        color = mix(color, uDanger, 0.3 + (1.0 - read) * 0.5);
        alpha *= 0.85;
      }

      // loupe: magnify the middle, squeeze the rim
      float lensScale = 1.0;
      vec2 offset = pos - uLens.xy;
      float s = length(offset) / uLensR;
      if (s < 1.0 && uLens.z > 0.001) {
        float safe = max(s, 0.0001);
        float mapped = s * (1.85 - 0.85 * s);
        pos = uLens.xy + offset * mix(1.0, mapped / safe, uLens.z);
        lensScale = mix(1.0, sqrt(max(0.06, (1.85 - 1.7 * s) * mapped / safe)), uLens.z);
      }

      float shown = clamp(appear, 0.0, 1.0);
      size *= max(0.0, appear) * lensScale;
      alpha *= shown;
      if (uGlowPass > 0.5) {
        size *= 4.0;
        alpha = energy * 0.3 * shown * step(0.12, energy);
      }

      vec2 clip = pos * uDpr / uResolution * 2.0 - 1.0;
      gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
      vSize = min(96.0, size * uDpr);
      gl_PointSize = vSize;
      vColor = color;
      vAlpha = clamp(alpha, 0.0, 1.0);
      vRing = ring;
    }
  `;

  const FRAGMENT_SHADER = `
    precision mediump float;
    uniform mediump float uGlowPass;
    varying vec3 vColor;
    varying float vAlpha;
    varying float vRing;
    varying float vSize;

    void main() {
      float r = length(gl_PointCoord * 2.0 - 1.0);
      float aa = 2.2 / max(vSize, 1.0);
      float shape;
      if (uGlowPass > 0.5) {
        shape = pow(max(0.0, 1.0 - r), 2.4);
      } else if (vRing > 0.5) {
        float band = smoothstep(0.6 - aa, 0.6 + aa, r) * (1.0 - smoothstep(0.84 - aa, 0.84 + aa, r));
        float core = 1.0 - smoothstep(0.24 - aa, 0.24 + aa, r);
        shape = max(band, core);
      } else {
        shape = 1.0 - smoothstep(1.0 - aa * 1.6, 1.0, r);
      }
      float alpha = shape * vAlpha;
      if (alpha < 0.004) discard;
      gl_FragColor = vec4(vColor * alpha, alpha);
    }
  `;

  function compileShader(gl, type, source) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      const message = gl.getShaderInfoLog(shader) || "Shader compilation failed";
      gl.deleteShader(shader);
      throw new Error(message);
    }
    return shader;
  }

  function createProgram(gl, vertexSource, fragmentSource) {
    const program = gl.createProgram();
    const vertex = compileShader(gl, gl.VERTEX_SHADER, vertexSource);
    const fragment = compileShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
    gl.attachShader(program, vertex);
    gl.attachShader(program, fragment);
    gl.linkProgram(program);
    gl.deleteShader(vertex);
    gl.deleteShader(fragment);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      const message = gl.getProgramInfoLog(program) || "Shader link failed";
      gl.deleteProgram(program);
      throw new Error(message);
    }
    return program;
  }

  class WebGLField {
    constructor(canvas) {
      this.canvas = canvas;
      this.gl = canvas.getContext("webgl", {
        alpha: true,
        antialias: false,
        depth: false,
        powerPreference: "low-power",
        premultipliedAlpha: true,
      }) || canvas.getContext("experimental-webgl");
      if (!this.gl) throw new Error("WebGL is unavailable");
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.palette = null;
      this.map = null;
      const gl = this.gl;
      this.program = createProgram(gl, VERTEX_SHADER, FRAGMENT_SHADER);
      this.buffer = gl.createBuffer();
      this.attributes = ["aPos", "aA", "aB", "aC"].map(name => gl.getAttribLocation(this.program, name));
      this.uniforms = {};
      for (const name of [
        "uResolution", "uDpr", "uTime", "uP", "uHeadLine", "uCount", "uActive", "uLinkP", "uFinish",
        "uState", "uLens", "uLensR", "uSpacing", "uWaveOrigin", "uWaveSpan", "uGlowPass",
        "uPrimary", "uHot", "uQuiet", "uSuccess", "uDanger",
      ]) this.uniforms[name] = gl.getUniformLocation(this.program, name);
    }

    setPalette(palette) {
      this.palette = palette;
    }

    setMap(map) {
      this.map = map;
      const gl = this.gl;
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      gl.bufferData(gl.ARRAY_BUFFER, map.data, gl.STATIC_DRAW);
    }

    resize(maxPixels) {
      const rect = this.canvas.getBoundingClientRect();
      const cssWidth = Math.max(1, Math.round(rect.width));
      const cssHeight = Math.max(1, Math.round(rect.height));
      const pixelLimit = Math.sqrt(maxPixels / Math.max(1, cssWidth * cssHeight));
      const dpr = clamp(Math.min(window.devicePixelRatio || 1, pixelLimit), 0.75, 2);
      const width = Math.max(1, Math.round(cssWidth * dpr));
      const height = Math.max(1, Math.round(cssHeight * dpr));
      if (width === this.width && height === this.height) return;
      this.width = width;
      this.height = height;
      this.dpr = dpr;
      this.canvas.width = width;
      this.canvas.height = height;
      this.gl.viewport(0, 0, width, height);
    }

    render(frame) {
      const gl = this.gl;
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (!this.palette || !this.map) return;
      const map = this.map;
      const u = this.uniforms;
      gl.useProgram(this.program);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buffer);
      const stride = CELL_FLOATS * 4;
      const [aPos, aA, aB, aC] = this.attributes;
      gl.enableVertexAttribArray(aPos);
      gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, stride, 0);
      gl.enableVertexAttribArray(aA);
      gl.vertexAttribPointer(aA, 4, gl.FLOAT, false, stride, 8);
      gl.enableVertexAttribArray(aB);
      gl.vertexAttribPointer(aB, 4, gl.FLOAT, false, stride, 24);
      gl.enableVertexAttribArray(aC);
      gl.vertexAttribPointer(aC, 2, gl.FLOAT, false, stride, 40);
      gl.uniform2f(u.uResolution, this.width, this.height);
      gl.uniform1f(u.uDpr, this.dpr);
      gl.uniform1f(u.uTime, frame.time);
      gl.uniform1fv(u.uP, frame.progress);
      gl.uniform1fv(u.uHeadLine, frame.headLine);
      gl.uniform1fv(u.uCount, frame.count);
      gl.uniform1f(u.uActive, frame.active);
      gl.uniform1fv(u.uLinkP, frame.links);
      gl.uniform1f(u.uFinish, frame.finish);
      gl.uniform1f(u.uState, frame.state);
      gl.uniform3f(u.uLens, frame.lens.x, frame.lens.y, frame.lens.strength);
      gl.uniform1f(u.uLensR, frame.lens.radius);
      gl.uniform1f(u.uSpacing, map.spacing);
      gl.uniform2f(u.uWaveOrigin, map.originX - map.spacing, map.originY + map.height);
      gl.uniform1f(u.uWaveSpan, Math.hypot(map.width, map.height));
      gl.uniform3fv(u.uPrimary, this.palette.primary);
      gl.uniform3fv(u.uHot, this.palette.hot);
      gl.uniform3fv(u.uQuiet, this.palette.quiet);
      gl.uniform3fv(u.uSuccess, this.palette.success);
      gl.uniform3fv(u.uDanger, this.palette.danger);
      gl.enable(gl.BLEND);
      // glow underneath (additive), crisp dots on top (premultiplied over)
      gl.blendFunc(gl.ONE, gl.ONE);
      gl.uniform1f(u.uGlowPass, 1);
      gl.drawArrays(gl.POINTS, 0, map.count);
      gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.uniform1f(u.uGlowPass, 0);
      gl.drawArrays(gl.POINTS, 0, map.count);
    }
  }

  // Fallback when WebGL is missing: the same map, read state and finish, as
  // flat dots without the loupe, glow or routes.
  class CanvasField {
    constructor(canvas) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d");
      if (!this.context) throw new Error("Canvas is unavailable");
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.palette = null;
      this.map = null;
    }

    setPalette(palette) {
      this.palette = palette;
    }

    setMap(map) {
      this.map = map;
    }

    resize(maxPixels) {
      const rect = this.canvas.getBoundingClientRect();
      const cssWidth = Math.max(1, Math.round(rect.width));
      const cssHeight = Math.max(1, Math.round(rect.height));
      const limit = Math.sqrt(maxPixels / Math.max(1, cssWidth * cssHeight));
      this.dpr = clamp(Math.min(window.devicePixelRatio || 1, limit), 0.75, 1.5);
      const width = Math.round(cssWidth * this.dpr);
      const height = Math.round(cssHeight * this.dpr);
      if (width === this.width && height === this.height) return;
      this.width = width;
      this.height = height;
      this.canvas.width = width;
      this.canvas.height = height;
    }

    render(frame) {
      const ctx = this.context;
      ctx.clearRect(0, 0, this.width, this.height);
      if (!this.palette || !this.map) return;
      const { data, count, spacing } = this.map;
      const mix = (a, b, t) => a.map((value, i) => value + (b[i] - value) * t);
      const rgb = color => `${Math.round(color[0] * 255)},${Math.round(color[1] * 255)},${Math.round(color[2] * 255)}`;
      const shown = clamp(frame.time / 1.2, 0, 1);
      for (let i = 0; i < count; i += 1) {
        const o = i * CELL_FLOATS;
        if (data[o + 6]) continue;
        const district = data[o + 2];
        const progress = frame.progress[district];
        const behind = (progress - data[o + 3]) * frame.count[district];
        const read = behind >= 0;
        const level = data[o + 7];
        const active = district === frame.active;
        let color = this.palette.quiet;
        let alpha = 0.1;
        let size = spacing * 0.16;
        if (read && level) {
          color = mix(mix(this.palette.quiet, this.palette.hot, 0.3 + level * 0.5), this.palette.primary, active ? 0.5 : 0);
          alpha = 0.3 + level * 0.5;
          size = spacing * (0.22 + level * 0.13);
        }
        if (read && progress < 0.985) {
          const trail = Math.exp(-behind / 4.5);
          color = mix(color, this.palette.hot, trail);
          alpha += (1 - alpha) * trail;
        }
        if (frame.state > 0) color = mix(color, this.palette.success, clamp(frame.finish * 1.4, 0, 0.72));
        if (frame.state < 0) color = mix(color, this.palette.danger, read ? 0.3 : 0.8);
        ctx.fillStyle = `rgba(${rgb(color)},${alpha * shown})`;
        ctx.beginPath();
        ctx.arc(data[o] * this.dpr, data[o + 1] * this.dpr, size * 0.5 * this.dpr, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }

  class SecurityScanField {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.options = options;
      this.overlay = canvas.closest(".security-scan-intro");
      this.backend = null;
      this.map = null;
      this.mapKey = "";
      this.layoutAt = 0;
      this.active = false;
      this.raf = 0;
      this.startedAt = 0;
      this.lastFrameAt = 0;
      this.lastClockAt = 0;
      this.stage = -1;
      this.state = 0;
      this.finishStartedAt = 0;
      this.linkStartedAt = 0;
      this.progress = new Float32Array(DISTRICTS.length);
      this.headLine = new Float32Array(DISTRICTS.length);
      this.count = new Float32Array(DISTRICTS.length);
      this.links = new Float32Array(LINKS.length);
      this.lens = { x: 0, y: 0, vx: 0, vy: 0, strength: 0, radius: 56, placed: false };
      this.readout = "";
      this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.remote = document.body.classList.contains("remote-client");
      this.mobile = matchMedia("(max-width: 700px)").matches;
      this.boundLoop = now => this._loop(now);
      this.boundVisibility = () => this._handleVisibility();
      this.boundContextLost = event => this._handleContextLost(event);
      this.boundContextRestored = () => this._handleContextRestored();
      document.addEventListener("visibilitychange", this.boundVisibility);
      this._attachCanvasEvents(canvas);
      this._buildChrome();
      this._createBackend();
    }

    // Labels and the loupe are DOM so the type stays crisp and themeable.
    _buildChrome() {
      const parent = this.canvas.parentElement;
      this.chrome = parent.querySelector(".security-scan-map") || document.createElement("div");
      this.chrome.className = "security-scan-map";
      this.chrome.setAttribute("aria-hidden", "true");
      this.chrome.innerHTML = DISTRICTS.map((district, index) =>
        `<span class="security-map-label" data-state="pending"><b>${String(index + 1).padStart(2, "0")}</b><em>${district.label}</em></span>`).join("")
        + `<div class="security-map-loupe"><i></i><span class="security-map-readout"></span></div>`;
      this.canvas.after(this.chrome);
      this.labels = [...this.chrome.querySelectorAll(".security-map-label")];
      this.loupe = this.chrome.querySelector(".security-map-loupe");
      this.readoutNode = this.chrome.querySelector(".security-map-readout");
      this.overlay?.classList.add("has-map");
    }

    _attachCanvasEvents(canvas) {
      canvas.addEventListener("webglcontextlost", this.boundContextLost);
      canvas.addEventListener("webglcontextrestored", this.boundContextRestored);
    }

    _replaceCanvas() {
      const previous = this.canvas;
      const replacement = previous.cloneNode(false);
      previous.removeEventListener("webglcontextlost", this.boundContextLost);
      previous.removeEventListener("webglcontextrestored", this.boundContextRestored);
      previous.replaceWith(replacement);
      this.canvas = replacement;
      this._attachCanvasEvents(replacement);
      return replacement;
    }

    _createBackend() {
      try {
        this.backend = new WebGLField(this.canvas);
        this.canvas.dataset.renderer = "webgl";
      } catch (error) {
        console.warn("Security scan WebGL renderer unavailable:", error);
        try {
          this.backend = new CanvasField(this.canvas);
          this.canvas.dataset.renderer = "canvas";
        } catch (fallbackError) {
          try {
            this.backend = new CanvasField(this._replaceCanvas());
            this.canvas.dataset.renderer = "canvas";
          } catch (replacementError) {
            console.warn("Security scan canvas renderer unavailable:", fallbackError, replacementError);
            this.backend = null;
            this.canvas.dataset.renderer = "css";
          }
        }
      }
      this.backend?.setPalette(readPalette(this.canvas));
      if (this.map) this.backend?.setMap(this.map);
    }

    _handleContextLost(event) {
      event.preventDefault();
      this.backend = null;
      this.canvas.dataset.renderer = "css";
    }

    _handleContextRestored() {
      this._createBackend();
      this.renderOnce();
    }

    _handleVisibility() {
      if (document.hidden) {
        cancelAnimationFrame(this.raf);
        this.raf = 0;
      } else if (this.active && !this.reduced && !this.raf) {
        this.lastFrameAt = 0;
        this.raf = requestAnimationFrame(this.boundLoop);
      }
    }

    // The map fills the space the overlay's text leaves free: right of the
    // copy on wide screens, between the copy and the source list on phones.
    _layout() {
      const rect = this.canvas.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 40) return;
      const local = node => {
        const box = node?.getBoundingClientRect();
        if (!box || (!box.width && !box.height)) return null;
        return { left: box.left - rect.left, right: box.right - rect.left, top: box.top - rect.top, bottom: box.bottom - rect.top };
      };
      const header = local(this.overlay?.querySelector(".security-scan-intro-chrome > header"));
      const footer = local(this.overlay?.querySelector(".security-scan-intro-chrome > footer"));
      const copy = local(this.overlay?.querySelector(".security-intro-copy"));
      const sources = local(this.overlay?.querySelector(".security-intro-sources"));
      const wide = rect.width > 600;
      const box = wide ? {
        left: Math.max((copy?.right || 0) + 64, rect.width * 0.36),
        right: rect.width - 44,
        top: (header?.bottom || 60) + 52,
        bottom: Math.min(sources?.top ?? Infinity, footer?.top ?? rect.height - 60) - 44,
      } : {
        left: 18,
        right: rect.width - 18,
        top: (copy?.bottom || rect.height * 0.35) + 34,
        bottom: Math.min(sources?.top ?? Infinity, footer?.top ?? rect.height - 60) - 26,
      };
      if (box.right - box.left < 160 || box.bottom - box.top < 120) return;
      box.spacing = wide ? clamp((box.right - box.left) / 58, 11, 17) : 11;
      const key = [box.left, box.right, box.top, box.bottom, box.spacing].map(Math.round).join(":");
      if (key === this.mapKey) return;
      this.mapKey = key;
      this.map = buildMap(box);
      this.map.districts.forEach((district, i) => { this.count[i] = district.count; });
      this.backend?.setMap(this.map);
      this.lens.radius = clamp(this.map.spacing * 4.2, 44, 70);
      this.lens.placed = false;
      this.chrome.style.setProperty("--lens", `${this.lens.radius * 2}px`);
      this.map.districts.forEach((district, i) => {
        const label = this.labels[i];
        const x = this.map.originX + (district.x0 - 0.5) * this.map.spacing;
        const y = this.map.originY + (district.y0 - 1) * this.map.spacing;
        label.style.transform = `translate(${Math.round(x)}px, ${Math.round(y)}px) translateY(-50%)`;
        label.classList.toggle("compact", (district.x1 - district.x0) * this.map.spacing < 96);
      });
    }

    _updateProgress(delta, now) {
      const seconds = delta / 1000;
      const complete = this.state === 1;
      for (let i = 0; i < DISTRICTS.length; i += 1) {
        let p = this.progress[i];
        if (complete || i < this.stage) {
          // done: the head races to the end of the district
          p += (1 - p) * (1 - Math.exp(-seconds / 0.2));
          if (p > 0.9995) p = 1;
        } else if (i === this.stage && this.state === 0) {
          // reading: eases toward the end, slowing while the collector runs
          p += (0.965 - p) * (1 - Math.exp(-seconds * 0.42));
        }
        this.progress[i] = p;
        const district = this.map?.districts[i];
        if (district) this.headLine[i] = Math.max(0, Math.floor((p * district.count - 0.5) / district.lineLength));
      }
      if (!this.linkStartedAt && (this.stage >= 8 || complete)) this.linkStartedAt = now;
      for (let i = 0; i < LINKS.length; i += 1) {
        this.links[i] = this.linkStartedAt
          ? easeOutCubic(clamp((now - this.linkStartedAt - i * 170) / 820, 0, 1))
          : 0;
      }
    }

    _updateLens(delta, time) {
      const lens = this.lens;
      const map = this.map;
      if (!map) return;
      const reading = this.state === 0 && this.stage >= 0 && this.stage < DISTRICTS.length;
      let target;
      if (reading) {
        // ride the scan line, leaning toward the head: the head rasters fast
        // and a loupe glued to it would only shake
        const head = map.head(this.stage, this.progress[this.stage]);
        target = { ...head, x: head.lineX + (head.x - head.lineX) * 0.35, y: head.lineY + (head.y - head.lineY) * 0.35 };
      } else {
        // between collectors the loupe idles on a lazy, lopsided orbit
        const cx = map.originX + map.width * 0.48;
        const cy = map.originY + map.height * 0.5;
        target = {
          x: cx + Math.sin(time * 0.37) * map.width * 0.2 + Math.sin(time * 0.91) * map.width * 0.05,
          y: cy + Math.sin(time * 0.53 + 1.3) * map.height * 0.17,
          c: -1,
          r: -1,
        };
      }
      if (!lens.placed) {
        lens.x = target.x;
        lens.y = target.y;
        lens.vx = 0;
        lens.vy = 0;
        lens.placed = true;
      }
      // a slightly loose spring: it overshoots a hair when the head jumps
      const step = Math.min(0.05, delta / 1000);
      const stiffness = 70;
      const damping = 12;
      lens.vx += ((target.x - lens.x) * stiffness - lens.vx * damping) * step;
      lens.vy += ((target.y - lens.y) * stiffness - lens.vy * damping) * step;
      lens.x += lens.vx * step;
      lens.y += lens.vy * step;
      const want = this.reduced || this.state !== 0 ? 0 : easeOutCubic(clamp((time - 1.55) / 0.7, 0, 1));
      lens.strength += (want - lens.strength) * (1 - Math.exp(-delta / 160));
      if (this.loupe) {
        this.loupe.style.transform = `translate(${lens.x - lens.radius}px, ${lens.y - lens.radius}px) scale(${0.6 + lens.strength * 0.4})`;
        this.loupe.style.opacity = lens.strength.toFixed(3);
        // the readout hangs off the right; near the edge it swings to the left
        const flip = lens.x + lens.radius + 250 > this.chrome.clientWidth;
        if (flip !== this.loupe.classList.contains("flip")) this.loupe.classList.toggle("flip", flip);
      }
      let text = this.state === 1 ? "SNAPSHOT COMPLETE" : this.state < 0 ? "SCAN STOPPED" : "IDLE · AWAITING COLLECTOR";
      if (reading && target.c >= 0) {
        const address = (target.r * map.cols + target.c).toString(16).toUpperCase().padStart(4, "0");
        text = `${DISTRICTS[this.stage].label.toUpperCase()} · R${String(target.r).padStart(2, "0")} C${String(target.c).padStart(2, "0")} · 0x${address}`;
      }
      if (text !== this.readout && this.readoutNode) {
        this.readout = text;
        this.readoutNode.textContent = text;
      }
    }

    _updateLabels(time) {
      this.labels.forEach((label, i) => {
        const state = this.state === 1 || this.progress[i] >= 1 || i < this.stage ? "complete"
          : i === this.stage && this.state === 0 ? "active" : "pending";
        if (label.dataset.state !== state) label.dataset.state = state;
        // labels type in after the lattice splits, top-right first
        const shown = this.reduced || time > 1.45 + (DISTRICTS.length - i) * 0.035;
        label.classList.toggle("is-shown", shown);
      });
    }

    _frame(now) {
      const finish = this.finishStartedAt ? clamp((now - this.finishStartedAt) / 1100, 0, 1.4) : 0;
      return {
        time: this.reduced ? 4 : Math.max(0, now - this.startedAt) / 1000,
        progress: this.progress,
        headLine: this.headLine,
        count: this.count,
        active: this.state === 0 && this.stage >= 0 ? this.stage : -1,
        links: this.links,
        finish,
        state: this.state,
        lens: this.lens,
      };
    }

    _tick(now, delta) {
      // re-measure the overlay's text a couple of times a second, not every frame
      if (!this.map || !(now - this.layoutAt < 500)) {
        this.layoutAt = now;
        this._layout();
      }
      const frame = this._frame(now);
      this._updateProgress(delta, now);
      this._updateLens(delta, frame.time);
      this._updateLabels(frame.time);
      const maxPixels = this.remote || this.mobile ? 1100000 : 2300000;
      this.backend?.resize(maxPixels);
      this.backend?.render(frame);
    }

    _loop(now) {
      this.raf = 0;
      if (!this.active || document.hidden) return;
      const frameInterval = this.remote || this.mobile ? 1000 / 30 : 1000 / 60;
      if (!this.lastFrameAt || now - this.lastFrameAt >= frameInterval - 1) {
        const delta = this.lastFrameAt ? Math.min(64, now - this.lastFrameAt) : frameInterval;
        this.lastFrameAt = now;
        this._tick(now, delta);
        if (this.options.onTick && (!this.lastClockAt || now - this.lastClockAt > 90)) {
          this.lastClockAt = now;
          this.options.onTick(Math.max(0, now - this.startedAt));
        }
      }
      this.raf = requestAnimationFrame(this.boundLoop);
    }

    start() {
      if (this.active) return;
      this.active = true;
      this.startedAt = performance.now();
      this.lastFrameAt = 0;
      this.lastClockAt = 0;
      this.finishStartedAt = 0;
      this.linkStartedAt = 0;
      this.state = 0;
      this.stage = -1;
      this.progress.fill(0);
      this.links.fill(0);
      this.lens.placed = false;
      this.lens.strength = 0;
      this.overlay?.classList.add("has-map");
      this.backend?.setPalette(readPalette(this.canvas));
      if (this.reduced) this.renderOnce();
      else if (!document.hidden) this.raf = requestAnimationFrame(this.boundLoop);
    }

    stop() {
      this.active = false;
      cancelAnimationFrame(this.raf);
      this.raf = 0;
    }

    setProgress(progress, stage) {
      const next = Number.isFinite(stage) ? stage : -1;
      // stages only move forward; a skipped collector counts as read
      if (next > this.stage) this.stage = next;
      if (this.reduced) {
        for (let i = 0; i < DISTRICTS.length; i += 1) {
          this.progress[i] = i < this.stage ? 1 : i === this.stage ? 0.6 : 0;
        }
        this.renderOnce();
      }
    }

    setState(state) {
      this.state = state === "complete" ? 1 : state === "error" ? -1 : 0;
      if (this.state !== 0) this.finishStartedAt = performance.now();
      if (this.reduced) {
        if (this.state === 1) this.progress.fill(1);
        this.renderOnce();
      }
    }

    renderOnce() {
      const now = performance.now();
      this._tick(now, 16);
      this.options.onTick?.(Math.max(0, now - this.startedAt));
    }
  }

  window.AccurettaSecurityScanField = {
    create(canvas, options) {
      if (!canvas) return null;
      return new SecurityScanField(canvas, options);
    },
  };
})();
