// main.js: compose frame(t) = scene -> optional 1-bit dither -> glitch -> crisp
// overlay layer -> style post -> fade-out, with optional motion-blur sampling.
// Preview with the song; exposes hooks for render.mjs. Rarely needs editing.
import { NAME, W, H, FPS, B, TOTAL, SONG_START, C, STYLE, canvas, ctx2d, loadFonts, initGrain, FX, ease, clamp, rng, fill, alpha, dither1bit, glitch } from './core.js';
import { SHOTS } from './shots.js';

const out = document.getElementById('c'); out.width = W; out.height = H; out.style.aspectRatio = `${W} / ${H}`;
const octx = ctx2d(out), pbuf = canvas(), pctx = ctx2d(pbuf), cbuf = canvas(), cctx = ctx2d(cbuf), abuf = canvas(), abx = ctx2d(abuf);
const total = TOTAL * B;

SHOTS.sort((a, b) => a.a - b.a);
SHOTS.forEach((s, i) => {
  if (!(s.b > s.a)) console.error(`SHOT ${i} (${s.name}) has b <= a`);
  if (i > 0 && s.a !== SHOTS[i - 1].b) console.error(`GAP OR OVERLAP between shot ${i - 1} and ${i} (${SHOTS[i - 1].b} vs ${s.a})`);
});
if (SHOTS[0].a !== 0) console.error('FIRST SHOT DOES NOT START AT BEAT 0');
if (SHOTS[SHOTS.length - 1].b !== TOTAL) console.error(`LAST SHOT ENDS AT ${SHOTS[SHOTS.length - 1].b}, TOTAL IS ${TOTAL}`);

// time seen by shots: optionally snapped ("on twos") when the style sets a cadence
const snap = t => (STYLE.cadence ? Math.floor(t * STYLE.cadence) / STYLE.cadence : t);

function where(t) {
  const b = t / B;
  let i = SHOTS.findIndex(s => b >= s.a && b < s.b); if (i < 0) i = SHOTS.length - 1;
  return { i, s: SHOTS[i], b };
}
function drawShotAt(ctx, i, t) {
  const s = SHOTS[i], ts = snap(t), b = ts / B;
  ctx.save(); ctx.clearRect(0, 0, W, H); s.draw(ctx, b - s.a, ts, b); ctx.restore();
}

// the shot at time t, rewritten into ctx, together with its incoming transition
function scene(ctx, t) {
  const { i, b } = where(t);
  const s = SHOTS[i], tin = s.tin, inT = tin && tin.type !== 'cut' && i > 0 && b - s.a < tin.dur;
  const p = inT ? (b - s.a) / tin.dur : 0, e0 = ease.io(p);
  const drawPrev = () => drawShotAt(pctx, i - 1, t);
  if (inT && tin.type === 'whip') {
    const dx = (tin.dir || -1) * W;
    drawPrev();
    ctx.save(); ctx.translate(dx * e0, 0); ctx.drawImage(pbuf, 0, 0); ctx.restore();
    ctx.save(); ctx.translate(-dx * (1 - e0), 0); drawShotAt(ctx, i, t); ctx.restore();
    return;
  }
  drawShotAt(ctx, i, t);
  if (!inT) return;
  drawPrev();
  if (tin.type === 'fade') {
    ctx.save(); ctx.globalAlpha = 1 - e0; ctx.drawImage(pbuf, 0, 0); ctx.restore();
  } else if (tin.type === 'wipe') {
    const x = e0 * W; ctx.save(); ctx.beginPath(); ctx.rect(x, 0, W - x, H); ctx.clip(); ctx.drawImage(pbuf, 0, 0); ctx.restore();
    ctx.fillStyle = C.accent; ctx.fillRect(x - W * .006, 0, W * .012, H);
  } else if (tin.type === 'push') {
    // blur-safe: current goes straight into the swap buffer, no readout of `out`
    drawShotAt(cctx, i, t);
    ctx.drawImage(pbuf, 0, -e0 * H); ctx.drawImage(cbuf, 0, (1 - e0) * H);
  } else if (tin.type === 'checker') {
    const cols = 6, rows = Math.max(4, Math.round(6 * H / W)), cw = W / cols, ch = H / rows, r = rng(s.tin.seed || 7);
    for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
      const order = ((x + y) % 2) * .45 + r() * .5;
      if (p < order) ctx.drawImage(pbuf, x * cw, y * ch, cw, ch, x * cw, y * ch, cw, ch);
      else if (p < order + .1) { ctx.fillStyle = C.accent; ctx.fillRect(x * cw, y * ch, cw + 1, ch + 1); }
    }
  } else if (tin.type === 'dip') {
    const a = p < .5 ? ease.io(p * 2) : 1 - ease.io((p - .5) * 2);
    ctx.fillStyle = `rgba(0,0,0,${a})`; ctx.fillRect(0, 0, W, H);
  } else if (tin.type === 'flash') {
    const a = Math.pow(1 - p, 3);
    ctx.fillStyle = tintAlpha(tin.col || C.bg, a); ctx.fillRect(0, 0, W, H);
  }
}
// accepts '#rrggbb' or 'rgba(...)' and returns it with a new alpha
function tintAlpha(col, a) {
  const s = String(col);
  if (s.startsWith('#')) return alpha(s, a);
  const m = s.match(/[\d.]+/g) || [255, 255, 255];
  return `rgba(${m[0]},${m[1]},${m[2]},${a})`;
}

