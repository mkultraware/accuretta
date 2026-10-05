// motion.js: the heavy helpers, ported from the original Accuretta promo engine.
// Pure functions of their inputs; no colours or fonts live here (callers pass them
// in via options so any style works). Determinism rules apply: use these instead
// of writing your own physics/keyframe/noise maths. All sizes in px for a reason:
// scale by U or W in the caller.
import { W, H, lerp, clamp, hash, rng } from './core.js';

const TAU = Math.PI * 2;

// smoothstep default easing (matches the original engine's kf)
const smooth = t => { t = clamp(t); return t * t * (3 - 2 * t); };

// piecewise keyframe interpolation. keys: [[x0,v0],[x1,v1],...]; e(x) for the
// segment easing; values may be numbers or [r,g,b] arrays of numbers.
export function kf(x, keys, e = smooth) {
  if (x <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i++) if (x < keys[i][0]) {
    const [a, va] = keys[i - 1], [b, vb] = keys[i], k = e((x - a) / (b - a));
    return Array.isArray(va) ? va.map((v, j) => lerp(v, vb[j], k)) : lerp(va, vb, k);
  }
  return keys[keys.length - 1][1];
}

// smooth deterministic 1D noise (value noise with smoothstep-wide lerps)
export function noise1(x) { const i = Math.floor(x), f = x - i; return lerp(hash(i), hash(i + 1), f * f * (3 - 2 * f)); }

// physics-flavoured motion, domain in SECONDS (x is a beat time -> pass seconds):
export const spring = (x, x0, k = 6, w = 18) => x < x0 ? 0 : Math.exp(-k * (x - x0)) * Math.sin(w * (x - x0));
export const hit = (x, at, k = 8) => x < at ? 0 : Math.exp(-(x - at) * k);

// deterministic camera shake at 30 fps; amt in px
export const shakeXY = (t, amt) => { const f = Math.floor(t * 30); return [(hash(f * 1.7) - .5) * 2 * amt, (hash(f * 2.3 + 9) - .5) * 2 * amt]; };

// camera: zoom/rot around a focal point (apply before drawing a scene)
export function cam(c, cx, cy, z = 1, rot = 0) { c.translate(W / 2, H / 2); c.rotate(rot); c.scale(z, z); c.translate(-cx, -cy); }

