(function () {
  "use strict";

  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function hash2(x, y) {
    const value = Math.sin(x * 127.1 + y * 311.7) * 43758.5453;
    return value - Math.floor(value);
  }

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
    return fallback.slice();
  }

  function readPalette(canvas) {
    const styles = getComputedStyle(canvas.closest(".security-scan-intro") || document.documentElement);
    return {
      primary: parseColor(styles.getPropertyValue("--scan-cool") || styles.getPropertyValue("--accent"), [0.42, 0.38, 1]),
      hot: parseColor(styles.getPropertyValue("--scan-hot"), [1, 0.25, 0.44]),
      warm: parseColor(styles.getPropertyValue("--scan-warm"), [1, 0.45, 0.22]),
      foreground: parseColor(styles.getPropertyValue("--fg"), [0.94, 0.94, 0.97]),
      quiet: parseColor(styles.getPropertyValue("--fg-muted"), [0.46, 0.46, 0.52]),
      success: parseColor(styles.getPropertyValue("--success"), [0.39, 0.76, 0.59]),
      danger: parseColor(styles.getPropertyValue("--danger"), [1, 0.34, 0.4]),
    };
  }

  function pushVertex(target, x, y, z, weight, group) {
    target.push(x, y, z, weight, group);
  }

  function pushLine(target, start, end, weight, group) {
    pushVertex(target, start[0], start[1], start[2], weight, group);
    pushVertex(target, end[0], end[1], end[2], weight, group);
  }

  function addFrame(target, cx, cz, width, depth, y, group, weight, density = 22) {
    const xSteps = Math.max(2, Math.round(width * density));
    const zSteps = Math.max(2, Math.round(depth * density));
    for (let i = 0; i <= xSteps; i += 1) {
      const x = cx - width / 2 + width * i / xSteps;
      pushVertex(target, x, y, cz - depth / 2, weight, group);
      pushVertex(target, x, y, cz + depth / 2, weight, group);
    }
    for (let i = 1; i < zSteps; i += 1) {
      const z = cz - depth / 2 + depth * i / zSteps;
      pushVertex(target, cx - width / 2, y, z, weight, group);
      pushVertex(target, cx + width / 2, y, z, weight, group);
    }
  }

  function buildGeometry() {
    const points = [];
    const lines = [];
    const columns = 101;
    const rows = 65;

    for (let row = 0; row < rows; row += 1) {
      const z = -0.88 + row / (rows - 1) * 1.76;
      for (let column = 0; column < columns; column += 1) {
        const x = -1.42 + column / (columns - 1) * 2.84;
        const boundary = Math.pow(Math.abs(x) / 1.42, 7) + Math.pow(Math.abs(z) / 0.88, 7);
        if (boundary > 1) continue;

        const ax = Math.abs(x);
        const az = Math.abs(z);
        const core = ax < 0.31 && az < 0.25;
        const memory = ax > 0.51 && ax < 0.69 && az < 0.59;
        const perimeter = boundary > 0.78;
        const horizontalBus = Math.abs(z - 0.43) < 0.018 || Math.abs(z + 0.43) < 0.018;
        const verticalBus = Math.abs(x - 0.92) < 0.018 || Math.abs(x + 0.92) < 0.018;
        const coreBus = (Math.abs(x) < 0.018 && az > 0.25) || (Math.abs(z) < 0.018 && ax > 0.31);
        const trace = horizontalBus || verticalBus || coreBus;
        const random = hash2(column, row);
        if (!core && !memory && !perimeter && !trace && random < 0.18) continue;

        let group = z < -0.5 ? 0 : z < -0.16 ? 1 : z < 0.12 ? 2 : z < 0.38 ? 4 : 5;
        if (core) group = 3;
        if (memory) group = x < 0 ? 2 : 5;
        if (perimeter) group = x < 0 ? 0 : 4;
        if (trace) group = 6;

        let y = -0.13 + Math.sin(column * 0.37 + row * 0.23) * 0.006;
        let weight = 0.22 + random * 0.15;
        if (trace) {
          y = -0.075;
          weight = 0.62;
        }
        if (memory) {
          y = 0.025;
          weight = 0.58;
        }
        if (core) {
          y = 0.085;
          weight = 0.76;
        }
        if (perimeter) weight = Math.max(weight, 0.44);
        pushVertex(points, x, y, z, weight, group);
      }
    }

    const chassisLevels = [-0.13, -0.035, 0.06, 0.155, 0.25];
    chassisLevels.forEach((y, index) => {
      addFrame(points, 0, 0, 2.67 - index * 0.035, 1.57 - index * 0.025, y, index < 2 ? 0 : index < 4 ? 6 : 7, 0.48 + index * 0.045, 25);
    });
    for (const x of [-1.305, 1.305]) {
      for (const z of [-0.755, 0.755]) {
        for (let step = 0; step <= 16; step += 1) {
          pushVertex(points, x, -0.13 + step * 0.024, z, 0.6, step < 8 ? 1 : 6);
        }
      }
    }

    for (let layer = 0; layer < 8; layer += 1) {
      const amount = layer / 7;
      addFrame(points, 0, 0, 0.62 - amount * 0.08, 0.50 - amount * 0.07, 0.09 + layer * 0.037, 3, 0.9, 25);
    }
    addFrame(points, 0, 0, 0.34, 0.25, 0.37, 7, 1, 28);

    const memoryCenters = [-0.61, 0.61];
    for (const x of memoryCenters) {
      for (const z of [-0.34, 0.34]) {
        for (let layer = 0; layer < 4; layer += 1) {
          addFrame(points, x, z, 0.16, 0.42, 0.03 + layer * 0.032, x < 0 ? 2 : 5, 0.72, 24);
        }
      }
    }

    for (const x of [-1.15, -0.84, 0.84, 1.15]) {
      for (let layer = 0; layer < 3; layer += 1) {
        addFrame(points, x, 0.68, 0.19, 0.12, -0.08 + layer * 0.035, x < 0 ? 0 : 4, 0.66, 28);
      }
    }

    const traceY = -0.068;
    const paths = [
      [[-1.28, traceY, -0.43], [-0.33, traceY, -0.43], 0],
      [[0.33, traceY, 0.43], [1.28, traceY, 0.43], 4],
      [[-0.93, traceY, -0.76], [-0.93, traceY, 0.68], 1],
      [[0.93, traceY, -0.68], [0.93, traceY, 0.76], 5],
      [[0, traceY, -0.84], [0, traceY, -0.26], 6],
      [[0, traceY, 0.26], [0, traceY, 0.84], 6],
      [[-0.31, traceY, 0], [-1.20, traceY, 0], 6],
      [[0.31, traceY, 0], [1.20, traceY, 0], 6],
    ];
    for (const [start, end, group] of paths) {
      pushLine(lines, start, end, 0.7, group);
      const distance = Math.hypot(end[0] - start[0], end[2] - start[2]);
      const steps = Math.max(2, Math.ceil(distance / 0.035));
      for (let step = 0; step <= steps; step += 1) {
        const amount = step / steps;
        pushVertex(
          points,
          start[0] + (end[0] - start[0]) * amount,
          start[1] + 0.006,
          start[2] + (end[2] - start[2]) * amount,
          0.86,
          group,
        );
      }
    }

    const ring = [
      [-0.31, traceY, -0.25], [0.31, traceY, -0.25],
      [0.31, traceY, 0.25], [-0.31, traceY, 0.25],
    ];
    for (let i = 0; i < ring.length; i += 1) pushLine(lines, ring[i], ring[(i + 1) % ring.length], 0.9, 7);

    // Suspended contour sheets expose the machine as a layered volume.
    for (let layer = 0; layer < 7; layer++) {
      const y = 0.44 + layer * 0.068;
      for (let row = 0; row < 13; row++) {
        const z = -0.69 + row * 0.115;
        let previous = null;
        for (let column = 0; column <= 44; column++) {
          const x = -1.2 + column / 44 * 2.4;
          const dome = Math.exp(-(x * x * 2.4 + z * z * 4.0)) * 0.11;
          const point = [x, y + dome, z];
          if (previous) pushLine(lines, previous, point, 0.16 + layer * 0.018, 8 + layer);
          if (column % 3 === 0) pushVertex(points, ...point, 0.23, 8 + layer);
          previous = point;
        }
      }
    }
    // Fine registration marks keep the silhouette legible between scan passes.
    for (const x of [-1.42, 1.42]) {
      for (const z of [-0.86, 0.86]) {
        const inwardX = -Math.sign(x) * 0.16;
        const inwardZ = -Math.sign(z) * 0.16;
        pushLine(lines, [x, -0.16, z], [x + inwardX, -0.16, z], 0.8, 7);
        pushLine(lines, [x, -0.16, z], [x, -0.16, z + inwardZ], 0.8, 7);
      }
    }

    return {
      points: new Float32Array(points),
      pointCount: points.length / 5,
      lines: new Float32Array(lines),
      lineCount: lines.length / 5,
    };
  }

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

  const FIELD_VERTEX_SHADER = `
    precision highp float;
    attribute vec3 aPosition;
    attribute float aWeight;
    attribute float aGroup;
    uniform vec2 uResolution;
    uniform float uDpr;
    uniform float uTime;
    uniform float uProgress;
    uniform float uStage;
    uniform float uState;
    uniform float uFinish;
    uniform mediump float uGlowPass;
    uniform vec3 uPrimary;
    uniform vec3 uHot;
    uniform vec3 uWarm;
    uniform vec3 uForeground;
    uniform vec3 uQuiet;
    uniform vec3 uSuccess;
    uniform vec3 uDanger;
    varying vec3 vColor;
    varying float vAlpha;

    vec2 rotate2(vec2 value, float angle) {
      float c = cos(angle);
      float s = sin(angle);
      return vec2(c * value.x - s * value.y, s * value.x + c * value.y);
    }

    void main() {
      float eased = uProgress * uProgress * (3.0 - 2.0 * uProgress);
      float scanZ = mix(-0.96, 0.96, eased) + sin(uTime * 1.7) * 0.025;
      float scan = exp(-pow((aPosition.z - scanZ) * 8.5, 2.0));
      float probeZ = mix(-1.02, 1.02, fract(uTime * 0.105));
      float probe = exp(-pow((aPosition.z - probeZ) * 18.0, 2.0));
      float layer = step(7.5, aGroup);
      float active = exp(-pow((mod(aGroup, 8.0) - uStage) * 1.35, 2.0));
      float completed = 1.0 - smoothstep(uStage - 0.15, uStage + 0.35, aGroup);
      completed *= step(-0.5, uStage);
      float radius = length(aPosition.xz / vec2(1.42, 0.88));
      float finishRing = exp(-pow((radius - uFinish * 1.18) * 8.0, 2.0)) * step(0.001, uFinish);
      float flux = sin(uTime * 2.1 + aPosition.x * 7.0 + aPosition.z * 10.0) * 0.5 + 0.5;

      vec3 position = aPosition;
      float assembled = smoothstep(layer * 0.18, 1.8 + layer * 0.65, uTime);
      float separation = (1.0 - assembled);
      position.xz *= 1.0 + separation * (0.2 + aPosition.y * 0.12);
      position.y += separation * (aPosition.y + 0.25) * 1.15;
      position.y += layer * sin(uTime * 0.48 + aGroup * 0.31) * 0.012;
      float surfaceWave = sin(position.x * 7.0 + position.z * 5.0 - uTime * 3.2) * 0.5 + 0.5;
      position.y += scan * (0.045 + surfaceWave * 0.035) * (0.45 + aWeight);
      position.y += probe * (0.018 + flux * 0.026) * (0.4 + aWeight);
      position.y += active * (flux - 0.5) * 0.013 * aWeight;
      position.y += finishRing * 0.055;

      float yaw = -0.28 + sin(uTime * 0.16) * 0.075;
      position.xz = rotate2(position.xz, yaw);
      position.yz = rotate2(position.yz, -0.74);
      float depth = 3.55 + position.z;
      float aspect = uResolution.x / max(1.0, uResolution.y);
      float viewScale = aspect > 1.15 ? 2.25 : 1.85;
      vec2 projected = position.xy * (viewScale / depth);
      projected.x /= aspect;
      projected += vec2(aspect > 1.15 ? 0.30 : 0.0, 0.04);
      float clipDepth = clamp((depth - 2.2) / 2.8, 0.0, 1.0) * 2.0 - 1.0;
      gl_Position = vec4(projected, clipDepth, 1.0);

      float energy = clamp(completed * 0.4 + active * (0.54 + flux * 0.12) + scan * 0.92 + probe * 0.24 + finishRing, 0.0, 1.0);
      vec3 baseColor = mix(uQuiet, uForeground, 0.34 + aWeight * 0.3);
      vColor = mix(baseColor, mix(uPrimary, uForeground, 0.22), energy);
      vColor = mix(vColor, uHot, clamp(active * 0.23 + scan * active * 0.22, 0.0, 0.48));
      vColor = mix(vColor, uWarm, scan * surfaceWave * 0.12);
      float slice = exp(-pow((aPosition.z - probeZ) * 46.0, 2.0));
      vColor = mix(vColor, uForeground, slice * 0.82);
      if (uState > 0.5) vColor = mix(vColor, uSuccess, 0.38 + finishRing * 0.62);
      if (uState < -0.5) vColor = mix(vColor, uDanger, 0.72);

      float size = 0.9 + aWeight * 1.35 + scan * 1.65 + slice * 1.2 + active * 0.45 + finishRing * 1.8;
      size *= clamp(3.25 / depth, 0.72, 1.25) * uDpr;
      gl_PointSize = min(18.0, size * mix(1.0, 2.65, uGlowPass));
      float alpha = 0.16 + aWeight * 0.32 + completed * 0.15 + active * (0.19 + flux * 0.06) + scan * 0.68 + probe * 0.22 + finishRing * 0.48;
      alpha = mix(alpha, 0.19 + probe * 0.52 + slice * 0.75 + scan * 0.12 + finishRing * 0.42, layer);
      if (uState < -0.5) alpha *= 0.8;
      vAlpha = clamp(alpha, 0.0, 0.98) * smoothstep(0.0, 0.6, uTime) * mix(1.0, 0.16, uGlowPass);
    }
  `;

  const POINT_FRAGMENT_SHADER = `
    precision mediump float;
    uniform mediump float uGlowPass;
    varying vec3 vColor;
    varying float vAlpha;

    void main() {
      vec2 point = gl_PointCoord * 2.0 - 1.0;
      float core = 1.0 - smoothstep(0.28, 1.0, length(point));
      float halo = 1.0 - smoothstep(0.0, 1.0, length(point));
      float shape = mix(core, halo * halo, uGlowPass);
      float alpha = shape * vAlpha;
      if (alpha < 0.008) discard;
      gl_FragColor = vec4(vColor, alpha);
    }
  `;

  const LINE_FRAGMENT_SHADER = `
    precision mediump float;
    varying vec3 vColor;
    varying float vAlpha;
    void main() {
      gl_FragColor = vec4(vColor, vAlpha * 0.65);
    }
  `;

  const BACKGROUND_VERTEX_SHADER = `
    attribute vec2 aPosition;
    varying vec2 vUv;
    void main() {
      vUv = aPosition * 0.5 + 0.5;
      gl_Position = vec4(aPosition, 0.0, 1.0);
    }
  `;

  const BACKGROUND_FRAGMENT_SHADER = `
    precision mediump float;
    uniform vec2 uResolution;
    uniform float uDpr;
    uniform float uTime;
    uniform float uProgress;
    uniform vec3 uPrimary;
    uniform vec3 uQuiet;
    varying vec2 vUv;

    float hash21(vec2 point) {
      point = fract(point * vec2(123.34, 456.21));
      point += dot(point, point + 45.32);
      return fract(point.x * point.y);
    }

    void main() {
      float aspect = uResolution.x / max(1.0, uResolution.y);
      vec2 center = vec2(aspect > 1.15 ? 0.68 : 0.5, aspect > 1.15 ? 0.47 : 0.55);
      vec2 fieldPoint = (vUv - center) * vec2(aspect, 1.0);
      float field = exp(-dot(fieldPoint * vec2(0.82, 1.0), fieldPoint * vec2(0.82, 1.0)) * 1.65);
      vec2 cell = mod(gl_FragCoord.xy, vec2(18.0 * uDpr)) - 9.0 * uDpr;
      float dotShape = 1.0 - smoothstep(0.7 * uDpr, 1.35 * uDpr, length(cell));
      float sweepX = mix(-0.72, 0.72, uProgress * uProgress * (3.0 - 2.0 * uProgress));
      float probeX = mix(-0.9, 0.9, fract(uTime * 0.07));
      float beam = exp(-pow((fieldPoint.x - sweepX) * 8.0, 2.0)) * field;
      float probe = exp(-pow((fieldPoint.x - probeX) * 26.0, 2.0)) * field;
      float grain = hash21(floor(gl_FragCoord.xy / max(1.0, uDpr)) + floor(uTime * 4.0)) - 0.5;
      vec3 color = mix(uQuiet, uPrimary, 0.58 + beam * 0.3 + probe * 0.12);
      float alpha = dotShape * field * (0.026 + beam * 0.09 + probe * 0.05) + field * 0.012 + grain * 0.006 * field;
      gl_FragColor = vec4(color, max(0.0, alpha));
    }
  `;

  class WebGLField {
    constructor(canvas, geometry) {
      this.canvas = canvas;
      this.geometry = geometry;
      this.gl = canvas.getContext("webgl", {
        alpha: true,
        antialias: true,
        depth: true,
        powerPreference: "low-power",
        premultipliedAlpha: true,
      }) || canvas.getContext("experimental-webgl");
      if (!this.gl) throw new Error("WebGL is unavailable");
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.palette = null;
      this._initialize();
    }

    _initialize() {
      const gl = this.gl;
      this.pointProgram = createProgram(gl, FIELD_VERTEX_SHADER, POINT_FRAGMENT_SHADER);
      this.lineProgram = createProgram(gl, FIELD_VERTEX_SHADER, LINE_FRAGMENT_SHADER);
      this.backgroundProgram = createProgram(gl, BACKGROUND_VERTEX_SHADER, BACKGROUND_FRAGMENT_SHADER);
      this.pointBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.pointBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, this.geometry.points, gl.STATIC_DRAW);
      this.lineBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.lineBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, this.geometry.lines, gl.STATIC_DRAW);
      this.quadBuffer = gl.createBuffer();
      gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, -1, 1, 1, -1, 1, 1]), gl.STATIC_DRAW);
    }

    setPalette(palette) {
      this.palette = palette;
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

    _bindField(program, buffer, frame, glowPass) {
      const gl = this.gl;
      gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      const stride = 5 * Float32Array.BYTES_PER_ELEMENT;
      const position = gl.getAttribLocation(program, "aPosition");
      const weight = gl.getAttribLocation(program, "aWeight");
      const group = gl.getAttribLocation(program, "aGroup");
      gl.enableVertexAttribArray(position);
      gl.enableVertexAttribArray(weight);
      gl.enableVertexAttribArray(group);
      gl.vertexAttribPointer(position, 3, gl.FLOAT, false, stride, 0);
      gl.vertexAttribPointer(weight, 1, gl.FLOAT, false, stride, 3 * Float32Array.BYTES_PER_ELEMENT);
      gl.vertexAttribPointer(group, 1, gl.FLOAT, false, stride, 4 * Float32Array.BYTES_PER_ELEMENT);
      gl.uniform2f(gl.getUniformLocation(program, "uResolution"), this.width, this.height);
      gl.uniform1f(gl.getUniformLocation(program, "uDpr"), this.dpr);
      gl.uniform1f(gl.getUniformLocation(program, "uTime"), frame.time);
      gl.uniform1f(gl.getUniformLocation(program, "uProgress"), frame.progress);
      gl.uniform1f(gl.getUniformLocation(program, "uStage"), frame.stage);
      gl.uniform1f(gl.getUniformLocation(program, "uState"), frame.state);
      gl.uniform1f(gl.getUniformLocation(program, "uFinish"), frame.finish);
      gl.uniform1f(gl.getUniformLocation(program, "uGlowPass"), glowPass);
      for (const [uniform, color] of [
        ["uPrimary", this.palette.primary], ["uHot", this.palette.hot], ["uWarm", this.palette.warm],
        ["uForeground", this.palette.foreground], ["uQuiet", this.palette.quiet],
        ["uSuccess", this.palette.success], ["uDanger", this.palette.danger],
      ]) gl.uniform3fv(gl.getUniformLocation(program, uniform), color);
    }

    _drawBackground(frame) {
      const gl = this.gl;
      const program = this.backgroundProgram;
      gl.useProgram(program);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.quadBuffer);
      const position = gl.getAttribLocation(program, "aPosition");
      gl.enableVertexAttribArray(position);
      gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
      gl.uniform2f(gl.getUniformLocation(program, "uResolution"), this.width, this.height);
      gl.uniform1f(gl.getUniformLocation(program, "uDpr"), this.dpr);
      gl.uniform1f(gl.getUniformLocation(program, "uTime"), frame.time);
      gl.uniform1f(gl.getUniformLocation(program, "uProgress"), frame.progress);
      gl.uniform3fv(gl.getUniformLocation(program, "uPrimary"), this.palette.primary);
      gl.uniform3fv(gl.getUniformLocation(program, "uQuiet"), this.palette.quiet);
      gl.drawArrays(gl.TRIANGLES, 0, 6);
    }

    render(frame) {
      if (!this.palette) return;
      const gl = this.gl;
      gl.depthMask(true);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      gl.disable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      this._drawBackground(frame);

      gl.enable(gl.DEPTH_TEST);
      gl.depthFunc(gl.LEQUAL);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      gl.depthMask(false);
      this._bindField(this.lineProgram, this.lineBuffer, frame, 0);
      gl.drawArrays(gl.LINES, 0, this.geometry.lineCount);
      this._bindField(this.pointProgram, this.pointBuffer, frame, 1);
      gl.drawArrays(gl.POINTS, 0, this.geometry.pointCount);
      gl.depthMask(true);
      this._bindField(this.pointProgram, this.pointBuffer, frame, 0);
      gl.drawArrays(gl.POINTS, 0, this.geometry.pointCount);
      gl.disable(gl.DEPTH_TEST);
    }
  }

  class CanvasField {
    constructor(canvas, geometry) {
      this.canvas = canvas;
      this.geometry = geometry;
      this.context = canvas.getContext("2d");
      if (!this.context) throw new Error("Canvas is unavailable");
      this.width = 0;
      this.height = 0;
      this.dpr = 1;
      this.palette = null;
    }

    setPalette(palette) {
      this.palette = palette;
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
      if (!this.palette) return;
      const ctx = this.context;
      const aspect = this.width / this.height;
      const centerX = this.width * (aspect > 1.15 ? 0.68 : 0.5);
      const centerY = this.height * (aspect > 1.15 ? 0.48 : 0.57);
      const scale = Math.min(this.width, this.height) * 0.42;
      const data = this.geometry.points;
      ctx.clearRect(0, 0, this.width, this.height);
      for (let i = 0; i < data.length; i += 5) {
        const x = data[i];
        const y = data[i + 1];
        const z = data[i + 2];
        const weight = data[i + 3];
        const group = data[i + 4];
        const rotatedY = y * 0.78 + z * 0.62;
        const px = centerX + x * scale;
        const py = centerY - rotatedY * scale;
        const scanZ = -0.96 + frame.progress * 1.92;
        const scan = Math.exp(-Math.pow((z - scanZ) * 7.5, 2));
        const active = Math.exp(-Math.pow((group - frame.stage) * 1.2, 2));
        const mixAmount = clamp(scan * 0.9 + active * 0.45, 0, 1);
        const color = this.palette.quiet.map((value, index) => value + (this.palette.primary[index] - value) * mixAmount);
        const alpha = clamp(0.07 + weight * 0.18 + scan * 0.62, 0, 0.9);
        const size = (1.1 + weight * 1.6 + scan * 2.2) * this.dpr;
        ctx.fillStyle = `rgba(${Math.round(color[0] * 255)},${Math.round(color[1] * 255)},${Math.round(color[2] * 255)},${alpha})`;
        ctx.fillRect(px - size / 2, py - size / 2, size, size);
      }
    }
  }

  class SecurityScanField {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.options = options;
      this.geometry = buildGeometry();
      this.backend = null;
      this.active = false;
      this.raf = 0;
      this.startedAt = 0;
      this.lastFrameAt = 0;
      this.lastClockAt = 0;
      this.progress = 0.04;
      this.targetProgress = 0.04;
      this.stage = -1;
      this.state = 0;
      this.finishStartedAt = 0;
      this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
      this.remote = document.body.classList.contains("remote-client");
      this.mobile = matchMedia("(max-width: 700px)").matches;
      this.boundLoop = now => this._loop(now);
      this.boundVisibility = () => this._handleVisibility();
      this.boundContextLost = event => this._handleContextLost(event);
      this.boundContextRestored = () => this._handleContextRestored();
      document.addEventListener("visibilitychange", this.boundVisibility);
      this._attachCanvasEvents(canvas);
      this._createBackend();
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
        this.backend = new WebGLField(this.canvas, this.geometry);
        this.canvas.dataset.renderer = "webgl";
      } catch (error) {
        console.warn("Security scan WebGL renderer unavailable:", error);
        try {
          this.backend = new CanvasField(this.canvas, this.geometry);
          this.canvas.dataset.renderer = "canvas";
        } catch (fallbackError) {
          try {
            this.backend = new CanvasField(this._replaceCanvas(), this.geometry);
            this.canvas.dataset.renderer = "canvas";
          } catch (replacementError) {
            console.warn("Security scan canvas renderer unavailable:", fallbackError, replacementError);
            this.backend = null;
            this.canvas.dataset.renderer = "css";
          }
        }
      }
      this.backend?.setPalette(readPalette(this.canvas));
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

    _frame(now) {
      const finish = this.finishStartedAt ? clamp((now - this.finishStartedAt) / 1050, 0, 1.25) : 0;
      return {
        time: this.reduced ? 3 : Math.max(0, now - this.startedAt) / 1000,
        progress: this.progress,
        stage: this.stage,
        state: this.state,
        finish,
      };
    }

    _loop(now) {
      this.raf = 0;
      if (!this.active || document.hidden) return;
      const frameInterval = this.remote || this.mobile ? 1000 / 30 : 1000 / 60;
      if (!this.lastFrameAt || now - this.lastFrameAt >= frameInterval) {
        const delta = this.lastFrameAt ? Math.min(64, now - this.lastFrameAt) : frameInterval;
        this.lastFrameAt = now;
        const smoothing = 1 - Math.exp(-delta / 360);
        this.progress += (this.targetProgress - this.progress) * smoothing;
        const maxPixels = this.remote || this.mobile ? 1100000 : 2300000;
        this.backend?.resize(maxPixels);
        this.backend?.render(this._frame(now));
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
      this.state = 0;
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
      this.targetProgress = clamp(Number(progress) || 0.04, 0.04, 1);
      this.stage = Number.isFinite(stage) ? stage : -1;
      if (this.reduced) {
        this.progress = this.targetProgress;
        this.renderOnce();
      }
    }

    setState(state) {
      this.state = state === "complete" ? 1 : state === "error" ? -1 : 0;
      if (this.state === 1) {
        this.targetProgress = 1;
        this.stage = 7;
        this.finishStartedAt = performance.now();
      } else if (this.state < 0) {
        this.finishStartedAt = performance.now();
      }
      if (this.reduced) {
        this.progress = this.targetProgress;
        this.renderOnce();
      }
    }

    renderOnce() {
      const now = performance.now();
      const maxPixels = this.remote || this.mobile ? 1100000 : 2300000;
      this.backend?.resize(maxPixels);
      this.backend?.render(this._frame(now));
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
