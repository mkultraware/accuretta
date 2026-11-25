/* SPDX-License-Identifier: AGPL-3.0-only */
/* lightweight syntax highlighter: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["highlighter"] = ({ esc }) => {
  // Single-pass tokenizer for chat code fences, conservative on purpose (false
  // positives look worse than no highlighting). Per language: keyword set +
  // comment style; strings, numbers and punctuation come from the shared base
  // tokenizer. Emits escaped HTML ready for <code>; unknown languages fall back
  // to plain esc().
  const LANG_KEYWORDS = {
    js: new Set(("var let const function return if else for while do switch case break continue " +
      "new typeof instanceof in of delete void this super class extends static get set " +
      "import export from as default async await yield try catch finally throw " +
      "true false null undefined").split(/\s+/)),
    ts: new Set(("var let const function return if else for while do switch case break continue " +
      "new typeof instanceof in of delete void this super class extends implements interface type " +
      "enum static get set import export from as default async await yield try catch finally throw " +
      "public private protected readonly abstract namespace declare " +
      "true false null undefined string number boolean any void never unknown").split(/\s+/)),
    py: new Set(("def class return if elif else for while break continue pass import from as " +
      "with try except finally raise yield lambda global nonlocal in is not and or " +
      "True False None async await match case").split(/\s+/)),
    sh: new Set(("if then else elif fi for while do done case esac in function return " +
      "echo export local readonly set unset source exit").split(/\s+/)),
    bash: new Set(("if then else elif fi for while do done case esac in function return " +
      "echo export local readonly set unset source exit").split(/\s+/)),
    powershell: new Set(("if elseif else switch foreach for while do until break continue return " +
      "function param begin process end try catch finally throw " +
      "true false null").split(/\s+/)),
    ps1: new Set(("if elseif else switch foreach for while do until break continue return " +
      "function param begin process end try catch finally throw " +
      "true false null").split(/\s+/)),
    css: new Set(("important inherit initial unset auto none").split(/\s+/)),
    sql: new Set(("select from where insert update delete into values set join inner left right outer " +
      "on as group by order having limit offset distinct union all create table drop alter index").split(/\s+/)),
    json: new Set(("true false null").split(/\s+/)),
    c: new Set(("auto break case char const continue default do double else enum extern float for goto " +
      "if inline int long register restrict return short signed sizeof static struct switch typedef " +
      "union unsigned void volatile while sizeof _Bool _Static_assert").split(/\s+/)),
    cpp: new Set(("alignas alignof auto bool break case catch char class const constexpr const_cast continue " +
      "decltype default delete do double dynamic_cast else enum explicit extern false float for friend goto if " +
      "inline int long mutable namespace new noexcept nullptr operator private protected public register " +
      "reinterpret_cast return short signed sizeof static static_assert static_cast struct switch template this " +
      "throw true try typedef typeid typename union unsigned using virtual void volatile while").split(/\s+/)),
    rust: new Set(("as async await break const continue crate dyn else enum extern false fn for if impl in let " +
      "loop match mod move mut pub ref return self Self static struct super trait true type unsafe use where " +
      "while box").split(/\s+/)),
    go: new Set(("break case chan const continue default defer else fallthrough for func go goto if import " +
      "interface map package range return select struct switch type var true false nil iota").split(/\s+/)),
    java: new Set(("abstract assert boolean break byte case catch char class const continue default do double " +
      "else enum extends final finally float for goto if implements import instanceof int interface long native " +
      "new package private protected public return short static strictfp super switch synchronized this throw " +
      "throws transient try void volatile while true false null var record sealed").split(/\s+/)),
  };
  // Aliases that map to a base language
  const LANG_ALIAS = {
    javascript: "js", node: "js", jsx: "js",
    typescript: "ts", tsx: "ts",
    python: "py", py3: "py",
    shell: "sh", zsh: "sh", bash: "bash",
    pwsh: "powershell",
    yml: "yaml",
    "c++": "cpp", cxx: "cpp", cc: "cpp", h: "c", hpp: "cpp", golang: "go", rs: "rust",
  };
  // Comment styles per language (line + optional block)
  const LANG_COMMENTS = {
    js: { line: "//", block: ["/*", "*/"] },
    ts: { line: "//", block: ["/*", "*/"] },
    py: { line: "#", block: null },
    sh: { line: "#", block: null },
    bash: { line: "#", block: null },
    powershell: { line: "#", block: ["<#", "#>"] },
    css: { line: null, block: ["/*", "*/"] },
    sql: { line: "--", block: ["/*", "*/"] },
    yaml: { line: "#", block: null },
    rust: { line: "//", block: ["/*", "*/"] },
    go: { line: "//", block: ["/*", "*/"] },
    c: { line: "//", block: ["/*", "*/"] },
    cpp: { line: "//", block: ["/*", "*/"] },
    java: { line: "//", block: ["/*", "*/"] },
  };

  function highlightCode(rawCode, lang) {
    const code = String(rawCode == null ? "" : rawCode);
    const baseLang = LANG_ALIAS[lang] || lang || "";
    // HTML/XML get a dedicated path (tag/attr/string).
    if (baseLang === "html" || baseLang === "xml" || baseLang === "svg") {
      return highlightMarkup(code);
    }
    const kw = LANG_KEYWORDS[baseLang] || null;
    const cmt = LANG_COMMENTS[baseLang] || null;
    // No spec for this language → return safely escaped only.
    if (!kw && !cmt) return esc(code);

    let out = "";
    let i = 0;
    const n = code.length;
    const isIdStart = c => /[A-Za-z_$]/.test(c);
    const isIdCont  = c => /[A-Za-z0-9_$]/.test(c);

    while (i < n) {
      const c = code[i];

      // Block comments
      if (cmt && cmt.block && code.startsWith(cmt.block[0], i)) {
        const end = code.indexOf(cmt.block[1], i + cmt.block[0].length);
        const stop = end === -1 ? n : end + cmt.block[1].length;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      // Line comments
      if (cmt && cmt.line && code.startsWith(cmt.line, i)) {
        const nl = code.indexOf("\n", i);
        const stop = nl === -1 ? n : nl;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      // Strings: ", ', `
      if (c === '"' || c === "'" || c === "`") {
        const quote = c;
        let j = i + 1;
        while (j < n) {
          if (code[j] === "\\" && j + 1 < n) { j += 2; continue; }
          if (code[j] === quote) { j++; break; }
          j++;
        }
        out += `<span class="tok-string">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Numbers (basic — int / float / hex)
      if (/\d/.test(c) || (c === "." && /\d/.test(code[i + 1] || ""))) {
        let j = i;
        if (c === "0" && /[xX]/.test(code[i + 1] || "")) {
          j = i + 2;
          while (j < n && /[0-9a-fA-F_]/.test(code[j])) j++;
        } else {
          while (j < n && /[0-9_]/.test(code[j])) j++;
          if (code[j] === "." && /\d/.test(code[j + 1] || "")) {
            j++;
            while (j < n && /[0-9_]/.test(code[j])) j++;
          }
          if (/[eE]/.test(code[j] || "")) {
            j++;
            if (/[+-]/.test(code[j] || "")) j++;
            while (j < n && /\d/.test(code[j])) j++;
          }
        }
        out += `<span class="tok-number">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Identifiers (keywords / function calls)
      if (isIdStart(c)) {
        let j = i + 1;
        while (j < n && isIdCont(code[j])) j++;
        const word = code.slice(i, j);
        if (kw && kw.has(word)) {
          out += `<span class="tok-keyword">${esc(word)}</span>`;
        } else if (code[j] === "(") {
          out += `<span class="tok-fn">${esc(word)}</span>`;
        } else {
          out += esc(word);
        }
        i = j;
        continue;
      }
      // Shell variable: $name or ${name}
      if (c === "$" && (baseLang === "sh" || baseLang === "bash")) {
        let j = i + 1;
        if (code[j] === "{") {
          const end = code.indexOf("}", j);
          j = end === -1 ? n : end + 1;
        } else {
          while (j < n && isIdCont(code[j])) j++;
        }
        out += `<span class="tok-var">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // PowerShell variable: $name
      if (c === "$" && (baseLang === "powershell")) {
        let j = i + 1;
        while (j < n && (isIdCont(code[j]) || code[j] === ":")) j++;
        out += `<span class="tok-var">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Python decorator
      if (c === "@" && baseLang === "py" && isIdStart(code[i + 1] || "")) {
        let j = i + 1;
        while (j < n && (isIdCont(code[j]) || code[j] === ".")) j++;
        out += `<span class="tok-decorator">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Punctuation / whitespace — passthrough (escaped)
      out += esc(c);
      i++;
    }
    return out;
  }

  // Does the highlighter have a spec for this language (or alias)?
  function _isKnownLang(l) {
    const b = LANG_ALIAS[l] || l;
    return b === "html" || b === "xml" || b === "svg" || b === "css" || b === "yaml" ||
      !!LANG_KEYWORDS[b] || !!LANG_COMMENTS[b];
  }

  // Sniff a language for UNTAGGED fences (the model forgot to label the block)
  // so it still colorizes. Conservative: returns "" when the signal is weak,
  // which leaves the block as plain text rather than mis-coloring it.
  function detectLang(code) {
    const s = String(code || "").slice(0, 4000);
    const has = re => re.test(s);
    if (has(/#include\s*[<"]/) || has(/\b(?:struct|enum|union|typedef)\s+\w+\s*\{/) ||
        has(/\b__(?:le|be)\d+\b/) || has(/\b(?:uint\d+_t|size_t|int|char|void)\s+\**\w+\s*[;(=]/))
      return has(/\b(?:class|namespace|template|std::)\b|::/) ? "cpp" : "c";
    if (has(/^\s*def\s+\w+\s*\(/m) || has(/^\s*(?:from\s+[\w.]+\s+)?import\s+\w/m) ||
        has(/^\s*class\s+\w+\s*[:(]/m) || (has(/\bself\b/) && has(/:\s*$/m)))
      return "py";
    if (has(/\bfn\s+\w+\s*\(/) && has(/\blet\s+(?:mut\s+)?\w+/)) return "rust";
    if (has(/\bpackage\s+\w+/) && has(/\bfunc\s+\w*\s*\(/)) return "go";
    if (has(/^#!.*\b(?:ba|z|k)?sh\b/m) || has(/^\s*(?:sudo|apt|npm|yarn|git|curl|wget|cd|echo|export|mkdir|chmod)\b/m))
      return "bash";
    if (has(/\b(?:Get|Set|New|Remove|Write|Invoke)-\w+\b/) || has(/\$\w+\s*=[^=]/) && has(/\|/)) return "powershell";
    if (has(/=>/) || has(/\bconsole\.\w+/) || has(/\b(?:const|let|var)\s+\w+\s*=/) || has(/\bfunction\s*\w*\s*\(/))
      return (has(/:\s*(?:string|number|boolean|any|void)\b/) || has(/\binterface\s+\w+/)) ? "ts" : "js";
    if (has(/\bSELECT\b[\s\S]*\bFROM\b/i) || has(/\b(?:INSERT\s+INTO|CREATE\s+TABLE|UPDATE\s+\w+\s+SET)\b/i))
      return "sql";
    if (has(/^\s*[{[]/) && has(/"[\w-]+"\s*:/)) return "json";
    return "";
  }

  // Resolve a fence language to a highlight.js language id, or "" if hljs
  // doesn't know it (in which case we auto-detect instead).
  function _hljsLang(lang) {
    const hl = window.hljs;
    if (!hl || !lang || lang === "text") return "";
    try { return hl.getLanguage(lang) ? lang : ""; } catch (_) { return ""; }
  }

  // Highlight a chat code block. Prefers highlight.js — real grammar-based
  // tokenizing across ~40 languages, plus auto-detection for untagged blocks —
  // and falls back to the built-in lightweight highlighter when hljs isn't
  // loaded (offline / CDN blocked). Returns { html, lang }.
  function highlightForCard(src, lang, opts = {}) {
    const streaming = !!opts.streaming;
    const hl = window.hljs;
    if (hl) {
      try {
        const known = _hljsLang(lang);
        if (known) return { html: hl.highlight(src, { language: known, ignoreIllegals: true }).value, lang: known };
        // highlightAuto tries EVERY language — far too slow to run per paint
        // on a growing untagged block. During streaming fall through to the
        // cheap built-in detector; the final render auto-detects once.
        if (!streaming) {
          const auto = hl.highlightAuto(src);
          if (auto && auto.value) return { html: auto.value, lang: auto.language || (lang === "text" ? "" : lang) || "text" };
        }
      } catch (_) { /* fall through to the built-in highlighter */ }
    }
    let l = lang;
    if (l === "text" || !_isKnownLang(l)) { const d = detectLang(src); if (d) l = d; }
    return { html: highlightCode(src, l), lang: l };
  }

  // Split highlighted HTML on \n while keeping multi-line token spans
  // (docstrings, block comments) properly closed before the break and
  // reopened on the next line so coloring stays continuous. Returns an
  // array of per-line HTML strings — wrap them however the caller wants.
  function splitHighlightedLines(html) {
    const lines = [];
    let cur = "";
    let openTag = null;
    let i = 0;
    const n = html.length;
    while (i < n) {
      const c = html[i];
      if (c === "<") {
        const end = html.indexOf(">", i);
        if (end === -1) { cur += html.slice(i); break; }
        const tag = html.slice(i, end + 1);
        if (tag.startsWith("</span")) openTag = null;
        else if (tag.startsWith("<span")) openTag = tag;
        cur += tag;
        i = end + 1;
        continue;
      }
      if (c === "\n") {
        if (openTag) cur += "</span>";
        lines.push(cur);
        cur = openTag ? openTag : "";
        i++;
        continue;
      }
      cur += c;
      i++;
    }
    if (cur.length || lines.length === 0) {
      if (openTag) cur += "</span>";
      lines.push(cur);
    }
    return lines;
  }

  // Wrap each line of the highlighted-HTML output in <span class="code-line">
  // so the line-number gutter (CSS counters) can index them and the body can
  // wrap visually if the user resizes the bubble. If a token <span> straddles
  // a newline (multi-line strings, block comments), we close it before the
  // break and reopen it on the next line so coloring stays continuous.
  function wrapCodeLines(html) {
    const lines = [];
    let cur = "";
    const stack = []; // open <span ...> tags, outermost first — hljs nests them
    let i = 0;
    const n = html.length;
    while (i < n) {
      const c = html[i];
      if (c === "<") {
        const end = html.indexOf(">", i);
        if (end === -1) { cur += html.slice(i); break; }
        const tag = html.slice(i, end + 1);
        if (/^<\/span/i.test(tag)) stack.pop();
        else if (/^<span\b/i.test(tag)) stack.push(tag);
        cur += tag;
        i = end + 1;
        continue;
      }
      if (c === "\n") {
        // Close open spans before the break, reopen them after, so multi-line
        // tokens keep their color and the gutter can index each row.
        for (let k = stack.length - 1; k >= 0; k--) cur += "</span>";
        lines.push(cur);
        cur = stack.join("");
        i++;
        continue;
      }
      cur += c;
      i++;
    }
    for (let k = stack.length - 1; k >= 0; k--) cur += "</span>";
    if (cur.length || lines.length === 0) lines.push(cur);
    // Each line: a row span with optional inner content. Empty lines render
    // as a blank row — we still want a number for them.
    return lines.map(line => `<span class="code-line">${line || "\u200b"}</span>`).join("");
  }

  // Markup highlighter for html/xml/svg fences.
  function highlightMarkup(code) {
    // Walk the source, treating <...> as tag spans with attribute tokenizing
    // inside. Outside of tags, just escape the body text. Comments and
    // doctype get their own classes.
    let out = "";
    let i = 0;
    const n = code.length;
    while (i < n) {
      // Comment
      if (code.startsWith("<!--", i)) {
        const end = code.indexOf("-->", i + 4);
        const stop = end === -1 ? n : end + 3;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      if (code[i] === "<") {
        const end = code.indexOf(">", i);
        if (end === -1) {
          out += esc(code.slice(i));
          break;
        }
        const tag = code.slice(i, end + 1);
        // tokenize the tag: <tagname attr="value" attr=value>
        let inner = "";
        const m = tag.match(/^<\s*\/?\s*([a-zA-Z][\w:-]*)?/);
        const tagName = m && m[1] ? m[1] : "";
        let pos = 0;
        const lt = tag.match(/^<\s*\/?\s*/)[0];
        inner += `<span class="tok-punct">${esc(lt)}</span>`;
        pos = lt.length;
        if (tagName) {
          inner += `<span class="tok-tag">${esc(tagName)}</span>`;
          pos += tagName.length;
        }
        // attribute tokens: name(=value)?
        while (pos < tag.length - 1) {
          const rest = tag.slice(pos, tag.length - 1);
          const ws = rest.match(/^\s+/);
          if (ws) { inner += esc(ws[0]); pos += ws[0].length; continue; }
          const am = rest.match(/^([a-zA-Z_:][\w:.-]*)/);
          if (am) {
            inner += `<span class="tok-attr">${esc(am[1])}</span>`;
            pos += am[1].length;
            const after = tag.slice(pos, tag.length - 1);
            const eq = after.match(/^\s*=\s*/);
            if (eq) {
              inner += `<span class="tok-punct">${esc(eq[0])}</span>`;
              pos += eq[0].length;
              const after2 = tag.slice(pos, tag.length - 1);
              const sm = after2.match(/^("[^"]*"|'[^']*'|[^\s>]+)/);
              if (sm) {
                inner += `<span class="tok-string">${esc(sm[1])}</span>`;
                pos += sm[1].length;
              }
            }
            continue;
          }
          // unknown char in tag — passthrough
          inner += esc(rest[0]);
          pos += 1;
        }
        // closing >
        inner += `<span class="tok-punct">${esc(tag.slice(tag.length - 1))}</span>`;
        out += inner;
        i = end + 1;
        continue;
      }
      // Body text up to next "<"
      const next = code.indexOf("<", i);
      const stop = next === -1 ? n : next;
      out += esc(code.slice(i, stop));
      i = stop;
    }
    return out;
  }

  // Linear scanner that finds `<tool_call>{"name":"write_file","arguments":
  // {..."content":"<HTML>","path":...}}</tool_call>` blobs and rewrites them to
  // a clean ```html``` fence. One indexOf walk, no regex backtracking, safe on
  // multi-MB input. Tolerates a missing </tool_call>, either argument order and
  // trailing truncation. The HTML body is JSON-unescaped on the way out.
  function decodeJsonStringBody(body) {
    const SENT = "\x00BS\x00";
    return body
      .split("\\\\").join(SENT)
      .split("\\n").join("\n")
      .split("\\r").join("\r")
      .split("\\t").join("\t")
      .split('\\"').join('"')
      .split("\\'").join("'")
      .split("\\/").join("/")
      .split(SENT).join("\\");
  }
  function rewriteWriteFileToolCallToFence(text) {
    if (!text || text.indexOf("write_file") === -1) return text;
    let out = "";
    let i = 0;
    const n = text.length;
    while (i < n) {
      // Find the next `<tool_call>`; the format is fixed by the prompt, so a
      // lowercase indexOf is enough.
      const tcStart = text.indexOf("<tool_call>", i);
      if (tcStart === -1) { out += text.slice(i); break; }
      // Quickly check if this tool_call mentions write_file before doing the
      // heavier scan — bail and keep the text untouched if not.
      const tcEndCandidate = text.indexOf("</tool_call>", tcStart);
      const sniffEnd = tcEndCandidate === -1 ? Math.min(n, tcStart + 200) : tcEndCandidate;
      if (text.slice(tcStart, sniffEnd).indexOf('"write_file"') === -1) {
        // Not us — copy through this opener and keep going past it.
        out += text.slice(i, tcStart + "<tool_call>".length);
        i = tcStart + "<tool_call>".length;
        continue;
      }
      // Find `"content"` somewhere after the opener.
      const contentKey = text.indexOf('"content"', tcStart);
      if (contentKey === -1) {
        // malformed — emit the opener and keep going (the strippers below
        // will clean it).
        out += text.slice(i, tcStart + "<tool_call>".length);
        i = tcStart + "<tool_call>".length;
        continue;
      }
      // Skip whitespace + colon + whitespace, expect an opening `"` for the value.
      let j = contentKey + '"content"'.length;
      while (j < n && (text[j] === ' ' || text[j] === '\t' || text[j] === '\n' || text[j] === '\r')) j++;
      if (text[j] !== ':') { out += text.slice(i, tcStart); i = tcStart; break; }
      j++;
      while (j < n && (text[j] === ' ' || text[j] === '\t' || text[j] === '\n' || text[j] === '\r')) j++;
      if (text[j] !== '"') { out += text.slice(i, tcStart); i = tcStart; break; }
      j++; // now positioned at first char of the JSON-string body
      const bodyStart = j;
      // Walk the body, honoring `\` escapes, until we hit an unescaped `"`
      // OR end-of-text (truncated). Linear, no backtracking.
      let bodyEnd = -1;
      while (j < n) {
        const c = text.charCodeAt(j);
        if (c === 92 /* \ */) { j += 2; continue; }
        if (c === 34 /* " */) { bodyEnd = j; break; }
        j++;
      }
      const body = text.slice(bodyStart, bodyEnd === -1 ? n : bodyEnd);
      // Find end of the whole tool_call to skip past it. If no closer, eat
      // through any trailing `}}</tool_call>` tail or to end-of-text.
      let blockEnd;
      if (bodyEnd === -1) {
        blockEnd = n;
      } else {
        const closer = text.indexOf("</tool_call>", bodyEnd);
        blockEnd = closer === -1 ? n : closer + "</tool_call>".length;
      }
      // Emit the prefix, the rewritten fence, then continue scanning.
      out += text.slice(i, tcStart);
      const decoded = decodeJsonStringBody(body);
      out += "\n```html\n" + decoded + "\n```\n";
      i = blockEnd;
    }
    return out;
  }

  // Some local fine-tunes emit fenced HTML/code already JSON-string-escaped
  // (real newlines become the two characters `\n`, quotes become `\"`), which
  // shows up as literal backslashes in the preview and a one-line code card.
  // Detect heavy escape usage with very few real newlines and decode in one
  // pass (a sentinel keeps `\\` from double-processing). Mirrors
  // _maybe_unescape_json_html in bridge.py.
  function maybeUnescapeJsonFence(code) {
    if (!code || code.indexOf("\\") === -1) return code;
    const realNewlines = (code.match(/\n/g) || []).length;
    const escapedN = (code.match(/\\n/g) || []).length;
    const escapedQuote = (code.match(/\\"/g) || []).length;
    if (escapedN < 3 && escapedQuote < 3) return code;
    if (realNewlines >= Math.max(5, Math.floor(escapedN / 2))) return code;
    const SENT = "\x00BS\x00";
    return code
      .split("\\\\").join(SENT)
      .split("\\n").join("\n")
      .split("\\r").join("\r")
      .split("\\t").join("\t")
      .split('\\"').join('"')
      .split("\\'").join("'")
      .split("\\/").join("/")
      .split(SENT).join("\\");
  }
  return {
    LANG_ALIAS, highlightCode, _isKnownLang, highlightForCard, splitHighlightedLines, wrapCodeLines,
    rewriteWriteFileToolCallToFence, maybeUnescapeJsonFence,
  };
};
