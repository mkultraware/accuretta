// ui.js: code-drawn UI pieces for mockups. Colours come from C only, sizes from U (aspect-agnostic).
import { C, U, clamp, ease, alpha, sans, mono, measure } from './core.js';

export const rr = (ctx, x, y, w, h, r) => { ctx.beginPath(); ctx.roundRect(x, y, w, h, r); };

export function card(ctx, x, y, w, h, o = {}) {
  ctx.save();
  if (o.shadow !== false) { ctx.shadowColor = 'rgba(0,0,0,.35)'; ctx.shadowBlur = 5 * U; ctx.shadowOffsetY = 1.8 * U; }
  rr(ctx, x, y, w, h, o.r ?? 2.4 * U); ctx.fillStyle = o.fill || C.surface; ctx.fill();
  ctx.shadowColor = 'transparent'; ctx.lineWidth = Math.max(1.5, U * .16); ctx.strokeStyle = o.stroke || C.line; ctx.stroke();
  ctx.restore();
}
export function pill(ctx, label, cx, cy, o = {}) {
  const s = o.size ?? 2.1 * U, pad = s * .9, w = measure(ctx, 'mono', label, s) + pad * 2, h = s * 2.1;
  ctx.save(); ctx.translate(cx, cy); ctx.scale(o.scale ?? 1, o.scale ?? 1); ctx.globalAlpha *= o.alpha ?? 1;
  rr(ctx, -w / 2, -h / 2, w, h, h / 2); ctx.fillStyle = o.fill || C.accent; ctx.fill();
  mono(ctx, label, 0, s * .36, s, { align: 'center', color: o.color || C.onAccent }); ctx.restore(); return w;
}
export function bar(ctx, x, y, w, h, p, o = {}) {
  rr(ctx, x, y, w, h, h / 2); ctx.fillStyle = o.track || C.line; ctx.fill();
  if (p > 0) { rr(ctx, x, y, Math.max(h, w * clamp(p)), h, h / 2); ctx.fillStyle = o.fill || C.accent; ctx.fill(); }
}
export function toggle(ctx, x, y, w, p) {
  const h = w * .55; rr(ctx, x, y, w, h, h / 2); ctx.fillStyle = p > .5 ? C.accent : C.line; ctx.fill();
  ctx.beginPath(); ctx.arc(x + h / 2 + (w - h) * clamp(p), y + h / 2, h * .38, 0, 7); ctx.fillStyle = p > .5 ? C.onAccent : C.muted; ctx.fill();
}
// a list row: label left, toggle right. p = 0..1 reveal
export function row(ctx, x, y, w, label, sub, p) {
  const a = ease.out(clamp(p)); if (a <= 0) return;
  ctx.save(); ctx.globalAlpha *= a; ctx.translate(0, (1 - a) * 2 * U);
  sans(ctx, label, x, y, 2.6 * U, { weight: 600 }); mono(ctx, sub, x, y + 3.2 * U, 1.6 * U, { color: C.muted });
  toggle(ctx, x + w - 8 * U, y - 2.4 * U, 8 * U, clamp((p - .4) * 2.5));
  ctx.strokeStyle = C.line; ctx.lineWidth = Math.max(1, U * .12); ctx.beginPath(); ctx.moveTo(x, y + 5.2 * U); ctx.lineTo(x + w, y + 5.2 * U); ctx.stroke();
  ctx.restore();
}
