// Minimal / luxury: negative space, thin wide-tracked type, slow expo easing, one accent, faint grain.
export const STYLE = {
  name: 'luxury',
  C: { bg: '#0f0e0c', surface: '#181613', ink: '#efe9dd', muted: '#9a9282', line: '#2c2924', accent: '#c9a96a', accent2: '#efe9dd', onAccent: '#0f0e0c' },
  fonts: { display: '"Cormorant Garamond", Georgia, serif', sans: '"Jost", "Helvetica Neue", Arial, sans-serif', mono: '"Jost", Arial, sans-serif' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@300;500&family=Jost:wght@300;400&display=block',
  preload: ['300 100px "Cormorant Garamond"', '400 100px "Jost"'],
  type: { displayWeight: 300, displayTrack: .08, sansWeight: 300, sansTrack: .04, monoWeight: 400, monoTrack: .3 },
  ease: 'expo',
  post: (ctx, t, b, fx) => { fx.vignette(ctx, .25); fx.grain(ctx, t, .035); },
};
