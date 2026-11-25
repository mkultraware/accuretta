/* SPDX-License-Identifier: AGPL-3.0-only */
/* friendly tool call labels: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["tool-labels"] = () => {
  function shortPath(p) {
    if (!p) return "";
    const s = String(p).replace(/\\/g, "/");
    const parts = s.split("/").filter(Boolean);
    return parts.length <= 2 ? s : "…/" + parts.slice(-2).join("/");
  }
  // Tools whose action the user almost always wants to see in full (paths,
  // commands). Everything else (reads, searches, listings, memory ops) gets
  // collapsed into a single chevron group to keep the chat readable.
  const COMMAND_TOOLS = new Set([
    "write_file", "delete_file", "edit_file", "patch_file",
    "run_powershell", "run_tests", "sandbox_run", "sandbox_nmap", "sandbox_sqlmap", "open_program",
    "remote_shell", "remote_write_file",
    "remote_file_begin", "remote_file_append", "remote_file_commit",
    "remote_copy_to", "remote_copy_from",
    "desktop_launch_app", "desktop_focus_window", "desktop_click",
    "desktop_type_text", "desktop_press_keys", "desktop_close_window",
  ]);
  function isCommandTool(name) { return COMMAND_TOOLS.has(name); }
  function toolResultFailed(result) {
    if (!result) return false;
    const exitCode = result.exit_code ?? result.exit ?? result.returncode;
    return Boolean(result.error || result.ok === false
      || (exitCode !== undefined && exitCode !== null && Number(exitCode) !== 0));
  }

  // Inline SVG wrench (crisper than the font glyph at small sizes and with the
  // spin animation); the tool strip's fallback when no specific tool is running.
  const WRENCH_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;

  // Accuretta split-A brand mark (348x282 viewBox), drawn by drawUsageMark().
  const _LOGO_SLAB_D = "M164 6 L226 6 Q240 6 233.7 18.5 L111.3 261.5 Q105 274 91 274 L24 274 Q10 274 16.5 261.6 L143.5 18.4 Q150 6 164 6 Z";
  const _LOGO_PURPLE_D = "M225 135 L260 135 Q272 135 277.2 145.8 L332.8 261.2 Q338 272 326 272 L269 272 Q257 272 253.3 260.6 L216.7 146.4 Q213 135 225 135 Z";
  const AGENT_AVATAR_HTML = '<div class="avatar agent-avatar" role="img" aria-label="Agent" title="Agent"><canvas class="agent-orb" width="80" height="80" data-state="idle" aria-hidden="true"></canvas></div>';
  return {
    shortPath, isCommandTool, toolResultFailed, WRENCH_SVG, _LOGO_SLAB_D, _LOGO_PURPLE_D,
    AGENT_AVATAR_HTML,
  };
};