// one output frame. samples > 1 = true motion blur (accumulate whole scenes over
// the shutter window). render.mjs drives this with --mb=N --shutter=s.
export function frame(t, samples = 1, shutter = 1 / 60) {
  const { i, s, b } = where(t), lb = b - s.a;
  octx.save(); octx.setTransform(1, 0, 0, 1, 0, 0); octx.globalAlpha = 1; octx.globalCompositeOperation = 'source-over'; octx.clearRect(0, 0, W, H); octx.restore();
  if (samples > 1) {
    const acc = samples;
    for (let k = 0; k < samples; k++) {
      scene(abx, t + ((k + .5) / acc - .5) * shutter);
      octx.save(); octx.globalAlpha = 1 / (k + 1); octx.drawImage(abuf, 0, 0); octx.restore();
    }
  } else {
    scene(octx, t);
  }
  // deterministic post layers, driven by the shot itself
  if (s.dither) dither1bit(octx, s.dither, s.ditherOpt || {});
  if (s.glitch) glitch(octx, s.glitch(lb, b), Math.floor(t * 24) * 31 + i);
  // crisp layer after the crunchy post layers (per-shot overlay)
  if (s.overlay) { octx.save(); s.overlay(octx, lb, snap(t), b); octx.restore(); }
  if (STYLE.post) STYLE.post(octx, t, b, FX);
  const fo = clamp((b - (TOTAL - .5)) / .5);
  if (fo > 0) { octx.fillStyle = alpha(C.bg, fo); octx.fillRect(0, 0, W, H); }
}

await loadFonts(); initGrain();
window.frameURL = (t, q = .93, samples = 1, shutter = 1 / 60) => { frame(t, samples, shutter); return out.toDataURL('image/jpeg', q); };
window.info = { name: NAME, total, songStart: SONG_START, B, fps: FPS, W, H, shots: SHOTS.map(s => ({ a: s.a, b: s.b, name: s.name })) };
window.ready = true;

if (!location.search.includes('render')) {
  const audio = new Audio('assets/song.mp3'); let playing = false, tStart = 0;
  const ui = document.getElementById('ui'), scrub = document.getElementById('scrub'), lab = document.getElementById('lab');
  scrub.max = total; scrub.step = 1 / FPS;
  const show = t => { frame(t); scrub.value = t; lab.textContent = `${t.toFixed(2)}s, beat ${(t / B).toFixed(2)}`; };
  const seek = t => { tStart = t; if (playing) audio.currentTime = Math.max(0, SONG_START + t); show(t); };
  scrub.oninput = () => seek(+scrub.value);
  const toggle = () => { playing = !playing; if (playing) { audio.currentTime = Math.max(0, SONG_START + tStart); audio.play().catch(() => {}); } else { audio.pause(); tStart = audio.currentTime - SONG_START; } };
  document.getElementById('play').onclick = toggle;
  addEventListener('keydown', e => {
    if (e.code === 'Space') { e.preventDefault(); toggle(); }
    if (e.key === 'h') ui.classList.toggle('hidden');
    if (e.key === 'ArrowRight') seek(Math.min(total, tStart + B));
    if (e.key === 'ArrowLeft') seek(Math.max(0, tStart - B));
  });
  const loop = () => { if (playing) { let t = audio.currentTime - SONG_START; if (t >= total) { playing = false; audio.pause(); t = 0; } tStart = t; show(t); } requestAnimationFrame(loop); };
  const q = new URLSearchParams(location.search); seek(q.has('b') ? +q.get('b') * B : 0); loop();
}
