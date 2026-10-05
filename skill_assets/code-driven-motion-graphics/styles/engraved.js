// Engraved print / editorial: monochrome plus one accent, condensed serif, mono HUD, grain, on-twos cadence.
export const STYLE = {
  name: 'engraved',
  C: { bg: '#0d0c0f', surface: '#17151b', ink: '#ebe6da', muted: '#8f8c95', line: '#34323a', accent: '#c8a25a', accent2: '#e9a383', onAccent: '#0d0c0f' },
  fonts: { display: '"Noto Serif Display", "Times New Roman", serif', sans: '"Inter Tight", Arial, sans-serif', mono: '"IBM Plex Mono", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Noto+Serif+Display:wght@400;900&family=Inter+Tight:wght@400;600&family=IBM+Plex+Mono:wght@400;500&display=block',
  preload: ['900 100px "Noto Serif Display"', '600 100px "Inter Tight"', '500 100px "IBM Plex Mono"'],
  type: { displayWeight: 900, displayTrack: 0, displayScaleX: .82, sansWeight: 500, sansTrack: -.01, monoWeight: 500, monoTrack: .18 },
  ease: 'out',
  cadence: 12,   // shots animate "on twos/threes": the printed, hand-made rhythm
  post: (ctx, t, b, fx) => { fx.vignette(ctx, .3); fx.grain(ctx, t, .09); fx.hud(ctx, t, b); },
};
