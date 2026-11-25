/* SPDX-License-Identifier: AGPL-3.0-only */
/* composer context row: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["composer-context"] = ({ $, esc, state, wbChatFolder, wbGitEntryFor }) => {
  function wbRenderContextRow(info) {
    const row = $("#wb-ctx-row");
    if (!row) return;
    const chat = state.chats?.chats?.[state.chatId];
    const folder = wbChatFolder(chat);
    const gitInfo = info || (folder ? wbGitEntryFor(folder) : null);
    const branch = chat?.github_worktree?.branch || (folder ? gitInfo?.branch : "") || "";
    const target = $("#execution-target-select")?.selectedOptions?.[0]?.textContent?.trim() || "";
    row.innerHTML = (branch ? `<span title="Git branch"><i class="ph ph-git-branch" aria-hidden="true"></i>${esc(branch)}</span>` : "")
      + (target ? `<span title="Where commands and file edits run"><i class="ph ph-desktop" aria-hidden="true"></i>${esc(target)}</span>` : "");
  }
  return { wbRenderContextRow };
};
