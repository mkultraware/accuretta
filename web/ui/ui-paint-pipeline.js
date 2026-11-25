/* SPDX-License-Identifier: AGPL-3.0-only */
/* streaming paint pipeline: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["paint-pipeline"] = () => {
  // One paint per animation frame, coalesced across deltas. All DOM writes
  // for a delta batch happen here, aligned with the compositor.
  function scheduleStreamPaint(row, paint) {
    if (!row || !row.isConnected) { paint(); return; }
    row._paintNext = paint;   // always run the LATEST state
    if (row._paintRaf) return;
    row._paintRaf = requestAnimationFrame(() => {
      row._paintRaf = null;
      const fn = row._paintNext;
      row._paintNext = null;
      if (fn) {
        try { fn(); } catch (e) { console.error("stream paint failed", e); }
      }
    });
  }

  function cancelStreamPaint(row) {
    if (!row) return;
    if (row._paintRaf) cancelAnimationFrame(row._paintRaf);
    row._paintRaf = null;
    row._paintNext = null;
    if (row._paintRetry) clearTimeout(row._paintRetry);
    row._paintRetry = 0;
    if (row._pace?.raf) cancelAnimationFrame(row._pace.raf);
    row._pace = null;
    // The turn is over for this row: nothing may repaint the streaming view now,
    // or a late paint would put "Writing response" back over the finished line.
    row._streamClosed = true;
  }
  return { scheduleStreamPaint, cancelStreamPaint };
};
