---
name: "code-driven-motion-graphics"
description: "Make a beat-synced promo, launch or explainer video where every frame is drawn in JavaScript on an HTML canvas, rendered headless in Chrome and encoded with ffmpeg. Works in any visual style: the look comes from a swappable style file. Use for product promos, kinetic typography, animated UI mockups, music-synced motion design."
budget: 1100
---

# Code-driven motion graphics

The template project lives in the workspace folder `code-driven-motion-graphics/`:
`engine/` (working project, copy it per video), `styles/` (one look per file),
`templates/STORYBOARD.md`, `docs/RULES.md`. If that folder is missing from the
workspace, copy it from this app installation's `skill_assets/code-driven-motion-graphics/`
first (the folder ships with the app; open the workspace root in a shell and copy).
New project folders belong next to it, inside the workspace.
The animation is deterministic: frame(t) is a pure function of time. Nothing opens on screen; you read frames as image files.

## Process (in order)
1. **Brief.** Settle: the one idea, mood, audience and platform, aspect and length, music, constraints. If unclear, offer 2 or 3 styles from `styles/` and let the user pick. Never default to one house style.
   - **Reference fidelity.** If the user gives reference links or material, treat them as the spec: analyze the reference's actual visual language (medium, composition, palette, type, pacing) and follow it to ~90% — the remaining 10% adapts it to the brief (product, copy, music). Match the MEDIUM, not just the motifs: photographic or engraved references need real public-domain imagery (Wikimedia Commons, museum open access) downloaded into `assets/` and composited; never silently swap the medium (photos → code-drawn line work, or the reverse). When an element cannot be matched (licensing, availability), substitute the closest equivalent and record the substitution in the storyboard. "Code-driven" is the rendering technique, not a ban on real images.
2. **Storyboard.** Read `docs/RULES.md`. Fill `templates/STORYBOARD.md` (shot table with beats, reads, the event, transition). Write no shot code before this exists.
3. **Music.** Run `node analyze.cjs assets/song.mp3`. It prints BPM, first beat and drop candidates. It is accurate to about 0.3%. Ask the user to confirm the drop second and check that the BPM feels right (half or double is common; rerun with `--bpm=`).
4. **Scaffold.** Copy `engine/` to a new folder. `cp styles/<name>.js src/style.js`. Run `npm install` (skip if the template already has `node_modules`). Edit `src/project.js` (size, BPM, DROP_SONG, DROP, TOTAL). Put the song in `assets/`.
5. **Build** `src/shots.js`, one shot per storyboard row. Add helpers to `src/ui.js`.
6. **Review loop** (below). Repeat until every panel passes.
7. **Render** the full video: `node render.mjs` (`--mb=8` for film-grade motion blur; heavy — try 4 first; `--shutter=1/60` to tune the blur window). The result is `out/<NAME>.mp4`.

## Code rules
- Time is in beats. A shot is `shot(a, b, name, (ctx, lb, t, b) => {...}, tin)`. `lb` = beats since the shot started. Shots must hold their last state when `lb` grows past their length.
- Colours only from `C.<name>` (bg, surface, ink, muted, line, accent, accent2, onAccent). No hex, no `rgba(` numbers: use `alpha(C.x, a)`. This keeps the style swappable.
- Rich helpers live in `src/motion.js`: keyframes `kf(x, [[beat,val],...])`, `spring`/`hit` impulses, `noise1`, camera `cam`/`shakeXY`, isometric `slabQ` + `slab` (perspective face with depth, window-lights via `win`, rim, fins, fog), `block` (brutalist tower), `vgrad`, `glowDot`, `haze`, `stars`, `planet`, `reticle`, `burst`, `polyR`/`quadAt`. Import them instead of rewriting physics/maths. Colours still come in through the options, from `C` (via `alpha`/`mix`).
- Post layers from `core.js`: worn type `stencil(str, size, o)` + `drawStencil(c, sp, x, y, o)` (cached sprite, shear/rotate/scale it), rubber `stamp(label, col, seed)` badge, 1-bit ordered `dither1bit(c, px, {dark, light, accent})` (crunchy print look), `glitch(c, amt, seed)` (datamosh).
- Per-shot extras: `dither` (px number), `ditherOpt`, `glitch(lb) -> 0..1` and `overlay(c, lb, t, b)` — a crisp layer drawn AFTER dither/glitch (dithered scene + clean text). Attach after the shot call: `SHOTS[i].dither = 3; SHOTS[i].overlay = (c, lb) => { ... };`
- Sizes from `U` (1% of the short side), `W`, `H`. Never fixed pixels. Every headline through `fit()`.
- No `Math.random`, `Date.now`, timers or stored state. Use `rng(seed)` or `hash(n)`.
- Text roles: `display`, `sans`, `mono`. Progress values go through `ease.*` or `ease0` (the style's default feel).
- Transition per shot: `tin: { type: 'cut'|'fade'|'wipe'|'push'|'checker'|'dip'|'flash'|'whip', dur: beats }` (`flash` takes optional `col`, `whip` takes `dir: -1|1`).
- After every edit run `node lint.mjs`. It finds syntax errors, hex literals and random calls.

## Review loop (headless)
```
node render.mjs --sheet=0,4,8,12,16,20 --cols=3 --w=400    # writes out/sheet.jpg
node render.mjs --stills=17,25                              # full-size frames in out/stills/
```
Open the image with your image-reading tool. For EVERY panel, first write what you see (text, colours, layout, what is moving), then judge it:
- Is all text fully inside the frame and readable? Any overlap?
- Is there one clear focal point? Does something change in this shot?
- Does this panel differ from its neighbours?
- Do colours match the style? Did the fonts load (no `FONT FALLBACK` line in the log)?
Fix only the shots that failed, then render only their beats again. Use 6 panels per sheet. Use a still to check small text. Do a full render last.
Sample transitions at `a+0.2`, `a+0.5`, `a+0.8` beats, and the drop beat +0.1.

## Notes
- Exit code 2 means the page logged errors (missing font, script error). Read them. Do not trust the output.
- Chrome is found automatically. Otherwise set `CHROME_PATH`. Fonts load from Google Fonts, so the first render needs internet.
- To change the look: replace `src/style.js` with another file from `styles/`. Shots stay unchanged. A new style copies an existing file and keeps the same `C` keys.
- `STYLE.cadence` (for example 12) makes shots animate "on twos". `STYLE.post` runs once per frame after all shots.
