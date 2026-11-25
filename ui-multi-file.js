/* SPDX-License-Identifier: AGPL-3.0-only */
/* multi-file parsing: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["multi-file"] = ({ esc, maybeUnescapeJsonFence }) => {
  // The model may emit companion files as fenced blocks with an info string like
  // ```css path=style.css. They are collected by path so the preview can inline
  // them and Export Project can zip them unchanged.
  function parseMultiFileBlocks(text) {
    if (!text) return {};
    const out = {};
    // match fenced blocks with info strings containing path=<path>
    const re = /```([a-zA-Z0-9]+)?\s+([^\n`]*?path=([^\s`]+)[^\n`]*)\n([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      const rawPath = (m[3] || "").trim().replace(/^["']|["']$/g, "");
      const body = m[4] || "";
      if (!rawPath) continue;
      // normalise + safety: strip leading slashes, no .. traversal, posix slashes only
      const safe = rawPath.replace(/\\/g, "/").replace(/^\/+/, "");
      if (safe.includes("..")) continue;
      out[safe] = maybeUnescapeJsonFence(body).replace(/\s+$/, "");
    }
    return out;
  }

  // Return the primary HTML with linked local css/js from `files` inlined, for
  // the preview iframe. Non-local hrefs are left alone.
  function inlineLocalAssets(html, files) {
    if (!html || !files || !Object.keys(files).length) return html;
    const keyOf = (href) => (href || "").replace(/\\/g, "/").replace(/^\.\//, "").replace(/^\/+/, "");
    // inline <link rel="stylesheet" href="..."> for local files
    html = html.replace(/<link\b([^>]*?)href=(["'])([^"']+)\2([^>]*)>/gi, (full, pre, _q, href, post) => {
      if (/rel\s*=\s*["']stylesheet/i.test(pre + post) || /rel\s*=\s*["']stylesheet/i.test(full)) {
        const key = keyOf(href);
        if (files[key] != null) return `<style data-inlined-from="${esc(key)}">\n${files[key]}\n</style>`;
      }
      return full;
    });
    // inline <script src="..."> for local files
    html = html.replace(/<script\b([^>]*?)src=(["'])([^"']+)\2([^>]*)><\/script>/gi, (full, pre, _q, src, post) => {
      const key = keyOf(src);
      if (files[key] != null) {
        const typeMatch = (pre + post).match(/type\s*=\s*(["'])([^"']+)\1/i);
        const typeAttr = typeMatch ? ` type="${esc(typeMatch[2])}"` : "";
        return `<script data-inlined-from="${esc(key)}"${typeAttr}>\n${files[key]}\n<\/script>`;
      }
      return full;
    });
    return html;
  }
  return { parseMultiFileBlocks, inlineLocalAssets };
};
