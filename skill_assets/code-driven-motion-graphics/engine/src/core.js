// core.js: timing, math, easing, text roles, effects. Style-neutral: every colour and font comes from style.js.
import { NAME, W, H, FPS, BPM, DROP_SONG, DROP, TOTAL } from './project.js';
import { STYLE } from './style.js';

export { NAME, W, H, FPS, BPM, DROP_SONG, DROP, TOTAL, STYLE };
export const B = 60 / BPM;                       // seconds per beat
export const SONG_START = DROP_SONG - DROP * B;  // song second that plays at edit second 0
export const C = STYLE.C;                        // the only place shots may take colours from
export const U = Math.min(W, H) / 100;           // layout unit: 1% of the short side (keeps shots aspect-agnostic)

// math
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
export const lerp = (a, b, t) => a + (b - a) * t;
export const inv = (a, b, x) => clamp((x - a) / (b - a));
export const frac = x => x - Math.floor(x);
export const ease = {
  lin: t => t, out: t => 1 - Math.pow(1 - t, 3), in: t => t * t * t,
  io: t => (t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
  expo: t => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
  inExpo: t => (t <= 0 ? 0 : Math.pow(2, 10 * t - 10)),
  back: t => { const c = 1.9; return 1 + (c + 1) * Math.pow(t - 1, 3) + c * Math.pow(t - 1, 2); },
  elastic: t => (t <= 0 ? 0 : t >= 1 ? 1 : Math.pow(2, -10 * t) * Math.sin((t * 10 - .75) * (2 * Math.PI / 3)) + 1),
};
export const ease0 = ease[STYLE.ease] || ease.out;   // the direction's default feel
export function rng(seed) { let a = seed >>> 0; return () => { a |= 0; a = a + 0x6D2B79F5 | 0; let t = Math.imul(a ^ a >>> 15, 1 | a); t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t; return ((t ^ t >>> 14) >>> 0) / 4294967296; }; }
export const hash = n => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453; return x - Math.floor(x); };
export const step = (t, fps) => Math.floor(t * fps) / fps;
export const pulse = (b, k = 5) => Math.exp(-frac(b) * k);   // 1 on every beat, decays after

// colour helpers
export function rgb(hex) { const h = hex.replace('#', ''), f = h.length === 3 ? h.replace(/./g, '$&$&') : h; return [parseInt(f.slice(0, 2), 16), parseInt(f.slice(2, 4), 16), parseInt(f.slice(4, 6), 16)]; }
export const alpha = (hex, a) => { const [r, g, b] = rgb(hex); return `rgba(${r},${g},${b},${a})`; };
export function mix(a, b, k) { const x = rgb(a), y = rgb(b); return '#' + x.map((v, i) => Math.round(lerp(v, y[i], k)).toString(16).padStart(2, '0')).join(''); }

// canvases
export function canvas(w = W, h = H) { const c = document.createElement('canvas'); c.width = w; c.height = h; return c; }
export const ctx2d = c => c.getContext('2d', { willReadFrequently: true });
export const fill = (ctx, color) => { ctx.fillStyle = color; ctx.fillRect(0, 0, W, H); };
export const flash = (ctx, color, a = 1) => { ctx.save(); ctx.globalAlpha = a; ctx.fillStyle = color; ctx.fillRect(0, 0, W, H); ctx.restore(); };

// fonts: load the style's Google Fonts before the first frame; warn loudly if they fall back
export async function loadFonts() {
  if (STYLE.fontsHref) {
    await new Promise(res => {
      const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = STYLE.fontsHref;
      l.onload = res; l.onerror = () => { console.error('FONT CSS FAILED TO LOAD (offline?): ' + STYLE.fontsHref); res(); };
      document.head.appendChild(l); setTimeout(res, 10000);
    });
  }
  const want = STYLE.preload || [];
  await Promise.all(want.map(f => document.fonts.load(f, 'Aa1').catch(() => {})));
  await document.fonts.ready;
  const bad = want.filter(f => !document.fonts.check(f));
  if (bad.length) console.error('FONT FALLBACK IN USE for: ' + bad.join(' | '));
}

