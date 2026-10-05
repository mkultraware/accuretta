// Data / HUD / tech: dark, monospace, corner ticks, telemetry readouts, green-cyan on black.
export const STYLE = {
  name: 'hud',
  C: { bg: '#04090a', surface: '#0a1416', ink: '#d8fff0', muted: '#5f8f86', line: '#12312f', accent: '#2dff9a', accent2: '#22d3ee', onAccent: '#04090a' },
  fonts: { display: '"Rajdhani", "Arial Narrow", sans-serif', sans: '"IBM Plex Mono", ui-monospace, monospace', mono: '"Share Tech Mono", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Rajdhani:wght@600;700&family=IBM+Plex+Mono:wght@400;500&family=Share+Tech+Mono&display=block',
  preload: ['700 100px "Rajdhani"', '500 100px "IBM Plex Mono"', '400 100px "Share Tech Mono"'],
  type: { displayWeight: 700, displayTrack: .04, sansWeight: 500, sansTrack: 0, monoWeight: 400, monoTrack: .12 },
  ease: 'out',
  post: (ctx, t, b, fx) => { fx.scanlines(ctx, .08, 3); fx.vignette(ctx, .3); fx.hud(ctx, t, b, { label: 'TELEMETRY' }); },
};
