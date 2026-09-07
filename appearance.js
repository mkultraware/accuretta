(() => {
  "use strict";
  const valid = value => typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
  const ink = color => {
    const rgb = color.slice(1).match(/../g).map(x => parseInt(x, 16) / 255)
      .map(x => x <= 0.04045 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4);
    return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722 > 0.179 ? "#202024" : "#faf9f6";
  };
  const mix = (a, b, weight) => "#" + [1, 3, 5].map(i =>
    Math.round(parseInt(a.slice(i, i + 2), 16) * (1 - weight) + parseInt(b.slice(i, i + 2), 16) * weight)
      .toString(16).padStart(2, "0")).join("");
  function tokens(colors) {
    if (!colors || ![colors.background, colors.surface, colors.accent].every(valid)) return {};
    const { background: bg, surface, accent } = colors;
    const fg = ink(bg);
    return {
      "--bg": bg, "--bg-raised": surface, "--bg-sunken": mix(bg, fg, 0.04), "--bg-inset": surface,
      "--fg": fg, "--fg-muted": mix(bg, fg, 0.72), "--fg-subtle": mix(bg, fg, 0.58),
      "--fg-faint": mix(bg, fg, 0.45), "--fg-invert": bg,
      "--surface-fg": ink(surface), "--surface-muted": mix(surface, ink(surface), 0.72),
      "--surface-subtle": mix(surface, ink(surface), 0.58), "--surface-faint": mix(surface, ink(surface), 0.45),
      "--border": mix(bg, fg, 0.16), "--border-strong": mix(bg, fg, 0.28), "--border-subtle": mix(bg, fg, 0.09),
      "--accent": accent, "--accent-hover": mix(accent, ink(accent), 0.12),
      "--accent-subtle": mix(bg, accent, 0.14), "--accent-fg": ink(accent),
      "--glass-bg": surface, "--glass-border": mix(bg, fg, 0.16),
      "--btn-send-gradient": `linear-gradient(${accent}, ${accent})`, "--btn-send-glow": "none",
      "--wb-a": surface, "--wb-b": mix(bg, accent, 0.45), "--wb-c": accent,
    };
  }
  let applied = [];
  function apply(theme, palettes) {
    const root = document.documentElement;
    applied.forEach(key => root.style.removeProperty(key));
    const palette = tokens(palettes?.[theme]);
    applied = Object.keys(palette);
    applied.forEach(key => root.style.setProperty(key, palette[key]));
    root.toggleAttribute("data-custom-colors", applied.length > 0);
    if (applied.length) {
      root.dataset.customInk = ink(palettes[theme].background) === "#202024" ? "dark" : "light";
      root.style.colorScheme = root.dataset.customInk === "dark" ? "light" : "dark";
    } else {
      delete root.dataset.customInk;
      root.style.removeProperty("color-scheme");
    }
    try { localStorage.setItem("accuretta:palettes", JSON.stringify(palettes || {})); } catch {}
  }
  window.AccurettaAppearance = { tokens, apply, ink };
  try { apply(document.documentElement.dataset.theme, JSON.parse(localStorage.getItem("accuretta:palettes") || "{}")); } catch {}
})();
