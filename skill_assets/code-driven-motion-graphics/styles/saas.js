// Clean SaaS / product: light bg, flat cards with soft shadows, modern sans, subtle grain, smooth expo easing.
export const STYLE = {
  name: 'saas',
  C: { bg: '#f5f6f8', surface: '#ffffff', ink: '#14161a', muted: '#6b7280', line: '#e3e6ea', accent: '#4f46e5', accent2: '#0ea5e9', onAccent: '#ffffff' },
  fonts: { display: '"Inter Tight", "Helvetica Neue", Arial, sans-serif', sans: '"Inter", "Helvetica Neue", Arial, sans-serif', mono: '"JetBrains Mono", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Inter+Tight:wght@700;800&family=Inter:wght@400;600;700&family=JetBrains+Mono:wght@500&display=block',
  preload: ['800 100px "Inter Tight"', '600 100px "Inter"', '500 100px "JetBrains Mono"'],
  type: { displayWeight: 800, displayTrack: -.035, sansWeight: 500, sansTrack: -.01, monoWeight: 500, monoTrack: .08 },
  ease: 'expo',
  post: (ctx, t, b, fx) => { fx.vignette(ctx, .08); fx.grain(ctx, t, .03); },
};