// text: one function, three roles (display / sans / mono), all styled by STYLE.type
function setFont(ctx, role, size, o) {
  const T = STYLE.type || {};
  ctx.font = `${o.italic ? 'italic ' : ''}${o.weight ?? T[role + 'Weight'] ?? 500} ${size}px ${STYLE.fonts[role]}`;
  ctx.letterSpacing = ((o.track ?? T[role + 'Track'] ?? 0) * size) + 'px';
  ctx.textAlign = o.align || 'left'; ctx.textBaseline = o.base || 'alphabetic';
}
export function text(ctx, role, str, x, y, size, o = {}) {
  const sx = o.sx ?? (STYLE.type || {})[role + 'ScaleX'] ?? 1;
  ctx.save(); ctx.translate(x, y); if (sx !== 1) ctx.scale(sx, 1);
  setFont(ctx, role, size, o);
  ctx.globalAlpha *= o.alpha ?? 1; ctx.fillStyle = o.color || C.ink;
  if (o.stroke) { ctx.lineWidth = o.strokeW || size * .04; ctx.strokeStyle = o.stroke; ctx.strokeText(str, 0, 0); }
  ctx.fillText(str, 0, 0); ctx.restore();
}
export const display = (ctx, s, x, y, size, o) => text(ctx, 'display', s, x, y, size, o);
export const sans = (ctx, s, x, y, size, o) => text(ctx, 'sans', s, x, y, size, o);
export const mono = (ctx, s, x, y, size, o) => text(ctx, 'mono', s, x, y, size, o);
export function measure(ctx, role, str, size, o = {}) {
  const sx = o.sx ?? (STYLE.type || {})[role + 'ScaleX'] ?? 1;
  ctx.save(); setFont(ctx, role, size, o); const w = ctx.measureText(str).width * sx; ctx.restore(); return w;
}
// largest size <= `size` at which `str` fits in maxW. Use it for every headline so nothing runs off the frame.
export function fit(ctx, role, str, maxW, size, o = {}) {
  const w = measure(ctx, role, str, size, o); return w <= maxW ? size : size * maxW / w;
}
// word wrap; returns { bottom, lines }
export function wrap(ctx, role, str, x, y, size, maxW, lh = 1.2, o = {}) {
  const words = str.split(' '), lines = []; let cur = '';
  for (const w of words) { const t = cur ? cur + ' ' + w : w; if (measure(ctx, role, t, size, o) > maxW && cur) { lines.push(cur); cur = w; } else cur = t; }
  if (cur) lines.push(cur);
  lines.forEach((l, i) => text(ctx, role, l, x, y + i * size * lh, size, o));
  return { bottom: y + (lines.length - 1) * size * lh, lines };
}
// slide-up reveal inside a clip box: p 0..1. drawFn(dy) draws the content offset by dy.
export function maskUp(ctx, x, y, w, h, p, drawFn) {
  ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); drawFn((1 - ease.expo(clamp(p))) * h); ctx.restore();
}
const GLY = 'ABCDEFGHJKLMNPQRSTUVWXYZ0123456789/+#';
export function scramble(str, p, seed = 1, spread = .5) {
  if (p >= 1) return str; if (p <= 0) return '';
  let s = ''; const n = str.length;
  for (let i = 0; i < n; i++) {
    const at = (i / Math.max(1, n)) * (1 - spread), ch = str[i];
    if (p < at) break;
    if (ch === ' ' || p > at + spread) s += ch; else s += GLY[Math.floor(hash(seed * 31 + i * 7 + Math.floor(p * 40)) * GLY.length)];
  }
  return s;
}
export const typed = (str, p) => str.slice(0, Math.floor(clamp(p) * str.length + 1e-6));

