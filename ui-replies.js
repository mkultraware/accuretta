/* SPDX-License-Identifier: AGPL-3.0-only */
/* replies: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["replies"] = ({ state, wbClock, wbDecorateFileRefs, wbDuration }) => {
  // No header above replies: the think line already says how long it took and
  // the composer names the model. The model and duration are still recorded
  // on the message for the sidebar row and the "Finished" footer.
  function wbLiveModel() { return state.settings?.model || state.loadedModel || ""; }
  function wbDecorateLiveRow(row) {
    if (row) row._wbModel = wbLiveModel();
  }
  function wbFinishLiveRow(row, msg) {
    if (!row) return;
    const secs = row._workStart ? Math.max(1, Math.round((Date.now() - row._workStart) / 1000)) : 0;
    if (msg) {
      if (row._wbModel && !msg.model) msg.model = row._wbModel;
      if (secs && !msg._worked_s) msg._worked_s = secs;
    }
    const finished = `${secs ? `Finished ${wbDuration(secs)} · ` : ""}${wbClock(Math.floor(Date.now() / 1000))}`;
    row.dataset.wbFinished = finished;
    const meta = row.querySelector(".bubble-meta");
    if (meta && !meta.textContent.startsWith("Finished")) {
      const rest = meta.textContent.replace(/streaming…?/i, "").trim();
      meta.textContent = rest && /tok/.test(rest) ? `${finished} · ${rest}` : finished;
    }
    // The final bubble is re-rendered in the finish batch; decorate after it.
    requestAnimationFrame(() => requestAnimationFrame(() => wbDecorateFileRefs(row.querySelector(".bubble.agent"))));
  }
  return { wbLiveModel, wbDecorateLiveRow, wbFinishLiveRow };
};