// ---- polygons / quads -------------------------------------------------------
export function poly(c, pts) { c.beginPath(); c.moveTo(pts[0][0], pts[0][1]); for (let i = 1; i < pts.length; i++) c.lineTo(pts[i][0], pts[i][1]); c.closePath(); }
// rounded-corner polygon: pts entries may be [x,y] or [x,y,cornerRadius]
export function polyR(c, pts) {
  c.beginPath(); c.moveTo(pts[0][0], pts[0][1]);
  const n = pts.length;
  for (let i = 0; i < n; i++) { const p = pts[i], q = pts[(i + 1) % n]; c.arcTo(p[0], p[1], q[0], q[1], p[2] || 0); }
  c.closePath();
}
export function qBounds(q) { let x0 = 1e9, x1 = -1e9, y0 = 1e9, y1 = -1e9; for (const [x, y] of q) { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); } return { x0, x1, y0, y1, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0, h: y1 - y0 }; }
// point at (u,v) inside a parallelogram viewed in perspective
export const quadAt = (q, u, v) => {
  const tx = lerp(q[0][0], q[1][0], u), ty = lerp(q[0][1], q[1][1], u);
  const bx = lerp(q[3][0], q[2][0], u), by = lerp(q[3][1], q[2][1], u);
  return [lerp(tx, bx, v), lerp(ty, by, v)];
};
// isometric slab quad (the Accuretta perspective look): LEAN ~ tan of the camera tilt
export function slabQ(x, base, w, h, dir = 1, lean = 0.65) { const s = h * lean * dir; return [[x + s, base - h], [x + w + s, base - h], [x + w, base], [x, base]]; }
// face-gradient slab with options: depth (px, right), win {cols,rows,fw,fh,lit|fn,col,darkHex,seed,cull},
// light [col,alpha,stop], fins n, rim col (+rimSide 'left'|'right'), fog [col,a]. All colours from the caller.
export function slab(c, q, o = {}) {
  const b = qBounds(q);
  if (o.depth) {
    const d = o.depth, s = o.sideDir || 1, a = s > 0 ? q[1] : q[0], e = s > 0 ? q[2] : q[3];
    c.fillStyle = o.side || 'rgba(0,0,0,.55)'; poly(c, [a, [a[0] + d * s, a[1] + d * .22], [e[0] + d * s, e[1]], e]); c.fill();
  }
  const g = c.createLinearGradient(0, b.y0, 0, b.y1); g.addColorStop(0, o.top); g.addColorStop(1, o.face);
  c.fillStyle = g; poly(c, q); c.fill();
  if (o.win) {
    const wv = o.win, R = rng(wv.seed || 1), fw = wv.fw ?? .38, fh = wv.fh ?? .5, dark = wv.dark || 'rgba(0,0,0,.4)';
    const cols = wv.cols, rows = wv.rows;
    for (let j = 0; j < rows; j++) {
      const v0 = (j + (1 - fh) / 2) / rows, v1 = v0 + fh / rows;
      if (wv.cull) { const yy = lerp(b.y0, b.y1, v0); if (yy < wv.cull[0] || yy > wv.cull[1]) { for (let i = 0; i < cols; i++) R(); continue; } }
      for (let i = 0; i < cols; i++) {
        const r = R(), lit = wv.fn ? wv.fn(i, j, r) : (r < (wv.lit || 0) ? 1 : 0);
        const u0 = (i + (1 - fw) / 2) / cols, u1 = u0 + fw / cols;
        c.fillStyle = lit > 0 ? (lit >= 1 ? wv.col : mixc(wv.dark || 'rgba(8,8,12,.9)', wv.col, lit)) : dark;
        poly(c, [quadAt(q, u0, v0), quadAt(q, u1, v0), quadAt(q, u1, v1), quadAt(q, u0, v1)]); c.fill();
      }
    }
  }
  if (o.light) { c.save(); poly(c, q); c.clip(); const lg = c.createLinearGradient(0, b.y1, 0, b.y0); lg.addColorStop(0, alphaStr(o.light[0], o.light[1])); lg.addColorStop(o.light[2] ?? .35, alphaStr(o.light[0], 0)); c.globalCompositeOperation = 'lighter'; c.fillStyle = lg; c.fillRect(b.x0, b.y0, b.w, b.h); c.restore(); }
  if (o.fins) { c.fillStyle = o.finCol || 'rgba(0,0,0,.35)'; for (let i = 1; i < o.fins; i++) { const u = i / o.fins; poly(c, [quadAt(q, u - .006, 0), quadAt(q, u + .006, 0), quadAt(q, u + .006, 1), quadAt(q, u - .006, 1)]); c.fill(); } }
  if (o.rim) { c.strokeStyle = o.rim; c.lineWidth = o.rimW || 3; const e = o.rimSide === 'left' ? [q[0], q[3]] : [q[1], q[2]]; c.beginPath(); c.moveTo(e[0][0], e[0][1]); c.lineTo(e[1][0], e[1][1]); c.stroke(); }
  if (o.fog) { c.fillStyle = alphaStr(o.fog[0], o.fog[1]); poly(c, q); c.fill(); }
}

// orthogonal brutalist block: setbacks, fins, deep window slits with varied light, occlusion shadow at base.
// colours (top, face, side, rim, lit window colours) come from the caller.
export function block(c, x, base, w, h, o = {}) {
  const R = rng(o.seed || 1), top = base - h;
  if (o.depth) { c.fillStyle = o.side || 'rgba(0,0,0,.55)'; poly(c, [[x + w, top], [x + w + o.depth, top + o.depth * .3], [x + w + o.depth, base], [x + w, base]]); c.fill(); }
  const g = c.createLinearGradient(0, top, 0, base); g.addColorStop(0, o.top); g.addColorStop(1, o.face); c.fillStyle = g; c.fillRect(x, top, w, h);
  const tiers = o.tiers ?? (R() < .5 ? 1 : 2); let cw = w, ct = top;
  for (let i = 0; i < tiers; i++) { const nw = cw * (.55 + R() * .25), th = 30 + R() * 90; ct -= th; c.fillStyle = o.top; c.fillRect(x + (w - nw) * (R() < .5 ? .5 : R()), ct, nw, th + 1); cw = nw; }
  const cols = Math.max(1, Math.round(w / (o.pitchX || 42))), rows = Math.max(1, Math.round(h / (o.pitchY || 54)));
  const pw = w / cols, ph = h / rows, sw = pw * (o.fw ?? .32), sh = ph * (o.fh ?? .55);
  const litCols = o.winCols || null;
  for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) {
    const r = R(), r2 = R(), wx = x + i * pw + (pw - sw) / 2, wy = top + j * ph + (ph - sh) / 2;
    const lit = o.fn ? o.fn(wx + sw / 2, wy + sh / 2, r, r2) : (r < (o.lit ?? .05) ? .45 + r2 * .55 : 0);
    if (lit) {
      const col = typeof lit === 'string' ? lit : (litCols ? litCols[Math.floor(r2 * litCols.length)] : 'rgba(255,241,214,.9)');
      c.fillStyle = col; c.globalAlpha = typeof lit === 'string' ? 1 : lit; c.fillRect(wx, wy, sw, sh); c.globalAlpha = 1;
    } else { c.fillStyle = 'rgba(0,0,0,.42)'; c.fillRect(wx, wy, sw, sh); }
  }
  if (o.fins) { c.fillStyle = 'rgba(0,0,0,.28)'; for (let i = 1; i < cols; i += o.fins) c.fillRect(x + i * pw - 3, top, 6, h); }
  const ao = c.createLinearGradient(0, base - 160, 0, base); ao.addColorStop(0, 'rgba(0,0,0,0)'); ao.addColorStop(1, 'rgba(0,0,0,.55)'); c.fillStyle = ao; c.fillRect(x, base - 160, w, 160);
  if (o.rim) { c.fillStyle = o.rim; c.fillRect(x, ct, 2, base - ct); }
  if (o.fog) { c.fillStyle = alphaStr(o.fog[0], o.fog[1]); c.fillRect(x, ct, w + (o.depth || 0), base - ct); }
}

