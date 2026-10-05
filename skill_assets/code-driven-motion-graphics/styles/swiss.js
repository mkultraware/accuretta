// Swiss / kinetic type: strict two-colour contrast, heavy grotesque, almost no effects. Type motion is the hero.
export const STYLE = {
  name: 'swiss',
  C: { bg: '#f2f0ea', surface: '#ffffff', ink: '#0b0b0b', muted: '#5c5c58', line: '#cfcdc5', accent: '#e8321c', accent2: '#0b0b0b', onAccent: '#ffffff' },
  fonts: { display: '"Archivo", "Helvetica Neue", Arial, sans-serif', sans: '"Archivo", "Helvetica Neue", Arial, sans-serif', mono: '"IBM Plex Mono", ui-monospace, monospace' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Archivo:wght@500;700;900&family=IBM+Plex+Mono:wght@500&display=block',
  preload: ['900 100px "Archivo"', '500 100px "Archivo"', '500 100px "IBM Plex Mono"'],
  type: { displayWeight: 900, displayTrack: -.05, sansWeight: 700, sansTrack: -.02, monoWeight: 500, monoTrack: .06 },
  ease: 'expo',
  post: () => {},
};
