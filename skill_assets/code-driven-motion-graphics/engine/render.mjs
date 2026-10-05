// render.mjs: headless, frame-perfect render. Nothing opens on screen.
//   node render.mjs                              full video -> out/<name>.mp4 (+ _silent.mp4)
//   node render.mjs --sheet=0,4,8,12,16,20       contact sheet -> out/sheet.jpg  (options: --cols=3 --w=400)
//   node render.mjs --stills=2,16.5              full-size frames -> out/stills/beat_<n>.jpg
//   node render.mjs --from=8 --to=16 --out=out/part.mp4   partial render (beats)
//   options: --workers=4 --chrome=<path> --song=<file> --mb=8 (motion-blur samples: heavy, try 4 first)
//            --shutter=1/60 (seconds per blur window, ratiors ok)   (or env CHROME_PATH)
import puppeteer from 'puppeteer-core';
import { spawn } from 'node:child_process';
import { writeFileSync, mkdirSync, existsSync, rmSync, readdirSync, renameSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { resolve, extname, join, sep } from 'node:path';

const args = Object.fromEntries(process.argv.slice(2).map(a => { const [k, ...v] = a.replace(/^--/, '').split('='); return [k, v.length ? v.join('=') : true]; }));
const candidates = [args.chrome, process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/google-chrome', '/usr/bin/google-chrome-stable', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/opt/google/chrome/chrome'].filter(Boolean);
const exe = candidates.find(p => existsSync(p));
if (!exe) { console.error('No Chrome/Edge found. Set CHROME_PATH or pass --chrome=<path>.'); process.exit(1); }

const workers = +(args.workers || 4);
const run = a => new Promise((ok, bad) => spawn('ffmpeg', a, { stdio: ['ignore', 'inherit', 'inherit'] }).on('error', bad).on('close', c => (c ? bad(new Error('ffmpeg exit ' + c)) : ok())));
const b64 = u => Buffer.from(u.slice(u.indexOf(',') + 1), 'base64');

// tiny static server: ES modules do not load from file://
const root = resolve('.');
const types = { '.html': 'text/html', '.js': 'text/javascript', '.json': 'application/json', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp', '.svg': 'image/svg+xml', '.mp3': 'audio/mpeg', '.woff2': 'font/woff2' };
const srv = createServer(async (q, r) => {
  const u = decodeURIComponent(q.url.split('?')[0]), p = resolve(join(root, u === '/' ? 'index.html' : u));
  if (p !== root && !p.startsWith(root + sep)) { r.statusCode = 403; return r.end(); }
  try { r.setHeader('content-type', types[extname(p)] || 'application/octet-stream'); r.end(await readFile(p)); } catch { r.statusCode = 404; r.end(); }
}).listen(0);
const port = srv.address().port;

let browser, exitCode = 0;
// --mb=8 style: motion-blur samples per frame. A ratio like 1/60 is parsed here;
// blur sheets/stills only happen when the flag is given.
const parseRatio = v => { const s = String(v ?? ''); const [a, b] = s.split('/').map(Number); return b ? a / b : Number(a); };
const MB = +(args.mb || 1), SHUTTER = args.shutter ? parseRatio(args.shutter) : 1 / 60;
try {
  browser = await puppeteer.launch({
    executablePath: exe, headless: true, protocolTimeout: 0,
    args: ['--ignore-gpu-blocklist', '--disable-renderer-backgrounding', '--disable-background-timer-throttling', ...(process.getuid?.() === 0 ? ['--no-sandbox'] : [])],
  });
  let problems = 0;
  const openPage = async () => {
    const page = await browser.newPage(); await page.setViewport({ width: 540, height: 960 });
    page.on('pageerror', e => { problems++; console.log('[page error]', e.message); });
    page.on('console', m => { if (m.type() === 'error') { problems++; console.log('[page]', m.text()); } });
    await page.goto(`http://localhost:${port}/?render`, { waitUntil: 'networkidle0' });
    await page.waitForFunction('window.ready === true', { timeout: 60000 });
    return page;
  };
  const main = await openPage(); const info = await main.evaluate(() => window.info);
  mkdirSync('out', { recursive: true });
  const fps = info.fps;

  if (args.sheet) {
    const beats = String(args.sheet).split(',').map(Number), cols = +(args.cols || 3), dir = 'out/_sheet';
    rmSync(dir, { recursive: true, force: true }); mkdirSync(dir, { recursive: true });
    const t0 = Date.now();
    for (let k = 0; k < beats.length; k++) writeFileSync(`${dir}/${String(k).padStart(3, '0')}.jpg`, b64(await main.evaluate((t, s) => window.frameURL(t, .85, s), beats[k] * info.B, MB)));
    const ms = (Date.now() - t0) / beats.length;
    await run(['-y', '-loglevel', 'error', '-i', `${dir}/%03d.jpg`, '-vf', `scale=${args.w || 400}:-1,tile=${cols}x${Math.ceil(beats.length / cols)}`, '-frames:v', '1', args.out || 'out/sheet.jpg']);
    console.log(`wrote ${args.out || 'out/sheet.jpg'} (${beats.length} panels, ${ms.toFixed(0)} ms/frame). Panel order: ${beats.join(', ')}`);
    if (ms > 2500) console.log('WARNING: frames are slow (>2.5 s). Reduce full-frame pixel effects or shape counts.');
  } else if (args.stills) {
    const beats = String(args.stills).split(',').map(Number); mkdirSync('out/stills', { recursive: true });
    for (const bt of beats) { const f = `out/stills/beat_${bt}.jpg`; writeFileSync(f, b64(await main.evaluate((t, s) => window.frameURL(t, .92, s), bt * info.B, MB))); console.log('wrote', f); }
  } else {
    const t0 = (+args.from || 0) * info.B, t1 = args.to ? +args.to * info.B : info.total;
    const n = Math.round((t1 - t0) * fps), final = args.out || `out/${info.name}.mp4`, silent = final.replace(/\.mp4$/, '_silent.mp4');
    console.log(`${n} frames, ${workers} workers, ${MB} motion-blur sample(s)`);
    const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(fps), '-c:v', 'mjpeg', '-i', '-', '-c:v', 'libx264', '-preset', 'slow', '-crf', '15', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', silent], { stdio: ['pipe', 'inherit', 'inherit'] });
    const ffDone = new Promise((ok, bad) => { ff.on('error', bad); ff.on('close', c => (c ? bad(new Error('ffmpeg exit ' + c)) : ok())); });
    ff.stdin.on('error', () => {});
    const pages = [main]; for (let w = 1; w < workers; w++) pages.push(await openPage());
    const done = new Map(); let next = 0, written = 0; const start = Date.now();
    const flush = async () => {
      while (done.has(written)) {
        const buf = done.get(written); done.delete(written);
        if (!ff.stdin.write(buf)) await new Promise(r => ff.stdin.once('drain', r));
        written++; if (written % 60 === 0) console.log(`${written}/${n}  ${((Date.now() - start) / written).toFixed(0)} ms/f`);
      }
    };
    await Promise.all(pages.map(async page => {
      while (next < n) {
        const i = next++;
        while (done.size > workers * 6) await new Promise(r => setTimeout(r, 15));
        done.set(i, b64(await page.evaluate((t, s, sh) => window.frameURL(t, .95, s, sh), t0 + i / fps, MB, SHUTTER)));
        await flush();
      }
    }));
    await flush(); ff.stdin.end(); await ffDone;

    const dur = t1 - t0, ss = info.songStart + t0;
    const song = args.song || (existsSync('assets') && readdirSync('assets').map(f => 'assets/' + f).find(f => /\.(mp3|wav|m4a|ogg)$/i.test(f)));
    if (song && existsSync(song)) {
      const lead = ss < 0 ? [`adelay=${Math.round(-ss * 1000)}:all=1`] : [];
      const af = [...lead, 'afade=t=in:d=0.04', `afade=t=out:st=${Math.max(0, dur - 1.4).toFixed(2)}:d=1.4`].join(',');
      await run(['-y', '-loglevel', 'error', '-i', silent, ...(ss > 0 ? ['-ss', ss.toFixed(4)] : []), '-i', song, '-map', '0:v', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '256k', '-af', af, '-t', dur.toFixed(3), '-movflags', '+faststart', final]);
      console.log('wrote', final, 'and', silent);
    } else { renameSync(silent, final); console.log('no song found in assets/, wrote silent video', final); }
  }
  if (problems) { console.log(`\n${problems} page problem(s) logged above. Fix them before you trust the output.`); exitCode = 2; }
} catch (e) { console.error(e); exitCode = 1; }
finally { if (browser) await browser.close().catch(() => {}); srv.close(); }
process.exit(exitCode);
