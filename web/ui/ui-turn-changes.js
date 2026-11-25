/* SPDX-License-Identifier: AGPL-3.0-only */
/* turn change card (files a reply changed): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["turn-changes"] = ({
  _commonDirOf, confirmModal, esc, loadWorkspace, openTaskFile, renderWorkspace, reviewTaskChanges,
  scheduleFinishBatch, state, taskFileAction, toast,
}) => {
  function renderTurnChanges(row, evt) {
    if (!row || !evt || !Array.isArray(evt.files) || !evt.files.length) return;
    const col = row.querySelector(".bubble-col");
    if (!col) return;
    const n = evt.files.length;
    const files = evt.files.map(f => {
      const tag = f.created ? `<span class="tc-tag tc-new">new</span>`
                : (f.removed ? `<span class="tc-tag tc-del-tag">deleted</span>` : "");
      const action = taskFileAction(f);
      return `<div class="tc-file" title="${esc(f.path || f.name)}"><i class="ph ph-file-text"></i><span class="tc-name">${esc(f.name)}</span>${tag}<span class="tc-stat">${f.text_diff === false ? "File changed" : `<span class="tc-add">+${f.added|0}</span> <span class="tc-del">−${f.deleted|0}</span>`}</span>${action ? `<button type="button" class="tc-file-open" data-file-index="${evt.files.indexOf(f)}">${action}</button>` : ""}</div>`;
    }).join("");
    // Normalize separators and Windows case before the root check: f.path uses
    // "/" while a saved root may use "\", and a raw startsWith would reject an
    // in-root folder.
    const normPath = s => String(s || "").replace(/\\/g, "/").replace(/\/+$/, "");
    const wsRoots = ((state.workspace && state.workspace.folders) || []).map(normPath);
    // "Open folder" target: the folder the files were actually saved into (their
    // shared parent). The bridge allows opening any folder the agent wrote into
    // this session, even outside the workspace roots; a workspace root is only
    // the fallback when no file path is known or the folder is rejected.
    const openDir = normPath(_commonDirOf(evt.files.map(f => f.path)));
    const bar = document.createElement("div");
    bar.className = "turn-changes";
    bar.dataset.turnId = evt.turn_id || "";
    const chatId = evt.chat_id || state.chatId;
    const verification = evt.verification || { status: "unchecked", checks: [] };
    // One quiet word for what was checked: green only for a real pass, red only
    // for a failure, nothing when no check ran. The per-check list is in the
    // tooltip; a failure also gets its own line below.
    const checkStatusLabels = { passed: "Passed", failed: "Failed", unchecked: "Not checked" };
    const checks = verification.checks || [];
    const okFiles = Number(verification.syntax_files);
    const check = evt.undone ? { cls: "", text: "Text edits undone" }
      : verification.status === "failed" ? { cls: "failed", text: "Check failed" }
      : verification.status === "passed" ? { cls: "passed", text: "Tests passed" }
      : verification.status === "partial" && n && okFiles >= n ? { cls: "passed", text: "Syntax OK" }
      : verification.status === "partial" && okFiles > 0 ? { cls: "", text: `${okFiles} of ${n} checked` }
      : verification.status === "partial" ? { cls: "", text: "Checked" }
      : { cls: "", text: "" };
    const checkTitle = checks.map(c => `${c.label} · ${checkStatusLabels[c.status] || "Not checked"}${c.detail ? ` · ${c.detail}` : ""}`).join("\n");
    const failed = evt.undone ? null : checks.find(c => c.status === "failed");
    bar.innerHTML = `
      <div class="tc-head">
        <span class="tc-title">${n} file${n === 1 ? "" : "s"} changed</span>
        <span class="tc-verification ${check.cls}" title="${esc(checkTitle)}">${check.cls ? `<i class="ph ph-${check.cls === "failed" ? "x" : "check"}"></i>` : ""}${esc(check.text)}</span>
        <span class="tc-actions">
          <button class="tc-review" type="button">Review</button>
          <button class="tc-openfolder" type="button" title="Open folder" aria-label="Open folder"><i class="ph ph-folder-open"></i></button>
          <button class="tc-undo" type="button" title="Undo file edits. Commands, external actions, and files edited again are kept." aria-label="Undo file edits" ${evt.undone || evt.files.every(f => f.restorable === false) ? "hidden" : ""}><i class="ph ph-arrow-counter-clockwise"></i></button>
        </span>
      </div>
      <div class="tc-files">${files}</div>
      ${failed ? `<div class="tc-fail">${esc(failed.label)} failed${failed.detail ? `: ${esc(failed.detail)}` : ""}</div>` : ""}`;
    // Same finish batch as the final bubble render, so the card lands in the same
    // frame as the think-line flip.
    scheduleFinishBatch(row, () => {
      if (!col.isConnected) return;
      col.querySelector(".turn-changes")?.remove();
      col.appendChild(bar);
      requestAnimationFrame(syncFade);
    });
    bar.querySelector(".tc-review").onclick = () => reviewTaskChanges(evt, chatId);
    bar.querySelectorAll(".tc-file-open").forEach(button => button.addEventListener("click", async () => {
      try { await openTaskFile(evt.files[Number(button.dataset.fileIndex)]); }
      catch (error) { toast(error.message || "Could not open file", "err", 3500); }
    }));
    // Fade + scroll affordance for long lists: mark the card as scrollable when
    // the file list overflows, and clear the bottom fade once scrolled to the end.
    const filesEl = bar.querySelector(".tc-files");
    const syncFade = () => {
      const overflow = filesEl.scrollHeight - filesEl.clientHeight > 2;
      bar.classList.toggle("tc-scroll", overflow);
      const atEnd = filesEl.scrollTop + filesEl.clientHeight >= filesEl.scrollHeight - 2;
      bar.classList.toggle("tc-at-end", atEnd);
    };
    filesEl.addEventListener("scroll", syncFade, { passive: true });
    const openBtn = bar.querySelector(".tc-openfolder");
    openBtn.addEventListener("click", async () => {
      const primary = openDir || wsRoots[0] || "";
      if (!primary) { toast("No folder to open", "warn", 2200); return; }
      const tryOpen = async (dir) => {
        const resp = await fetch("/api/open-folder", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: dir }),
        });
        return resp.json().catch(() => ({}));
      };
      try {
        let r = await tryOpen(primary);
        // Rare: the shared parent spans roots / wasn't written this session.
        // Fall back to a workspace root so the click still lands somewhere.
        if (!(r && r.ok) && wsRoots[0] && primary !== wsRoots[0]) {
          r = await tryOpen(wsRoots[0]);
        }
        if (!(r && r.ok)) toast(r && r.error ? r.error : "Could not open folder", "err", 3200);
      } catch (e) {
        toast("Could not open folder: " + (e.message || e), "err", 3200);
      }
    });
    const btn = bar.querySelector(".tc-undo");
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      const confirmed = await confirmModal({ title: "Undo file edits?",
        message: "Restore this task’s recorded text files. Files edited again, commands, and external actions will be preserved.",
        confirmText: "Undo file edits" });
      if (!confirmed || !btn.isConnected) return;
      btn.disabled = true;
      btn.innerHTML = `<i class="ph ph-circle-notch tc-spin"></i>`;
      try {
        const resp = await fetch("/api/undo", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ turn_id: bar.dataset.turnId }),
        });
        const r = await resp.json();
        if (r && r.ok) {
          bar.classList.add("tc-undone");
          bar.querySelector(".tc-title").innerHTML = `<i class="ph ph-check"></i> Reverted ${r.restored} file${r.restored === 1 ? "" : "s"}`;
          bar.querySelector(".tc-verification").textContent = "Edits undone";
          bar.querySelector(".tc-verification").className = "tc-verification";
          if (r.errors?.length) toast(r.errors.join("\n"), "warn", 5000);
          btn.remove();
          try { await loadWorkspace(); renderWorkspace(); } catch {}
        } else {
          toast(r && r.error ? r.error : "Undo failed", "err", 4000);
          btn.disabled = false;
          btn.innerHTML = `<i class="ph ph-arrow-counter-clockwise"></i>`;
        }
      } catch (e) {
        toast("Undo failed: " + (e.message || e), "err", 4000);
        btn.disabled = false;
        btn.innerHTML = `<i class="ph ph-arrow-counter-clockwise"></i>`;
      }
    });
    // No explicit scroll here: the finish-batch flush scrolls once (follow-
    // respecting) after the card lands, instead of chasing it a frame later.
  }
  return { renderTurnChanges };
};
