// Retro / VHS: warm faded palette, scanlines, colour fringe, timestamp HUD, grain.
export const STYLE = {
  name: 'vhs',
  C: { bg: '#1b1410', surface: '#2a1f18', ink: '#f0dfc0', muted: '#a58f6d', line: '#4a3a2c', accent: '#ff7a3c', accent2: '#4fd1c5', onAccent: '#1b1410' },
  fonts: { display: '"VT323", ui-monospace, monospace', sans: '"Share Tech Mono", ui-monospace, monospace', mono: '"VT323", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=VT323&family=Share+Tech+Mono&display=block',
  preload: ['400 100px "VT323"', '400 100px "Share Tech Mono"'],
  type: { displayWeight: 400, displayTrack: .02, sansWeight: 400, sansTrack: .02, monoWeight: 400, monoTrack: .05 },
  ease: 'out',
  post: (ctx, t, b, fx) => { fx.scanlines(ctx, .18, 4); fx.chroma(ctx, 3); fx.grain(ctx, t, .12); fx.vignette(ctx, .4); fx.hud(ctx, t, b, { label: 'PLAY' }); },
};