// effects: pure functions of (ctx, t). Pick the subset a style needs in its post().
let grainTile = null;
export function initGrain() {
  grainTile = canvas(256, 256); const g = ctx2d(grainTile), im = g.createImageData(256, 256), r = rng(1234);
  for (let i = 0; i < im.data.length; i += 4) { const v = r() * 255; im.data[i] = im.data[i + 1] = im.data[i + 2] = v; im.data[i + 3] = 255; }
  g.putImageData(im, 0, 0);
}
export function grain(ctx, t, amt = .06) {
  if (!grainTile) initGrain();
  const k = Math.floor(t * 24), ox = Math.floor(hash(k) * 256), oy = Math.floor(hash(k + 91) * 256);
  ctx.save(); ctx.globalAlpha = amt; ctx.globalCompositeOperation = 'overlay';
  for (let y = -oy; y < H; y += 256) for (let x = -ox; x < W; x += 256) ctx.drawImage(grainTile, x, y);
  ctx.restore();
}
export function vignette(ctx, amt = .35) {
  const g = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * .3, W / 2, H / 2, Math.hypot(W, H) * .6);
  g.addColorStop(0, 'rgba(0,0,0,0)'); g.addColorStop(1, `rgba(0,0,0,${amt})`); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
}
export function scanlines(ctx, amt = .12, gap = 4) {
  ctx.save(); ctx.fillStyle = `rgba(0,0,0,${amt})`; for (let y = 0; y < H; y += gap) ctx.fillRect(0, y, W, Math.max(1, gap / 2)); ctx.restore();
}
export function chroma(ctx, px = 3) {
  const im = ctx.getImageData(0, 0, W, H), d = im.data, src = new Uint8ClampedArray(d);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4, xr = Math.min(W - 1, x + px), xb = Math.max(0, x - px);
    d[i] = src[(y * W + xr) * 4]; d[i + 2] = src[(y * W + xb) * 4 + 2];
  }
  ctx.putImageData(im, 0, 0);
}
const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5];
export function dither(ctx, c0 = C.bg, c1 = C.ink, o = {}) {
  const a = rgb(c0), b = rgb(c1), im = ctx.getImageData(0, 0, W, H), d = im.data, bias = o.bias ?? 0, cell = o.cell ?? 1;
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4, l = (d[i] * .3 + d[i + 1] * .59 + d[i + 2] * .11) / 255;
    const th = (BAYER[((y / cell | 0) % 4) * 4 + ((x / cell | 0) % 4)] + .5) / 16 + bias;
    const c = l > th ? b : a; d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2];
  }
  ctx.putImageData(im, 0, 0);
}
export function glow(ctx, color, blur, drawFn) { ctx.save(); ctx.shadowColor = color; ctx.shadowBlur = blur; drawFn(); ctx.restore(); }
export function hud(ctx, t, b, o = {}) {
  const m = 3.2 * U, tick = 2.2 * U, col = o.color || C.muted;
  ctx.save(); ctx.strokeStyle = col; ctx.lineWidth = Math.max(1.5, U * .18); ctx.globalAlpha = .9;
  [[m, m, 1, 1], [W - m, m, -1, 1], [m, H - m, 1, -1], [W - m, H - m, -1, -1]].forEach(([x, y, sx, sy]) => {
    ctx.beginPath(); ctx.moveTo(x + sx * tick, y); ctx.lineTo(x, y); ctx.lineTo(x, y + sy * tick); ctx.stroke();
  });
  ctx.restore();
  const s = 1.5 * U, mm = String(Math.floor(t / 60)).padStart(2, '0'), ss = String(Math.floor(t % 60)).padStart(2, '0'), ff = String(Math.floor((t % 1) * FPS)).padStart(2, '0');
  mono(ctx, o.label || NAME.toUpperCase(), m * 1.7, m * 1.7 + s, s, { color: col });
  mono(ctx, `${mm}:${ss}:${ff}`, W - m * 1.7, m * 1.7 + s, s, { color: col, align: 'right' });
  mono(ctx, `BEAT ${b.toFixed(1).padStart(5, '0')}`, m * 1.7, H - m * 1.7, s, { color: col });
}
export const FX = { grain, vignette, scanlines, chroma, dither, glow, hud };

// ---------- worn stencil sprite (cached so it can be sheared/scaled/blur-lifted cheaply) ----------
const SPRITES = new Map();
export function stencil(str, size, o = {}) {
  const role = o.role || 'sans';
  const key = [str, size, role, o.col, o.worn, o.weight, o.track, o.grainSeed || 1, STYLE.name].join('|');
  if (SPRITES.has(key)) return SPRITES.get(key);
  const sctx = ctx2d(canvas(1, 1));
  const w = Math.ceil(measure(sctx, role, str, size, o)) + size * .4, h = Math.ceil(size * 1.3);
  const cv = canvas(w, h), c = ctx2d(cv);
  text(c, role, str, size * .2, size * 1.02, size, { weight: o.weight, track: o.track, color: o.col });
  if (o.worn) {
    c.globalCompositeOperation = 'destination-out'; const R = rng(str.length * 97 + size);
    for (let i = 0; i < w * h / 90 * o.worn; i++) { c.globalAlpha = R() * .9; c.fillRect(R() * w, R() * h, 1 + R() * 3, 1 + R() * 2); }
    c.globalAlpha = .6; for (let i = 0; i < 6 * o.worn; i++) c.fillRect(0, R() * h, w, 1 + R() * 2);
    // stencil bridges: thin vertical gaps like a cut stencil
    c.globalAlpha = 1; for (let i = 0; i < str.length; i++) if (R() < .5) c.fillRect(size * .2 + (i + .5) * (w - size * .4) / str.length, size * .15 + R() * size * .5, size * .035, size * .22);
  }
  const sp = { cv, w, h, ox: size * .2, oy: size * 1.02 }; SPRITES.set(key, sp); return sp;
}
// draw a stencil so its baseline-left sits at (x, y); shear follows an italic slant when o.shear is set
export function drawStencil(c, sp, x, y, o = {}) {
  c.save(); c.translate(x, y); if (o.rot) c.rotate(o.rot); if (o.shear) c.transform(1, 0, -o.shear, 1, 0, 0); if (o.sx || o.sy) c.scale(o.sx || 1, o.sy || 1);
  if (o.alpha != null) c.globalAlpha *= o.alpha; if (o.op) c.globalCompositeOperation = o.op;
  const ax = o.align === 'center' ? sp.w / 2 - sp.ox : o.align === 'right' ? sp.w - sp.ox * 2 : 0;
  c.drawImage(sp.cv, -sp.ox - ax, -sp.oy); c.restore();
}

