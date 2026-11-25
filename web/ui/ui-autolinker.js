/* SPDX-License-Identifier: AGPL-3.0-only */
/* chat link autolinker + hover preview: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["autolinker"] = ({ state }) => {
  // Trailing punctuation stripped from auto-detected URLs so a sentence-ending
  // period isn't part of the link, unless it is balanced inside the URL
  // (`https://en.wikipedia.org/wiki/Foo_(bar)` keeps its paren).
  const URL_TRAIL_PUNCT = /[)\]\}>"',.;:!?]+$/;
  function trimTrailingPunct(url) {
    let u = url;
    while (URL_TRAIL_PUNCT.test(u)) {
      const last = u.slice(-1);
      // keep one paren if it has a matching opener inside the URL
      if (last === ")" && (u.match(/\(/g) || []).length > (u.match(/\)/g) || []).length - 0) break;
      if (last === "]" && (u.match(/\[/g) || []).length > (u.match(/\]/g) || []).length - 0) break;
      u = u.slice(0, -1);
      if (!u) break;
    }
    return u;
  }
  function buildLinkAnchor(rawUrl, label) {
    const url = trimTrailingPunct(rawUrl);
    let host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch {}
    const fav = host && !state.settings.offline_active ? `https://icons.duckduckgo.com/ip3/${host}.ico` : "";
    const text = label != null ? label : (host || url);
    const safeUrl = url.replace(/"/g, "&quot;");
    const safeHost = (host || "").replace(/"/g, "&quot;");
    const safeText = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const favHtml = fav
      ? `<img class="chat-link-fav" src="${fav.replace(/"/g, "&quot;")}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : "";
    // data-link-url drives the lazy hover fetch; OG title/description are not
    // inlined (they'd balloon the bubble).
    return `<a class="chat-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer" data-link-url="${safeUrl}" data-link-host="${safeHost}">${favHtml}<span class="chat-link-text">${safeText}</span></a>`;
  }
  function autolinkChatHtml(html) {
    if (!html) return html;
    // Step 1: markdown links [text](url) — only when the url looks http/https
    // or a bare protocol-less www.* domain (we'll prefix https://).
    html = html.replace(
      /\[([^\]\n]+)\]\(((?:https?:\/\/|www\.)[^\s)]+)\)/g,
      (_m, label, url) => {
        if (url.startsWith("www.")) url = "https://" + url;
        return buildLinkAnchor(url, label);
      }
    );
    // Step 2: bare URLs. The input is escaped user text mixed with our own tags,
    // so a tag-tokenizer split keeps URL matching out of attribute values,
    // <code>, <pre> and existing <a>.
    const parts = html.split(/(<[^>]+>)/g);
    const inSkip = []; // stack of tag names we shouldn't autolink inside
    const SKIP_TAGS = new Set(["a", "code", "pre", "kbd", "samp", "script", "style"]);
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i];
      if (!seg) continue;
      if (seg.startsWith("<")) {
        const m = /^<\s*(\/?)\s*([a-zA-Z][\w-]*)/.exec(seg);
        if (m) {
          const closing = m[1] === "/";
          const tag = m[2].toLowerCase();
          if (SKIP_TAGS.has(tag)) {
            if (closing) {
              const idx = inSkip.lastIndexOf(tag);
              if (idx >= 0) inSkip.splice(idx, 1);
            } else if (!/\/\s*>$/.test(seg)) {
              // not self-closing — push onto skip stack
              inSkip.push(tag);
            }
          }
        }
        continue;
      }
      if (inSkip.length) continue;
      // bare URL pass: http(s)://… and www.x.y…
      parts[i] = seg.replace(
        /\b((?:https?:\/\/|www\.)[^\s<>()\[\]"']+)/g,
        (_m, raw) => {
          let url = raw;
          if (url.startsWith("www.")) url = "https://" + url;
          return buildLinkAnchor(url, raw);
        }
      );
    }
    return parts.join("");
  }
  return { autolinkChatHtml };
};
