// The notch's sound cues, for the main window too: one set of sounds across
// Accuretta. A copy of the Sound engine in accuretta-notch.html (that page
// stays byte-identical to its approved UI, so it keeps its own);
// tests/test_notch.py checks the two stay the same.
//
// Synthesized, never sampled: each cue is two soft, low mallet tones (a sine
// with a faint, quickly fading FM overtone for the strike, a sine an octave
// below for body), darkly low-passed and quiet, with a short room behind them.
// Every voice ramps up from silence and decays exponentially to -80 dB before
// it stops, so there is no click at the start and no hard cut at the end.
window.AccurettaSound = (() => {
  const N = { A3: 220, C4: 261.63, D4: 293.66, E4: 329.63, G4: 392, A4: 440, B4: 493.88, C5: 523.25, D5: 587.33, E5: 659.25 };
  const CUES = {
    open:    { gain: .26, dur: .55, notes: [['A4', 0], ['E5', .06]] },
    close:   { gain: .18, dur: .45, notes: [['E5', 0], ['A4', .05]] },
    gate:    { gain: .32, dur: .9,  notes: [['D4', 0], ['A4', .1]] },
    approve: { gain: .26, dur: .6,  notes: [['E4', 0], ['B4', .07]] },
    deny:    { gain: .24, dur: .55, notes: [['D4', 0], ['A3', .08]] },
    done:    { gain: .24, dur: .95, notes: [['G4', 0], ['D5', .09]] },
    error:   { gain: .28, dur: .75, notes: [['E4', 0], ['C4', .1]] },
    send:    { gain: .14, dur: .28, notes: [['A4', 0]] },
    drop:    { gain: .22, dur: .55, notes: [['G4', 0], ['C5', .07]] },
  };
  let ctx = null, bus = null, lastAt = 0;

  function room(sec) {
    const n = Math.floor(ctx.sampleRate * sec), buf = ctx.createBuffer(2, n, ctx.sampleRate);
    for (let ch = 0; ch < 2; ch++) {
      const d = buf.getChannelData(ch);
      for (let i = 0; i < n; i++) {
        const t = i / n, fadeIn = Math.min(1, i / (ctx.sampleRate * .004));
        d[i] = (Math.random() * 2 - 1) * Math.pow(1 - t, 3.4) * fadeIn;
      }
    }
    return buf;
  }
  function init() {
    if (ctx) return ctx;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    try { ctx = new AC(); } catch (e) { return null; }
    const lp = ctx.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 1900; lp.Q.value = .3;
    const comp = ctx.createDynamicsCompressor();
    comp.threshold.value = -20; comp.knee.value = 14; comp.ratio.value = 3; comp.attack.value = .005; comp.release.value = .3;
    const master = ctx.createGain(); master.gain.value = .13;
    const dry = ctx.createGain(); dry.gain.value = .86;
    const wet = ctx.createGain(); wet.gain.value = .15;
    const verb = ctx.createConvolver(); verb.buffer = room(1.5);
    lp.connect(dry); dry.connect(comp);
    lp.connect(verb); verb.connect(wet); wet.connect(comp);
    comp.connect(master); master.connect(ctx.destination);
    bus = lp;
    return ctx;
  }
  function voice(freq, t0, dur, gain, bright) {
    const car = ctx.createOscillator(); car.type = 'sine'; car.frequency.value = freq;
    const mod = ctx.createOscillator(); mod.type = 'sine'; mod.frequency.value = freq * 2;
    const depth = ctx.createGain();
    depth.gain.setValueAtTime(freq * .35 * bright, t0);
    depth.gain.exponentialRampToValueAtTime(freq * .005, t0 + .12);
    mod.connect(depth); depth.connect(car.frequency);
    const sub = ctx.createOscillator(); sub.type = 'sine'; sub.frequency.value = freq / 2;
    const subGain = ctx.createGain(); subGain.gain.value = .3;
    const env = ctx.createGain();
    env.gain.setValueAtTime(0, t0);
    env.gain.linearRampToValueAtTime(gain, t0 + .016);
    env.gain.exponentialRampToValueAtTime(gain * .32, t0 + .14);
    env.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    car.connect(env); sub.connect(subGain); subGain.connect(env); env.connect(bus);
    const stop = t0 + dur + .06;
    for (const o of [car, mod, sub]) { o.start(t0); o.stop(stop); }
  }
  function play(name) {
    const cue = CUES[name];
    if (!cue) return;
    const now = performance.now();
    if (now - lastAt < 180) return;         // one cue per gesture, never a pile-up
    lastAt = now;
    if (!init()) return;
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    const t0 = ctx.currentTime + .02;
    for (const [note, at, g] of cue.notes) voice(N[note], t0 + at, cue.dur, cue.gain * (g || 1), cue.bright || 1);
  }
  return { play, unlock: () => { if (init() && ctx.state === 'suspended') ctx.resume().catch(() => {}); } };
})();
