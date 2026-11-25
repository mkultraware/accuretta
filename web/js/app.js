/* SPDX-License-Identifier: AGPL-3.0-only */
/* ============================================================
   Accuretta frontend — single-file app logic.
   No framework. Vanilla JS. SSE for streaming.
   ============================================================ */
(() => {
  // Sections moved into their own ui-*.js file register a function on
  // Acc.sections; this file calls it where the section used to be.
  const Acc = (window.Acc = window.Acc || {});
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const api = (p, opts) => fetch(p, opts).then(r => r.json());
  // Constants that sections in other files use: created here, before any of them runs.
  // Reasoning-block closing tags, shared by splitThinking and the empty-bubble
  // promote logic. Covers  response, </thinking>, </reasoning>, <|/thinking|>,
  // [/think], [/reasoning], [/scratchpad].
  const THINK_CLOSE_RE = /<\s*\/\s*(?:think|thinking|reasoning)\s*>|<\s*\|\s*\/\s*thinking\s*\|>|\[\s*\/\s*(?:thought|thinking|reasoning|scratchpad)\s*\]/i;
  const API_RATE_NOTE = "Based on published API list rates. Hardware and electricity costs are excluded.";
  // File extension -> highlightCode() language id: which tree entries get a
  // view-source button, and how the formatted-source preview tokenizes them.
  // Markdown (book icon) and HTML (iframe lightning bolt) have their own
  // buttons.
  const SOURCE_VIEW_LANGS = {
    js: "js", mjs: "js", cjs: "js", jsx: "js",
    ts: "ts", tsx: "ts",
    py: "py", pyw: "py",
    json: "json", jsonc: "json",
    css: "css",
    sh: "sh", bash: "bash", zsh: "sh",
    ps1: "powershell", psm1: "powershell",
    sql: "sql",
    yaml: "yaml", yml: "yaml",
    toml: "text", ini: "text", cfg: "text", conf: "text",
    txt: "text", log: "text",
    rs: "rust", go: "go", c: "c", h: "c", cpp: "cpp", hpp: "cpp", java: "java",
    xml: "xml", svg: "svg",
  };
  // settings whose changes require relaunching llama-server (load-time flags)
  const LOAD_TIME_KEYS = [
    "num_ctx", "num_gpu", "num_batch", "num_thread", "kv_cache_type", "kv_cache_type_v", "model_path", "vision_model",
    "n_cpu_moe", "n_ubatch", "n_parallel", "flash_attn",
    "spec_strategy", "spec_draft_model", "spec_draft_mode", "no_warmup", "enable_metrics", "llama_extra_args",
    "spec_draft_n_max", "moe_cache_mib",
    // mmproj_path changes how the server is launched (--mmproj <path>) so it
    // also requires a relaunch to take effect.
    "mmproj_mode", "mmproj_auto", "mmproj_path", "mmproj_device",
  ];
  const touchedLoadControls = new Set();
  const mcpPop = { open: false, expanded: new Set() };
  const mcpUi = { data: null, selected: "", dirty: false, poll: 0, paste: false };
  const SVG_BOOK     = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
  // Pricing in $ per 1M tokens for each provider's top-tier model, at the
  // highest published rate so "saved" is conservative. Updated May 2026. Keys
  // are brand names, not model names.
  const CLOUD_PRICING = {
    "openai":    { label: "OpenAI",    input: 30.00, output: 180.00 },  // GPT-5.5 Pro
    "anthropic": { label: "Anthropic", input: 10.00, output:  50.00 },  // Claude Fable 5
    "google":    { label: "Google",    input:  4.00, output:  18.00 },  // Gemini 3.1 Pro (>200K ctx)
    "xai":       { label: "xAI",       input:  2.00, output:   6.00 },  // Grok 4.20
    "deepseek":  { label: "DeepSeek",  input:  1.74, output:   3.48 },  // V4 Pro (standard)
    "mistral":   { label: "Mistral",   input:  2.00, output:   5.00 },  // Magistral Medium
  };
  const wbFileIndex = { files: null, loadedAt: 0, loading: null };
  const SVG_PYCHECK  = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
  const SVG_EYE      = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

  // ---------- state ----------
  const {
    state, SHORTCUT_ACTIONS, defaultShortcuts, effectiveShortcuts, app, isMobile, isCompactViewport,
    syncCompactShell, syncSidebarToggle,
  } = Acc.sections["state"](
    {
      $, wbChordText: (...args) => wbChordText(...args),
    }
  );

  // ---------- utilities ----------
  const {
    _toasts, _deckHeightBefore, _deckHeightNow, _deckHeightSlide, _syncDeckStack, toast,
    requestModelLoad, positionAppUpdateCard, initAppUpdateCheck, buildFoldDivider,
    buildModelSwitchDivider, modelSwitchPoints, syncLiveModelSwitchDivider, isRenderedMessage,
    foldBoundaryVisibleIdx, ensureCompactionRow, finishCompactionRow, ensureSkillSaveRow,
    finishSkillSaveRow, hideToast, esc, setChatFollowBottom, scrollToBottom, initChatJumpButton,
    relTime, humanBytes, detectClientOs, currentClientHint, copyText,
  } = Acc.sections["utilities"](
    {
      $, api, app, isMobile, refreshContextStats: (...args) => refreshContextStats(...args),
      renderStatus: (...args) => renderStatus(...args),
      shortenModelName: (...args) => shortenModelName(...args), state,
      wbLiveModel: (...args) => wbLiveModel(...args),
    }
  );

  // ---------- notifications & audio ----------
  const {
    resetSoundSlot, playCue, notifyApproval, notifyCompletion, notifyFailure,
  } = Acc.sections["notifications"](
    {
      state,
    }
  );

  // ---------- presence heartbeat (notification routing) ----------
  Acc.sections["presence"]();

  // ---------- tool icons (inlined so no extra HTTP / static-whitelist changes) ----------
  const {
    TOOL_SVG, TOOL_ICON_MAP, approvalIconSvg, renderWebSearchChips, renderNetworkChart,
  } = Acc.sections["tool-icons"](
    {
      esc, state,
    }
  );

  // ---------- friendly tool call labels ----------
  const {
    shortPath, isCommandTool, toolResultFailed, WRENCH_SVG, _LOGO_SLAB_D, _LOGO_PURPLE_D,
    AGENT_AVATAR_HTML,
  } = Acc.sections["tool-labels"](
  );

  // ---------- tool card ----------
  const {
    tcardSync, tcardSetOpen, finalizeToolGroup, startToolActivity, findToolActivity,
    setToolActivityLabel, finishToolActivity, updateToolGroupHead, toolLabel, toolResultLabel,
  } = Acc.sections["tool-card"](
    {
      $, TOOL_ICON_MAP, TOOL_SVG, WRENCH_SVG,
      buildCommandsCardHtml: (...args) => buildCommandsCardHtml(...args),
      buildMcpCardHtml: (...args) => buildMcpCardHtml(...args),
      buildWritesCardHtml: (...args) => buildWritesCardHtml(...args), esc, isCommandTool,
      previewWorkspaceSource: (...args) => previewWorkspaceSource(...args), renderWebSearchChips,
      shortPath, state, toolResultFailed,
    }
  );

  // ---------- lightweight syntax highlighter ----------
  const {
    LANG_ALIAS, highlightCode, _isKnownLang, highlightForCard, splitHighlightedLines, wrapCodeLines,
    rewriteWriteFileToolCallToFence, maybeUnescapeJsonFence,
  } = Acc.sections["highlighter"](
    {
      esc,
    }
  );

  // ---------- unified-diff rendering (```diff fences) ----------
  const { renderDiffCard } = Acc.sections["diff-render"]({ esc });

  // ---------- email cards (```email fences) ----------
  const {
    renderEmailCard, STEP_BREAK, STEP_BREAK_RE, renderWorkingNotesBlock,
  } = Acc.sections["email-cards"](
    {
      esc,
    }
  );

  // ---------- streaming pipeline helpers ----------
  const {
    scanOpenFenceState, fenceBodyLooksLikeCode, findStableCut, shouldPromoteAnswer,
    stripJsonToolCallShapes, extractLineAnchoredFences, _MD_LIST_ITEM, _mdListHtml,
  } = Acc.sections["stream-helpers"](
    {
      _isKnownLang, isLikelyLiveWorkingNotes: (...args) => isLikelyLiveWorkingNotes(...args),
      maybeUnescapeJsonFence,
    }
  );

  // ---------- markdown-lite for chat bubbles ----------
  const {
    renderMarkdown,
  } = Acc.sections["markdown"](
    {
      LANG_ALIAS, _MD_LIST_ITEM, _isKnownLang, _mdListHtml,
      autolinkChatHtml: (...args) => autolinkChatHtml(...args), esc, extractLineAnchoredFences,
      highlightForCard, renderDiffCard, renderEmailCard, rewriteWriteFileToolCallToFence, state,
      stripJsonToolCallShapes, wrapCodeLines,
    }
  );

  // ---------- chat link autolinker + hover preview ----------
  const { autolinkChatHtml } = Acc.sections["autolinker"]({ state });

  // ---- hover popover -----------------------------------------------------
  const { highlightHTML } = Acc.sections["hover-popover"]({ esc, state });

  // ---------- bootstrap ----------
  const {
    boot, reflectIdeToggles,
  } = Acc.sections["bootstrap"](
    {
      $, api, applyAutoTune: (...args) => applyAutoTune(...args),
      applyTheme: (...args) => applyTheme(...args),
      checkModelAdvisor: (...args) => checkModelAdvisor(...args), initAppUpdateCheck,
      initCostWidget: (...args) => initCostWidget(...args), initWorkbench,
      initWorkflowUI: (...args) => initWorkflowUI(...args), isMobile,
      loadChats: (...args) => loadChats(...args),
      loadClientContext: (...args) => loadClientContext(...args),
      loadModels: (...args) => loadModels(...args),
      loadSettings: (...args) => loadSettings(...args),
      loadWorkspace: (...args) => loadWorkspace(...args), newChat: (...args) => newChat(...args),
      openRequestedChat: (...args) => openRequestedChat(...args),
      refreshModels: (...args) => refreshModels(...args),
      renderChatList: (...args) => renderChatList(...args),
      renderModelPill: (...args) => renderModelPill(...args),
      renderStatus: (...args) => renderStatus(...args),
      renderWorkspace: (...args) => renderWorkspace(...args), requestModelLoad,
      selectChat: (...args) => selectChat(...args),
      startApprovalSync: (...args) => startApprovalSync(...args), state,
      subscribeSSE: (...args) => subscribeSSE(...args), syncCompactShell, toast,
      wireEvents: (...args) => wireEvents(...args),
    }
  );

  // ---------- data loading ----------
  const {
    loadSettings, saveSettings, persistUiPreference, loadWorkspace, loadChats, loadModels,
    renderClientContext, loadClientContext,
  } = Acc.sections["data-loading"](
    {
      $, api, currentClientHint, detectClientOs, esc, isMobile,
      normalizeTheme: (...args) => normalizeTheme(...args),
      refreshDraftPairs: (...args) => refreshDraftPairs(...args),
      renderModelPill: (...args) => renderModelPill(...args),
      renderReasoningEffort: (...args) => renderReasoningEffort(...args),
      renderRemoteAccess: (...args) => renderRemoteAccess(...args),
      renderStatus: (...args) => renderStatus(...args),
      renderWorkspaceIdentity: (...args) => renderWorkspaceIdentity(...args), state,
      stopStreaming: (...args) => stopStreaming(...args), toast,
    }
  );

  // ---------- chat ----------
  const {
    newChat, refreshContextStats, renderTopbarContext, renderProjectChip, pickedPath,
    addProjectFolder, projectScopeMatches, clearConversations, initProjectScope, initProjectPicker,
    openRequestedChat, selectChat, removeProjectGroup,
  } = Acc.sections["chat"](
    {
      $, api, app, applyMobileTab: (...args) => applyMobileTab(...args),
      autoResize: (...args) => autoResize(...args),
      closeSecurityOverview: (...args) => closeSecurityOverview(...args),
      confirmModal: (...args) => confirmModal(...args), esc, foldBoundaryVisibleIdx,
      isCompactViewport, isMobile, isRenderedMessage,
      leaveWelcome: (...args) => leaveWelcome(...args), loadChats,
      loadVersions: (...args) => loadVersions(...args),
      refreshSessionDesktopState: (...args) => refreshSessionDesktopState(...args),
      renderBubble: (...args) => renderBubble(...args),
      renderChatList: (...args) => renderChatList(...args),
      renderCostWidget: (...args) => renderCostWidget(...args),
      renderEditBanner: (...args) => renderEditBanner(...args),
      renderMessageQueue: (...args) => renderMessageQueue(...args),
      renderMessages: (...args) => renderMessages(...args),
      renderSkillPill: (...args) => renderSkillPill(...args),
      renderStatus: (...args) => renderStatus(...args),
      renderTokTotal: (...args) => renderTokTotal(...args),
      renderWorkspace: (...args) => renderWorkspace(...args),
      renderWorkspaceIdentity: (...args) => renderWorkspaceIdentity(...args),
      scheduleMessageQueueDrain: (...args) => scheduleMessageQueueDrain(...args), scrollToBottom,
      setComposerMode: (...args) => setComposerMode(...args),
      setGoalModeUI: (...args) => setGoalModeUI(...args), state, toast,
      wbNorm: (...args) => wbNorm(...args),
      wbOnChatSelected: (...args) => wbOnChatSelected(...args),
      wireOverflow: (...args) => wireOverflow(...args),
    }
  );

  // ---------- session-scoped desktop kill switch ----------
  const {
    refreshSessionDesktopState, toggleSessionDesktop, confirmModal, deleteChat,
  } = Acc.sections["desktop-kill-switch"](
    {
      $, esc, loadChats, newChat, renderChatList: (...args) => renderChatList(...args), selectChat,
      state, toast,
    }
  );

  // ---------- command palette (⌘K) ----------
  const {
    openPalette, closePalette, _fuzzyScore, refreshPaletteList, paletteMove, paletteCommit,
    missionCategory, renderChatList, renderMessages, leaveWelcome, renderBubble, getCodeText,
    enhanceCodeBlocks, renderEditBanner, sendEditTurn, renderRegenerateChip,
  } = Acc.sections["command-palette"](
    {
      $, AGENT_AVATAR_HTML, THINK_CLOSE_RE, app, applyTheme: (...args) => applyTheme(...args),
      autoResize: (...args) => autoResize(...args), buildFoldDivider, buildModelSwitchDivider,
      copyText, deleteChat, ensureCompactionRow, esc,
      exportProjectZip: (...args) => exportProjectZip(...args),
      folderLeafName: (...args) => folderLeafName(...args),
      highlightMentionsInBubble: (...args) => highlightMentionsInBubble(...args), loadChats,
      modelSwitchPoints, newChat, nextTheme: (...args) => nextTheme(...args),
      noteOwnTurnEnded: (...args) => noteOwnTurnEnded(...args), notifyCompletion, notifyFailure,
      openSettings: (...args) => openSettings(...args), projectScopeMatches, relTime,
      removeProjectGroup, renderMarkdown, renderPlanPanel: (...args) => renderPlanPanel(...args),
      renderStoppedTurn: (...args) => renderStoppedTurn(...args),
      renderTaskHandoff: (...args) => renderTaskHandoff(...args),
      renderTurnChanges: (...args) => renderTurnChanges(...args),
      revealSettingsControl: (...args) => revealSettingsControl(...args), saveSettings,
      saveSnapshot: (...args) => saveSnapshot(...args),
      sbFillEmptySections: (...args) => sbFillEmptySections(...args),
      sbListSections: (...args) => sbListSections(...args),
      sbRenderFilesView: (...args) => sbRenderFilesView(...args),
      sbSetView: (...args) => sbSetView(...args),
      scheduleMessageQueueDrain: (...args) => scheduleMessageQueueDrain(...args),
      screenshotPreview: (...args) => screenshotPreview(...args), scrollToBottom, selectChat,
      send: (...args) => send(...args), setChatFollowBottom,
      setStreamingUI: (...args) => setStreamingUI(...args),
      shownImagesHtml: (...args) => shownImagesHtml(...args),
      splitCascade: (...args) => splitCascade(...args),
      splitThinking: (...args) => splitThinking(...args), state,
      stoppedTurnMarkup: (...args) => stoppedTurnMarkup(...args),
      streamChat: (...args) => streamChat(...args),
      streamPaceFinish: (...args) => streamPaceFinish(...args),
      stripMentionRefs: (...args) => stripMentionRefs(...args), toast,
      wbChatGroup: (...args) => wbChatGroup(...args), wbClock: (...args) => wbClock(...args),
      wbCollapsedGroups: (...args) => wbCollapsedGroups(...args),
      wbDecorateFileRefs: (...args) => wbDecorateFileRefs(...args),
      wbDuration: (...args) => wbDuration(...args), wbFileIndex,
      wbLoadFileIndex: (...args) => wbLoadFileIndex(...args),
      wbMcpOpen: (...args) => wbMcpOpen(...args), wbModelShort: (...args) => wbModelShort(...args),
      wbNorm: (...args) => wbNorm(...args),
      wbPaletteCommands: (...args) => wbPaletteCommands(...args),
      wbPaletteFiles: (...args) => wbPaletteFiles(...args),
      wbRenderTabs: (...args) => wbRenderTabs(...args),
      wbSaveCollapsedGroups: (...args) => wbSaveCollapsedGroups(...args),
      wbShortTime: (...args) => wbShortTime(...args),
      wbSyncCollapseAll: (...args) => wbSyncCollapseAll(...args),
    }
  );

  // ---------- attachments ----------
  const {
    renderImageTray, setAttachProgress, addImageFiles, addClientFiles, messageQueue,
    renderMessageQueue, enqueueMessage, scheduleMessageQueueDrain, uploadClientFiles,
  } = Acc.sections["attachments"](
    {
      $, appendAgentLog: (...args) => appendAgentLog(...args),
      autoResize: (...args) => autoResize(...args),
      captureIframePng: (...args) => captureIframePng(...args), currentClientHint, esc,
      hideMentionMenu: (...args) => hideMentionMenu(...args), humanBytes,
      openSettings: (...args) => openSettings(...args), send: (...args) => send(...args), state,
      toast, withMentionRefs: (...args) => withMentionRefs(...args),
    }
  );

  // ---------- send / stream ----------
  const {
    stoppedTurnMarkup, renderStoppedTurn, setPermsChipIcon, setGoalModeUI, turnGoalOff, send,
    setStreamingUI, stopStreaming, streamChat, splitThinking, isLikelyLiveWorkingNotes,
    splitCascade, thinkingLabel, toolProgressLabel, orbPhaseForTool, updateThinkLine,
  } = Acc.sections["send-stream"](
    {
      $, AGENT_AVATAR_HTML, THINK_CLOSE_RE, appendAgentLog: (...args) => appendAgentLog(...args),
      attackRailFinalize: (...args) => attackRailFinalize(...args),
      autoResize: (...args) => autoResize(...args),
      captureIframePng: (...args) => captureIframePng(...args),
      collapseWorkBlock: (...args) => collapseWorkBlock(...args), currentClientHint, enqueueMessage,
      esc, finalizeToolGroup, handleEvent: (...args) => handleEvent(...args),
      hideMentionMenu: (...args) => hideMentionMenu(...args), hideToast, leaveWelcome, loadChats,
      noteOwnTurnEnded: (...args) => noteOwnTurnEnded(...args), notifyCompletion, notifyFailure,
      openSettings: (...args) => openSettings(...args),
      osintCardFinalize: (...args) => osintCardFinalize(...args), renderBubble, renderChatList,
      renderCtxGauge: (...args) => renderCtxGauge(...args), renderImageTray, renderMarkdown,
      renderPlanPanel: (...args) => renderPlanPanel(...args),
      renderStatus: (...args) => renderStatus(...args),
      renderTaskHandoff: (...args) => renderTaskHandoff(...args), scheduleMessageQueueDrain,
      scrollToBottom, secretRailFinalize: (...args) => secretRailFinalize(...args), sendEditTurn,
      setAttachProgress, setComposerMode: (...args) => setComposerMode(...args), state,
      streamPaceFinish: (...args) => streamPaceFinish(...args), toast, uploadClientFiles,
      wbDecorateLiveRow: (...args) => wbDecorateLiveRow(...args),
      wbSyncAction: (...args) => wbSyncAction(...args),
      withMentionRefs: (...args) => withMentionRefs(...args),
    }
  );
  // ---------- security assessment progress ----------
  const {
    isAttackRailEnabled, ensureAttackRail, renderRail, attackRailToolStart, attackRailToolResult,
    attackRailMission, attackRailFinalize, attackRailBreach,
  } = Acc.sections["assessment-progress"](
    {
      esc, isFrontendSecretAudit: (...args) => isFrontendSecretAudit(...args),
      isPassiveOsint: (...args) => isPassiveOsint(...args), state,
    }
  );

  // ---------- frontend exposure rail ----------
  const {
    isFrontendSecretAudit, secretRailToolStart, secretRailToolResult, secretRailMission,
    secretRailFinalize,
  } = Acc.sections["exposure-rail"](
    {
      esc,
    }
  );

  // ---------- confirmed recon -> exploit transition ----------
  const {
    rtPhaseMarker,
  } = Acc.sections["recon-exploit"](
    {
      appendAgentLog: (...args) => appendAgentLog(...args), ensureAttackRail, renderRail,
    }
  );

  // ---------- OSINT recon card (passive intel gathering) ----------
  const {
    isPassiveOsint, osintCardToolStart, osintCardToolResult, osintCardFinalize, passiveIntelMission,
  } = Acc.sections["osint-card"](
    {
      esc,
    }
  );

  // ---- streaming paint pipeline ----
  const { scheduleStreamPaint, cancelStreamPaint } = Acc.sections["paint-pipeline"]();

  // ---- reveal pacing ----
  const {
    STREAM_PACE_PASSTHROUGH, pacedBuf, streamPaceArrival, streamPaceFlush, streamPaceReset,
    streamPaceFinish,
  } = Acc.sections["reveal-pacing"](
    {
      cancelStreamPaint, paintStreamDelta: (...args) => paintStreamDelta(...args),
      scheduleStreamPaint,
    }
  );

  // ---- finish batch ----
  const {
    scheduleFinishBatch, splitThinkingMemo, baseSplitMemo,
  } = Acc.sections["finish-batch"](
    {
      scrollToBottom, splitCascade, splitThinking,
    }
  );

  // ---------- streaming fade ----------
  const {
    paintStreamDelta, handleEvent,
  } = Acc.sections["streaming-fade"](
    {
      $, STEP_BREAK, STEP_BREAK_RE, STREAM_PACE_PASSTHROUGH,
      _applySavings: (...args) => _applySavings(...args),
      appendAgentLog: (...args) => appendAgentLog(...args),
      appendExecutionText: (...args) => appendExecutionText(...args),
      appendSandboxText: (...args) => appendSandboxText(...args),
      appendTerminalText: (...args) => appendTerminalText(...args), attackRailBreach,
      attackRailMission, attackRailToolResult, attackRailToolStart, baseSplitMemo,
      cancelStreamPaint, checkModelAdvisor: (...args) => checkModelAdvisor(...args),
      enhanceCodeBlocks, ensureSkillSaveRow, esc,
      executionActivity: (...args) => executionActivity(...args),
      executionTarget: (...args) => executionTarget(...args), fenceBodyLooksLikeCode, findStableCut,
      findToolActivity, finishSkillSaveRow, finishToolActivity,
      fmtToolDuration: (...args) => fmtToolDuration(...args),
      folderLeafName: (...args) => folderLeafName(...args),
      injectMobilePreviewCard: (...args) => injectMobilePreviewCard(...args), isAttackRailEnabled,
      isLikelyLiveWorkingNotes, isMobile, messageQueue, orbPhaseForTool, osintCardToolResult,
      osintCardToolStart, pacedBuf,
      parseMultiFileBlocks: (...args) => parseMultiFileBlocks(...args), passiveIntelMission,
      receiveApproval: (...args) => receiveApproval(...args), renderBubble, renderChatList,
      renderClientContext, renderCostWidget: (...args) => renderCostWidget(...args),
      renderCtxGauge: (...args) => renderCtxGauge(...args), renderMarkdown, renderMessageQueue,
      renderMessages, renderNetworkChart, renderPlanPanel: (...args) => renderPlanPanel(...args),
      renderPreview: (...args) => renderPreview(...args), renderProjectChip, renderRegenerateChip,
      renderStatus: (...args) => renderStatus(...args),
      renderStatusFast: (...args) => renderStatusFast(...args), renderStoppedTurn,
      renderTokTotal: (...args) => renderTokTotal(...args), renderTopbarContext,
      renderTurnChanges: (...args) => renderTurnChanges(...args),
      renderVersions: (...args) => renderVersions(...args), renderWorkingNotesBlock,
      renderWorkspace: (...args) => renderWorkspace(...args), rtPhaseMarker, scanOpenFenceState,
      scheduleFinishBatch, scheduleStreamPaint, scrollToBottom, secretRailMission,
      secretRailToolResult, secretRailToolStart,
      setActiveVersion: (...args) => setActiveVersion(...args), setGoalModeUI, setToolActivityLabel,
      shouldPromoteAnswer, shownImagesHtml: (...args) => shownImagesHtml(...args), splitCascade,
      splitThinking, splitThinkingMemo, startToolActivity, state, streamPaceArrival,
      streamPaceFlush, streamPaceReset, surfaceShell: (...args) => surfaceShell(...args),
      thinkingLabel, toast, toolLabel, toolProgressLabel, toolResultFailed, toolResultLabel,
      updateThinkLine, updateToolGroupHead, wbFinishLiveRow: (...args) => wbFinishLiveRow(...args),
      wsFileUrl: (...args) => wsFileUrl(...args),
    }
  );

  // ---------- per-turn change rollup + one-click Undo ----------
  const {
    _commonDirOf, taskFileAction, openTaskFile, renderWorkspaceIdentity, initWorkflowUI,
    controlFeedback, renderTaskHandoff, reviewTaskChanges,
  } = Acc.sections["change-rollup"](
    {
      $, api, app, autoResize: (...args) => autoResize(...args), copyText, esc, humanBytes,
      initSettingsSections: (...args) => initSettingsSections(...args), loadChats,
      previewWorkspaceHtml: (...args) => previewWorkspaceHtml(...args),
      previewWorkspaceMarkdown: (...args) => previewWorkspaceMarkdown(...args),
      previewWorkspaceSource: (...args) => previewWorkspaceSource(...args), renderChatList,
      selectChat, state, toast,
    }
  );

  // ---------- turn change card (files a reply changed) ----------
  const {
    renderTurnChanges,
  } = Acc.sections["turn-changes"](
    {
      _commonDirOf, confirmModal, esc, loadWorkspace, openTaskFile,
      renderWorkspace: (...args) => renderWorkspace(...args), reviewTaskChanges,
      scheduleFinishBatch, state, taskFileAction, toast,
    }
  );

  // ---------- task-progress panel ----------
  const { setPlanBtn, renderPlanPanel } = Acc.sections["task-progress"]({ esc, isMobile });

  // ---------- GitHub Activity ----------
  const {
    ghActivityOpen, ghActivityClose, loadGitHubActivity, usageCompactNumber, usageDuration,
    drawUsageMark, loadUsageStats, openUsageStats, closeUsageStats,
  } = Acc.sections["github-activity"](
    {
      $, API_RATE_NOTE, CLOUD_PRICING, _LOGO_PURPLE_D, _LOGO_SLAB_D, api,
      autoResize: (...args) => autoResize(...args), copyText, esc, loadChats,
      openSettings: (...args) => openSettings(...args),
      openSettingsAt: (...args) => openSettingsAt(...args),
      renderCostWidget: (...args) => renderCostWidget(...args),
      renderUsageProfile: (...args) => renderUsageProfile(...args), selectChat, send, state, toast,
    }
  );

  // ---------- Security Overview ----------
  const {
    hideSecurityScanIntro, renderSecurityAlerts, ackSecurityAttention, closeSecurityWatchDialog,
    syncSecurityWatchConfirm, renderSecurityWatchOptions, openSecurityWatchDialog,
    submitSecurityWatch, loadSecurityOverview, refreshSecurityOverview, updateSecurityAnalysis,
    closeSecurityWhitelist, confirmSecurityWhitelist, openSecurityOverview, closeSecurityOverview,
    filterFaq, syncFaqNav, openFaq, openFaqAt, closeFaq, refreshGitHubStatus, collapseWorkBlock,
  } = Acc.sections["security-overview"](
    {
      $, $$, api, autoResize: (...args) => autoResize(...args),
      closeSettings: (...args) => closeSettings(...args), closeUsageStats, esc, loadChats,
      selectChat, toast,
    }
  );

  // ---------- versions / preview ----------
  const {
    loadVersions, renderVersions, setActiveVersion, clearPreview, injectCspIfNeeded,
    injectTailwindIfNeeded,
  } = Acc.sections["versions"](
    {
      $, api, app, esc, humanBytes,
      injectMobilePreviewCard: (...args) => injectMobilePreviewCard(...args), isMobile,
      maybeUnescapeJsonFence, relTime, renderPreview: (...args) => renderPreview(...args), state,
      toast,
    }
  );

  // ---------- multi-file parsing ----------
  const {
    parseMultiFileBlocks, inlineLocalAssets,
  } = Acc.sections["multi-file"](
    {
      esc, maybeUnescapeJsonFence,
    }
  );

  // ---------- console forwarder ----------
  const { injectConsoleForwarder, clearConsole } = Acc.sections["console-forwarder"]({ state });

  // ---------- viewport presets ----------
  const {
    applyViewport, buildPreviewHtml,
  } = Acc.sections["viewport-presets"](
    {
      $, $$, injectConsoleForwarder, injectCspIfNeeded, injectTailwindIfNeeded, inlineLocalAssets,
      state,
    }
  );

  // ---------- workspace file actions ----------
  const {
    wsFileUrl, downloadWorkspaceFile, previewWorkspaceHtml, renderWorkspacePreview, runPythonCheck,
    _ensureDocPreviewPane, previewWorkspaceMarkdown, previewWorkspaceSource, renderPreview,
  } = Acc.sections["file-actions"](
    {
      $, SOURCE_VIEW_LANGS, SVG_BOOK, SVG_EYE, SVG_PYCHECK, buildPreviewHtml, clearPreview, esc,
      fileExt: (...args) => fileExt(...args), highlightCode, highlightHTML, isMobile,
      renderMarkdown, splitHighlightedLines, state,
      wbFormatBytes: (...args) => wbFormatBytes(...args),
      wbViewerBannerHtml: (...args) => wbViewerBannerHtml(...args),
    }
  );

  // ---------- preview: screenshot / export / review-UI ----------
  const {
    captureIframePng, screenshotPreview, reviewUiAttach, saveSnapshot, saveToWorkspace,
    toggleConsolePane, exportProjectZip,
  } = Acc.sections["preview-tools"](
    {
      $, api, autoResize: (...args) => autoResize(...args), buildPreviewHtml, confirmModal,
      renderImageTray, state, toast,
    }
  );

  // ---------- workspace ----------
  // ---- workspace file tree ----
  const {
    fileIconFor, folderLeafName, fetchFolderListing, fileExt, relPathFromRoot, renderTreeNode,
    renderWorkspace, addWorkspaceFolder, fmtToolDuration, executionTarget, executionActivity,
    appendExecutionText, buildWritesCardHtml, buildCommandsCardHtml, buildMcpCardHtml,
    reflectApprovalInLiveTurn, renderApprovals, decidedApprovals, receiveApproval, decideApproval,
    loadApprovals, startApprovalSync,
  } = Acc.sections["file-tree"](
    {
      $, SOURCE_VIEW_LANGS, SVG_BOOK, SVG_EYE, SVG_PYCHECK, _deckHeightBefore, _deckHeightNow,
      _deckHeightSlide, api, appendSandboxText: (...args) => appendSandboxText(...args),
      appendTerminalText: (...args) => appendTerminalText(...args), approvalIconSvg,
      downloadWorkspaceFile, esc, notifyApproval, previewWorkspaceHtml, previewWorkspaceMarkdown,
      previewWorkspaceSource, renderChatList, runPythonCheck, shortPath, state, tcardSync, toast,
      toolLabel, wbFileKind: (...args) => wbFileKind(...args),
      wbGitCodeFor: (...args) => wbGitCodeFor(...args),
      wbGitLabel: (...args) => wbGitLabel(...args), wbLoadGit: (...args) => wbLoadGit(...args),
      wbOpenWorkspaceFile: (...args) => wbOpenWorkspaceFile(...args),
    }
  );

  // ---------- foreign live turn ----------
  const {
    noteOwnTurnEnded, subscribeSSE,
  } = Acc.sections["live-events"](
    {
      $, AGENT_AVATAR_HTML, api, appendAgentLog: (...args) => appendAgentLog(...args),
      appendSandboxText: (...args) => appendSandboxText(...args), attackRailFinalize,
      collapseWorkBlock, decidedApprovals, ensureCompactionRow, ensureSkillSaveRow, esc,
      finalizeToolGroup, finishCompactionRow, finishSkillSaveRow, foldBoundaryVisibleIdx,
      handleEvent, isRenderedMessage, leaveWelcome, loadChats,
      loadMemories: (...args) => loadMemories(...args), loadModels, loadSecurityOverview,
      loadSettings, loadSkills: (...args) => loadSkills(...args), loadWorkspace, openRequestedChat,
      osintCardFinalize, populateSettingsForm: (...args) => populateSettingsForm(...args),
      receiveApproval, reflectApprovalInLiveTurn,
      refreshDesktopStatus: (...args) => refreshDesktopStatus(...args), renderApprovals,
      renderBubble, renderChatList, renderCtxGauge: (...args) => renderCtxGauge(...args),
      renderMessages, renderModelPill: (...args) => renderModelPill(...args),
      renderReasoningEffort: (...args) => renderReasoningEffort(...args),
      renderSkillPill: (...args) => renderSkillPill(...args),
      renderStatus: (...args) => renderStatus(...args), renderTaskHandoff, renderWorkspace,
      scheduleMessageQueueDrain, scrollToBottom, secretRailFinalize, setGoalModeUI, setStreamingUI,
      state, streamPaceFinish, toast, updateThinkLine,
      wbDecorateLiveRow: (...args) => wbDecorateLiveRow(...args),
    }
  );

  // ---------- settings drawer ----------
  const {
    initSettingsSections, openSettingsAt, renderSettingsProfile, initEmailSettings, wbMcpShown,
    revealSettingsControl, loadModelHealth, checkModelAdvisor, renderRemoteAccess,
    settingsReturnTarget, openSettings, shortcutLabel, setShortcutRecording, renderShortcutSettings,
    handleShortcutKeydown,
  } = Acc.sections["settings"](
    {
      $, SHORTCUT_ACTIONS, _syncDeckStack, _toasts, api, closeFaq, closeSecurityOverview,
      closeSettings: (...args) => closeSettings(...args), closeUsageStats, confirmModal,
      decideApproval, defaultShortcuts, drawUsageMark, effectiveShortcuts, esc, humanBytes,
      kTokens: (...args) => kTokens(...args), loadClientContext,
      loadCmdHistory: (...args) => loadCmdHistory(...args),
      loadDetectedVram: (...args) => loadDetectedVram(...args), loadGitHubActivity, loadModels,
      loadSkills: (...args) => loadSkills(...args),
      loadSystemContext: (...args) => loadSystemContext(...args), loadUsageStats, newChat, openFaq,
      openSecurityOverview, openUsageStats, persistUiPreference,
      populateSettingsForm: (...args) => populateSettingsForm(...args),
      refreshModels: (...args) => refreshModels(...args),
      refreshSandboxStatus: (...args) => refreshSandboxStatus(...args),
      renderModelPill: (...args) => renderModelPill(...args),
      renderSidebarProfile: (...args) => renderSidebarProfile(...args),
      renderStatus: (...args) => renderStatus(...args), requestModelLoad, state, stopStreaming,
      toast, touchedLoadControls, wbMcpLoad: (...args) => wbMcpLoad(...args),
      wbRefreshShortcutHints: (...args) => wbRefreshShortcutHints(...args),
      wbRunAction: (...args) => wbRunAction(...args),
      wbShortcutBlocked: (...args) => wbShortcutBlocked(...args),
      wbShowKeycap: (...args) => wbShowKeycap(...args),
    }
  );

  // ---------- Sandbox (WSL) ----------
  const { refreshSandboxStatus } = Acc.sections["sandbox"]({ $, api, esc });

  // ---------- VRAM auto-tune ----------
  const {
    loadDetectedVram, applyAutoTune, runAutoTune, loadSystemContext, saveSystemContext,
  } = Acc.sections["vram-tune"](
    {
      $, LOAD_TIME_KEYS, api, renderModelPill: (...args) => renderModelPill(...args),
      renderStatus: (...args) => renderStatus(...args), requestModelLoad, saveSettings, state,
      toast,
    }
  );
  // ---------- memories panel ----------
  const {
    loadMemories, addMemoryFromInput, rescanSystemContext, closeSettings,
  } = Acc.sections["memories-panel"](
    {
      $, api, closeUsageStats, confirmModal, esc, mcpUi, settingsReturnTarget, toast, wbMcpShown,
    }
  );

  // ---------- Command history drawer ----------
  const {
    openCmdHistory, closeCmdHistory, loadCmdHistory, clearCmdHistory, normalizedMmprojMode,
    syncMmprojModeUi,
  } = Acc.sections["cmd-history"](
    {
      $, api, confirmModal, copyText, esc, openSettingsAt, relTime, state, toast,
    }
  );

  // ---- speculative draft picker (DFlash / DSpark) ----
  const {
    specStrategyWantsDraft, syncSpecDraftSelection, specDraftKind, refreshSpecDrafts,
    populateSettingsForm, refreshDesktopStatus, markLoadControlTouched, wireLoadControlTracking,
    collectAndSaveSettings, THEME_CYCLE, appearanceDraft, fillAppearanceColors,
    previewAppearanceColors, normalizeTheme, nextTheme, stepTheme, applyTheme, activateTerminalTab,
  } = Acc.sections["draft-picker"](
    {
      $, LOAD_TIME_KEYS, api, controlFeedback, drawUsageMark, esc, loadMemories, loadModels,
      normalizedMmprojMode, reflectIdeToggles, refreshGitHubStatus,
      renderModelPill: (...args) => renderModelPill(...args), renderShortcutSettings,
      renderStatus: (...args) => renderStatus(...args), requestModelLoad, saveSettings,
      startBackendLogPoll: (...args) => startBackendLogPoll(...args),
      startShellPoll: (...args) => startShellPoll(...args), state,
      stopBackendLogPoll: (...args) => stopBackendLogPoll(...args),
      stopShellPoll: (...args) => stopShellPoll(...args), syncMmprojModeUi,
      syncThinkingBudgetControl: (...args) => syncThinkingBudgetControl(...args), toast,
      touchedLoadControls, wbSyncJumps: (...args) => wbSyncJumps(...args),
    }
  );

  // ---- llama.cpp backend log (Backend terminal tab) ----
  const {
    startBackendLogPoll, stopBackendLogPoll,
  } = Acc.sections["backend-log"](
    {
      colorizeBackendLine: (...args) => colorizeBackendLine(...args),
    }
  );

  // ---- shared interactive shell (Shell tab) ----
  const {
    selectShellSession, refreshShellOutput, startShellPoll, stopShellPoll, shellSend, shellKill,
    surfaceShell, colorizeBackendLine, appendTerminalText, appendSandboxText, appendAgentLog,
    statusSpeedText, renderStatus, renderStatusFast, computeCtxUsage, renderCtxGauge,
  } = Acc.sections["shell-tab"](
    {
      $, activateTerminalTab, esc, state, toggleConsolePane,
      wbRenderStatusbar: (...args) => wbRenderStatusbar(...args),
    }
  );
  // ---------- context breakdown hover ----------
  const {
    ctxPopoverOpen, renderCtxBreakdown, renderTokTotal,
  } = Acc.sections["ctx-breakdown"](
    {
      $, computeCtxUsage, esc, state,
    }
  );

  // ---------- cost savings widget ----------
  const {
    calcCost, _applySavings, _fetchSavings, _fmtSince, renderCostWidget,
  } = Acc.sections["savings-widget"](
    {
      $, API_RATE_NOTE, CLOUD_PRICING, state,
    }
  );

  // ---------- shareable savings card ----------
  const {
    shareSavingsCard,
  } = Acc.sections["savings-card"](
    {
      API_RATE_NOTE, CLOUD_PRICING, _fmtSince, calcCost, esc, state, toast,
    }
  );

  // ---------- usage stats: profile picture + share card ----------
  const {
    renderUsageProfile, initUsageProfile,
  } = Acc.sections["usage-stats"](
    {
      $, persistUiPreference, renderSettingsProfile, state, toast, usageCompactNumber,
      usageDuration,
    }
  );

  // ---------- cost widget start-up ----------
  const {
    initCostWidget,
  } = Acc.sections["cost-widget-init"](
    {
      $, CLOUD_PRICING, _fetchSavings, initUsageProfile, renderCostWidget, shareSavingsCard, state,
    }
  );

  // ---------- mobile preview card ----------
  const {
    injectMobilePreviewCard, shortenModelName, renderModelPill, COMPOSER_MODES, setComposerMode,
    wireComposerMode, effortLevels, effortLabel, syncThinkingBudgetControl, setReasoningEffort,
    renderReasoningEffort, wireReasoningEffort,
  } = Acc.sections["mobile-preview"](
    {
      $, api, buildEffortSlider: (...args) => buildEffortSlider(...args),
      effortSliderOpened: (...args) => effortSliderOpened(...args), esc, humanBytes,
      isCompactViewport, isMobile, persistUiPreference, scrollToBottom, send, state,
      syncEffortSliders: (...args) => syncEffortSliders(...args), toast,
      wbSyncModeChip: (...args) => wbSyncModeChip(...args),
    }
  );

  // ---- llama.cpp update ----
  const {
    offerLlamaUpdate,
  } = Acc.sections["llama-update"](
    {
      api, confirmModal, refreshModels: (...args) => refreshModels(...args), toast,
    }
  );

  // ---- Maya install ----
  const {
    offerMayaInstall,
  } = Acc.sections["maya-install"](
    {
      api, confirmModal, loadModelByPath: (...args) => loadModelByPath(...args),
      refreshModels: (...args) => refreshModels(...args), toast,
    }
  );

  // ---- effort slider ----
  const {
    buildEffortSlider, syncEffortSliders, effortSliderOpened, refreshModels, loadModelByPath,
    refreshDraftPairs,
  } = Acc.sections["effort-stream"](
    {
      $, activateTerminalTab, api, effortLabel, effortLevels, loadModels, loadSettings,
      offerLlamaUpdate, offerMayaInstall, populateSettingsForm, renderModelPill, renderStatus, requestModelLoad,
      saveSettings, setReasoningEffort, state, syncLiveModelSwitchDivider, toast, toggleConsolePane,
    }
  );

  // Rows for the model-pill dropdown from state.modelsList, rebuilt on every
  // open so the "loaded" indicator matches what the bridge is running.
  // ---- model menu ----
  const {
    wireModelMenu,
  } = Acc.sections["model-menu"](
    {
      $, esc, isMobile, loadModelByPath, shortenModelName, state,
    }
  );

  // ---------- mobile tabs ----------
  const {
    applyMobileTab,
  } = Acc.sections["mobile-tabs"](
    {
      $$, app, openSettings, positionAppUpdateCard, state,
    }
  );

  // ---------- event wiring ----------
  const {
    autoResize, _MENTION_RE, loadWorkspaceFiles, resolveMentionToken, syncComposerMirrorStyle,
    updateComposerMirror,
  } = Acc.sections["event-wiring"](
    {
      esc, state,
    }
  );

  // ---- the @/# autocomplete menu ----
  const {
    updateMentionMenu, hideMentionMenu, mentionMenuKeydown, loadSkills, kTokens, activateSkill,
    clearActiveSkill, renderSkillPill,
  } = Acc.sections["mention-menu"](
    {
      api, appendAgentLog, autoResize, esc, loadWorkspaceFiles, state, toast,
    }
  );

  // ---- send-time + render-time helpers ----
  const {
    withMentionRefs, stripMentionRefs, highlightMentionsInBubble, wireOverflow, wireEvents,
  } = Acc.sections["render-helpers"](
    {
      $, $$, _MENTION_RE, ackSecurityAttention, activateTerminalTab, addClientFiles, addImageFiles,
      addMemoryFromInput, addWorkspaceFolder, api, app, appearanceDraft, applyAutoTune,
      applyMobileTab, applyTheme, applyViewport, autoResize, clearCmdHistory, clearConsole,
      closeCmdHistory, closeFaq, closePalette, closeSecurityOverview, closeSecurityWatchDialog,
      closeSecurityWhitelist, closeSettings, closeUsageStats, collectAndSaveSettings, confirmModal,
      confirmSecurityWhitelist, copyText, ctxPopoverOpen, drawUsageMark, fillAppearanceColors,
      filterFaq, getCodeText, ghActivityClose, ghActivityOpen, handleShortcutKeydown,
      hideMentionMenu, hideSecurityScanIntro, initChatJumpButton, initEmailSettings,
      initProjectPicker, initProjectScope, isMobile, loadClientContext, loadCmdHistory,
      loadMemories, loadModelByPath, loadModelHealth, loadSettings, loadUsageStats,
      loadWorkspaceFiles, markLoadControlTouched, mentionMenuKeydown, newChat, nextTheme,
      openCmdHistory, openFaq, openFaqAt, openPalette, openSecurityOverview,
      openSecurityWatchDialog, openSettings, openUsageStats, paletteCommit, paletteMove,
      persistUiPreference, pickedPath, playCue, previewAppearanceColors, previewWorkspaceHtml,
      previewWorkspaceMarkdown, previewWorkspaceSource, reflectIdeToggles, refreshDesktopStatus,
      refreshGitHubStatus, refreshModels, refreshPaletteList, refreshSandboxStatus,
      refreshSecurityOverview, refreshShellOutput, refreshSpecDrafts, renderCtxBreakdown,
      renderCtxGauge, renderImageTray, renderModelPill, renderPreview, renderSecurityAlerts,
      renderSecurityWatchOptions, renderShortcutSettings, renderStatus, renderWorkspacePreview,
      requestModelLoad, rescanSystemContext, resetSoundSlot, resolveMentionToken, reviewUiAttach,
      runAutoTune, runPythonCheck, saveSettings, saveSystemContext, saveToWorkspace,
      selectShellSession, send, setPermsChipIcon, setPlanBtn, setShortcutRecording, shellKill,
      shellSend, specDraftKind, specStrategyWantsDraft, state, stepTheme, stopStreaming,
      submitSecurityWatch, syncCompactShell, syncComposerMirrorStyle, syncFaqNav, syncMmprojModeUi,
      syncSecurityWatchConfirm, syncSpecDraftSelection, tcardSetOpen, toast, toggleConsolePane,
      toggleSessionDesktop, turnGoalOff, updateComposerMirror, updateMentionMenu,
      updateSecurityAnalysis, wbMcpClose: (...args) => wbMcpClose(...args),
      wbRefreshShortcutHints: (...args) => wbRefreshShortcutHints(...args), wireComposerMode,
      wireLoadControlTracking, wireModelMenu, wireReasoningEffort, wsFileUrl,
    }
  );

  // =====================================================================
  // ---------- workbench: tabs, status bar, panels, explorer, files -------
  const {
    wb, WB_TABS_KEY, WB_GROUPS_KEY, SB_SECTIONS_KEY, wbNorm, wbStore,
  } = Acc.sections["workbench"](
    {
      wbFileIndex,
    }
  );

  // ---- small formatters ----
  const {
    wbModelShort, wbModelColor, wbShortTime, wbDuration, wbClock, wbFormatBytes, WB_IMAGE_EXT,
    wbFileKind, wbFileIcon,
  } = Acc.sections["small-formatters"](
    {
      fileIconFor,
    }
  );

  // ---- shortcuts: blocking, keycap toast, hints ----
  const {
    wbShortcutBlocked, wbKeycapsOn, wbShowKeycap, wbChordText, wbRefreshShortcutHints,
  } = Acc.sections["shortcuts"](
    {
      $, effectiveShortcuts, esc, isMobile, shortcutLabel, syncSidebarToggle, wb, wbStore,
    }
  );

  // ---- actions ----
  const {
    wbRunAction, wbToggleExplorer, wbToggleTerminal, wbSyncTerminalButton,
  } = Acc.sections["wb-actions"](
    {
      $, app, applyMobileTab, closePalette, isMobile, openPalette, state,
      wbCloseTab: (...args) => wbCloseTab(...args), wbCycleTab: (...args) => wbCycleTab(...args),
      wbStore,
    }
  );

  // ---- session tabs ----
  const {
    wbOnChatSelected, wbRenderTabs, wbCycleTab, wbCloseTab,
  } = Acc.sections["session-tabs"](
    {
      $, WB_TABS_KEY, esc, newChat, selectChat, state, wb, wbModelColor,
      wbRefreshGitForChat: (...args) => wbRefreshGitForChat(...args),
      wbRenderContextRow: (...args) => wbRenderContextRow(...args),
      wbRevealChatGroup: (...args) => wbRevealChatGroup(...args), wbStore,
    }
  );

  // ---- sidebar groups ----
  const {
    wbChatGroup, wbRevealChatGroup, wbCollapsedGroups, wbSaveCollapsedGroups, wbSyncCollapseAll,
    wbBuildSessionsHeader,
  } = Acc.sections["sidebar-groups"](
    {
      $, WB_GROUPS_KEY, folderLeafName, missionCategory, renderChatList,
      sbRevealSection: (...args) => sbRevealSection(...args), state, wbNorm, wbStore,
    }
  );

  // ---- replies ----
  const {
    wbLiveModel, wbDecorateLiveRow, wbFinishLiveRow,
  } = Acc.sections["replies"](
    {
      state, wbClock, wbDecorateFileRefs: (...args) => wbDecorateFileRefs(...args), wbDuration,
    }
  );

  // ---- file references in replies ----
  const {
    wbDecorateFileRefs, wbRootFor, wbOpenRef,
  } = Acc.sections["file-refs"](
    {
      relPathFromRoot, state, toast, wbChatFolder: (...args) => wbChatFolder(...args), wbFileIcon,
      wbFileKind, wbLoadFileIndex: (...args) => wbLoadFileIndex(...args), wbNorm,
      wbOpenWorkspaceFile: (...args) => wbOpenWorkspaceFile(...args),
    }
  );

  // ---- file index (Ctrl+P) ----
  const {
    wbLoadFileIndex, wbPaletteFiles, wbPaletteCommands,
  } = Acc.sections["file-index"](
    {
      $, _fuzzyScore, api, closePalette, effectiveShortcuts, folderLeafName, openSettings,
      refreshPaletteList, revealSettingsControl, shortcutLabel, state, wbFileIcon, wbFileIndex,
      wbFileKind, wbOpenWorkspaceFile: (...args) => wbOpenWorkspaceFile(...args), wbRootFor,
      wbRunAction,
    }
  );

  // ---- viewer ----
  const {
    wbOpenWorkspaceFile, wbViewerBannerHtml, shownImagesHtml,
  } = Acc.sections["wb-viewer"](
    {
      WB_IMAGE_EXT, _ensureDocPreviewPane, esc, folderLeafName, previewWorkspaceHtml,
      previewWorkspaceMarkdown, previewWorkspaceSource, state, wbFileIcon, wbFileKind,
      wbFormatBytes, wsFileUrl,
    }
  );

  // ---- git ----
  const {
    wbGitEntryFor, wbGitCodeFor, wbGitLabel, wbLoadGit, wbChatFolder, wbRefreshGitForChat,
  } = Acc.sections["wb-git"](
    {
      $, api, esc, folderLeafName, state, wb, wbNorm,
      wbRenderContextRow: (...args) => wbRenderContextRow(...args),
    }
  );

  // ---- composer context row ----
  const {
    wbRenderContextRow,
  } = Acc.sections["composer-context"](
    {
      $, esc, state, wbChatFolder, wbGitEntryFor,
    }
  );

  // ---- status bar, meters, chips ----
  const {
    wbRenderStatusbar, wbPollStatus,
  } = Acc.sections["status-bar"](
    {
      $, api, computeCtxUsage, esc, renderSidebarUsage: (...args) => renderSidebarUsage(...args),
      state, statusSpeedText, wb, wbModelColor, wbModelShort,
    }
  );

  // ---- explorer tools ----
  const { wbApplyTreeFilter, wbCollapseAllFolders } = Acc.sections["explorer-tools"]();

  // ---- composer: four controls ----
  const {
    wbSyncModeChip, wbSyncAction, wbInitComposer,
  } = Acc.sections["composer-controls"](
    {
      $, COMPOSER_MODES, activateSkill, clearActiveSkill, esc, isCompactViewport, loadSkills, state,
      stopStreaming,
    }
  );

  // ---- sidebar ----
  const {
    renderSidebarProfile, renderSidebarUsage, sbSetView, sbRenderFilesView, sbListSections,
    sbFillEmptySections, sbRevealSection, wbInitSidebar,
  } = Acc.sections["sidebar"](
    {
      $, SB_SECTIONS_KEY, THEME_CYCLE, addProjectFolder, clearConversations, esc,
      fetchFolderListing, folderLeafName, openFaq, openSecurityOverview, openSettings,
      openSettingsAt, renderChatList, renderTreeNode, state, stepTheme, wb, wbChatGroup, wbNorm,
      wbStore,
    }
  );

  // ---- MCP servers window ----
  const {
    wbMcpOpen, wbMcpClose, wbMcpLoad, wbMcpApply, wbMcpRenderList, wbMcpCurrent, wbMcpEnvRow,
    wbMcpRenderDetail, wbMcpFormBody, wbMcpPost, wbMcpSetDisabled,
  } = Acc.sections["mcp-servers"](
    {
      $, confirmModal, esc, mcpPop, mcpUi, openSettingsAt, toast,
      wbMcpPopRender: (...args) => wbMcpPopRender(...args), wbMcpShown, wbPollStatus,
    }
  );

  // ---- MCP quick look ----
  const {
    wbMcpPopOpen, wbMcpPopClose, wbMcpPopRender, wbInitMcpWindow,
  } = Acc.sections["mcp-quick-look"](
    {
      $, confirmModal, esc, mcpPop, mcpUi, state, toast, wbMcpApply, wbMcpClose, wbMcpCurrent,
      wbMcpEnvRow, wbMcpFormBody, wbMcpLoad, wbMcpOpen, wbMcpPost, wbMcpRenderDetail,
      wbMcpRenderList, wbMcpSetDisabled,
    }
  );

  // ---- terminal jump-to-bottom ----
  const { wbInitJumps, wbSyncJumps } = Acc.sections["terminal-jump"]({ $ });

  // ---- init ----
  function initWorkbench() {
    if (wb.ready) return;
    wb.ready = true;
    document.body.classList.add("wb-on");
    wbBuildSessionsHeader();
    wbInitComposer();
    wbInitJumps();
    wbInitMcpWindow();
    wbInitSidebar();

    // tabs live in the topbar path, after the project context
    const path = $(".topbar .path");
    if (path && !$("#wb-tabs")) {
      const tabs = document.createElement("div");
      tabs.id = "wb-tabs";
      tabs.className = "wb-tabs";
      tabs.innerHTML = `<div class="wb-tab-strip" role="tablist" aria-label="Open sessions"></div><button class="wb-tab-new" id="wb-tab-new" type="button" aria-label="New session" title="New session"><i class="ph ph-plus"></i></button>`;
      path.appendChild(tabs);
      const strip = tabs.querySelector(".wb-tab-strip");
      strip.addEventListener("click", (e) => {
        const tab = e.target.closest(".wb-tab");
        if (!tab) return;
        if (e.target.closest(".wb-tab-close")) { e.stopPropagation(); wbCloseTab(tab.dataset.id); return; }
        if (tab.dataset.id !== state.chatId) selectChat(tab.dataset.id);
      });
      strip.addEventListener("auxclick", (e) => {
        const tab = e.target.closest(".wb-tab");
        if (tab && e.button === 1) { e.preventDefault(); wbCloseTab(tab.dataset.id); }
      });
      strip.addEventListener("keydown", (e) => {
        const tab = e.target.closest(".wb-tab");
        if (tab && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectChat(tab.dataset.id); }
      });
      strip.addEventListener("wheel", (e) => {
        if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { strip.scrollLeft += e.deltaY; e.preventDefault(); }
      }, { passive: false });
      tabs.querySelector("#wb-tab-new").addEventListener("click", () => newChat());
    }

    // capability chips + working-tree diffstat, first in the topbar actions
    const actions = $(".topbar .actions");
    if (actions && !$("#wb-chips")) {
      const chips = document.createElement("div");
      chips.id = "wb-chips";
      chips.className = "wb-chips";
      chips.innerHTML = `<button class="wb-chip" id="wb-chip-mcp" type="button" hidden><i class="ph ph-plugs" aria-hidden="true"></i><span></span></button>`
        + `<button class="wb-chip" id="wb-chip-skills" type="button" hidden><i class="ph ph-sparkle" aria-hidden="true"></i><span></span></button>`
        + `<button class="wb-chip wb-diffstat" id="wb-chip-diff" type="button" hidden></button>`;
      actions.prepend(chips);
      $("#wb-chip-mcp").setAttribute("aria-haspopup", "dialog");
      $("#wb-chip-mcp").setAttribute("aria-expanded", "false");
      $("#wb-chip-mcp").addEventListener("click", () => (mcpPop.open ? wbMcpPopClose() : wbMcpPopOpen()));
      $("#wb-chip-skills").addEventListener("click", () => {
        const input = $("#composer-input");
        if (!input) return;
        // Skills load with "#": put one at the caret so the skills picker opens.
        input.focus();
        const value = input.value;
        const pos = input.selectionStart ?? value.length;
        const before = value.slice(0, pos);
        const lead = before && !/\s$/.test(before) ? " " : "";
        input.value = `${before}${lead}#${value.slice(pos)}`;
        const caret = pos + lead.length + 1;
        input.setSelectionRange(caret, caret);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      });
      $("#wb-chip-diff").addEventListener("click", (e) => {
        const folder = e.currentTarget.dataset.folder;
        wbToggleExplorer(true);
        const root = [...document.querySelectorAll("#ws-list .ws-root")]
          .find(r => wbNorm(r.querySelector(".path")?.title) === wbNorm(folder)) || document.querySelector("#ws-list .ws-root");
        if (root && !root.classList.contains("open")) root.querySelector(".ws-folder")?.click();
        root?.scrollIntoView({ block: "nearest" });
      });
    }

    // composer context row (branch + where commands run)
    const projects = $(".composer-projects");
    if (projects && !$("#wb-ctx-row")) {
      const row = document.createElement("div");
      row.id = "wb-ctx-row";
      row.className = "wb-ctx-row";
      projects.appendChild(row);
      $("#execution-target-select")?.addEventListener("change", () => wbRenderContextRow());
    }

    // sidebar meters, above the cost widget
    const foot = $(".side-foot");
    if (foot && !$("#wb-meters")) {
      const meters = document.createElement("div");
      meters.id = "wb-meters";
      meters.className = "wb-meters";
      foot.prepend(meters);
    }

    // bottom status bar
    if (!$("#wb-statusbar")) {
      const bar = document.createElement("footer");
      bar.id = "wb-statusbar";
      bar.className = "wb-statusbar";
      bar.setAttribute("aria-label", "Status");
      bar.innerHTML = `
        <span class="wb-sb-item"><span class="wb-mdot" data-sb="dot"></span><span data-sb="model"></span></span>
        <span class="wb-sb-item" title="Context window in use">ctx <span class="wb-sb-bar"><i data-sb="ctxbar"></i></span><span data-sb="ctx"></span></span>
        <span class="wb-sb-item"><i class="ph ph-lightning" aria-hidden="true"></i><span data-sb="speed"></span></span>
        <span class="wb-sb-item" data-sb="vram" hidden><i class="ph ph-graphics-card" aria-hidden="true"></i><span></span></span>
        <span class="wb-sb-item" data-sb="gputemp" hidden><i class="ph ph-thermometer-simple" aria-hidden="true"></i><span></span></span>
        <span class="wb-sb-item wb-sb-state" data-sb="state"><i class="ph ph-pulse" aria-hidden="true"></i><span>idle</span></span>
        <span class="grow"></span>
        <span class="wb-sb-item" data-sb="backend"><span class="wb-sb-led"></span><span>llama-server</span></span>
        <button class="wb-sb-item wb-sb-btn" id="wb-sb-terminal" type="button"><i class="ph ph-terminal-window" aria-hidden="true"></i><span>Terminal</span><kbd></kbd></button>`;
      document.body.appendChild(bar);
      $("#wb-sb-terminal").addEventListener("click", () => wbToggleTerminal());
      document.body.classList.add("wb-has-statusbar");
    }

    // keycap toast
    if (!$("#wb-keycap")) {
      const cap = document.createElement("div");
      cap.id = "wb-keycap";
      cap.className = "wb-keycap";
      cap.setAttribute("aria-hidden", "true");
      document.body.appendChild(cap);
    }
    const capToggle = $("#set-keycap-hints");
    if (capToggle) {
      capToggle.checked = wbKeycapsOn();
      capToggle.addEventListener("change", () => wbStore.set("accuretta:wb:keycaps", capToggle.checked));
    }

    // terminal tabs: inactive ones collapse to their icon
    document.querySelectorAll(".term-tab").forEach(tab => {
      if (tab.querySelector(".term-tab-label")) return;
      const text = [...tab.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim());
      const label = text.map(n => n.textContent.trim()).join(" ");
      text.forEach(n => n.remove());
      tab.insertAdjacentHTML("beforeend", `<span class="term-tab-label">${esc(label)}</span>`);
      tab.setAttribute("aria-label", label);
      if (!tab.title) tab.title = label;
    });

    // explorer header tools
    $("#btn-wb-explorer")?.addEventListener("click", () => wbToggleExplorer());
    $("#btn-ws-collapse")?.addEventListener("click", wbCollapseAllFolders);
    $("#btn-ws-refresh")?.addEventListener("click", () => {
      wb.git.clear();
      renderWorkspace();
      wbLoadFileIndex(true);
      wbRefreshGitForChat();
    });
    $("#btn-ws-filter")?.addEventListener("click", () => {
      const box = $("#wb-ws-filter");
      if (!box) return;
      box.hidden = !box.hidden;
      $("#btn-ws-filter").setAttribute("aria-pressed", String(!box.hidden));
      if (!box.hidden) $("#wb-ws-filter-input")?.focus();
      else { $("#wb-ws-filter-input").value = ""; wbApplyTreeFilter(""); }
    });
    $("#wb-ws-filter-input")?.addEventListener("input", (e) => wbApplyTreeFilter(e.target.value));
    $("#wb-ws-filter-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); $("#btn-ws-filter")?.click(); }
    });

    // clickable file references in replies
    $("#chat-inner")?.addEventListener("click", (e) => {
      const ref = e.target.closest(".wb-fref");
      if (ref) wbOpenRef(ref.dataset.ref);
    });
    $("#chat-inner")?.addEventListener("keydown", (e) => {
      const ref = e.target.closest?.(".wb-fref");
      if (ref && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); wbOpenRef(ref.dataset.ref); }
    });

    // keep the Terminal button honest when the preview pane is toggled elsewhere
    new MutationObserver(wbSyncTerminalButton).observe(app, { attributes: true, attributeFilter: ["class"] });

    // restore panel state
    if (wbStore.get("accuretta:wb:explorer-hidden", false)) wbToggleExplorer(false);
    if (wbStore.get("accuretta:wb:terminal-hidden", false)) app.classList.add("wb-term-hidden");

    wbRefreshShortcutHints();
    wbOnChatSelected(state.chatId);
    wbRefreshGitForChat();
    document.querySelectorAll("#chat-inner .bubble.agent").forEach(wbDecorateFileRefs);
    renderStatus();
    wbPollStatus();
    // 30 s: VRAM comes from nvidia-smi, and polling the driver often causes hitches.
    setInterval(wbPollStatus, 30000);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") wbPollStatus(); });
  }

  // Periodic refresh for real-time updates
  setInterval(() => {
    if (document.visibilityState === "visible") {
      renderCtxGauge();
    }
  }, 3000);

  // kick off
  loadApprovals();
  boot().catch(e => {
    console.error(e);
    toast("Boot error: " + (e.message || e), "err", 10000, "boot-error");
  });
})();