// full-height vertical gradient (pad by 4x frame so sweeping y0/y1 never shows edges)
export function vgrad(c, stops, y0 = 0, y1 = H) {
  const g = c.createLinearGradient(0, y0, 0, y1); for (const [o, col] of stops) g.addColorStop(o, col); c.fillStyle = g; c.fillRect(-W * 4, y0 - H * 4, W * 9, (y1 - y0) + H * 8);
}

// radial glow dot (soft light blob, 3-stop falloff — tighter core than one stop)
export function glowDot(c, x, y, r, col, a = 1) {
  if (a <= 0 || r <= 0) return;
  const g = c.createRadialGradient(x, y, 0, x, y, r);
  g.addColorStop(0, alphaStr(col, a)); g.addColorStop(.25, alphaStr(col, a * .45)); g.addColorStop(1, alphaStr(col, 0));
  c.fillStyle = g; c.fillRect(x - r, y - r, 2 * r, 2 * r);
}

// horizontal light haze band centred on y
export function haze(c, y, h, col, a) { const g = c.createLinearGradient(0, y - h, 0, y + h); g.addColorStop(0, alphaStr(col, 0)); g.addColorStop(.5, alphaStr(col, a)); g.addColorStop(1, alphaStr(col, 0)); c.fillStyle = g; c.fillRect(-W * 4, y - h, W * 9, h * 2); }

// twinkling star field, deterministic; tint usually white or a pale C colour
export function stars(c, a, maxY = H, t = 0, tint = 'rgba(255,255,255,1)') {
  if (a <= 0) return; c.fillStyle = tint;
  for (let i = 0; i < 420; i++) {
    const x = hash(i * 3.1) * W, y = hash(i * 7.7) * H; if (y > maxY) continue;
    const r = .6 + hash(i * 1.3) * 1.6, aa = .25 + hash(i * 5.9) * .75, tw = hash(i) * TAU;
    c.globalAlpha = a * aa * (.7 + .3 * Math.sin(t * 2.3 + tw)); c.fillRect(x, y, r, r);
  }
  c.globalAlpha = 1;
}

// shaded planet with banding + terminator shadow + optional [ [ringR,lineW,col], ... ] rings.
// colours are caller-supplied (o.rim / o.light / o.base / o.dark); neutral fallbacks keep it style-agnostic.
export function planet(c, x, y, r, o = {}) {
  const rim = o.rim || 'rgba(255,255,255,.8)', light = o.light || 'rgba(230,228,235,1)', base = o.base || 'rgba(125,120,150,1)', dark = o.dark || 'rgba(21,19,31,1)';
  glowDot(c, x, y, r * 1.5, rim, o.glow ?? .18);
  const rot = o.ringRot ?? -.22, tilt = o.tilt ?? .17;
  const ring = front => { if (!o.ring) return; c.save(); c.translate(x, y); c.rotate(rot); for (const [rr, wd, col] of o.ring) { c.strokeStyle = col; c.lineWidth = wd; c.beginPath(); c.ellipse(0, 0, r * rr, r * rr * tilt, 0, front ? 0 : Math.PI, front ? Math.PI : TAU); c.stroke(); } c.restore(); };
  ring(false);
  c.save(); c.beginPath(); c.arc(x, y, r, 0, TAU); c.clip();
  const lg = c.createLinearGradient(x - r, y - r, x + r, y + r); lg.addColorStop(0, light); lg.addColorStop(.55, base); lg.addColorStop(1, dark);
  c.fillStyle = lg; c.fillRect(x - r, y - r, 2 * r, 2 * r);
  c.save(); c.translate(x, y); c.rotate(rot); const R = rng(o.seed || 3);
  for (let i = 0; i < 18; i++) { const by = (R() * 2 - 1) * r, bh = R() * r * .06 + 2; c.fillStyle = R() < .5 ? 'rgba(0,0,0,.07)' : 'rgba(255,255,255,.05)'; c.fillRect(-r, by, 2 * r, bh); }
  c.restore();
  const sg = c.createRadialGradient(x - r * .35, y - r * .4, r * .1, x - r * .1, y - r * .1, r * 1.45);
  sg.addColorStop(0, 'rgba(0,0,0,0)'); sg.addColorStop(.55, 'rgba(0,0,0,.2)'); sg.addColorStop(1, 'rgba(0,0,0,.94)'); c.fillStyle = sg; c.fillRect(x - r, y - r, 2 * r, 2 * r);
  c.restore(); ring(true);
}

