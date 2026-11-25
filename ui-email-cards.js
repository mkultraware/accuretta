/* SPDX-License-Identifier: AGPL-3.0-only */
/* email cards (```email fences): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["email-cards"] = ({ esc }) => {
  // A draft (or a quoted message) written as mail headers, a blank line, then
  // the body. Rendered as a letter instead of a code block. A fence with no
  // recognisable header returns "" and falls back to the plain code card.
  const EMAIL_HEADER_RE = /^[*_\s]*(from|to|cc|bcc|reply-to|subject|date)[*_\s]*:[*_]*\s*(.*)$/i;
  function parseEmailFence(raw) {
    const lines = String(raw).replace(/\r\n?/g, "\n").replace(/^\n+/, "").split("\n");
    const headers = {};
    let i = 0, last = "";
    for (; i < lines.length; i++) {
      const ln = lines[i];
      if (!ln.trim()) { i++; break; }
      const m = EMAIL_HEADER_RE.exec(ln);
      if (m) { last = m[1].toLowerCase(); headers[last] = m[2].replace(/[*_]+$/, "").trim(); continue; }
      if (last && /^[ \t]+\S/.test(ln)) { headers[last] += " " + ln.trim(); continue; }   // folded header
      break;   // first non-header line starts the body
    }
    if (!Object.keys(headers).length) return null;
    return { headers, body: lines.slice(i).join("\n").replace(/^\n+|\s+$/g, "") };
  }

  // "Name <a@b.c>, d@e.f" -> name in full ink, address quieter. Splits only on
  // separators outside quotes and angle brackets ("Doe, Jane" <j@x.y> is one).
  function emailAddressHtml(value) {
    const parts = [];
    let cur = "", inQuote = false, inAngle = false;
    for (const ch of String(value)) {
      if (ch === '"') inQuote = !inQuote;
      else if (ch === "<" && !inQuote) inAngle = true;
      else if (ch === ">" && !inQuote) inAngle = false;
      if ((ch === "," || ch === ";") && !inQuote && !inAngle) { parts.push(cur); cur = ""; }
      else cur += ch;
    }
    parts.push(cur);
    return parts.map(p => p.trim()).filter(Boolean).map(p => {
      const m = /^"?([^"<]*?)"?\s*<([^>]+)>$/.exec(p);
      if (m && m[1].trim())
        return `<span class="email-who">${esc(m[1].trim())}</span> <span class="email-addr">${esc(m[2].trim())}</span>`;
      return `<span class="email-who">${esc(m ? m[2].trim() : p)}</span>`;
    }).join(`<span class="email-sep">, </span>`);
  }

  // Body stays plain text; runs of ">" lines become a quoted block.
  function emailBodyHtml(body) {
    const out = [];
    let buf = [], quoted = false;
    const flush = () => {
      const text = buf.join("\n").replace(/^\n+|\n+$/g, "");
      buf = [];
      if (!text) return;
      out.push(`<div class="${quoted ? "email-quote" : "email-text"}">${esc(text)}</div>`);
    };
    for (const ln of String(body).split("\n")) {
      const isQuote = /^\s*>/.test(ln);
      if (isQuote !== quoted) { flush(); quoted = isQuote; }
      buf.push(isQuote ? ln.replace(/^\s*>\s?/, "") : ln);
    }
    flush();
    return out.join("");
  }

  function renderEmailCard(raw) {
    const mail = parseEmailFence(raw);
    if (!mail) return "";
    const h = mail.headers;
    const fields = [["from", "From"], ["to", "To"], ["cc", "Cc"], ["bcc", "Bcc"], ["reply-to", "Reply to"]]
      .filter(([key]) => h[key])
      .map(([key, label]) => `<dt>${label}</dt><dd>${emailAddressHtml(h[key])}</dd>`)
      .join("");
    // No Date header means it has not been sent: it is a draft.
    const kind = h.date ? esc(h.date) : "Draft";
    const subject = h.subject
      ? `<div class="email-subject">${esc(h.subject)}</div>`
      : `<div class="email-subject is-empty">No subject</div>`;
    return `<div class="email-card${h.date ? "" : " is-draft"}">` +
      `<textarea class="email-raw" hidden>${esc(mail.body)}</textarea>` +
      `<div class="email-head">` +
        `<div class="email-top"><span class="email-kind">${kind}</span>` +
        `<button type="button" class="cc-act cc-copy" title="Copy message text"><i class="ph ph-copy"></i></button></div>` +
        subject +
        (fields ? `<dl class="email-fields">${fields}</dl>` : "") +
      `</div>` +
      (mail.body ? `<div class="email-body">${emailBodyHtml(mail.body)}</div>` : "") +
    `</div>`;
  }

  // Paragraph break the app inserts between agent steps (see tool_start), led by
  // an invisible separator (U+2063) so it can be told apart from model text.
  const STEP_BREAK = "⁣\n\n";
  const STEP_BREAK_RE = /⁣\n\n/g;

  // Working notes are model narration, not a deliverable. Render them as
  // escaped prose so stray backticks cannot turn whole thoughts into code UI.
  function renderWorkingNotes(text) {
    if (!text) return "";
    const plain = String(text)
      .replace(/⁣/g, "")
      .replace(/(^|\n)[ \t]*```[^\n]*\n?/g, "$1")
      .replace(/```/g, "")
      .replace(/`+/g, "")
      .replace(/\r\n?/g, "\n")
      .trim();
    if (!plain) return "";
    return plain.split(/\n{2,}/)
      .map(block => `<p>${esc(block).replace(/\n/g, "<br>")}</p>`)
      .join("");
  }

  function renderWorkingNotesBlock(text) {
    const html = renderWorkingNotes(text);
    return html ? `<div class="answer-interim">${html}</div>` : "";
  }
  return { renderEmailCard, STEP_BREAK, STEP_BREAK_RE, renderWorkingNotesBlock };
};
