# Accuretta Design System

> Local AI, in the spirit of OpenClaw / Hermes Agent / Claude Computer — with a baked-in IDE that **prerenders HTML** for you. Runs on your machine. Your keys, your models, your files.

This design system is the source of truth for Accuretta's visual and interaction language across desktop app, mobile app, and marketing surfaces.

---

## 1. Product context

Accuretta is a **local-first AI agent platform** with an integrated IDE surface. The agent can see your screen, read your files, and drive a live HTML prerender pane — so instead of staring at a stream of tokens, you see the interface materialize as the model thinks.

Key product beliefs, reflected in the design:
- **Local by default.** No telemetry, no round-trips. The UI should always make "where is this running?" answerable at a glance.
- **Technical audience.** Engineers, tinkerers, prosumer users. They reward precision; they punish pretense.
- **Agent + IDE are a single surface.** Not a chatbot glued next to an editor. The composer, plan, and prerender pane share the same real estate and same typographic rhythm.
- **Multi-form factor.** Desktop is primary; mobile is a first-class companion (approve plans, monitor runs, read transcripts) — not a shrunken desktop.

### Sources of reference

| Source | Where | Notes |
| --- | --- | --- |
| Current UI screenshots | (pending — user will attach) | The user mentioned screenshots of the current iteration to modernize. **Not yet provided** — the first UI kit pass was built from the product description alone and will need a second pass against the real screens. |
| Logo | `assets/logo-mark.png` | Atomic "A" mark, silver with orange gradient accent — informed the **Ember** accent color. |
| Brand inputs | User-provided questionnaire | Pastel candy palette, off-white/off-black modes, mono-forward type, Phosphor icons, "technical / engineering-forward" vibe. |

---

## 2. Content fundamentals

### Voice
Accuretta talks the way a careful engineer talks to another engineer. **Precise, lowercase, unceremonious.** No sales voice, no hype, no emoji. If a sentence could appear in a CHANGELOG, it's probably on-brand.

- **Perspective:** Mostly imperative ("Run the agent", "Prerender now") and system-neutral third person ("Agent is planning."). Second person ("you") used sparingly in docs/onboarding. First person plural ("we") avoided — the product is a tool, not a team.
- **Case:** UI controls are **Sentence case**. Section labels and eyebrows are **UPPERCASE with letterspacing**. Prose is lowercase-leaning; headlines keep proper nouns capitalized but avoid Title Case.
- **Tense:** Present. Status lines read as observations, not promises. "Agent is planning" — not "Agent will plan."
- **Numbers, units, keys:** Always literal. `2,340 tok/s`, `⌘⏎`, `200k ctx`, `v0.8.2`. Never "lightning-fast" or "tons of tokens".
- **Emoji:** Never. Status uses colored dots, Phosphor icons, or pastel pill backgrounds.
- **Punctuation:** Em-dashes and middle dots (`·`) are the house separators. Avoid exclamation points.

### Examples

| ✅ Accuretta voice | ❌ Off-voice |
| --- | --- |
| Agent is planning. 14 steps. | 🤖 Our AI is cooking up an amazing plan! |
| Prerender HTML, instantly. | Supercharge your workflow with next-gen AI |
| Key rejected. Check env. | Oops! Something went wrong 😅 |
| Local only · 200k ctx · v0.8.2 | Fast, private, and powerful ✨ |
| Preview → branch | Create a new branch from preview |

### Copy patterns
- **Empty states** state the fact, then the one next action. "No sessions yet. `⌘N` to start one."
- **Errors** name the cause before the remedy. "Key rejected. Check env."
- **Progress** is a noun + count. "Planning · step 3 of 14". Never a percentage unless deterministic.
- **Buttons** are verbs. "Prerender", "Run agent", "Approve plan" — not "Submit" or "Continue".

---

## 3. Visual foundations

### Color
- **Candy pastel palette** — mint, lilac, safety orange, lemon — used for **status, categorization, and quiet flair**. Pastels are never used for full-page backgrounds; they appear as chip fills, card washes, and icon backers.
- **Neutrals are warm.** Off-white is paper-warm (`#F6F4EF`), off-black is graphite-warm (`#141412`). No pure white, no pure black anywhere.
- **Accent is Ember** (`#E8813A`) — drawn from the logo's orange gradient. Used for primary CTAs, focus rings, live cursors, and "prerender now". Used **sparingly** — it should be a spark, not a wash.
- **Dark mode is a first-class peer**, not an afterthought. Every card should look correct in both modes.

### Type
- **Mono-forward.** JetBrains Mono is the default for UI, body, and code. Space Mono is the display face for H1/hero moments. Inter Tight is a sans fallback reserved for long-form prose where mono becomes tiring (docs, blog).
- **Weights used:** 400 (body), 500 (labels/eyebrows), 600 (H3–H5), 700 (display).
- **Letterspacing:** Negative on display (–0.04em to –0.02em), wide on eyebrows (+0.12em UPPERCASE).
- **Tabular numerals** on by default via `font-feature-settings`. Critical for token counts, timestamps, file sizes.

