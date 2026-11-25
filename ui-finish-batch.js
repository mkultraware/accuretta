/* SPDX-License-Identifier: AGPL-3.0-only */
/* finish batch: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["finish-batch"] = ({ scrollToBottom, splitCascade, splitThinking }) => {
  // The final SSE event, the turn_changes card and the think-line flip land
  // within milliseconds. Writing each synchronously costs a layout + scroll chase
  // per write, so collect a row's finish writes, flush them in one frame and
  // scroll once (follow-respecting; the ResizeObserver backstops late height
  // changes).
  function scheduleFinishBatch(row, fn) {
    if (!row || !row.isConnected) { try { fn(); } catch (e) { console.error("finish batch failed", e); } return; }
    (row._finishBatch ||= []).push(fn);
    if (row._finishRaf) return;
    row._finishRaf = requestAnimationFrame(() => {
      row._finishRaf = null;
      const fns = row._finishBatch;
      row._finishBatch = null;
      if (!row.isConnected || !fns) return;
      try { for (const f of fns) f(); } catch (e) { console.error("finish batch failed", e); }
      scrollToBottom();
    });
  }

  // splitThinking memo — the base-split below re-parses the SAME prefix
  // every paint (buf only grows at the tail), so cache by input identity.
  function splitThinkingMemo(row, buf) {
    const m = row._stMemo;
    if (m && m.buf === buf) return m.res;
    const res = splitThinking(buf);
    row._stMemo = { buf, res };
    return res;
  }

  function baseSplitMemo(row, newBuf, base) {
    if (!base) return "";
    const src = newBuf.slice(0, base);
    const m = row._baseMemo;
    if (m && m.base === base && m.src === src) return m.out;
    const out = splitCascade(splitThinking(src).content).content;
    row._baseMemo = { base, src, out };
    return out;
  }
  return { scheduleFinishBatch, splitThinkingMemo, baseSplitMemo };
};
