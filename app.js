/* ============================================================
   Accuretta frontend — single-file app logic.
   No framework. Vanilla JS. SSE for streaming.
   ============================================================ */
(() => {
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const api = (p, opts) => fetch(p, opts).then(r => r.json());

  // ---------- state ----------
  const state = {
    chats: { chats: {}, order: [] },
    chatId: null,
    messages: [],
    settings: {},
    workspace: { folders: [] },
    models: [],
    mode: "auto",          // auto | ide | agent
    view: "preview",       // preview | code
    versions: [],
    activeVersion: null,   // vid
    currentHtml: "",
    currentFiles: {},      // { "style.css": "...", "script.js": "...", ... } parsed from the current assistant turn
    streaming: false,
    abortCtl: null,
    approvals: new Map(),
    mobileTab: "chat",
    pendingImages: [],  // [{ dataUrl, name }]
    viewport: "full",        // full | desktop | tablet | mobile
    consoleOpen: false,
    consoleLogs: [],         // [{level, text, t}]
    tokTotal: 0,             // cumulative generated tokens for this session (client-side)
    tokPromptTotal: 0,       // cumulative prompt tokens this session (for cost calc)
    totalGenDuration: 0,     // cumulative generation duration (seconds) across session
    _streamOutEstimate: 0,   // live output token estimate during streaming (chars/4)
    _streamPromptEstimate: 0,// live prompt token estimate during streaming
    costProvider: "openai",  // selected provider for cost widget
    sessionDesktopDisabled: false,
    palette: { open: false, items: [], idx: 0 },
    _versionsExpanded: false,
    _lastMsgTokens: 0,
    _lastMsgPromptTokens: 0,
    _ctxPoll: null,
    touchedFiles: new Set(),
  };

  const app = $("#app");
  const isMobile = () => window.matchMedia("(max-width: 600px)").matches;

  // ---------- utilities ----------
  // simple toast system — bottom-right, auto-dismiss. keyed toasts replace each other.
  const _toasts = new Map();
  function toast(msg, kind = "info", ms = 3000, key = null) {
    if (kind === "err" || kind === "error") {
      triggerComposerStatus("error");
    } else if (msg.includes("auto-tuned") || msg.includes("auto-tune")) {
      triggerComposerStatus("autotuned");
    } else if (msg.includes("loaded") || msg.includes("ready")) {
      triggerComposerStatus("loaded");
    }

    let host = document.getElementById("toast-host");
    if (!host) {
      host = document.createElement("div");
      host.id = "toast-host";
      // Prefer the composer-wrap as the anchor so toasts pop above the
      // prompt box and slide up "from behind" it. Falls back to body for
      // pages that don't have a composer.
      const anchor = document.querySelector(".composer-wrap") || document.body;
      anchor.appendChild(host);
    }
    if (key && _toasts.has(key)) {
      try { _toasts.get(key).remove(); } catch {}
      _toasts.delete(key);
    }
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = msg;
    host.appendChild(el);
    if (key) _toasts.set(key, el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => { try { el.remove(); } catch {} if (key && _toasts.get(key) === el) _toasts.delete(key); }, 250);
    }, ms);
    return el;
  }

  function triggerComposerStatus(status) {
    const comp = document.querySelector(".composer");
    if (!comp) return;
    comp.classList.remove("status-loaded", "status-autotuned", "status-error");
    if (status === "loaded") {
      comp.classList.add("status-loaded");
      setTimeout(() => comp.classList.remove("status-loaded"), 2200);
    } else if (status === "autotuned") {
      comp.classList.add("status-autotuned");
      setTimeout(() => comp.classList.remove("status-autotuned"), 2200);
    } else if (status === "error") {
      comp.classList.add("status-error");
      setTimeout(() => comp.classList.remove("status-error"), 5000);
    }
  }

  const esc = (s) => String(s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));

  function isNearBottom() {
    const s = $("#chat-scroll");
    return s.scrollHeight - s.scrollTop - s.clientHeight < 120;
  }
  function scrollToBottom(force = false) {
    const s = $("#chat-scroll");
    if (force || isNearBottom()) {
      s.scrollTop = s.scrollHeight;
    }
  }

  function relTime(t) {
    const d = Math.floor(Date.now() / 1000) - (t || 0);
    if (d < 60) return "just now";
    if (d < 3600) return Math.floor(d / 60) + "m ago";
    if (d < 86400) return Math.floor(d / 3600) + "h ago";
    return Math.floor(d / 86400) + "d ago";
  }

  function humanBytes(n) {
    if (n == null) return "—";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / 1024 / 1024).toFixed(2) + " MB";
  }

  // ---------- notifications & audio ----------
  function playDing() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.setValueAtTime(1046.50, ctx.currentTime); // C6
      gain.gain.setValueAtTime(0.2, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.6);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      osc.stop(ctx.currentTime + 0.6);
    } catch(e) {}
  }

  function notifyCompletion() {
    if (document.visibilityState === "visible") return;
    playDing();
    if (Notification.permission === "granted") {
      const n = new Notification("Accuretta", { body: "Agent finished generating.", icon: "logo-mark-dark.png" });
      n.onclick = () => { window.focus(); n.close(); };
    } else if (Notification.permission === "default") {
      Notification.requestPermission();
    }
  }

  function playApprovalDing() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const playTone = (freq, time, dur) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, time);
        gain.gain.setValueAtTime(0.1, time);
        gain.gain.exponentialRampToValueAtTime(0.01, time + dur);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(time);
        osc.stop(time + dur);
      };
      // A soft double-knock/chime (C5 then E5)
      playTone(523.25, ctx.currentTime, 0.3);
      playTone(659.25, ctx.currentTime + 0.15, 0.4);
    } catch(e) {}
  }

  function notifyApproval() {
    if (document.visibilityState === "visible") return;
    playApprovalDing();
    if (Notification.permission === "granted") {
      const n = new Notification("Accuretta Needs Approval", { body: "The agent requires your permission to proceed.", icon: "logo-mark-dark.png" });
      n.onclick = () => { window.focus(); n.close(); };
    } else if (Notification.permission === "default") {
      Notification.requestPermission();
    }
  }

  // ---------- tool icons (inlined so no extra HTTP / static-whitelist changes) ----------
  const TOOL_SVG = {
    searching_computer: '<svg viewBox="0 0 150.817 150.817" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M58.263,64.946c3.58-8.537,9.834-16.039,18.456-21.02c6.644-3.842,14.225-5.876,21.902-5.876c6.376,0,12.568,1.461,18.207,4.031V21.677C116.829,9.706,92.563,0,62.641,0C32.71,0,8.448,9.706,8.448,21.677v21.681C8.436,54.75,30.372,64.061,58.263,64.946z M62.629,5.416c29.77,0,48.768,9.633,48.768,16.255c0,6.634-18.998,16.258-48.768,16.258c-29.776,0-48.774-9.624-48.774-16.258C13.855,15.049,32.853,5.416,62.629,5.416z M8.429,75.883V54.202c0,10.973,20.396,20.015,46.841,21.449c-1.053,7.21-0.311,14.699,2.375,21.799C30.055,96.445,8.436,87.184,8.429,75.883z M95.425,125.631c-9.109,2.771-20.457,4.445-32.796,4.445c-29.931,0-54.193-9.706-54.193-21.684V86.709c0,11.983,24.256,21.684,54.193,21.684c0.341,0,0.673-0.018,1.014-0.018C71.214,118.373,82.827,124.656,95.425,125.631z M131.296,63.11c-10.388-17.987-33.466-24.174-51.46-13.785c-17.987,10.388-24.173,33.463-13.792,51.45c10.388,17.993,33.478,24.174,51.465,13.798C135.51,104.191,141.684,81.102,131.296,63.11z M71.449,97.657C62.778,82.66,67.945,63.394,82.955,54.72c15.01-8.662,34.275-3.504,42.946,11.509c8.672,15.013,3.502,34.279-11.508,42.943C99.377,117.85,80.117,112.686,71.449,97.657z M139.456,133.852l-16.203,9.353l-12.477-21.598l16.209-9.359L139.456,133.852z M137.708,149.562c-4.488,2.582-10.199,1.06-12.794-3.429l16.216-9.353C143.718,141.268,142.184,146.979,137.708,149.562z"/></svg>',
    computer_search_failed: '<svg viewBox="0 0 139.558 139.558" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M54.19,65.705c29.938,0,54.19-9.703,54.19-21.681V22.344c0-11.977-24.265-21.68-54.19-21.68C24.263,0.664,0,10.367,0,22.344v21.681C0,56.002,24.256,65.705,54.19,65.705z M54.19,6.089c29.773,0,48.771,9.627,48.771,16.255c0,6.628-18.998,16.262-48.771,16.262c-29.772,0-48.771-9.627-48.771-16.255C5.419,15.722,24.418,6.089,54.19,6.089z"/><path d="M54.19,98.225c6.467,0,12.638-0.476,18.39-1.304c4.643-15.381,18.928-26.609,35.801-26.609V54.866c0,11.971-24.265,21.681-54.19,21.681C24.263,76.547,0,66.844,0,54.866v21.681C0,88.518,24.256,98.225,54.19,98.225z"/><path d="M54.19,109.057c-29.934,0-54.19-9.7-54.19-21.678v21.678c0,11.978,24.263,21.684,54.19,21.684c8.306,0,16.148-0.779,23.19-2.107c-3.997-5.906-6.342-13.006-6.394-20.648C65.696,108.673,60.058,109.057,54.19,109.057z"/><path d="M108.381,76.541c-17.214,0-31.176,13.962-31.176,31.176c0,17.215,13.962,31.177,31.176,31.177s31.177-13.962,31.177-31.177C139.558,90.503,125.595,76.541,108.381,76.541z M122.709,115.432l-6.613,6.613l-7.715-7.709l-7.715,7.709l-6.612-6.613l7.708-7.715l-7.708-7.715l6.612-6.613l7.715,7.722l7.715-7.722l6.613,6.613L115,107.717L122.709,115.432z"/></svg>',
    writing_file: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path d="M17.093,1.293l-11.2,11.2a.99.99,0,0,0-.242.391l-1.6,4.8A1,1,0,0,0,5,19a1.014,1.014,0,0,0,.316-.051l4.8-1.6a1.006,1.006,0,0,0,.391-.242l11.2-11.2a1,1,0,0,0,0-1.414l-3.2-3.2A1,1,0,0,0,17.093,1.293ZM9.26,15.526l-2.679.893.893-2.679L17.8,3.414,19.586,5.2ZM3,21H20a1,1,0,0,1,0,2H3a1,1,0,0,1,0-2Z"/></svg>',
    editing_file: '<svg viewBox="0 0 48 48" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5.5,9.2H9.2v0H35.1a3.9828,3.9828,0,0,1,3.7,3.7l.1123,20.7359"/><path d="M9.281,13.7433,9.2,35.1a3.9807,3.9807,0,0,0,3.7,3.7H38.8v0h3.7"/><path d="M16.6,31.4V27.7L27.7,16.6l3.7,3.7L20.3,31.4Z"/></svg>',
    deleted: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><path fill-rule="evenodd" clip-rule="evenodd" d="M10.3094 2.25002H13.6908C13.9072 2.24988 14.0957 2.24976 14.2737 2.27819C14.977 2.39049 15.5856 2.82915 15.9146 3.46084C15.9978 3.62073 16.0573 3.79961 16.1256 4.00494L16.2373 4.33984C16.2562 4.39653 16.2616 4.41258 16.2661 4.42522C16.4413 4.90933 16.8953 5.23659 17.4099 5.24964C17.4235 5.24998 17.44 5.25004 17.5001 5.25004H20.5001C20.9143 5.25004 21.2501 5.58582 21.2501 6.00004C21.2501 6.41425 20.9143 6.75004 20.5001 6.75004H3.5C3.08579 6.75004 2.75 6.41425 2.75 6.00004C2.75 5.58582 3.08579 5.25004 3.5 5.25004H6.50008C6.56013 5.25004 6.5767 5.24998 6.59023 5.24964C7.10488 5.23659 7.55891 4.90936 7.73402 4.42524C7.73863 4.41251 7.74392 4.39681 7.76291 4.33984L7.87452 4.00496C7.94281 3.79964 8.00233 3.62073 8.08559 3.46084C8.41453 2.82915 9.02313 2.39049 9.72643 2.27819C9.90445 2.24976 10.093 2.24988 10.3094 2.25002ZM9.00815 5.25004C9.05966 5.14902 9.10531 5.04404 9.14458 4.93548C9.1565 4.90251 9.1682 4.86742 9.18322 4.82234L9.28302 4.52292C9.37419 4.24941 9.39519 4.19363 9.41601 4.15364C9.52566 3.94307 9.72853 3.79686 9.96296 3.75942C10.0075 3.75231 10.067 3.75004 10.3553 3.75004H13.6448C13.9331 3.75004 13.9927 3.75231 14.0372 3.75942C14.2716 3.79686 14.4745 3.94307 14.5842 4.15364C14.605 4.19363 14.626 4.2494 14.7171 4.52292L14.8169 4.82216L14.8556 4.9355C14.8949 5.04405 14.9405 5.14902 14.992 5.25004H9.00815Z"/><path d="M5.91509 8.45015C5.88754 8.03685 5.53016 7.72415 5.11686 7.7517C4.70357 7.77925 4.39086 8.13663 4.41841 8.54993L4.88186 15.5017C4.96736 16.7844 5.03642 17.8205 5.19839 18.6336C5.36679 19.4789 5.65321 20.185 6.2448 20.7385C6.8364 21.2919 7.55995 21.5308 8.4146 21.6425C9.23662 21.7501 10.275 21.7501 11.5606 21.75H12.4395C13.7251 21.7501 14.7635 21.7501 15.5856 21.6425C16.4402 21.5308 17.1638 21.2919 17.7554 20.7385C18.347 20.185 18.6334 19.4789 18.8018 18.6336C18.9638 17.8206 19.0328 16.7844 19.1183 15.5017L19.5818 8.54993C19.6093 8.13663 19.2966 7.77925 18.8833 7.7517C18.47 7.72415 18.1126 8.03685 18.0851 8.45015L17.6251 15.3493C17.5353 16.6971 17.4713 17.6349 17.3307 18.3406C17.1943 19.025 17.004 19.3873 16.7306 19.6431C16.4572 19.8989 16.083 20.0647 15.391 20.1552C14.6776 20.2485 13.7376 20.25 12.3868 20.25H11.6134C10.2626 20.25 9.32255 20.2485 8.60915 20.1552C7.91715 20.0647 7.54299 19.8989 7.26958 19.6431C6.99617 19.3873 6.80583 19.025 6.66948 18.3406C6.52892 17.6349 6.46489 16.6971 6.37503 15.3493L5.91509 8.45015Z"/><path d="M9.42546 10.2538C9.83762 10.2125 10.2052 10.5133 10.2464 10.9254L10.7464 15.9254C10.7876 16.3376 10.4869 16.7051 10.0747 16.7463C9.66256 16.7875 9.29503 16.4868 9.25381 16.0747L8.75381 11.0747C8.7126 10.6625 9.01331 10.295 9.42546 10.2538Z"/><path d="M14.5747 10.2538C14.9869 10.295 15.2876 10.6625 15.2464 11.0747L14.7464 16.0747C14.7052 16.4868 14.3376 16.7875 13.9255 16.7463C13.5133 16.7051 13.2126 16.3376 13.2538 15.9254L13.7538 10.9254C13.795 10.5133 14.1626 10.2125 14.5747 10.2538Z"/></svg>',
    running_command: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 17H20"/><path d="M5 7L10 12L5 17"/></svg>',
    command_failed: '<svg viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg" fill="currentColor"><rect x="17" y="23" width="6" height="2"/><polygon points="7 24.11 16.6 19.7 16.6 17.89 7 13.48 7 15.68 13.79 18.8 7 21.91 7 24.11"/><path d="M33.68,15.4H32V29H4V10.8H18.68A3.66,3.66,0,0,1,19,9.89l.4-.69H4V7H20.71l1.15-2H4A2,2,0,0,0,2,7V29a2,2,0,0,0,2,2H32a2,2,0,0,0,2-2V15.38Z"/><path d="M26.85,1.14,21.13,11A1.28,1.28,0,0,0,22.23,13H33.68A1.28,1.28,0,0,0,34.78,11L29.06,1.14A1.28,1.28,0,0,0,26.85,1.14Z"/></svg>',
    downloading_file: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11C3 11.9319 3 12.3978 3.15224 12.7654C3.35523 13.2554 3.74458 13.6448 4.23463 13.8478C4.60218 14 5.06812 14 6 14H6.67544C7.25646 14 7.54696 14 7.77888 14.1338C7.83745 14.1675 7.89245 14.2072 7.94303 14.2521C8.14326 14.4298 8.23513 14.7054 8.41886 15.2566L8.54415 15.6325C8.76416 16.2925 8.87416 16.6225 9.13605 16.8112C9.39794 17 9.7458 17 10.4415 17H13.5585C14.2542 17 14.6021 17 14.864 16.8112C15.1258 16.6225 15.2358 16.2925 15.4558 15.6325L15.5811 15.2566C15.7649 14.7054 15.8567 14.4298 16.057 14.2521C16.1075 14.2072 16.1625 14.1675 16.2211 14.1338C16.453 14 16.7435 14 17.3246 14H18C18.9319 14 19.3978 14 19.7654 13.8478C20.2554 13.6448 20.6448 13.2554 20.8478 12.7654C21 12.3978 21 11.9319 21 11"/><path d="M8 9L12 12M12 12L16 9M12 12L12 2"/><path d="M16 5H17C18.8856 5 19.8284 5 20.4142 5.58579C21 6.17157 21 7.11438 21 9V17C21 18.8856 21 19.8284 20.4142 20.4142C19.8284 21 18.8856 21 17 21H7C5.11438 21 4.17157 21 3.58579 20.4142C3 19.8284 3 18.8856 3 17V9C3 7.11438 3 6.17157 3.58579 5.58579C4.17157 5 5.11438 5 7 5H8"/></svg>',
    multiple_tasks_complete: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M8.00009 13L12.2278 16.3821C12.6557 16.7245 13.2794 16.6586 13.6264 16.2345L22.0001 6"/><path d="M9.6434 11.5995L14.5356 5.6201"/><path d="M2.36 13.52L4.87309 16.9049C5.559 17.4193 6.52849 17.3016 7.07142 16.638L8.03225 15.4637"/></svg>',
    globe: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
    // YARA: shield-with-magnifier. The shield is the rule set, the lens is
    // matching — together it reads "scanning for known-bad patterns".
    yara_scanning: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 1.4-.45 2.7-1.1 3.8-1.95"/><circle cx="16.5" cy="14.5" r="3.2"/><path d="m18.9 16.9 2.6 2.6"/></svg>',
    yara_failed: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 4.8-1.5 8-5.5 8-10V5z"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
    // binary_inspect: microchip with pin legs. Reads as "looking inside a
    // compiled artifact" without leaning on a generic file glyph.
    chip_inspecting: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5" rx="0.6"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/></svg>',
    chip_failed: '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="1.5"/><path d="m9.5 9.5 5 5M14.5 9.5l-5 5"/><path d="M9 3v3M12 3v3M15 3v3M9 18v3M12 18v3M15 18v3M3 9h3M3 12h3M3 15h3M18 9h3M18 12h3M18 15h3"/></svg>',
  };
  const TOOL_ICON_MAP = {
    list_directory:  { run: "searching_computer", err: "computer_search_failed" },
    read_file:       { run: "searching_computer", err: "computer_search_failed" },
    grep_files:      { run: "searching_computer", err: "computer_search_failed" },
    write_file:      { run: "writing_file",       err: "command_failed" },
    edit_file:       { run: "editing_file",       err: "command_failed" },
    patch_file:      { run: "editing_file",       err: "command_failed" },
    delete_file:     { run: "deleted",            err: "command_failed" },
    run_powershell:  { run: "running_command",    err: "command_failed" },
    open_program:    { run: "running_command",    err: "command_failed" },
    web_fetch:       { run: "downloading_file",   err: "command_failed" },
    web_search:      { run: "globe",              err: "computer_search_failed" },
    network_snapshot:{ run: "globe",              err: "computer_search_failed" },
    yara_scan:       { run: "yara_scanning",      err: "yara_failed" },
    binary_inspect:  { run: "chip_inspecting",    err: "chip_failed" },
  };
  function renderWebSearchChips(results) {
    if (!results || !results.length) return "";
    const max = 4;
    const visible = results.slice(0, max);
    const overflow = results.length - visible.length;
    const chips = visible.map(r => {
      let host = "";
      try { host = new URL(r.url).hostname.replace(/^www\./, ""); } catch {}
      const label = host || r.url;
      const fav = host ? `https://icons.duckduckgo.com/ip3/${host}.ico` : "";
      // Wrap: globe fallback always rendered; img sits on top, hides itself on error.
      const favHtml = `<span class="web-fav-wrap">${TOOL_SVG.globe}${fav ? `<img class="web-fav" src="${esc(fav)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}</span>`;
      return `<a class="web-chip" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" title="${esc(r.title || r.url)}">${favHtml}<span>${esc(label)}</span></a>`;
    }).join("");
    const moreChip = overflow > 0
      ? `<span class="web-chip web-chip-more">+${overflow} more</span>`
      : "";
    return `<div class="web-results">${chips}${moreChip}</div>`;
  }

  function renderNetworkChart(res) {
    if (!res || res.error) return "";
    const tcp = res.tcp_count || 0;
    const udp = res.udp_count || 0;
    const dns = (res.recent_dns || []).length;
    const procs = (res.top_processes || []).slice(0, 6);
    const remotes = (res.top_remotes || []).slice(0, 6);
    // Force max to be at least 1 so we never divide by zero, and clamp the
    // resulting width to [4, 100]. The chart is purely data-driven — same
    // input always produces the same bars, regardless of which model called
    // the tool. (Previously the absolute-positioned fill resolved its
    // percentage width against the wrong containing block in some layouts,
    // making bars look like they shrank progressively per row.)
    const procMax = Math.max(1, ...procs.map(p => p.connections || 0));
    const remMax  = Math.max(1, ...remotes.map(r => r.count || 0));
    const pct = (val, max) => Math.max(4, Math.min(100, Math.round((val / max) * 100)));
    const stat = (label, n, cls) => `<div class="net-stat ${cls}"><div class="net-stat-num">${n}</div><div class="net-stat-lbl">${label}</div></div>`;
    const procRow = (p) => {
      const v = p.connections || 0;
      const w = pct(v, procMax);
      return `<div class="net-bar-row"><span class="net-bar-lbl" title="${esc(p.process || "?")}">${esc(p.process || "?")}</span><span class="net-bar-track" data-v="${v}" data-max="${procMax}"><span class="net-bar-fill net-bar-proc" style="width:${w}%"></span></span><span class="net-bar-num">${v}</span></div>`;
    };
    const remRow = (r) => {
      const v = r.count || 0;
      const w = pct(v, remMax);
      const lbl = `${r.address || "?"}${r.port ? ":" + r.port : ""}`;
      return `<div class="net-bar-row"><span class="net-bar-lbl" title="${esc(lbl)}">${esc(lbl)}</span><span class="net-bar-track" data-v="${v}" data-max="${remMax}"><span class="net-bar-fill net-bar-rem" style="width:${w}%"></span></span><span class="net-bar-num">${v}</span></div>`;
    };
    const procBlock = procs.length
      ? `<div class="net-block"><div class="net-block-title">Top processes</div>${procs.map(procRow).join("")}</div>`
      : "";
    const remBlock = remotes.length
      ? `<div class="net-block"><div class="net-block-title">Top remote endpoints</div>${remotes.map(remRow).join("")}</div>`
      : "";
    return `<div class="netscan-card">
      <div class="net-stats">${stat("TCP", tcp, "net-stat-tcp")}${stat("UDP", udp, "net-stat-udp")}${stat("DNS", dns, "net-stat-dns")}</div>
      ${procBlock}${remBlock}
    </div>`;
  }

  function toolIconHtml(name, kind /* "run" | "done" | "err" */) {
    const map = TOOL_ICON_MAP[name];
    if (!map) return null;
    const which = kind === "err" ? map.err : map.run;
    const svg = TOOL_SVG[which];
    if (!svg) return null;
    const cls = `tool-svg ${kind === "run" ? "breathing" : kind === "err" ? "is-err" : "is-done"}`;
    return `<span class="${cls}">${svg}</span>`;
  }

  // ---------- friendly tool call labels ----------
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
    "run_powershell", "open_program",
    "desktop_launch_app", "desktop_focus_window", "desktop_click",
    "desktop_type_text", "desktop_press_keys", "desktop_close_window",
  ]);
  // Tools whose result needs rich rendering in the body (chart, etc).
  // Everything else just shows as a tool-line. ALL tools — including web_search
  // and command tools — live in the single per-turn wrench group; nothing
  // bypasses it anymore. That's what stops the vertical stacking.
  const RICH_RESULT_TOOLS = new Set(["network_snapshot"]);
  function isCommandTool(name) { return COMMAND_TOOLS.has(name); }

  // Real SVG wrench (not a phosphor font glyph) — crisper at small sizes and
  // cleaner with our breathing/spin animation. Used as fallback in the tool
  // strip when no specific tool is currently running.
  const WRENCH_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`;

  // Agent avatar: the Accuretta split-A brand mark. We pre-load BOTH theme
  // variants (dark slab on light bg, white slab on dark bg) and let CSS pick
  // which one shows via [data-theme]. Same approach as the sidebar brand
  // mark — keeps theme toggling instant with no fetch lag.
  const AGENT_AVATAR_HTML = `<div class="avatar"><img class="avatar-mark avatar-mark-light" src="logo-mark-light.png" alt="" aria-hidden="true" draggable="false"><img class="avatar-mark avatar-mark-dark" src="logo-mark-dark.png" alt="" aria-hidden="true" draggable="false"></div>`;

  function getOrCreateToolGroup(stack) {
    // ONE group per agent turn, period. The toolStack itself is created fresh
    // for each new agent row, so this naturally scopes to the turn. No more
    // sealing-and-recreating between tool calls (the source of the vertical
    // pill stack the user complained about).
    let group = stack.querySelector(".tool-group");
    if (group) return { group, body: group.querySelector(".tool-group-body") };
    group = document.createElement("div");
    group.className = "tool-group collapsed";
    group.innerHTML = `
      <div class="tool-group-head">
        <span class="tool-group-icon spinning">${WRENCH_SVG}</span>
        <span class="tool-group-activity">working…</span>
        <span class="tool-group-chips" hidden></span>
        <span class="tool-group-summary" hidden></span>
        <i class="ph ph-caret-down chevron"></i>
      </div>
      <div class="tool-group-body"></div>`;
    const head = group.querySelector(".tool-group-head");
    head.addEventListener("click", () => group.classList.toggle("collapsed"));
    stack.appendChild(group);
    return { group, body: group.querySelector(".tool-group-body") };
  }

  // Update the head to reflect the most-recently-started running tool: swap
  // the icon to that tool's actual SVG and update the activity label. This is
  // what gives the "tool icon refreshes as the model chains tools" behavior
  // — no permanent wrench placeholder, the head IS the live tool.
  function updateToolGroupActivity(group, evt) {
    if (!group || !evt) return;
    const activity = group.querySelector(".tool-group-activity");
    const iconSlot = group.querySelector(".tool-group-icon");
    if (activity) {
      activity.classList.add("shimmer");
      activity.textContent = toolLabel(evt.name, evt.arguments).replace(/…$/, "");
    }
    if (iconSlot) {
      // Swap with a brief fade so chained tools visibly "refresh" rather
      // than snap-replace.
      iconSlot.classList.remove("icon-in");
      iconSlot.classList.add("icon-out");
      const map = TOOL_ICON_MAP[evt.name];
      const svg = (map && TOOL_SVG[map.run]) || WRENCH_SVG;
      setTimeout(() => {
        iconSlot.innerHTML = svg;
        iconSlot.classList.remove("icon-out");
        iconSlot.classList.add("icon-in", "spinning");
      }, 120);
    }
  }

  // Render web-search chips into the head's chip strip. New searches REPLACE
  // the chip set with a fade-in animation — gives the "rotating sources" feel
  // the user asked for without stacking.
  function refreshHeadChips(group, results) {
    if (!group) return;
    const slot = group.querySelector(".tool-group-chips");
    if (!slot) return;
    if (!results || !results.length) return;
    const max = 4;
    const chips = results.slice(0, max).map(r => {
      let host = "";
      try { host = new URL(r.url).hostname.replace(/^www\./, ""); } catch {}
      const label = host || r.url;
      const fav = host ? `https://icons.duckduckgo.com/ip3/${host}.ico` : "";
      const favHtml = `<span class="tool-chip-fav">${TOOL_SVG.globe}${fav ? `<img src="${esc(fav)}" alt="" loading="lazy" onerror="this.style.display='none'">` : ""}</span>`;
      return `<a class="tool-chip" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer" title="${esc(r.title || r.url)}">${favHtml}<span>${esc(label)}</span></a>`;
    }).join("");
    const overflow = results.length - Math.min(max, results.length);
    const more = overflow > 0 ? `<span class="tool-chip tool-chip-more">+${overflow}</span>` : "";
    slot.hidden = false;
    // Brief swap animation: fade out → swap → fade in.
    slot.classList.remove("chips-in");
    slot.classList.add("chips-out");
    setTimeout(() => {
      slot.innerHTML = chips + more;
      slot.classList.remove("chips-out");
      slot.classList.add("chips-in");
    }, 140);
  }

  // Called when the agent turn fully ends (chat_end, stream done, or stop).
  // Moves the tool group from its top-of-column position to AFTER the answer
  // bubble and adds the .done-pill class so the strip looks faded/detached
  // rather than like part of the answer. Per user request: "should be under
  // the models final response, faded, not look like part of the answer bubble."
  function finalizeToolGroup(row) {
    if (!row) return;
    const stack = row.querySelector(".tool-stack");
    const bubble = row.querySelector(".bubble");
    if (!stack || !bubble) return;
    const group = stack.querySelector(".tool-group");
    if (!group) {
      // No tools ran — clean the empty stack out so it doesn't leave a gap.
      stack.remove();
      return;
    }
    // Make sure summary is computed (running flag is now false).
    updateToolGroupHead(stack);
    
    // Reset the head icon back to the wrench — in finalized state the strip
    // represents "tools the model used" generically, not a specific live tool.
    const iconSlot = group.querySelector(".tool-group-icon");
    if (iconSlot) {
      iconSlot.classList.remove("spinning", "icon-out", "icon-in");
      iconSlot.innerHTML = WRENCH_SVG;
    }
    // Move strip after the bubble in the bubble-col.
    const col = bubble.parentNode;
    if (col && bubble.nextSibling !== group) {
      col.insertBefore(group, bubble.nextSibling);
    }
    // Empty stack node can go now.
    if (!stack.children.length) stack.remove();
  }

  function updateToolGroupHead(stack) {
    const group = stack.querySelector(".tool-group");
    if (!group) return;
    const cards = group.querySelectorAll(".tool-line");
    const running = group.querySelectorAll(".tool-line.running");
    const done = group.querySelectorAll(".tool-line.done").length;
    const err = group.querySelectorAll(".tool-line.err").length;
    const icon = group.querySelector(".tool-group-icon");
    const activity = group.querySelector(".tool-group-activity");
    const summary = group.querySelector(".tool-group-summary");
    if (running.length > 0) {
      icon?.classList.add("spinning");
      // Activity stays as set by updateToolGroupActivity (the most recent
      // tool_start label). Don't overwrite mid-run.
      activity.hidden = false;
      summary.hidden = true;
      group.classList.remove("done-pill");
    } else {
      icon?.classList.remove("spinning");
      activity.hidden = true;
      summary.hidden = false;
      group.classList.add("done-pill");
      // Count commands separately from other tools so the summary reads as
      // "X tools · Y commands" — the user's exact ask.
      let cmd = 0, tools = 0;
      cards.forEach(c => {
        if (isCommandTool(c.dataset.name)) cmd++;
        else tools++;
      });
      const parts = [];
      let html = "";
      
      // Get compressed messages count from row, if any
      const row = group.closest(".bubble-row");
      const dropped = row && row.dataset.dropped ? parseInt(row.dataset.dropped, 10) : 0;
      
      if (dropped > 0) {
        html += `<span class="summary-item"><i class="ph ph-arrows-in-line-horizontal"></i> ${dropped} msgs</span>`;
      }
      if (tools > 0) {
        if (html) html += `<span class="dot-sep"></span>`;
        html += `<span class="summary-item"><i class="ph ph-gear"></i> ${tools}</span>`;
      }
      if (cmd > 0) {
        if (html) html += `<span class="dot-sep"></span>`;
        // Replace SVG width/height or just use it raw. 
        // The user asked to use TOOL_SVG.running_command.
        html += `<span class="summary-item cmd-item">${TOOL_SVG.running_command} ${cmd}</span>`;
      }
      if (err > 0) {
        if (html) html += `<span class="dot-sep"></span>`;
        html += `<span class="summary-item err-item"><i class="ph ph-warning"></i> ${err} failed</span>`;
        group.classList.add("has-err");
      } else {
        group.classList.remove("has-err");
      }
      
      if (html) {
        summary.innerHTML = html;
      } else {
        summary.textContent = `${done} step${done === 1 ? "" : "s"}`;
      }
    }
  }

  function toolLabel(name, args) {
    args = args || {};
    switch (name) {
      case "list_directory": return `Looking in ${shortPath(args.path) || "folder"}…`;
      case "read_file":      return `Reading ${shortPath(args.path)}…`;
      case "write_file":     return `Writing ${shortPath(args.path)}…`;
      case "edit_file":      return `Editing ${shortPath(args.path)}…`;
      case "delete_file":    return `Deleting ${shortPath(args.path)}…`;
      case "run_powershell": return `Running command…`;
      case "open_program":   return `Opening ${args.name || args.path || "program"}…`;
      case "web_fetch":      return `Fetching ${args.url || "the web"}…`;
      case "network_snapshot": return `Scanning network…`;
      case "scan_apk":         return `Scanning APK${args.path ? " " + shortPath(args.path) : ""}…`;
      case "decompile_apk":    return `Decompiling APK${args.path ? " " + shortPath(args.path) : ""}…`;
      case "ghidra_analyze":   return `Analyzing with Ghidra${args.path ? " · " + shortPath(args.path) : ""}…`;
      case "binary_inspect":   return `Inspecting binary${args.path ? " · " + shortPath(args.path) : ""}…`;
      case "yara_scan":        return `Scanning with YARA${args.path ? " · " + shortPath(args.path) : ""}…`;
      default:               return `Running ${name || "tool"}…`;
    }
  }
  function toolResultLabel(name, res) {
    res = res || {};
    if (res.error) return `${name}: ${String(res.error).slice(0, 120)}`;
    switch (name) {
      case "list_directory": {
        const n = (res.entries || []).length;
        return `Found ${n} item${n === 1 ? "" : "s"}${res.path ? " in " + shortPath(res.path) : ""}`;
      }
      case "read_file":      return `Read ${shortPath(res.path)}${res.bytes != null ? ` (${res.bytes} bytes)` : ""}`;
      case "write_file":     return `Wrote ${shortPath(res.path)}`;
      case "edit_file":      return `Edited ${shortPath(res.path)} · ${res.edits_applied || 0} change${(res.edits_applied || 0) === 1 ? "" : "s"}`;
      case "delete_file":    return `Deleted ${shortPath(res.path)}`;
      case "run_powershell": {
        const out = (res.stdout || "").trim();
        const first = out.split(/\r?\n/)[0] || "(no output)";
        return `Done · ${first.slice(0, 120)}`;
      }
      case "open_program":   return `Opened ${res.name || ""}`;
      case "web_fetch":      return `Fetched ${shortPath(res.url)}`;
      case "network_snapshot": {
        const t = res.tcp_count || 0;
        const u = res.udp_count || 0;
        const d = (res.recent_dns || []).length;
        return `Scan: ${t} TCP · ${u} UDP · ${d} DNS entries`;
      }
      case "scan_apk": {
        const findings = (res.secret_findings || []).length;
        const perms = (res.dangerous_permissions || []).length;
        return `APK scanned · ${findings} secret hit${findings === 1 ? "" : "s"} · ${perms} dangerous perm${perms === 1 ? "" : "s"}`;
      }
      case "decompile_apk": {
        const j = res.output_summary && res.output_summary.java_count;
        return `Decompiled${j != null ? ` · ${j} class${j === 1 ? "" : "es"}` : ""}`;
      }
      case "ghidra_analyze": {
        const fc = res.function_count || 0;
        const ic = res.import_count || 0;
        const rs = (res.risk_summary || []).length;
        return `Ghidra · ${fc} func · ${ic} import${ic === 1 ? "" : "s"}${rs ? ` · ${rs} risk hit${rs === 1 ? "" : "s"}` : ""}`;
      }
      case "binary_inspect": {
        const det = res.details || {};
        const fmt = res.format || "?";
        const arch = det.arch ? ` ${det.arch}` : "";
        const imp = det.import_total != null ? det.import_total : (det.import_count || 0);
        const signed = (fmt === "PE" && det.signed === true) ? " · signed" : (fmt === "PE" && det.signed === false ? " · unsigned" : "");
        const risks = (res.risk_summary || []).length;
        return `${fmt}${arch} · ${imp} import${imp === 1 ? "" : "s"}${signed}${risks ? ` · ${risks} risk hit${risks === 1 ? "" : "s"}` : ""}`;
      }
      case "yara_scan": {
        const hits = res.files_with_matches || 0;
        const rules = (res.rules_fired || []).length;
        const scanned = res.files_scanned || 0;
        return `YARA · ${hits}/${scanned} file${scanned === 1 ? "" : "s"} hit · ${rules} rule${rules === 1 ? "" : "s"}`;
      }
      default:               return `${name} complete`;
    }
  }

  // ---------- lightweight syntax highlighter ----------
  // Single-pass tokenizer for chat code fences. Conservative on purpose —
  // false positives in a code block look uglier than no highlighting at all.
  // Per language: keyword set + comment style. Strings, numbers, and basic
  // punctuation are handled by the shared base tokenizer.
  //
  // Emits HTML (already escaped) so the result drops straight into <code>.
  // Falls through to plain esc() for unknown / unsupported langs.
  const LANG_KEYWORDS = {
    js: new Set(("var let const function return if else for while do switch case break continue " +
      "new typeof instanceof in of delete void this super class extends static get set " +
      "import export from as default async await yield try catch finally throw " +
      "true false null undefined").split(/\s+/)),
    ts: new Set(("var let const function return if else for while do switch case break continue " +
      "new typeof instanceof in of delete void this super class extends implements interface type " +
      "enum static get set import export from as default async await yield try catch finally throw " +
      "public private protected readonly abstract namespace declare " +
      "true false null undefined string number boolean any void never unknown").split(/\s+/)),
    py: new Set(("def class return if elif else for while break continue pass import from as " +
      "with try except finally raise yield lambda global nonlocal in is not and or " +
      "True False None async await match case").split(/\s+/)),
    sh: new Set(("if then else elif fi for while do done case esac in function return " +
      "echo export local readonly set unset source exit").split(/\s+/)),
    bash: new Set(("if then else elif fi for while do done case esac in function return " +
      "echo export local readonly set unset source exit").split(/\s+/)),
    powershell: new Set(("if elseif else switch foreach for while do until break continue return " +
      "function param begin process end try catch finally throw " +
      "true false null").split(/\s+/)),
    ps1: new Set(("if elseif else switch foreach for while do until break continue return " +
      "function param begin process end try catch finally throw " +
      "true false null").split(/\s+/)),
    css: new Set(("important inherit initial unset auto none").split(/\s+/)),
    sql: new Set(("select from where insert update delete into values set join inner left right outer " +
      "on as group by order having limit offset distinct union all create table drop alter index").split(/\s+/)),
    json: new Set(("true false null").split(/\s+/)),
  };
  // Aliases that map to a base language
  const LANG_ALIAS = {
    javascript: "js", node: "js", jsx: "js",
    typescript: "ts", tsx: "ts",
    python: "py", py3: "py",
    shell: "sh", zsh: "sh", bash: "bash",
    pwsh: "powershell",
    yml: "yaml",
  };
  // Comment styles per language (line + optional block)
  const LANG_COMMENTS = {
    js: { line: "//", block: ["/*", "*/"] },
    ts: { line: "//", block: ["/*", "*/"] },
    py: { line: "#", block: null },
    sh: { line: "#", block: null },
    bash: { line: "#", block: null },
    powershell: { line: "#", block: ["<#", "#>"] },
    css: { line: null, block: ["/*", "*/"] },
    sql: { line: "--", block: ["/*", "*/"] },
    yaml: { line: "#", block: null },
    rust: { line: "//", block: ["/*", "*/"] },
    go: { line: "//", block: ["/*", "*/"] },
    c: { line: "//", block: ["/*", "*/"] },
    cpp: { line: "//", block: ["/*", "*/"] },
    java: { line: "//", block: ["/*", "*/"] },
  };

  function highlightCode(rawCode, lang) {
    const code = String(rawCode == null ? "" : rawCode);
    const baseLang = LANG_ALIAS[lang] || lang || "";
    // HTML/XML get a dedicated path (tag/attr/string).
    if (baseLang === "html" || baseLang === "xml" || baseLang === "svg") {
      return highlightMarkup(code);
    }
    const kw = LANG_KEYWORDS[baseLang] || null;
    const cmt = LANG_COMMENTS[baseLang] || null;
    // No spec for this language → return safely escaped only.
    if (!kw && !cmt) return esc(code);

    let out = "";
    let i = 0;
    const n = code.length;
    const isIdStart = c => /[A-Za-z_$]/.test(c);
    const isIdCont  = c => /[A-Za-z0-9_$]/.test(c);

    while (i < n) {
      const c = code[i];
      const c2 = code.slice(i, i + 2);

      // Block comments
      if (cmt && cmt.block && code.startsWith(cmt.block[0], i)) {
        const end = code.indexOf(cmt.block[1], i + cmt.block[0].length);
        const stop = end === -1 ? n : end + cmt.block[1].length;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      // Line comments
      if (cmt && cmt.line && code.startsWith(cmt.line, i)) {
        const nl = code.indexOf("\n", i);
        const stop = nl === -1 ? n : nl;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      // Strings: ", ', `
      if (c === '"' || c === "'" || c === "`") {
        const quote = c;
        let j = i + 1;
        while (j < n) {
          if (code[j] === "\\" && j + 1 < n) { j += 2; continue; }
          if (code[j] === quote) { j++; break; }
          j++;
        }
        out += `<span class="tok-string">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Numbers (basic — int / float / hex)
      if (/\d/.test(c) || (c === "." && /\d/.test(code[i + 1] || ""))) {
        let j = i;
        if (c === "0" && /[xX]/.test(code[i + 1] || "")) {
          j = i + 2;
          while (j < n && /[0-9a-fA-F_]/.test(code[j])) j++;
        } else {
          while (j < n && /[0-9_]/.test(code[j])) j++;
          if (code[j] === "." && /\d/.test(code[j + 1] || "")) {
            j++;
            while (j < n && /[0-9_]/.test(code[j])) j++;
          }
          if (/[eE]/.test(code[j] || "")) {
            j++;
            if (/[+-]/.test(code[j] || "")) j++;
            while (j < n && /\d/.test(code[j])) j++;
          }
        }
        out += `<span class="tok-number">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Identifiers (keywords / function calls)
      if (isIdStart(c)) {
        let j = i + 1;
        while (j < n && isIdCont(code[j])) j++;
        const word = code.slice(i, j);
        if (kw && kw.has(word)) {
          out += `<span class="tok-keyword">${esc(word)}</span>`;
        } else if (code[j] === "(") {
          out += `<span class="tok-fn">${esc(word)}</span>`;
        } else {
          out += esc(word);
        }
        i = j;
        continue;
      }
      // Shell variable: $name or ${name}
      if (c === "$" && (baseLang === "sh" || baseLang === "bash")) {
        let j = i + 1;
        if (code[j] === "{") {
          const end = code.indexOf("}", j);
          j = end === -1 ? n : end + 1;
        } else {
          while (j < n && isIdCont(code[j])) j++;
        }
        out += `<span class="tok-var">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // PowerShell variable: $name
      if (c === "$" && (baseLang === "powershell")) {
        let j = i + 1;
        while (j < n && (isIdCont(code[j]) || code[j] === ":")) j++;
        out += `<span class="tok-var">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Python decorator
      if (c === "@" && baseLang === "py" && isIdStart(code[i + 1] || "")) {
        let j = i + 1;
        while (j < n && (isIdCont(code[j]) || code[j] === ".")) j++;
        out += `<span class="tok-decorator">${esc(code.slice(i, j))}</span>`;
        i = j;
        continue;
      }
      // Punctuation / whitespace — passthrough (escaped)
      out += esc(c);
      i++;
    }
    return out;
  }

  // Split highlighted HTML on \n while keeping multi-line token spans
  // (docstrings, block comments) properly closed before the break and
  // reopened on the next line so coloring stays continuous. Returns an
  // array of per-line HTML strings — wrap them however the caller wants.
  function splitHighlightedLines(html) {
    const lines = [];
    let cur = "";
    let openTag = null;
    let i = 0;
    const n = html.length;
    while (i < n) {
      const c = html[i];
      if (c === "<") {
        const end = html.indexOf(">", i);
        if (end === -1) { cur += html.slice(i); break; }
        const tag = html.slice(i, end + 1);
        if (tag.startsWith("</span")) openTag = null;
        else if (tag.startsWith("<span")) openTag = tag;
        cur += tag;
        i = end + 1;
        continue;
      }
      if (c === "\n") {
        if (openTag) cur += "</span>";
        lines.push(cur);
        cur = openTag ? openTag : "";
        i++;
        continue;
      }
      cur += c;
      i++;
    }
    if (cur.length || lines.length === 0) {
      if (openTag) cur += "</span>";
      lines.push(cur);
    }
    return lines;
  }

  // Wrap each line of the highlighted-HTML output in <span class="code-line">
  // so the line-number gutter (CSS counters) can index them and the body can
  // wrap visually if the user resizes the bubble. If a token <span> straddles
  // a newline (multi-line strings, block comments), we close it before the
  // break and reopen it on the next line so coloring stays continuous.
  function wrapCodeLines(html) {
    const lines = [];
    let cur = "";
    let openTag = null; // current <span ...> tag, if any (our tokens never nest)
    let i = 0;
    const n = html.length;
    while (i < n) {
      const c = html[i];
      if (c === "<") {
        const end = html.indexOf(">", i);
        if (end === -1) { cur += html.slice(i); break; }
        const tag = html.slice(i, end + 1);
        if (tag.startsWith("</span")) openTag = null;
        else if (tag.startsWith("<span")) openTag = tag;
        cur += tag;
        i = end + 1;
        continue;
      }
      if (c === "\n") {
        if (openTag) cur += "</span>";
        lines.push(cur);
        cur = openTag ? openTag : "";
        i++;
        continue;
      }
      cur += c;
      i++;
    }
    if (cur.length || lines.length === 0) {
      if (openTag) cur += "</span>";
      lines.push(cur);
    }
    // Each line: a row span with optional inner content. Empty lines render
    // as a blank row — we still want a number for them.
    return lines.map(line => `<span class="code-line">${line || "\u200b"}</span>`).join("");
  }

  // Markup highlighter for html/xml/svg fences.
  function highlightMarkup(code) {
    // Walk the source, treating <...> as tag spans with attribute tokenizing
    // inside. Outside of tags, just escape the body text. Comments and
    // doctype get their own classes.
    let out = "";
    let i = 0;
    const n = code.length;
    while (i < n) {
      // Comment
      if (code.startsWith("<!--", i)) {
        const end = code.indexOf("-->", i + 4);
        const stop = end === -1 ? n : end + 3;
        out += `<span class="tok-comment">${esc(code.slice(i, stop))}</span>`;
        i = stop;
        continue;
      }
      if (code[i] === "<") {
        const end = code.indexOf(">", i);
        if (end === -1) {
          out += esc(code.slice(i));
          break;
        }
        const tag = code.slice(i, end + 1);
        // tokenize the tag: <tagname attr="value" attr=value>
        let inner = "";
        const m = tag.match(/^<\s*\/?\s*([a-zA-Z][\w:-]*)?/);
        const tagName = m && m[1] ? m[1] : "";
        let pos = 0;
        const lt = tag.match(/^<\s*\/?\s*/)[0];
        inner += `<span class="tok-punct">${esc(lt)}</span>`;
        pos = lt.length;
        if (tagName) {
          inner += `<span class="tok-tag">${esc(tagName)}</span>`;
          pos += tagName.length;
        }
        // attribute tokens: name(=value)?
        while (pos < tag.length - 1) {
          const rest = tag.slice(pos, tag.length - 1);
          const ws = rest.match(/^\s+/);
          if (ws) { inner += esc(ws[0]); pos += ws[0].length; continue; }
          const am = rest.match(/^([a-zA-Z_:][\w:.-]*)/);
          if (am) {
            inner += `<span class="tok-attr">${esc(am[1])}</span>`;
            pos += am[1].length;
            const after = tag.slice(pos, tag.length - 1);
            const eq = after.match(/^\s*=\s*/);
            if (eq) {
              inner += `<span class="tok-punct">${esc(eq[0])}</span>`;
              pos += eq[0].length;
              const after2 = tag.slice(pos, tag.length - 1);
              const sm = after2.match(/^("[^"]*"|'[^']*'|[^\s>]+)/);
              if (sm) {
                inner += `<span class="tok-string">${esc(sm[1])}</span>`;
                pos += sm[1].length;
              }
            }
            continue;
          }
          // unknown char in tag — passthrough
          inner += esc(rest[0]);
          pos += 1;
        }
        // closing >
        inner += `<span class="tok-punct">${esc(tag.slice(tag.length - 1))}</span>`;
        out += inner;
        i = end + 1;
        continue;
      }
      // Body text up to next "<"
      const next = code.indexOf("<", i);
      const stop = next === -1 ? n : next;
      out += esc(code.slice(i, stop));
      i = stop;
    }
    return out;
  }

  // Linear scanner that finds `<tool_call>{"name":"write_file","arguments":
  // {..."content":"<HTML>","path":...}}</tool_call>` blobs and rewrites them
  // to a clean ```html``` fence. Walks the string once with indexOf — no
  // regex backtracking, safe on multi-MB inputs. Tolerates: missing closing
  // </tool_call> tag, attribute order (path before/after content), trailing
  // truncation. The HTML body is JSON-unescaped on the way out.
  function decodeJsonStringBody(body) {
    const SENT = "\x00BS\x00";
    return body
      .split("\\\\").join(SENT)
      .split("\\n").join("\n")
      .split("\\r").join("\r")
      .split("\\t").join("\t")
      .split('\\"').join('"')
      .split("\\'").join("'")
      .split("\\/").join("/")
      .split(SENT).join("\\");
  }
  function rewriteWriteFileToolCallToFence(text) {
    if (!text || text.indexOf("write_file") === -1) return text;
    let out = "";
    let i = 0;
    const n = text.length;
    while (i < n) {
      // Find next `<tool_call>` (case-insensitive — but the format is fixed
      // by the prompt, so a literal lowercase indexOf is enough in practice).
      const tcStart = text.indexOf("<tool_call>", i);
      if (tcStart === -1) { out += text.slice(i); break; }
      // Quickly check if this tool_call mentions write_file before doing the
      // heavier scan — bail and keep the text untouched if not.
      const tcEndCandidate = text.indexOf("</tool_call>", tcStart);
      const sniffEnd = tcEndCandidate === -1 ? Math.min(n, tcStart + 200) : tcEndCandidate;
      if (text.slice(tcStart, sniffEnd).indexOf('"write_file"') === -1) {
        // Not us — copy through this opener and keep going past it.
        out += text.slice(i, tcStart + "<tool_call>".length);
        i = tcStart + "<tool_call>".length;
        continue;
      }
      // Find `"content"` somewhere after the opener.
      const contentKey = text.indexOf('"content"', tcStart);
      if (contentKey === -1) {
        // malformed — emit the opener and keep going (the strippers below
        // will clean it).
        out += text.slice(i, tcStart + "<tool_call>".length);
        i = tcStart + "<tool_call>".length;
        continue;
      }
      // Skip whitespace + colon + whitespace, expect an opening `"` for the value.
      let j = contentKey + '"content"'.length;
      while (j < n && (text[j] === ' ' || text[j] === '\t' || text[j] === '\n' || text[j] === '\r')) j++;
      if (text[j] !== ':') { out += text.slice(i, tcStart); i = tcStart; break; }
      j++;
      while (j < n && (text[j] === ' ' || text[j] === '\t' || text[j] === '\n' || text[j] === '\r')) j++;
      if (text[j] !== '"') { out += text.slice(i, tcStart); i = tcStart; break; }
      j++; // now positioned at first char of the JSON-string body
      const bodyStart = j;
      // Walk the body, honoring `\` escapes, until we hit an unescaped `"`
      // OR end-of-text (truncated). Linear, no backtracking.
      let bodyEnd = -1;
      while (j < n) {
        const c = text.charCodeAt(j);
        if (c === 92 /* \ */) { j += 2; continue; }
        if (c === 34 /* " */) { bodyEnd = j; break; }
        j++;
      }
      const body = text.slice(bodyStart, bodyEnd === -1 ? n : bodyEnd);
      // Find end of the whole tool_call to skip past it. If no closer, eat
      // through any trailing `}}</tool_call>` tail or to end-of-text.
      let blockEnd;
      if (bodyEnd === -1) {
        blockEnd = n;
      } else {
        const closer = text.indexOf("</tool_call>", bodyEnd);
        blockEnd = closer === -1 ? n : closer + "</tool_call>".length;
      }
      // Emit the prefix, the rewritten fence, then continue scanning.
      out += text.slice(i, tcStart);
      const decoded = decodeJsonStringBody(body);
      out += "\n```html\n" + decoded + "\n```\n";
      i = blockEnd;
    }
    return out;
  }

  // Some local fine-tunes emit fenced HTML/code already JSON-string-escaped:
  // real newlines become the two-character sequence `\n`, real quotes become
  // `\"`, etc. The browser renders those backslash-letter pairs as visible
  // text in the preview iframe and the bubble code card collapses to a single
  // unbroken horizontal line. Detect heavy escape-sequence usage with very
  // few real newlines and decode in one pass (sentinel guards `\\` from
  // double-processing). Mirrors _maybe_unescape_json_html in bridge.py.
  function maybeUnescapeJsonFence(code) {
    if (!code || code.indexOf("\\") === -1) return code;
    const realNewlines = (code.match(/\n/g) || []).length;
    const escapedN = (code.match(/\\n/g) || []).length;
    const escapedQuote = (code.match(/\\"/g) || []).length;
    if (escapedN < 3 && escapedQuote < 3) return code;
    if (realNewlines >= Math.max(5, Math.floor(escapedN / 2))) return code;
    const SENT = "\x00BS\x00";
    return code
      .split("\\\\").join(SENT)
      .split("\\n").join("\n")
      .split("\\r").join("\r")
      .split("\\t").join("\t")
      .split('\\"').join('"')
      .split("\\'").join("'")
      .split("\\/").join("/")
      .split(SENT).join("\\");
  }

  // ---------- markdown-lite for chat bubbles ----------
  // Preserves code fences, ignores tool_call tags (rendered as tool cards separately).
  function renderMarkdown(text) {
    if (!text) return "";

    // === FAILSAFE: STRIP CASCADE TAGS ===
    // Force-strip proactive tags right before the renderer so they can never bleed into the UI.
    text = text.replace(/(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*?)(?:<|&lt;|\\<)\/cascade(?:>|&gt;|\\>)/gi, "");
    text = text.replace(/(?:<|&lt;|\\<)cascade[\s\S]*$/gi, "");
    text = text.replace(/(?:<|&lt;|\\<)c(?:a(?:s(?:c(?:a(?:d(?:e)?)?)?)?)?)?$/gi, "");

    // === REWRITE write_file TOOL_CALL BACK TO ```html``` FENCE ===
    // Some models (Qwen3, DeepSeek-distilled) ignore IDE-mode prompts and wrap
    // the requested HTML inside a `write_file` tool_call. Bridge intercepts it
    // for the preview pane, but the chat bubble would either show raw JSON or
    // (after the strippers below) be empty. Pull the content out and present
    // it as a normal ```html``` fence so the user gets a code-card.
    //
    // Implementation note: a regex with `[\s\S]*?` around `(?:\\.|[^"\\])*`
    // catastrophic-backtracks on 30KB+ HTML bodies and freezes the tab
    // ("Page Unresponsive"). Use a linear indexOf-based scanner instead —
    // walk the string char-by-char, no backtracking ever.
    text = rewriteWriteFileToolCallToFence(text);

    // === STRIP ALL TOOL CALL FORMATS ===
    // Closed forms — the parser already executed these; just clean the bubble.
    text = text.replace(/<tool_call>[\s\S]*?<\/tool_call>/gi, "");
    text = text.replace(/```tool_call[\s\S]*?```/gi, "");
    // BugTraceAI / gemma-tunes:  <call:NAME>{...}</call(:NAME)?>
    text = text.replace(/<call:[a-zA-Z0-9_\-]+>[\s\S]*?<\/call(?::[a-zA-Z0-9_\-]+)?>/gi, "");
    // Llama 3.x native:  <|python_tag|>{...}(<|eom_id|>|<|eot_id|>)
    text = text.replace(/<\|python_tag\|>[\s\S]*?(<\|eom_id\|>|<\|eot_id\|>)/gi, "");
    // Mistral native:  [TOOL_CALLS][{...}]
    text = text.replace(/\[TOOL_CALLS\]\s*\[[\s\S]*?\]/gi, "");
    // Gemma 4 native: <|tool_call>call:NAME{...}<tool_call|>
    text = text.replace(/<\|tool_call>[\s\S]*?(?:<tool_call\|>)/gi, "");
    // Gemma python dialect: ```tool_code\n name(arg=value) \n``` (the bridge
    // parsed + executed this; strip the fence so the bubble isn't raw code).
    text = text.replace(/```tool_code[\s\S]*?```/gi, "");
    // Self-closing XML tag: <tool_call name="..." />
    text = text.replace(/<tool_call\s+[^>]*?\/>/gi, "");
    
    // Partial / streaming open-only forms — the closer hasn't arrived yet,
    // so the regex above can't catch them and the user sees raw tag spam
    // flicker mid-stream. Strip from the open tag to end-of-text.
    text = text.replace(/<tool_call>[\s\S]*$/gi, "");
    text = text.replace(/<call:[a-zA-Z0-9_\-]+>[\s\S]*$/gi, "");
    text = text.replace(/<\|python_tag\|>[\s\S]*$/gi, "");
    text = text.replace(/<\|tool_call>[\s\S]*$/gi, "");
    text = text.replace(/```tool_code[\s\S]*$/gi, "");
    text = text.replace(/\[TOOL_CALLS\][\s\S]*$/gi, "");
    text = text.replace(/```tool_call[\s\S]*$/gi, "");
    text = text.replace(/```json\s*\{[\s\S]*?"name"[\s\S]*?\}\s*```/gi, "");
    text = text.replace(/```json\s*\{[\s\S]*?"function"[\s\S]*?\}\s*```/gi, "");
    text = text.replace(/<function_calls>[\s\S]*?<\/function_calls>/gi, "");
    text = text.replace(/<functions>[\s\S]*?<\/functions>/gi, "");
    text = text.replace(/<invoke>[\s\S]*?<\/invoke>/gi, "");
    text = text.replace(/<tool>[\s\S]*?<\/tool>/gi, "");
    text = text.replace(/\{\s*"name"\s*:\s*"[^"]+"\s*,\s*"arguments"\s*:\s*\{[\s\S]*?\}\s*\}/g, "");
    text = text.replace(/\{\s*"function"\s*:\s*"[^"]+"\s*,\s*"parameter(?:s)?"\s*:\s*\{[\s\S]*?\}\s*\}/g, "");
    text = text.replace(/\[[\s\S]*?\{[\s\S]*?"name"[\s\S]*?"arguments"[\s\S]*?\}[\s\S]*?\]/g, "");
    text = text.replace(/\[[\s\S]*?\{[\s\S]*?"function"[\s\S]*?"parameter(?:s)?"[\s\S]*?\}[\s\S]*?\]/g, "");
    text = text.replace(/\*\*Tool call:.*?\*\*/gi, "");
    text = text.replace(/\*\*Function call:.*?\*\*/gi, "");
    text = text.replace(/Calling\s+\w+\s*\(.*?\)\s*\.\.\./gi, "");
    text = text.replace(/\[\s*\d+\s*tool\s*calls?\s*\]/gi, "");
    text = text.replace(/\n{3,}/g, "\n\n");
    text = text.trim();

    // extract code fences
    const fences = [];
    text = text.replace(/```([^\n]+)?\n?([\s\S]*?)```/g, (_m, infoStr, code) => {
      fences.push({ infoStr: infoStr || "", code: maybeUnescapeJsonFence(code) });
      return `\x00F${fences.length - 1}\x00`;
    });

    // extract short-term memory markers (bridge.py wraps prior <think> blocks
    // as [scratchpad-from-earlier-turn]…[/scratchpad-from-earlier-turn] — the
    // wire marker stays "scratchpad" so existing chat histories keep rendering
    // correctly, but the UI brand is "short-term memory"). Render inline as a
    // small icon + italic body instead of leaking the literal tag text.
    const stmBlocks = [];
    text = text.replace(
      /\[scratchpad-from-earlier-turn\]([\s\S]*?)\[\/scratchpad-from-earlier-turn\]/gi,
      (_m, body) => {
        stmBlocks.push((body || "").trim());
        return `\x00S${stmBlocks.length - 1}\x00`;
      }
    );

    // Strip bare inline HTML emitted by chatty models (Gemma in particular
    // sprinkles <b>, <i>, <br>, <p> between sentences from web-trained habit).
    // We render via markdown-lite + esc(), so an unescaped tag would show up
    // as literal "<b>" text. Code fences are already extracted to placeholders
    // above, so this can't touch any real <b> the user pasted into a code
    // block. Keep the list narrow — only zero-arg formatting tags whose
    // semantics map to "do nothing" in chat. Tags with attributes or any
    // other HTML are left alone (esc() will turn them into visible literals,
    // which is the safer default for unexpected input).
    text = text.replace(/<\/?(?:b|i|u|em|strong|small|big|sub|sup|mark|ins|del|s|strike)\s*>/gi, "");
    text = text.replace(/<br\s*\/?\s*>/gi, "\n");
    text = text.replace(/<\/?p\s*>/gi, "\n\n");
    // collapse any extra blank lines those replacements introduced
    text = text.replace(/\n{3,}/g, "\n\n").trim();

    let escaped = esc(text);

    // --- block-level passes (work on escaped text, line-by-line) ---
    const lines = escaped.split("\n");
    const blocks = [];
    let i2 = 0;
    while (i2 < lines.length) {
      const ln = lines[i2];
      // pipe table: header row + separator (| --- | --- |) + body rows
      if (/^\s*\|.*\|\s*$/.test(ln) && i2 + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(lines[i2 + 1])) {
        const headerCells = ln.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        const sepCells = lines[i2 + 1].trim().replace(/^\||\|$/g, "").split("|");
        const aligns = sepCells.map(c => {
          const t = c.trim();
          if (t.startsWith(":") && t.endsWith(":")) return "center";
          if (t.endsWith(":")) return "right";
          return "left";
        });
        const rows = [];
        let j = i2 + 2;
        while (j < lines.length && /^\s*\|.*\|\s*$/.test(lines[j])) {
          rows.push(lines[j].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim()));
          j++;
        }
        const inline = (s) => s
          .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
          .replace(/\*([^*]+)\*/g, "<em>$1</em>")
          .replace(/`([^`]+)`/g, "<code>$1</code>");
        const thead = "<thead><tr>" + headerCells.map((c, k) => `<th style="text-align:${aligns[k] || "left"}">${inline(c)}</th>`).join("") + "</tr></thead>";
        const tbody = "<tbody>" + rows.map(r => "<tr>" + r.map((c, k) => `<td style="text-align:${aligns[k] || "left"}">${inline(c)}</td>`).join("") + "</tr>").join("") + "</tbody>";
        blocks.push(`<div class="md-table-wrapper"><table class="md-table">${thead}${tbody}</table></div>`);
        i2 = j;
        continue;
      }
      // ATX headings
      const h = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(ln);
      if (h) {
        const lvl = h[1].length;
        blocks.push(`<h${lvl} class="md-h${lvl}">${h[2]}</h${lvl}>`);
        i2++;
        continue;
      }
      // horizontal rule
      if (/^\s*(---|\*\*\*|___)\s*$/.test(ln)) {
        blocks.push(`<hr class="md-hr">`);
        i2++;
        continue;
      }
      // unordered list
      if (/^\s*[-*]\s+/.test(ln)) {
        const items = [];
        while (i2 < lines.length && /^\s*[-*]\s+/.test(lines[i2])) {
          items.push(lines[i2].replace(/^\s*[-*]\s+/, ""));
          i2++;
        }
        blocks.push("<ul class=\"md-list\">" + items.map(t => `<li>${t}</li>`).join("") + "</ul>");
        continue;
      }
      // ordered list
      if (/^\s*\d+\.\s+/.test(ln)) {
        const items = [];
        while (i2 < lines.length && /^\s*\d+\.\s+/.test(lines[i2])) {
          items.push(lines[i2].replace(/^\s*\d+\.\s+/, ""));
          i2++;
        }
        blocks.push("<ol class=\"md-list\">" + items.map(t => `<li>${t}</li>`).join("") + "</ol>");
        continue;
      }
      // accumulate paragraph lines until blank
      const para = [];
      while (i2 < lines.length && lines[i2].trim() !== "" &&
             !/^\s*\|.*\|\s*$/.test(lines[i2]) &&
             !/^#{1,6}\s+/.test(lines[i2]) &&
             !/^\s*(---|\*\*\*|___)\s*$/.test(lines[i2]) &&
             !/^\s*[-*]\s+/.test(lines[i2]) &&
             !/^\s*\d+\.\s+/.test(lines[i2])) {
        para.push(lines[i2]);
        i2++;
      }
      if (para.length) {
        blocks.push("<p>" + para.join("<br>") + "</p>");
      } else {
        i2++;
      }
    }

    let out = blocks.join("")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, '<img src="$2" alt="$1">');

    // Autolink URLs into clickable chips with hover preview. Runs BEFORE
    // fence restoration so URLs that the model embedded inside ``` blocks
    // stay as plain text. Inline `<code>` spans are left alone too — we
    // skip any URL that's inside an existing tag attribute or `<a>` /
    // `<code>` element.
    out = autolinkChatHtml(out);

    out = out.replace(/\x00F(\d+)\x00/g, (_, i) => {
      const { infoStr, code } = fences[+i];
      const langLabel = (infoStr || "").trim().split(/\s+/)[0] || "";
      const displayLang = langLabel ? langLabel.toLowerCase() : "text";
      const highlighted = highlightCode(code, displayLang);
      const lined = wrapCodeLines(highlighted);
      // Dynamic single-line detection for super compact visual density
      const isSingleLine = code.trim().split('\n').length <= 1;
      const singleClass = isSingleLine ? " single-line" : "";
      
      // Parse path=<filename> from infoStr
      const pathMatch = (infoStr || "").match(/path=([^\s]+)/i);
      const filename = pathMatch ? pathMatch[1] : "";
      
      // Preview button only useful for HTML/SVG/XML — surface it conditionally.
      const previewBtn = /^(html|xml|svg)$/.test(displayLang)
        ? `<button type="button" class="cc-act cc-preview" title="Open this block in the preview pane"><i class="ph ph-monitor-play"></i><span>Preview</span></button>`
        : "";
      
      if (filename) {
        return `
          <div class="code-card-tabs-container" data-filename="${esc(filename)}">
            <div class="code-card-tabs-header">
              <div class="code-card-tab">
                <span class="purple-dot"></span>
                <span>${esc(filename)}</span>
              </div>
              <div class="code-card-tab-actions">
                <button type="button" class="cc-act cc-open-file" title="Open in workspace pane" data-filename="${esc(filename)}"><i class="ph ph-folder-open"></i><span>Open file</span></button>
                <button type="button" class="cc-act cc-copy" title="Copy"><i class="ph ph-copy"></i><span>Copy</span></button>
                ${previewBtn}
              </div>
            </div>
            <pre class="code-card has-header${singleClass}" data-lang="${esc(displayLang)}"><code class="code-card-body">${lined}</code></pre>
          </div>
        `.trim();
      }
      
      // Card layout: floating action cluster (top-right), gutter+body row.
      // Buttons live inline so the card has no separate header bar — that
      // was the source of the nested-card feel.
      return `<pre class="code-card${singleClass}" data-lang="${esc(displayLang)}"><div class="code-card-actions"><button type="button" class="cc-act cc-copy" title="Copy"><i class="ph ph-copy"></i></button>${previewBtn}</div><code class="code-card-body">${lined}</code></pre>`;
    });

    // restore short-term memory blocks as a small icon + italic body
    const STM_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="14 3 14 9 20 9"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>';
    out = out.replace(/\x00S(\d+)\x00/g, (_, i) => {
      const body = stmBlocks[+i] || "";
      const safe = esc(body).replace(/\n/g, "<br>");
      return `<span class="stm-block" title="model's short-term memory from an earlier turn"><span class="stm-tag">${STM_SVG}<span class="stm-label">short-term memory</span></span><em class="stm-body">${safe}</em></span>`;
    });
    return out;
  }

  // ---------- chat link autolinker + hover preview ----------
  // Trailing punctuation we strip from auto-detected URLs so `Visit https://
  // example.com.` doesn't lasso the period into the link. Only strip if there
  // are no balanced wrappers — `https://en.wikipedia.org/wiki/Foo_(bar)` keeps
  // its closing paren when the opener is also part of the URL.
  const URL_TRAIL_PUNCT = /[)\]\}>"',.;:!?]+$/;
  function trimTrailingPunct(url) {
    let u = url;
    while (URL_TRAIL_PUNCT.test(u)) {
      const last = u.slice(-1);
      // keep one paren if it has a matching opener inside the URL
      if (last === ")" && (u.match(/\(/g) || []).length > (u.match(/\)/g) || []).length - 0) break;
      if (last === "]" && (u.match(/\[/g) || []).length > (u.match(/\]/g) || []).length - 0) break;
      u = u.slice(0, -1);
      if (!u) break;
    }
    return u;
  }
  function buildLinkAnchor(rawUrl, label) {
    const url = trimTrailingPunct(rawUrl);
    let host = "";
    try { host = new URL(url).hostname.replace(/^www\./, ""); } catch {}
    const fav = host ? `https://icons.duckduckgo.com/ip3/${host}.ico` : "";
    const text = label != null ? label : (host || url);
    const safeUrl = url.replace(/"/g, "&quot;");
    const safeHost = (host || "").replace(/"/g, "&quot;");
    const safeText = String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const favHtml = fav
      ? `<img class="chat-link-fav" src="${fav.replace(/"/g, "&quot;")}" alt="" loading="lazy" onerror="this.style.display='none'">`
      : "";
    // data-link-url drives the hover-preview lazy fetch. We DO NOT inline the
    // OG title/description in the HTML — that would balloon the bubble; the
    // popover fetches on first hover and caches both server-side and JS-side.
    return `<a class="chat-link" href="${safeUrl}" target="_blank" rel="noopener noreferrer" data-link-url="${safeUrl}" data-link-host="${safeHost}">${favHtml}<span class="chat-link-text">${safeText}</span></a>`;
  }
  function autolinkChatHtml(html) {
    if (!html) return html;
    // Step 1: markdown links [text](url) — only when the url looks http/https
    // or a bare protocol-less www.* domain (we'll prefix https://).
    html = html.replace(
      /\[([^\]\n]+)\]\(((?:https?:\/\/|www\.)[^\s)]+)\)/g,
      (_m, label, url) => {
        if (url.startsWith("www.")) url = "https://" + url;
        return buildLinkAnchor(url, label);
      }
    );
    // Step 2: bare URLs — split on existing tags so we never touch URL chars
    // sitting inside attribute values, <code>, <pre>, or already-built <a>.
    // The HTML coming in here is *escaped* user text mixed with our own
    // structural tags, so we can use a tag-tokenizer split safely.
    const parts = html.split(/(<[^>]+>)/g);
    const inSkip = []; // stack of tag names we shouldn't autolink inside
    const SKIP_TAGS = new Set(["a", "code", "pre", "kbd", "samp", "script", "style"]);
    for (let i = 0; i < parts.length; i++) {
      const seg = parts[i];
      if (!seg) continue;
      if (seg.startsWith("<")) {
        const m = /^<\s*(\/?)\s*([a-zA-Z][\w-]*)/.exec(seg);
        if (m) {
          const closing = m[1] === "/";
          const tag = m[2].toLowerCase();
          if (SKIP_TAGS.has(tag)) {
            if (closing) {
              const idx = inSkip.lastIndexOf(tag);
              if (idx >= 0) inSkip.splice(idx, 1);
            } else if (!/\/\s*>$/.test(seg)) {
              // not self-closing — push onto skip stack
              inSkip.push(tag);
            }
          }
        }
        continue;
      }
      if (inSkip.length) continue;
      // bare URL pass: http(s)://… and www.x.y…
      parts[i] = seg.replace(
        /\b((?:https?:\/\/|www\.)[^\s<>()\[\]"']+)/g,
        (_m, raw) => {
          let url = raw;
          if (url.startsWith("www.")) url = "https://" + url;
          return buildLinkAnchor(url, raw);
        }
      );
    }
    return parts.join("");
  }

  // ---- hover popover -----------------------------------------------------
  // Single shared popover element, lazy-created. Lives at body level so it
  // can escape any overflow:hidden bubble parent and float over the message.
  const _linkPreviewCache = new Map(); // url -> {title, description, image, host} or {error}
  const _linkPreviewInflight = new Map(); // url -> Promise
  let _linkPopoverEl = null;
  let _linkPopoverHideTimer = null;
  let _linkPopoverActiveAnchor = null;
  function ensureLinkPopover() {
    if (_linkPopoverEl) return _linkPopoverEl;
    const el = document.createElement("div");
    el.className = "chat-link-popover";
    el.setAttribute("hidden", "");
    el.innerHTML = `
      <div class="chat-link-popover-inner">
        <div class="chat-link-popover-img" hidden></div>
        <div class="chat-link-popover-body">
          <div class="chat-link-popover-site"><img class="chat-link-popover-fav" alt=""><span class="chat-link-popover-host"></span></div>
          <div class="chat-link-popover-title">Loading preview…</div>
          <div class="chat-link-popover-desc"></div>
          <div class="chat-link-popover-url"></div>
        </div>
      </div>`;
    // Keep the popover open while the cursor is over IT (so the user can read
    // long descriptions without losing the card to a flicker).
    el.addEventListener("mouseenter", () => {
      if (_linkPopoverHideTimer) { clearTimeout(_linkPopoverHideTimer); _linkPopoverHideTimer = null; }
    });
    el.addEventListener("mouseleave", scheduleHideLinkPopover);
    document.body.appendChild(el);
    _linkPopoverEl = el;
    return el;
  }
  function fetchLinkPreview(url) {
    if (_linkPreviewCache.has(url)) return Promise.resolve(_linkPreviewCache.get(url));
    if (_linkPreviewInflight.has(url)) return _linkPreviewInflight.get(url);
    const p = fetch(`/api/link_preview?url=${encodeURIComponent(url)}`)
      .then(r => r.json())
      .then(j => { _linkPreviewCache.set(url, j); _linkPreviewInflight.delete(url); return j; })
      .catch(e => { const j = { error: String(e) }; _linkPreviewCache.set(url, j); _linkPreviewInflight.delete(url); return j; });
    _linkPreviewInflight.set(url, p);
    return p;
  }
  function positionLinkPopover(anchor) {
    if (!_linkPopoverEl) return;
    const r = anchor.getBoundingClientRect();
    // Measure the popover itself so we can flip if it would clip the viewport.
    const pw = _linkPopoverEl.offsetWidth || 320;
    const ph = _linkPopoverEl.offsetHeight || 120;
    let top = r.bottom + 8;
    if (top + ph > window.innerHeight - 8) {
      top = Math.max(8, r.top - ph - 8); // place above
    }
    let left = r.left;
    if (left + pw > window.innerWidth - 8) left = window.innerWidth - pw - 8;
    if (left < 8) left = 8;
    _linkPopoverEl.style.top = top + "px";
    _linkPopoverEl.style.left = left + "px";
  }
  function renderLinkPopover(data) {
    if (!_linkPopoverEl) return;
    const titleEl = _linkPopoverEl.querySelector(".chat-link-popover-title");
    const descEl = _linkPopoverEl.querySelector(".chat-link-popover-desc");
    const urlEl = _linkPopoverEl.querySelector(".chat-link-popover-url");
    const hostEl = _linkPopoverEl.querySelector(".chat-link-popover-host");
    const favEl = _linkPopoverEl.querySelector(".chat-link-popover-fav");
    const imgWrap = _linkPopoverEl.querySelector(".chat-link-popover-img");
    if (data.error) {
      titleEl.textContent = data.url || "(link)";
      descEl.textContent = "Couldn't load preview: " + (data.error || "unknown error");
      urlEl.textContent = "";
      hostEl.textContent = "";
      favEl.removeAttribute("src");
      imgWrap.setAttribute("hidden", "");
      imgWrap.style.backgroundImage = "";
      return;
    }
    const host = data.host || "";
    titleEl.textContent = data.title || data.url || "(no title)";
    descEl.textContent = data.description || "";
    descEl.style.display = data.description ? "" : "none";
    urlEl.textContent = data.url || "";
    hostEl.textContent = data.site_name || host || "";
    if (host) favEl.src = `https://icons.duckduckgo.com/ip3/${host}.ico`;
    else favEl.removeAttribute("src");
    if (data.image) {
      imgWrap.style.backgroundImage = `url("${data.image.replace(/"/g, '\\"')}")`;
      imgWrap.removeAttribute("hidden");
    } else {
      imgWrap.setAttribute("hidden", "");
      imgWrap.style.backgroundImage = "";
    }
  }
  function showLinkPopover(anchor) {
    const url = anchor.getAttribute("data-link-url");
    if (!url) return;
    const el = ensureLinkPopover();
    if (_linkPopoverHideTimer) { clearTimeout(_linkPopoverHideTimer); _linkPopoverHideTimer = null; }
    _linkPopoverActiveAnchor = anchor;
    // Initial render: optimistic skeleton so the card pops instantly.
    renderLinkPopover({ url, host: anchor.getAttribute("data-link-host") || "", title: "Loading preview…", description: "", image: "" });
    el.removeAttribute("hidden");
    requestAnimationFrame(() => {
      el.classList.add("visible");
      positionLinkPopover(anchor);
    });
    fetchLinkPreview(url).then(data => {
      // Only render if the user hasn't moved on to a different anchor.
      if (_linkPopoverActiveAnchor !== anchor) return;
      renderLinkPopover(data);
      positionLinkPopover(anchor);
    });
  }
  function scheduleHideLinkPopover() {
    if (_linkPopoverHideTimer) clearTimeout(_linkPopoverHideTimer);
    _linkPopoverHideTimer = setTimeout(() => {
      if (_linkPopoverEl) {
        _linkPopoverEl.classList.remove("visible");
        _linkPopoverEl.setAttribute("hidden", "");
      }
      _linkPopoverActiveAnchor = null;
      _linkPopoverHideTimer = null;
    }, 220);
  }
  // Global delegated hover — works for chat-links inserted at any time.
  document.addEventListener("mouseover", (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest("a.chat-link");
    if (!a) return;
    showLinkPopover(a);
  });
  document.addEventListener("mouseout", (ev) => {
    const a = ev.target && ev.target.closest && ev.target.closest("a.chat-link");
    if (!a) return;
    // ignore moves into descendants of the same anchor
    if (a.contains(ev.relatedTarget)) return;
    // ignore moves into the popover itself (its own mouseleave handles hiding)
    if (_linkPopoverEl && _linkPopoverEl.contains(ev.relatedTarget)) return;
    scheduleHideLinkPopover();
  });

  // very small HTML syntax highlighter for code view
  function highlightHTML(src) {
    const s = esc(src);
    return s
      .replace(/(&lt;!--[\s\S]*?--&gt;)/g, '<span class="tok-comment">$1</span>')
      .replace(/(&lt;\/?)([a-zA-Z][\w-]*)/g, '$1<span class="tok-tag">$2</span>')
      .replace(/(\s)([a-zA-Z][\w-]*)=(&quot;[^&]*?&quot;)/g, '$1<span class="tok-attr">$2</span>=<span class="tok-str">$3</span>');
  }

  // ---------- bootstrap ----------
  async function boot() {
    // on-device hint
    if (isMobile()) document.body.classList.add("is-mobile");

    await Promise.all([
      loadSettings(),
      loadWorkspace(),
      loadChats(),
      loadModels(),
    ]);

    // pick or create current chat
    if (state.chats.order.length) {
      selectChat(state.chats.order[0]);
    } else {
      await newChat();
    }

    applyTheme(state.settings.theme || "light");
    renderStatus();
    renderModelPill();
    renderChatList();
    renderWorkspace();
    reflectIdeToggles();

    wireEvents();
    subscribeSSE();
    initCostWidget();

    // Background self-correct: re-tune for the currently loaded model on every
    // boot so saved settings from old/buggy autotune runs heal themselves.
    // Same "grow only" rule — never shrinks ctx behind the user's back. Silent
    // on success; logs to console on failure so we don't spam toasts at boot.
    autoRetuneOnBoot().catch(e => console.warn("boot auto-retune skipped:", e));
  }

  async function autoRetuneOnBoot() {
    const s = state.settings || {};
    const modelPath = s.model_path || "";
    const tier = Number(s.vram_tier_gb || 0) || 0;
    if (!modelPath || !tier) return;  // nothing to tune for
    let r;
    try {
      r = await api("/api/llama/auto-tune", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ model_path: modelPath, vram_gb: tier }),
      });
    } catch (e) {
      throw e;  // bubbled to caller's .catch
    }
    const sug = r?.suggested || {};
    const cur = state.settings;
    // Compare what would change. Only push an update if at least one tuned
    // value differs AND (for ctx) it's strictly larger than current.
    const update = {};
    const sugCtx = Number(sug.num_ctx || 0) || 0;
    const curCtx = Number(cur.num_ctx || 0) || 0;
    if (sugCtx > curCtx) update.num_ctx = sugCtx;  // grow only
    // For non-ctx flags, defer to the suggester since these are speed knobs
    // and the user explicitly asked for autotune behavior.
    const speedKeys = ["num_gpu", "num_batch", "n_cpu_moe", "n_ubatch", "kv_cache_type", "spec_strategy"];
    for (const k of speedKeys) {
      if (sug[k] != null && String(sug[k]) !== String(cur[k] ?? "")) {
        update[k] = sug[k];
      }
    }
    // Booleans: same idea but explicit
    for (const k of ["flash_attn"]) {
      if (sug[k] != null && !!sug[k] !== !!cur[k]) {
        update[k] = !!sug[k];
      }
    }
    if (!Object.keys(update).length) return;  // already tuned, nothing to do
    await saveSettings(update);
    // If llama-server is already running, restart it so the tuned flags take
    // effect immediately — otherwise the user would still see stale ctx until
    // they manually reload. If it's not running, the next /api/models/load
    // will pick the new values up automatically.
    if (state.llamaRunning && modelPath) {
      try {
        await api("/api/models/load", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path: modelPath }),
        });
        await refreshModels();
      } catch (e) { console.warn("boot reload failed:", e); }
    }
    if (sugCtx > curCtx) {
      toast(`auto-tune grew context: ${curCtx.toLocaleString()} → ${sugCtx.toLocaleString()}`, "ok", 4000);
    }
  }

  function reflectIdeToggles() {
    const tw = $("#toggle-tailwind");
    if (tw) tw.classList.toggle("on", !!state.settings.use_tailwind_cdn);
    const mf = $("#toggle-multifile");
    if (mf) mf.classList.toggle("on", !!state.settings.ide_multifile);
  }

  // ---------- data loading ----------
  async function loadSettings() {
    state.settings = await api("/api/settings");
    if (state.settings && state.settings.theme) {
      localStorage.setItem("accuretta:theme", state.settings.theme);
    }
  }
  async function saveSettings(update) {
    const prevModel = state.settings.model;
    state.settings = await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(update),
    });
    // model changed mid-stream → abort so next send uses fresh model cleanly
    if (update.model && update.model !== prevModel && state.streaming) {
      stopStreaming();
    }
    renderStatus();
    renderModelPill();
  }
  async function loadWorkspace() {
    state.workspace = await api("/api/workspace");
  }
  async function loadChats() {
    state.chats = await api("/api/chats");
  }
  async function loadModels() {
    try {
      const r = await api("/api/models");
      state.modelsDir = r.models_dir || "";
      state.loadedModel = r.loaded_model || "";
      state.llamaRunning = !!r.llama_running;
      // Native vision: model was booted with --mmproj. Drives the model-pill
      // badge and the Settings hint so the user knows whether attached images
      // get seen by the chat model directly or routed through the OCR side.
      state.visionCapable = !!r.vision_capable;
      state.loadedMmproj = r.loaded_mmproj || "";
      state.modelsList = Array.isArray(r.models) ? r.models : [];
      state.models = state.modelsList.map(m => m.name).filter(Boolean);
      if (r.error) state.modelsError = r.error;
      else if (!state.modelsDir) state.modelsError = "no models folder set — pick one above.";
      else if (!state.models.length) state.modelsError = "no .gguf files found in " + state.modelsDir;
      else state.modelsError = "";
    } catch (e) {
      state.models = [];
      state.modelsList = [];
      state.modelsError = "bridge unreachable: " + (e.message || e);
    }
  }

  // ---------- chat ----------
  async function newChat() {
    // Tag the session with where it was born so the chat list can show a phone
    // icon for mobile-started sessions. The bridge persists this on the chat
    // record; it never changes after creation.
    const origin = isMobile() ? "mobile" : "desktop";
    const c = await api("/api/chats", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "new session", origin }),
    });
    await loadChats();
    selectChat(c.id);
    const ta = $("#composer-input");
    ta.value = "";
    autoResize(ta);
  }

  function selectChat(id) {
    state.chatId = id;
    const chat = state.chats.chats[id];
    // Only render visible bubbles. The chat record now also stores
    // intermediate assistant turns (with tool_calls) and tool-result messages
    // so the bridge can replay the full agentic working memory on the next
    // turn — but those aren't bubbles, the renderer skips them.
    state.messages = chat
      ? (chat.messages || []).filter(
          m => (m.role === "user" || m.role === "assistant") && !m._internal
        )
      : [];
    $("#chat-title").textContent = chat ? chat.title : "new session";
    // restore the last-used mode for this chat so the toolbar feels sticky.
    // on mobile we drop IDE — there's no preview pane to render into — and
    // fall back to agent so the user lands in a sensible default.
    if (chat && chat.last_mode && ["auto", "ide", "agent"].includes(chat.last_mode)) {
      state.mode = chat.last_mode;
      if (isMobile() && state.mode === "ide") state.mode = "agent";
      $$('[data-mode]').forEach(x => x.classList.toggle("on", x.dataset.mode === state.mode));
    } else if (isMobile() && state.mode === "ide") {
      state.mode = "agent";
      $$('[data-mode]').forEach(x => x.classList.toggle("on", x.dataset.mode === state.mode));
    }
    let chatPromptTok = 0;
    let chatOutTok = 0;
    if (chat && chat.messages) {
      for (const m of chat.messages) {
        if (Number.isFinite(m.prompt_tokens)) {
          chatPromptTok += m.prompt_tokens;
        }
        if (Number.isFinite(m.tokens)) {
          chatOutTok += m.tokens;
        }
      }
    }
    state.tokTotal = chatOutTok;
    state.tokPromptTotal = chatPromptTok;
    // Seed the context gauge from the last turn's real prompt-token count so a
    // freshly-loaded chat shows true context fill immediately, instead of the
    // crude char estimate until the next poll/turn. Walk newest-first for the
    // most recent message that carries a real count.
    state._lastMsgPromptTokens = 0;
    state._ctxSource = "";
    if (chat && chat.messages) {
      for (let i = chat.messages.length - 1; i >= 0; i--) {
        const pt = chat.messages[i].prompt_tokens;
        if (Number.isFinite(pt) && pt > 0) { state._lastMsgPromptTokens = pt; state._ctxSource = "live"; break; }
      }
    }
    state.totalGenDuration = 0;
    state._streamOutEstimate = 0;
    state._streamPromptEstimate = 0;
    renderTokTotal();
    renderCostWidget();
    refreshSessionDesktopState();
    renderMessages();
    state._versionsExpanded = false;
    loadVersions();
    renderChatList();
    // restore composer draft
    const ta = $("#composer-input");
    ta.value = localStorage.getItem("accuretta:draft:" + id) || "";
    autoResize(ta);
    if (isMobile()) {
      state.mobileTab = "chat";
      applyMobileTab();
    }
    // start context-stats polling
    clearInterval(state._ctxPoll);
    state._ctxPoll = setInterval(async () => {
      try {
        const cid = state.chatId;
        if (!cid) return;
        const r = await api("/api/ctx-stats?chat_id=" + encodeURIComponent(cid));
        // Guard against a slow response landing after the user switched chats.
        if (r && typeof r.prompt_tokens === "number" && state.chatId === cid) {
          state._lastMsgPromptTokens = r.prompt_tokens;
          state._ctxSource = r.source || "";
          if (Number.isFinite(r.capacity) && r.capacity > 0) state._ctxCapacity = r.capacity;
          renderCtxGauge();
        }
      } catch (_) {}
    }, 2000);
  }

  // ---------- session-scoped desktop kill switch ----------
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
    // Class name matches the CSS rule `#btn-session-desktop.is-disabled`
    // in app.css (red border / red-tinted background while OFF). The
    // older `off` class string had no matching rule, so toggling it
    // dropped the button into a styleless state that read as "gone".
    btn.classList.toggle("is-disabled", state.sessionDesktopDisabled);
    btn.title = state.sessionDesktopDisabled
      ? "Desktop automation OFF for this chat — click to re-enable"
      : "Desktop automation ON for this chat — click to disable";
    btn.innerHTML = state.sessionDesktopDisabled
      ? '<i class="ph ph-desktop"></i>'
      : '<i class="ph ph-desktop"></i>';
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

  async function deleteChat(id) {
    if (!confirm("Delete this session and its versions?")) return;
    await fetch(`/api/chats/${id}`, { method: "DELETE" });
    localStorage.removeItem("accuretta:draft:" + id);
    await loadChats();
    if (state.chatId === id) {
      if (state.chats.order.length) selectChat(state.chats.order[0]);
      else await newChat();
    } else {
      renderChatList();
    }
  }

  // ---------- command palette (⌘K) ----------
  function openPalette() {
    state.palette.open = true;
    state.palette.idx = 0;
    $("#palette-scrim").classList.remove("hidden");
    $("#palette").classList.remove("hidden");
    const inp = $("#palette-input");
    inp.value = "";
    refreshPaletteList("");
    setTimeout(() => inp.focus(), 0);
  }
  function closePalette() {
    state.palette.open = false;
    $("#palette-scrim").classList.add("hidden");
    $("#palette").classList.add("hidden");
  }
  function _fuzzyScore(query, text) {
    if (!query) return 0;
    const q = query.toLowerCase();
    const t = (text || "").toLowerCase();
    if (t.includes(q)) return 100 - t.indexOf(q);
    // cheap subsequence scoring: every char of q must appear in order
    let i = 0, score = 0, last = -1;
    for (let j = 0; j < t.length && i < q.length; j++) {
      if (t[j] === q[i]) { score += 5 - Math.min(4, j - last - 1); last = j; i++; }
    }
    return i === q.length ? score : -1;
  }
  function refreshPaletteList(query) {
    const list = $("#palette-list");
    list.innerHTML = "";
    const items = [];
    // built-in commands always appear first
    const commands = [
      { kind: "cmd", icon: "ph-plus", label: "New session", action: () => { closePalette(); newChat(); } },
      { kind: "cmd", icon: "ph-gear-six", label: "Open Settings", action: () => { closePalette(); openSettings(); } },
      { kind: "cmd", icon: "ph-brain", label: "Open Long-term memory", action: () => { closePalette(); openSettings(); setTimeout(() => $("#btn-mem-refresh")?.scrollIntoView({ behavior: "smooth" }), 80); } },
      { kind: "cmd", icon: "ph-arrow-counter-clockwise", label: "Regenerate last reply", action: () => { closePalette(); regenerateLast(); } },
      { kind: "cmd", icon: "ph-moon", label: "Cycle theme (dark / dim / aurora / nebula / soft / light)", action: async () => { closePalette(); const next = nextTheme(state.settings.theme || "light"); await saveSettings({ theme: next }); applyTheme(next); } },
      { kind: "cmd", icon: "ph-browser", label: "Toggle preview pane", action: () => { closePalette(); app.classList.toggle("preview-collapsed"); } },
      { kind: "cmd", icon: "ph-camera", label: "Screenshot preview", action: () => { closePalette(); screenshotPreview(); } },
      { kind: "cmd", icon: "ph-package", label: "Export project", action: () => { closePalette(); exportProjectZip(); } },
      { kind: "cmd", icon: "ph-floppy-disk", label: "Save snapshot", action: () => { closePalette(); saveSnapshot(); } },
    ];
    for (const c of commands) {
      const s = query ? _fuzzyScore(query, c.label) : 0;
      if (query && s < 0) continue;
      items.push({ ...c, score: s + 50 });
    }
    // then sessions
    for (const id of state.chats.order) {
      const c = state.chats.chats[id];
      if (!c) continue;
      const label = c.title || "(untitled)";
      const s = query ? _fuzzyScore(query, label) : 0;
      if (query && s < 0) continue;
      items.push({
        kind: "session",
        icon: "ph-chat-circle",
        label,
        sub: id === state.chatId ? "current" : relTime(c.updated || c.created),
        action: () => { closePalette(); selectChat(id); },
        score: s,
      });
    }
    items.sort((a, b) => b.score - a.score);
    state.palette.items = items;
    state.palette.idx = 0;
    items.forEach((it, i) => {
      const el = document.createElement("div");
      el.className = "palette-item" + (i === 0 ? " sel" : "");
      el.innerHTML = `
        <i class="ph ${it.icon}"></i>
        <div class="pi-main">
          <div class="pi-label">${esc(it.label)}</div>
          ${it.sub ? `<div class="pi-sub">${esc(it.sub)}</div>` : ""}
        </div>
        <span class="pi-kind">${esc(it.kind)}</span>`;
      el.addEventListener("click", it.action);
      list.appendChild(el);
    });
    if (!items.length) {
      list.innerHTML = `<div class="palette-empty">no matches.</div>`;
    }
  }
  function paletteMove(delta) {
    const items = state.palette.items;
    if (!items.length) return;
    state.palette.idx = (state.palette.idx + delta + items.length) % items.length;
    const rows = document.querySelectorAll("#palette-list .palette-item");
    rows.forEach((r, i) => r.classList.toggle("sel", i === state.palette.idx));
    const r = rows[state.palette.idx];
    if (r) r.scrollIntoView({ block: "nearest" });
  }
  function paletteCommit() {
    const it = state.palette.items[state.palette.idx];
    if (it) it.action();
  }

  function renderChatList() {
    const wrap = $("#chatlist");
    wrap.innerHTML = "";
    for (const id of state.chats.order) {
      const c = state.chats.chats[id];
      if (!c) continue;
      const row = document.createElement("div");
      const isActive = id === state.chatId;
      row.className = "chatrow" + (isActive ? " active" : "");
      // Mobile-born sessions get a phone glyph; everything else keeps the
      // chat-circle. The active row also shows the colored dot bullet via
      // the `.chatrow.active::before` rule in app.css — the icon is the
      // SECONDARY signal, the dot is the primary.
      const iconClass = c.origin === "mobile" ? "ph ph-device-mobile" : "ph ph-chat-circle";
      row.innerHTML = `
        <i class="${iconClass}"></i>
        <span class="t">${esc(c.title)}</span>
        <span class="d">${relTime(c.updated)}</span>
        <button class="del" title="Delete"><i class="ph ph-trash"></i></button>`;
      row.addEventListener("click", (e) => {
        if (e.target.closest(".del")) return;
        selectChat(id);
      });
      row.querySelector(".del").addEventListener("click", (e) => {
        e.stopPropagation();
        deleteChat(id);
      });
      wrap.appendChild(row);
    }
  }

  function renderMessages() {
    const inner = $("#chat-inner");
    inner.innerHTML = "";
    if (!state.messages.length) {
      inner.innerHTML = `
        <div class="welcome-screen">
          <div class="welcome-blobs"></div>
          <div class="welcome-content">
            <div class="welcome-logo-wrap">
              <div class="welcome-logo welcome-logo-accent" aria-hidden="true"></div>
            </div>
            <h1 class="welcome-title">accuretta</h1>
            <p class="welcome-subtitle">Your model, your machine. What are we building today?</p>
            <div class="welcome-suggestions">
              <button class="welcome-suggest-btn" data-prompt="Design a landing page for my product using HTML, CSS and JS.">
                <div class="welcome-suggest-icon-wrap">
                  <i class="ph ph-layout"></i>
                </div>
                <span class="welcome-suggest-text">
                  <span class="welcome-suggest-label">Design a landing page</span>
                  <span class="welcome-suggest-sub">HTML, CSS + JS, live preview</span>
                </span>
                <i class="ph ph-arrow-up-right welcome-suggest-go"></i>
              </button>
              <button class="welcome-suggest-btn" data-prompt="Create a Python backend script using FastAPI that serves a simple database.">
                <div class="welcome-suggest-icon-wrap">
                  <i class="ph ph-database"></i>
                </div>
                <span class="welcome-suggest-text">
                  <span class="welcome-suggest-label">Create a Python backend</span>
                  <span class="welcome-suggest-sub">FastAPI over a simple DB</span>
                </span>
                <i class="ph ph-arrow-up-right welcome-suggest-go"></i>
              </button>
              <button class="welcome-suggest-btn" data-prompt="Help me debug a memory leak in my application.">
                <div class="welcome-suggest-icon-wrap">
                  <i class="ph ph-bug"></i>
                </div>
                <span class="welcome-suggest-text">
                  <span class="welcome-suggest-label">Debug a memory leak</span>
                  <span class="welcome-suggest-sub">paste code, get a diagnosis</span>
                </span>
                <i class="ph ph-arrow-up-right welcome-suggest-go"></i>
              </button>
            </div>
          </div>
        </div>`;
      initWelcomeScreen();
      scrollToBottom(true);
      return;
    }
    for (const m of state.messages) {
      if (m.invisible) continue;
      inner.appendChild(renderBubble(m));
    }
    renderRegenerateChip();
    scrollToBottom(true);
  }

  function initWelcomeScreen() {
    // 1. Suggestion buttons trigger instant invisible submission with an ether animation
    document.querySelectorAll(".welcome-suggest-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        const prompt = btn.dataset.prompt;

        // Apply stylish "sent to the ether" disintegrating transition
        const screen = document.querySelector(".welcome-screen");
        if (screen) {
          screen.classList.add("welcome-disintegrating");
          document.querySelectorAll(".welcome-suggest-btn").forEach(b => {
            if (b === btn) {
              b.classList.add("clicked-ether");
            } else {
              b.classList.add("fade-out-ether");
            }
          });
        }

        setTimeout(() => {
          send({ prompt, invisible: true });
        }, 650);
      });
    });

    // Background: soft vertical colour bars of varying height (no canvas).
    buildWelcomeBars();
  }

  // Generates the welcome-screen backdrop: a warm→cool set of blurred vertical
  // bars, each fading into the background at a different point (some at the
  // top, some at the bottom, some spanning) so they read as varying lengths.
  // Colours reference the --bar-* palette on .welcome-blobs, so they follow
  // the active theme.
  function buildWelcomeBars() {
    const wrap = document.querySelector(".welcome-blobs");
    if (!wrap) return;
    // Deterministic PRNG so the layout stays stable across new chats.
    let seed = 0x9e3779b9 >>> 0;
    const rnd = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
    const palette = ["--bar-warm", "--bar-rose", "--bar-mid", "--bar-violet", "--bar-blue", "--bar-sky"];
    const N = 18;
    let html = "";
    for (let i = 0; i < N; i++) {
      const x = i / (N - 1);                                     // 0 → 1, left → right
      const left = (x * 106 - 3 + (rnd() - 0.5) * 3).toFixed(1);
      const width = (4 + rnd() * 5).toFixed(1);
      const c = palette[Math.min(palette.length - 1, Math.floor(x * palette.length))];
      const topFade = Math.round(rnd() * 42);                    // fade-in distance from the top
      const botFade = Math.round(rnd() * 42);                    // fade-out distance before the bottom
      const o = (0.32 + rnd() * 0.4).toFixed(2);
      const blur = (16 + rnd() * 26).toFixed(0);
      const dur = (18 + rnd() * 16).toFixed(1);
      const delay = (-rnd() * 22).toFixed(1);
      html += `<div class="welcome-bar" style="left:${left}%;width:${width}%;--o:${o};opacity:${o};`
        + `background:linear-gradient(180deg,transparent 0%,var(${c}) ${topFade}%,var(${c}) ${100 - botFade}%,transparent 100%);`
        + `filter:blur(${blur}px);animation-duration:${dur}s;animation-delay:${delay}s;"></div>`;
    }
    wrap.innerHTML = html;
  }

  function initWelcomeWebGL() {
    const canvas = document.getElementById("welcome-canvas");
    if (!canvas) return;

    let gl;
    try {
      gl = canvas.getContext("webgl") || canvas.getContext("experimental-webgl");
    } catch (e) {
      console.warn("WebGL not supported by this browser. Falling back to CSS blobs.");
      return;
    }
    if (!gl) {
      console.warn("WebGL context creation failed. Falling back to CSS blobs.");
      return;
    }

    const vsSource = `
      attribute vec2 position;
      void main() {
        gl_Position = vec4(position, 0.0, 1.0);
      }
    `;

    const fsSource = `
      #ifdef GL_FRAGMENT_PRECISION_HIGH
      precision highp float;
      #else
      precision mediump float;
      #endif
      uniform vec2 u_resolution;
      uniform float u_time;
      uniform vec2 u_mouse;
      uniform vec3 u_accent_color;
      uniform vec3 u_bg_color;

      float hash21(vec2 p) {
        p = fract(p * vec2(123.34, 345.45));
        p += dot(p, p + 34.345);
        return fract(p.x * p.y);
      }

      float vnoise(vec2 p) {
        vec2 i = floor(p);
        vec2 f = fract(p);
        vec2 u = f * f * (3.0 - 2.0 * f);
        float a = hash21(i);
        float b = hash21(i + vec2(1.0, 0.0));
        float c = hash21(i + vec2(0.0, 1.0));
        float d = hash21(i + vec2(1.0, 1.0));
        return mix(mix(a, b, u.x), mix(c, d, u.x), u.y);
      }

      float fbm(vec2 p) {
        float v = 0.0;
        float amp = 0.5;
        for (int i = 0; i < 3; i++) {
          v += amp * vnoise(p);
          p *= 2.02;
          amp *= 0.5;
        }
        return v;
      }

      void main() {
        vec2 uv = gl_FragCoord.xy / u_resolution.xy;
        float aspect = u_resolution.x / u_resolution.y;
        vec2 st = uv * 2.0 - 1.0;
        st.x *= aspect;

        // Gentle pointer parallax — dormant while the cursor rests at centre.
        vec2 m = (u_mouse / u_resolution) * 2.0 - 1.0;
        vec2 p = st * 0.8 - m * 0.06;
        float t = u_time * 0.05;

        // Companion tones derived from the theme accent — each theme drives
        // its own palette with no per-theme branching.
        vec3 shift1 = vec3(u_accent_color.z, u_accent_color.x, u_accent_color.y);
        vec3 shift2 = vec3(u_accent_color.y, u_accent_color.z, u_accent_color.x);
        vec3 base   = mix(u_bg_color, u_accent_color, 0.12);
        vec3 bright = min(u_accent_color * 1.35 + 0.05, vec3(1.0));

        // Domain-warped flow field (Inigo Quilez style) — smooth, liquid,
        // slowly folding gradients rather than discrete blobs.
        vec2 q = vec2(fbm(p + t), fbm(p + vec2(3.4, 1.7) - t));
        vec2 r = vec2(fbm(p + 1.7 * q + vec2(1.2, 6.1) + t * 1.2),
                      fbm(p + 1.7 * q + vec2(8.3, 2.4) - t * 0.9));
        float f = fbm(p + 1.6 * r);

        // Blend the palette along the flow so colours pool and drift.
        vec3 col = base;
        col = mix(col, u_accent_color, smoothstep(0.15, 0.95, f + 0.15));
        col = mix(col, shift1, clamp(q.x * 0.9, 0.0, 1.0));
        col = mix(col, shift2, clamp(r.y * 0.8, 0.0, 1.0));
        col = mix(col, bright, smoothstep(0.55, 1.05, f) * 0.6);

        // Bold toward the edges, calmer behind the centred logo + title.
        float centreClear = smoothstep(0.1, 1.1, length(st * vec2(0.7, 1.0)));
        float vign = smoothstep(2.9, 0.2, length(st));
        float amt = mix(0.4, 1.0, centreClear) * vign * 0.92;

        vec3 color = mix(u_bg_color, col, clamp(amt, 0.0, 1.0));
        gl_FragColor = vec4(color, 1.0);
      }
    `;

    function compileShader(source, type) {
      const shader = gl.createShader(type);
      gl.shaderSource(shader, source);
      gl.compileShader(shader);
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
        console.error("Shader compile error:", gl.getShaderInfoLog(shader));
        gl.deleteShader(shader);
        return null;
      }
      return shader;
    }

    const vs = compileShader(vsSource, gl.VERTEX_SHADER);
    const fs = compileShader(fsSource, gl.FRAGMENT_SHADER);
    if (!vs || !fs) return;

    const program = gl.createProgram();
    gl.attachShader(program, vs);
    gl.attachShader(program, fs);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("Program link error:", gl.getProgramInfoLog(program));
      return;
    }
    gl.useProgram(program);

    const vertices = new Float32Array([
      -1, -1,   1, -1,  -1,  1,
      -1,  1,   1, -1,   1,  1
    ]);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, vertices, gl.STATIC_DRAW);

    const posAttr = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(posAttr);
    gl.vertexAttribPointer(posAttr, 2, gl.FLOAT, false, 0, 0);

    const uResolution = gl.getUniformLocation(program, "u_resolution");
    const uTime = gl.getUniformLocation(program, "u_time");
    const uMouse = gl.getUniformLocation(program, "u_mouse");
    const uAccentColor = gl.getUniformLocation(program, "u_accent_color");
    const uBgColor = gl.getUniformLocation(program, "u_bg_color");

    let mouseX = canvas.width / 2;
    let mouseY = canvas.height / 2;
    let targetMouseX = mouseX;
    let targetMouseY = mouseY;

    function onPointerMove(e) {
      const rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      targetMouseX = e.clientX - rect.left;
      targetMouseY = rect.height - (e.clientY - rect.top);
    }
    window.addEventListener("mousemove", onPointerMove, { passive: true });

    function getThemeColors() {
      const styles = getComputedStyle(document.documentElement);
      
      function hexToRgb(hex) {
        hex = hex.trim();
        if (hex.startsWith("rgba")) {
          const m = hex.match(/\d+/g);
          return m ? [parseInt(m[0])/255, parseInt(m[1])/255, parseInt(m[2])/255] : [1, 1, 1];
        }
        if (hex.startsWith("#")) {
          if (hex.length === 4) {
            const r = parseInt(hex[1] + hex[1], 16) / 255;
            const g = parseInt(hex[2] + hex[2], 16) / 255;
            const b = parseInt(hex[3] + hex[3], 16) / 255;
            return [r, g, b];
          }
          const r = parseInt(hex.substring(1, 3), 16) / 255;
          const g = parseInt(hex.substring(3, 5), 16) / 255;
          const b = parseInt(hex.substring(5, 7), 16) / 255;
          return [r, g, b];
        }
        return [0.5, 0.5, 0.5];
      }

      const accent = styles.getPropertyValue("--accent") || "#B8A3F2";
      const bg = styles.getPropertyValue("--bg") || "#1a1a1d";

      return {
        accent: hexToRgb(accent),
        bg: hexToRgb(bg)
      };
    }

    function resize() {
      const displayWidth = canvas.clientWidth;
      const displayHeight = canvas.clientHeight;
      if (canvas.width !== displayWidth || canvas.height !== displayHeight) {
        canvas.width = displayWidth;
        canvas.height = displayHeight;
        gl.viewport(0, 0, canvas.width, canvas.height);
      }
    }

    const startTime = performance.now();

    function render(now) {
      if (!document.getElementById("welcome-canvas")) {
        window.removeEventListener("mousemove", onPointerMove);
        return;
      }

      resize();

      const elapsed = (now - startTime) / 1000;

      mouseX += (targetMouseX - mouseX) * 0.1;
      mouseY += (targetMouseY - mouseY) * 0.1;

      const colors = getThemeColors();

      gl.uniform2f(uResolution, canvas.width, canvas.height);
      gl.uniform1f(uTime, elapsed);
      gl.uniform2f(uMouse, mouseX, mouseY);
      gl.uniform3fv(uAccentColor, colors.accent);
      gl.uniform3fv(uBgColor, colors.bg);

      gl.drawArrays(gl.TRIANGLES, 0, 6);

      requestAnimationFrame(render);
    }

    requestAnimationFrame(render);
  }

  function renderBubble(m) {
    const row = document.createElement("div");
    row.className = "bubble-row " + (m.role === "user" ? "user" : "");
    const avatar = m.role === "user"
      ? `<div class="avatar user">me</div>`
      : AGENT_AVATAR_HTML;

    let visible = m.content || "";
    let thoughtChip = "";
    let cascadeChips = "";
    if (m.role === "assistant") {
      const { thinking, content } = splitThinking(visible);
      visible = content;
      if (thinking) {
        thoughtChip = `
          <div class="think-container done">
            <div class="think-header" style="cursor: pointer;">
              <i class="ph ph-caret-right think-caret"></i>
              <i class="ph ph-check-circle think-check-icon done"></i>
              <span class="think-title">Thought for a moment</span>
            </div>
            <div class="think-content hidden">${esc(thinking)}</div>
          </div>`;
      }
      const cascadeRes = splitCascade(visible);
      visible = cascadeRes.content;
      if (cascadeRes.cascade && cascadeRes.cascade.length > 0) {
        let btns = cascadeRes.cascade.map(text => 
          `<button class="cascade-chip" data-prompt="${esc(text)}"><i class="ph ph-sparkle"></i>${esc(text)}</button>`
        ).join("");
        cascadeChips = `<div class="cascade-container">${btns}</div>`;
      }
    }

    const tokTip = m.tokens ? ` title="${m.tokens.toLocaleString()} tokens"` : "";
    row.innerHTML = `
      ${avatar}
      <div class="bubble-col">
        ${thoughtChip}
        <div class="bubble ${m.role === "user" ? "user" : "agent"}">${renderMarkdown(visible)}</div>
        ${cascadeChips}
        <div class="bubble-meta"${tokTip}>${m.role === "user" ? "you" : (state.settings.model || "agent")} · ${relTime(m.t)}</div>
      </div>`;
    
    // (Cascade click listeners are now handled via event delegation on #chat-inner)
    // Single copy action under every bubble. Same .bubble-actions row used
    // for the assistant's regenerate strip — keeps placement consistent
    // (under the bubble, on whichever edge the bubble-col flexes to). The
    // last assistant bubble's actions get rebuilt by renderRegenerateChip
    // with both regen + copy, so we skip adding here for that one to avoid
    // the duplicate; non-last assistants and all user bubbles keep this row.
    const isLastAssistant = m.role === "assistant" &&
      state.messages.length > 0 &&
      state.messages[state.messages.length - 1] === m;
    if (!isLastAssistant) {
      const actions = document.createElement("div");
      actions.className = "bubble-actions";
      actions.innerHTML = `<button type="button" class="bubble-action" data-act="copy" title="Copy"><i class="ph ph-copy"></i></button>`;
      const copyBtn = actions.querySelector('[data-act="copy"]');
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(m.content || "");
          const icon = copyBtn.querySelector("i");
          copyBtn.classList.add("copied");
          icon.classList.remove("ph-copy");
          icon.classList.add("ph-check");
          setTimeout(() => {
            copyBtn.classList.remove("copied");
            icon.classList.remove("ph-check");
            icon.classList.add("ph-copy");
          }, 1200);
        } catch {
          toast("Clipboard blocked", "warn", 2000);
        }
      });
      row.querySelector(".bubble-col").appendChild(actions);
    }
    enhanceCodeBlocks(row);
    return row;
  }

  // Wire Copy / Preview action buttons on rendered code cards. For legacy
  // <pre> blocks (e.g. tool output) we still graft on a floating copy
  // button so they're not left bare. Idempotent via data-enhanced.
  function enhanceCodeBlocks(root) {
    const pres = root.querySelectorAll("pre");
    pres.forEach(pre => {
      if (pre.dataset.enhanced === "1") return;
      pre.dataset.enhanced = "1";
      pre.classList.add("code-block");

      const codeEl = pre.querySelector("code");
      // wrapCodeLines() emits each source line as a <span class="code-line">
      // and joins them with NO newline (because the spans are display:block
      // and a literal \n inside the <pre> would render as an extra blank
      // row). That makes the visual layout right but breaks copy-paste —
      // .textContent on the <code> returns every line concatenated with no
      // separator, so pasting comes out as one long line.
      // Fix: when the body uses our line spans, walk them and join with \n.
      // For legacy / tool-output <pre> blocks that don't have line spans,
      // fall back to plain textContent (which already has real newlines).
      const getText = () => {
        if (codeEl) {
          const lines = codeEl.querySelectorAll(".code-line");
          if (lines.length) {
            return Array.from(lines).map(l => l.textContent).join("\n");
          }
          return codeEl.textContent || "";
        }
        return pre.textContent || "";
      };

      // Modern code-card path — buttons emitted by renderMarkdown live inline or in tabs header.
      const container = pre.closest(".code-card-tabs-container");
      const copyAct = pre.querySelector(".cc-copy") || container?.querySelector(".cc-copy");
      const previewAct = pre.querySelector(".cc-preview") || container?.querySelector(".cc-preview");
      const openAct = container?.querySelector(".cc-open-file");

      if (openAct) {
        openAct.addEventListener("click", () => {
          const filename = openAct.dataset.filename;
          const rootFolder = (state.workspace && state.workspace.folders && state.workspace.folders[0]) || "";
          if (!rootFolder) {
            toast("No workspace folder configured", "warn", 2000);
            return;
          }
          if (filename.endsWith(".html")) {
            previewWorkspaceHtml(rootFolder, filename, filename);
          } else if (filename.endsWith(".md")) {
            previewWorkspaceMarkdown(rootFolder, filename, filename);
          } else if (filename.endsWith(".py")) {
            runPythonCheck(rootFolder, filename, filename);
          } else {
            previewWorkspaceSource(rootFolder, filename, filename);
          }
        });
      }

      if (copyAct) {
        copyAct.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(getText());
            const labelEl = copyAct.querySelector("span");
            const iconEl = copyAct.querySelector("i");
            const prevLabel = labelEl ? labelEl.textContent : "";
            if (iconEl) { iconEl.classList.remove("ph-copy"); iconEl.classList.add("ph-check"); }
            if (labelEl) labelEl.textContent = "Copied";
            copyAct.classList.add("copied");
            setTimeout(() => {
              if (iconEl) { iconEl.classList.add("ph-copy"); iconEl.classList.remove("ph-check"); }
              if (labelEl) labelEl.textContent = prevLabel || "Copy";
              copyAct.classList.remove("copied");
            }, 1200);
          } catch {
            toast("Clipboard blocked", "warn", 2000);
          }
        });
      }

      if (previewAct) {
        previewAct.addEventListener("click", () => {
          const code = getText();
          if (!code.trim()) { toast("Code block is empty", "warn", 1800); return; }
          // Push the block into the preview pipeline. Reuse state.currentHtml +
          // renderPreview() so the existing iframe/srcdoc path handles sandbox,
          // tailwind injection, and the code-view tab.
          state.currentHtml = code;
          state.currentFiles = {};
          state.view = "preview";
          $("#btn-view-preview")?.classList.add("active");
          $("#btn-view-code")?.classList.remove("active");
          if (app.classList.contains("preview-collapsed") && !isMobile()) {
            app.classList.remove("preview-collapsed");
          }
          renderPreview();
          toast("Loaded into preview pane", "info", 1500);
        });
      }

      // Legacy fallback — older <pre> blocks that didn't go through the
      // code-card emit (tool result lines, short-term memory tails, etc.) still
      // get a small floating copy button so they're not bare.
      if (!copyAct) {
        const btn = document.createElement("button");
        btn.className = "copy-code";
        btn.type = "button";
        btn.innerHTML = '<i class="ph ph-copy"></i>';
        btn.title = "Copy";
        btn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(getText());
            btn.innerHTML = '<i class="ph ph-check"></i>';
            setTimeout(() => (btn.innerHTML = '<i class="ph ph-copy"></i>'), 1200);
          } catch { toast("Clipboard blocked", "warn", 2000); }
        });
        pre.appendChild(btn);
      }
    });

    // Code-only bubbles: when the agent reply is essentially just one code
    // card, drop the bubble's padding and background so the card itself
    // becomes the surface — kills the "card inside a card" effect.
    // Include `root` itself — the live/final render calls this with the
    // bubble element directly, where querySelectorAll (descendants-only) would
    // miss it; reload calls it with the row. Checking both keeps the slim
    // code-only look consistent before and after a refresh.
    const _agentBubbles = Array.from(root.querySelectorAll(".bubble.agent"));
    if (root.matches && root.matches(".bubble.agent")) _agentBubbles.push(root);
    _agentBubbles.forEach(bubble => {
      const meaningful = Array.from(bubble.children).filter(el => {
        if (el.nodeType !== 1) return false;
        if (el.tagName === "BR") return false;
        if (!(el.textContent || "").trim() && !el.matches?.("pre")) return false;
        return true;
      });
      if (meaningful.length === 1 && meaningful[0].matches?.("pre.code-card")) {
        bubble.classList.add("bubble-code-only");
      } else {
        bubble.classList.remove("bubble-code-only");
      }
    });
  }

  // regenerate the most recent assistant reply by re-sending the turn with
  // regenerate:true.  the backend pops trailing assistant messages and
  // replays the last user message through the same pipeline.
  async function regenerateLast() {
    if (state.streaming) return;
    if (!state.messages.some(m => m.role === "assistant")) {
      toast("Nothing to regenerate yet.", "warn", 2200);
      return;
    }
    // drop the last assistant bubble visually before re-streaming
    while (state.messages.length && state.messages[state.messages.length - 1].role === "assistant") {
      state.messages.pop();
    }
    renderMessages();

    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    agentRow.innerHTML = `
      ${AGENT_AVATAR_HTML}
      <div class="bubble-col">
        <div class="think-container think-line">
          <div class="think-header" style="cursor: pointer;">
            <i class="ph ph-caret-right think-caret"></i>
            <i class="ph ph-brain think-check-icon"></i>
            <span class="think-title shimmer">Regenerating…</span>
          </div>
          <div class="think-content hidden"></div>
        </div>
        <div class="tool-stack" id="tool-stack"></div>
        <div class="bubble agent hidden" id="stream-bubble"></div>
        <div class="bubble-meta streaming">${esc(state.settings.model)} · streaming<span class="typing"><span></span><span></span><span></span></span></div>
      </div>`;
    $("#chat-inner").appendChild(agentRow);
    scrollToBottom(true);

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);
    try {
      await streamChat("", agentRow, state.abortCtl.signal, [], { regenerate: true });
    } catch (e) {
      if (e.name !== "AbortError") toast("regenerate failed: " + e.message, "err");
    } finally {
      state.streaming = false;
      state.abortCtl = null;
      setStreamingUI(false);
      renderRegenerateChip();
    }
  }

  // show an action row (regenerate + copy) under the last assistant bubble.
  // Each non-last bubble already carries its own copy-only .bubble-actions
  // row from renderBubble, so we only need to (a) remove any prior REGEN
  // chip (identified by the regen button — not just any .bubble-actions, or
  // we'd nuke the per-bubble copy rows), (b) drop the last assistant's own
  // copy-only row if present, and (c) install the regen+copy chip there.
  function renderRegenerateChip() {
    document.querySelectorAll('.bubble-actions:has([data-act="regen"])').forEach(el => el.remove());
    const rows = [...document.querySelectorAll("#chat-inner .bubble-row")];
    const lastAssistant = rows.reverse().find(r => r.querySelector(".bubble.agent"));
    if (!lastAssistant) return;
    const col = lastAssistant.querySelector(".bubble-col");
    if (!col) return;
    const meta = col.querySelector(".bubble-meta");
    if (!meta) return;
    // strip any existing copy-only row on this bubble so we don't end up
    // with two action rows stacked under the last assistant message.
    col.querySelectorAll(".bubble-actions").forEach(el => el.remove());
    const bubble = col.querySelector(".bubble.agent");
    const actions = document.createElement("div");
    actions.className = "bubble-actions";
    actions.innerHTML = `
      <button type="button" class="bubble-action" data-act="regen" title="Regenerate"><i class="ph ph-arrow-counter-clockwise"></i></button>
      <button type="button" class="bubble-action" data-act="copy" title="Copy"><i class="ph ph-copy"></i></button>
    `;
    const regenBtn = actions.querySelector('[data-act="regen"]');
    regenBtn.addEventListener("click", regenerateLast);
    // If the last reply came back empty, pulse the retry button so the user
    // knows what to click. Cleared on the next turn (see streamChat).
    if (state.attentionRetry) regenBtn.classList.add("attention");
    actions.querySelector('[data-act="copy"]').addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      const text = bubble?.innerText || "";
      try {
        await navigator.clipboard.writeText(text);
        const icon = btn.querySelector("i");
        if (icon) {
          icon.classList.remove("ph-copy");
          icon.classList.add("ph-check");
          btn.classList.add("copied");
          setTimeout(() => {
            icon.classList.remove("ph-check");
            icon.classList.add("ph-copy");
            btn.classList.remove("copied");
          }, 1400);
        }
      } catch {
        toast("copy failed", "err", 2000);
      }
    });
    meta.after(actions);
  }

  // ---------- image attachments ----------
  function renderImageTray() {
    const tray = $("#image-tray");
    if (!tray) return;
    tray.innerHTML = "";
    if (!state.pendingImages.length) { tray.classList.add("hidden"); return; }
    tray.classList.remove("hidden");
    state.pendingImages.forEach((img, i) => {
      const div = document.createElement("div");
      div.className = "thumb";
      div.innerHTML = `<img src="${img.dataUrl}" alt="${esc(img.name || "image")}"><button class="rm" title="Remove"><i class="ph ph-x"></i></button>`;
      div.querySelector(".rm").addEventListener("click", () => {
        state.pendingImages.splice(i, 1);
        renderImageTray();
      });
      tray.appendChild(div);
    });
  }
  function fileToDataURL(file) {
    return new Promise((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result);
      r.onerror = reject;
      r.readAsDataURL(file);
    });
  }
  async function addImageFiles(files) {
    for (const f of files) {
      if (!f.type.startsWith("image/")) continue;
      try {
        const dataUrl = await fileToDataURL(f);
        state.pendingImages.push({ dataUrl, name: f.name });
      } catch (e) { console.warn("read failed", e); }
    }
    renderImageTray();
  }

  // ---------- send / stream ----------
  async function send(opts = {}) {
    if (state.streaming) return;
    const ta = $("#composer-input");
    let text = opts.prompt !== undefined ? opts.prompt.trim() : ta.value.trim();

    // "review this UI" → auto-capture the preview iframe and attach as an image
    // so the vision model actually sees it. Skip if the user already attached
    // something or the phrase is trivially present in an unrelated way.
    if (text && /\breview this ui\b/i.test(text) && !state.pendingImages.length && state.currentHtml) {
      const canvas = await captureIframePng();
      if (canvas) {
        const dataUrl = canvas.toDataURL("image/png");
        state.pendingImages.push({ dataUrl, name: `ui-${Date.now()}.png` });
        renderImageTray();
      }
    }

    const images = state.pendingImages.slice();
    if (!text && !images.length) return;
    if (!state.settings.model) {
      alert("Pick a model in Settings first.");
      openSettings();
      return;
    }
    if (opts.prompt === undefined) {
      ta.value = "";
      autoResize(ta);
    }
    if (state.chatId) localStorage.removeItem("accuretta:draft:" + state.chatId);
    state.pendingImages = [];
    renderImageTray();

    // show the image count in the user bubble so they know what got sent.
    // Plain text label — no emoji (rendered inline in stored content, where an
    // SVG can't live; "nothing" per the no-emoji rule).
    const imgNote = `${images.length} image${images.length > 1 ? "s" : ""} attached`;
    const bubbleText = images.length
      ? (text ? `${text}\n\n${imgNote}` : imgNote)
      : text;
    const userMsg = { role: "user", content: bubbleText, t: Math.floor(Date.now() / 1000) };
    if (opts.invisible) {
      userMsg.invisible = true;
    }
    state.messages.push(userMsg);

    // Clear welcome screen visually if we are about to append the agent stream bubble
    const welcome = document.querySelector("#chat-inner .welcome-screen");
    if (welcome) {
      $("#chat-inner").innerHTML = "";
    }

    if (!opts.invisible) {
      $("#chat-inner").appendChild(renderBubble(userMsg));
    }
    scrollToBottom(true);
    renderCtxGauge();

    // placeholder agent bubble
    const agentRow = document.createElement("div");
    agentRow.className = "bubble-row";
    agentRow.innerHTML = `
      ${AGENT_AVATAR_HTML}
      <div class="bubble-col">
        <div class="think-container think-line">
          <div class="think-header" style="cursor: pointer;">
            <i class="ph ph-caret-right think-caret"></i>
            <i class="ph ph-brain think-check-icon"></i>
            <span class="think-title shimmer">Thinking…</span>
          </div>
          <div class="think-content hidden"></div>
        </div>
        <div class="tool-stack" id="tool-stack"></div>
        <div class="bubble agent hidden" id="stream-bubble"></div>
        <div class="bubble-meta streaming">${esc(state.settings.model)} · streaming<span class="typing"><span></span><span></span><span></span></span></div>
      </div>`;
    $("#chat-inner").appendChild(agentRow);
    scrollToBottom(true);

    state.streaming = true;
    state.abortCtl = new AbortController();
    setStreamingUI(true);

    try {
      await streamChat(text, agentRow, state.abortCtl.signal, images, opts);
    } catch (e) {
      const b = agentRow.querySelector("#stream-bubble") || agentRow.querySelector(".bubble");
      if (b) {
        if (e.name === "AbortError") b.innerHTML += `<div style="color: var(--fg-faint); font-size:11px; margin-top:6px;">— stopped</div>`;
        else b.innerHTML = `<span style="color: var(--danger)">error: ${esc(e.message)}</span>`;
      }
    } finally {
      state.streaming = false;
      state.abortCtl = null;
      setStreamingUI(false);
      await loadChats();
      renderChatList();
      notifyCompletion();
    }
  }

  function setStreamingUI(on) {
    $("#btn-send").classList.toggle("hidden", on);
    $("#btn-stop").classList.toggle("hidden", !on);
    $("#composer-input").disabled = false; // always allow typing next message
    const comp = document.querySelector(".composer");
    if (comp) comp.classList.toggle("status-thinking", on);
    renderStatus(0, on ? "streaming" : "idle");
    appendAgentLog(on ? "Agent streaming started." : "Agent streaming completed.");
  }

  function stopStreaming() {
    // tell the bridge to force-close the llama-server socket first — otherwise
    // generation keeps running server-side until it hits its own limit.
    const cid = state.chatId;
    if (cid) {
      try {
        fetch("/api/cancel", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: cid }),
          keepalive: true,
        }).catch(() => {});
      } catch {}
    }
    if (state.abortCtl) {
      try { state.abortCtl.abort(); } catch {}
    }
  }

  async function streamChat(text, agentRow, signal, images, opts) {
    const regenerate = !!(opts && opts.regenerate);
    const bubble = agentRow.querySelector("#stream-bubble");
    const toolStack = agentRow.querySelector("#tool-stack");
    // Cleared each turn; the empty-reply branch sets it true so the retry
    // button pulses for attention until a real reply lands.
    state.attentionRetry = false;
    let buf = "";
    const toolCards = new Map();

    // heartbeat: if no delta arrives, rotate through varied status lines
    // so the user sees the model is alive (not frozen). Mix of plain progress
    // and dry one-liners. Never repeats until the pool is exhausted.
    const idlePool = [
      "still working", "thinking it through", "crunching tokens",
      "wrangling the model", "hitting the monitor with a hammer",
      "politely asking the weights", "re-reading the prompt",
      "weighing options", "arguing with itself", "lining up the next move",
      "checking its own math", "rehearsing the reply", "taking the scenic route",
      "compiling thoughts", "sharpening the pencil", "consulting the rubber duck",
      "shaking the dice", "yelling at the GPU",
    ];
    let pool = idlePool.slice();
    let currentIdle = "still working";
    let lastActivity = Date.now();
    let lastRotate = 0;
    const started = lastActivity;
    const markActivity = () => { lastActivity = Date.now(); };
    const heartbeat = setInterval(() => {
      const line = agentRow.querySelector(".think-line");
      if (!line || line.classList.contains("done")) return;
      const span = line.querySelector("span");
      if (!span || !span.classList.contains("shimmer")) return;
      const idle = Math.floor((Date.now() - lastActivity) / 1000);
      const total = Math.floor((Date.now() - started) / 1000);
      if (idle < 3) return;
      // rotate phrase every 6 seconds of continuous idleness
      if (Date.now() - lastRotate > 6000) {
        if (!pool.length) pool = idlePool.slice();
        currentIdle = pool.splice(Math.floor(Math.random() * pool.length), 1)[0];
        lastRotate = Date.now();
      }
      span.textContent = `${currentIdle}… ${total}s`;
    }, 1000);

     const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: state.chatId,
        message: text,
        mode: state.mode,
        images: (images || []).map(x => x.dataUrl),
        regenerate,
        invisible: !!(opts && opts.invisible),
      }),
      signal,
    });
    if (!resp.body) throw new Error("no response body");

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let carry = "";
    let ended = false;

    try {
      while (!ended) {
        const { value, done } = await reader.read();
        if (done) break;
        carry += dec.decode(value, { stream: true });
        const chunks = carry.split(/\n\n/);
        carry = chunks.pop();
        for (const chunk of chunks) {
          const line = chunk.split("\n").find(l => l.startsWith("data: "));
          if (!line) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          try {
            handleEvent(evt, { bubble, toolStack, toolCards, row: agentRow, getBuf: () => buf, setBuf: v => buf = v });
          } catch (err) {
            // A single bad event must not tear down the read loop — later events
            // include stats (token/cost accounting) and the final message. Log
            // and keep consuming.
            console.error("handleEvent failed on", evt && evt.type, err);
          }
          markActivity();
          if (evt.type === "chat_end") { ended = true; break; }
        }
      }
    } finally {
      try { await reader.cancel(); } catch {}
      clearInterval(heartbeat);
      if (agentRow) {
        updateThinkLine(agentRow, false);
        const meta = agentRow.querySelector(".bubble-meta");
        if (meta) {
          meta.classList.remove("streaming");
          meta.querySelectorAll(".typing").forEach(d => d.remove());
        }
        // Move tool strip below the bubble + apply faded "done-pill" styling
        // so it looks like a footnote, not part of the answer.
        finalizeToolGroup(agentRow);
      }
      // safety net: if the model ran tools or thought for a while but ended
      // without a visible answer, surface what we have so the user isn't
      // staring at nothing. Promote the tail of thinking if it's substantive.
      if (bubble && bubble.classList.contains("hidden")) {
        const { thinking, content } = splitThinking(buf);
        const cascadeRes = splitCascade(content);
        const hasCascade = cascadeRes.cascade && cascadeRes.cascade.length > 0;
        const hadTools = toolStack && toolStack.children.length > 0;
        
        if (!hasCascade) {
          bubble.classList.remove("hidden");
          bubble.classList.add("quiet");
          // Reply came back empty/incomplete — flag so the retry button pulses.
          state.attentionRetry = true;
          // All three empty-state branches render as: leading info icon +
          // italic message text. The CSS for .bubble.quiet handles the flex
          // layout, padding, and accent-tinted icon — see .quiet-icon there.
          if (!hadTools && thinking && thinking.length > 40) {
            const tail = thinking.length > 900 ? "…" + thinking.slice(-900) : thinking;
          bubble.innerHTML =
            `<i class="quiet-icon ph ph-info"></i>` +
            `<div class="quiet-text">` +
              `<div style="margin-bottom:6px;opacity:0.85;font-size:12px;">model spent its whole budget thinking — here's the tail</div>` +
              `<pre style="white-space:pre-wrap;font-family:inherit;margin:0;font-style:normal;">${esc(tail)}</pre>` +
            `</div>`;
        } else {
          let msg = "No response — the model may have crashed or hit a context limit. Check the backend console for errors.";
          if (hadTools) {
            msg = "model ended turn without a reply — ask it what it found, or try again";
          } else if (state.settings.num_predict > 0 && state.settings.num_predict < 50) {
            msg = `No response — Max reply tokens is set very low (${state.settings.num_predict}). Try raising it in Settings.`;
          } else if (state.settings.num_predict === 0) {
            msg = "No response — Max reply tokens is set to 0. Try raising it in Settings.";
          } else if ((images && images.length > 0) && (state.settings.spec_strategy === "draft-mtp" || state.settings.enable_speculative)) {
            msg = "No response — Speculative Decoding often crashes when processing images. Try disabling it in Settings.";
          }
          bubble.innerHTML =
            `<i class="quiet-icon ph ph-info"></i>` +
            `<span class="quiet-text">${esc(msg)}</span>`;
          }
        }
      }
    }
  }

  // strip reasoning wrappers from several model families so the chat bubble
  // only shows the final answer. Accumulate thinking text into the think line.
  function splitThinking(buf) {
    // tags observed: <think>, <thinking>, <reasoning>, and <|thinking|>…<|/thinking|>.
    // bracketed reasoning tags: [thought], [thinking], [reasoning], [scratchpad]…
    // many local models (Qwen/DeepSeek/Nemotron) emit bare </think> with no opening tag,
    // sometimes multiple times between tool rounds. rule: everything up to the LAST closing
    // reasoning tag is thinking; everything after is the visible answer.
    const closeRe = /<\/(?:think|thinking|reasoning)>|<\|\/thinking\|>|\[\/(?:thought|thinking|reasoning|scratchpad)\]/gi;
    let lastClose = -1;
    let m;
    while ((m = closeRe.exec(buf)) !== null) lastClose = m.index + m[0].length;

    let thinking = "";
    let content = "";
    if (lastClose >= 0) {
      thinking = buf.slice(0, lastClose);
      content = buf.slice(lastClose);
    } else {
      // no closing tag yet — if an opening tag is present, everything from it is in-flight thinking
      const openIdx = buf.search(/<(?:think|thinking|reasoning)>|<\|thinking\|>|\[(?:thought|thinking|reasoning|scratchpad)\]/i);
      if (openIdx >= 0) {
        content = buf.slice(0, openIdx);
        thinking = buf.slice(openIdx);
        
        // Implicit close: if the model forgot to close the think tag but started a
        // native tool call, treat the tool call opener as the end of the thinking block.
        const implicitCloseIdx = thinking.search(/<\/?tool_call>|<\|tool_call>|<call:[a-zA-Z0-9_\-]+>|\[TOOL_CALLS\]|```tool_call/i);
        if (implicitCloseIdx > 0) {
          content += thinking.slice(implicitCloseIdx);
          thinking = thinking.slice(0, implicitCloseIdx);
        }
      } else {
        content = buf;
      }
    }
    const stripTags = /<\/?(?:think|thinking|reasoning)>|<\|\/?thinking\|>|\[\/?(?:thought|thinking|reasoning|scratchpad)\]/gi;
    thinking = thinking.replace(stripTags, "").trim();
    content = content.replace(stripTags, "");
    // strip model-specific content delimiters that leak into output:
    //   GLM 4.x: ◁begin_of_box▷ … ◁end_of_box▷  and the <|…|> variants
    //   Command-R: <|START_OF_TURN_TOKEN|> etc.
    //   generic: <|im_start|>assistant / <|im_end|>, <|eot_id|>, [INST] wrappers
    const junk = [
      /◁\|?begin_of_box\|?▷/gi,
      /◁\|?end_of_box\|?▷/gi,
      /<\|?begin_of_box\|?>/gi,
      /<\|?end_of_box\|?>/gi,
      /<\|im_start\|>(?:assistant|user|system)?/gi,
      /<\|im_end\|>/gi,
      /<\|eot_id\|>/gi,
      /<\|start_header_id\|>[\s\S]*?<\|end_header_id\|>/gi,
      /<\|(?:START|END)_OF_TURN_TOKEN\|>/gi,
      /<\|begin_of_text\|>/gi,
      /<\|end_of_text\|>/gi,
      /\[\/?INST\]/gi,
      /<s>|<\/s>/gi,
      // Orphan tool_call tags. The bridge's tool_call extractor only strips
      // MATCHED <tool_call>...</tool_call> pairs; if the model emits a stray
      // opener / closer without a partner, it bleeds into the visible reply.
      // Gemma + some other tunes do this routinely after their last real
      // tool call, leaving artifacts like a lone "</tool_call>" or "</".
      /<\/?tool_call>/gi,
      /<\/?function(?:=\w+)?>/gi,
      // Quote-wrapper special tokens. Gemma 4's native tool-call dialect
      // uses <|"|> as a STRING DELIMITER (not a quote replacement) — the
      // bridge's TOOL_CALL_GEMMA_RE parser consumes valid <|tool_call>…
      // blocks before this filter runs, so anything that reaches here is
      // an orphan / partial emit. Strip both quote-token variants so the
      // visible bubble is clean.
      /<\|"\|>/g,
      /<\|'\|>/g,
      // Note: we DO NOT strip <|tool_call> tags here anymore. If we strip
      // the tags here, the naked body (NAME{...}) bleeds into the UI because
      // renderMarkdown won't be able to find the start/end bounds to strip
      // the whole block. renderMarkdown handles it instead.
    ];
    for (const re of junk) { thinking = thinking.replace(re, ""); content = content.replace(re, ""); }
    // Trailing partial-tag stripper. Catches the case where the stream cuts
    // mid-tag — `<`, `</`, `</to`, `</tool_call` (no closing `>`), `</think`
    // etc. — and any leading whitespace before it. Scoped to known tag
    // names so we don't accidentally eat legitimate trailing `<` characters
    // in prose like "use the < operator".
    content = content.replace(/\s*<\/?(?:tool_call|tool|call|think|thinking|reasoning|function|im_start|im_end)\w*\s*$/i, "");
    content = content.replace(/\s*\[\/?(?:thought|thinking|reasoning|scratchpad)\w*\s*$/i, "");
    content = content.replace(/\s*<\/\s*$/, "");  // bare "</" with nothing after
    content = content.replace(/\s*\[\/?\s*$/, ""); // bare "[" or "[/" with nothing after
    return { thinking: thinking.trim(), content };
  }

  function splitCascade(buf) {
    let cascade = null;
    let content = buf;
    
    // Match <cascade>, \<cascade\>, or &lt;cascade&gt;
    const cascadeRe = /(?:<|&lt;|\\<)cascade(?:>|&gt;|\\>)([\s\S]*?)(?:<|&lt;|\\<)\/cascade(?:>|&gt;|\\>)/i;
    const match = cascadeRe.exec(buf);
    
    if (match) {
      try {
        // Unescape entities and replace smart quotes
        let jsonStr = match[1].trim()
            .replace(/['‘’]/g, '"')
            .replace(/&quot;/g, '"');
        cascade = JSON.parse(jsonStr);
        if (!Array.isArray(cascade)) cascade = null;
      } catch (e) {
        cascade = null;
      }
      content = buf.replace(cascadeRe, "").trim();
    } else {
      // Hide partial tags while streaming (handles <, \<, and &lt;)
      content = content.replace(/(?:<|&lt;|\\<)cascade[\s\S]*$/i, "").trim();
      // Catch even smaller partials like `<cas` at the absolute end of the stream
      content = content.replace(/(?:<|&lt;|\\<)c(?:a(?:s(?:c(?:a(?:d(?:e)?)?)?)?)?)?$/i, "").trim();
    }
    
    return { cascade, content };
  }
  function updateThinkLine(row, running, label) {
    const container = row.querySelector(".think-container");
    if (!container) return;
    const span = container.querySelector(".think-title");
    const icon = container.querySelector(".think-check-icon");
    
    if (running && !container._thinkStart) {
      container._thinkStart = Date.now();
    }
    if (!running) {
      container.classList.add("done");
      if (span) span.classList.remove("shimmer");
      const elapsed = container._thinkStart ? Math.max(1, Math.round((Date.now() - container._thinkStart) / 1000)) : 0;
      const fmt = elapsed >= 60
        ? `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`
        : `${elapsed}s`;
      const finalLabel = elapsed > 0
        ? `Thought for ${fmt}`
        : (label || "Thought for a moment");
      if (span) span.textContent = finalLabel;
      if (icon) {
        icon.className = "ph ph-check-circle think-check-icon done";
      }
      return;
    }
    if (label) {
      if (span) span.textContent = label;
    }
  }
  // ---------- attack-chain progress rail (red-team flow) ----------
  // A slim kill-chain visual that lights up as the model breaches. Driven
  // entirely by events already emitted — tool_start (activity line + which
  // stage is active) and breach (a captured FLAG advances a stage). Only shown
  // when a red-team tool or breach fires, so ordinary coding turns never see
  // it. Colors are theme tokens, so it adapts to every theme automatically.
  const ATTACK_NODES = [
    { label: "recon",  tech: "",      icon: "ph-crosshair" },
    { label: "access", tech: "T1548", icon: "ph-key" },
    { label: "pivot",  tech: "T1190", icon: "ph-path" },
    { label: "rce",    tech: "T1059", icon: "ph-terminal-window" },
  ];
  function isRedTeamTool(name) {
    return !!name && (name.startsWith("recon_") || name === "http_request" || name === "encode_decode");
  }
  function attackActivitySummary(name, args) {
    args = args || {};
    if (name === "http_request") {
      const m = (args.method || "GET").toUpperCase();
      let u = String(args.url || args.target || "");
      u = u.replace(/^https?:\/\/[^/]+/i, "") || u; // keep path+query when possible
      if (u.length > 44) u = u.slice(0, 44) + "…";
      return `http_request → ${m} ${u || "/"}`;
    }
    if (name === "encode_decode") {
      return `encode_decode → ${args.scheme || "base64"} ${args.operation || "encode"}`;
    }
    if (name && name.startsWith("recon_")) {
      return `${name} → ${args.url || args.target || args.domain || args.host || ""}`;
    }
    return name || "";
  }
  function ensureAttackRail(row) {
    if (!row) return null;
    let rail = row.querySelector(".attack-rail");
    if (rail) return rail;
    const toolStack = row.querySelector(".tool-stack");
    rail = document.createElement("div");
    rail.className = "attack-rail";
    rail.dataset.flags = "0";
    rail.dataset.active = "-1";
    rail.dataset.recondone = "0";
    const nodesHtml = ATTACK_NODES.map((n, i) =>
      `<div class="ar-node is-pending" data-i="${i}">` +
        `<span class="ar-dot"><i class="ph ${n.icon}"></i></span>` +
        `<span class="ar-label">${n.label}</span>` +
        `<span class="ar-tech">${n.tech || "&nbsp;"}</span>` +
      `</div>` +
      (i < ATTACK_NODES.length - 1 ? `<span class="ar-seg" data-s="${i}"></span>` : "")
    ).join("");
    rail.innerHTML =
      `<div class="ar-head">` +
        `<span class="ar-title"><i class="ph ph-crosshair"></i> attack chain</span>` +
        // No fixed denominator — real engagements have no set number of "flags"
        // to find (that's a CTF concept). Hidden until a FLAG{...} is actually
        // captured, then shows a plain running count. See renderRail.
        `<span class="ar-flags" hidden><i class="ph ph-flag"></i> <span class="ar-flag-count"></span></span>` +
      `</div>` +
      `<div class="ar-track">${nodesHtml}</div>` +
      `<div class="ar-activity"><span class="ar-pulse"></span><span class="ar-activity-text">initializing…</span></div>` +
      `<div class="ar-banner"><i class="ph ph-shield-check"></i> confirmed access — full chain breached</div>`;
    if (toolStack && toolStack.parentNode) toolStack.parentNode.insertBefore(rail, toolStack);
    else row.appendChild(rail);
    return rail;
  }
  function renderRail(rail) {
    const flags = +rail.dataset.flags;
    const active = +rail.dataset.active;
    const reconDone = rail.dataset.recondone === "1";
    // node 0 = recon (no flag; "done" once exploitation starts or any flag lands)
    const done = [reconDone || flags >= 1, flags >= 1, flags >= 2, flags >= 3];
    rail.querySelectorAll(".ar-node").forEach((el) => {
      const i = +el.dataset.i;
      el.classList.toggle("is-active", i === active);
      el.classList.toggle("is-done", done[i] && i !== active);
      el.classList.toggle("is-pending", !done[i] && i !== active);
    });
    rail.querySelectorAll(".ar-seg").forEach((el) => {
      el.classList.toggle("is-done", done[+el.dataset.s]);
    });
    // Flag pill: hidden at zero (no misleading "0/N captured" on a clean
    // target), then a plain count once something is actually captured.
    const flagsRaw = +(rail.dataset.flagsraw || 0);
    const flagsWrap = rail.querySelector(".ar-flags");
    if (flagsWrap) flagsWrap.hidden = flagsRaw <= 0;
    const fc = rail.querySelector(".ar-flag-count");
    if (fc) fc.textContent = `${flagsRaw} captured`;
    rail.classList.toggle("has-flags", flags > 0);
    rail.classList.toggle("is-complete", flags >= 3);
  }
  function attackRailToolStart(row, name, args) {
    if (!isRedTeamTool(name)) return;
    const rail = ensureAttackRail(row);
    if (!rail) return;
    const txt = rail.querySelector(".ar-activity-text");
    if (txt) txt.textContent = attackActivitySummary(name, args);
    rail.querySelector(".ar-pulse")?.classList.add("live");
    if (name.startsWith("recon_")) {
      if (+rail.dataset.flags === 0 && rail.dataset.recondone !== "1") rail.dataset.active = "0";
    } else {
      // exploitation began — recon is behind us; light the current stage
      rail.dataset.recondone = "1";
      if (+rail.dataset.active < 1) rail.dataset.active = "1";
    }
    renderRail(rail);
  }
  function attackRailBreach(row, stage) {
    const rail = ensureAttackRail(row);
    if (!rail) return;
    const raw = Math.max(1, parseInt(stage, 10) || 1);
    // dataset.flags (capped at 3) drives the 4-node visual; flagsraw is the
    // TRUE captured count shown in the pill, so a range with >3 flags isn't
    // undercounted.
    const s = Math.min(3, raw);
    rail.dataset.flags = String(Math.max(+rail.dataset.flags, s));
    rail.dataset.flagsraw = String(Math.max(+(rail.dataset.flagsraw || 0), raw));
    rail.dataset.recondone = "1";
    rail.dataset.active = s < 3 ? String(s + 1) : "-1";
    if (s >= 3) rail.querySelector(".ar-pulse")?.classList.remove("live");
    renderRail(rail);
  }

  function handleEvent(evt, ctx) {
    const { bubble, toolStack, toolCards, row } = ctx;
    if (evt.type === "delta") {
      const newBuf = ctx.getBuf() + evt.content;
      ctx.setBuf(newBuf);
      // Live cost estimate: count EVERY delta (chars/4). This MUST run per-delta,
      // not inside the render throttle below — otherwise a fast code stream is
      // mostly uncounted and the visible cost looks frozen until the round's real
      // eval_count lands. Uses delta length, not buf length, because buf resets
      // between agent rounds but the estimate should accumulate across the turn.
      state._streamOutEstimate += Math.round(evt.content.length / 4);
      // Throttled gauge + cost RENDER during streaming (display only).
      if (!state._lastGaugeUpdate || Date.now() - state._lastGaugeUpdate > 500) {
        renderCtxGauge();
        renderCostWidget();
        state._lastGaugeUpdate = Date.now();
      }
      // Live tok/s in the bubble meta — stash the start time on the agentRow
      // DOM node (persists across deltas; ctx itself is rebuilt every event).
      // Token count is approximate (chars/4) until the final stats arrives.
      if (ctx.row) {
        if (!ctx.row._streamStart) ctx.row._streamStart = Date.now();
        const elapsed = (Date.now() - ctx.row._streamStart) / 1000;
        if (elapsed > 0.5 && (!ctx.row._lastTpsUpdate || Date.now() - ctx.row._lastTpsUpdate > 400)) {
          ctx.row._lastTpsUpdate = Date.now();
          const approxTokens = Math.max(1, Math.round(newBuf.length / 4));
          const liveTps = (approxTokens / elapsed).toFixed(1);
          renderStatus(liveTps, "streaming");
          const meta = ctx.row.querySelector(".bubble-meta.streaming");
          if (meta) {
            const dots = meta.querySelector(".typing");
            meta.innerHTML = `${esc(state.settings.model)} · ${liveTps} tok/s · streaming`;
            if (dots) meta.appendChild(dots);
            else {
              const d = document.createElement("span");
              d.className = "typing";
              d.innerHTML = "<span></span><span></span><span></span>";
              meta.appendChild(d);
            }
          }
        }
      }
      let { thinking, content } = splitThinking(newBuf);
      const cascadeRes = splitCascade(content);
      content = cascadeRes.content;
      
      if (thinking && ctx.row) {
        // first few words of current thinking snippet, shimmering
        const preview = thinking.split(/\s+/).slice(-12).join(" ");
        updateThinkLine(ctx.row, true, preview || "Thinking…");
        const thinkContent = ctx.row.querySelector(".think-content");
        if (thinkContent) {
          thinkContent.textContent = thinking;
        }
      }
      
      // Render live cascade chips into the parent container regardless of other content
      if (cascadeRes.cascade && cascadeRes.cascade.length > 0) {
        let btns = cascadeRes.cascade.map(text => 
          `<button class="cascade-chip" data-prompt="${esc(text)}"><i class="ph ph-sparkle"></i>${esc(text)}</button>`
        ).join("");
        let container = ctx.row.querySelector(".cascade-container");
        if (!container) {
          container = document.createElement("div");
          container.className = "cascade-container";
          bubble.parentNode.appendChild(container);
        }
        container.innerHTML = btns;
      } else {
        let container = ctx.row.querySelector(".cascade-container");
        if (container) container.remove();
      }

      if (content.trim()) {
        bubble.classList.remove("hidden");
        // Detect an in-progress LARGE code fence. The full markdown render
        // (strip + syntax-highlight + line-wrap) is O(N) on the whole buffer,
        // so re-running it on every token while the model dumps a 700-line
        // HTML doc lags the page hard. For that ONE case we swap to a cheap
        // progress placeholder; everything else (plain text, small code
        // snippets) still streams token-by-token like normal.
        const openFenceMatch = content.match(/```(\w*)\n([\s\S]*)$/);
        const inOpenFence = openFenceMatch && (content.match(/```/g) || []).length % 2 === 1;
        const fenceBodyLen = inOpenFence ? openFenceMatch[2].length : 0;

        if (inOpenFence && fenceBodyLen > 4000) {
          // Big code-in-progress: throttle to 400ms and skip highlighting.
          // The final-event handler does the proper render at the end so the
          // user still gets the full code-card with syntax colors.
          const now = Date.now();
          if (now - (bubble._lastProgressAt || 0) >= 400) {
            const lang = (openFenceMatch[1] || "code").toLowerCase();
            const lines = (openFenceMatch[2].match(/\n/g) || []).length + 1;
            const kb = (fenceBodyLen / 1024).toFixed(1);
            bubble.innerHTML = `<div class="code-progress"><i class="ph ph-code code-progress-icon"></i><span class="code-progress-text">writing <strong>${esc(lang)}</strong> — ${lines} lines, ${kb} KB so far…</span></div>`;
            bubble._lastProgressAt = now;
          }
        } else {
          // Plain text or small code: render every delta. Reset the
          // progress flag so the next big-code stream starts fresh.
          bubble._lastProgressAt = 0;
          let renderable = content;
          const openCount = (renderable.match(/```/g) || []).length;
          if (openCount % 2 === 1) renderable = renderable + "\n```";
          bubble.innerHTML = renderMarkdown(renderable);
          enhanceCodeBlocks(bubble);
        }
        if (ctx.row) updateThinkLine(ctx.row, false);
      } else if (bubble.innerHTML && !bubble.classList.contains("hidden")) {
        // Content was stripped to empty (e.g. model emitted only a partial
        // </tool_call> that splitThinking's junk filters cleaned out).
        // Reset the bubble + re-hide so the end-of-stream empty-bubble
        // fallback can fire ("model ended turn without a reply…") instead
        // of leaving stale partial characters from before the strip.
        bubble.innerHTML = "";
        bubble.classList.add("hidden");
      }
      scrollToBottom();
    } else if (evt.type === "tool_start") {
      attackRailToolStart(row, evt.name, evt.arguments);
      if (evt.name === "run_powershell") {
        const cmd = evt.arguments?.command || "";
        appendTerminalText(`\n$ ${cmd}\n`, false);
        appendAgentLog(`Command execution started: ${cmd}`);
      } else {
        const lbl = toolLabel(evt.name, evt.arguments);
        appendAgentLog(`Tool started: ${evt.name} -> ${lbl}`);
      }
      const { group, body } = getOrCreateToolGroup(toolStack);
      const card = document.createElement("div");
      card.className = "tool-line running";
      const customIcon = toolIconHtml(evt.name, "run");
      const iconHtml = customIcon || `<i class="ph ph-circle-notch"></i>`;
      card.innerHTML = `${iconHtml}<span class="shimmer">${esc(toolLabel(evt.name, evt.arguments))}</span>`;
      card.dataset.name = evt.name || "";
      body.appendChild(card);
      // Reflect the new tool in the head's activity slot, then refresh head
      // counts. The head stays expanded-on-click only; collapsed visually but
      // the live activity line is always visible at the top.
      updateToolGroupActivity(group, evt);
      updateToolGroupHead(toolStack);
      // Update the think line to show what the agent is actually doing
      if (row) {
        const label = toolLabel(evt.name, evt.arguments);
        updateThinkLine(row, true, label);
      }
      // Estimate tokens for the tool call itself — the model generated
      // the tool name + JSON arguments, which aren't in content deltas.
      const argsLen = evt.arguments ? JSON.stringify(evt.arguments).length : 0;
      state._streamOutEstimate += Math.round((evt.name.length + argsLen) / 4);
      renderCostWidget();
      scrollToBottom();
    } else if (evt.type === "tool_stream") {
      if (evt.name === "run_powershell") {
        appendTerminalText(evt.text || "", false);
      }
      const cards = Array.from(toolStack.querySelectorAll(".tool-line.running"));
      const card = cards.reverse().find(c => c.dataset.name === evt.name);
      if (card) {
        const span = card.querySelector("span");
        if (span) span.textContent = (evt.text || "").slice(-120);
      }
    } else if (evt.type === "heartbeat") {
      // while a long tool runs, the backend sends heartbeats — keep the shimmer alive
      const line = row && row.querySelector(".think-line");
      if (line && !line.classList.contains("done")) {
        const span = line.querySelector("span");
        if (span && span.classList.contains("shimmer")) {
          span.textContent = (evt.note || "working…").slice(0, 80);
        }
      }
    } else if (evt.type === "tool_result") {
      if (evt.name === "run_powershell") {
        const isErr = evt.result && evt.result.error;
        const exitCode = evt.result && evt.result.exit_code !== undefined ? evt.result.exit_code : (isErr ? 1 : 0);
        appendTerminalText(`\nCommand finished with exit code ${exitCode}\n`, isErr);
        appendAgentLog(`Command execution finished: exit code ${exitCode}`);
      } else {
        const label = toolResultLabel(evt.name, evt.result);
        appendAgentLog(`Tool finished: ${evt.name} -> ${label}`);
      }
      const cards = Array.from(toolStack.querySelectorAll(".tool-line.running"));
      const card = cards.reverse().find(c => c.dataset.name === evt.name);
      if (card) {
        const isErr = evt.result && evt.result.error;
        card.classList.remove("running");
        card.classList.add(isErr ? "err" : "done");
        const customIcon = toolIconHtml(evt.name, isErr ? "err" : "done");
        const iconHtml = customIcon || `<i class="ph ${isErr ? "ph-x-circle" : "ph-check"}"></i>`;
        const label = toolResultLabel(evt.name, evt.result);
        card.innerHTML = `${iconHtml}<span>${esc(label)}</span>`;
        if (!isErr && (evt.name === "edit_file" || evt.name === "write_file")) {
          if (evt.result && evt.result.path) {
            state.touchedFiles.add(evt.result.path);
            renderWorkspace();
          }
          const added = (evt.result && evt.result.added) || 0;
          const deleted = (evt.result && evt.result.deleted) || 0;
          if (added > 0 || deleted > 0) {
            const filename = folderLeafName(evt.result.path || "");
            const msg = `<span style="color:#00ff88;font-weight:600;">+${added}</span>, <span style="color:#ff3b30;font-weight:600;">-${deleted}</span> <span style="opacity:0.4;margin:0 4px;">|</span> <span style="font-weight:500;">${esc(filename)}</span>`;
            toast(msg, "ok", 4000);
          }
          // On mobile: inject a preview card for .html files written by the agent
          const filePath = (evt.result && evt.result.path) || "";
          if (isMobile() && /\.html?$/i.test(filePath)) {
            const wsRoot = (state.workspace && state.workspace.folders && state.workspace.folders[0]) || "";
            if (wsRoot) {
              const rel = filePath.replace(/\\/g, "/").replace(wsRoot.replace(/\\/g, "/"), "").replace(/^\//, "");
              injectMobilePreviewCard({
                filename: folderLeafName(filePath),
                size: evt.result.bytes || 0,
                url: wsFileUrl(wsRoot, rel),
              });
            } else {
              // No workspace root — try blob URL with the content if available
              injectMobilePreviewCard({
                filename: folderLeafName(filePath),
                size: evt.result.bytes || 0,
              });
            }
          }
        }
        // web_search: refresh the head's chip strip so sources show inline
        // without stacking. Each new search REPLACES the chip set with a quick
        // fade — that's the "rotating sources" behavior the user asked for.
        if (!isErr && evt.name === "web_search") {
          const group = toolStack.querySelector(".tool-group");
          refreshHeadChips(group, evt.result && evt.result.results);
          // Also append the full chip list inside the body for when the user
          // expands the group — useful when many sources came back.
          const chips = renderWebSearchChips(evt.result && evt.result.results);
          if (chips) card.insertAdjacentHTML("afterend", chips);
        }
        // network_snapshot: rich bar-chart card. Mount it OUTSIDE the
        // tool-group so it stays visible even when the group is collapsed
        // (the group is collapsed by default — putting the chart inside the
        // body meant the bars were invisible until the user clicked the pill,
        // which is why the model's own markdown table was filling the gap).
        if (!isErr && evt.name === "network_snapshot") {
          const chart = renderNetworkChart(evt.result);
          if (chart) {
            const group = toolStack.querySelector(".tool-group");
            if (group && group.parentElement) {
              group.insertAdjacentHTML("afterend", chart);
            } else {
              card.insertAdjacentHTML("afterend", chart);
            }
          }
        }
        updateToolGroupHead(toolStack);
      }
    } else if (evt.type === "tool_dialect_warning") {
      // Display dialect parsing errors so the user knows the tool didn't run.
      // Rendered as a distinct system notice — visually separated from the
      // model's prose so it doesn't read as model output.
      bubble.classList.remove("hidden");
      bubble.innerHTML += `<div class="dialect-warn"><i class="ph ph-warning"></i><span><strong>Warning:</strong> ${esc(evt.message)}</span></div>`;
    } else if (evt.type === "tools_unavailable") {
      // Model can't do tools (e.g. Gemma). Render as a persistent sibling ABOVE
      // the bubble — NOT inside it, since bubble.innerHTML gets replaced as
      // content streams. Reuses the .dialect-warn notice styling.
      let note = row && row.querySelector(".tools-off-note");
      if (!note && bubble && bubble.parentNode) {
        note = document.createElement("div");
        note.className = "dialect-warn tools-off-note";
        bubble.parentNode.insertBefore(note, bubble);
      }
      if (note) {
        note.innerHTML = `<i class="ph ph-warning"></i><span><strong>Tools off:</strong> ${esc(evt.message)}</span>`;
      }
    } else if (evt.type === "context_trimmed") {
      // Conveyor belt elision notice — show a single pill above the agent
      // row's tool stack. Replaces any prior pill from this turn so re-rounds
      // don't stack pills.
      if (row) {
        let pill = row.querySelector(".ctx-trim-pill");
        if (!pill) {
          pill = document.createElement("div");
          pill.className = "ctx-trim-pill";
          const tip = "Conversation exceeds context window. Older middle messages aren't sent to the model to save space. Your system prompt and first message are always preserved.";
          pill.innerHTML = `
            <i class="ph ph-arrows-in-line-horizontal"></i>
            <span class="ctx-trim-text"></span>
            <i class="ph ph-info ctx-trim-info" title="${esc(tip)}"></i>`;
          // Insert above tool-stack so it sits at the top of the agent column.
          const stack = row.querySelector(".tool-stack");
          if (stack && stack.parentNode) stack.parentNode.insertBefore(pill, stack);
          else row.appendChild(pill);
        }
        const text = pill.querySelector(".ctx-trim-text");
        if (text) text.textContent = `${evt.dropped} message${evt.dropped === 1 ? "" : "s"} summarized`;
        row.dataset.dropped = evt.dropped;
        // Re-render tool group head if it exists to pick up the dropped count
        const stack = row.querySelector(".tool-stack");
        if (stack) updateToolGroupHead(stack);
      }
    } else if (evt.type === "version_saved") {
      state.versions.push(evt.version);
      renderVersions();
      setActiveVersion(evt.version.id);
    } else if (evt.type === "stats") {
      const tok = evt.eval_count;
      const dur = (evt.eval_duration || 0) / 1e9;
      if (Number.isFinite(tok)) {
        state.tokTotal += tok;
        // Clear streaming estimate — real values have arrived
        state._streamOutEstimate = 0;
        state._streamPromptEstimate = 0;
      }
      if (dur > 0 && tok > 0) {
        state.totalGenDuration = (state.totalGenDuration || 0) + dur;
      }
      if (Number.isFinite(tok)) {
        renderTokTotal();
      }
      const tps = dur > 0 ? (tok / dur).toFixed(1) : "—";
      renderStatus(tps, "idle");
      const meta = bubble.parentElement.querySelector(".bubble-meta");
      if (meta) {
        meta.textContent = `${state.settings.model} · ${tok} tok · ${tps} tok/s`;
        if (state.streaming && meta.classList.contains("streaming")) {
          const dots = document.createElement("span");
          dots.className = "typing";
          dots.innerHTML = "<span></span><span></span><span></span>";
          meta.appendChild(dots);
        }
      }
      // accumulate prompt tokens for cost widget
      const promptTok = evt.prompt_eval_count;
      if (Number.isFinite(promptTok) && promptTok > 0) {
        state.tokPromptTotal += promptTok;
      }
      // Persist to all-time counters
      _accumulateAllTime(tok || 0, promptTok || 0);
      renderCostWidget();
      // In agent mode the model may do multiple rounds (tool calls + re-inference).
      // Each round emits stats. Reset the stream start so the next round's
      // live tok/s display starts fresh, not from the first round's timestamp.
      if (ctx.row) {
        ctx.row._streamStart = null;
        ctx.row._lastTpsUpdate = null;
      }
      // stash for the final message object
      state._lastMsgTokens = tok;
      state._lastMsgPromptTokens = evt.prompt_eval_count;
      // refresh gauge live — prompt_eval_count is the truth from llama-server,
      // and tool-heavy turns can blow past where the char-count estimate sits.
      renderCtxGauge();
    } else if (evt.type === "final") {
      const full = evt.message.content || "";
      const msg = {
        role: "assistant",
        content: full,
        t: Math.floor(Date.now() / 1000),
        tokens: state._lastMsgTokens || 0,
        prompt_tokens: state._lastMsgPromptTokens || 0,
      };
      // Fallback: if stats event never fired (some llama-server versions
      // don't emit timings/usage), use the streaming char estimate so the
      // cost widget isn't stuck at $0.00 after generation.
      if (!state._lastMsgTokens && full.length > 0) {
        const fallbackTok = Math.max(1, Math.round(full.length / 4));
        state.tokTotal += fallbackTok;
        msg.tokens = fallbackTok;
        // Clear streaming estimate since we've committed the fallback
        state._streamOutEstimate = 0;
        state._streamPromptEstimate = 0;
        // Persist fallback to all-time counters
        _accumulateAllTime(fallbackTok, 0);
        renderTokTotal();
        renderCostWidget();
      }
      state.messages.push(msg);
      state._lastMsgTokens = 0;
      // Keep the real prompt-token count from this turn so the gauge holds the
      // true context fill between turns instead of flashing back to the char
      // estimate. The next turn (or the 2s poll) refreshes it.
      if (msg.prompt_tokens > 0) state._lastMsgPromptTokens = msg.prompt_tokens;
      // update the meta tooltip on the bubble we just rendered
      const rows = [...document.querySelectorAll("#chat-inner .bubble-row")];
      const lastRow = rows.reverse().find(r => r.querySelector(".bubble.agent"));
      if (lastRow) {
        const meta = lastRow.querySelector(".bubble-meta");
        if (meta && msg.tokens) {
          meta.title = `${msg.tokens.toLocaleString()} tokens${msg.prompt_tokens ? ` (prompt: ${msg.prompt_tokens.toLocaleString()})` : ""}`;
        }
        // Final-event bubble re-render: the streaming deltas can race or miss
        // a fence boundary, leaving the bubble blank when the model emitted
        // pure-code (one giant ```html```) or only-thinking-then-fence. The
        // `full` content is authoritative — re-render it now so the user sees
        // the result. splitThinking strips think tags; renderMarkdown produces
        // the code-card.
        const finalBubble = lastRow.querySelector(".bubble.agent");
        if (finalBubble) {
          const { content: finalContent } = splitThinking(full);
          if (finalContent.trim()) {
            const rendered = renderMarkdown(finalContent);
            if (rendered && rendered.trim()) {
              finalBubble.classList.remove("hidden");
              finalBubble.innerHTML = rendered;
              enhanceCodeBlocks(finalBubble);
              setTimeout(() => scrollToBottom(true), 50);
            } else {
              // All content was tool-call-stripped. Keep the bubble hidden.
              finalBubble.innerHTML = "";
              finalBubble.classList.add("hidden");
            }
          }
          updateThinkLine(lastRow, false);
        }
      }
      // parse companion files emitted alongside the primary html block
      // (```css path=style.css ..., ```js path=script.js ..., etc.)
      const files = parseMultiFileBlocks(full);
      if (Object.keys(files).length) {
        state.currentFiles = files;
        if (state.currentHtml) renderPreview();
      }
      renderCtxGauge();
      renderRegenerateChip();
    } else if (evt.type === "notice") {
      toast(evt.note || "", "info", 3000, "ctx-notice");
    } else if (evt.type === "breach") {
      // Cyber-range / CTF: a FLAG{...} was captured in a tool response = a
      // confirmed breach. Advance the attack-chain rail and surface a toast.
      attackRailBreach(row, evt.stage);
      // Inline SVG flag — no emoji, ever. Model-supplied fields are esc()'d
      // since toast() renders via innerHTML.
      const breachFlag = `<svg class="toast-ico" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z"/><line x1="4" y1="22" x2="4" y2="15"/></svg>`;
      toast(`${breachFlag} Breach — stage ${esc(String(evt.stage))}: ${esc(evt.flag)} (via ${esc(evt.via)})`, "ok", 6000);
    } else if (evt.type === "error") {
      bubble.innerHTML = `<span style="color: var(--danger)">error: ${esc(evt.error)}</span>`;
    }
  }

  // ---------- versions / preview ----------
  async function loadVersions() {
    if (!state.chatId) return;
    try {
      const r = await api(`/api/versions/${state.chatId}`);
      state.versions = r.versions || [];
    } catch { state.versions = []; }
    renderVersions();
    if (state.versions.length) {
      setActiveVersion(state.versions[state.versions.length - 1].id);
    } else {
      clearPreview();
    }
  }

  function renderVersions() {
    const bar = $("#version-bar");
    bar.innerHTML = "";
    if (!state.versions.length) {
      bar.innerHTML = `<span id="versions-empty" style="color:var(--fg-faint)">no versions yet</span><span class="spacer"></span>`;
      return;
    }
    const maxVisible = state._versionsExpanded ? Infinity : 8;
    const hiddenCount = Math.max(0, state.versions.length - maxVisible);
    const visible = state._versionsExpanded ? state.versions : state.versions.slice(-maxVisible);
    if (hiddenCount > 0) {
      const expand = document.createElement("button");
      expand.className = "version-chip";
      expand.innerHTML = `<span class="n">…</span><span style="opacity:.6">+${hiddenCount} more</span>`;
      expand.title = `Show all ${state.versions.length} versions`;
      expand.addEventListener("click", () => {
        state._versionsExpanded = true;
        renderVersions();
      });
      bar.appendChild(expand);
    }
    for (const v of visible) {
      const wrap = document.createElement("span");
      wrap.className = "version-wrap" + (v.id === state.activeVersion ? " active" : "");
      const chip = document.createElement("button");
      chip.className = "version-chip" + (v.id === state.activeVersion ? " active" : "");
      chip.innerHTML = `<span class="n">v${String(v.n).padStart(2, "0")}</span>${v.label ? `<span style="opacity:.6">· ${esc(v.label).slice(0, 32)}</span>` : ""}`;
      chip.title = `${v.id} · ${humanBytes(v.bytes)} · ${relTime(v.t)}`;
      chip.addEventListener("click", () => setActiveVersion(v.id));
      wrap.appendChild(chip);
      const rerun = document.createElement("button");
      rerun.className = "version-rerun";
      rerun.type = "button";
      rerun.title = "Re-run: reload this version into the preview";
      rerun.innerHTML = `<i class="ph ph-arrow-counter-clockwise"></i>`;
      rerun.addEventListener("click", (e) => {
        e.stopPropagation();
        setActiveVersion(v.id);
        toast(`v${String(v.n).padStart(2, "0")} re-loaded`, "info", 1600, "vrerun");
      });
      wrap.appendChild(rerun);
      bar.appendChild(wrap);
    }
    const spacer = document.createElement("span");
    spacer.className = "spacer";
    bar.appendChild(spacer);
  }

  async function setActiveVersion(vid) {
    state.activeVersion = vid;
    // user is opening a model-generated version → leave workspace-preview mode
    state.workspacePreview = null;
    const resp = await fetch(`/api/versions/${state.chatId}/${vid}`);
    let html = await resp.text();
    // safety net for older versions saved before the bridge unescape pass
    html = maybeUnescapeJsonFence(html);
    state.currentHtml = html;
    // companion-file map is per-turn; switching to a persisted version clears it
    state.currentFiles = {};
    const v = state.versions.find(x => x.id === vid);
    $("#preview-url").textContent = vid;
    $("#preview-meta").textContent = v ? `v${String(v.n).padStart(2, "0")} · ${relTime(v.t)}` : "—";
    $("#preview-size").textContent = humanBytes((html || "").length);
    renderPreview();
    renderVersions();
    // auto-open preview pane if collapsed (desktop only)
    if (app.classList.contains("preview-collapsed") && !isMobile()) {
      app.classList.remove("preview-collapsed");
    }
    // On mobile: inject a tappable artifact card in the chat
    if (isMobile() && html) {
      injectMobilePreviewCard({
        filename: v ? `v${String(v.n).padStart(2, "0")} preview` : "preview",
        size: html.length,
        html,
      });
    }
  }

  function clearPreview() {
    state.currentHtml = "";
    state.currentFiles = {};
    state.activeVersion = null;
    state.workspacePreview = null;
    $("#preview-url").textContent = "—";
    $("#preview-meta").textContent = "—";
    $("#preview-size").textContent = "—";
    $("#preview-frame").classList.add("hidden");
    $("#preview-stage").classList.add("hidden");
    $("#code-view").classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    $("#preview-empty").classList.remove("hidden");
    renderVersions();
  }

  function injectCspIfNeeded(html) {
    if (state.settings.allow_web_preview !== false) return html;
    // when Tailwind CDN is on we must relax CSP enough to load it, otherwise the script is blocked.
    const scriptSrc = state.settings.use_tailwind_cdn
      ? "'unsafe-inline' 'self' data: https://cdn.tailwindcss.com"
      : "'unsafe-inline' 'self' data:";
    const styleSrc = state.settings.use_tailwind_cdn
      ? "'unsafe-inline' 'self' data: https://cdn.tailwindcss.com"
      : "'unsafe-inline' 'self' data:";
    const connectSrc = state.settings.use_tailwind_cdn
      ? "'self' https://cdn.tailwindcss.com"
      : "'self'";
    const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'self' data: blob:; style-src ${styleSrc}; script-src ${scriptSrc}; img-src 'self' data: blob: https:; font-src 'self' data: https:; connect-src ${connectSrc};">`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + csp);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + csp + "</head>");
    return csp + html;
  }

  function injectTailwindIfNeeded(html) {
    if (!state.settings.use_tailwind_cdn) return html;
    // idempotent — bail out if the doc already pulls in Tailwind
    if (/cdn\.tailwindcss\.com/i.test(html)) return html;
    const tag = `<script src="https://cdn.tailwindcss.com"></script>`;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + tag);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + tag + "</head>");
    return tag + html;
  }

  // ---------- multi-file parsing ----------
  // the model may emit companion files via fenced blocks with an info string
  // like ```css path=style.css.  we collect them keyed by path so the preview
  // can inline them and Export Project can zip them unchanged.
  function parseMultiFileBlocks(text) {
    if (!text) return {};
    const out = {};
    // match fenced blocks with info strings containing path=<path>
    const re = /```([a-zA-Z0-9]+)?\s+([^\n`]*?path=([^\s`]+)[^\n`]*)\n([\s\S]*?)```/g;
    let m;
    while ((m = re.exec(text)) !== null) {
      const rawPath = (m[3] || "").trim().replace(/^["']|["']$/g, "");
      const body = m[4] || "";
      if (!rawPath) continue;
      // normalise + safety: strip leading slashes, no .. traversal, posix slashes only
      const safe = rawPath.replace(/\\/g, "/").replace(/^\/+/, "");
      if (safe.includes("..")) continue;
      out[safe] = maybeUnescapeJsonFence(body).replace(/\s+$/, "");
    }
    return out;
  }

  // Given the primary html and a map of extra files, return a single HTML
  // string suitable for the preview iframe with any linked local css/js
  // inlined.  Non-local hrefs are left alone.
  function inlineLocalAssets(html, files) {
    if (!html || !files || !Object.keys(files).length) return html;
    const keyOf = (href) => (href || "").replace(/\\/g, "/").replace(/^\.\//, "").replace(/^\/+/, "");
    // inline <link rel="stylesheet" href="..."> for local files
    html = html.replace(/<link\b([^>]*?)href=(["'])([^"']+)\2([^>]*)>/gi, (full, pre, _q, href, post) => {
      if (/rel\s*=\s*["']stylesheet/i.test(pre + post) || /rel\s*=\s*["']stylesheet/i.test(full)) {
        const key = keyOf(href);
        if (files[key] != null) return `<style data-inlined-from="${esc(key)}">\n${files[key]}\n</style>`;
      }
      return full;
    });
    // inline <script src="..."> for local files
    html = html.replace(/<script\b([^>]*?)src=(["'])([^"']+)\2([^>]*)><\/script>/gi, (full, pre, _q, src, post) => {
      const key = keyOf(src);
      if (files[key] != null) {
        const typeMatch = (pre + post).match(/type\s*=\s*(["'])([^"']+)\1/i);
        const typeAttr = typeMatch ? ` type="${esc(typeMatch[2])}"` : "";
        return `<script data-inlined-from="${esc(key)}"${typeAttr}>\n${files[key]}\n<\/script>`;
      }
      return full;
    });
    return html;
  }

  // ---------- console forwarder ----------
  // this script is injected into every preview so console.log / warn / error /
  // info and uncaught errors get posted back to the parent via postMessage.
  // the parent pushes them into the console-pane under the preview.
  const CONSOLE_FORWARDER = `<script>
(function(){
  if (window.__accConsoleWired) return;
  window.__accConsoleWired = true;
  var levels = ["log","info","warn","error","debug"];
  levels.forEach(function(lvl){
    var orig = console[lvl] && console[lvl].bind(console);
    console[lvl] = function(){
      try {
        var parts = [];
        for (var i = 0; i < arguments.length; i++) {
          var a = arguments[i];
          if (a instanceof Error) parts.push(a.stack || a.message);
          else if (typeof a === "object") { try { parts.push(JSON.stringify(a)); } catch(_){ parts.push(String(a)); } }
          else parts.push(String(a));
        }
        parent.postMessage({ __acc: "console", level: lvl, text: parts.join(" ") }, "*");
      } catch(_){}
      if (orig) orig.apply(console, arguments);
    };
  });
  window.addEventListener("error", function(e){
    try { parent.postMessage({ __acc: "console", level: "error", text: (e.message||"error") + (e.filename?(" ("+e.filename+":"+e.lineno+")"):"") }, "*"); } catch(_){}
  });
  window.addEventListener("unhandledrejection", function(e){
    try { parent.postMessage({ __acc: "console", level: "error", text: "unhandled rejection: " + ((e.reason && (e.reason.stack||e.reason.message))||String(e.reason)) }, "*"); } catch(_){}
  });
})();
<\/script>`;

  function injectConsoleForwarder(html) {
    if (!html) return html;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + CONSOLE_FORWARDER);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + CONSOLE_FORWARDER + "</head>");
    return CONSOLE_FORWARDER + html;
  }

  function pushConsoleLog(level, text) {
    const entry = { level, text: String(text || ""), t: Date.now() };
    state.consoleLogs.push(entry);
    if (state.consoleLogs.length > 400) state.consoleLogs.splice(0, state.consoleLogs.length - 400);
    const body = document.getElementById("console-body");
    if (!body) return;
    const row = document.createElement("div");
    row.className = `c-row c-${level}`;
    row.textContent = entry.text;
    body.appendChild(row);
    body.scrollTop = body.scrollHeight;
  }

  function clearConsole() {
    state.consoleLogs = [];
    const body = document.getElementById("console-body");
    if (body) body.innerHTML = "";
  }

  // single global listener — receives postMessage from *any* preview iframe
  window.addEventListener("message", (e) => {
    const d = e && e.data;
    if (!d || d.__acc !== "console") return;
    pushConsoleLog(d.level || "log", d.text || "");
  });

  // ---------- viewport presets ----------
  const VIEWPORT_WIDTHS = { full: null, desktop: 1280, tablet: 820, mobile: 390 };

  function applyViewport(vp) {
    state.viewport = vp;
    const stage = $("#preview-stage");
    const frame = $("#preview-frame");
    if (!stage || !frame) return;
    const w = VIEWPORT_WIDTHS[vp];
    if (w) {
      stage.classList.add("vp-constrained");
      frame.style.maxWidth = w + "px";
      frame.style.marginInline = "auto";
    } else {
      stage.classList.remove("vp-constrained");
      frame.style.maxWidth = "";
      frame.style.marginInline = "";
    }
    $$(".vp-btn").forEach(b => b.classList.toggle("active", b.dataset.vp === vp));
  }

  function buildPreviewHtml() {
    let html = inlineLocalAssets(state.currentHtml, state.currentFiles);
    html = injectTailwindIfNeeded(html);
    html = injectConsoleForwarder(html);
    html = injectCspIfNeeded(html);
    return html;
  }

  // ---------- workspace file actions ----------
  // urlsafe-base64 encode the workspace root path so the bridge can recover
  // it from a path segment (no query string → relative-asset URLs in served
  // HTML resolve correctly through the same /api/wsfs/<token>/... endpoint).
  function wsRootToken(root) {
    // unicode-safe utf8 → base64, then urlsafe (+→-, /→_) and strip padding
    const utf8 = unescape(encodeURIComponent(root));
    return btoa(utf8).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function wsFileUrl(root, rel) {
    // encode each path segment but keep the slashes as separators
    const encRel = (rel || "").replace(/\\/g, "/").split("/").map(encodeURIComponent).join("/");
    return `/api/wsfs/${wsRootToken(root)}/${encRel}`;
  }

  // Stream an existing .html from a workspace folder into the preview iframe.
  // Uses iframe `src` (not srcdoc) so relative asset URLs resolve back through
  // /api/wsfs/<token>/... — the bridge enforces strict containment so the
  // page can only ever load assets that live inside the same workspace root.
  function previewWorkspaceHtml(root, rel, displayName) {
    if (!root || !rel) return;
    if (isMobile()) {
      window.open(wsFileUrl(root, rel), "_blank");
      return;
    }
    // make sure preview pane is open
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    // remember BEFORE rendering so view-toggle / refresh handlers can detect mode
    state.workspacePreview = { root, rel, name: displayName || rel };
    state.currentHtml = "";  // ensure model-output flow doesn't fight us
    state.view = "preview";
    document.getElementById("btn-view-preview")?.classList.add("active");
    document.getElementById("btn-view-code")?.classList.remove("active");
    renderWorkspacePreview();
  }

  // Renders the current workspace-preview state into the right pane.
  // Honours state.view so the user can toggle Preview ↔ Code on workspace
  // files exactly like they can on model-generated HTML.
  async function renderWorkspacePreview() {
    const wp = state.workspacePreview;
    if (!wp) return;
    document.getElementById("preview-empty")?.classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = wp.name || wp.rel;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `workspace · ${wp.name || wp.rel}`;

    if (state.view === "code") {
      // hide iframe stage, show code-view, fetch source as text
      document.getElementById("preview-stage")?.classList.add("hidden");
      const c = document.getElementById("code-view");
      if (!c) return;
      c.classList.remove("hidden");
      c.textContent = "loading…";
      try {
        const r = await fetch(wsFileUrl(wp.root, wp.rel));
        if (!r.ok) {
          const j = await r.json().catch(() => ({}));
          c.textContent = `error: ${j.error || r.statusText}`;
          return;
        }
        const txt = await r.text();
        c.innerHTML = highlightHTML(txt);
      } catch (e) {
        c.textContent = `error: ${e.message || e}`;
      }
      return;
    }

    // preview mode — recreate iframe pointing at the path-style endpoint
    document.getElementById("code-view")?.classList.add("hidden");
    const stage = document.getElementById("preview-stage");
    if (stage) stage.classList.remove("hidden");
    const old = document.getElementById("preview-frame");
    const fresh = document.createElement("iframe");
    fresh.id = "preview-frame";
    fresh.className = "preview-frame";
    fresh.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups allow-same-origin");
    fresh.src = wsFileUrl(wp.root, wp.rel);
    old?.replaceWith(fresh);
  }

  // Run a server-side Python syntax check on a workspace .py file. Renders
  // the result in a dedicated panel in the bottom pane: a pass/fail banner
  // (SVG icon) + the source with the error line highlighted. Never executes it.
  async function runPythonCheck(root, rel, displayName) {
    if (!root || !rel) return;
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    const banner = document.getElementById("pycheck-banner");
    const codeEl = document.getElementById("pycheck-code")?.querySelector("code");
    if (!banner || !codeEl) return;

    banner.className = "pycheck-banner pending";
    banner.textContent = `checking ${displayName || rel}…`;
    codeEl.textContent = "";
    // Ensure the inner pycheck-pane is visible (other preview functions hide it)
    document.getElementById("pycheck-pane")?.classList.remove("hidden");
    activateTerminalTab("pycheck");

    let res;
    try {
      const r = await fetch("/api/py-check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root, path: rel }),
      });
      res = await r.json();
    } catch (e) {
      banner.className = "pycheck-banner err";
      banner.textContent = `request failed: ${e.message || e}`;
      return;
    }
    if (res.error) {
      banner.className = "pycheck-banner err";
      banner.textContent = res.error;
      return;
    }

    // also fetch the actual source to show under the banner
    let srcText = "";
    try {
      const sr = await fetch(wsFileUrl(root, rel));
      if (sr.ok) srcText = await sr.text();
    } catch {}

    const lines = srcText.split("\n");
    const errLine = res.ok ? -1 : Math.max(1, parseInt(res.line || 0, 10));
    // Run the whole source through the Python tokenizer in ONE pass so
    // multi-line strings / docstrings keep their string coloring continuous,
    // then split on \n so each rendered line still gets its own block (line
    // numbers + error highlighting). Block-level .pyc-line spans give us the
    // newline; the join("") avoids a literal \n inside <pre> that would
    // double-space every line (the original bug from the screenshot).
    const fullHtml = highlightCode(srcText, "py");
    const htmlLines = splitHighlightedLines(fullHtml);
    const numbered = htmlLines.map((html, i) => {
      const n = i + 1;
      const isErr = !res.ok && n === errLine;
      const cls = isErr ? "pyc-line err" : "pyc-line";
      return `<span class="${cls}"><span class="pyc-num">${String(n).padStart(4, " ")}</span> ${html}</span>`;
    }).join("");
    codeEl.innerHTML = numbered;

    if (res.ok) {
      banner.className = "pycheck-banner ok";
      banner.innerHTML = `${SVG_PYCHECK} <strong>syntax OK</strong> · ${esc(res.file || displayName || rel)} · ${res.lines} lines`;
    } else {
      const at = res.line ? ` at line ${res.line}${res.col ? `, col ${res.col}` : ""}` : "";
      banner.className = "pycheck-banner err";
      banner.innerHTML = `<strong>SyntaxError</strong>${esc(at)}: ${esc(res.msg || "unknown")} · ${esc(res.file || displayName || rel)}`;
      // scroll to the error line
      requestAnimationFrame(() => {
        const errEl = document.getElementById("pycheck-pane")?.querySelector(".pyc-line.err");
        if (errEl) errEl.scrollIntoView({ block: "center", behavior: "smooth" });
      });
    }
  }

  // Hide every right-pane mode and lazy-create or return the doc-preview pane
  // shared by the markdown renderer and the formatted-source view. The pane
  // lives next to .pycheck-pane inside .preview-body — same layout, same
  // banner-on-top + scrollable body convention.
  function _ensureDocPreviewPane() {
    const app = document.getElementById("app");
    if (app && app.classList.contains("preview-collapsed")) {
      app.classList.remove("preview-collapsed");
    }
    document.getElementById("preview-empty")?.classList.add("hidden");
    document.getElementById("preview-stage")?.classList.add("hidden");
    document.getElementById("code-view")?.classList.add("hidden");
    document.getElementById("pycheck-pane")?.classList.add("hidden");
    document.getElementById("doc-preview-pane")?.classList.add("hidden");
    let pane = document.getElementById("doc-preview-pane");
    if (!pane) {
      pane = document.createElement("div");
      pane.id = "doc-preview-pane";
      pane.className = "doc-preview-pane";
      pane.innerHTML = `
        <div class="doc-preview-banner" id="doc-preview-banner"></div>
        <div class="doc-preview-body" id="doc-preview-body"></div>`;
      const pBody = document.getElementById("preview-body");
      if (pBody) {
        const resizer = document.getElementById("preview-v-resizer");
        pBody.insertBefore(pane, resizer);
      }
    }
    pane.classList.remove("hidden");
    return pane;
  }

  // Render a workspace .md file as formatted Markdown in the preview pane.
  // Reuses the same renderMarkdown() that powers chat bubbles, so headings,
  // lists, tables, code fences (with syntax highlighting), inline code, links,
  // and bold/italic all work identically. No iframe — the markdown body is
  // injected straight into the pane and scoped via .doc-preview-body so the
  // app's own CSS doesn't bleed into it weirdly.
  async function previewWorkspaceMarkdown(root, rel, displayName) {
    if (!root || !rel) return;
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.remove("doc-source-mode");
    banner.className = "doc-preview-banner pending";
    banner.innerHTML = `${SVG_BOOK} <strong>${esc(displayName || rel)}</strong> · loading…`;
    body.innerHTML = "";
    try {
      const r = await fetch(wsFileUrl(root, rel));
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        banner.className = "doc-preview-banner err";
        banner.textContent = `error: ${j.error || r.statusText}`;
        return;
      }
      const txt = await r.text();
      body.innerHTML = `<div class="doc-md">${renderMarkdown(txt)}</div>`;
      banner.className = "doc-preview-banner ok";
      banner.innerHTML = `${SVG_BOOK} <strong>${esc(displayName || rel)}</strong> · markdown · ${txt.split("\n").length} lines`;
    } catch (e) {
      banner.className = "doc-preview-banner err";
      banner.textContent = `error: ${e.message || e}`;
    }
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `md · ${displayName || rel}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `markdown · ${displayName || rel}`;
  }

  // Render a workspace text/code file as syntax-highlighted source in the
  // preview pane. Uses the same single-pass tokenizer as chat code fences,
  // dispatched by extension via SOURCE_VIEW_LANGS. Plain-text files (.txt,
  // .toml, .ini) fall through to escaped monospace with no token coloring.
  async function previewWorkspaceSource(root, rel, displayName) {
    if (!root || !rel) return;
    const pane = _ensureDocPreviewPane();
    const banner = pane.querySelector("#doc-preview-banner");
    const body = pane.querySelector("#doc-preview-body");
    pane.classList.add("doc-source-mode");
    banner.className = "doc-preview-banner pending";
    banner.innerHTML = `${SVG_EYE} <strong>${esc(displayName || rel)}</strong> · loading…`;
    body.innerHTML = "";
    try {
      const r = await fetch(wsFileUrl(root, rel));
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        banner.className = "doc-preview-banner err";
        banner.textContent = `error: ${j.error || r.statusText}`;
        return;
      }
      const txt = await r.text();
      const lang = SOURCE_VIEW_LANGS[fileExt(displayName || rel)] || "text";
      const fullHtml = highlightCode(txt, lang);
      const htmlLines = splitHighlightedLines(fullHtml);
      const numbered = htmlLines.map((html, i) => {
        const n = i + 1;
        return `<span class="pyc-line"><span class="pyc-num">${String(n).padStart(4, " ")}</span> ${html}</span>`;
      }).join("");
      body.innerHTML = `<pre class="pycheck-code"><code>${numbered}</code></pre>`;
      banner.className = "doc-preview-banner ok";
      banner.innerHTML = `${SVG_EYE} <strong>${esc(displayName || rel)}</strong> · ${esc(lang)} · ${htmlLines.length} lines`;
    } catch (e) {
      banner.className = "doc-preview-banner err";
      banner.textContent = `error: ${e.message || e}`;
    }
    state.workspacePreview = null;
    const pill = document.getElementById("preview-url");
    if (pill) pill.textContent = `source · ${displayName || rel}`;
    const meta = document.getElementById("preview-meta");
    if (meta) meta.textContent = `source · ${displayName || rel}`;
  }

  function renderPreview() {
    if (!state.currentHtml) { clearPreview(); return; }
    $("#preview-empty").classList.add("hidden");
    if (state.view === "preview") {
      $("#code-view").classList.add("hidden");
      $("#preview-stage").classList.remove("hidden");
      // recreate iframe each time we switch back — srcdoc on a hidden iframe
      // can end up blank in some browsers. Cheap and always correct.
      const old = $("#preview-frame");
      const fresh = document.createElement("iframe");
      fresh.id = "preview-frame";
      fresh.className = "preview-frame";
      // allow-same-origin so the page can read its own localStorage / cookies
      // (theme toggles commonly do `localStorage.getItem("theme")`, which
      // throws DOMException in an opaque-origin srcdoc and the page silently
      // falls back to its light-mode default — visible as a white iframe even
      // though "open in new tab" renders the same HTML correctly because the
      // tab gets a real origin). The HTML in this iframe is generated by the
      // local agent on the user's own machine, not arbitrary web input, so
      // the scripts+same-origin combination is acceptable here.
      fresh.setAttribute("sandbox", "allow-scripts allow-forms allow-modals allow-popups allow-same-origin");
      // tabindex makes the iframe element itself focusable, which is what
      // lets contentWindow.focus() actually take effect from the parent.
      fresh.setAttribute("tabindex", "0");
      fresh.srcdoc = buildPreviewHtml();
      // Auto-focus on hover. Without this, a fresh iframe doesn't own the
      // wheel events — they bubble to the parent doc and the user has to
      // click inside the preview before scroll-wheel works. Hovering with
      // the mouse is the natural "I'm about to interact with this" signal.
      fresh.addEventListener("mouseenter", () => {
        try { fresh.contentWindow && fresh.contentWindow.focus(); } catch {}
      });
      // Belt & braces: also focus once the document inside loads, so the
      // first scroll attempt right after a new render works without needing
      // a hover first.
      fresh.addEventListener("load", () => {
        try { fresh.contentWindow && fresh.contentWindow.focus(); } catch {}
      });
      old.replaceWith(fresh);
    } else {
      $("#preview-stage").classList.add("hidden");
      const c = $("#code-view");
      c.classList.remove("hidden");
      c.innerHTML = highlightHTML(state.currentHtml);
    }
  }

  // ---------- preview: screenshot / export / review-UI ----------
  function safeSlug(s, fallback) {
    const t = (s || "").toString().toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
    return t || fallback;
  }

  function currentProjectBase() {
    const v = state.versions.find(x => x.id === state.activeVersion);
    const title = (state.chats?.find?.(x => x.id === state.chatId)?.title) || "";
    return safeSlug(title || (v ? `version-${v.n}` : "preview"), "preview");
  }

  async function captureIframePng({ scale = 1 } = {}) {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return null;
    }
    if (typeof window.html2canvas !== "function") {
      toast("Screenshot library hasn't loaded yet — try again in a second.", "warn", 2500);
      return null;
    }
    // srcdoc iframes inherit the parent origin, so contentDocument is accessible
    const frame = $("#preview-frame");
    const doc = frame && frame.contentDocument;
    const body = doc && doc.body;
    if (!body) {
      toast("Preview frame isn't ready.", "warn", 2500);
      return null;
    }
    try {
      const canvas = await window.html2canvas(body, {
        backgroundColor: getComputedStyle(body).backgroundColor || "#ffffff",
        useCORS: true,
        allowTaint: true,
        scale,
        logging: false,
        windowWidth: doc.documentElement.scrollWidth,
        windowHeight: doc.documentElement.scrollHeight,
      });
      return canvas;
    } catch (e) {
      toast(`Screenshot failed: ${e.message || e}`, "err", 3500);
      return null;
    }
  }

  async function screenshotPreview() {
    const canvas = await captureIframePng();
    if (!canvas) return;
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${currentProjectBase()}-${Date.now()}.png`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Screenshot saved", "ok", 2000, "ss");
    }, "image/png");
  }

  async function reviewUiAttach() {
    const canvas = await captureIframePng();
    if (!canvas) return;
    const dataUrl = canvas.toDataURL("image/png");
    state.pendingImages.push({ dataUrl, name: `ui-${Date.now()}.png` });
    renderImageTray();
    const ta = $("#composer-input");
    if (ta) {
      const existing = ta.value.trim();
      if (!/review this ui/i.test(existing)) {
        ta.value = existing ? `${existing}\n\nReview this UI — note what feels off and suggest concrete fixes.`
                            : "Review this UI — note what feels off and suggest concrete fixes.";
      }
      autoResize(ta);
      ta.focus();
    }
    toast("Preview attached — press Send to have the model review it.", "ok", 3000, "review");
  }

  async function saveSnapshot() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    const base = currentProjectBase();
    const html = buildPreviewHtml();  // persist what the user actually sees
    const resp = await fetch("/api/snapshots", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: base, html }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      toast(`Snapshot failed: ${data.error || resp.status}`, "err", 3500);
      return;
    }
    toast(`Saved: ${data.name}`, "ok", 2600, "snap");
  }

  // Save the current preview HTML to a workspace folder. No model call —
  // we POST the bytes directly to the bridge, which validates the root is
  // a configured workspace and writes the file. The whole point is the
  // user shouldn't have to ask the agent to regenerate HTML it already
  // wrote (and that the bridge already has on disk as a version file).
  async function saveToWorkspace() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    // 1. Get the current workspace folders.
    let folders = [];
    try {
      const ws = await api("/api/workspace");
      folders = (ws && ws.folders) || [];
    } catch {
      toast("Couldn't load workspace folders.", "err", 3000);
      return;
    }
    if (!folders.length) {
      toast("No workspace folders configured. Add one in the Workspace panel first.", "warn", 4000);
      return;
    }
    // 2. Pick a root. Single folder = use it; multiple = prompt with a
    //    numbered list (kept dead simple — no modal infra needed).
    let root;
    if (folders.length === 1) {
      root = folders[0];
    } else {
      const list = folders.map((f, i) => `${i + 1}. ${f}`).join("\n");
      const pick = window.prompt(`Save to which workspace folder?\n\n${list}\n\nEnter 1-${folders.length}:`, "1");
      if (pick == null) return;
      const idx = parseInt(pick, 10) - 1;
      if (!(idx >= 0 && idx < folders.length)) {
        toast("Invalid choice.", "warn", 2200);
        return;
      }
      root = folders[idx];
    }
    // 3. Filename — default to project slug + .html.
    const defaultName = `${currentProjectBase()}.html`;
    const filename = window.prompt(`Save as (in ${root}):`, defaultName);
    if (filename == null || !filename.trim()) return;
    const html = buildPreviewHtml();
    // 4. POST. Handle 409 (file exists) by re-asking with overwrite=true.
    const send = async (overwrite) => fetch("/api/save-to-workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ root, filename: filename.trim(), html, overwrite }),
    });
    let resp = await send(false);
    if (resp.status === 409) {
      if (!confirm(`"${filename.trim()}" already exists in ${root}. Overwrite?`)) return;
      resp = await send(true);
    }
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      toast(`Save failed: ${data.error || resp.status}`, "err", 3500);
      return;
    }
    toast(`Saved to ${data.path}`, "ok", 3000, "ws-save");
  }

  async function copyPreviewAsDataUrl() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    const html = buildPreviewHtml();
    const b64 = btoa(unescape(encodeURIComponent(html)));
    const url = `data:text/html;charset=utf-8;base64,${b64}`;
    try {
      await navigator.clipboard.writeText(url);
      toast(`Copied data URL (${Math.round(url.length / 1024)}KB)`, "ok", 2400, "dataurl");
    } catch {
      // clipboard may be blocked — fall back to a throwaway prompt
      try { window.prompt("Copy this data URL:", url); } catch {}
    }
  }

  function toggleConsolePane(force) {
    const want = typeof force === "boolean" ? force : !state.consoleOpen;
    state.consoleOpen = want;
    const pane = $("#console-pane");
    if (!pane) return;
    pane.classList.toggle("hidden", !want);
    $("#btn-toggle-console")?.classList.toggle("active", want);
  }

  async function exportProjectZip() {
    if (!state.currentHtml) {
      toast("Nothing in the preview yet.", "warn", 2200);
      return;
    }
    const base = currentProjectBase();
    const files = state.currentFiles || {};
    const hasCompanions = Object.keys(files).length > 0;

    // single-file path: just download the html
    if (!hasCompanions) {
      const blob = new Blob([state.currentHtml], { type: "text/html" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${base}.html`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Downloaded HTML", "ok", 2000, "exp");
      return;
    }

    if (typeof window.JSZip !== "function") {
      toast("Zip library hasn't loaded yet — try again in a second.", "warn", 2500);
      return;
    }

    const zip = new window.JSZip();
    // if the model also emitted its own index.html path, prefer that verbatim;
    // otherwise write state.currentHtml as index.html
    if (!files["index.html"]) zip.file("index.html", state.currentHtml);
    for (const [path, body] of Object.entries(files)) zip.file(path, body);
    const blob = await zip.generateAsync({ type: "blob" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${base}.zip`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast(`Exported ${Object.keys(files).length + (files["index.html"] ? 0 : 1)} files`, "ok", 2400, "exp");
  }

  // ---------- workspace ----------
  // ---- workspace file tree ----
  const FILE_ICON = {
    // scripts
    js: "ph-file-js", jsx: "ph-file-js", ts: "ph-file-ts", tsx: "ph-file-ts",
    py: "ph-file-py", rb: "ph-file-code", go: "ph-file-code", rs: "ph-file-rs",
    java: "ph-file-code", c: "ph-file-c", cpp: "ph-file-cpp", h: "ph-file-c",
    cs: "ph-file-cs", php: "ph-file-code", sh: "ph-terminal-window",
    ps1: "ph-terminal-window", bat: "ph-terminal-window", lua: "ph-file-code",
    // web
    html: "ph-file-html", htm: "ph-file-html", css: "ph-file-css",
    scss: "ph-file-css", sass: "ph-file-css", less: "ph-file-css",
    vue: "ph-file-vue", svelte: "ph-file-code",
    // data
    json: "ph-brackets-curly", yaml: "ph-brackets-angle", yml: "ph-brackets-angle",
    xml: "ph-brackets-angle", toml: "ph-brackets-angle", ini: "ph-brackets-angle",
    csv: "ph-table", tsv: "ph-table", sql: "ph-database", db: "ph-database",
    sqlite: "ph-database",
    // docs
    md: "ph-file-md", mdx: "ph-file-md", txt: "ph-file-text", rtf: "ph-file-text",
    pdf: "ph-file-pdf", doc: "ph-file-doc", docx: "ph-file-doc",
    xls: "ph-file-xls", xlsx: "ph-file-xls", ppt: "ph-file-ppt", pptx: "ph-file-ppt",
    // media
    png: "ph-file-image", jpg: "ph-file-image", jpeg: "ph-file-image",
    gif: "ph-file-image", webp: "ph-file-image", svg: "ph-file-svg",
    ico: "ph-file-image", bmp: "ph-file-image", avif: "ph-file-image",
    mp3: "ph-file-audio", wav: "ph-file-audio", flac: "ph-file-audio",
    ogg: "ph-file-audio", m4a: "ph-file-audio",
    mp4: "ph-file-video", mov: "ph-file-video", mkv: "ph-file-video",
    webm: "ph-file-video", avi: "ph-file-video",
    // archives
    zip: "ph-file-zip", rar: "ph-file-zip", "7z": "ph-file-zip",
    tar: "ph-file-zip", gz: "ph-file-zip", bz2: "ph-file-zip",
    // config
    env: "ph-key", lock: "ph-lock-simple", log: "ph-article",
    gitignore: "ph-git-branch", dockerfile: "ph-cube",
  };
  function fileIconFor(name, ext) {
    const lower = (name || "").toLowerCase();
    if (lower === "dockerfile") return "ph-cube";
    if (lower === "makefile") return "ph-hammer";
    if (lower === "license" || lower === "license.md") return "ph-scales";
    if (lower === "readme" || lower === "readme.md") return "ph-book-open-text";
    if (lower.startsWith(".git")) return "ph-git-branch";
    return FILE_ICON[ext] || "ph-file";
  }
  function folderLeafName(path) {
    return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
  }
  async function fetchFolderListing(path) {
    try {
      const r = await api(`/api/list-folder?path=${encodeURIComponent(path)}`);
      if (r.error) throw new Error(r.error);
      return r.entries || [];
    } catch (e) {
      return { _error: e.message || String(e) };
    }
  }
  // SVG icons for inline file actions. Phosphor's <i class="ph"> would work
  // but inline SVG keeps the tree row from stealing the icon font's vertical
  // metrics, and the user explicitly asked for SVG over emoji/icon-font.
  const SVG_LIGHTNING = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>';
  const SVG_PYCHECK  = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
  const SVG_BOOK     = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>';
  const SVG_EYE      = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

  // Map file extension → highlightCode() language id. Determines which
  // entries get an "eye" view-source button in the tree and how the
  // formatted-source preview tokenizes them. Markdown has its own button
  // (book icon) and HTML has the iframe lightning bolt — neither belongs here.
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
  function fileExt(name) {
    const m = (name || "").toLowerCase().match(/\.([a-z0-9]+)$/);
    return m ? m[1] : "";
  }
  function isMarkdownFile(name) {
    const e = fileExt(name);
    return e === "md" || e === "markdown" || e === "mdown" || e === "mkd";
  }
  function isSourceViewable(name) {
    return Object.prototype.hasOwnProperty.call(SOURCE_VIEW_LANGS, fileExt(name));
  }

  // relative path from a workspace root → entry.path. `entry.path` is the
  // absolute on-disk path returned by /api/list-folder.
  function relPathFromRoot(root, abs) {
    const r = (root || "").replace(/\\/g, "/").replace(/\/+$/, "");
    const a = (abs  || "").replace(/\\/g, "/");
    if (r && a.toLowerCase().startsWith(r.toLowerCase() + "/")) return a.slice(r.length + 1);
    return a; // fallback — bridge will still validate
  }

  function isPreviewableHtml(name) {
    const n = (name || "").toLowerCase();
    return n.endsWith(".html") || n.endsWith(".htm");
  }
  function isPythonFile(name) {
    return (name || "").toLowerCase().endsWith(".py");
  }

  function renderTreeNode(entry, depth, rootFolder) {
    const node = document.createElement("div");
    node.className = entry.is_dir ? "tree-node tree-dir" : "tree-node tree-file";
    node.style.setProperty("--depth", depth);
    const icon = entry.is_dir ? "ph-folder" : fileIconFor(entry.name, entry.ext);
    const chev = entry.is_dir ? `<i class="ph ph-caret-right tree-chev"></i>` : `<span class="tree-chev-spacer"></span>`;
    // file-type-specific inline action buttons (SVG, not emoji)
    let actions = "";
    if (!entry.is_dir) {
      if (isPreviewableHtml(entry.name)) {
        actions += `<button class="tree-action ws-preview-html" title="Preview this HTML in the panel">${SVG_LIGHTNING}</button>`;
      }
      if (isPythonFile(entry.name)) {
        actions += `<button class="tree-action ws-pycheck" title="Check Python syntax">${SVG_PYCHECK}</button>`;
      }
      if (isMarkdownFile(entry.name)) {
        actions += `<button class="tree-action ws-preview-md" title="Render Markdown in the panel">${SVG_BOOK}</button>`;
      } else if (isSourceViewable(entry.name) && !isPythonFile(entry.name)) {
        // .py files already get a syntax-checker that shows highlighted
        // source — no need for a duplicate "view source" button on those.
        actions += `<button class="tree-action ws-preview-source" title="View formatted source in the panel">${SVG_EYE}</button>`;
      }
    }
    const hasBadge = state.touchedFiles.has(entry.path);
    const badgeHtml = hasBadge ? `<span class="ws-badge-m" title="Modified by agent">M</span>` : "";
    node.innerHTML = `
      <div class="tree-row" title="${esc(entry.path)}">
        ${chev}
        <i class="ph ${icon} tree-icon"></i>
        <span class="tree-name">${esc(entry.name)}</span>
        ${badgeHtml}
        ${actions ? `<span class="tree-actions">${actions}</span>` : ""}
      </div>
      ${entry.is_dir ? `<div class="tree-children" hidden></div>` : ""}`;
    if (entry.is_dir) {
      const rowEl = node.querySelector(".tree-row");
      const kids = node.querySelector(".tree-children");
      let loaded = false;
      rowEl.addEventListener("click", async (e) => {
        e.stopPropagation();
        const expanded = !kids.hasAttribute("hidden");
        if (expanded) {
          kids.setAttribute("hidden", "");
          node.classList.remove("open");
          return;
        }
        node.classList.add("open");
        kids.removeAttribute("hidden");
        if (!loaded) {
          kids.innerHTML = `<div class="tree-loading" style="--depth:${depth + 1}">loading…</div>`;
          const entries = await fetchFolderListing(entry.path);
          kids.innerHTML = "";
          if (entries._error) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            kids.innerHTML = `<div class="tree-empty" style="--depth:${depth + 1}">empty</div>`;
          } else {
            for (const child of entries) kids.appendChild(renderTreeNode(child, depth + 1, rootFolder));
          }
          loaded = true;
        }
      });
    } else {
      // wire file-action buttons; both stop propagation so clicking them
      // doesn't also toggle the row.
      const previewBtn = node.querySelector(".ws-preview-html");
      if (previewBtn) {
        previewBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceHtml(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const pyBtn = node.querySelector(".ws-pycheck");
      if (pyBtn) {
        pyBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          runPythonCheck(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const mdBtn = node.querySelector(".ws-preview-md");
      if (mdBtn) {
        mdBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceMarkdown(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
      const srcBtn = node.querySelector(".ws-preview-source");
      if (srcBtn) {
        srcBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          e.preventDefault();
          previewWorkspaceSource(rootFolder, relPathFromRoot(rootFolder, entry.path), entry.name);
        });
      }
    }
    return node;
  }

  function renderWorkspace() {
    const wrap = $("#ws-list");
    wrap.innerHTML = "";
    if (!state.workspace.folders.length) {
      wrap.innerHTML = `<div style="padding: 10px 12px; font-size: 11px; color: var(--fg-faint);">no folders. add one to let the agent read/write files.</div>`;
      return;
    }
    for (const f of state.workspace.folders) {
      const wrapper = document.createElement("div");
      wrapper.className = "ws-root";

      const header = document.createElement("div");
      header.className = "ws-folder";
      header.innerHTML = `
        <i class="ph ph-caret-right ws-chev"></i>
        <i class="ph ph-folder"></i>
        <span class="path" title="${esc(f)}">${esc(folderLeafName(f))}</span>
        <button class="rm" title="Remove"><i class="ph ph-x"></i></button>`;
      wrapper.appendChild(header);

      const tree = document.createElement("div");
      tree.className = "ws-tree";
      tree.hidden = true;
      wrapper.appendChild(tree);

      let loaded = false;
      header.querySelector(".rm").addEventListener("click", async (e) => {
        e.stopPropagation();
        const next = state.workspace.folders.filter(x => x !== f);
        await api("/api/workspace", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ folders: next }),
        });
        state.workspace.folders = next;
        renderWorkspace();
      });
      header.addEventListener("click", async () => {
        const wasOpen = wrapper.classList.toggle("open");
        tree.hidden = !wasOpen;
        if (wasOpen && !loaded) {
          tree.innerHTML = `<div class="tree-loading" style="--depth:1">loading…</div>`;
          const entries = await fetchFolderListing(f);
          tree.innerHTML = "";
          if (entries._error) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">${esc(entries._error)}</div>`;
          } else if (!entries.length) {
            tree.innerHTML = `<div class="tree-empty" style="--depth:1">empty</div>`;
          } else {
            for (const child of entries) tree.appendChild(renderTreeNode(child, 1, f));
          }
          loaded = true;
        }
      });

      wrap.appendChild(wrapper);
    }
  }

  async function addWorkspaceFolder() {
    const inp = $("#ws-input");
    const v = inp.value.trim();
    if (!v) return;
    const next = Array.from(new Set([...state.workspace.folders, v]));
    const r = await api("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folders: next }),
    });
    state.workspace = r;
    inp.value = "";
    $("#ws-add").classList.add("hidden");
    renderWorkspace();
  }

  // ---------- approvals ----------
  // Inline SVG icons used inside approval cards. Kept tiny and self-contained
  // so the approval system has no Phosphor-icon-font dependency — even if that
  // font fails to load, the card still reads clearly.
  const APPR_SVG = {
    shield: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 4.8-1.5 8-5.5 8-10V5z"/><path d="m9 12 2 2 4-4"/></svg>',
    file:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><polyline points="14 3 14 9 20 9"/></svg>',
    hash:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="4" y1="9" x2="20" y2="9"/><line x1="4" y1="15" x2="20" y2="15"/><line x1="10" y1="3" x2="8" y2="21"/><line x1="16" y1="3" x2="14" y2="21"/></svg>',
    pencil: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
    folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    trash:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6 17.5 20a2 2 0 0 1-2 1.8h-7a2 2 0 0 1-2-1.8L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>',
    monitor:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
    cursor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m3 3 7 19 2.5-8.5L21 11z"/></svg>',
    keyboard:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg>',
    text:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 7 4 4 20 4 20 7"/><line x1="9" y1="20" x2="15" y2="20"/><line x1="12" y1="4" x2="12" y2="20"/></svg>',
    play:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
    terminal:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>',
    globe:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/></svg>',
    info:   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>',
    check:  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>',
    x:      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    chevron:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>',
  };

  // Per-kind metadata: header subtitle (what the action does in plain English)
  // and the body of the "What does this mean?" info bubble. Centralized so the
  // wording stays consistent across kinds.
  const APPR_KIND_META = {
    write_file:        { sub: "The model wants to write to a file on your system.",        info: "This will create or update the file at the specified location with the provided content." },
    edit_file:         { sub: "The model wants to edit a file on your system.",            info: "This will apply the listed search-and-replace edits in place. The previous content is captured in version history." },
    delete:            { sub: "The model wants to delete something from your filesystem.", info: "This is permanent — once approved, the file or folder cannot be restored from inside Accuretta." },
    powershell:        { sub: "The model wants to run a PowerShell command on your machine.", info: "PowerShell commands run with your current user privileges. Read the command below carefully before approving." },
    launch:            { sub: "The model wants to launch a program.",                       info: "This starts the program with your user privileges. Once running, it can do anything you can do." },
    network_snapshot:  { sub: "The model wants to read your active network connections.",   info: "Read-only: no packets are sent. The model will see open ports, owning processes, and your DNS cache." },
    "desktop.launch":  { sub: "The model wants to launch a desktop app.",                    info: "Only allowlisted apps (Settings → Desktop) can be launched this way." },
    "desktop.focus":   { sub: "The model wants to bring a window to the foreground.",        info: "Switches focus to the chosen window. No keystrokes or clicks are sent." },
    "desktop.click":   { sub: "The model wants to click somewhere on your screen.",          info: "Sends a real mouse click at the chosen coordinates. Verify the target is what you expect." },
    "desktop.type":    { sub: "The model wants to type text into the active window.",        info: "Sends keystrokes to whatever window currently has focus. Don't approve if a sensitive prompt is open." },
    "desktop.keys":    { sub: "The model wants to press a keyboard shortcut.",               info: "Sends a key combination to the focused window." },
    "desktop.close":   { sub: "The model wants to close a window.",                          info: "Sends a close signal to the chosen window. Unsaved work in that app may be lost." },
    "scan_apk":        { sub: "The model wants to run an APK security scan.",                info: "Read-only — parses the APK and looks for hardcoded secrets, dangerous permissions, and risky exports." },
    "decompile_apk":   { sub: "The model wants to decompile an APK to Java sources.",        info: "Writes the JADX output into a sandbox subfolder next to the APK. Long-running on big APKs." },
    "ghidra_analyze":  { sub: "The model wants to analyze a native binary with Ghidra.",     info: "Runs Ghidra in-process — first call boots the JVM (~10s) and runs auto-analysis (~30s). Read-only on disk." },
    binwalk_scan:      { sub: "The model wants to scan a firmware blob for embedded files.", info: "Read-only — pattern-matches known headers (squashfs, jffs2, gzip, ELF, etc.) and reports offsets." },
    extract_archive:   { sub: "The model wants to extract an archive.",                      info: "Writes the unpacked tree into a sandbox subfolder next to the archive." },
    extract_squashfs:  { sub: "The model wants to extract a squashfs image.",                info: "Writes the unpacked filesystem into a sandbox subfolder next to the image." },
    carve_file:        { sub: "The model wants to carve a region out of a file.",            info: "Reads the requested byte range and writes it as a new file in the workspace." },
    registry:          { sub: "The model wants to MODIFY THE WINDOWS REGISTRY (user hive).", info: "Registry edits change how Windows and installed apps behave for your user account. They're not file-level — there's no undo. System hives (HKLM / HKCR / HKU) are hard-blocked at the bridge and never reach this card; only HKCU / HKCC writes can ask. Hold the Approve button for 2 seconds to confirm." },
  };

  // Build a friendly, language-style command preview from kind + details. We
  // intentionally don't surface the raw PowerShell / shell command in the main
  // body — that lives in the Advanced details expander. Reading
  // `write_file(path="...", content=<15 bytes>)` is more honest about what's
  // about to happen than `Set-Content -Path "..." -Value <15 chars>`.
  function approvalCommandPreview(a) {
    const d = a.details || {};
    const kind = d.kind || "";
    const fmtArgs = (args) => args.map(([k, v]) => `<span class="appr-cmd-arg">${esc(k)}</span>=<span class="appr-cmd-val">${esc(v)}</span>`).join(",\n  ");
    const buildCall = (fn, args) => {
      if (!args.length) return `<span class="appr-cmd-fn">${esc(fn)}</span>()`;
      return `<span class="appr-cmd-fn">${esc(fn)}</span>(\n  ${fmtArgs(args)}\n)`;
    };
    const q = (s) => `"${String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
    if (kind === "write_file") {
      return buildCall("write_file", [
        ["path", q(d.path || "")],
        ["content", `<${(d.bytes || 0).toLocaleString()} bytes>`],
      ]);
    }
    if (kind === "edit_file") {
      const args = [["path", q(d.path || "")], ["edits", String(d.edits || 0)]];
      if (d.preview) args.push(["preview", q(String(d.preview).slice(0, 80).replace(/\n/g, "\\n"))]);
      return buildCall("edit_file", args);
    }
    if (kind === "delete") {
      return buildCall("delete_file", [
        ["path", q(d.path || "")],
        ["target", d.dir ? "directory" : "file"],
      ]);
    }
    if (kind === "launch") {
      return buildCall("open_program", [["path", q(d.path || "")]]);
    }
    if (kind === "powershell") {
      // PowerShell is the only kind where the raw command IS the most honest
      // preview. Show it verbatim, line-wrapped.
      return `<span class="appr-cmd-shell">${esc(a.command || "")}</span>`;
    }
    if (kind === "registry") {
      // Same reasoning as powershell — the raw command IS the truth, and
      // hiding it behind a synthetic call would be misleading for the one
      // approval where reading every character matters most.
      return `<span class="appr-cmd-shell">${esc(a.command || "")}</span>`;
    }
    if (kind === "network_snapshot") {
      return buildCall("network_snapshot", []);
    }
    if (kind === "desktop.launch")  return buildCall("desktop_launch_app",   [["target", q(d.target || "")]]);
    if (kind === "desktop.focus")   return buildCall("desktop_focus_window", [["title", q(d.title || "")]]);
    if (kind === "desktop.click")   return buildCall("desktop_click",        [["x", String(d.x ?? "?")], ["y", String(d.y ?? "?")], ["button", q(d.button || "left")]]);
    if (kind === "desktop.type")    return buildCall("desktop_type_text",    [["chars", String(d.length ?? (d.text || "").length)]]);
    if (kind === "desktop.keys")    return buildCall("desktop_press_keys",   [["combo", q(d.combo || "")]]);
    if (kind === "desktop.close")   return buildCall("desktop_close_window", [["title", q(d.title || "")]]);
    if (kind === "scan_apk")        return buildCall("scan_apk",      [["path", q(d.path || "")]]);
    if (kind === "decompile_apk")   return buildCall("decompile_apk", [["path", q(d.path || "")]]);
    if (kind === "ghidra_analyze")  return buildCall("ghidra_analyze",[["path", q(d.path || "")]]);
    if (kind === "binwalk_scan")    return buildCall("binwalk_scan",  [["path", q(d.path || "")]]);
    if (kind === "extract_archive") return buildCall("extract_archive",[["path", q(d.path || "")]]);
    if (kind === "extract_squashfs")return buildCall("extract_squashfs",[["path", q(d.path || "")]]);
    if (kind === "carve_file")      return buildCall("carve_file",    [["path", q(d.path || "")]]);
    // Generic fallback: dump the raw command verbatim.
    return `<span class="appr-cmd-shell">${esc(a.command || kind || "")}</span>`;
  }

  // Build the structured DETAILS rows for the new card — icon + label + value.
  // Compact: only the fields that matter for the kind. Returns "" if there's
  // nothing meaningful to show, which lets the card hide the section entirely.
  function approvalDetailRows(a) {
    const d = a.details || {};
    const kind = d.kind || "";
    const rows = [];
    const row = (icon, k, v) => rows.push({ icon, k, v: String(v) });
    if (kind === "write_file") {
      row(APPR_SVG.file, "Path", d.path || "?");
      row(APPR_SVG.hash, "Size", (d.bytes || 0).toLocaleString() + " bytes");
      row(APPR_SVG.pencil, "Overwrite", "Yes, if exists");
    } else if (kind === "edit_file") {
      row(APPR_SVG.file, "Path", d.path || "?");
      row(APPR_SVG.pencil, "Edits", String(d.edits || "?"));
      if (d.preview) row(APPR_SVG.text, "Preview", String(d.preview).slice(0, 120).replace(/\n/g, " "));
    } else if (kind === "delete") {
      row(d.dir ? APPR_SVG.folder : APPR_SVG.file, "Path", d.path || "?");
      row(APPR_SVG.trash, "Target", d.dir ? "Directory" : "File");
      row(APPR_SVG.info, "Reversible", "No — permanent");
    } else if (kind === "launch") {
      row(APPR_SVG.play, "Launches", d.path || "?");
    } else if (kind === "powershell") {
      row(APPR_SVG.terminal, "Shell", "PowerShell");
      row(APPR_SVG.info, "Read the command below", "before approving");
    } else if (kind === "registry") {
      // Surface every hive being touched as its own row so the user can
      // scan the targets at a glance. System hives never reach this card
      // (bridge refuses them), so everything here is HKCU/HKCC scope.
      const targets = Array.isArray(d.targets) ? d.targets : [];
      if (targets.length === 0) row(APPR_SVG.hash, "Target", "(unknown — opaque .reg import?)");
      else for (const t of targets) row(APPR_SVG.hash, "Key", t);
      row(APPR_SVG.info, "Scope", "User hive (HKCU / HKCC) — no system damage possible");
      row(APPR_SVG.info, "Reversible", "No undo — registry edits are immediate");
    } else if (kind === "network_snapshot") {
      row(APPR_SVG.globe, "Reads", "Active TCP/UDP + DNS cache");
      row(APPR_SVG.info, "Admin", "Not required");
      row(APPR_SVG.info, "Network", "Read-only — no packets sent");
    } else if (kind === "desktop.launch") {
      row(APPR_SVG.play, "Launches", d.target || "?");
      row(APPR_SVG.info, "Allowlist", "Passed");
    } else if (kind === "desktop.focus") {
      row(APPR_SVG.monitor, "Focus", d.title || "?");
    } else if (kind === "desktop.click") {
      row(APPR_SVG.cursor, "At", `${d.x ?? "?"}, ${d.y ?? "?"}`);
      row(APPR_SVG.cursor, "Button", d.button || "left");
      if (d.clicks) row(APPR_SVG.hash, "Count", String(d.clicks));
    } else if (kind === "desktop.type") {
      row(APPR_SVG.keyboard, "Length", `${d.length ?? (d.text || "").length} chars`);
      if (d.text) row(APPR_SVG.text, "Preview", d.text.slice(0, 80) + ((d.text || "").length > 80 ? "…" : ""));
    } else if (kind === "desktop.keys") {
      row(APPR_SVG.keyboard, "Combo", d.combo || "?");
    } else if (kind === "desktop.close") {
      row(APPR_SVG.monitor, "Closes", d.title || "?");
    } else if (kind === "scan_apk" || kind === "decompile_apk" || kind === "ghidra_analyze" || kind === "binwalk_scan" || kind === "extract_archive" || kind === "extract_squashfs" || kind === "carve_file") {
      if (d.path) row(APPR_SVG.file, "Path", d.path);
    } else {
      // Generic: surface any path / target field if present.
      if (d.path)   row(APPR_SVG.file,   "Path",   d.path);
      if (d.target) row(APPR_SVG.play,   "Target", d.target);
      if (d.title)  row(APPR_SVG.monitor, "Window", d.title);
    }
    if (!rows.length) return "";
    return `<div class="appr-details">${rows.map(r =>
      `<div class="appr-detail-row"><span class="appr-detail-icon">${r.icon}</span><span class="appr-detail-key">${esc(r.k)}</span><span class="appr-detail-val">${esc(r.v)}</span></div>`
    ).join("")}</div>`;
  }

  // Pick the right header icon variant for this kind. Destructive kinds get
  // a warning shield; everything else gets the friendly check shield.
  function approvalHeaderIcon(kind) {
    if (kind === "delete" || kind === "write_file") {
      // warning-tone shield — same outline, no checkmark.
      return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2 4 5v6c0 4.5 3.2 8.5 8 10 4.8-1.5 8-5.5 8-10V5z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
    }
    if (kind === "registry") {
      // Filled warning triangle — heaviest icon in the set, deliberately
      // distinct from the shield used elsewhere so the registry card reads
      // as "this is a different category of dangerous" at a glance.
      return '<svg viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" fill="currentColor" stroke="currentColor"/><line x1="12" y1="9" x2="12" y2="13" stroke="#fff" stroke-width="2"/><line x1="12" y1="17" x2="12.01" y2="17" stroke="#fff" stroke-width="2.5"/></svg>';
    }
    return APPR_SVG.shield;
  }

  function renderApprovals() {
    const stack = $("#approval-stack");
    if (stack) stack.innerHTML = "";
    const chatInner = $("#chat-inner");
    if (!chatInner) return;
    chatInner.querySelectorAll(".approval-row").forEach(n => n.remove());

    for (const a of state.approvals.values()) {
      const row = document.createElement("div");
      row.className = "bubble-row approval-row";
      row.dataset.approvalId = a.id;
      const details = a.details || {};
      const kind = details.kind || "command";
      const isDesktop = String(kind).startsWith("desktop.") || kind === "ui.action";
      const isDestructive = ["delete", "write_file", "powershell", "launch"].includes(kind);
      const meta = APPR_KIND_META[kind] || {};
      const sub = meta.sub || "The model is requesting permission to run a privileged action.";
      const info = meta.info || "Read the command preview below before approving. Once approved, the action runs immediately with your user privileges.";

      const detailsHtml = approvalDetailRows(a);
      const cmdPreview = approvalCommandPreview(a);
      const headerIcon = approvalHeaderIcon(kind);

      const card = document.createElement("div");
      card.className = "approval inline";
      if (isDesktop) card.classList.add("kind-desktop");
      if (isDestructive) card.classList.add("kind-destructive");
      // The registry kind gets its own strong-warning treatment AND a
      // hold-to-approve button (wired below). Sits above kind-destructive
      // in the visual hierarchy.
      const isRegistry = kind === "registry";
      if (isRegistry) card.classList.add("kind-registry");
      // Status pill markup. Default state is "pending" — on click of the
      // approve/deny buttons we morph the pill in place (`is-pending` →
      // `is-approved` / `is-denied`) for a brief moment before the card
      // slides out, so the user sees the decision land instead of the
      // card just disappearing. Icons use Phosphor classes inline so
      // they swap with the variant via JS.
      const pillSvg = {
        pending:  '<i class="ph-fill ph-clock"></i>',
        approved: '<i class="ph-fill ph-check-circle"></i>',
        denied:   '<i class="ph-fill ph-x-circle"></i>',
        expired:  '<i class="ph-fill ph-hourglass"></i>',
      };
      card.innerHTML = `
        <div class="appr-accent-bar"></div>
        <div class="appr-head" data-appr-child>
          <span class="appr-head-icon">${headerIcon}</span>
          <span class="appr-head-title">${esc(a.title || "Action")}</span>
          <span class="appr-head-tag">${esc(String(kind).toUpperCase())}</span>
          <span class="status-pill-soft is-pending" data-status-pill>
            <span class="pill-dot">${pillSvg.pending}</span>
            <span class="pill-label">Pending</span>
          </span>
        </div>
        <div class="appr-sub" data-appr-child>${esc(sub)}</div>
        <pre class="appr-cmd" data-appr-child><code>${cmdPreview}</code></pre>
        ${detailsHtml ? `<div data-appr-child>${detailsHtml}</div>` : ""}
        <div class="appr-info-line" data-appr-child>
          <span class="appr-info-icon">${APPR_SVG.info}</span>
          <span>${esc(info)}</span>
        </div>
        <div class="appr-actions" data-appr-child>
          <button class="appr-btn appr-btn-deny" data-act="deny">${APPR_SVG.x}<span>Deny</span></button>
          <button class="appr-btn appr-btn-approve" data-act="approve">${APPR_SVG.check}<span>Approve</span></button>
        </div>
        <details class="appr-advanced" data-appr-child>
          <summary><span class="appr-advanced-chevron">${APPR_SVG.chevron}</span><span>Advanced</span></summary>
          <pre class="appr-advanced-body">${esc(a.command || "(no raw command)")}</pre>
        </details>`;
      // Morph the status pill in place before the card slides out so the
      // user sees the decision register. The pill swap is purely cosmetic
      // (the actual decideApproval() POST + state mutation runs in
      // parallel) and adds ~280ms of visible feedback.
      const morphPill = (toState) => {
        const pill = card.querySelector("[data-status-pill]");
        if (!pill) return;
        pill.classList.remove("is-pending", "is-approved", "is-denied", "is-expired");
        pill.classList.add("is-" + toState);
        const labelEl = pill.querySelector(".pill-label");
        const dotEl = pill.querySelector(".pill-dot");
        if (labelEl) labelEl.textContent = toState.charAt(0).toUpperCase() + toState.slice(1);
        if (dotEl && pillSvg[toState]) dotEl.innerHTML = pillSvg[toState];
      };
      const approveBtn = card.querySelector('[data-act="approve"]');
      const denyBtn = card.querySelector('[data-act="deny"]');
      if (isRegistry) {
        // Hold-to-approve. 2-second hold required; releasing the button or
        // dragging off cancels and resets the fill. Prevents accidental
        // enter-key / single-click approvals on a destructive operation.
        // The .appr-btn-hold class triggers a ::before progress fill in
        // CSS; the is-holding class enables the 2s width transition.
        const HOLD_MS = 2000;
        let holdTimer = null;
        approveBtn.classList.add("appr-btn-hold");
        const labelEl = approveBtn.querySelector("span");
        if (labelEl) labelEl.textContent = "Hold to approve";
        const cancelHold = () => {
          if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
          approveBtn.classList.remove("is-holding");
        };
        const startHold = (e) => {
          e.preventDefault();
          if (holdTimer) return;
          approveBtn.classList.add("is-holding");
          holdTimer = setTimeout(() => {
            holdTimer = null;
            approveBtn.classList.remove("is-holding");
            approveBtn.classList.add("is-armed");
            morphPill("approved");
            setTimeout(() => decideApproval(a.id, "approve"), 280);
          }, HOLD_MS);
        };
        approveBtn.addEventListener("pointerdown", startHold);
        approveBtn.addEventListener("pointerup", cancelHold);
        approveBtn.addEventListener("pointerleave", cancelHold);
        approveBtn.addEventListener("pointercancel", cancelHold);
        // Block plain click + Enter key — they'd skip the hold gate
        // entirely. The pointerdown path is the only way in.
        approveBtn.addEventListener("click", (e) => e.preventDefault());
        approveBtn.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") e.preventDefault();
        });
      } else {
        approveBtn.addEventListener("click", () => {
          morphPill("approved");
          setTimeout(() => decideApproval(a.id, "approve"), 280);
        });
      }
      denyBtn.addEventListener("click", () => {
        morphPill("denied");
        setTimeout(() => decideApproval(a.id, "deny"), 280);
      });
      row.innerHTML = `
        <div class="avatar approval-avatar">${APPR_SVG.shield}</div>
        <div class="bubble-col"></div>`;
      row.querySelector(".bubble-col").appendChild(card);
      chatInner.appendChild(row);
    }
    if (state.approvals.size > 0) {
      requestAnimationFrame(() => scrollToBottom(true));
    }
  }

  async function decideApproval(id, decision) {
    state.approvals.delete(id);
    renderApprovals();
    await fetch("/api/approvals/decide", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, decision }),
    });
  }

  async function loadApprovals() {
    const r = await api("/api/approvals");
    state.approvals.clear();
    for (const a of r.pending || []) state.approvals.set(a.id, a);
    renderApprovals();
  }

  // ---------- SSE ----------
  // Tracks the bridge's monotonic event-id snapshot across reconnects.
  // When `hello` arrives with a snapshot_id LOWER than the last one we
  // saw, the bridge restarted (id counter resets on boot) — useful for
  // showing a "bridge restarted" toast distinct from a normal reconnect.
  let _lastSnapshotId = -1;

  function subscribeSSE() {
    const es = new EventSource("/api/events");
    es.onmessage = (e) => {
      let evt;
      try { evt = JSON.parse(e.data); } catch { return; }
      if (evt.type === "approval:new") {
        state.approvals.set(evt.approval.id, evt.approval);
        renderApprovals();
        notifyApproval();
      } else if (evt.type === "approval:decided") {
        state.approvals.delete(evt.id);
        renderApprovals();
      } else if (evt.type === "settings:update") {
        loadSettings().then(renderStatus).then(renderModelPill);
      } else if (evt.type === "workspace:update") {
        loadWorkspace().then(renderWorkspace);
      } else if (evt.type === "chat:rename") {
        const c = state.chats && state.chats.chats && state.chats.chats[evt.chat_id];
        if (c) {
          c.title = evt.title;
          renderChatList();
        }
      } else if (evt.type === "desktop:panic") {
        if (evt.on) toast("desktop automation PANICKED — all actions blocked", "warn", 6000, "desktop-panic");
        else toast("desktop automation resumed", "ok", 2000, "desktop-panic");
        refreshDesktopStatus();
      } else if (evt.type === "memories:update") {
        if ($("#settings-drawer")?.classList.contains("open")) loadMemories();
      } else if (evt.type === "models:update") {
        loadModels().then(() => {
          if ($("#settings-drawer")?.classList.contains("open")) populateSettingsForm();
          renderModelPill();
        });
      } else if (evt.type === "hello") {
        // New connection. If snapshot_id went BACKWARDS (or jumped to a
        // tiny number after we'd been running for a while), the bridge
        // restarted — surface that distinctly from a normal wifi blip.
        const snap = Number(evt.snapshot_id || 0);
        if (_lastSnapshotId > 0 && snap < _lastSnapshotId) {
          toast("Bridge restarted — reconnected.", "info", 3500, "sse-hello");
        } else if (evt.replayed_from && _lastSnapshotId > 0) {
          // Reconnect WITHOUT a bridge restart — just say "back online".
          toast("Reconnected.", "ok", 1800, "sse-hello");
        }
        _lastSnapshotId = snap;
      } else if (evt.type === "events:gap") {
        // The disconnect was longer than the bridge's 256-event ring buffer
        // — some events were lost forever. Tell the user so they know to
        // reload if things look stale (mid-tool-call, half-rendered turn).
        const lost = (evt.lost_to || 0) - (evt.lost_from || 0) + 1;
        toast(
          `Connection dropped for too long — ${lost} event${lost === 1 ? "" : "s"} missed. ` +
          `Reload the page if anything looks half-finished.`,
          "warn", 8000, "sse-gap"
        );
      } else if (evt.type === "llama:watchdog_restart") {
        // llama-server crashed; bridge is auto-restarting. Backoff is in
        // evt.delay seconds; attempt N of 3.
        const att = evt.attempt || 1;
        const max = 3;
        const sec = Math.round(evt.delay || 2);
        toast(
          `llama-server crashed — auto-restart attempt ${att}/${max} in ${sec}s…`,
          "warn", 4500, "llama-watchdog"
        );
      } else if (evt.type === "llama:watchdog_restored") {
        toast(
          `llama-server back up${evt.pid ? ` (pid ${evt.pid})` : ""} — keep going.`,
          "ok", 3000, "llama-watchdog"
        );
        // Refresh model pill / status since the loaded model is back.
        try { loadModels().then(renderModelPill); } catch {}
      } else if (evt.type === "llama:watchdog_stuck") {
        // Circuit breaker tripped: 3 crashes in 60s. The bridge has given
        // up auto-restarting. This is the most important event in the
        // batch — long-lived toast with a clear next-step.
        toast(
          (evt.message || "llama-server keeps crashing.") +
          " Auto-restart suspended. Open Settings → Models and pick a different model or lower num_ctx.",
          "err", 60000, "llama-watchdog"
        );
        try { loadModels().then(renderModelPill); } catch {}
      }
    };
    es.onerror = () => {
      es.close();
      setTimeout(subscribeSSE, 3000);
    };
  }

  // ---------- settings drawer ----------
  async function openSettings() {
    $("#drawer-scrim").classList.add("open");
    $("#settings-drawer").classList.add("open");
    await loadModels();
    populateSettingsForm();
    loadSystemContext();
    loadDetectedVram();
  }

  // ---------- VRAM auto-tune ----------
  // Last detected GPU info, kept around so the auto-tune button can quote it
  // in its notes ("based on detected 12.0 GB RTX 4070...").
  const _vramState = { detected: null };

  // Best-effort pre-select of the closest tier in the dropdown.
  function _pickClosestVramTier(gb) {
    const sel = $("#set-vram-tier");
    if (!sel) return;
    const opts = Array.from(sel.options).map(o => Number(o.value)).filter(v => v > 0);
    if (!opts.length) return;
    // Pick the largest tier that is <= detected, falling back to the smallest.
    let pick = opts[0];
    for (const v of opts) if (v <= gb && v >= pick) pick = v;
    if (gb >= Math.max(...opts)) pick = Math.max(...opts);
    sel.value = String(pick);
  }

  async function loadDetectedVram() {
    const hint = $("#vram-detected-hint");
    if (!hint) return;
    hint.textContent = "detecting GPU...";
    try {
      const r = await api("/api/llama/detect-vram");
      _vramState.detected = r;
      const gb = Number(r?.gb || 0);
      if (gb > 0) {
        const name = r.name ? ` ${r.name}` : "";
        hint.textContent = `detected: ${gb.toFixed(1)} GB${name} (via ${r.source || "nvidia-smi"})`;
        // If the user hasn't picked a tier yet (it's still 0 = Manual), nudge to
        // the closest detected tier so the Suggest button is one click away.
        const sel = $("#set-vram-tier");
        if (sel && Number(sel.value) === 0) _pickClosestVramTier(gb);
      } else {
        hint.textContent = "no NVIDIA GPU detected — pick a VRAM tier manually if you want a suggestion";
      }
    } catch (e) {
      hint.textContent = `vram detect failed: ${e.message || e}`;
    }
  }

  async function runAutoTune() {
    const btn = $("#btn-autotune");
    const notes = $("#autotune-notes");
    const sel = $("#set-vram-tier");
    const modelPath = ($("#set-model")?.value || "").trim();
    const tier = Number(sel?.value || 0);
    if (!modelPath) {
      toast("pick a model first — auto-tune needs to know its size", "warn", 3000);
      return;
    }
    if (!tier) {
      toast("pick a VRAM tier (or leave on Manual to skip auto-tune)", "warn", 3000);
      return;
    }
    if (btn) btn.disabled = true;
    if (notes) notes.textContent = "thinking...";
    try {
      const r = await api("/api/llama/auto-tune", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model_path: modelPath, vram_gb: tier }),
      });
      const sug = r?.suggested || {};
      // RULE: autotune may GROW num_ctx, never shrink it. If the user already
      // has a larger context working, don't downgrade them.
      const curCtx = Number($("#set-ctx")?.value || state.settings.num_ctx || 0) || 0;
      const sugCtx = Number(sug.num_ctx || 0) || 0;
      if (sugCtx > 0 && sugCtx < curCtx) {
        sug.num_ctx = curCtx;
      }
      // Prefill every field the suggester returns. Only touches fields that
      // exist in the suggested payload — leaves untouched fields alone so the
      // user's manual tweaks survive.
      const setVal = (id, v) => { const el = $(id); if (el != null && v != null) el.value = String(v); };
      const setSwitch = (id, v) => {
        const el = $(id);
        if (!el || v == null) return;
        el.classList.toggle("on", !!v);
      };
      setVal("#set-ctx", sug.num_ctx);
      setVal("#set-gpu", sug.num_gpu);
      setVal("#set-batch", sug.num_batch);
      const kv = $("#set-kv");
      if (kv && sug.kv_cache_type) kv.value = sug.kv_cache_type;
      setVal("#set-ncmoe", sug.n_cpu_moe);
      setVal("#set-ubatch", sug.n_ubatch);
      setVal("#set-parallel", sug.n_parallel);
      setSwitch("#sw-flash", sug.flash_attn);
      if (sug.spec_strategy) setVal("#set-spec-strategy", sug.spec_strategy);
      setSwitch("#sw-nowarmup", sug.no_warmup);
      setSwitch("#sw-metrics", sug.enable_metrics);

      // Build a short, friendly notes blob from the server reply + model meta.
      const lines = [];
      const m = r?.model || {};
      const head = [];
      if (m.name) head.push(m.name);
      if (m.quant) head.push(m.quant);
      if (m.size_gb) head.push(`${Number(m.size_gb).toFixed(1)} GB on disk`);
      if (m.is_moe) {
        const tag = m.active_params_b
          ? `MoE ${m.total_params_b || "?"}B-A${m.active_params_b}B`
          : "MoE";
        head.push(tag);
      }
      if (head.length) lines.push(head.join(" · "));
      const v = `target VRAM: ${Number(r?.vram_gb || tier).toFixed(0)} GB${r?.vram_name ? ` (${r.vram_name})` : ""}`;
      lines.push(v);

      // Quant-downshift banner: highest-leverage user-facing recommendation.
      // Rendered with a leading marker so it stands out in the plain-text panel.
      if (sug.quant_downshift) {
        lines.push("");
        lines.push(`>> ${sug.quant_downshift}`);
        lines.push("");
      }
      if (sug.notes) lines.push(sug.notes);
      lines.push("review the values below, then Save to apply (model will reload).");
      if (notes) notes.textContent = lines.join("\n");
      const toastMsg = sug.quant_downshift
        ? "values filled — but consider the quant suggestion in the notes"
        : "suggested values filled in — review then Save";
      toast(toastMsg, sug.quant_downshift ? "warn" : "ok", 4000);
    } catch (e) {
      if (notes) notes.textContent = `auto-tune failed: ${e.message || e}`;
      toast("auto-tune failed: " + (e.message || e), "error", 5000);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function loadSystemContext() {
    const ta = $("#set-sysctx");
    const path = $("#sysctx-path");
    if (!ta) return;
    ta.value = "loading…";
    try {
      const r = await api("/api/system-context");
      ta.value = r.md || "";
      if (path) path.textContent = r.path || "";
    } catch (e) {
      ta.value = `(failed: ${e.message || e})`;
    }
  }
  async function saveSystemContext() {
    const ta = $("#set-sysctx");
    if (!ta) return;
    const btn = $("#btn-sysctx-save");
    if (btn) btn.disabled = true;
    try {
      await api("/api/system-context", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ md: ta.value }) });
    } catch (e) {
      alert(`save failed: ${e.message || e}`);
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  // ---------- memories panel ----------
  function renderMemoriesList(items) {
    const host = $("#mem-list");
    if (!host) return;
    host.innerHTML = "";
    if (!items || !items.length) {
      host.innerHTML = `<div class="mem-empty">no memories yet — add one below, or let the model call <code>remember</code>.</div>`;
      return;
    }
    for (const m of items) {
      const row = document.createElement("div");
      row.className = "mem-item";
      const tags = Array.isArray(m.tags) && m.tags.length
        ? `<span class="mem-tags">${m.tags.map(t => `<span class="mem-tag">${esc(t)}</span>`).join("")}</span>`
        : "";
      row.innerHTML = `
        <div class="mem-text">${esc(m.text || "")}</div>
        <div class="mem-foot">
          ${tags}
          <span class="mem-ts">${m.t ? relTime(m.t) : ""}</span>
          <button class="btn ghost sm mem-del" type="button" title="Forget"><i class="ph ph-trash"></i></button>
        </div>`;
      row.querySelector(".mem-del").addEventListener("click", async () => {
        try {
          await api("/api/memories/forget", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: m.id }),
          });
        } catch (e) { toast("forget failed: " + e.message, "error"); }
      });
      host.appendChild(row);
    }
  }
  async function loadMemories() {
    try {
      const r = await api("/api/memories");
      renderMemoriesList(r.memories || []);
    } catch (e) {
      const host = $("#mem-list");
      if (host) host.innerHTML = `<div class="mem-empty">(failed: ${esc(e.message || String(e))})</div>`;
    }
  }
  async function addMemoryFromInput() {
    const input = $("#mem-add-text");
    if (!input) return;
    const text = (input.value || "").trim();
    if (!text) return;
    const btn = $("#btn-mem-add");
    if (btn) btn.disabled = true;
    try {
      await api("/api/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      input.value = "";
    } catch (e) {
      toast("add failed: " + e.message, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function rescanSystemContext() {
    const btn = $("#btn-sysctx-rescan");
    const ta = $("#set-sysctx");
    if (btn) btn.disabled = true;
    if (ta) ta.value = "scanning…";
    try {
      const r = await api("/api/system-context/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (ta) ta.value = r.md || "";
    } catch (e) {
      if (ta) ta.value = `(rescan failed: ${e.message || e})`;
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  function closeSettings() {
    $("#drawer-scrim").classList.remove("open");
    $("#settings-drawer").classList.remove("open");
  }

  // ---------- Command history drawer ----------
  // PowerShell command audit log. Backend logs every _run_powershell call
  // to data/cmd_history.jsonl (with chat_id, exit code, stdout/stderr, and
  // duration); this drawer fetches /api/cmd-history on open and renders
  // each entry as a collapsed row + click-to-expand detail. Reusing the
  // same scrim mechanism as Settings keeps the open/close UX consistent.
  async function openCmdHistory() {
    $("#cmd-history-scrim").classList.add("open");
    $("#cmd-history-drawer").classList.add("open");
    await loadCmdHistory();
  }
  function closeCmdHistory() {
    $("#cmd-history-scrim").classList.remove("open");
    $("#cmd-history-drawer").classList.remove("open");
  }
  async function loadCmdHistory() {
    const body = $("#cmd-history-body");
    const countEl = $("#cmd-history-count");
    if (!body) return;
    body.innerHTML = `<div class="cmd-history-empty">Loading…</div>`;
    try {
      const r = await api("/api/cmd-history?limit=300");
      const entries = Array.isArray(r?.entries) ? r.entries : [];
      if (countEl) countEl.textContent = entries.length ? `${entries.length} entries` : "0 entries";
      renderCmdHistory(entries);
    } catch (e) {
      body.innerHTML = `<div class="cmd-history-empty">Failed to load history: ${esc(e.message || String(e))}</div>`;
      if (countEl) countEl.textContent = "—";
    }
  }
  function renderCmdHistory(entries) {
    const body = $("#cmd-history-body");
    if (!body) return;
    if (!entries.length) {
      body.innerHTML = `<div class="cmd-history-empty">No PowerShell commands recorded yet.<br><br><span style="font-size:11px;">Anything the agent runs via <code>run_powershell</code> shows up here.</span></div>`;
      return;
    }
    const chatsMap = (state.chats && state.chats.chats) || {};
    const chevSvg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>`;
    body.innerHTML = entries.map((e, idx) => {
      const okClass = e.spawn_error ? "fail" : (e.timed_out ? "warn" : (e.ok ? "ok" : "fail"));
      const exitLabel = e.spawn_error ? "ERR" : (e.timed_out ? "TIME" : (e.ok ? "OK" : `EXIT ${e.exit ?? "?"}`));
      const cmdPreview = String(e.command || "").split("\n")[0].slice(0, 240);
      // ts comes from the bridge as milliseconds (Python int(time.time() * 1000));
      // relTime expects seconds, so divide.
      const when = e.ts ? relTime(Math.floor(e.ts / 1000)) : "—";
      const chatLabel = e.chat_id ? (chatsMap[e.chat_id]?.title || e.chat_id.slice(0, 8)) : "—";
      const stdoutPre = e.stdout && e.stdout.trim()
        ? `<pre class="cmd-detail-pre">${esc(e.stdout)}</pre>`
        : `<pre class="cmd-detail-pre empty">(empty)</pre>`;
      const stderrPre = e.stderr && e.stderr.trim()
        ? `<pre class="cmd-detail-pre stderr">${esc(e.stderr)}</pre>`
        : `<pre class="cmd-detail-pre empty">(empty)</pre>`;
      const dur = e.duration_ms != null ? `${e.duration_ms.toLocaleString()} ms` : "—";
      const noteLine = e.timed_out ? "<b>Timed out</b> · " : (e.spawn_error ? "<b>Spawn error</b> · " : "");
      return `<div class="cmd-row" data-idx="${idx}">
  <div class="cmd-row-summary">
    <span class="cmd-row-time">${esc(when)}</span>
    <span class="cmd-row-exit ${okClass}">${esc(exitLabel)}</span>
    <span class="cmd-row-cmd" title="${esc(cmdPreview)}">${esc(cmdPreview)}</span>
    <span class="cmd-row-chev">${chevSvg}</span>
  </div>
  <div class="cmd-row-detail">
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">Full command<button class="cmd-detail-copy" data-copy="cmd">Copy</button></div>
      <pre class="cmd-detail-pre" data-cmd>${esc(e.command || "")}</pre>
    </div>
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">stdout<button class="cmd-detail-copy" data-copy="stdout">Copy</button></div>
      ${stdoutPre}
    </div>
    <div class="cmd-detail-section">
      <div class="cmd-detail-label">stderr<button class="cmd-detail-copy" data-copy="stderr">Copy</button></div>
      ${stderrPre}
    </div>
    <div class="cmd-detail-meta">
      ${noteLine}<span><b>Duration:</b> ${esc(dur)}</span>
      <span><b>Chat:</b> ${esc(chatLabel)}</span>
    </div>
  </div>
</div>`;
    }).join("");
    // wire row expand + copy buttons (delegated)
    body.querySelectorAll(".cmd-row-summary").forEach(sum => {
      sum.addEventListener("click", () => sum.parentElement.classList.toggle("expanded"));
    });
    body.querySelectorAll(".cmd-detail-copy").forEach(btn => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const what = btn.dataset.copy;
        const row = btn.closest(".cmd-row");
        if (!row) return;
        const idx = Number(row.dataset.idx);
        const e = entries[idx];
        let text = "";
        if (what === "cmd") text = e.command || "";
        else if (what === "stdout") text = e.stdout || "";
        else if (what === "stderr") text = e.stderr || "";
        if (!text) { toast("nothing to copy", "warn", 1500); return; }
        navigator.clipboard.writeText(text).then(
          () => { toast("copied", "ok", 1500); },
          () => { toast("clipboard failed", "error", 2000); },
        );
      });
    });
  }
  async function clearCmdHistory() {
    if (!confirm("Clear all PowerShell command history? This can't be undone.")) return;
    try {
      await api("/api/cmd-history", { method: "DELETE" });
      toast("history cleared", "ok", 1500);
      await loadCmdHistory();
    } catch (e) {
      toast("clear failed: " + (e.message || e), "error");
    }
  }
  function populateSettingsForm() {
    const s = state.settings;
    const fill = (id, v) => { const el = $(id); if (el) el.value = v ?? ""; };

    // models folder + dropdown
    const dirInput = $("#set-models-dir");
    if (dirInput) dirInput.value = state.modelsDir || s.models_dir || "";
    const modelSel = $("#set-model");
    if (modelSel) {
      modelSel.innerHTML = "";
      const list = state.modelsList || [];
      const loaded = state.loadedModel || s.model_path || "";
      if (!list.length) {
        const msg = state.modelsError || "pick a models folder above";
        modelSel.innerHTML = `<option value="">(${msg})</option>`;
      } else {
        for (const m of list) {
          const o = document.createElement("option");
          o.value = m.path; o.textContent = m.name + (m.size_gb ? `  (${m.size_gb} GB)` : "");
          if (m.path === loaded || m.loaded) o.selected = true;
          modelSel.appendChild(o);
        }
      }
    }
    const hint = $("#set-model-hint");
    if (hint) {
      if (state.llamaRunning && state.loadedModel) {
        const name = state.loadedModel.split(/[\\/]/).pop();
        hint.textContent = `loaded: ${name}`;
      } else if (state.modelsDir) {
        hint.textContent = `${(state.modelsList || []).length} model(s) in ${state.modelsDir}`;
      } else {
        hint.textContent = "selecting a model loads it into llama-server.";
      }
    }
    const visionSel = $("#set-vision");
    if (visionSel) {
      visionSel.innerHTML = "";
      const emptyOpt = document.createElement("option");
      emptyOpt.value = ""; emptyOpt.textContent = "(none)";
      visionSel.appendChild(emptyOpt);
      for (const m of (state.modelsList || [])) {
        const o = document.createElement("option");
        o.value = m.path; o.textContent = m.name;
        if (m.path === s.vision_model) o.selected = true;
        visionSel.appendChild(o);
      }
    }
    // mmproj path field — text input + auto-detect/clear buttons. Hint reflects
    // current vision capability so the user knows whether the loaded model
    // already speaks images or is falling back to the side-OCR path.
    const mmInp = $("#set-mmproj");
    if (mmInp) mmInp.value = s.mmproj_path || "";
    const mmHint = $("#set-mmproj-hint");
    if (mmHint) {
      if (state.visionCapable) {
        mmHint.innerHTML = `<span style="color:var(--mint-3);">native vision active</span> — chat model is reading images directly.`;
      } else if (s.mmproj_path) {
        mmHint.textContent = "projector configured — restart the model for it to take effect.";
      } else {
        mmHint.innerHTML = `leave blank to auto-detect a sibling <code>mmproj-*.gguf</code>. requires a model relaunch.`;
      }
    }

    fill("#set-ctx", s.num_ctx);
    fill("#set-gpu", s.num_gpu);
    fill("#set-batch", s.num_batch);
    fill("#set-thread", s.num_thread);
    fill("#set-predict", s.num_predict);
    const kvSel = $("#set-kv");
    if (kvSel) kvSel.value = s.kv_cache_type || "q8_0";
    // VRAM tier picker (auto-tune persistence — 0 = Manual)
    const vramSel = $("#set-vram-tier");
    if (vramSel) vramSel.value = String(s.vram_tier_gb ?? 0);
    // advanced llama-server flags
    fill("#set-ncmoe", s.n_cpu_moe ?? 0);
    fill("#set-ubatch", s.n_ubatch ?? 0);
    fill("#set-parallel", s.n_parallel ?? 1);
    $("#sw-flash")?.classList.toggle("on", s.flash_attn !== false);
    // spec_strategy is the new field; fall back to enable_speculative for
    // settings.json files written before the MTP option landed. Mirror the
    // bridge's launch-time rule: an explicit `enable_speculative=false` on
    // the legacy field always wins, even if spec_strategy got filled in from
    // DEFAULTS during merge. Otherwise the UI would show "ngram-mod" while
    // the bridge actually launched with "off".
    const specSel = $("#set-spec-strategy");
    if (specSel) {
      let strat = (s.spec_strategy || "").trim().toLowerCase();
      if (s.enable_speculative === false) {
        strat = "off";
      } else if (!["off", "ngram-mod", "draft-mtp"].includes(strat)) {
        strat = "ngram-mod";
      }
      specSel.value = strat;
    }
    $("#sw-nowarmup")?.classList.toggle("on", !!s.no_warmup);
    $("#sw-metrics")?.classList.toggle("on", !!s.enable_metrics);
    const xa = $("#set-extra-args");
    if (xa) xa.value = s.llama_extra_args || "";
    fill("#set-temp", s.temperature);
    fill("#set-topp", s.top_p);
    fill("#set-topk", s.top_k ?? 40);
    fill("#set-minp", s.min_p ?? 0.05);
    fill("#set-repeat", s.repeat_penalty ?? 1.1);
    fill("#set-presence", s.presence_penalty ?? 0);
    fill("#set-frequency", s.frequency_penalty ?? 0);
    $("#sw-thinking")?.classList.toggle("on", s.enable_thinking !== false);
    fill("#set-think-budget", s.thinking_budget ?? 2048);
    const themeSel = $("#set-theme");
    if (themeSel) themeSel.value = s.theme || "light";
    $("#sw-web").classList.toggle("on", s.allow_web_preview !== false);

    // IDE toggles mirror back into the composer chips
    reflectIdeToggles();

    // desktop automation
    $("#sw-desktop-enabled")?.classList.toggle("on", !!s.desktop_enabled);
    $("#sw-red-team-enabled")?.classList.toggle("on", !!s.red_team_enabled);
    $("#sw-discord-enabled")?.classList.toggle("on", !!s.discord_enabled);
    fill("#set-discord-token", s.discord_bot_token || "");
    fill("#set-discord-owner", s.discord_owner_id || "");
    const al = $("#set-desktop-allowlist");
    if (al) al.value = (s.desktop_app_allowlist || []).join("\n");
    fill("#set-desktop-rate", s.desktop_max_actions_per_minute || 30);
    refreshDesktopStatus();
    // memories panel
    loadMemories();
  }

  async function refreshDesktopStatus() {
    try {
      const r = await api("/api/desktop/status");
      const badge = $("#desktop-deps-badge");
      if (badge) {
        const missing = [];
        if (!r.have_pyautogui) missing.push("pyautogui");
        if (!r.have_pil) missing.push("Pillow");
        if (!r.have_pygetwindow) missing.push("pygetwindow");
        badge.textContent = missing.length
          ? `missing: pip install ${missing.join(" ")}`
          : "all libs installed";
        badge.style.color = missing.length ? "var(--danger)" : "var(--mint-3)";
      }
      const ps = $("#desktop-panic-state");
      if (ps) {
        ps.textContent = r.panic ? "PANIC — all actions blocked" : "ready";
        ps.style.color = r.panic ? "var(--danger)" : "var(--fg-faint)";
      }
    } catch {}
  }
  // settings whose changes require relaunching llama-server (load-time flags)
  const LOAD_TIME_KEYS = [
    "num_ctx", "num_gpu", "num_batch", "num_thread", "kv_cache_type", "model_path",
    "n_cpu_moe", "n_ubatch", "n_parallel", "flash_attn",
    "spec_strategy", "no_warmup", "enable_metrics", "llama_extra_args",
    // mmproj_path changes how the server is launched (--mmproj <path>) so it
    // also requires a relaunch to take effect.
    "mmproj_path",
  ];

  async function collectAndSaveSettings() {
    const n = (id) => Number($(id).value);
    const modelPath = $("#set-model").value || "";
    const payload = {
      model_path: modelPath,
      model: modelPath ? modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, "") : "",
      models_dir: ($("#set-models-dir")?.value || "").trim(),
      vision_model: $("#set-vision").value,
      num_ctx: n("#set-ctx") || 8192,
      num_gpu: n("#set-gpu"),
      num_batch: n("#set-batch") || 512,
      num_thread: n("#set-thread"),
      num_predict: n("#set-predict"),
      kv_cache_type: $("#set-kv")?.value || "q8_0",
      n_cpu_moe: Math.max(0, n("#set-ncmoe") || 0),
      n_ubatch: Math.max(0, n("#set-ubatch") || 0),
      n_parallel: Math.max(1, n("#set-parallel") || 1),
      flash_attn: $("#sw-flash")?.classList.contains("on") !== false,
      // Write both the new spec_strategy and the legacy enable_speculative
      // so anything still reading the old field (older bridge instances, the
      // launch-time legacy-honoring rule) stays in sync with the user's pick.
      spec_strategy: ($("#set-spec-strategy")?.value || "ngram-mod"),
      enable_speculative: ($("#set-spec-strategy")?.value || "ngram-mod") !== "off",
      no_warmup: !!$("#sw-nowarmup")?.classList.contains("on"),
      enable_metrics: !!$("#sw-metrics")?.classList.contains("on"),
      llama_extra_args: ($("#set-extra-args")?.value || "").trim(),
      mmproj_path: ($("#set-mmproj")?.value || "").trim(),
      vram_tier_gb: Math.max(0, Number($("#set-vram-tier")?.value || 0)),
      temperature: n("#set-temp"),
      top_p: n("#set-topp"),
      top_k: n("#set-topk"),
      min_p: n("#set-minp"),
      repeat_penalty: n("#set-repeat"),
      presence_penalty: n("#set-presence"),
      frequency_penalty: n("#set-frequency"),
      enable_thinking: $("#sw-thinking")?.classList.contains("on") !== false,
      thinking_budget: n("#set-think-budget"),
      theme: ($("#set-theme")?.value || "light"),
      allow_web_preview: $("#sw-web").classList.contains("on"),
      desktop_enabled: $("#sw-desktop-enabled")?.classList.contains("on") || false,
      red_team_enabled: $("#sw-red-team-enabled")?.classList.contains("on") || false,
      discord_enabled: $("#sw-discord-enabled")?.classList.contains("on") || false,
      discord_bot_token: ($("#set-discord-token")?.value || "").trim(),
      discord_owner_id: ($("#set-discord-owner")?.value || "").trim(),
      desktop_app_allowlist: ($("#set-desktop-allowlist")?.value || "")
        .split("\n").map(x => x.trim()).filter(Boolean),
      desktop_max_actions_per_minute: Math.max(1, Math.min(300, n("#set-desktop-rate") || 30)),
      use_tailwind_cdn: !!state.settings.use_tailwind_cdn,
      ide_multifile: !!state.settings.ide_multifile,
    };

    // Detect which load-time keys actually changed → triggers a llama-server
    // restart so the new flags take effect without the user having to know.
    const prev = state.settings || {};
    const changedLoadKeys = LOAD_TIME_KEYS.filter(k => String(prev[k] ?? "") !== String(payload[k] ?? ""));

    await saveSettings(payload);
    applyTheme(payload.theme || "light");

    if (changedLoadKeys.length && payload.model_path) {
      const tid = "reload-llama";
      toast(`reloading model (${changedLoadKeys.join(", ")})…`, "info", 60000, tid);
      try {
        await api("/api/models/load", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path: payload.model_path }),
        });
        toast("model reloaded with new settings", "ok", 2500, tid);
      } catch (e) {
        toast("reload failed: " + (e.message || e), "error", 6000, tid);
      }
    }
    closeSettings();
  }

  async function prewarmModel(model) {
    if (!model) return;
    toast(`loading ${model.split("/").pop()}…`, "info", 30000, "prewarm");
    try {
      await api("/api/prewarm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model }) });
      toast(`${model.split("/").pop()} ready`, "ok", 2500, "prewarm");
    } catch {
      toast("prewarm failed — will load on first send", "warn", 3000, "prewarm");
    }
  }

  // Seven themes: dark (default), dim (warm cappuccino), aurora, nebula,
  // operator (phosphor CRT terminal), soft, light. THEME_CYCLE below is the
  // source of truth for order; the toggle button walks it start→end so the
  // first click from dark lands on the next option instead of jumping
  // jumping straight to bright white. nextTheme() handles the cycle and
  // accepts whatever string is in settings as the starting point.
  const THEME_CYCLE = ["dark", "dim", "aurora", "nebula", "operator", "soft", "light"];
  const THEME_ICONS = {
    dark:     "ph ph-moon",
    dim:      "ph ph-moon-stars",
    aurora:   "ph ph-sparkle",
    nebula:   "ph ph-planet",
    operator: "ph ph-command",
    soft:     "ph ph-cloud",
    light:    "ph ph-sun",
  };
  function nextTheme(cur) {
    const idx = THEME_CYCLE.indexOf(cur);
    return THEME_CYCLE[(idx + 1) % THEME_CYCLE.length];
  }
  // applyTheme accepts a theme STRING ("dark" | "dim" | "soft" | "light"). For
  // backward-compat with old callers that passed a boolean, we coerce:
  // true → "dark", false → "light". New code should pass the string.
  function applyTheme(theme) {
    if (theme === true) theme = "dark";
    else if (theme === false) theme = "light";
    if (!THEME_CYCLE.includes(theme)) theme = "light";
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("accuretta:theme", theme);
    const iconClass = THEME_ICONS[theme] || THEME_ICONS.light;
    const topBtn = $("#btn-theme");
    if (topBtn) topBtn.innerHTML = `<i class="${iconClass}"></i>`;
    // Keep the sidebar-foot mirror in sync. We only swap the inner <i>'s
    // class (instead of replacing innerHTML) so the small inline font-size
    // style on the icon stays put.
    const sideIcon = document.getElementById("btn-theme-side-icon");
    if (sideIcon) sideIcon.className = iconClass;
  }

  function activateTerminalTab(tabId) {
    const tabs = document.querySelectorAll(".term-tab");
    const panes = document.querySelectorAll(".term-tab-pane");
    
    tabs.forEach(tab => {
      const active = tab.dataset.tab === tabId;
      tab.classList.toggle("active", active);
    });
    
    panes.forEach(pane => {
      const active = pane.id === `term-pane-${tabId}`;
      pane.classList.toggle("hidden", !active);
    });
  }

  function appendTerminalText(text, isError) {
    const consolePre = document.getElementById("term-console-pre");
    if (!consolePre) return;
    const codeEl = consolePre.querySelector("code");
    if (!codeEl) return;
    
    const cursor = consolePre.querySelector(".term-cursor");
    
    let safeText = esc(text);
    if (isError) {
      safeText = `<span class="term-err">${safeText}</span>`;
    }
    
    if (cursor) {
      cursor.insertAdjacentHTML("beforebegin", safeText);
    } else {
      codeEl.insertAdjacentHTML("beforeend", safeText);
    }
    
    const pane = consolePre.closest('.term-tab-pane');
    if (pane) pane.scrollTop = pane.scrollHeight;
    activateTerminalTab("terminal");
  }

  function appendAgentLog(msg) {
    const agentLogPre = document.getElementById("term-agent-log-pre");
    if (!agentLogPre) return;
    const codeEl = agentLogPre.querySelector("code") || agentLogPre;
    
    const timestamp = new Date().toLocaleTimeString();
    const safeMsg = esc(msg);
    
    codeEl.insertAdjacentHTML("beforeend", `[${timestamp}] ${safeMsg}<br>`);
    const pane = agentLogPre.closest('.term-tab-pane');
    if (pane) pane.scrollTop = pane.scrollHeight;
  }

  function renderStatus(speed, stateStr) {
    renderCtxGauge();
    
    const statusLine = document.getElementById("status-line");
    if (!statusLine) return;
    
    const isStreaming = !!state.streaming || stateStr === "streaming";
    const statusText = stateStr || (isStreaming ? "streaming" : "idle");
    
    const modelName = state.settings.model || "no model loaded";
    
    const ctxUse = computeCtxUsage();
    const ctxLimit = ctxUse.capacity >= 1024 ? Math.round(ctxUse.capacity / 1024) + "k" : ctxUse.capacity;
    const ctxPct = Math.round(ctxUse.pct * 100);
    const ctxCls = ctxUse.pct >= 0.9 ? "is-crit" : (ctxUse.pct >= 0.7 ? "is-warn" : "");
    const ctxUsedTitle = `${ctxUse.used.toLocaleString()} / ${ctxUse.capacity.toLocaleString()} tokens of context used (~${ctxPct}%)`;
    
    let speedText = "- tok/s";
    if (isStreaming) {
      if (speed && speed > 0) {
        speedText = `${Number(speed).toFixed(1)} tok/s`;
      } else if (state._lastTps && state._lastTps > 0) {
        speedText = `${Number(state._lastTps).toFixed(1)} tok/s`;
      }
    } else {
      // Idle state: show average tok/s of the session
      if (state.totalGenDuration && state.totalGenDuration > 0 && state.tokTotal && state.tokTotal > 0) {
        const avg = state.tokTotal / state.totalGenDuration;
        speedText = `${avg.toFixed(1)} tok/s`;
      } else {
        speedText = "- tok/s";
      }
    }
    
    const heartbeatSvg = `
      <svg class="heartbeat-icon" viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
      </svg>
    `;
    
    statusLine.innerHTML = `
      <div class="status-item"><i class="ph ph-cpu"></i><span>${esc(modelName)}</span></div>
      <span class="status-dot">·</span>
      <div class="status-item"><i class="ph ph-database"></i><span>${ctxLimit} ctx</span></div>
      <span class="status-dot">·</span>
      <div class="status-item status-ctx ${ctxCls}" title="${esc(ctxUsedTitle)}"><i class="ph ph-gauge"></i><span>${ctxPct}%</span></div>
      <span class="status-dot">·</span>
      <div class="status-item"><i class="ph ph-lightning"></i><span>${speedText}</span></div>
      <span class="status-dot">·</span>
      <div class="status-item status-state ${isStreaming ? 'is-streaming' : ''}">
        ${heartbeatSvg}
        <span>${statusText}</span>
      </div>
    `;
  }
  // Shared context-window usage math, consumed by both the sidebar radial
  // gauge and the conversation status bar so the two never disagree.
  function computeCtxUsage() {
    // Prefer the live server ctx reported by /api/ctx-stats — settings.num_ctx
    // can drift from how llama-server was actually launched.
    const capacity = Math.max(1, Number(state._ctxCapacity) || Number(state.settings.num_ctx) || 32768);
    // Prefer llama-server's actual reported prompt-token count from the most
    // recent turn. The visible-bubble char count below is blind to tool calls,
    // tool results, and intermediate assistant rounds — for tool-heavy work
    // (firmware, multi-step research) it under-reports by 10x or more.
    const livePromptTokens = Number(state._lastMsgPromptTokens || 0);
    let used, source;
    if (livePromptTokens > 0) {
      used = Math.min(capacity, livePromptTokens);
      // _ctxSource is set by the poll / chat-load seed: "live" = real
      // prompt_eval_count from a completed turn, "tokenize" = exact tokenizer
      // count of the assembled prompt before the first turn has run.
      source = state._ctxSource === "tokenize"
        ? "tokenizer count (no completed turn yet)"
        : "llama-server prompt_eval_count";
    } else {
      const systemPromptChars = 2500;
      const msgChars = (state.messages || []).reduce((a, m) => {
        const content = String(m.content || "");
        const multiplier = m.role === "tool" ? 1.5 : 1.0;
        return a + (content.length * multiplier);
      }, 0);
      const imageOverhead = (state.pendingImages || []).length * 500;
      const totalChars = systemPromptChars + msgChars + imageOverhead;
      used = Math.min(capacity, Math.round(totalChars / 3.0));
      source = "char-count estimate (no live data yet)";
    }
    const pct = Math.min(1, used / capacity);
    return { used, capacity, pct, source };
  }

  function renderCtxGauge() {
    const arc = $("#ctx-gauge-arc");
    const label = $("#ctx-gauge-label");
    if (!arc || !label) return;
    const { used, capacity, pct, source } = computeCtxUsage();
    const circ = 2 * Math.PI * 13;
    arc.setAttribute("stroke-dasharray", circ.toFixed(2));
    arc.setAttribute("stroke-dashoffset", (circ * (1 - pct)).toFixed(2));
    label.textContent = `${Math.round(pct * 100)}%`;
    const gauge = $("#ctx-gauge");
    gauge.classList.toggle("warn", pct >= 0.7 && pct < 0.9);
    gauge.classList.toggle("crit", pct >= 0.9);
    gauge.title = `${used.toLocaleString()} / ${capacity.toLocaleString()} tokens (~${Math.round(pct * 100)}%)\nsource: ${source}`;
  }
  function renderTokTotal() {
    const el = $("#tok-total");
    if (!el) return;
    el.textContent = `${state.tokTotal.toLocaleString()} tok`;
  }

  // ---------- cost savings widget ----------
  // Pricing: $ per 1M tokens — top-tier model from each provider.
  // Uses highest published rate so the "saved" number is conservative.
  // Updated May 2026. Keys are brand names, not model names.
  const CLOUD_PRICING = {
    "openai":    { label: "OpenAI",    input: 30.00, output: 180.00 },  // GPT-5.5 Pro
    "anthropic": { label: "Anthropic", input: 10.00, output:  50.00 },  // Claude Fable 5
    "google":    { label: "Google",    input:  4.00, output:  18.00 },  // Gemini 3.1 Pro (>200K ctx)
    "xai":       { label: "xAI",       input:  2.00, output:   6.00 },  // Grok 4.20
    "deepseek":  { label: "DeepSeek",  input:  1.74, output:   3.48 },  // V4 Pro (standard)
    "mistral":   { label: "Mistral",   input:  2.00, output:   5.00 },  // Magistral Medium
  };

  function calcCost(provider) {
    const p = CLOUD_PRICING[provider];
    if (!p) return 0;
    // Use all-time persistent totals + any live streaming estimate
    const promptTok = state._allTimeTokIn + (state._streamPromptEstimate || 0);
    const outTok = state._allTimeTokOut + (state._streamOutEstimate || 0);
    const inCost  = (promptTok / 1_000_000) * p.input;
    const outCost = (outTok / 1_000_000) * p.output;
    return inCost + outCost;
  }

  // Persist all-time token totals to localStorage
  function _persistAllTimeTok() {
    try {
      localStorage.setItem("accuretta:all-tok-out", String(state._allTimeTokOut));
      localStorage.setItem("accuretta:all-tok-in", String(state._allTimeTokIn));
    } catch {}
  }

  // Calculate session-only cost (uses session token counters, not all-time)
  function calcSessionCost(provider) {
    const p = CLOUD_PRICING[provider];
    if (!p) return 0;
    const promptTok = state.tokPromptTotal + (state._streamPromptEstimate || 0);
    const outTok = state.tokTotal + (state._streamOutEstimate || 0);
    const inCost  = (promptTok / 1_000_000) * p.input;
    const outCost = (outTok / 1_000_000) * p.output;
    return inCost + outCost;
  }

  function renderCostWidget() {
    const el = $("#cost-amount");
    if (!el) return;
    const allTimeCost = calcCost(state.costProvider);
    const sessionCost = calcSessionCost(state.costProvider);
    // Main big number = all-time total
    el.textContent = allTimeCost < 0.005 ? "$0.00" : "$" + allTimeCost.toFixed(2);
    el.classList.toggle("zero", allTimeCost < 0.005);

    // Update the "saved vs" label dynamically with the provider name
    const label = $("#cost-widget .cost-widget-label");
    if (label) {
      const p = CLOUD_PRICING[state.costProvider];
      label.textContent = `Saved Vs ${p ? p.label : state.costProvider} Cost`;
    }

    // Session row
    const sessionEl = $("#cost-session");
    if (sessionEl) {
      sessionEl.textContent = sessionCost < 0.005 ? "$0.00" : "$" + sessionCost.toFixed(2);
    }
    // All-time row
    const alltimeEl = $("#cost-alltime");
    if (alltimeEl) {
      alltimeEl.textContent = allTimeCost < 0.005 ? "$0.00" : "$" + allTimeCost.toFixed(2);
    }
  }

  // ---------- shareable savings card ----------
  function _fmtTokensShort(n) {
    n = Math.max(0, Math.round(n || 0));
    if (n >= 1e6) return +(n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return +(n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  // Build an offscreen 540x540 card styled with the app's own font
  // (var(--font-sans)) so html2canvas captures it, not Claude's default.
  // Fixed espresso palette so the shared image always looks premium
  // regardless of the active theme.
  function _savingsCardEl(logoDataUrl) {
    const provider = CLOUD_PRICING[state.costProvider];
    const saved = calcCost(state.costProvider);
    const savedStr = saved < 0.005
      ? "$0.00"
      : "$" + saved.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const tokens = _fmtTokensShort((state._allTimeTokIn || 0) + (state._allTimeTokOut || 0));
    const sessions = (state.chats && state.chats.order && state.chats.order.length) || 0;
    const providerLabel = provider ? provider.label : state.costProvider;
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:fixed;left:-10000px;top:0;z-index:-1;";
    wrap.innerHTML = `
      <div style="width:540px;height:540px;box-sizing:border-box;background:#2B2722;border-radius:24px;padding:46px;display:flex;flex-direction:column;font-family:var(--font-sans);">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div style="display:flex;align-items:center;gap:11px;">
            ${logoDataUrl
              ? `<img src="${logoDataUrl}" alt="" style="height:30px;width:auto;display:block;">`
              : `<span style="width:14px;height:14px;border-radius:4px;background:#B5544A;display:inline-block;"></span>`}
            <span style="color:#EAE1D0;font-size:18px;font-weight:500;letter-spacing:-0.01em;">accuretta</span>
          </div>
          <span style="color:#8A8170;font-size:13px;font-weight:500;letter-spacing:0.01em;">github.com/mkultraware/accuretta</span>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:2px;">
          <span style="color:#8A8170;font-size:13px;font-weight:500;letter-spacing:0.18em;text-transform:uppercase;">saved by going local</span>
          <span style="color:#67C28C;font-size:82px;font-weight:500;letter-spacing:-0.035em;line-height:1.05;">${savedStr}</span>
          <span style="color:#B5AB95;font-size:17px;margin-top:8px;">vs running the same prompts on ${esc(providerLabel)}</span>
        </div>
        <div style="display:flex;border-top:1px solid #3D372E;padding-top:18px;margin-bottom:20px;">
          <div style="flex:1;"><div style="color:#EAE1D0;font-size:19px;font-weight:500;">${tokens}</div><div style="color:#8A8170;font-size:12px;">tokens run</div></div>
          <div style="flex:1;"><div style="color:#EAE1D0;font-size:19px;font-weight:500;">${sessions}</div><div style="color:#8A8170;font-size:12px;">sessions</div></div>
          <div style="flex:1;"><div style="color:#EAE1D0;font-size:19px;font-weight:500;">$0.00</div><div style="color:#8A8170;font-size:12px;">sent to a cloud</div></div>
        </div>
        <div style="display:flex;align-items:center;">
          <span style="color:#6E6555;font-size:13px;">your model, your machine</span>
        </div>
      </div>`;
    return wrap;
  }

  function _downloadCanvasPng(canvas, name) {
    canvas.toBlob((blob) => {
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Savings card saved", "ok", 2000, "share");
    }, "image/png");
  }

  async function shareSavingsCard() {
    if (typeof window.html2canvas !== "function") {
      toast("Image library hasn't loaded yet — try again in a second.", "warn", 2500);
      return;
    }
    // Inline the logo as a data URI so html2canvas captures it reliably (no
    // mid-render async image load). Falls back to the accent square on failure.
    let logoDataUrl = "";
    try {
      const blob = await (await fetch("/logo-mark-light.png")).blob();
      logoDataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = rej;
        fr.readAsDataURL(blob);
      });
    } catch { logoDataUrl = ""; }
    const wrap = _savingsCardEl(logoDataUrl);
    document.body.appendChild(wrap);
    const card = wrap.firstElementChild;
    try {
      const canvas = await window.html2canvas(card, {
        backgroundColor: "#2B2722",
        scale: 2,
        useCORS: true,
        logging: false,
      });
      const fname = `accuretta-savings-${Date.now()}.png`;
      // Prefer clipboard (one tap to paste into a post); fall back to download.
      if (navigator.clipboard && window.ClipboardItem) {
        await new Promise((resolve) => {
          canvas.toBlob(async (blob) => {
            try {
              await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
              toast("Savings card copied — paste it anywhere", "ok", 2400, "share");
            } catch {
              _downloadCanvasPng(canvas, fname);
            }
            resolve();
          }, "image/png");
        });
      } else {
        _downloadCanvasPng(canvas, fname);
      }
    } catch (e) {
      toast(`Card render failed: ${e.message || e}`, "err", 3500);
    } finally {
      wrap.remove();
    }
  }

  function initCostWidget() {
    // Restore persisted provider selection
    const saved = localStorage.getItem("accuretta:cost-provider");
    if (saved && CLOUD_PRICING[saved]) state.costProvider = saved;
    // Restore all-time persistent token totals
    state._allTimeTokOut = parseInt(localStorage.getItem("accuretta:all-tok-out") || "0", 10) || 0;
    state._allTimeTokIn  = parseInt(localStorage.getItem("accuretta:all-tok-in")  || "0", 10) || 0;
    // Collapsible: start minimized to save vertical space; remember the choice.
    const widget = $("#cost-widget");
    const toggle = $("#cost-widget-toggle");
    if (widget && toggle) {
      const expanded = localStorage.getItem("accuretta:cost-expanded") === "1";
      widget.classList.toggle("collapsed", !expanded);
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      toggle.addEventListener("click", () => {
        const isExpanded = !widget.classList.toggle("collapsed");
        toggle.setAttribute("aria-expanded", isExpanded ? "true" : "false");
        localStorage.setItem("accuretta:cost-expanded", isExpanded ? "1" : "0");
      });
    }
    // Wire up dropdown
    const sel = $("#cost-select");
    if (sel) {
      sel.value = state.costProvider;
      sel.addEventListener("change", () => {
        state.costProvider = sel.value;
        localStorage.setItem("accuretta:cost-provider", state.costProvider);
        renderCostWidget();
      });
    }
    // Wire up the shareable savings card
    const shareBtn = $("#cost-share");
    if (shareBtn) shareBtn.addEventListener("click", shareSavingsCard);
    renderCostWidget();
  }

  // Call after each confirmed token accumulation to update all-time counters
  function _accumulateAllTime(outTok, inTok) {
    if (outTok > 0) state._allTimeTokOut += outTok;
    if (inTok > 0)  state._allTimeTokIn += inTok;
    _persistAllTimeTok();
  }

  // ---------- mobile preview card ----------
  // On mobile, the preview pane is hidden. Instead we inject a tappable
  // artifact card into the chat that opens the generated HTML in a new tab.
  // Accepts either raw HTML string (blob URL) or a wsfs URL for workspace files.
  function injectMobilePreviewCard(opts) {
    if (!isMobile()) return;
    const { filename, size, url, html } = opts || {};
    const name = filename || "index.html";
    const sizeText = size ? humanBytes(size) : "";
    const card = document.createElement("div");
    card.className = "mobile-preview-card";
    card.innerHTML = `
      <div class="mobile-preview-card-icon"><i class="ph ph-browser"></i></div>
      <div class="mobile-preview-card-body">
        <div class="mobile-preview-card-title">${esc(name)}</div>
        <div class="mobile-preview-card-meta">Tap to preview${sizeText ? " · " + sizeText : ""}</div>
      </div>
      <div class="mobile-preview-card-arrow"><i class="ph ph-arrow-square-out"></i></div>`;
    card.addEventListener("click", () => {
      if (url) {
        window.open(url, "_blank");
      } else if (html) {
        const blob = new Blob([html], { type: "text/html" });
        window.open(URL.createObjectURL(blob), "_blank");
      }
    });
    // Insert into the chat flow at the end of the current messages
    const chatInner = $("#chat-inner");
    if (chatInner) {
      chatInner.appendChild(card);
      scrollToBottom(true);
    }
  }

  // Shorten a GGUF filename like "qwen2.5-coder-32b-instruct-q4_k_m.gguf"
  // into a clean, capitalized display label like "Qwen2.5 Coder 32B".
  // Tries to keep the family name, the optional 'coder/instruct/chat' tag,
  // and the parameter count (e.g. 7B, 32B, 70B). Drops quant suffixes,
  // version revisions, and packaging cruft. Falls back to the raw stem.
  function shortenModelName(filename) {
    if (!filename) return "";
    let stem = String(filename).split(/[\\/]/).pop();
    // strip extension
    stem = stem.replace(/\.gguf$|\.bin$|\.safetensors$/i, "");
    // strip common quant + packaging suffixes (everything from -q?_? onward,
    // -imat, -kquants, -gguf, etc.)
    stem = stem.replace(/[._-](?:q\d[a-z0-9_]*|iq\d[a-z0-9_]*|f16|fp16|f32|bf16)\b.*$/i, "");
    stem = stem.replace(/[._-](?:imat|kquants?|gguf|ggml|hf|fixed|fix|merged|abliterated)\b.*$/i, "");
    // collapse separators to spaces
    let parts = stem.split(/[._\-\s]+/).filter(Boolean);
    // keep at most ~5 segments — anything beyond that is usually metadata.
    // We also re-capitalize each segment so "qwen2.5" → "Qwen2.5", "32b" → "32B".
    parts = parts.slice(0, 5).map(p => {
      // parameter count: "32b" / "8x7b" / "1.5b" → uppercase B
      if (/^\d+(?:\.\d+)?b$/i.test(p) || /^\d+x\d+(?:\.\d+)?b$/i.test(p)) return p.toUpperCase();
      // moe-like "a3b": uppercase
      if (/^a\d+(?:\.\d+)?b$/i.test(p)) return p.toUpperCase();
      // bare numbers stay
      if (/^\d/.test(p)) return p.charAt(0).toUpperCase() + p.slice(1);
      // word: capitalize first letter only
      return p.charAt(0).toUpperCase() + p.slice(1).toLowerCase();
    });
    const out = parts.join(" ").trim();
    return out || stem;
  }

  function renderModelPill() {
    const pill = $("#model-pill");
    const nameEl = pill.querySelector(".model-pill-name") || pill;
    const loadedPath = state.loadedModel || state.settings.model_path || state.settings.model || "";
    if (loadedPath) {
      const fullName = String(loadedPath).split(/[\\/]/).pop();
      const shortName = shortenModelName(fullName);
      nameEl.textContent = shortName;
      pill.title = `${fullName} — click to change model`;
    } else if (state.models && state.models.length) {
      nameEl.textContent = "select model";
      pill.title = "Click to pick a model";
    } else {
      nameEl.textContent = "no models";
      pill.title = state.modelsError || "Pick a models folder in Settings";
    }
    // Vision badge — small "eye" chip glued to the pill when the loaded model
    // has its own vision tower (mmproj). Hover tells the user images are
    // going straight to the chat model rather than the OCR fallback.
    let badge = pill.querySelector(".model-pill-vision");
    const wantBadge = !!state.visionCapable && !!loadedPath;
    if (wantBadge && !badge) {
      badge = document.createElement("span");
      badge.className = "model-pill-vision";
      badge.innerHTML = '<i class="ph ph-eye"></i>';
      // Insert before the caret so the order reads name → badge → caret.
      const caret = pill.querySelector(".model-pill-caret");
      if (caret) pill.insertBefore(badge, caret);
      else pill.appendChild(badge);
    } else if (!wantBadge && badge) {
      badge.remove();
      badge = null;
    }
    if (badge) {
      const mm = state.loadedMmproj ? String(state.loadedMmproj).split(/[\\/]/).pop() : "mmproj";
      badge.title = `vision: native — ${mm}`;
    }
  }

  // Reload model list from the bridge, then re-mirror it everywhere it shows
  // (settings dropdown + pill + dropdown menu). Lifted to module scope so any
  // module-level caller (loadModelByPath, autoRetuneOnBoot) can reach it —
  // previously a local closure inside wireEvents which made the others throw.
  async function refreshModels() {
    await loadModels();
    populateSettingsForm();
    renderModelPill();
  }

  // Shared model-load flow — called from the settings <select> AND the new
  // model-pill dropdown. Mirrors auto-tuned values into the settings form so
  // the user can see what got applied (form elements still exist whether the
  // drawer is open or not). Returns true on success.
  async function loadModelByPath(modelPath, opts = {}) {
    if (!modelPath) return false;
    const hint = opts.hint || null;
    const prev = hint?.textContent;
    const tier = Number($("#set-vram-tier")?.value || 0);
    let tuned = null;
    if (tier > 0) {
      if (hint) hint.textContent = "auto-tuning for this model…";
      try {
        const r = await api("/api/llama/auto-tune", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ model_path: modelPath, vram_gb: tier }),
        });
        tuned = r?.suggested || null;
        if (tuned) {
          // RULE: autotune may GROW num_ctx, never shrink it.
          const curCtx = Number($("#set-ctx")?.value || state.settings.num_ctx || 0) || 0;
          const sugCtx = Number(tuned.num_ctx || 0) || 0;
          if (sugCtx > 0 && sugCtx < curCtx) tuned.num_ctx = curCtx;
          const setVal = (id, v) => { const el = $(id); if (el != null && v != null) el.value = String(v); };
          const setSwitch = (id, v) => { const el = $(id); if (!el || v == null) return; el.classList.toggle("on", !!v); };
          setVal("#set-ctx", tuned.num_ctx);
          setVal("#set-gpu", tuned.num_gpu);
          setVal("#set-batch", tuned.num_batch);
          const kv = $("#set-kv"); if (kv && tuned.kv_cache_type) kv.value = tuned.kv_cache_type;
          setVal("#set-ncmoe", tuned.n_cpu_moe);
          setVal("#set-ubatch", tuned.n_ubatch);
          setSwitch("#sw-flash", tuned.flash_attn);
          if (tuned.spec_strategy) setVal("#set-spec-strategy", tuned.spec_strategy);
          const tnotes = $("#autotune-notes");
          if (tnotes && tuned.notes) {
            const lines = [];
            if (tuned.quant_downshift) { lines.push(`>> ${tuned.quant_downshift}`); lines.push(""); }
            lines.push(tuned.notes);
            tnotes.textContent = lines.join("\n");
          }
        }
      } catch (e) {
        console.warn("auto-tune on model change failed:", e);
      }
    }
    if (hint) hint.textContent = "loading model into llama-server...";
    try {
      const persistPayload = {
        model_path: modelPath,
        model: modelPath.split(/[\\/]/).pop().replace(/\.gguf$/i, ""),
      };
      if (tuned) {
        if (tuned.num_ctx != null) persistPayload.num_ctx = Number(tuned.num_ctx);
        if (tuned.num_gpu != null) persistPayload.num_gpu = Number(tuned.num_gpu);
        if (tuned.num_batch != null) persistPayload.num_batch = Number(tuned.num_batch);
        if (tuned.kv_cache_type) persistPayload.kv_cache_type = tuned.kv_cache_type;
        if (tuned.n_cpu_moe != null) persistPayload.n_cpu_moe = Number(tuned.n_cpu_moe);
        if (tuned.n_ubatch != null) persistPayload.n_ubatch = Number(tuned.n_ubatch);
        if (tuned.flash_attn != null) persistPayload.flash_attn = !!tuned.flash_attn;
        if (tuned.spec_strategy) persistPayload.spec_strategy = String(tuned.spec_strategy);
      }
      await saveSettings(persistPayload);
      await api("/api/models/load", {
        method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ path: modelPath }),
      });
      await refreshModels();
      if (tuned) {
        const msg = tuned.quant_downshift
          ? "model loaded — auto-tuned (see quant suggestion in notes)"
          : `model loaded — auto-tuned (ctx ${Number(tuned.num_ctx).toLocaleString()}, n_cpu_moe ${tuned.n_cpu_moe ?? 0})`;
        toast(msg, tuned.quant_downshift ? "warn" : "ok", 4000);
      } else {
        toast("model loaded", "ok", 2500);
      }
      return true;
    } catch (e) {
      if (hint) hint.textContent = prev || "";
      toast("load failed: " + (e.message || e), "error", 6000);
      return false;
    }
  }

  // Build the rows for the model-pill dropdown from state.modelsList. Re-run
  // on every open so the "loaded" indicator stays in sync with whatever the
  // bridge actually has running right now.
  function renderModelMenu() {
    const menu = $("#model-pill-menu");
    if (!menu) return;
    menu.innerHTML = "";
    const list = state.modelsList || [];
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "model-pill-menu-empty";
      empty.textContent = state.modelsError || "pick a models folder in Settings";
      menu.appendChild(empty);
      return;
    }
    const loadedPath = state.loadedModel || state.settings.model_path || "";
    for (const m of list) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "mm-row" + ((m.path === loadedPath || m.loaded) ? " loaded" : "");
      row.dataset.path = m.path;
      row.setAttribute("role", "option");
      row.title = m.path;
      const dot = document.createElement("span");
      dot.className = "mm-row-dot";
      const name = document.createElement("span");
      name.className = "mm-row-name";
      name.textContent = m.name;
      row.appendChild(dot);
      row.appendChild(name);
      if (m.size_gb) {
        const size = document.createElement("span");
        size.className = "mm-row-size";
        size.textContent = `${m.size_gb} GB`;
        row.appendChild(size);
      }
      row.addEventListener("click", async () => {
        const btn = $("#model-pill");
        menu.classList.remove("open");
        btn?.classList.remove("open");
        const allRows = menu.querySelectorAll(".mm-row");
        allRows.forEach(r => r.setAttribute("disabled", "true"));
        try {
          await loadModelByPath(m.path);
        } finally {
          allRows.forEach(r => r.removeAttribute("disabled"));
        }
      });
      menu.appendChild(row);
    }
  }

  // Toggle behaviour for the model-pill dropdown. Mirrors wireOverflow's
  // positioning approach (reparent to <body> to escape any composer-level
  // backdrop-filter / transform, then JS-position each open) but right-
  // aligns to the pill since the pill sits at the bottom-right corner.
  function wireModelMenu() {
    const btn = $("#model-pill");
    const menu = $("#model-pill-menu");
    if (!btn || !menu) return;
    if (menu.parentNode !== document.body) document.body.appendChild(menu);
    function positionMenu() {
      const r = btn.getBoundingClientRect();
      const mw = menu.offsetWidth || 240;
      const mh = menu.offsetHeight || 200;
      const margin = 8;
      const vw = window.innerWidth, vh = window.innerHeight;
      let top = r.top - mh - 6;
      if (top < margin) top = r.bottom + 6;
      if (top + mh > vh - margin) top = Math.max(margin, vh - mh - margin);
      let left = r.right - mw;
      if (left + mw > vw - margin) left = vw - mw - margin;
      if (left < margin) left = margin;
      menu.style.top = `${Math.round(top)}px`;
      menu.style.left = `${Math.round(left)}px`;
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !menu.classList.contains("open");
      if (willOpen) {
        renderModelMenu();
        menu.classList.add("open");
        btn.classList.add("open");
        positionMenu();
        requestAnimationFrame(positionMenu);
      } else {
        menu.classList.remove("open");
        btn.classList.remove("open");
      }
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        menu.classList.remove("open");
        btn.classList.remove("open");
      }
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && menu.classList.contains("open")) {
        menu.classList.remove("open");
        btn.classList.remove("open");
      }
    });
    window.addEventListener("resize", () => { if (menu.classList.contains("open")) positionMenu(); });
    window.addEventListener("scroll", () => { if (menu.classList.contains("open")) positionMenu(); }, true);
  }

  // ---------- mobile tabs ----------
  function applyMobileTab() {
    $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === state.mobileTab));
    app.classList.remove("m-tab-chat", "m-tab-sessions", "m-tab-approvals", "m-tab-settings");
    if (state.mobileTab === "settings") {
      openSettings();
      state.mobileTab = "chat";
      $$(".mobile-tab").forEach(t => t.classList.toggle("active", t.dataset.mtab === "chat"));
      app.classList.add("m-tab-chat");
      return;
    }
    app.classList.add("m-tab-" + state.mobileTab);
  }

  // ---------- event wiring ----------
  function autoResize(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(200, ta.scrollHeight) + "px";
  }

  
  // ===== TOOLBAR OVERFLOW MENU =====
  function wireOverflow(btnSel, menuSel) {
    const btn = $(btnSel);
    const menu = $(menuSel);
    if (!btn || !menu) return;
    // Move menu out of composer/preview-head — those have backdrop-filter/transform
    // which create a containing block for position:fixed children, so the menu
    // would otherwise stay clipped inside its parent. Re-parenting to <body>
    // makes the viewport its containing block, so JS coords actually work.
    if (menu.parentNode !== document.body) {
      document.body.appendChild(menu);
    }
    function positionMenu() {
      const r = btn.getBoundingClientRect();
      const mw = menu.offsetWidth || 180;
      const mh = menu.offsetHeight || 200;
      const margin = 8;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      let top = r.top - mh - 6;
      if (top < margin) top = r.bottom + 6;
      if (top + mh > vh - margin) top = Math.max(margin, vh - mh - margin);
      let left = r.left;
      if (left + mw > vw - margin) left = vw - mw - margin;
      if (left < margin) left = margin;
      menu.style.top = `${Math.round(top)}px`;
      menu.style.left = `${Math.round(left)}px`;
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = !menu.classList.contains("open");
      if (willOpen) {
        menu.classList.add("open");
        // measure AFTER it's displayed, then reposition
        positionMenu();
        // re-measure on next frame in case fonts/content shifted size
        requestAnimationFrame(positionMenu);
      } else {
        menu.classList.remove("open");
      }
    });
    menu.addEventListener("click", (e) => {
      if (e.target.closest(".mm-item, .chip")) menu.classList.remove("open");
    });
    document.addEventListener("click", (e) => {
      if (!menu.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        menu.classList.remove("open");
      }
    });
    window.addEventListener("resize", () => {
      if (menu.classList.contains("open")) positionMenu();
    });
    window.addEventListener("scroll", () => {
      if (menu.classList.contains("open")) positionMenu();
    }, true);
  }
  // On mobile, EVERYTHING goes into the overflow (sliders) menu — Agent,
  // Auto, Image — leaving just `[sliders] [send]` visible inline. The
  // previous "send button is clipping into the sliders chip" complaint was
  // really "there are too many chips fighting for room next to a round
  // send button on a 390px-wide screen". Collapsing to a single trigger
  // sidesteps the problem entirely. IDE stays hidden by CSS (no preview
  // pane at this width). The Build / Network items already live in the
  // menu, so the chips just join them.
  const _MOBILE_TOOLBAR_IDS = ["mode-agent", "mode-auto", "btn-attach-image"];
  function applyMobileToolbarLayout() {
    const tools = document.querySelector(".composer-tools");
    const menu = document.getElementById("toolbar-overflow-menu");
    const wrap = document.querySelector(".toolbar-overflow-wrap");
    if (!tools || !menu || !wrap) return;
    const mobile = isMobile();
    if (mobile) {
      // ensure a "Mode" section label sits at the top of the menu
      let modeLabel = menu.querySelector('.overflow-section-label[data-section="mode"]');
      if (!modeLabel) {
        modeLabel = document.createElement("div");
        modeLabel.className = "overflow-section-label";
        modeLabel.dataset.section = "mode";
        modeLabel.textContent = "Mode";
        menu.insertBefore(modeLabel, menu.firstChild);
      }
      // place each chip immediately after the Mode label, in declared order
      let cursor = modeLabel.nextSibling;
      _MOBILE_TOOLBAR_IDS.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.parentNode === menu && el === cursor) {
          cursor = cursor.nextSibling;
          return;
        }
        menu.insertBefore(el, cursor);
      });
    } else {
      // restore chips to the toolbar, before the overflow wrap, in declared order
      _MOBILE_TOOLBAR_IDS.forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (el.parentNode !== tools) {
          tools.insertBefore(el, wrap);
        }
      });
      const modeLabel = menu.querySelector('.overflow-section-label[data-section="mode"]');
      if (modeLabel) modeLabel.remove();
    }
  }

  function initMobileToolbarOverflow() {
    wireOverflow("#btn-toolbar-overflow", "#toolbar-overflow-menu");
    wireOverflow("#btn-preview-overflow", "#preview-overflow-menu");
    applyMobileToolbarLayout();
  }

  function wireEvents() {
    $("#btn-new-chat").addEventListener("click", newChat);
    $("#btn-settings").addEventListener("click", openSettings);
    $("#btn-settings-m")?.addEventListener("click", openSettings);
    $("#btn-close-settings").addEventListener("click", closeSettings);
    $("#drawer-scrim").addEventListener("click", closeSettings);
    $("#btn-cmd-history")?.addEventListener("click", openCmdHistory);
    $("#btn-close-cmd-history")?.addEventListener("click", closeCmdHistory);
    $("#cmd-history-scrim")?.addEventListener("click", closeCmdHistory);
    $("#btn-cmd-history-refresh")?.addEventListener("click", loadCmdHistory);
    $("#btn-cmd-history-clear")?.addEventListener("click", clearCmdHistory);
    const openFaq = () => { $("#faq-scrim").classList.add("open"); $("#faq-modal").classList.add("open"); };
    const closeFaq = () => { $("#faq-scrim").classList.remove("open"); $("#faq-modal").classList.remove("open"); };
    $("#btn-faq")?.addEventListener("click", openFaq);
    $("#btn-close-faq")?.addEventListener("click", closeFaq);
    $("#faq-scrim")?.addEventListener("click", closeFaq);
    
    const openShutdown = () => { $("#shutdown-scrim")?.classList.add("open"); $("#shutdown-modal")?.classList.add("open"); };
    const closeShutdown = () => { $("#shutdown-scrim")?.classList.remove("open"); $("#shutdown-modal")?.classList.remove("open"); };
    $("#btn-shutdown-side")?.addEventListener("click", openShutdown);
    $$('[data-mm="shutdown"]').forEach(el => el.addEventListener("click", () => { closeMobileMenu(); openShutdown(); }));
    $("#btn-close-shutdown")?.addEventListener("click", closeShutdown);
    $("#shutdown-scrim")?.addEventListener("click", closeShutdown);
    
    $("#btn-shutdown-no-save")?.addEventListener("click", async () => {
      window.__allowClose = true;
      try { await api("/api/shutdown", { method: "POST", body: { save: false } }); } catch (e) {}
      window.close();
    });
    
    $("#btn-shutdown-save")?.addEventListener("click", async () => {
      $("#btn-shutdown-save").disabled = true;
      $("#btn-shutdown-no-save").disabled = true;
      $("#shutdown-loader").classList.remove("hidden");
      try {
        await api("/api/shutdown", { method: "POST", body: { save: true, messages: state.messages } });
      } catch (e) {
        console.error("Shutdown save failed", e);
      }
      window.__allowClose = true;
      
      const loader = $("#shutdown-loader");
      if (loader) {
        loader.innerHTML = "<p style='color: var(--ok); font-weight: 500;'>Done! It is now safe to close this window.</p>";
      }
      
      window.close();
    });

    window.addEventListener("beforeunload", (e) => {
      if (!window.__allowClose && state.messages && state.messages.length > 2) {
        e.preventDefault();
        e.returnValue = "";
        setTimeout(() => {
          if (!window.__allowClose) openShutdown();
        }, 100);
      }
    });

    $("#btn-save-settings").addEventListener("click", collectAndSaveSettings);
    $("#set-theme")?.addEventListener("change", e => {
      const next = e.target.value;
      applyTheme(next);
      saveSettings({ theme: next });
    });
    // Auto-save on toggle (the "Save settings" button isn't the only path, and
    // "save & quit" doesn't flush the form — so persist eagerly or an enabled
    // toggle silently vanishes on restart). Feature gates only; load-time
    // toggles stay on the explicit Save since they trigger a model reload.
    $("#sw-desktop-enabled")?.addEventListener("click", (e) => {
      e.currentTarget.classList.toggle("on");
      saveSettings({ desktop_enabled: e.currentTarget.classList.contains("on") });
      toast("desktop setting saved", "ok", 1500);
    });
    $("#sw-red-team-enabled")?.addEventListener("click", (e) => {
      e.currentTarget.classList.toggle("on");
      saveSettings({ red_team_enabled: e.currentTarget.classList.contains("on") });
      toast("red team setting saved", "ok", 1500);
    });
    // Discord controls auto-save on change (the drawer's Save button isn't the
    // only path, and "save & quit" doesn't flush settings — so persist eagerly
    // or an enabled toggle silently vanishes on restart).
    const _saveDiscord = () => saveSettings({
      discord_enabled: $("#sw-discord-enabled")?.classList.contains("on") || false,
      discord_bot_token: ($("#set-discord-token")?.value || "").trim(),
      discord_owner_id: ($("#set-discord-owner")?.value || "").trim(),
    });
    $("#sw-discord-enabled")?.addEventListener("click", (e) => {
      e.currentTarget.classList.toggle("on");
      _saveDiscord();
      toast("discord settings saved — restart to apply", "ok", 1800);
    });
    $("#set-discord-token")?.addEventListener("change", _saveDiscord);
    $("#set-discord-owner")?.addEventListener("change", _saveDiscord);
    $("#btn-desktop-panic")?.addEventListener("click", async () => {
      try {
        await api("/api/desktop/panic", { method: "POST" });
        toast("desktop automation panicked — all actions blocked", "warn", 4000);
        refreshDesktopStatus();
      } catch (e) { toast("panic failed: " + e.message, "error"); }
    });
    $("#btn-desktop-resume")?.addEventListener("click", async () => {
      try {
        await api("/api/desktop/resume", { method: "POST" });
        toast("desktop automation resumed", "ok", 2500);
        refreshDesktopStatus();
      } catch (e) { toast("resume failed: " + e.message, "error"); }
    });
    $("#sw-web").addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    $("#sw-thinking")?.addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    // advanced llama-server toggles
    $("#sw-flash")?.addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    // (#set-spec-strategy is a <select>, no click handler needed)
    $("#sw-nowarmup")?.addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    $("#sw-metrics")?.addEventListener("click", e => e.currentTarget.classList.toggle("on"));
    // Auto-tune (VRAM picker → suggested flags)
    $("#btn-autotune")?.addEventListener("click", runAutoTune);
    $("#btn-refresh-models").addEventListener("click", async () => {
      const btn = $("#btn-refresh-models");
      btn.disabled = true;
      try { await refreshModels(); } finally { btn.disabled = false; }
    });
    $("#btn-rescan-models-dir")?.addEventListener("click", async () => {
      const btn = $("#btn-rescan-models-dir");
      const path = ($("#set-models-dir")?.value || "").trim();
      if (!path) { toast("pick a models folder first", "warn"); return; }
      btn.disabled = true;
      try {
        await api("/api/models/scan-dir", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path }),
        });
        await refreshModels();
        toast("models folder scanned", "ok", 2000);
      } catch (e) {
        toast("scan failed: " + (e.message || e), "error");
      } finally { btn.disabled = false; }
    });
    // mmproj auto-detect — asks the bridge to look for a sibling vision
    // projector next to whichever model is currently selected/loaded.
    $("#btn-mmproj-detect")?.addEventListener("click", async () => {
      const btn = $("#btn-mmproj-detect");
      const inp = $("#set-mmproj");
      const modelSel = $("#set-model");
      const modelPath = (modelSel?.value || state.loadedModel || state.settings?.model_path || "").trim();
      if (!modelPath) { toast("pick a chat model first", "warn"); return; }
      btn.disabled = true;
      try {
        const r = await api("/api/models/probe-mmproj", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path: modelPath }),
        });
        if (r.mmproj_path) {
          if (inp) inp.value = r.mmproj_path;
          toast("found vision projector — save & relaunch to apply", "ok", 4000);
        } else {
          toast("no mmproj-*.gguf next to this model", "warn", 4000);
        }
      } catch (e) {
        toast("probe failed: " + (e.message || e), "error");
      } finally { btn.disabled = false; }
    });
    $("#btn-mmproj-clear")?.addEventListener("click", () => {
      const inp = $("#set-mmproj");
      if (inp) inp.value = "";
      toast("vision projector cleared — chat model will be text-only after relaunch", "ok", 3500);
    });
    $("#btn-browse-models-dir")?.addEventListener("click", async () => {
      const btn = $("#btn-browse-models-dir");
      btn.disabled = true;
      try {
        const r = await api("/api/browse-folder", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ title: "Pick models folder" }),
        });
        if (!r.path) return;
        $("#set-models-dir").value = r.path;
        await api("/api/models/scan-dir", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path: r.path }),
        });
        await refreshModels();
        toast("models folder set", "ok", 2000);
      } catch (e) {
        toast("browse failed: " + (e.message || e), "error");
      } finally { btn.disabled = false; }
    });
    $("#set-models-dir")?.addEventListener("change", async (e) => {
      const path = (e.target.value || "").trim();
      if (!path) return;
      try {
        await api("/api/models/scan-dir", {
          method: "POST", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ path }),
        });
        await refreshModels();
      } catch (err) {
        toast("scan failed: " + (err.message || err), "error");
      }
    });
    wireModelMenu();
    $("#set-model").addEventListener("change", async () => {
      const sel = $("#set-model");
      const m = sel.value;
      if (!m) return;
      sel.disabled = true;
      try {
        await loadModelByPath(m, { hint: $("#set-model-hint") });
      } finally {
        sel.disabled = false;
      }
    });
    $("#btn-sysctx-rescan").addEventListener("click", rescanSystemContext);
    $("#btn-sysctx-save").addEventListener("click", saveSystemContext);
    $("#btn-theme").addEventListener("click", async () => {
      // Cycle: dark → dim → light → dark. The dim middle option is the
      // OLED-safe pick for users who find pure white too harsh; first
      // click from the dark default lands there instead of jumping
      // straight to bright light.
      const next = nextTheme(state.settings.theme || "light");
      await saveSettings({ theme: next });
      applyTheme(next);
    });
    // Sidebar-foot mirror so the toggle is reachable even if the topbar
    // gets covered, the sidebar is the only thing visible on a narrow
    // window, etc. Delegates to the topbar handler so behaviour stays
    // identical and we keep one source of truth.
    document.getElementById("btn-theme-side")?.addEventListener("click", () => {
      $("#btn-theme")?.click();
    });
    $("#btn-send").addEventListener("click", send);
    $("#btn-stop").addEventListener("click", stopStreaming);
    $("#composer-input").addEventListener("input", e => autoResize(e.target));
    $("#composer-input").addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      if (e.shiftKey) return; // newline
      e.preventDefault();
      send();
    });

    // image attach: click button, paste, drop
    $("#btn-attach-image")?.addEventListener("click", () => $("#file-image").click());

    // trust-writes toggle: auto-approve in-workspace file writes/edits. Styled
    // like the mode chips (.chip.on). Registry/system/PowerShell still prompt.
    const trustBtn = $("#btn-trust-writes");
    if (trustBtn) {
      // .trust-on gets a quiet amber tint (see app.css) so the active state
      // reads as caution rather than a normal accent-lit mode chip.
      const syncTrust = () => trustBtn.classList.toggle("trust-on", !!(state.settings && state.settings.auto_approve_write));
      syncTrust();
      trustBtn.addEventListener("click", async () => {
        const next = !(state.settings && state.settings.auto_approve_write);
        trustBtn.classList.toggle("trust-on", next);
        try { await saveSettings({ auto_approve_write: next }); } catch (_) {}
        syncTrust();
        toast(next ? "Trust writes on — files save and edit without asking. Registry, Windows system folders, and PowerShell still require approval."
                   : "Trust writes off — file writes ask for approval again.",
              next ? "warn" : "info", next ? 5000 : 3000);
      });
    }
    $("#file-image")?.addEventListener("change", async (e) => {
      await addImageFiles(Array.from(e.target.files || []));
      e.target.value = "";
    });
    $("#composer-input").addEventListener("paste", (e) => {
      const items = Array.from(e.clipboardData?.items || []);
      const files = items.filter(i => i.kind === "file" && i.type.startsWith("image/")).map(i => i.getAsFile()).filter(Boolean);
      if (files.length) { e.preventDefault(); addImageFiles(files); }
    });
    const composerEl = document.querySelector(".composer");
    if (composerEl) {
      composerEl.addEventListener("dragover", (e) => { e.preventDefault(); composerEl.classList.add("drag-over"); });
      composerEl.addEventListener("dragleave", () => composerEl.classList.remove("drag-over"));
      composerEl.addEventListener("drop", async (e) => {
        e.preventDefault();
        composerEl.classList.remove("drag-over");
        const files = Array.from(e.dataTransfer?.files || []).filter(f => f.type.startsWith("image/"));
        if (files.length) addImageFiles(files);
      });
    }

    // composer draft auto-save
    $("#composer-input").addEventListener("input", (e) => {
      autoResize(e.target);
      if (state.chatId) {
        localStorage.setItem("accuretta:draft:" + state.chatId, e.target.value);
      }
    });

    // mode chips
    $$('[data-mode]').forEach(b => {
      b.addEventListener("click", () => {
        state.mode = b.dataset.mode;
        $$('[data-mode]').forEach(x => x.classList.remove("on"));
        b.classList.add("on");
      });
    });

    // IDE toolbar: Tailwind CDN toggle
    $("#toggle-tailwind")?.addEventListener("click", async () => {
      const next = !state.settings.use_tailwind_cdn;
      await saveSettings({ use_tailwind_cdn: next });
      reflectIdeToggles();
      if (state.currentHtml) renderPreview();
      toast(next ? "Tailwind CDN will be injected into the preview" : "Tailwind CDN off", "info", 2200, "ide-tw");
    });

    // IDE toolbar: multi-file output toggle
    $("#toggle-multifile")?.addEventListener("click", async () => {
      const next = !state.settings.ide_multifile;
      await saveSettings({ ide_multifile: next });
      reflectIdeToggles();
      toast(next ? "Model will emit multi-file folder structure" : "Single-file mode", "info", 2200, "ide-mf");
    });

    // Network: quick prompt insert for "scan this machine"
    $("#quick-netscan-mothership")?.addEventListener("click", () => {
      const tmpl = "Run a network snapshot on this machine (call network_snapshot). Then: list the active TCP connections grouped by process, flag anything that looks unusual (unknown processes, connections to suspicious IPs/domains, unexpected open ports), summarize the recent DNS queries, and tell me whether anything warrants a closer look.";
      $("#toolbar-overflow-menu")?.classList.remove("open");
      send({ prompt: tmpl, invisible: true });
    });

    // ----- authorized recon / pentest: gate -> target -> invisible prompt -----
    let reconObjective = "recon";
    const reconClose = () => {
      $("#recon-scrim")?.classList.remove("open");
      $("#recon-modal")?.classList.remove("open");
    };
    const reconOpen = (objective) => {
      if (!state.settings || !state.settings.red_team_enabled) {
        toast("Enable Red team tools in Settings first.", "warn", 3000);
        return;
      }
      reconObjective = objective || "recon";
      const access = reconObjective === "gain_access";
      const h3 = $("#recon-modal h3"); if (h3) h3.textContent = access ? "Authorized pentest" : "Authorized recon";
      const go = $("#recon-go"); if (go) go.textContent = access ? "Find a way in" : "Run recon";
      const hint = $("#recon-step-target .recon-hint");
      if (hint) hint.textContent = access
        ? "The model chains recon, exposure checks, weakness identification and access attempts (default creds, exposed services), captures proof, then reports. Only run against systems you are authorized to test."
        : "A bare domain or hostname. The model runs a stealth port scan, TLS audit, HTTP fingerprint, passive subdomain enumeration and DNS recon, then summarizes.";
      $("#recon-step-auth")?.classList.remove("hidden");
      $("#recon-step-target")?.classList.add("hidden");
      const inp = $("#recon-target-input"); if (inp) inp.value = "";
      $("#recon-scrim")?.classList.add("open");
      $("#recon-modal")?.classList.add("open");
    };
    const reconRun = () => {
      const raw = ($("#recon-target-input")?.value || "").trim();
      if (!raw) { toast("Enter a target first.", "warn", 2200); return; }
      const target = raw.replace(/^https?:\/\//i, "").replace(/\/.*$/, "").replace(/\s+/g, "").trim();
      if (!target) { toast("That doesn't look like a domain.", "warn", 2200); return; }
      reconClose();
      const reconTmpl =
        `Authorized reconnaissance on ${target}. I have confirmed I am authorized to test this target. ` +
        `Run, in order: recon_dns (domain "${target}"), recon_subdomains (domain "${target}"), ` +
        `recon_tls_audit (host "${target}"), recon_http_fingerprint (url "${target}"), and ` +
        `recon_port_scan (host "${target}"). ` +
        `Then present a clean recon report with sections: DNS & infrastructure, Subdomains, ` +
        `TLS / certificate posture, HTTP fingerprint, and Open ports. Use markdown tables where they help. ` +
        `End with a short "Findings worth a look" heading. Be concise and factual.`;
      const accessTmpl =
        `Authorized penetration test on ${target}. I have confirmed I am authorized to test this target and I accept responsibility. ` +
        `This is a REAL engagement, NOT a CTF. There is no fixed number of "flags" to find and no CTF scoring. Do NOT invent a flag count, do NOT track progress as "N/3" or "captured X of Y", and do NOT assume a FLAG{...} exists — a FLAG{...} only matters if a tool result literally contains that string. If the target is clean, the correct outcome is "no exploitable findings", not "0 flags". ` +
        `MINDSET — the scope is wide, so go as deep as the target allows. Gaining access is the START of the work, not the end. After EVERY result ask "what does this unlock, and is there a deeper version of it?", and assume there IS more until you have proven there isn't. Test every endpoint and every parameter in scope, not just the first thing that works. Stay strictly NON-DESTRUCTIVE: read, enumerate, and copy proof — never delete, modify, overwrite, deface, DoS, or leave persistence. Be honest both ways: if something is genuinely safe, say so and move on; if it is exploitable, take it as far as it goes. Do not fabricate a finding to look thorough, and do not abandon a real one early.\n` +
        `Work these phases, letting each result drive the next:\n` +
        `1) RECON — recon_dns, recon_subdomains, recon_http_fingerprint, recon_tls_audit, recon_port_scan on ${target}.\n` +
        `2) MAP THE SURFACE — recon_content_discovery (quiet=true) + recon_check_exposure on the web root(s); recon_open_services on the host; recon_subdomain_takeover on subdomains. Run scan_js_secrets on the front-end pages to pull hardcoded API keys, tokens, JWTs, and cloud credentials out of the linked JavaScript (do not skip this — front-end bundles leak keys constantly). Run validate_finding before trusting any exposure — do NOT report decoys, catch-all, or placeholder pages.\n` +
        `3) FIND WEAKNESSES — recon_cve_match on any component versions you see; recon_injection_probe on every URL that takes parameters (or batch_probe to fire one payload set at every parameterized endpoint at once); cors_probe any API endpoint for credentialed cross-origin reads; probe EACH parameter, not just the obvious one.\n` +
        `4) EXPLOIT — breach each finding for real with http_request: decode+flip+re-encode an unsigned cookie and replay it, aim an SSRF param at an internal address, POST an SSTI/injection payload, tamper a header. Use encode_decode for base64/url/hex and jwt_tool to decode/forge/crack a JWT — don't compute crypto by hand. Read every response body, set_cookie, and header.\n` +
        `5) GO DEEPER (post-exploitation) — do NOT stop at "confirmed", loot it. On SQL injection, use sql_injection to enumerate the schema and dump the interesting tables (extract="name FROM sqlite_master", then the real data) — 'OR 1=1' only proves the bug, it doesn't extract the secrets. On file read or RCE, pull configs, source, environment variables, and known secret paths; hunt for keys and credentials. On gained access, enumerate what the new role/session unlocks and pivot from it. On an object/id endpoint, use fuzz to enumerate ids and read the outliers (IDOR). Feed each result into the next move.\n` +
        `6) PROVE IT — for every confirmed access or extracted secret, re-send the winning request with save_evidence set (archives request+response with a sha256), or use recon_capture_evidence.\n` +
        `STEALTH — a signature IDS matches the raw request text, so to stay quiet keep recon low-footprint (quiet=true) and obfuscate payloads (url-encode via encode_decode, vary keyword case, use alternate separators); the server still decodes and executes them while the signature misses.\n` +
        `Only stop when you have genuinely exhausted the scope — every endpoint tested, every real finding taken to its depth. Then write a report: Executive summary (did you get in, how deep, what you reached), What's broken (each finding with severity + evidence), Loot (data, secrets, and credentials extracted), Recommendations. Never claim access or a finding you did not verify with a tool result or a captured artifact — but do not leave a real avenue unexplored. Be factual AND thorough. Once you deliver that report the engagement is CLOSED: treat any later message as a debrief — answer it directly and conversationally, and do NOT run recon or exploit tools again unless the user explicitly tells you to resume or keep testing.`;
      send({ prompt: reconObjective === "gain_access" ? accessTmpl : reconTmpl, invisible: true });
    };
    $("#quick-recon-target")?.addEventListener("click", () => {
      $("#toolbar-overflow-menu")?.classList.remove("open");
      $("#btn-toolbar-overflow")?.classList.remove("open");
      reconOpen("recon");
    });
    $("#quick-gain-access")?.addEventListener("click", () => {
      $("#toolbar-overflow-menu")?.classList.remove("open");
      $("#btn-toolbar-overflow")?.classList.remove("open");
      reconOpen("gain_access");
    });
    $("#recon-auth-no")?.addEventListener("click", reconClose);
    $("#btn-close-recon")?.addEventListener("click", reconClose);
    $("#recon-scrim")?.addEventListener("click", reconClose);
    $("#recon-auth-yes")?.addEventListener("click", () => {
      $("#recon-step-auth")?.classList.add("hidden");
      $("#recon-step-target")?.classList.remove("hidden");
      setTimeout(() => $("#recon-target-input")?.focus(), 50);
    });
    $("#recon-back")?.addEventListener("click", () => {
      $("#recon-step-target")?.classList.add("hidden");
      $("#recon-step-auth")?.classList.remove("hidden");
    });
    $("#recon-go")?.addEventListener("click", reconRun);
    $("#recon-target-input")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); reconRun(); }
    });

    // preview: screenshot the iframe to PNG
    $("#btn-screenshot")?.addEventListener("click", screenshotPreview);

    // preview: export the current preview as a zip (or single .html if no companions)
    $("#btn-export-project")?.addEventListener("click", exportProjectZip);

    // preview: review this UI — capture and attach to composer
    $("#btn-review-ui")?.addEventListener("click", reviewUiAttach);

    // preview toggle
    $("#btn-view-preview").addEventListener("click", () => {
      state.view = "preview";
      $("#btn-view-preview").classList.add("active");
      $("#btn-view-code").classList.remove("active");
      if (state.workspacePreview) { renderWorkspacePreview(); return; }
      renderPreview();
    });
    $("#btn-view-code").addEventListener("click", () => {
      state.view = "code";
      $("#btn-view-code").classList.add("active");
      $("#btn-view-preview").classList.remove("active");
      if (state.workspacePreview) { renderWorkspacePreview(); return; }
      renderPreview();
    });
    $("#btn-refresh").addEventListener("click", () => {
      if (state.workspacePreview) { renderWorkspacePreview(); return; }
      renderPreview();
    });
    $("#btn-open-new").addEventListener("click", () => {
      // 1. Saved version — serve from the versions API
      if (state.activeVersion) {
        window.open(`/api/versions/${state.chatId}/${state.activeVersion}`, "_blank");
        return;
      }
      // 2. Workspace file preview (agent mode write_file) — open via wsfs URL
      const wp = state.workspacePreview;
      if (wp && wp.root && wp.rel) {
        window.open(wsFileUrl(wp.root, wp.rel), "_blank");
        return;
      }
      // 3. Live model-generated HTML not yet saved as a version
      if (state.currentHtml) {
        const blob = new Blob([state.currentHtml], { type: "text/html" });
        window.open(URL.createObjectURL(blob), "_blank");
        return;
      }
    });
    $("#btn-close-preview").addEventListener("click", () => app.classList.add("preview-collapsed"));

    // preview pane resize drag
    const resizer = $("#preview-resizer");
    if (resizer) {
      let dragging = false;
      const endDrag = () => {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove("dragging");
        app.classList.remove("resizing");
        document.body.style.userSelect = "";
        try { resizer.releasePointerCapture?.(resizer._pid); } catch {}
        localStorage.setItem("accuretta:preview-w", app.style.getPropertyValue("--preview-w"));
      };
      resizer.addEventListener("pointerdown", (e) => {
        dragging = true;
        resizer._pid = e.pointerId;
        try { resizer.setPointerCapture(e.pointerId); } catch {}
        resizer.classList.add("dragging");
        app.classList.add("resizing");
        document.body.style.userSelect = "none";
        e.preventDefault();
      });
      resizer.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const w = Math.max(280, Math.min(window.innerWidth - 280, window.innerWidth - e.clientX));
        app.style.setProperty("--preview-w", w + "px");
      });
      resizer.addEventListener("pointerup", endDrag);
      resizer.addEventListener("pointercancel", endDrag);
      window.addEventListener("blur", endDrag);
      const saved = localStorage.getItem("accuretta:preview-w");
      if (saved) app.style.setProperty("--preview-w", saved);
    }
    $("#pull-tab").addEventListener("click", () => app.classList.remove("preview-collapsed"));
    $("#btn-toggle-preview").addEventListener("click", () => app.classList.toggle("preview-collapsed"));

    // sidebar toggles
    $("#btn-toggle-sidebar").addEventListener("click", () => {
      if (isMobile()) {
        state.mobileTab = "chat";
        applyMobileTab();
      } else {
        app.classList.add("sidebar-collapsed");
      }
    });
    $("#btn-toggle-sidebar-m").addEventListener("click", () => {
      if (isMobile()) {
        state.mobileTab = "sessions";
        applyMobileTab();
      } else {
        app.classList.toggle("sidebar-collapsed");
      }
    });
    $("#pull-tab-left").addEventListener("click", () => app.classList.remove("sidebar-collapsed"));

    // workspace add
    $("#btn-ws-add-toggle").addEventListener("click", () => {
      $("#ws-add").classList.toggle("hidden");
      $("#ws-input").focus();
    });
    $("#ws-add-btn").addEventListener("click", addWorkspaceFolder);
    $("#ws-browse-btn").addEventListener("click", async () => {
      const btn = $("#ws-browse-btn");
      btn.disabled = true;
      try {
        const r = await api("/api/browse-folder", { method: "POST", headers: {"Content-Type": "application/json"}, body: "{}" });
        if (r.path) {
          $("#ws-input").value = r.path;
          await addWorkspaceFolder();
        }
      } finally { btn.disabled = false; }
    });
    $("#ws-input").addEventListener("keydown", e => {
      if (e.key === "Enter") { e.preventDefault(); addWorkspaceFolder(); }
    });

    // sessions/workspace split — drag the divider to trade sidebar space
    // between the two panes (long worktrees vs. long session lists).
    // Height persists per-browser; double-click resets to natural sizing.
    (() => {
      const divider = $("#sidebar-resizer");
      const list = $("#chatlist");
      if (!divider || !list) return;
      const KEY = "accuretta:sidebarSplit";
      const apply = (h) => {
        if (h == null) { list.style.height = ""; list.style.flex = ""; return; }
        // flex 0 1 auto keeps the pane shrinkable below the set height so
        // an expanding cost widget or a short window never clips the foot.
        list.style.height = `${h}px`;
        list.style.flex = "0 1 auto";
      };
      const saved = parseInt(localStorage.getItem(KEY), 10);
      if (Number.isFinite(saved)) apply(saved);
      divider.addEventListener("dblclick", () => {
        localStorage.removeItem(KEY);
        apply(null);
      });
      divider.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        divider.setPointerCapture(e.pointerId);
        divider.classList.add("dragging");
        const startY = e.clientY;
        const startH = list.getBoundingClientRect().height;
        const scroll = list.closest(".sidebar-scroll");
        const onMove = (ev) => {
          // reserve room below for the workspace head + a few tree rows
          const max = Math.max(44, scroll.getBoundingClientRect().height - 140);
          apply(Math.max(44, Math.min(max, startH + (ev.clientY - startY))));
        };
        const onUp = () => {
          divider.classList.remove("dragging");
          divider.removeEventListener("pointermove", onMove);
          divider.removeEventListener("pointerup", onUp);
          divider.removeEventListener("pointercancel", onUp);
          const h = parseInt(list.style.height, 10);
          if (Number.isFinite(h)) localStorage.setItem(KEY, String(h));
        };
        divider.addEventListener("pointermove", onMove);
        divider.addEventListener("pointerup", onUp);
        divider.addEventListener("pointercancel", onUp);
      });
    })();

    // mobile tabs (legacy bottom bar, still wired for desktop testing)
    $$('.mobile-tab').forEach(t => t.addEventListener("click", () => {
      state.mobileTab = t.dataset.mtab;
      applyMobileTab();
    }));

    // mobile top-right overflow menu
    const mm = $("#mobile-menu");
    const mmScrim = $("#mobile-menu-scrim");
    const mmBtn = $("#btn-mobile-menu");
    const closeMM = () => { mm.classList.remove("open"); mmScrim.classList.remove("open"); };
    const openMM = () => {
      // Mobile menu shows the NEXT theme as the action label
      // ("Switch to dim", "Switch to light", "Switch to dark") so the
      // user knows what the tap will do, mirroring the desktop cycle.
      const cur = document.documentElement.getAttribute("data-theme") || "light";
      const next = nextTheme(cur);
      const niceName = { dark: "Dark", dim: "Dim", aurora: "Aurora", nebula: "Nebula", operator: "Operator", soft: "Soft", light: "Light" }[next] || next;
      const lbl = $("#mm-theme-label");
      if (lbl) lbl.textContent = `Switch to ${niceName.toLowerCase()}`;
      mm.classList.add("open"); mmScrim.classList.add("open");
    };
    mmBtn?.addEventListener("click", (e) => {
      e.stopPropagation();
      mm.classList.contains("open") ? closeMM() : openMM();
    });
    mmScrim?.addEventListener("click", closeMM);
    $$(".mm-item").forEach(it => it.addEventListener("click", () => {
      const a = it.dataset.mm;
      closeMM();
      if (a === "theme") { $("#btn-theme").click(); return; }
      if (a === "settings") { openSettings(); return; }
      if (a === "faq") { $("#btn-faq")?.click(); return; }
      if (a === "chat" || a === "sessions" || a === "approvals") {
        state.mobileTab = a;
        applyMobileTab();
      }
    }));

    // responsive
    window.addEventListener("resize", () => {
      document.body.classList.toggle("is-mobile", isMobile());
      applyMobileToolbarLayout();
    });

    // ----- mobile swipe-left from sidebar back to chat -----
    // when the sidebar/sessions screen is showing on mobile, a left swipe
    // (>60px horizontal, dominant over vertical) flips back to the chat tab.
    // touchmove fires on the sidebar element only, so vertical scrolling
    // inside the chat list still works normally.
    (function wireSidebarSwipe() {
      const sidebar = document.getElementById("sidebar");
      if (!sidebar) return;
      let startX = 0, startY = 0, tracking = false;
      sidebar.addEventListener("touchstart", (e) => {
        if (!isMobile()) return;
        if (state.mobileTab !== "sessions") return;
        const t = e.touches[0];
        startX = t.clientX;
        startY = t.clientY;
        tracking = true;
      }, { passive: true });
      sidebar.addEventListener("touchend", (e) => {
        if (!tracking) return;
        tracking = false;
        const t = (e.changedTouches && e.changedTouches[0]);
        if (!t) return;
        const dx = t.clientX - startX;
        const dy = t.clientY - startY;
        if (Math.abs(dx) < 60) return;             // not far enough
        if (Math.abs(dy) > Math.abs(dx)) return;   // mostly vertical, ignore
        // either direction returns to chat — sidebar is the "above" layer
        state.mobileTab = "chat";
        applyMobileTab();
      }, { passive: true });
    })();

    // ----- command palette -----
    $("#btn-palette")?.addEventListener("click", openPalette);
    const palInput = $("#palette-input");
    if (palInput) {
      palInput.addEventListener("input", (e) => refreshPaletteList(e.target.value));
      palInput.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { e.preventDefault(); closePalette(); }
        else if (e.key === "ArrowDown") { e.preventDefault(); paletteMove(1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); paletteMove(-1); }
        else if (e.key === "Enter") { e.preventDefault(); paletteCommit(); }
      });
    }
    $("#palette-scrim")?.addEventListener("click", closePalette);

    // ⌘K / Ctrl+K anywhere
    window.addEventListener("keydown", (e) => {
      const isCmd = e.metaKey || e.ctrlKey;
      if (isCmd && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        if (state.palette && state.palette.open) closePalette();
        else openPalette();
      }
    });

    // ----- per-session desktop kill switch -----
    $("#btn-session-desktop")?.addEventListener("click", toggleSessionDesktop);

    // ----- preview extras -----
    $("#btn-save-snapshot")?.addEventListener("click", saveSnapshot);
    $("#btn-save-to-workspace")?.addEventListener("click", saveToWorkspace);
    $("#btn-copy-dataurl")?.addEventListener("click", copyPreviewAsDataUrl);

    // ----- console pane -----
    $("#btn-toggle-console")?.addEventListener("click", () => toggleConsolePane());
    $("#btn-console-clear")?.addEventListener("click", clearConsole);
    $("#btn-console-close")?.addEventListener("click", () => toggleConsolePane(false));

    // ----- viewport presets -----
    $$(".vp-btn").forEach(b => b.addEventListener("click", () => applyViewport(b.dataset.vp)));

    // ----- memories panel -----
    $("#btn-mem-refresh")?.addEventListener("click", loadMemories);
    $("#btn-mem-clear")?.addEventListener("click", async () => {
      if (!confirm("Forget all memories? This cannot be undone.")) return;
      try {
        await api("/api/memories/clear", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      } catch (e) { toast("clear failed: " + e.message, "error"); }
    });
    $("#btn-mem-add")?.addEventListener("click", addMemoryFromInput);
    $("#mem-add-text")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); addMemoryFromInput(); }
    });
    // ----- vertical preview/terminal splitter -----
    const vResizer = $("#preview-v-resizer");
    if (vResizer) {
      let dragging = false;
      const endDrag = () => {
        if (!dragging) return;
        dragging = false;
        vResizer.classList.remove("dragging");
        document.body.style.userSelect = "";
        try { vResizer.releasePointerCapture?.(vResizer._pid); } catch {}
        localStorage.setItem("accuretta:terminal-h", app.style.getPropertyValue("--terminal-h"));
      };
      vResizer.addEventListener("pointerdown", (e) => {
        dragging = true;
        vResizer._pid = e.pointerId;
        try { vResizer.setPointerCapture(e.pointerId); } catch {}
        vResizer.classList.add("dragging");
        document.body.style.userSelect = "none";
        e.preventDefault();
      });
      vResizer.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const bodyRect = $("#preview-body")?.getBoundingClientRect();
        if (bodyRect) {
          const h = Math.max(100, Math.min(bodyRect.height * 0.8, bodyRect.bottom - e.clientY));
          app.style.setProperty("--terminal-h", h + "px");
        }
      });
      vResizer.addEventListener("pointerup", endDrag);
      vResizer.addEventListener("pointercancel", endDrag);
      window.addEventListener("blur", endDrag);
      const saved = localStorage.getItem("accuretta:terminal-h");
      if (saved) app.style.setProperty("--terminal-h", saved);
    }

    // ----- terminal pane tab buttons -----
    document.querySelectorAll(".term-tab").forEach(btn => {
      btn.addEventListener("click", () => {
        const tabId = btn.dataset.tab;
        activateTerminalTab(tabId);
      });
    });

    // ----- clear active terminal console -----
    $("#btn-term-clear")?.addEventListener("click", () => {
      const activeTab = document.querySelector(".term-tab.active");
      if (!activeTab) return;
      const tabId = activeTab.dataset.tab;
      if (tabId === "terminal") {
        const consolePre = document.getElementById("term-console-pre");
        if (consolePre) {
          const codeEl = consolePre.querySelector("code");
          if (codeEl) {
            codeEl.innerHTML = `$ <span class="term-cursor"></span>`;
          }
        }
      } else if (tabId === "pycheck") {
        const banner = document.getElementById("pycheck-banner");
        const codeEl = document.getElementById("pycheck-code")?.querySelector("code");
        if (banner) {
          banner.className = "pycheck-banner pending";
          banner.textContent = "No python check run yet. Run check from a .py file in the workspace.";
        }
        if (codeEl) {
          codeEl.textContent = "";
        }
      } else if (tabId === "agentlog") {
        const logPre = document.getElementById("term-agent-log-pre");
        if (logPre) {
          const codeEl = logPre.querySelector("code") || logPre;
          codeEl.innerHTML = "";
        }
      }
    });

    // ----- collapsible reasoning header click delegation -----
    $("#chat-inner")?.addEventListener("click", (e) => {
      // cascade chip click delegation
      const cascadeBtn = e.target.closest(".cascade-chip");
      if (cascadeBtn) {
        const prompt = cascadeBtn.dataset.prompt;
        if (prompt) send({ prompt, invisible: true });
        const container = cascadeBtn.parentElement;
        if (container) {
          container.style.opacity = "0.5";
          container.style.pointerEvents = "none";
        }
        return;
      }

      const thinkHeader = e.target.closest(".think-header");
      if (!thinkHeader) return;
      const container = thinkHeader.closest(".think-container");
      if (!container) return;
      const content = container.querySelector(".think-content");
      if (!content) return;
      
      const isHidden = content.classList.toggle("hidden");
      const caret = thinkHeader.querySelector(".think-caret");
      if (caret) {
        if (isHidden) {
          caret.className = "ph ph-caret-right think-caret";
        } else {
          caret.className = "ph ph-caret-down think-caret";
        }
      }
    });

    // ----- topbar back navigation chevron -----
    $("#btn-back-chevron")?.addEventListener("click", () => {
      if (isMobile()) {
        state.mobileTab = "sessions";
        applyMobileTab();
      } else {
        app.classList.toggle("sidebar-collapsed");
      }
    });

    // ----- mobile toolbar overflow -----
    initMobileToolbarOverflow();
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
    alert("boot error: " + e.message);
  });
})();
