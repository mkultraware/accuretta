/* SPDX-License-Identifier: AGPL-3.0-only */
/* notifications & audio: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["notifications"] = ({ state }) => {
  // One set of sounds across Accuretta: the notch's cues (sound-cues.js) for a
  // request arriving and a long turn finishing or failing.
  let _lastSound = 0;
  function soundOn() { return state.settings ? state.settings.sound_notifications !== false : true; }
  function reserveSoundSlot() {
    const now = performance.now();
    if (now - _lastSound < 1200) return false;
    _lastSound = now;
    return true;
  }
  // So the settings toggle can preview a chime right away (it lives in another file).
  function resetSoundSlot() { _lastSound = 0; }
  function playCue(name) {
    if (!soundOn() || !reserveSoundSlot()) return;
    window.AccurettaSound?.play(name);
  }
  // The notch already sounds every request on this PC, so the main window
  // stays quiet then: one sound, not two. A phone or another PC still sounds.
  function notchSoundsApprovals() {
    return !!state.settings?.notch_enabled && !state.clientContext?.remote;
  }
  function playApprovalSound() { if (!notchSoundsApprovals()) playCue("gate"); }
  function playCompletionSound() { playCue("done"); }
  function playFailureSound() { playCue("error"); }
  document.addEventListener("pointerdown", () => window.AccurettaSound?.unlock(), { once: true, capture: true });

  function notifyApproval() {
    playApprovalSound();   // always audible (when enabled) — approvals are the point
    if (document.visibilityState !== "visible") {
      if (Notification.permission === "granted") {
        const n = new Notification("Accuretta needs approval", { body: "The agent is waiting for your permission.", icon: "/assets/brand/logo-mark-dark.png" });
        n.onclick = () => { window.focus(); n.close(); };
      } else if (Notification.permission === "default") {
        Notification.requestPermission();
      }
    }
  }

  const COMPLETION_SOUND_MIN_MS = 40000;

  // End-of-task notifications stay quiet for quick replies. Approval alerts
  // remain immediate because the agent is blocked waiting for the user.
  function notifyCompletion(durMs) {
    if ((durMs || 0) < COMPLETION_SOUND_MIN_MS) return;
    playCompletionSound();
    if (document.visibilityState !== "visible") {
      if (Notification.permission === "granted") {
        const n = new Notification("Accuretta", { body: "The agent finished.", icon: "/assets/brand/logo-mark-dark.png" });
        n.onclick = () => { window.focus(); n.close(); };
      } else if (Notification.permission === "default") {
        Notification.requestPermission();
      }
    }
  }

  function notifyFailure(message, durMs) {
    if ((durMs || 0) < COMPLETION_SOUND_MIN_MS) return;
    playFailureSound();
    if (document.visibilityState !== "visible") {
      if (Notification.permission === "granted") {
        const n = new Notification("Accuretta task failed", { body: message || "The agent stopped with an error.", icon: "/assets/brand/logo-mark-dark.png" });
        n.onclick = () => { window.focus(); n.close(); };
      } else if (Notification.permission === "default") {
        Notification.requestPermission();
      }
    }
  }
  return { resetSoundSlot, playCue, notifyApproval, notifyCompletion, notifyFailure };
};
