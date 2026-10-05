// shots.js: the timeline. DEMO CONTENT: replace the shots, keep the patterns.
// Rules: read colours only from C, sizes from U/W/H, text through fit(). A shot is a pure function of (lb, t, b):
//   lb = beats since the shot started. It must hold its final state when lb grows past its length (transitions redraw it).
import { W, H, U, C, ease, clamp, inv, pulse, fill, flash, display, sans, mono, fit, measure, maskUp, typed, alpha } from './core.js';
import { card, pill, bar, row } from './ui.js';

export const SHOTS = [];
const shot = (a, b, name, draw, tin) => SHOTS.push({ a, b, name, draw, tin });
const at = (lb, start, len = 1) => inv(start, start + len, lb);   // 0..1 progress of something that starts at beat `start`
const cx = W / 2;

// 1. TITLE (beats 0-8): three words land on beats 1, 2, 3, tagline types out
shot(0, 8, 'title', (ctx, lb) => {
  fill(ctx, C.bg);
  const words = ['YOUR', 'PRODUCT', 'NAME'], size = fit(ctx, 'display', 'PRODUCT', W * .84, 15 * U), lh = size * 1.05;
  words.forEach((w, i) => {
    const y = H * .42 + (i - 1) * lh, p = at(lb, 1 + i, .6);
    maskUp(ctx, 0, y - size, W, size * 1.15, p, dy => display(ctx, w, cx, y + dy, size, { align: 'center', color: i === 1 ? C.accent : C.ink }));
  });
  const bw = W * .3 * ease.out(at(lb, 3.5, 1)); ctx.fillStyle = C.accent; ctx.fillRect(cx - bw / 2, H * .42 + lh * 1.25, bw, .6 * U);
  mono(ctx, typed('A ONE-LINE PROMISE.', at(lb, 4.5, 2.5)), cx, H * .42 + lh * 2.2, 2.4 * U, { align: 'center', color: C.muted });
  ctx.save(); ctx.translate(cx, H * .12); ctx.scale(1 + pulse(lb) * .25, 1 + pulse(lb) * .25);
  ctx.fillStyle = C.accent; ctx.fillRect(-1.2 * U, -1.2 * U, 2.4 * U, 2.4 * U); ctx.restore();
});

// 2. UI MOCKUP (8-16): a card rises, rows appear one per beat, a bar fills
shot(8, 16, 'ui', (ctx, lb) => {
  fill(ctx, C.bg);
  const w = W * .84, h = Math.min(H * .5, 70 * U), x = (W - w) / 2, rise = ease.expo(at(lb, 0, 1.2)), y = H * .5 - h / 2 + (1 - rise) * H * .3;
  ctx.save(); ctx.globalAlpha = rise; card(ctx, x, y, w, h);
  sans(ctx, 'Feature settings', x + 3.5 * U, y + 6 * U, 3.2 * U, { weight: 700 });
  const rows = [['Smart sync', 'runs in the background'], ['Offline mode', 'keeps a local copy'], ['Team sharing', 'invite by link'], ['Auto backup', 'every night']];
  rows.forEach(([a, s], i) => row(ctx, x + 3.5 * U, y + 14 * U + i * 8.6 * U, w - 7 * U, a, s, at(lb, 1.5 + i, 1)));
  bar(ctx, x + 3.5 * U, y + h - 6 * U, w - 7 * U, 1.2 * U, ease.io(at(lb, 2, 5)));
  ctx.restore();
  mono(ctx, typed('EVERYTHING IN ONE PLACE', at(lb, 1, 3)), cx, H * .14, 2.2 * U, { align: 'center', color: C.muted });
}, { type: 'wipe', dur: 1 });

// 3. STAT (16-24): the drop. Number counts up on the beat, chips pop
shot(16, 24, 'stat', (ctx, lb) => {
  fill(ctx, C.bg);
  const n = Math.round(98 * ease.out(at(lb, .25, 5))), s = fit(ctx, 'display', '98%', W * .8, 30 * U), k = 1 + pulse(lb) * .05;
  ctx.save(); ctx.translate(cx, H * .45); ctx.scale(k, k); display(ctx, n + '%', 0, 0, s, { align: 'center', color: C.accent }); ctx.restore();
  sans(ctx, 'of tasks done before lunch', cx, H * .45 + 6 * U, 3 * U, { align: 'center', color: C.muted });
  ['FAST', 'SIMPLE', 'YOURS'].forEach((c, i) => { const p = ease.back(at(lb, 2 + i * .5, .6)); pill(ctx, c, cx + (i - 1) * 24 * U, H * .62, { scale: p, alpha: clamp(p * 2), fill: i === 1 ? C.accent2 : C.accent }); });
  const f = 1 - inv(0, .5, lb); if (f > 0) flash(ctx, C.accent, f * .9);
}, { type: 'checker', dur: 1 });

// 4. OUTRO (24-32): closing line and call to action
shot(24, 32, 'outro', (ctx, lb) => {
  fill(ctx, C.bg);
  const s = fit(ctx, 'display', 'START TODAY', W * .84, 13 * U);
  maskUp(ctx, 0, H * .44 - s, W, s * 1.2, at(lb, .5, .8), dy => display(ctx, 'START TODAY', cx, H * .44 + dy, s, { align: 'center' }));
  pill(ctx, 'YOURPRODUCT.COM', cx, H * .56, { size: 2.6 * U, scale: ease.back(at(lb, 2, .8)) });
}, { type: 'fade', dur: 1 });
