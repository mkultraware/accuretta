// lint.mjs: node lint.mjs  -> catches the mistakes that break style swaps and frame determinism.
import { readFileSync, readdirSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
let bad = 0;
const say = (f, n, msg) => { bad++; console.log(`${f}:${n}  ${msg}`); };
for (const f of readdirSync('src').filter(f => f.endsWith('.js')).map(f => 'src/' + f)) {
  const r = spawnSync(process.execPath, ['--check', f], { encoding: 'utf8' });
  if (r.status) say(f, 0, 'SYNTAX ERROR (file may be truncated)\n' + r.stderr.split('\n').slice(0, 4).join('\n'));
}
for (const f of ['src/shots.js', 'src/ui.js']) {
  readFileSync(f, 'utf8').split('\n').forEach((line, i) => {
    const code = line.replace(/\/\/.*$/, '');
    if (/#[0-9a-fA-F]{3,8}\b/.test(code)) say(f, i + 1, 'hex colour literal: use C.<name>');
    if (/rgba?\(\s*\d/.test(code) && !/shadow/i.test(code)) say(f, i + 1, 'rgb()/rgba() literal: use alpha(C.<name>, a)');
    if (/Math\.random|Date\.now|performance\.now/.test(code)) say(f, i + 1, 'non-deterministic call: use rng(seed) or hash(n)');
    if (/(localStorage|setTimeout|setInterval|requestAnimationFrame)/.test(code)) say(f, i + 1, 'hidden state or timers: frames must be pure functions of t');
  });
}
console.log(bad ? `\n${bad} problem(s)` : 'lint ok'); process.exit(bad ? 1 : 0);
