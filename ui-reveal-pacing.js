/* SPDX-License-Identifier: AGPL-3.0-only */
/* reveal pacing: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["reveal-pacing"] = ({
  cancelStreamPaint, paintStreamDelta, scheduleStreamPaint,
}) => {
  // Deltas arrive at the model's pace, and fast setups deliver them in bursts (a
  // draft model confirms several tokens at once: Inkling on Maya runs ~150 tok/s
  // in lumps of ~12). Painting whatever had arrived showed those as blocks. The
  // pacer reveals the received text a little every frame at the measured arrival
  // speed, never more than STREAM_PACE_MAX_LAG_MS behind, so a burst spreads out
  // while a slow model reads as before. Any event other than plain streaming text
  // first reveals everything (streamPaceFlush), so tool steps, steering and the
  // final render see the same buffer they always did.
  const STREAM_PACE_MAX_LAG_MS = 300;
  const STREAM_PACE_WINDOW_MS = 1500;
  const STREAM_PACE_MIN_CPS = 30;
  const STREAM_PACE_PASSTHROUGH = new Set([
    "delta", "thinking_delta", "heartbeat", "ctx_fill", "stats", "savings",
    "tool_progress", "tool_stream", "subagent_progress", "plan", "chat:note",
  ]);

  // What the painter shows: the received buffer up to the revealed length.
  function pacedBuf(ctx) {
    const buf = ctx.getBuf();
    const p = ctx.row && ctx.row._pace;
    if (!p) return buf;
    if (p.shown > buf.length) p.shown = buf.length;
    return p.shown >= buf.length ? buf : buf.slice(0, p.shown);
  }

  function streamPaceArrival(ctx, chars) {
    const row = ctx.row;
    const p = row._pace || (row._pace = { shown: 0, carry: 0, arrivals: [], raf: 0, last: 0, ctx: null });
    const now = performance.now();
    p.ctx = ctx;   // getBuf reads the live buffer, whichever event's ctx this is
    p.arrivals.push([now, chars]);
    while (p.arrivals.length > 1 && now - p.arrivals[0][0] > STREAM_PACE_WINDOW_MS) p.arrivals.shift();
    if (!p.raf) {
      p.last = now;
      p.raf = requestAnimationFrame(() => streamPaceTick(row));
    }
  }

  function streamPaceTick(row) {
    const p = row && row._pace;
    if (!p || !p.ctx) return;
    p.raf = 0;
    const total = p.ctx.getBuf().length;
    if (p.shown > total) p.shown = total;
    const now = performance.now();
    if (!row.isConnected) { p.shown = total; return; }   // switched away: nothing to animate
    const backlog = total - p.shown;
    if (backlog <= 0) { p.last = now; return; }
    const dt = Math.min(100, Math.max(0, now - (p.last || now)));
    p.last = now;
    let arrivalCps = 0;
    if (p.arrivals.length) {
      const span = Math.max(300, now - p.arrivals[0][0]);
      arrivalCps = p.arrivals.reduce((sum, a) => sum + a[1], 0) * 1000 / span;
    }
    // Keep up with arrivals, and never fall more than the max lag behind.
    const cps = Math.max(STREAM_PACE_MIN_CPS, arrivalCps, backlog * 1000 / STREAM_PACE_MAX_LAG_MS);
    p.carry += cps * dt / 1000;
    const step = Math.floor(p.carry);
    if (step > 0) {
      p.carry -= step;
      p.shown = Math.min(total, p.shown + step);
      const ctx = p.ctx;
      scheduleStreamPaint(row, () => paintStreamDelta(ctx));
    }
    if (p.shown < total) p.raf = requestAnimationFrame(() => streamPaceTick(row));
  }

  // Reveal everything received so far (before a structural event).
  function streamPaceFlush(row) {
    const p = row && row._pace;
    if (!p) return;
    if (p.raf) cancelAnimationFrame(p.raf);
    p.raf = 0;
    p.carry = 0;
    if (p.ctx && p.shown < p.ctx.getBuf().length) {
      p.shown = p.ctx.getBuf().length;
      const ctx = p.ctx;
      scheduleStreamPaint(row, () => paintStreamDelta(ctx));
    }
  }

  // The buffer was reset (steering): start revealing from the new text's start.
  function streamPaceReset(row) {
    if (row?._pace?.raf) cancelAnimationFrame(row._pace.raf);
    if (row) row._pace = null;
  }

  // The turn ended without a final render (Stop, an error): paint everything that
  // arrived once, then close the row so no later paint can reopen it.
  function streamPaceFinish(row) {
    if (!row || row._streamClosed) return;
    const p = row._pace;
    if (p && p.ctx && row.isConnected && p.shown < p.ctx.getBuf().length) {
      p.shown = p.ctx.getBuf().length;
      row._lastStreamPaint = 0;
      try { paintStreamDelta(p.ctx); } catch (e) { console.error("stream paint failed", e); }
    }
    cancelStreamPaint(row);
  }
  return {
    STREAM_PACE_PASSTHROUGH, pacedBuf, streamPaceArrival, streamPaceFlush, streamPaceReset,
    streamPaceFinish,
  };
};
