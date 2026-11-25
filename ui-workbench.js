/* SPDX-License-Identifier: AGPL-3.0-only */
/* workbench: tabs, status bar, panels, explorer, files: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["workbench"] = ({ wbFileIndex }) => {
  // =====================================================================
  // Desktop chrome around the chat: open-session tabs, a bottom status bar,
  // capability chips, panel toggles with configurable shortcuts, the Ctrl+P
  // file index, clickable file references and the explorer's git colors.
  // Everything degrades to nothing on mobile or when data is unavailable.
  const wb = {
    ready: false,
    status: null,           // last /api/workbench/status
    git: new Map(),         // root (normalized) -> { repo, branch, files, dirs, added, deleted }
    keycapTimer: null,
    lastTab: "",
  };
  const WB_TABS_KEY = "accuretta:wb:tabs";
  const WB_GROUPS_KEY = "accuretta:wb:collapsed-groups";
  const SB_SECTIONS_KEY = "accuretta:sb:collapsed-sections";
  const wbNorm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const wbStore = {
    get(key, fallback) { try { const v = localStorage.getItem(key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; } },
    set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch {} },
  };
  return { wb, WB_TABS_KEY, WB_GROUPS_KEY, SB_SECTIONS_KEY, wbNorm, wbStore };
};
