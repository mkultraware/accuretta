# Accuretta · Desktop UI Kit

The primary Accuretta surface: a three-pane IDE workspace with sidebar, file tree, plan+composer column, and live prerender pane.

## Files
- `index.html` — click-through demo of the desktop app.
- `components.jsx` — `Sidebar`, `Topbar`, `FileTree`, `PlanPanel`, `PlanStep`, `Composer`, `PrerenderPane`, `Icon` (all exposed on `window`).
- `styles.css` — all `acr-*` classes. Depends on tokens from `../../colors_and_type.css`.

## Layout
```
┌─────────┬────────────────────────────────────────────────┐
│ Sidebar │ Topbar                                         │
│         ├──────┬──────────────────┬─────────────────────┤
│ nav     │ Tree │ Plan             │ Prerender           │
│ status  │      │ (stepwise agent) │ (live HTML frame)   │
│         │      │ ─────────────    │                     │
│         │      │ Composer (dock)  │                     │
└─────────┴──────┴──────────────────┴─────────────────────┘
```

## Caveat
This first pass was extrapolated from the product description (local AI + IDE + HTML prerender). Once real screenshots of the current product are provided, expect a second pass to align with the actual UI.
