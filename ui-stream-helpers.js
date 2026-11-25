/* SPDX-License-Identifier: AGPL-3.0-only */
/* streaming pipeline helpers: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["stream-helpers"] = ({
  _isKnownLang, isLikelyLiveWorkingNotes, maybeUnescapeJsonFence,
}) => {
  // Line-anchored fence state for a live buffer. A fence opens only when a ```
  // run starts a line (<=3 spaces indent) and closes only on a bare backtick
  // line, so mid-prose ``` runs can't pair up into a phantom code card.
  function scanOpenFenceState(text) {
    const lines = String(text || "").split("\n");
    let inFence = false, infoStr = "", bodyLen = 0, openIdx = -1, bodyStart = -1;
    let pos = 0;
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      if (!inFence) {
        if (/^[ \t]*`{3,}/.test(ln)) {
          inFence = true;
          infoStr = ln.replace(/^[ \t]*`{3,}/, "").trim();
          openIdx = pos;
          bodyStart = pos + ln.length + 1;
          bodyLen = 0;
        }
      } else if (/^[ \t]*`{3,}[ \t]*$/.test(ln)) {
        inFence = false;
        infoStr = "";
      } else {
        bodyLen += ln.length + 1;
      }
      pos += ln.length + 1;
    }
    return { inFence, infoStr, bodyLen, openIdx, bodyStart };
  }

  // Does an unlabeled (or labeled) fence body actually read like code? Long
  // bodies must earn code treatment via punctuation/keyword density; long
  // rambling prose after a stray fence renders as prose instead. Short
  // bodies get the benefit of the doubt so real snippets never flicker.
  function fenceBodyLooksLikeCode(body, infoStr) {
    const lang = (infoStr || "").split(/\s+/)[0].toLowerCase();
    if (lang === "email") return true;   // prose by nature; the card renders as it streams
    if (lang && _isKnownLang(lang)) return true;
    if (/path=/i.test(infoStr || "")) return true;
    const b = String(body || "");
    if (b.length < 500) return true;
    const signals = (b.match(/[{};<>=()[\]]|=>|->|::|\b(?:function|return|def|class|import|export|const|let|var|public|void|SELECT|INSERT)\b/g) || []).length;
    const sentences = (b.match(/[.!?]["')\]]?(?:\s|$)/g) || []).length;
    return signals * 12 >= b.length || sentences < 4;
  }

  // Largest position in `text` at or after `minLen` where the answer can be
  // split into "frozen prefix" + "volatile tail" with IDENTICAL markdown
  // rendering: a blank-line block boundary, outside any open fence, whose
  // next line starts a fresh block (not a list/table/quote/indent
  // continuation). Returns 0 when no safe cut exists yet.
  function findStableCut(text, minLen) {
    const t = String(text || "");
    if (t.length < 600) return 0;
    const lines = t.split("\n");
    let pos = 0, inFence = false, cut = 0;
    for (let i = 0; i < lines.length; i++) {
      const ln = lines[i];
      const lineEnd = pos + ln.length;
      if (!inFence && /^[ \t]*`{3,}/.test(ln)) inFence = true;
      else if (inFence && /^[ \t]*`{3,}[ \t]*$/.test(ln)) inFence = false;
      else if (!inFence && ln.trim() === "" && lineEnd + 1 >= minLen && i + 1 < lines.length) {
        const nxt = lines[i + 1];
        const continuation = /^\s*(?:[-*+]\s|\d+[.)]\s|>|\||\t| {4,})/.test(nxt);
        if (!continuation && nxt.trim() !== "") cut = lineEnd + 1;
      }
      pos = lineEnd + 1;
    }
    return cut;
  }

  // DEFAULT-DIM promotion: while a reasoning-capable model's untagged output
  // has not yet proven it is the ANSWER, it presents as working notes. These
  // structural shapes promote immediately; plain prose promotes once it is
  // long enough to stop matching the planning-prose heuristics.
  function shouldPromoteAnswer(text) {
    const s = String(text || "").trimStart();
    if (!s) return false;
    if (/^(?:#{1,6}\s|```|\*\*[^\s*]|[-*+]\s|\d+\.\s)/.test(s)) return true;
    return s.length >= 160 && !isLikelyLiveWorkingNotes(s);
  }

  // Bracket matcher for the linear JSON tool-call stripper — string-aware,
  // no backtracking, bounded by the caller.
  function matchBracket(text, start) {
    const open = text[start];
    const close = open === "{" ? "}" : "]";
    let depth = 0, inStr = false, esc = false;
    for (let i = start; i < text.length; i++) {
      const c = text[i];
      if (inStr) {
        if (esc) esc = false;
        else if (c === "\\") esc = true;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') inStr = true;
      else if (c === open) depth++;
      else if (c === close) {
        depth--;
        if (!depth) return i;
      }
    }
    return -1;
  }

  // Linear stripper for {name,arguments} blobs (nested lazy regexes could
  // backtrack catastrophically on JSON-array-shaped prose and freeze the tab).
  // Walks candidates left to right; removed segments are never re-scanned.
  function stripJsonToolCallShapes(text) {
    let t = String(text || "");
    let i = 0;
    while (i < t.length) {
      const c = t[i];
      if (c !== "{" && c !== "[") { i++; continue; }
      const end = matchBracket(t, i);
      if (end === -1 || end - i > 4000) { i++; continue; }
      const seg = t.slice(i, end + 1);
      if (/"name"\s*:/.test(seg) && /"(?:arguments|parameters?|function)"\s*:/.test(seg)) {
        t = t.slice(0, i) + t.slice(end + 1);
        continue;
      }
      i++;
    }
    return t;
  }

  // renderMarkdown's fence extractor — same line-anchored rules as
  // scanOpenFenceState so streaming and final renders always agree. Fenced
  // regions become \x00F{i}\x00 placeholders; an unclosed fence at EOF is
  // only treated as code when the body reads like code (the ramble guard).
  function extractLineAnchoredFences(text, fences) {
    const lines = String(text || "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const openM = /^[ \t]*`{3,}([^\n`]*)$/.exec(lines[i]);
      if (!openM) { out.push(lines[i]); i++; continue; }
      const infoStr = (openM[1] || "").trim();
      const body = [];
      let j = i + 1;
      let closed = false;
      while (j < lines.length) {
        if (/^[ \t]*`{3,}[ \t]*$/.test(lines[j])) { closed = true; break; }
        body.push(lines[j]);
        j++;
      }
      const bodyText = body.join("\n");
      if (!closed && !fenceBodyLooksLikeCode(bodyText, infoStr)) {
        // stray fence opener inside prose — keep it as literal text
        out.push(lines[i]);
        i++;
        continue;
      }
      fences.push({ infoStr, code: maybeUnescapeJsonFence(bodyText) });
      out.push(`\x00F${fences.length - 1}\x00`);
      i = closed ? j + 1 : j;
    }
    return out.join("\n");
  }

  // A list line: indent, marker ("-", "*", "+" or "3."), text.
  const _MD_LIST_ITEM = /^(\s*)([-*+]|\d+\.)\s+(.*)$/;
  // Nested <ul>/<ol> from [{indent, ordered, num, text}]. Deeper indentation
  // nests under the item above; a numbered list keeps its first number, so a
  // list the model split with a paragraph goes on 3, 4… instead of restarting.
  function _mdListHtml(items) {
    let i = 0;
    const build = (indent) => {
      const ordered = items[i].ordered;
      const start = ordered && items[i].num > 1 ? ` start="${items[i].num}"` : "";
      let html = ordered ? `<ol class="md-list"${start}>` : '<ul class="md-list">';
      while (i < items.length && items[i].indent >= indent) {
        if (items[i].indent === indent && items[i].ordered !== ordered) break;
        const item = items[i++];
        let li = `<li>${item.text}`;
        while (i < items.length && items[i].indent > item.indent) li += build(items[i].indent);
        html += li + "</li>";
      }
      return html + (ordered ? "</ol>" : "</ul>");
    };
    let out = "";
    while (i < items.length) out += build(items[i].indent);
    return out;
  }
  return {
    scanOpenFenceState, fenceBodyLooksLikeCode, findStableCut, shouldPromoteAnswer,
    stripJsonToolCallShapes, extractLineAnchoredFences, _MD_LIST_ITEM, _mdListHtml,
  };
};
