// Playful / pop: saturated colour, chunky rounded type, bouncy easing. Add shapes and particles in shots.
export const STYLE = {
  name: 'playful',
  C: { bg: '#ffe9a8', surface: '#ffffff', ink: '#2a1a5e', muted: '#7a5fc0', line: '#f2c94c', accent: '#ff5d8f', accent2: '#2ec4b6', onAccent: '#ffffff' },
  fonts: { display: '"Fredoka", "Arial Rounded MT Bold", sans-serif', sans: '"Nunito", Arial, sans-serif', mono: '"Fredoka", sans-serif' },
  fontsHref: 'https://fonts.googleapis.com/css2?family=Fredoka:wght@500;700&family=Nunito:wght@600;800&display=block',
  preload: ['700 100px "Fredoka"', '800 100px "Nunito"'],
  type: { displayWeight: 700, displayTrack: -.01, sansWeight: 800, sansTrack: 0, monoWeight: 600, monoTrack: .06 },
  ease: 'back',
  post: () => {},
};
