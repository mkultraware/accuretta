/* SPDX-License-Identifier: AGPL-3.0-only */
/* presence heartbeat (notification routing): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["presence"] = () => {
  // One of the bridge's two "is the user active?" signals (the other is host
  // input idle). Held external notifications (Discord approval DMs, security
  // alerts) are released once this says the user is away, or at the escalation
  // deadline. A tab that had focus in the last few minutes still counts, so
  // switching to an IDE does not read as "away". Sent only on focus changes or
  // every 30s while the tab is visible.
  const PRESENCE_ID = (() => {
    try {
      let id = sessionStorage.getItem("accuretta:presence-id");
      if (!id) {
        id = "web-" + (crypto.randomUUID ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`);
        sessionStorage.setItem("accuretta:presence-id", id);
      }
      return id;
    } catch (_) {
      return "web-" + Math.random().toString(36).slice(2);
    }
  })();
  function sendPresence(keepalive) {
    const focused = document.visibilityState === "visible" && document.hasFocus();
    try {
      fetch("/api/ui/presence", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        ...(keepalive ? { keepalive: true } : {}),
        body: JSON.stringify({ id: PRESENCE_ID, focused }),
      }).catch(() => {});
    } catch (_) {}
  }
  sendPresence();
  document.addEventListener("visibilitychange", () => sendPresence(true));
  window.addEventListener("focus", () => sendPresence());
  window.addEventListener("blur", () => sendPresence());
  window.addEventListener("pagehide", () => sendPresence(true));
  if (!window._presenceKeepalive) {
    window._presenceKeepalive = setInterval(() => {
      if (document.visibilityState === "visible") sendPresence();
    }, 30000);
  }
};
