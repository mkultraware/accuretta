// Neon / synthwave: near-black, glowing accents, scanlines, chromatic edge, punchy on-beat hits.
export const STYLE = {
  name: 'neon',
  C: { bg: '#07050f', surface: '#120b25', ink: '#f4eeff', muted: '#8f7fc0', line: '#2b1f57', accent: '#ff2bd6', accent2: '#22e6ff', onAccent: '#07050f' },
  fonts: { display: '"Orbitron", "Arial Black", sans-serif', sans: '"Inter", Arial, sans-serif', mono: '"Space Mono", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Orbitron:wght@700;900&family=Inter:wght@500;700&family=Space+Mono:wght@400;700&display=block',
  preload: ['900 100px "Orbitron"', '700 100px "Inter"', '700 100px "Space Mono"'],
  type: { displayWeight: 900, displayTrack: .02, sansWeight: 600, sansTrack: 0, monoWeight: 700, monoTrack: .12 },
  ease: 'back',
  post: (ctx, t, b, fx) => { fx.scanlines(ctx, .14, 4); fx.chroma(ctx, 2); fx.vignette(ctx, .35); fx.grain(ctx, t, .04); },
};
