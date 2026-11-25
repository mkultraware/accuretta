/* SPDX-License-Identifier: AGPL-3.0-only */
/* viewport presets: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["viewport-presets"] = ({
  $, $$, injectConsoleForwarder, injectCspIfNeeded, injectTailwindIfNeeded, inlineLocalAssets,
  state,
}) => {
  const VIEWPORT_WIDTHS = { full: null, desktop: 1280, tablet: 820, mobile: 390 };

  function applyViewport(vp) {
    state.viewport = vp;
    const stage = $("#preview-stage");
    const frame = $("#preview-frame");
    if (!stage || !frame) return;
    const w = VIEWPORT_WIDTHS[vp];
    if (w) {
      stage.classList.add("vp-constrained");
      frame.style.maxWidth = w + "px";
      frame.style.marginInline = "auto";
    } else {
      stage.classList.remove("vp-constrained");
      frame.style.maxWidth = "";
      frame.style.marginInline = "";
    }
    $$(".vp-opt").forEach(b => {
      const on = b.dataset.vp === vp;
      b.classList.toggle("active", on);
      b.setAttribute("aria-checked", String(on));
    });
    // One header button stands in for the four presets: it shows the current
    // one and is accented while the frame is narrowed.
    const vpBtn = $("#btn-viewport");
    const pick = $(`.vp-opt[data-vp="${vp}"]`);
    if (vpBtn && pick) {
      vpBtn.querySelector("i").className = pick.querySelector("i").className;
      const label = `Viewport: ${pick.querySelector("span").textContent}${w ? ` ${w}px` : ""}`;
      vpBtn.title = label;
      vpBtn.setAttribute("aria-label", label);
      vpBtn.classList.toggle("on", !!w);
    }
  }

  function buildPreviewHtml() {
    let html = inlineLocalAssets(state.currentHtml, state.currentFiles);
    html = injectTailwindIfNeeded(html);
    html = injectConsoleForwarder(html);
    html = injectCspIfNeeded(html);
    return html;
  }
  return { applyViewport, buildPreviewHtml };
};
