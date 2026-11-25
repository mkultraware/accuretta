/* SPDX-License-Identifier: AGPL-3.0-only */
/* the @/# autocomplete menu: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["mention-menu"] = ({
  api, appendAgentLog, autoResize, esc, loadWorkspaceFiles, state, toast,
}) => {
  let _mentionItems = [];
  let _mentionActive = 0;
  function composerMentionQuery() {
    const ta = document.getElementById("composer-input");
    if (!ta || ta.selectionStart !== ta.selectionEnd) return null;
    const pos = ta.selectionStart;
    const before = ta.value.slice(0, pos);
    // @ = workspace files, # = skills. Both are <trigger><query>$ after a
    // boundary, so the menu can be driven by either at the caret.
    const m = before.match(/(?:^|[\s(])([@#])([\w./-]*)$/);
    if (!m) return null;
    return { trigger: m[1], query: m[2], at: pos - m[2].length - 1 };
  }
  function updateMentionMenu() {
    const q = composerMentionQuery();
    if (!q) { hideMentionMenu(); return; }
    const ql = q.query.toLowerCase();
    let items = [];
    if (q.trigger === "#") {
      // Skills: the user-curated catalog, loaded on first use (20s cache).
      // The first "#" arrives before the catalog does; show it once loaded.
      if (!state.skills) {
        loadSkills().then(() => { if (composerMentionQuery()?.trigger === "#") updateMentionMenu(); });
        return;
      }
      loadSkills();
      const skills = (state.skills || []).filter(s =>
        !ql || s.name.toLowerCase().includes(ql) ||
        (s.description || "").toLowerCase().includes(ql))
        .map(s => ({ kind: "skill", skill: s }));
      if (ql) {
        skills.sort((a, b) =>
          (a.skill.name.toLowerCase().startsWith(ql) ? 0 : 1) -
          (b.skill.name.toLowerCase().startsWith(ql) ? 0 : 1));
      }
      items = skills.slice(0, 8);
    } else {
      // Workspace files: match + sort the existing lazy-loaded list.
      loadWorkspaceFiles();
      let fItems = (state._wsFiles || []).map(f => ({ kind: "file", file: f }));
      if (ql) {
        fItems = fItems.filter(f =>
          f.file.name.toLowerCase().includes(ql) || f.file.rel.toLowerCase().includes(ql));
        fItems.sort((a, b) =>
          (a.file.name.toLowerCase().startsWith(ql) ? 0 : 1) - (b.file.name.toLowerCase().startsWith(ql) ? 0 : 1)
          || a.file.rel.length - b.file.rel.length);
      }
      items = fItems.slice(0, 8);
    }
    if (!items.length) { hideMentionMenu(); return; }
    _mentionItems = items;
    if (_mentionActive >= items.length) _mentionActive = 0;
    showMentionMenu();
  }
  function showMentionMenu() {
    const menu = document.getElementById("mention-menu");
    if (!menu) return;
    menu.innerHTML = _mentionItems.map((it, i) => {
      const active = i === _mentionActive ? " active" : "";
      if (it.kind === "skill") {
        const s = it.skill;
        const tag = s.budget ? `skill · ~${kTokens(s.budget)} tok` : "skill";
        return `<div class="mention-item is-skill${active}" data-i="${i}">` +
          `<i class="ph ph-book-open-text"></i><span class="mention-name">${esc(s.name)}</span>` +
          `<span class="mention-rel">${esc(tag)}</span></div>`;
      }
      const f = it.file;
      return `<div class="mention-item${active}" data-i="${i}">` +
        `<i class="ph ph-file"></i><span class="mention-name">${esc(f.name)}</span>` +
        `<span class="mention-rel">${esc(f.rel)}</span></div>`;
    }).join("");
    menu.classList.remove("hidden");
    menu.querySelectorAll(".mention-item").forEach(el =>
      el.addEventListener("mousedown", (e) => { e.preventDefault(); insertMention(_mentionItems[+el.dataset.i]); }));
  }
  function hideMentionMenu() {
    const menu = document.getElementById("mention-menu");
    if (menu) menu.classList.add("hidden");
    _mentionItems = [];
    _mentionActive = 0;
  }
  function mentionMenuOpen() {
    const menu = document.getElementById("mention-menu");
    return !!(menu && !menu.classList.contains("hidden") && _mentionItems.length);
  }
  function mentionMenuKeydown(e) {
    if (!mentionMenuOpen()) return false;
    if (e.key === "ArrowDown") { _mentionActive = (_mentionActive + 1) % _mentionItems.length; showMentionMenu(); e.preventDefault(); return true; }
    if (e.key === "ArrowUp")   { _mentionActive = (_mentionActive - 1 + _mentionItems.length) % _mentionItems.length; showMentionMenu(); e.preventDefault(); return true; }
    if (e.key === "Enter" || e.key === "Tab") { insertMention(_mentionItems[_mentionActive]); e.preventDefault(); return true; }
    if (e.key === "Escape") { hideMentionMenu(); e.preventDefault(); return true; }
    return false;
  }
  function insertMention(item) {
    const ta = document.getElementById("composer-input");
    const q = composerMentionQuery();
    if (!ta || !q || !item) { hideMentionMenu(); return; }
    if (item.kind === "skill") {
      // A skill was picked: consume the #token (q.at is the trigger char, as for @
      // files) and call load_skill via /api/tools/call. The result arrives as the
      // skill:loaded SSE event (deck card + pill + per-chat active slot).
      const before = ta.value.slice(0, q.at);
      const after = ta.value.slice(ta.selectionStart);
      ta.value = before + after;
      const pos = before.length;
      ta.setSelectionRange(pos, pos);
      hideMentionMenu();
      autoResize(ta);
      if (state.chatId) localStorage.setItem("accuretta:draft:" + state.chatId, ta.value);
      ta.focus();
      activateSkill(item.skill);
      return;
    }
    const file = item.file;
    const before = ta.value.slice(0, q.at);
    const after = ta.value.slice(ta.selectionStart);
    // Use the rel path when the bare name is ambiguous (duplicate filenames in
    // a crowded workspace), so it resolves to the exact file the user picked.
    const dup = (state._wsFiles || []).filter(f => f.name.toLowerCase() === file.name.toLowerCase()).length > 1;
    const token = "@" + (dup ? file.rel : file.name) + " ";
    ta.value = before + token + after;
    const pos = (before + token).length;
    ta.setSelectionRange(pos, pos);
    hideMentionMenu();
    autoResize(ta);
    if (state.chatId) localStorage.setItem("accuretta:draft:" + state.chatId, ta.value);
    ta.focus();
  }

  // ===== skills: user-curated markdown procedures =====
  const _SKILL_ICON = '<i class="ph ph-book-open-text"></i>';

  async function loadSkills(force) {
    if (!force && state.skills && Date.now() - state.skillsAt < 20000) return state.skills;
    try {
      const r = await (await fetch("/api/skills")).json();
      state.skills = (r && r.skills) || [];
      state.skillsAt = Date.now();
    } catch { state.skills = state.skills || []; }
    return state.skills;
  }

  function fireToolCall(name, argumentsObj) {
    return fetch("/api/tools/call", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, arguments: argumentsObj })
    }).then(r => r.json()).catch(() => null);
  }

  function kTokens(n) {
    n = Number(n) || 0;
    return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, "") + "K" : String(n);
  }

  function activateSkill(skill) {
    if (!skill || !state.chatId) return;
    appendAgentLog(`User activated skill: ${skill.name}.`);
    fireToolCall("load_skill", { name: skill.name, chat_id: state.chatId })
      .then(res => { if (res && res.error && state.chatId) toast("Skill failed to load: " + res.error, "err", 4000, "skill-load-fail"); })
      .catch(() => {});
  }

  async function clearActiveSkill() {
    const chatId = state.chatId;
    const previous = state.activeSkill;
    if (!chatId || !previous) return;

    const raw = state.chats?.chats?.[chatId];
    state.activeSkill = null;
    if (raw) {
      delete raw.active_skill;
      delete raw.active_skill_budget;
    }
    renderSkillPill();

    try {
      const result = await api("/api/skills/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chatId })
      });
      if (result?.error) throw new Error(result.error);
      toast(`Skill ${previous.name} unloaded`, "ok", 2200, "skill-unloaded");
    } catch (error) {
      if (state.chatId === chatId && !state.activeSkill) {
        state.activeSkill = previous;
        if (raw) {
          raw.active_skill = previous.name;
          raw.active_skill_budget = previous.budget || 0;
        }
        renderSkillPill();
      }
      toast(`Could not unload ${previous.name}: ${error?.message || error}`, "err", 4500, "skill-unload-fail");
    }
  }

  function renderSkillPill() {
    const pill = document.getElementById("skill-pill");
    if (!pill) return;
    const s = state.activeSkill;
    if (!s || !state.chatId) {
      pill.classList.add("hidden");
      pill.innerHTML = "";
      return;
    }
    const suffix = s.budget ? ` · ~${kTokens(s.budget)} tok` : "";
    pill.innerHTML = `${_SKILL_ICON}<span class="skill-pill-name">skill: ${esc(s.name)}${suffix}</span>` +
      `<button class="skill-pill-x" title="Unload active skill" aria-label="Unload active skill">×</button>`;
    pill.classList.remove("hidden");
    pill.querySelector(".skill-pill-x").addEventListener("click", async (e) => {
      e.stopPropagation();
      const button = e.currentTarget;
      button.disabled = true;
      await clearActiveSkill();
    });
  }
  return {
    updateMentionMenu, hideMentionMenu, mentionMenuKeydown, loadSkills, kTokens, activateSkill,
    clearActiveSkill, renderSkillPill,
  };
};
