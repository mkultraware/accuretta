/* Minimal, safe markdown renderer for the notch reply area.
 *
 * The approved widget appends streamed reply text as plain spans, so the
 * formatting has to happen after the fact (the HTML file is never edited).
 * Everything is escaped first and only a small, known set of inline marks and
 * block types is recognised — the reply text is model output and must never be
 * able to inject markup.
 *
 * Loaded before notch-wire.js. Exposed for tests:
 *   if (typeof module !== "undefined") module.exports = { render, renderInline };
 */
(function (root) {
"use strict";

function escHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
}

// Inline marks. Inline code is extracted first so its contents are never
// re-interpreted as bold/italic.
function renderInline(text) {
  let s = escHtml(text);
  const codes = [];
  s = s.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(c);
    return "\u0000C" + (codes.length - 1) + "\u0000";
  });
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s([{>-])\*([^*\n]+)\*(?=[\s).,!?:;\]}]|$)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  s = s.replace(/\u0000C(\d+)\u0000/g, (_, i) => "<code>" + codes[+i] + "</code>");
  return s;
}

function render(md) {
  const lines = String(md == null ? "" : md).split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];

    // fenced code block (``` or ```lang) — content kept verbatim
    const fence = line.match(/^\s*(`{3,}|~{3,})\s*([A-Za-z0-9_+#.-]*)\s*$/);
    if (fence) {
      const marker = fence[1][0];
      const lang = fence[2] || "";
      const body = [];
      i++;
      while (i < lines.length && !new RegExp("^\\s*" + marker + "{3,}\\s*$").test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      i++; // closing fence
      out.push(
        '<pre class="md-code"' + (lang ? ' data-lang="' + escHtml(lang) + '"' : "") +
        "><code>" + escHtml(body.join("\n")) + "</code></pre>"
      );
      continue;
    }

    // heading
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const level = Math.min(6, h[1].length);
      out.push("<h" + level + ">" + renderInline(h[2]) + "</h" + level + ">");
      i++;
      continue;
    }

    if (!line.trim()) { i++; continue; }

    // bullet list (also accepts 1. / 1) ordered markers). One line per item:
    // joining "continuation" lines swallowed whatever followed the list (a
    // link, a rule, the next paragraph).
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      const re = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      while (i < lines.length && re.test(lines[i])) {
        items.push("<li>" + renderInline(lines[i].replace(re, "")) + "</li>");
        i++;
      }
      out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
      continue;
    }

    // horizontal rule
    if (/^\s*([-*_])\s*(\1\s*){2,}$/.test(line)) {
      out.push("<hr>");
      i++;
      continue;
    }

    // paragraph (consume until a blank line or the start of another block)
    const para = [];
    while (i < lines.length && lines[i].trim() &&
           !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
           !/^\s*(`{3,}|~{3,})/.test(lines[i]) &&
           !/^\s*#{1,6}\s/.test(lines[i]) &&
           !/^\s*([-*_])\s*(\1\s*){2,}$/.test(lines[i])) {
      para.push(lines[i]);
      i++;
    }
    if (para.length) out.push("<p>" + renderInline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  return out.join("");
}

const api = { render, renderInline, escHtml };
root.notchMarkdown = api;
if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);