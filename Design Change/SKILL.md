---
name: accuretta-design
description: Use this skill to generate well-branded interfaces and assets for Accuretta, a local-first AI agent with a baked-in IDE. Contains design guidelines, colors, type, fonts, assets, and UI kit components for building prototypes, mocks, slides, or production UI.
user-invocable: true
---

Read the `README.md` file in this skill for the full brand + product context. Then explore:

- `colors_and_type.css` — all CSS variables (colors, type, spacing, radii, shadows, motion).
- `assets/` — logo + any static imagery. Copy these into your output; do not redraw them.
- `preview/` — small HTML cards demonstrating each design-system primitive. Useful as reference snippets.
- `ui_kits/desktop/` — React/JSX components for the desktop IDE surface.
- `ui_kits/mobile/` — React/JSX components for the mobile companion app.

## How to use

**For visual artifacts** (slides, mocks, throwaway prototypes, pitch decks):
1. Copy `colors_and_type.css` into your output and `@import` it.
2. Copy `assets/logo-mark.png` for any branded surface.
3. Load Phosphor icons via CDN: `<script src="https://unpkg.com/@phosphor-icons/web"></script>`.
4. Lift component patterns from `ui_kits/*` wherever useful.

**For production code:** read the rules in `README.md` and apply them. Never invent new colors, new typography, or custom SVG icons — the system is the system.

## House rules, always

- **Off-white (`#F6F4EF`) and off-black (`#141412`)** — never pure white or pure black.
- **Mono-forward.** JetBrains Mono is default; Space Mono for display; Inter Tight only for long-form prose.
- **Ember (`#E8813A`) is the accent** — use it sparingly, as a spark, not a wash.
- **Pastel candy palette** (mint, lilac, safety orange, lemon) for status and categorization only. Never as page backgrounds.
- **Phosphor icons only.** No emoji. No custom SVG icons (except the brand mark).
- **Voice:** lowercase-leaning, precise, no hype. If it wouldn't fit in a CHANGELOG, rewrite it.
- **No bluish-purple gradients, no hand-drawn illustrations, no grain, no colored left-borders.**

## If invoked with no guidance

Ask the user what they want to build (prototype, slide deck, marketing page, production component, etc.), ask 3–5 clarifying questions about audience and surface, then act as an expert designer producing HTML artifacts or production code. Always start by loading `colors_and_type.css` and the logo.
