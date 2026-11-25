/* SPDX-License-Identifier: AGPL-3.0-only */
/* markdown-lite for chat bubbles: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["markdown"] = ({
  LANG_ALIAS, _MD_LIST_ITEM, _isKnownLang, _mdListHtml, autolinkChatHtml, esc,
  extractLineAnchoredFences, highlightForCard, renderDiffCard, renderEmailCard,
  rewriteWriteFileToolCallToFence, state, stripJsonToolCallShapes, wrapCodeLines,
}) => {
  // Preserves code fences, ignores tool_call tags (rendered as tool cards separately).
  function renderMarkdown(text, opts = {}) {
    const streaming = !!opts.streaming;
    if (!text) return "";

    // === FAILSAFE: STRIP CASCADE TAGS ===
    // Force-strip proactive tags right before the renderer so they can never bleed into the UI.
    text = text.replace(/(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*?)(?:<|&lt;|\\<)\/cascade(?:>|&gt;|\\>)/gi, "");
    text = text.replace(/(?:<|&lt;|\\<)cascade[\s\S]*$/gi, "");
    text = text.replace(/(?:<|&lt;|\\<)c(?:a(?:s(?:c(?:a(?:d(?:e)?)?)?)?)?)?$/gi, "");

    // === REWRITE write_file TOOL_CALL BACK TO ```html``` FENCE ===
    // Some models (Qwen3, DeepSeek distills) ignore IDE-mode prompts and wrap the
    // requested HTML in a `write_file` tool_call. The bridge extracts it for the
    // preview, but the bubble would show raw JSON (or nothing, after the strippers
    // below), so present it as a normal ```html``` fence. Uses the linear scanner:
    // a backtracking regex freezes the tab on 30KB+ bodies.
    text = rewriteWriteFileToolCallToFence(text);

    // === STRIP ALL TOOL CALL FORMATS ===
    // Closed forms — the parser already executed these; just clean the bubble.
    text = text.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, "");
    text = text.replace(/```tool_call[\s\S]*?```/gi, "");
    // BugTraceAI / gemma-tunes:  <call:NAME>{...}</call(:NAME)?>
    text = text.replace(/<call:[a-zA-Z0-9_\-]+>[\s\S]*?<\/call(?::[a-zA-Z0-9_\-]+)?>/gi, "");
    // Llama 3.x native:  <|python_tag|>{...}(<|eom_id|>|<|eot_id|>)
    text = text.replace(/<\|python_tag\|>[\s\S]*?(<\|eom_id\|>|<\|eot_id\|>)/gi, "");
    // Mistral native:  [TOOL_CALLS][{...}]
    text = text.replace(/\[TOOL_CALLS\]\s*\[[\s\S]*?\]/gi, "");
    // Gemma 4 native: <|tool_call>call:NAME{...}<tool_call|>
    text = text.replace(/<\|tool_call>[\s\S]*?(?:<tool_call\|>)/gi, "");
    // Gemma python dialect: ```tool_code\n name(arg=value) \n``` (the bridge
    // parsed + executed this; strip the fence so the bubble isn't raw code).
    text = text.replace(/```tool_code[\s\S]*?```/gi, "");
    // Self-closing XML tag: <tool_call name="..." />
    text = text.replace(/<tool_call\s+[^>]*?\/>/gi, "");
    
    // Partial open-only forms while streaming (no closer yet): strip from the
    // open tag to end of text so raw tags don't flicker.
    text = text.replace(/<tool_call>[\s\S]*$/gi, "");
    text = text.replace(/<call:[a-zA-Z0-9_\-]+>[\s\S]*$/gi, "");
    text = text.replace(/<\|python_tag\|>[\s\S]*$/gi, "");
    text = text.replace(/<\|tool_call>[\s\S]*$/gi, "");
    text = text.replace(/```tool_code[\s\S]*$/gi, "");
    text = text.replace(/\[TOOL_CALLS\][\s\S]*$/gi, "");
    text = text.replace(/```tool_call[\s\S]*$/gi, "");
    text = text.replace(/```json\s*\{[\s\S]*?"name"[\s\S]*?\}\s*```/gi, "");
    text = text.replace(/```json\s*\{[\s\S]*?"function"[\s\S]*?\}\s*```/gi, "");
    text = text.replace(/<function_calls>[\s\S]*?<\/function_calls>/gi, "");
    text = text.replace(/<functions>[\s\S]*?<\/functions>/gi, "");
    text = text.replace(/<invoke>[\s\S]*?<\/invoke>/gi, "");
    text = text.replace(/<tool>[\s\S]*?<\/tool>/gi, "");
    // {name, arguments} / [..{name, function}..] blobs, via the linear scanner.
    text = stripJsonToolCallShapes(text);
    text = text.replace(/\*\*Tool call:.*?\*\*/gi, "");
    text = text.replace(/\*\*Function call:.*?\*\*/gi, "");
    text = text.replace(/Calling\s+\w+\s*\(.*?\)\s*\.\.\./gi, "");
    text = text.replace(/\[\s*\d+\s*tool\s*calls?\s*\]/gi, "");
    text = text.replace(/\n{3,}/g, "\n\n");
    text = text.trim();

    // Extract code fences, line-anchored (see extractLineAnchoredFences).
    const fences = [];
    text = extractLineAnchoredFences(text, fences);

    // Short-term memory markers: bridge.py wraps prior <think> blocks as
    // [scratchpad-from-earlier-turn]...[/scratchpad-from-earlier-turn] (the wire
    // name stays for older histories; the UI calls it "short-term memory").
    // Render inline as an icon + italic body instead of the literal tags.
    const stmBlocks = [];
    text = text.replace(
      /\[scratchpad-from-earlier-turn\]([\s\S]*?)\[\/scratchpad-from-earlier-turn\]/gi,
      (_m, body) => {
        stmBlocks.push((body || "").trim());
        return `\x00S${stmBlocks.length - 1}\x00`;
      }
    );

    // Strip bare inline formatting HTML from chatty models (Gemma sprinkles <b>,
    // <i>, <br>, <p> between sentences). Rendering goes through markdown-lite +
    // esc(), so these would show as literal text. Fences are already placeholders,
    // so code is untouched. Only zero-arg formatting tags are stripped; anything
    // else is left for esc() to show literally, the safer default.
    text = text.replace(/<\/?(?:b|i|u|em|strong|small|big|sub|sup|mark|ins|del|s|strike)\s*>/gi, "");
    text = text.replace(/<br\s*\/?\s*>/gi, "\n");
    text = text.replace(/<\/?p\s*>/gi, "\n\n");
    // collapse any extra blank lines those replacements introduced
    text = text.replace(/\n{3,}/g, "\n\n").trim();

    let escaped = esc(text);

    // --- block-level passes (work on escaped text, line-by-line) ---
    const lines = escaped.split("\n");
    const blocks = [];
    let i2 = 0;
    while (i2 < lines.length) {
      const ln = lines[i2];
      // pipe table: header row + separator (| --- | --- |) + body rows
      if (/^\s*\|.*\|\s*$/.test(ln) && i2 + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(lines[i2 + 1])) {
        const headerCells = ln.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const sepCells = lines[i2 + 1].trim().replace(/^\||\|$/g, "").split("|");
        const aligns = sepCells.map(c => {
          const t = c.trim();
          if (t.startsWith(":") && t.endsWith(":")) return "center";
          if (t.endsWith(":")) return "right";
          return "left";
        });
        const rows = [];
        let j = i2 + 2;
        while (j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j])) {
          rows.push(lines[j].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
          j++;
        }
        const inline = (s) => s
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(/\*([^*]+)\*/g, "<em>$1</em>")
          .replace(/`([^`]+)`/g, "<code>$1</code>");
        const thead = "<thead><tr>" + headerCells.map((c, k) => `<th style="text-align:${aligns[k] || "left"}">${inline(c)}</th>`).join("") + "</tr></thead>";
        const tbody = "<tbody>" + rows.map(r => "<tr>" + r.map((c, k) => `<td style="text-align:${aligns[k] || "left"}">${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody>";
        blocks.push(`<div class="md-table-wrapper"><table class="md-table">${thead}${tbody}</table></div>`);
        i2 = j;
        continue;
      }
      // ATX headings
      const h = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(ln);
      if (h) {
        const lvl = h[1].length;
        blocks.push(`<h${lvl} class="md-h${lvl}">${h[2]}</h${lvl}>`);
        i2++;
        continue;
      }
      // horizontal rule
      if (/^\s*(---|\*\*\*|___)\s*$/.test(ln)) {
        blocks.push(`<hr class="md-hr">`);
        i2++;
        continue;
      }
      // blockquote ("> " arrives escaped)
      if (/^\s*&gt;\s?/.test(ln)) {
        const quoted = [];
        while (i2 < lines.length && /^\s*&gt;\s?/.test(lines[i2])) {
          quoted.push(lines[i2].replace(/^\s*&gt;\s?/, ""));
          i2++;
        }
        blocks.push(`<blockquote class="md-quote">${quoted.join("<br>")}</blockquote>`);
        continue;
      }
      // lists, bulleted or numbered, nested by indentation
      if (_MD_LIST_ITEM.test(ln)) {
        const items = [];
        while (i2 < lines.length) {
          const m = _MD_LIST_ITEM.exec(lines[i2]);
          if (m) {
            items.push({ indent: m[1].replace(/\t/g, "    ").length, ordered: /\d/.test(m[2]),
              num: parseInt(m[2], 10), text: m[3] });
            i2++;
            continue;
          }
          // an indented line right under an item continues that item
          if (items.length && /^\s{2,}\S/.test(lines[i2])) {
            items[items.length - 1].text += "<br>" + lines[i2].trim();
            i2++;
            continue;
          }
          break;
        }
        blocks.push(_mdListHtml(items));
        continue;
      }
      // accumulate paragraph lines until blank
      const para = [];
      while (i2 < lines.length && lines[i2].trim() !== "" &&
             !/^\s*\|.*\|\s*$/.test(lines[i2]) &&
             !/^#{1,6}\s+/.test(lines[i2]) &&
             !/^\s*(---|\*\*\*|___)\s*$/.test(lines[i2]) &&
             !/^\s*&gt;\s?/.test(lines[i2]) &&
             !_MD_LIST_ITEM.test(lines[i2])) {
        para.push(lines[i2]);
        i2++;
      }
      if (para.length) {
        blocks.push("<p>" + para.join("<br>") + "</p>");
      } else {
        i2++;
      }
    }

    let out = blocks.join("")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, (_m, alt, src) => {
        const sourceUrl = state.imageUrlToSourceMap?.get(src);
        const imgHtml = `<img src="${src}" alt="${alt}">`;
        if (sourceUrl) {
          return `<a href="${sourceUrl}" target="_blank" rel="noopener noreferrer" class="img-source-link">${imgHtml}</a>`;
        }
        return imgHtml;
      });

    // Group consecutive images (or image-source links) into a scrollable horizontal grid gallery
    out = out.replace(/(?:(?:<a[^>]*class="img-source-link"[^>]*><img[^>]+><\/a>|<img[^>]+>)\s*){2,}/g, (match) => {
      return `<div class="md-image-grid">${match}</div>`;
    });

    // Autolink URLs into chips with hover preview, before fence restoration so
    // URLs inside ``` blocks stay plain. URLs inside tag attributes, <a> or <code>
    // are skipped.
    out = autolinkChatHtml(out);

    out = out.replace(/\x00F(\d+)\x00/g, (_, i) => {
      const { infoStr, code } = fences[+i];
      // Skip empty fences (mid-stream before the code arrives, or a bare ``` ```
      // pair); they would render as a blank card with a lone copy button.
      if (!code || !code.trim()) return "";
      const langLabel = (infoStr || "").trim().split(/\s+/)[0] || "";
      let displayLang = langLabel ? langLabel.toLowerCase() : "text";
      let src = code;
      // Untagged fence: recover the language so the block still colorizes.
      if (displayLang === "text") {
        // Language written on its own first line (```<newline>python)? Strip it.
        // Otherwise leave "text" and let highlight.js auto-detect.
        const bare = src.match(/^[ \t]*([A-Za-z][A-Za-z0-9+#.]*)[ \t]*\r?\n/);
        const first = bare && bare[1].toLowerCase();
        if (first && _isKnownLang(first) && src.slice(bare[0].length).trim()) {
          displayLang = LANG_ALIAS[first] || first;
          src = src.slice(bare[0].length);
        }
      }
      if (displayLang === "diff") {
        const dpm = (infoStr || "").match(/path=([^\s]+)/i);
        return renderDiffCard(src, dpm ? dpm[1] : "");
      }
      if (displayLang === "email") {
        const emailCard = renderEmailCard(src);
        if (emailCard) return emailCard;
      }
      const hl = highlightForCard(src, displayLang, { streaming });
      if (hl.lang) displayLang = hl.lang;
      const lined = wrapCodeLines(hl.html);
      // Dynamic single-line detection for super compact visual density
      const isSingleLine = src.trim().split('\n').length <= 1;
      const singleClass = isSingleLine ? " single-line" : "";
      
      // Parse path=<filename> from infoStr
      const pathMatch = (infoStr || "").match(/path=([^\s]+)/i);
      const filename = pathMatch ? pathMatch[1] : "";
      
      // Preview button only useful for HTML/SVG/XML — surface it conditionally.
      const previewBtn = /^(html|xml|svg)$/.test(displayLang)
        ? `<button type="button" class="cc-act cc-preview" title="Open this block in the preview pane"><i class="ph ph-monitor-play"></i><span>Preview</span></button>`
        : "";
      
      if (filename) {
        return `
          <div class="code-card-tabs-container" data-filename="${esc(filename)}">
            <div class="code-card-tabs-header">
              <div class="code-card-tab">
                <span class="purple-dot"></span>
                <span>${esc(filename)}</span>
              </div>
              <div class="code-card-tab-actions">
                <button type="button" class="cc-act cc-open-file" title="Open in workspace pane" data-filename="${esc(filename)}"><i class="ph ph-folder-open"></i><span>Open file</span></button>
                <button type="button" class="cc-act cc-copy" title="Copy"><i class="ph ph-copy"></i><span>Copy</span></button>
                ${previewBtn}
              </div>
            </div>
            <pre class="code-card has-header${singleClass}" data-lang="${esc(displayLang)}"><code class="code-card-body">${lined}</code></pre>
          </div>
        `.trim();
      }
      
      // Card layout: floating action cluster (top-right) plus a gutter+body row;
      // no separate header bar.
      return `<pre class="code-card${singleClass}" data-lang="${esc(displayLang)}"><div class="code-card-actions"><button type="button" class="cc-act cc-copy" title="Copy"><i class="ph ph-copy"></i></button>${previewBtn}</div><code class="code-card-body">${lined}</code></pre>`;
    });

    // restore short-term memory blocks as a small icon + italic body
    const STM_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="14 3 14 9 20 9"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>';
    out = out.replace(/\x00S(\d+)\x00/g, (_, i) => {
      const body = stmBlocks[+i] || "";
      const safe = esc(body).replace(/\n/g, "<br>");
      return `<span class="stm-block" title="model's short-term memory from an earlier turn"><span class="stm-tag">${STM_SVG}<span class="stm-label">short-term memory</span></span><em class="stm-body">${safe}</em></span>`;
    });
    return out;
  }
  return { renderMarkdown };
};