### Spacing & layout
- **4px base grid.** Tokens `s-1` (4) through `s-24` (96). Most interior card padding is `s-4`/`s-5` (16/20); screen padding is `s-6`/`s-8`.
- **Dense by default.** Technical audience rewards density. Line-height for body is 1.5, for UI is 1.25.
- **Fixed elements:** command palette is always ⌘K anywhere. Status pill is always bottom-left of the window. Agent composer is anchored bottom.

### Backgrounds
- **No gradients on page backgrounds.** The brand refuses the bluish-purple AI aesthetic entirely.
- **Pastel washes** are used for contextual surfaces (approved plan = mint card, warning = lemon card).
- **No hand-drawn illustrations, no patterns, no grain, no noise.** The product is the aesthetic — surfaces are clean and instrumented.
- Imagery, when used at all, is **literal** (screenshots of the app's own prerender pane), never stock.

### Animation
- **Motion is functional, not decorative.** 120–200ms transitions for state changes.
- **Easing:** `cubic-bezier(0.2, 0.8, 0.2, 1)` for most enters/exits; springy easing (`0.34, 1.56, 0.64, 1`) reserved for the prerender pane's "snap into place" moment.
- **No bounces on navigation, no page transitions, no parallax.** Fades and small translations only.
- **Live indicators blink** — cursor, "● live" dot. Everything else is static unless user-triggered.

### Hover / press / focus
- **Hover:** 4–8% overlay darken on neutrals; for pastel chips, the `-2` ramp. Cursor changes to pointer.
- **Press:** `transform: scale(0.98)` for buttons, no color shift.
- **Focus:** 3px `rgba(Ember, 0.15)` ring + 1px Ember border. Visible on keyboard only (use `:focus-visible`).
- **Disabled:** 50% opacity, no pointer events.

### Borders, shadows, corners
- **Borders are standard.** 1px `--border` on cards, `--border-strong` on inputs. No 2px accent borders, no colored left-borders (anti-pattern).
- **Shadows are quiet.** Low y-offset, low alpha. The heaviest shadow (`--shadow-pop`) is reserved for modals and command palette.
- **Inset shadows** mark wells — code blocks, input backgrounds, sunken panels.
- **Corners:** 6px is the default (`--r-md`). Cards use 10px. Pills and avatars use 999px. Nothing goes above 20px.

### Transparency & blur
- **Backdrop blur** only on floating overlays (command palette backdrop, mobile sheet scrim). Never on cards or chrome.
- **Transparency** is used in dark-mode chrome borders (`rgba(255,255,255,0.04)`) and in modal scrims (`rgba(20,20,18,0.5)`).

### Iconography
See [§4](#4-iconography) below.

### Imagery tone
When real imagery appears (marketing site, onboarding), it's **warm, slightly desaturated, low-contrast**, evoking paper under a desk lamp. No blue-hour tech photography; no 3D-rendered abstract shapes.

---

## 4. Iconography

Accuretta uses **[Phosphor Icons](https://phosphoricons.com/)** exclusively. Geometric, multi-weight, good at technical density.

- **Delivery:** CDN via `@phosphor-icons/web` — `<i class="ph ph-cpu"></i>`. No icon font is bundled; all icons are pulled at runtime. For React, use `phosphor-react`.
- **Weights:** `ph` (regular, default), `ph-bold` (for small sizes and on-color), `ph-duotone` (for feature tiles and illustrations), `ph-thin`/`ph-light` used sparingly. Never mix weights within a single toolbar.
- **Sizing:** 14px inline, 16px default button icon, 20px card/menu, 24px toolbar, 32px+ hero.
- **Color:** inherits `currentColor`. On pastel chips, icon color matches the chip's `-3` ramp or the neutral `fg`.
- **No emoji. No custom SVG icons.** If Phosphor doesn't have it, the need is suspicious — reconsider the UI. The only exception is the brand mark itself (`assets/logo-mark.png`).
- **Unicode chars** used sparingly as typographic glyphs only: `·`, `→`, `⌘`, `⏎`, `⌥`, `⇧`, `↑↓`. Never as bullets or icons.

---

## 5. File index

| Path | Purpose |
| --- | --- |
| `README.md` | This file. |
| `SKILL.md` | Agent-Skills-compatible entry point for using this system in Claude Code. |
| `colors_and_type.css` | All CSS variables: colors, type, spacing, radii, shadows, motion. |
| `assets/` | Logo, brand imagery, any static assets. |
| `preview/` | Design System tab cards (one HTML file per card). |
| `ui_kits/desktop/` | Desktop app UI kit — IDE surface, agent composer, prerender pane. |
| `ui_kits/mobile/` | Mobile companion UI kit — session monitor, plan approval, transcript viewer. |

---

## 6. Caveats & open items

- **Screenshots of the current product have not been received.** The UI kit is a first pass extrapolated from the product description. **Expect a second pass once real screens are attached.**
- **Font substitution:** No proprietary fonts were provided. Currently using JetBrains Mono (body/UI), Space Mono (display), Inter Tight (sans fallback) — all Google Fonts. Supply licensed files to swap.
- **Phosphor is loaded via CDN** (runtime dependency). For offline/embedded builds, bundle locally.