// rubber-stamp style badge: framed label with punched-out wear. Returns a canvas ~760x240; scale on draw.
export function stamp(label, col, seed = 3, o = {}) {
  const s = canvas(760, 240), c = ctx2d(s);
  c.strokeStyle = col; c.lineWidth = 12; c.strokeRect(14, 14, 732, 212);
  c.lineWidth = 4; c.strokeRect(34, 34, 692, 172);
  const fs = Math.min(118, 100 * 620 / measure(c, o.role || 'display', label, 100, { weight: 900, track: .06 }));
  text(c, o.role || 'display', label, 380, 126, fs, { weight: 900, track: .06, align: 'center', base: 'middle', color: col });
  c.globalCompositeOperation = 'destination-out'; const R = rng(seed);
  for (let i = 0; i < 900; i++) { c.globalAlpha = R() * .8; c.beginPath(); c.arc(R() * 760, R() * 240, R() * 3.2, 0, TAU); c.fill(); }
  return s;
}

// ---------- the crunchy 1-bit ordered dither (print/house look) ----------
const DITHER8 = (() => { const m = [[0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26], [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22], [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25], [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21]]; return m.map(r => r.map(v => (v + .5) / 64)); })();
const DCAN = {};
function dcan(px) { if (!DCAN[px]) { const cv = canvas(Math.ceil(W / px), Math.ceil(H / px)); DCAN[px] = { cv, c: ctx2d(cv) }; } return DCAN[px]; }
// accepts '#rrggbb' or 'rgba(...)' and returns [r,g,b]
export function colToRgb(col) {
  const s = String(col);
  if (s.startsWith('#')) return rgb(s);
  const p = s.match(/[\d.]+/g) || [0, 0, 0];
  return [Number(p[0]), Number(p[1]), Number(p[2])];
}
// Down-scale to 1/px, threshold to 1 bit with gamma, nearest-neighbour upscale.
// The accent heuristic keeps blue/violet pixels un-dithered. o: {dark, light, accent,
// gamma, lift} — colours come from the style (C.<name> values are hex strings).
export function dither1bit(c, px, o = {}) {
  const { cv, c: dc } = dcan(px), w = cv.width, h = cv.height;
  dc.save(); dc.globalCompositeOperation = 'copy'; dc.imageSmoothingEnabled = true; dc.drawImage(c.canvas, 0, 0, w, h); dc.restore();
  const id = dc.getImageData(0, 0, w, h), d = id.data, gam = o.gamma ?? .9, lift = o.lift ?? 0;
  const dpale = o.dark ? colToRgb(o.dark) : [8, 8, 10], lpale = o.light ? colToRgb(o.light) : [236, 234, 228], apale = o.accent ? colToRgb(o.accent) : [150, 112, 255];
  for (let y = 0; y < h; y++) { const row = DITHER8[y & 7]; for (let x = 0; x < w; x++) {
    const k = (y * w + x) * 4, r = d[k], g = d[k + 1], b = d[k + 2];
    const L = Math.pow((.299 * r + .587 * g + .114 * b) / 255, gam) + lift;
    const on = L > row[x & 7];
    const accent = b > 90 && b > r * 1.25 && b > g * 1.45;            // blue/violet survives as the accent
    const col = on ? (accent ? apale : lpale) : dpale;
    d[k] = col[0]; d[k + 1] = col[1]; d[k + 2] = col[2]; d[k + 3] = 255;
  } }
  dc.putImageData(id, 0, 0);
  c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.imageSmoothingEnabled = false; c.drawImage(cv, 0, 0, W, H); c.restore();
}

// ---------- datamosh / pixel-sort glitch: displaced slabs + smeared columns ----------
let GBUF = null;
export function glitch(c, amt, seed) {
  if (amt <= .01) return;
  if (!GBUF) GBUF = canvas();
  const gx = ctx2d(GBUF);
  gx.save(); gx.setTransform(1, 0, 0, 1, 0, 0); gx.clearRect(0, 0, W, H); gx.drawImage(c.canvas, 0, 0); gx.restore();
  c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.imageSmoothingEnabled = false;
  const R = rng(seed), n = Math.floor(3 + amt * 14);
  for (let i = 0; i < n; i++) { const y = R() * H, h2 = 8 + R() * 140 * amt, dx = (R() - .5) * 260 * amt; c.drawImage(GBUF, 0, y, W, h2, dx, y, W, h2); }
  const m = Math.floor(amt * 26);
  for (let i = 0; i < m; i++) { const x = Math.floor(R() * W), y = R() * H, len = 80 + R() * 700 * amt; c.globalAlpha = .85; c.drawImage(GBUF, x, y, 2 + R() * 6, 1, x, y, 2 + R() * 6, len); }
  c.restore();
}

const TAU = Math.PI * 2;