// targeting bracket reticle (HUD motifs), col from caller
export function reticle(c, x, y, r, rot, col, lw = 3) {
  c.save(); c.translate(x, y); c.strokeStyle = col; c.lineWidth = lw; c.shadowColor = col; c.shadowBlur = 12;
  c.beginPath(); c.arc(0, 0, r, 0, TAU); c.stroke();
  c.save(); c.rotate(rot); for (let i = 0; i < 4; i++) { c.beginPath(); c.moveTo(r + 10, 0); c.lineTo(r + 50, 0); c.stroke(); c.rotate(TAU / 4); } c.restore();
  const b = r * 1.4, l = r * .38; for (const [sx, sy] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) { c.beginPath(); c.moveTo(sx * b, sy * (b - l)); c.lineTo(sx * b, sy * b); c.lineTo(sx * (b - l), sy * b); c.stroke(); }
  c.restore();
}

// deterministic particle burst (dust/sparks streaks) at time age (s). col from caller.
export function burst(c, x, y, age, o = {}) {
  if (age < 0 || age > (o.life || 1.2)) return;
  const n = o.n || 60, R = rng(o.seed || 7), g = o.g ?? 900, life = o.life || 1.2, col = o.col || 'rgba(255,255,255,1)';
  for (let i = 0; i < n; i++) {
    const a = (o.a0 ?? -Math.PI) + R() * (o.spread ?? Math.PI), sp = (o.v || 900) * (.3 + R() * .9), L = life * (.4 + R() * .6);
    if (age > L) { R(); continue; }
    const px = x + Math.cos(a) * sp * age, py = y + Math.sin(a) * sp * age + .5 * g * age * age, f = 1 - age / L;
    const vx = Math.cos(a) * sp, vy = Math.sin(a) * sp + g * age, len = (o.streak ?? .03);
    c.strokeStyle = alphaStr(col, clamp(f) * (o.alpha ?? 1)); c.lineWidth = (o.w || 3) * (.5 + R());
    c.beginPath(); c.moveTo(px, py); c.lineTo(px - vx * len, py - vy * len); c.stroke();
  }
}

// blend two rgba()/hex colour strings by k (0..1); used for lit windows
function mixc(a, b, k) {
  const pa = (alphaStr(a, 1).match(/[\d.]+/g) || [0, 0, 0, 1]).map(Number);
  const pb = (alphaStr(b, 1).match(/[\d.]+/g) || [255, 255, 255, 1]).map(Number);
  const ch = i => Math.round(lerp(pa[i] ?? 0, pb[i] ?? (pb.length === 3 ? 255 : 1), clamp(k)));
  return `rgba(${ch(0)},${ch(1)},${ch(2)},${lerp(pa[3] ?? 1, pb[3] ?? 1, clamp(k)).toFixed(3)})`;
}
// accept '#rrggbb' or 'rgba(...)' and produce rgba() with the given alpha
function alphaStr(col, a) {
  const s = String(col);
  if (s.startsWith('#')) {
    const h = s.slice(1), f = h.length === 3 ? h.replace(/./g, '$&$&') : h;
    return `rgba(${parseInt(f.slice(0, 2), 16)},${parseInt(f.slice(2, 4), 16)},${parseInt(f.slice(4, 6), 16)},${a})`;
  }
  const p = s.match(/[\d.]+/g) || [255, 255, 255, 1];
  return `rgba(${p[0]},${p[1]},${p[2]},${a})`;
}
