// analyze.cjs: find BPM, first-beat phase and drop candidates from an audio file.
//   node analyze.cjs assets/song.mp3 [--min=80] [--max=160] [--bpm=114.3] [--around=74.5]
// The model cannot hear audio. This script turns it into numbers; a human or the model then reads them.
const { execFileSync } = require('child_process');
const file = process.argv[2];
if (!file) { console.error('usage: node analyze.cjs <audio> [--min=80 --max=160 --bpm=X --around=seconds]'); process.exit(1); }
const opt = Object.fromEntries(process.argv.slice(3).map(a => { const [k, v] = a.replace(/^--/, '').split('='); return [k, +v]; }));
const raw = execFileSync('ffmpeg', ['-v', 'error', '-i', file, '-ac', '1', '-ar', '11025', '-f', 'f32le', '-'], { maxBuffer: 1 << 30 });
const x = new Float32Array(raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.length - (raw.length % 4)));
const FR = 100, hop = 110, n = Math.floor(x.length / hop), dur = n / FR;   // 100 frames per second
const e = new Float32Array(n);
for (let i = 0; i < n; i++) { let s = 0; for (let j = 0; j < hop; j++) { const v = x[i * hop + j]; s += v * v; } e[i] = Math.sqrt(s / hop); }
const f = new Float32Array(n); for (let i = 1; i < n; i++) f[i] = Math.max(0, e[i] - e[i - 1]);   // onset strength
const at = p => { const i = Math.floor(p), k = p - i; return i + 1 < n && i >= 0 ? f[i] * (1 - k) + f[i + 1] * k : 0; };

// tempo: autocorrelation of the onset signal at the beat lag (plus the double lag)
const lo = opt.min || 80, hi = opt.max || 160, cand = [];
for (let bpm = lo; bpm <= hi; bpm += 0.05) {
  const lag = 6000 / bpm; let s = 0;
  for (let i = 0; i + lag * 2 < n; i++) s += f[i] * (at(i + lag) + .5 * at(i + 2 * lag));
  cand.push([bpm, s]);
}
const peaks = cand.filter((c, i) => i > 0 && i < cand.length - 1 && c[1] >= cand[i - 1][1] && c[1] > cand[i + 1][1]).sort((a, b) => b[1] - a[1]);
const top = []; for (const p of peaks) if (top.every(q => Math.abs(q[0] - p[0]) > 3)) top.push(p); top.length = Math.min(3, top.length);
// refine: search tempo (+-2 BPM, 0.01 steps) and phase together. Beats far into the song only line up at the true tempo.
function comb(bp, ph) { const per = 60 / bp; let s = 0; for (let t = ph; t < dur - .1; t += per) s += at(t * FR); return s; }
let bpm = opt.bpm || top[0][0], bp = 0, bs = -1;
const from = opt.bpm ? bpm : bpm - 2, to = opt.bpm ? bpm : bpm + 2;
for (let b = from; b <= to + 1e-9; b += 0.01) { const per = 60 / b; for (let ph = 0; ph < per; ph += 0.005) { const s = comb(b, ph); if (s > bs) { bs = s; bpm = b; bp = ph; } } }
const per = 60 / bpm;
console.log(`length ${dur.toFixed(1)} s`);
console.log('tempo candidates:', top.map(p => `${p[0].toFixed(2)} (score ${(p[1] / top[0][1] * 100).toFixed(0)}%)`).join('  |  '));
console.log(`refined BPM ${bpm.toFixed(2)}. If it feels twice or half as fast when you tap along, run again with --bpm=${(bpm * 2).toFixed(2)} or --bpm=${(bpm / 2).toFixed(2)}`);
console.log(`first beat at ${bp.toFixed(3)} s   (beat k is at ${bp.toFixed(3)} + k * ${per.toFixed(4)} s)`);

// loudness per beat, then find the biggest jump: mean of next 8 beats vs previous 8 beats
const nb = Math.floor((dur - bp) / per), m = [];
for (let k = 0; k < nb; k++) { let s = 0, c = 0; for (let i = Math.round((bp + k * per) * FR); i < Math.round((bp + (k + 1) * per) * FR) && i < n; i++) { s += e[i]; c++; } m.push(c ? s / c : 0); }
const avg = (a, b) => { let s = 0, c = 0; for (let i = Math.max(0, a); i < Math.min(m.length, b); i++) { s += m[i]; c++; } return c ? s / c : 0; };
const jumps = []; for (let k = 8; k < nb - 8; k++) jumps.push([k, avg(k, k + 8) / (avg(k - 8, k) + 1e-6)]);
jumps.sort((a, b) => b[1] - a[1]); const drops = []; for (const j of jumps) if (drops.every(d => Math.abs(d[0] - j[0]) > 16)) drops.push(j); drops.length = Math.min(3, drops.length);
console.log('\ndrop candidates (energy jump ratio, beat index, song second):');
drops.forEach(d => console.log(`  x${d[1].toFixed(2)}  beat ${d[0]}  at ${(bp + d[0] * per).toFixed(2)} s`));
let line = '\nloudness per 2 s:  '; for (let t = 0; t < dur; t += 2) { let s = 0, c = 0; for (let i = t * FR; i < (t + 2) * FR && i < n; i++) { s += e[i]; c++; } line += `${t}:${(s / c * 100).toFixed(0)} `; } console.log(line);
const c0 = opt.around != null ? Math.round((opt.around - bp) / per) : drops[0] ? drops[0][0] : 0;
let tb = '\nbeats around the target:  ';
for (let k = Math.max(0, c0 - 6); k < Math.min(nb, c0 + 7); k++) tb += `${k}@${(bp + k * per).toFixed(2)}s:${(m[k] * 100).toFixed(0)}  `;
console.log(tb);
console.log('\nPut in src/project.js: BPM = ' + bpm.toFixed(2) + ';  DROP_SONG = <the drop second you choose>;  DROP = <edit beat where it lands>.');
console.log('SONG_START = DROP_SONG - DROP * 60/BPM is computed for you in core.js.');
