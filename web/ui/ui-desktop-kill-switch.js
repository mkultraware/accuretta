/* SPDX-License-Identifier: AGPL-3.0-only */
/* session-scoped desktop kill switch: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["desktop-kill-switch"] = ({
  $, esc, loadChats, newChat, renderChatList, selectChat, state, toast,
}) => {
  async function refreshSessionDesktopState() {
    const btn = $("#btn-session-desktop");
    if (!btn) return;
    if (!state.chatId || !state.settings.desktop_enabled) {
      btn.hidden = true;
      return;
    }
    btn.hidden = false;
    try {
      const r = await fetch(`/api/desktop/chat-state/${state.chatId}`).then(x => x.json());
      state.sessionDesktopDisabled = !!r.disabled;
    } catch { state.sessionDesktopDisabled = false; }
    // `is-disabled` matches the `#btn-session-desktop.is-disabled` rule in app.css
    // (red border / tint while off).
    btn.classList.toggle("is-disabled", state.sessionDesktopDisabled);
    btn.title = state.sessionDesktopDisabled
      ? "Desktop automation OFF for this chat — click to re-enable"
      : "Desktop automation ON for this chat — click to disable";
  }

  async function toggleSessionDesktop() {
    if (!state.chatId) return;
    const next = !state.sessionDesktopDisabled;
    try {
      await fetch("/api/desktop/chat-toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: state.chatId, disabled: next }),
      });
      state.sessionDesktopDisabled = next;
      refreshSessionDesktopState();
      toast(next ? "Desktop off for this chat" : "Desktop on for this chat", "info", 2200, "sess-desk");
    } catch (e) {
      toast("Toggle failed: " + e.message, "err", 2800);
    }
  }

  // Promise-based replacement for window.confirm() in the app's modal style.
  // Resolves true on confirm, false on cancel / Escape / scrim click.
  function confirmModal(opts = {}) {
    const {
      title = "Are you sure?",
      message = "",
      confirmText = "Confirm",
      cancelText = "Cancel",
      danger = false,
      icon = danger ? "ph-warning-circle" : "ph-question",
    } = opts;
    return new Promise((resolve) => {
      const scrim = document.createElement("div");
      scrim.className = "modal-scrim";
      const modal = document.createElement("aside");
      modal.className = "modal confirm-modal";
      modal.setAttribute("role", "dialog");
      modal.setAttribute("aria-modal", "true");
      modal.innerHTML = `
        <div class="modal-head">
          <i class="ph ${icon}"></i>
          <h3>${esc(title)}</h3>
          <button class="iconbtn" data-act="cancel" aria-label="Cancel"><i class="ph ph-x"></i></button>
        </div>
        <div class="modal-body">
          ${message ? `<p>${esc(message)}</p>` : ""}
          <div class="confirm-actions">
            <button class="btn" data-act="cancel">${esc(cancelText)}</button>
            <button class="btn ${danger ? "danger" : "accent"}" data-act="confirm">${esc(confirmText)}</button>
          </div>
        </div>`;
      document.body.appendChild(scrim);
      document.body.appendChild(modal);
      // Next frame so the .open transition animates in rather than snapping.
      requestAnimationFrame(() => { scrim.classList.add("open"); modal.classList.add("open"); });

      let settled = false;
      const finish = (result) => {
        if (settled) return;
        settled = true;
        document.removeEventListener("keydown", onKey);
        scrim.classList.remove("open");
        modal.classList.remove("open");
        setTimeout(() => { scrim.remove(); modal.remove(); }, 220);
        resolve(result);
      };
      const onKey = (e) => {
        if (e.key === "Escape") { e.preventDefault(); finish(false); }
        else if (e.key === "Enter") { e.preventDefault(); finish(true); }
      };
      modal.querySelectorAll('[data-act="cancel"]').forEach(b => b.addEventListener("click", () => finish(false)));
      modal.querySelector('[data-act="confirm"]').addEventListener("click", () => finish(true));
      scrim.addEventListener("click", () => finish(false));
      document.addEventListener("keydown", onKey);
      requestAnimationFrame(() => modal.querySelector('[data-act="confirm"]').focus());
    });
  }

  async function deleteChat(id) {
    const ok = await confirmModal({
      title: "Delete session",
      message: "This deletes the session and all its saved versions. This can't be undone.",
      confirmText: "Delete",
      danger: true,
      icon: "ph-trash",
    });
    if (!ok) return;
    await fetch(`/api/chats/${id}`, { method: "DELETE" });
    localStorage.removeItem("accuretta:draft:" + id);
    state.messageQueues.delete(id);
    if (state.queueEditId) state.queueEditId = null;
    await loadChats();
    if (state.chatId === id) {
      if (state.chats.order.length) selectChat(state.chats.order[0]);
      else await newChat();
    } else {
      renderChatList();
    }
  }
  return { refreshSessionDesktopState, toggleSessionDesktop, confirmModal, deleteChat };
};
