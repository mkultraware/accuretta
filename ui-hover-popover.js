/* SPDX-License-Identifier: AGPL-3.0-only */
/* hover popover: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["hover-popover"] = ({ esc, state }) => {
  // One shared, lazily created popover at body level, so it escapes any
  // overflow:hidden bubble parent.
  const _linkPreviewCache = new Map(); // url -> {title, description, image, host} or {error}
  const _linkPreviewInflight = new Map(); // url -> Promise
  let _linkPopoverEl = null;
  let _linkPopoverHideTimer = null;
  let _linkPopoverActiveAnchor = null;
  function ensureLinkPopover() {
    if (_linkPopoverEl) return _linkPopoverEl;
    const el = document.createElement("div");
    el.className = "chat-link-popover";
    el.setAttribute("hidden", "");
    el.innerHTML = `
      <div class="chat-link-popover-inner">
        <div class="chat-link-popover-img" hidden></div>
        <div class="chat-link-popover-body">
          <div class="chat-link-popover-site"><img class="chat-link-popover-fav" alt=""><span class="chat-link-popover-host"></span></div>
          <div class="chat-link-popover-title">Loading preview…</div>
          <div class="chat-link-popover-desc"></div>
          <div class="chat-link-popover-url"></div>
        </div>
      </div>`;
    // Keep the popover open while the cursor is over IT (so the user can read
    // long descriptions without losing the card to a flicker).
    el.addEventListener("mouseenter", () => {
      if (_linkPopoverHideTimer) { clearTimeout(_linkPopoverHideTimer); _linkPopoverHideTimer = null; }
    });
    el.addEventListener("mouseleave", scheduleHideLinkPopover);
    document.body.appendChild(el);
    _linkPopoverEl = el;
    return el;
  }
  function fetchLinkPreview(url) {
    if (_linkPreviewCache.has(url)) return Promise.resolve(_linkPreviewCache.get(url));
    if (_linkPreviewInflight.has(url)) return _linkPreviewInflight.get(url);
    const p = fetch(`/api/link_preview?url=${encodeURIComponent(url)}`)
      .then(r => r.json())
      .then(j => { _linkPreviewCache.set(url, j); _linkPreviewInflight.delete(url); return j; })
      .catch(e => { const j = { error: String(e) }; _linkPreviewCache.set(url, j); _linkPreviewInflight.delete(url); return j; });
    _linkPreviewInflight.set(url, p);
    return p;
  }
  function positionLinkPopover(anchor) {
    if (!_linkPopoverEl) return;
    const r = anchor.getBoundingClientRect();
    // Measure the popover itself so we can flip if it would clip the viewport.
    const pw = _linkPopoverEl.offsetWidth || 320;
    const ph = _linkPopoverEl.offsetHeight || 120;
    let top = r.bottom + 8;
    if (top + ph > window.innerHeight - 8) {
      top = Math.max(8, r.top - ph - 8); // place above
    }
    let left = r.left;
    if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
    if (left < 8) left = 8;
    _linkPopoverEl.style.top = top + "px";
    _linkPopoverEl.style.left = left + "px";
  }
  function renderLinkPopover(data) {
    if (!_linkPopoverEl) return;
    const titleEl = _linkPopoverEl.querySelector(".chat-link-popover-title");
    const descEl = _linkPopoverEl.querySelector(".chat-link-popover-desc");
    const urlEl = _linkPopoverEl.querySelector(".chat-link-popover-url");
    const hostEl = _linkPopoverEl.querySelector(".chat-link-popover-host");
    const favEl = _linkPopoverEl.querySelector(".chat-link-popover-fav");
    const imgWrap = _linkPopoverEl.querySelector(".chat-link-popover-img");
    if (data.error) {
      titleEl.textContent = data.url || "(link)";
      descEl.textContent = "Couldn't load preview: " + (data.error || "unknown error");
      urlEl.textContent = "";
      hostEl.textContent = "";
      favEl.removeAttribute("src");
      imgWrap.setAttribute("hidden", "");
      imgWrap.style.backgroundImage = "";
      return;
    }
    const host = data.host || "";
    titleEl.textContent = data.title || data.url || "(no title)";
    descEl.textContent = data.description || "";
    descEl.style.display = data.description ? "" : "none";
    urlEl.textContent = data.url || "";
    hostEl.textContent = data.site_name || host || "";
    if (host && !state.settings.offline_active) favEl.src = `https://icons.duckduckgo.com/ip3/${host}.ico`;
    else favEl.removeAttribute("src");
    if (data.image) {
      imgWrap.style.backgroundImage = `url("${data.image.replace(/"/g, '\\"')}")`;
      imgWrap.removeAttribute("hidden");
    } else {
      imgWrap.setAttribute("hidden", "");
      imgWrap.style.backgroundImage = "";
    }
  }
  function showLinkPopover(anchor) {
    const url = anchor.getAttribute("data-link-url");
    if (!url) return;
    const el = ensureLinkPopover();
    if (_linkPopoverHideTimer) { clearTimeout(_linkPopoverHideTimer); _linkPopoverHideTimer = null; }
    _linkPopoverActiveAnchor = anchor;
    // Initial render: optimistic skeleton so the card pops instantly.
    renderLinkPopover({ url, host: anchor.getAttribute("data-link-host") || "", title: "Loading preview…", description: "", image: "" });
    el.removeAttribute("hidden");
    requestAnimationFrame(() => {
      el.classList.add("visible");
      positionLinkPopover(anchor);
    });
    fetchLinkPreview(url).then(data => {
      // Only render if the user hasn't moved on to a different anchor.
      if (_linkPopoverActiveAnchor !== anchor) return;
      renderLinkPopover(data);
      positionLinkPopover(anchor);
    });
  }
  function scheduleHideLinkPopover() {
    if (_linkPopoverHideTimer) clearTimeout(_linkPopoverHideTimer);
    _linkPopoverHideTimer = setTimeout(() => {
      if (_linkPopoverEl) {
        _linkPopoverEl.classList.remove("visible");
        _linkPopoverEl.setAttribute("hidden", "");
      }
      _linkPopoverActiveAnchor = null;
      _linkPopoverHideTimer = null;
    }, 220);
  }
  // Global delegated hover — works for chat-links inserted at any time.
  document.addEventListener("mouseover", (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest("a.chat-link");
    if (!a) return;
    showLinkPopover(a);
  });
  document.addEventListener("mouseout", (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest("a.chat-link");
    if (!a) return;
    // ignore moves into descendants of the same anchor
    if (a.contains(ev.relatedTarget)) return;
    // ignore moves into the popover itself (its own mouseleave handles hiding)
    if (_linkPopoverEl && _linkPopoverEl.contains(ev.relatedTarget)) return;
    scheduleHideLinkPopover();
  });

  // very small HTML syntax highlighter for code view
  function highlightHTML(src) {
    const s = esc(src);
    return s
      .replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="tok-comment">$1</span>')
      .replace(/(&lt;\/?)([a-zA-Z][\w-]*)/g, '$1<span class="tok-tag">$2</span>')
      .replace(/(\s)([a-zA-Z][\w-]*)=(&quot;[^&]*?&quot;)/g, '$1<span class="tok-attr">$2</span>=<span class="tok-str">$3</span>');
  }
  return { highlightHTML };
};
