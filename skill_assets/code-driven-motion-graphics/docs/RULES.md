# Rules that work in every style

These come from two finished projects (a launch film and a music video). They apply to any look.

## Plan before code
- Write the storyboard first (templates/STORYBOARD.md). Show it to the user if one is there. Build only after it exists.
- One world: one palette, one type system, one idea. The ending should echo the opening.
- Link the shots: motion or a shape carries across a cut, and screen direction stays the same.

## Every shot
- Something CHANGES between the first and last frame of a shot. A title that only sits there is not a shot.
- One focal point. The eye goes to what moves, what is bright, what is big. Get the eye to the right place before the important thing happens.
- Nothing is ever fully still: a slow push, a drift, a pulse on the beat, a grain shift.
- Hits land ON beats. Use `pulse(lb)` for a beat-locked kick.
- The drop lands on the strongest shot, and that shot starts on the drop beat.

## Timing: model the viewer
You know what happens because you wrote it. The viewer sees it once, at full speed.
- For each shot, write the "reads" in order: what the viewer must understand. Give each read time to find, understand, and register.
- One read at a time. Never start a new read while the last one is landing.
- Fast actions, slow meanings: a move can be quick, but what it means needs a hold. Rhythm is the contrast of quick and held.
- Text needs reading time. Rough guide: 0.3 s per word plus 0.5 s. At 120 BPM, one beat is 0.5 s.
- The last shot's final read needs time to land before the video ends.

## Transitions
- Every seam has a transition (set `tin` on the shot): `cut`, `fade`, `wipe`, `push`, `checker`. Do not use the same one everywhere.
- A plain `cut` is fine only on action or as a deliberate smash cut on the drop.
- Things that appear or leave inside a shot ease in and out. They never pop.

## Motion quality
- Never move linearly. Run every progress value through an easing.
- Anticipate: a small move the other way before a big move.
- Overlap: parts of one object start a little apart from each other.
- Avoid twinning: offset timing and seed between similar elements.
- Exaggerate. In a short video, subtle reads as nothing.

## Text and layout
- Every headline goes through `fit()` so it cannot run off the frame.
- Keep important content inside the safe area: at least 5% from every edge (about 5 * U).
- Text must contrast with its background. Never put muted text on an accent fill.
- No signs or captions that only repeat what the picture already shows.

## Common failures (check the sheet against this list)
- Text cut off or overlapping something
- A shot where nothing happens
- Everything at one brisk speed, events stacked on top of each other
- Two shots that look the same in the sheet
- Hard cuts everywhere, or a video that just starts and stops
- A reference was given, but the result swapped its medium or drifted from its look (photos re-drawn as line work, palette re-tinted) — fidelity to a supplied reference is a check, not a suggestion
- Hex colours or Math.random() inside shots (run `node lint.mjs`)
- Fonts fell back (the render prints FONT FALLBACK). The style is then wrong.
- The video ends without a closing frame that holds
