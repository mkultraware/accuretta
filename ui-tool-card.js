/* SPDX-License-Identifier: AGPL-3.0-only */
/* tool card: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["tool-card"] = ({
  $, TOOL_ICON_MAP, TOOL_SVG, WRENCH_SVG, buildCommandsCardHtml, buildMcpCardHtml,
  buildWritesCardHtml, esc, isCommandTool, previewWorkspaceSource, renderWebSearchChips, shortPath,
  state, toolResultFailed,
}) => {
  // One card per agent turn, always the full width of the reply column, so
  // changing labels never resize it. Collapsed (the default) it is a single
  // line holding the latest action, replaced in place as the agent moves on.
  // Expanded it lists every call: the newest four in view, earlier ones a
  // scroll up. Each row opens to show the call's input and result. The root
  // keeps .tool-group/.activity-group so turn-level code still finds it.
  const TCARD_STATE_SVG = {
    running: '<span class="tcard-spin" aria-hidden="true"></span>',
    done: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 8.4l3 3 6-6.8"/></svg>',
    err: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4.9v3.5M8 11h.01"/></svg>',
    stopped: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><circle cx="8" cy="8" r="6"/><rect x="6" y="6" width="4" height="4" rx=".6" fill="currentColor" stroke="none"/></svg>',
    unchecked: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M5.6 8h4.8"/></svg>',
    waiting: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" aria-hidden="true"><path d="M8 1.9l5 1.9v3.9c0 3.1-2.1 5.3-5 6.4-2.9-1.1-5-3.3-5-6.4V3.8z"/></svg>',
  };
  const TCARD_HEAD_ICONS = 4;

  function tcardIconKey(name) {
    const mcp = /^mcp_([^_]+)_/.exec(name || "");
    if (mcp) return `mcp:${mcp[1]}`;
    return TOOL_ICON_MAP[name]?.run || "wrench";
  }
  // What a tool does, for its icon's tint (workbench.css .tcard-ico[data-cat]).
  // Checked in order, so security tools win over the generic read/run
  // prefixes they share (read_bytes, sandbox_nmap). Unlisted tools (memory,
  // plans, skills) stay neutral.
  const TCARD_CATS = [
    ["sec", /^(recon_|sandbox_(nmap|sqlmap)|yara_|binary_|binwalk_|ghidra_|disasm_|decompile_|scan_|jwt_|sql_injection|fuzz|cors_probe|audit_http|batch_probe|tcp_send|persistence_hunt|parse_ev|analyze_pcap|strings_dump|read_bytes|extract_squashfs|network_snapshot|file_inspect|carve_file|rt_|\w*finding)/],
    ["web", /^(web_|http_request|observe_response)/],
    ["write", /^(write_file|edit_file|patch_file|delete_file|replace_ast_node|remote_write_file|remote_file_|remote_copy_|extract_archive|git_(add|commit|reset|restore|checkout|branch|init))/],
    ["run", /^(run_|sandbox_run|remote_shell|open_program|session_|desktop_|check_syntax|check_deps|git_(push|pull|fetch|clone))/],
    ["read", /^(read_|list_directory|remote_list_directory|remote_read_file|grep_files|find_|project_map|git_(status|log|diff|show|remote))/],
  ];
  function tcardIconCat(name) {
    const hit = TCARD_CATS.find(([, re]) => re.test(name || ""));
    return hit ? hit[0] : "";
  }
  function tcardIconHtml(name) {
    const key = tcardIconKey(name);
    if (key.startsWith("mcp:")) {
      // Each MCP server keeps one hue, hashed from its name, so the same
      // server always reads as the same coin.
      const server = key.slice(4);
      let h = 0;
      for (const ch of server) h = (h * 31 + ch.charCodeAt(0)) % 360;
      return `<span class="tcard-ico tcard-mono" data-cat="mcp" style="--tc-h:${h}" title="${esc(server)}">${esc((server[0] || "?").toUpperCase())}</span>`;
    }
    const cat = tcardIconCat(name);
    return `<span class="tcard-ico"${cat ? ` data-cat="${cat}"` : ""}>${TOOL_SVG[key] || WRENCH_SVG}</span>`;
  }
  function tcardRowState(row) {
    if (row.classList.contains("running")) return row.dataset.waiting === "true" ? "waiting" : "running";
    for (const s of ["err", "stopped", "unchecked"]) if (row.classList.contains(s)) return s;
    return "done";
  }
  function tcardDuration(ms) {
    return ms < 1000 ? `${Math.max(1, Math.round(ms))}ms` : `${(ms / 1000).toFixed(1)}s`;
  }

  // One countable fact about a result for the row's right edge ("4 results",
  // "+12 −3", "exit 1"); empty when nothing countable came back.
  function tcardResultFact(res) {
    if (!res || typeof res !== "object") return "";
    const n = (v, one, many) => `${v} ${v === 1 ? one : many}`;
    if (Array.isArray(res.results)) return n(res.results.length, "result", "results");
    if (Array.isArray(res.entries)) return n(res.entries.length, "item", "items");
    if (Array.isArray(res.rows)) return n(res.rows.length, "row", "rows");
    if (Number.isFinite(res.match_count)) return n(res.match_count, "match", "matches");
    if (Number.isFinite(res.added) || Number.isFinite(res.deleted)) return `+${res.added || 0} −${res.deleted || 0}`;
    const exit = res.exit_code ?? res.exit ?? res.returncode;
    if (exit !== undefined && exit !== null && exit !== "") return `exit ${exit}`;
    return "";
  }

  // The row's right edge as HTML: a leading "+12 −3" in the diff colors.
  function tcardMetaHtml(text) {
    return esc(text).replace(/^\+(\d+) −(\d+)/, '<span class="tc-add">+$1</span> <span class="tc-del">−$2</span>');
  }

  // The failure text for an opened row, with its line breaks kept and the
  // shell's boilerplate dropped so the actual message leads: PowerShell's
  // "At line:1 char:1", the echoed "+ command" and "~~~~" underline, and the
  // CategoryInfo / FullyQualifiedErrorId records.
  const TCARD_ERROR_NOISE = /^\s*(?:At (?:line:\d+ char:\d+|[^\n]*:\d+ char:\d+)|\+\s*~+\s*$|~+\s*$|\+\s*(?:CategoryInfo|FullyQualifiedErrorId)\b|(?:CategoryInfo|FullyQualifiedErrorId)\s*:)/;
  function tcardErrorText(res) {
    const raw = toolFailureRaw(res);
    if (raw == null || typeof raw !== "string") return toolFailureDetail(res);
    const lines = raw.replace(/\r\n?/g, "\n").split("\n");
    const kept = [];
    let echo = false;
    for (const line of lines) {
      // "At line:1 char:1" is followed by "+ <the command>" lines; skip them too.
      if (/^\s*At (?:line:\d+|[^\n]*:\d+) char:\d+/.test(line)) { echo = true; continue; }
      if (echo && /^\s*\+/.test(line)) continue;
      echo = false;
      if (TCARD_ERROR_NOISE.test(line)) continue;
      if (!line.trim() && (!kept.length || !kept[kept.length - 1].trim())) continue;
      kept.push(line.replace(/\s+$/, ""));
    }
    while (kept.length && !kept[kept.length - 1].trim()) kept.pop();
    const text = kept.slice(0, 14).join("\n") + (kept.length > 14 ? "\n…" : "");
    return (text || raw.trim()).slice(0, 1400);
  }

  // The opened row: what was asked (arguments) and what came back. Built on
  // open, so a long turn doesn't carry hidden markup for every call.
  function tcardDetailHtml(row) {
    const parts = [];
    const args = row._args && typeof row._args === "object" ? row._args : {};
    const keys = Object.keys(args).slice(0, 8);
    if (keys.length) {
      parts.push(`<dl class="tcard-kv">${keys.map(k => {
        let v = args[k];
        if (typeof v === "string" && v.includes("\n")) v = `${v.split("\n").length} lines`;
        else if (typeof v !== "string") { try { v = JSON.stringify(v); } catch { v = String(v); } }
        v = String(v ?? "");
        return `<dt>${esc(k)}</dt><dd>${esc(v.length > 220 ? `${v.slice(0, 220)}…` : v)}</dd>`;
      }).join("")}</dl>`);
    }
    const res = row._result;
    if (res !== undefined) {
      if (toolResultFailed(res)) parts.push(`<pre class="tcard-note err">${esc(tcardErrorText(res))}</pre>`);
      else if (row.dataset.name === "web_search" && res?.results?.length) parts.push(renderWebSearchChips(res.results));
      if (res?.code_check?.status === "issues_found" && res.code_check.details) {
        parts.push(`<pre class="tcard-pre">${esc(res.code_check.details)}</pre>`);
      }
      if (row.classList.contains("unchecked") && res?.reason) parts.push(`<p class="tcard-note">${esc(res.reason)}</p>`);
    }
    parts.push(`<p class="tcard-where">${esc(row.dataset.targetLabel || "")}<code>${esc(row.dataset.name || "tool")}</code></p>`);
    return parts.join("");
  }
  function tcardToggleRow(row, open = !row.classList.contains("open")) {
    const more = row.querySelector(".tcard-more");
    const line = row.querySelector(".tcard-line");
    if (!more || !line) return;
    if (open) more.innerHTML = tcardDetailHtml(row);
    more.hidden = !open;
    row.classList.toggle("open", open);
    line.setAttribute("aria-expanded", String(open));
    // Bring opened details into the list's view without scrolling the chat.
    const list = row.closest(".tcard-list");
    if (open && list) {
      const over = row.offsetTop + row.offsetHeight - (list.scrollTop + list.clientHeight) + 6;
      if (over > 0) list.scrollTop += Math.min(over, row.offsetTop - list.scrollTop);
    }
  }

  // Keep the newest row in view while the list is pinned to the bottom; a
  // reader who scrolled up to look at history is left where they are.
  function tcardFollow(list, force = false) {
    if (!list || !(force || list._stick !== false)) return;
    list.scrollTop = list.scrollHeight;
    list._stick = true;
  }

  // Head title: a ticker. A new line slides up and the old one leaves, so a
  // stream of actions reads as one line updating rather than text jumping.
  function tcardTick(group, text, shimmer) {
    const title = group.querySelector(".tcard-title");
    if (!title) return;
    const cur = title.querySelector(".tcard-tick:not(.out)");
    if (cur && cur.textContent === text) {
      cur.firstElementChild?.classList.toggle("shimmer", !!shimmer);
      return;
    }
    const next = document.createElement("span");
    next.className = "tcard-tick";
    next.innerHTML = `<span class="${shimmer ? "shimmer" : ""}">${esc(text)}</span>`;
    if (cur && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      cur.classList.add("out");
      setTimeout(() => cur.remove(), 260);
      next.classList.add("in");
    } else {
      title.textContent = "";
    }
    title.appendChild(next);
  }

  function tcardPaintRow(row) {
    const st = tcardRowState(row);
    if (row.dataset.state === st) return;
    row.dataset.state = st;
    const slot = row.querySelector(":scope > .tcard-line > .tcard-state");
    if (slot) slot.innerHTML = TCARD_STATE_SVG[st];
  }

  // Web search alone: when every call in a card is a web search, the card
  // reads as a small tree: "Ran 3 searches", a row per query with its result
  // count and a Sources row of favicons (which opens the result chips), X and
  // Reddit searches named as such, and "Working Ns" while the model writes.
  const TCARD_SEARCH_SITES = [
    { re: /\bsite:(?:www\.)?(?:x|twitter)\.com\b/i, name: "X", key: "x", icon: "ph-x-logo", unit: "post" },
    { re: /\bsite:(?:www\.|old\.)?reddit\.com\b|^\s*r\/\w+/i, name: "Reddit", key: "reddit", icon: "ph-reddit-logo", unit: "thread" },
  ];
  function tcardDecorateSearch(row) {
    const query = String(row._args?.query || "").trim();
    const site = TCARD_SEARCH_SITES.find(s => s.re.test(query)) || null;
    const shown = (site ? query.replace(/\s*\bsite:\S+/gi, "").trim() : query) || query;
    const results = Array.isArray(row._result?.results) ? row._result.results : null;
    const running = row.classList.contains("running");
    const failed = row.classList.contains("err");
    const sig = `${running ? "r" : failed ? "e" : "d"}|${results ? results.length : "-"}|${query}`;
    if (row.dataset.searchSig === sig) return;
    row.dataset.searchSig = sig;
    const line = row.querySelector(":scope > .tcard-line");
    const icon = line?.querySelector(":scope > .tcard-ico");
    if (icon) icon.outerHTML = `<span class="tcard-ico tcard-search-ico" data-site="${site ? site.key : "web"}">`
      + `<i class="ph ${site ? site.icon : "ph-lightbulb"}" aria-hidden="true"></i></span>`;
    row.querySelector(":scope > .tcard-sources")?.remove();
    if (failed || !query) return;
    const label = line?.querySelector(":scope > .tcard-label");
    if (label) label.innerHTML = site
      ? `${running ? "Searching" : "Searched"} ${site.name} for <code>${esc(shown)}</code>`
      : `${running ? "Searching for" : "Searched for"} ${esc(shown)}`;
    const meta = line?.querySelector(":scope > .tcard-meta");
    if (meta && results) {
      const unit = site ? site.unit : "result";
      meta.textContent = `${results.length} ${unit}${results.length === 1 ? "" : "s"}`;
    }
    if (!results?.length) return;
    const hosts = [...new Set(results.map(r => {
      try { return new URL(r.url).hostname.replace(/^www\./, ""); } catch { return ""; }
    }).filter(Boolean))].slice(0, 5);
    const favs = hosts.map(host => `<span class="tcard-fav" title="${esc(host)}">${TOOL_SVG.globe}`
      + (state.settings.offline_active ? "" : `<img src="https://icons.duckduckgo.com/ip3/${esc(host)}.ico" alt="" loading="lazy" onerror="this.remove()">`)
      + `</span>`).join("");
    line.insertAdjacentHTML("afterend", `<button type="button" class="tcard-sources" aria-label="Show the sources">`
      + `<span>Sources</span><span class="tcard-favs">${favs}</span><i class="ph ph-caret-down" aria-hidden="true"></i></button>`);
  }
  function tcardSearchWorking(group, show) {
    let working = group.querySelector(".tcard-working");
    if (!show) { working?.remove(); return; }
    if (!working) {
      working = document.createElement("div");
      working.className = "tcard-working";
      working.innerHTML = `<i class="ph ph-sparkle" aria-hidden="true"></i><span class="shimmer">Working</span><span class="tcard-working-time"></span>`;
      group.querySelector(".tcard-list")?.appendChild(working);
    }
    const secs = group._t0 ? (Date.now() - group._t0) / 1000 : 0;
    working.querySelector(".tcard-working-time").textContent = `${secs.toFixed(1)}s`;
  }

  function tcardSync(group) {
    if (!group) return;
    const head = group.querySelector(".tcard-head");
    const rows = [...group.querySelectorAll(".tcard-row")];
    rows.forEach(tcardPaintRow);
    const live = group.classList.contains("live-activity");
    const open = !group.classList.contains("collapsed");
    const running = rows.filter(r => r.classList.contains("running"));
    const waiting = running.some(r => r.dataset.waiting === "true");
    const failed = rows.filter(r => r.classList.contains("err")).length;
    const calls = rows.reduce((sum, r) => sum + (Number(r.dataset.count) || 1), 0);
    const latest = running[running.length - 1] || rows[rows.length - 1];
    const searchOnly = rows.length > 0 && rows.every(r => r.dataset.name === "web_search");
    group.classList.toggle("search-only", searchOnly);
    if (searchOnly) {
      rows.forEach(tcardDecorateSearch);
      // The tree is the point, so a live search card opens once by itself.
      if (live && !group.dataset.searchOpened) {
        group.dataset.searchOpened = "1";
        if (!open) { tcardSetOpen(group, true); return; }
      }
    }
    tcardSearchWorking(group, searchOnly && live && !running.length);
    // Collapsed while live, the head IS the latest row: its icon, label,
    // fact and status. Otherwise it summarises with the tools' icons.
    const single = live && !open;
    // One icon per distinct glyph, keeping the first tool name that drew it
    // so the icon gets that tool's tint.
    let names;
    if (single) {
      names = [latest ? latest.dataset.name : ""];
    } else {
      const seen = new Set();
      names = [];
      rows.forEach(r => {
        const k = tcardIconKey(r.dataset.name);
        if (!seen.has(k)) { seen.add(k); names.push(r.dataset.name); }
      });
      names = names.slice(-TCARD_HEAD_ICONS);
    }
    const icons = group.querySelector(".tcard-icons");
    const iconSig = `${single ? 1 : 0}|${names.join(",")}`;
    if (icons && icons.dataset.sig !== iconSig) {
      icons.dataset.sig = iconSig;
      icons.classList.toggle("stack", !single);
      icons.innerHTML = names.map(tcardIconHtml).join("");
    }
    const meta = head.querySelector(".tcard-meta");
    let state = "";
    const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
    if (single) {
      const label = latest?.querySelector(".tcard-label")?.textContent || "Working…";
      tcardTick(group, label, !!latest?.classList.contains("running"));
      meta.innerHTML = running.length > 1 ? `${running.length} in parallel`
        : latest?.querySelector(".tcard-meta")?.innerHTML || "";
      state = latest ? tcardRowState(latest) : "running";
    } else if (live) {
      tcardTick(group, waiting ? "Waiting for approval" : searchOnly ? "Searching the web" : `Using ${plural(calls, "tool")}`, false);
      meta.textContent = running.length > 1 ? `${running.length} in parallel` : "";
      state = waiting ? "waiting" : running.length ? "running" : "";
    } else {
      const used = searchOnly ? `Ran ${calls} search${calls === 1 ? "" : "es"}` : `Used ${plural(calls, "tool")}`;
      tcardTick(group, `${used}${failed ? ` · ${failed} failed` : ""}`, false);
      meta.textContent = group._t0 && group._t1 ? tcardDuration(group._t1 - group._t0) : "";
      state = failed ? "err" : "";
    }
    const slot = head.querySelector(".tcard-state");
    if (slot && slot.dataset.state !== state) {
      slot.dataset.state = state;
      slot.innerHTML = state ? TCARD_STATE_SVG[state] : "";
    }
    group.classList.toggle("has-err", failed > 0);
    const dropped = Number(group.closest(".bubble-row")?.dataset.dropped || 0);
    head.title = dropped > 0 ? `${dropped} older messages were trimmed from context this turn` : "";
  }

  // Running rows show how long they have been going, so a long command reads
  // as alive rather than stuck, and say when it has printed nothing for a
  // minute or more (a buffered or wedged process looks exactly like that).
  // One ticker per card, stopped once nothing in it is running.
  const TCARD_QUIET_MS = 60000;
  function tcardClock(ms) {
    const sec = Math.floor(ms / 1000);
    return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  }
  function tcardTickRunning(group) {
    const now = Date.now();
    let live = 0;
    group.querySelectorAll(".tcard-row.running").forEach(row => {
      const meta = row.querySelector(".tcard-meta");
      if (!meta) return;
      live++;
      const elapsed = now - (Number(row.dataset.t0) || now);
      if (row.dataset.waiting === "true" || elapsed < 2000) { meta.textContent = ""; return; }
      let text = tcardClock(elapsed);
      const lastOut = Number(row.dataset.lastOut) || 0;
      const quiet = now - (lastOut || Number(row.dataset.t0) || now);
      if (isCommandTool(row.dataset.name) && quiet >= TCARD_QUIET_MS) {
        text += lastOut ? ` · no output for ${tcardClock(quiet)}` : " · no output yet";
      }
      meta.textContent = text;
    });
    return live;
  }
  function tcardStartTimer(group) {
    if (!group || group._timer) return;
    group._timer = setInterval(() => {
      const searching = group.classList.contains("search-only") && group.classList.contains("live-activity");
      if (!group.isConnected || (!tcardTickRunning(group) && !searching)) {
        clearInterval(group._timer);
        group._timer = 0;
      }
      tcardSync(group);
    }, 1000);
  }

  function tcardSetOpen(group, open) {
    if (!group) return;
    group.classList.toggle("collapsed", !open);
    group.querySelector(".tcard-head")?.setAttribute("aria-expanded", String(open));
    const caret = group.closest(".bubble-col")?.querySelector(".think-container.has-worklog .think-caret");
    if (caret) caret.className = `ph ph-caret-${open ? "down" : "right"} think-caret`;
    tcardSync(group);
    if (open) requestAnimationFrame(() => tcardFollow(group.querySelector(".tcard-list"), true));
  }

  function wireToolCard(group) {
    if (!group || group.dataset.wired === "true") return;
    group.dataset.wired = "true";
    const activate = (e) => {
      const sources = e.target.closest(".tcard-sources");
      if (sources) { tcardToggleRow(sources.closest(".tcard-row")); return; }
      if (e.target.closest("a, button, summary, .tcard-more")) return;
      const line = e.target.closest(".tcard-line");
      if (line) { tcardToggleRow(line.closest(".tcard-row")); return; }
      if (e.target.closest(".tcard-head")) tcardSetOpen(group, group.classList.contains("collapsed"));
    };
    group.addEventListener("click", activate);
    group.addEventListener("keydown", (e) => {
      if ((e.key !== "Enter" && e.key !== " ") || !e.target.matches(".tcard-head, .tcard-line")) return;
      e.preventDefault();
      activate(e);
    });
    const list = group.querySelector(".tcard-list");
    list?.addEventListener("scroll", () => {
      list._stick = list.scrollHeight - list.scrollTop - list.clientHeight < 8;
      list.classList.toggle("scrolled", list.scrollTop > 2);
    }, { passive: true });
  }

  // Called when the agent turn fully ends (chat_end, stream done, or stop).
  // Settles the card into its "Used N tools" line and moves it after the
  // answer bubble.
  function finalizeToolGroup(row) {
    if (!row) return;
    row._activityFinished = true;
    const stack = row.querySelector(".tool-stack");
    const bubble = row.querySelector(".bubble");
    if (!stack || !bubble) return;

    // Clear active cards from revealer deck
    const deck = $("#revealer-deck");
    if (deck) {
      // Move red-team cards from the deck back to the chat row!
      const rail = deck.querySelector(".attack-rail");
      if (rail) {
        rail.classList.remove("revealer-card");
        if (stack && stack.parentNode) {
          stack.parentNode.insertBefore(rail, stack);
        } else {
          row.appendChild(rail);
        }
      }
      const osint = deck.querySelector(".osint-card");
      if (osint) {
        osint.classList.remove("revealer-card");
        if (stack && stack.parentNode) {
          stack.parentNode.insertBefore(osint, stack);
        } else {
          row.appendChild(osint);
        }
      }
      const secretRail = deck.querySelector(".secret-rail");
      if (secretRail) {
        secretRail.classList.remove("revealer-card");
        if (stack && stack.parentNode) {
          stack.parentNode.insertBefore(secretRail, stack);
        } else {
          row.appendChild(secretRail);
        }
      }

      deck.querySelectorAll('[data-card-type="writes"], [data-card-type="commands"], [data-card-type="mcp"]').forEach(c => c.remove());
      if (deck.children.length === 0) deck.innerHTML = "";
    }

    const group = stack.querySelector(".tcard");
    if (!group) {
      stack.remove();
      return;
    }

    group.classList.remove("live-activity");
    group.classList.add("done");
    tcardSearchWorking(group, false);
    group._t1 = Date.now();
    clearInterval(group._timer);
    group._timer = 0;
    group.querySelectorAll(".tcard-row.running").forEach(line => {
      line.classList.remove("running");
      line.classList.add(row._stopRequested ? "stopped" : "err");
      line.querySelector(".tcard-label")?.classList.remove("shimmer");
      const meta = line.querySelector(".tcard-meta");
      if (meta && !meta.textContent) meta.textContent = "stopped";
    });

    // Under the call list, the richer file/command/MCP cards for anyone who
    // opens the finished work and wants the details.
    const activities = row._activities || { writes: [], commands: [], mcp: [] };
    let html = "";
    html += buildWritesCardHtml(activities.writes, true);
    html += buildCommandsCardHtml(activities.commands, true);
    html += buildMcpCardHtml(activities.mcp, true);
    const extra = group.querySelector(".tcard-extra");
    if (html && extra) {
      extra.innerHTML = html;
      extra.querySelectorAll(".revealer-card").forEach(card => {
        const head = card.querySelector(".revealer-card-head");
        head?.addEventListener("click", () => card.classList.toggle("collapsed"));
        card.querySelectorAll(".btn-preview-file").forEach(btn => {
          btn.addEventListener("click", (e) => {
            e.stopPropagation();
            const path = btn.dataset.path;
            if (path) {
              const root = state.workspace?.folders?.[0] || "";
              const rel = path.replace(/\\/g, "/").replace(root.replace(/\\/g, "/"), "").replace(/^\//, "");
              previewWorkspaceSource(root, rel, rel);
            }
          });
        });
      });
    }

    tcardSetOpen(group, $("#app")?.dataset.activityDetail === "detailed");
    const col = bubble.parentNode;
    if (col && bubble.nextSibling !== stack) col.insertBefore(stack, bubble.nextSibling);
  }

  function ensureToolActivityGroup(stack) {
    if (!stack) return null;
    let group = stack.querySelector(".tcard");
    if (group) return group;
    const open = $("#app")?.dataset.activityDetail === "detailed";
    group = document.createElement("div");
    group.className = `tool-group activity-group tcard live-activity${open ? "" : " collapsed"}`;
    group.innerHTML = `
      <div class="tcard-head" role="button" tabindex="0" aria-expanded="${open}">
        <span class="tcard-icons" aria-hidden="true"></span>
        <span class="tcard-title" aria-live="polite"></span>
        <span class="tcard-meta"></span>
        <span class="tcard-state"></span>
        <i class="ph ph-caret-down tcard-caret" aria-hidden="true"></i>
      </div>
      <div class="tcard-body"><div class="tcard-inner">
        <div class="tcard-list" role="log"></div>
        <div class="tcard-extra"></div>
      </div></div>`;
    stack.appendChild(group);
    wireToolCard(group);
    return group;
  }

  function tcardRowHtml(name, label) {
    return `<div class="tcard-line" role="button" tabindex="0" aria-expanded="false">`
      + tcardIconHtml(name)
      + `<span class="tcard-label shimmer">${esc(label)}</span>`
      + `<span class="tcard-meta"></span>`
      + `<span class="tcard-state"></span>`
      + `<i class="ph ph-caret-down tcard-caret" aria-hidden="true"></i></div>`
      + `<div class="tcard-more" hidden></div>`;
  }

  function startToolActivity(stack, toolCards, evt) {
    const group = ensureToolActivityGroup(stack);
    const list = group?.querySelector(".tcard-list");
    if (!group || !list) return null;
    if (!group._t0) group._t0 = Date.now();
    const key = String(evt.call_id || `activity-${(stack._activitySeq = (stack._activitySeq || 0) + 1)}`);
    const existing = toolCards.get(key);
    if (existing) return existing;
    const label = toolLabel(evt.name, evt.arguments);
    // The same tool on the same file right after a clean finish (a rewrite,
    // a re-read) folds into the previous row as "×N" instead of stacking
    // identical lines. Failures always keep their own row.
    const prev = list.lastElementChild;
    const samePath = String(evt.arguments?.path || "");
    if (prev && prev.classList.contains("tcard-row") && prev.classList.contains("done")
        && samePath && prev.dataset.path === samePath && prev.dataset.name === (evt.name || "tool")) {
      const count = (Number(prev.dataset.count) || 1) + 1;
      prev.dataset.count = String(count);
      prev.dataset.callId = String(evt.call_id || "");
      prev.dataset.t0 = String(Date.now());
      prev.classList.remove("done", "has-code-findings");
      prev.classList.add("running");
      prev._args = evt.arguments || {};
      delete prev._result;
      prev.querySelector(".tcard-meta").textContent = "";
      if (prev.classList.contains("open")) tcardToggleRow(prev, true);
      const prevLabel = prev.querySelector(".tcard-label");
      if (prevLabel) { prevLabel.textContent = label; prevLabel.classList.add("shimmer"); }
      let badge = prev.querySelector(".wb-count");
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "wb-count";
        prevLabel?.after(badge);
      }
      badge.textContent = `×${count}`;
      delete prev.dataset.lastOut;
      toolCards.set(key, prev);
      tcardSync(group);
      tcardStartTimer(group);
      return prev;
    }
    const line = document.createElement("div");
    line.className = "tcard-row running";
    line.dataset.name = evt.name || "tool";
    line.dataset.callId = String(evt.call_id || "");
    line.dataset.path = String(evt.arguments?.path || "");
    line.dataset.t0 = String(Date.now());
    line.dataset.targetLabel = evt.name?.startsWith("mcp_") ? "Connected tool"
      : evt.name?.startsWith("sandbox_") ? "WSL guest"
      : state.executionTarget && state.executionTarget !== "host" ? "Selected remote computer" : "This PC";
    line._args = evt.arguments || {};
    line.innerHTML = tcardRowHtml(evt.name, label);
    list.appendChild(line);
    toolCards.set(key, line);
    // Refresh the head now so the parallel count appears with the first of a
    // batch's calls, not only after the first result lands.
    tcardSync(group);
    tcardFollow(list);
    tcardStartTimer(group);
    return line;
  }

  function findToolActivity(stack, toolCards, evt) {
    const callId = String(evt.call_id || "");
    if (callId && toolCards.has(callId)) return toolCards.get(callId);
    return [...(stack?.querySelectorAll(".tcard-row.running") || [])]
      .find(line => !evt.name || line.dataset.name === evt.name) || null;
  }

  function setToolActivityLabel(stack, toolCards, evt, label) {
    const line = findToolActivity(stack, toolCards, evt);
    if (!line) return;
    line.dataset.waiting = String(/waiting.*approval/i.test(label || ""));
    const text = line.querySelector(".tcard-label");
    if (text && label) text.textContent = label;
    tcardSync(line.closest(".tcard"));
  }

  function finishToolActivity(stack, toolCards, evt) {
    const line = findToolActivity(stack, toolCards, evt);
    if (!line) return null;
    const isErr = toolResultFailed(evt.result);
    const unchecked = evt.name === "check_syntax" && evt.result?.check_status === "unchecked";
    line.classList.remove("running");
    line.classList.add(unchecked ? "unchecked" : isErr ? "err" : "done");
    line.dataset.waiting = "false";
    line._result = evt.result;
    if (!isErr && evt.result?.code_check?.status === "issues_found") line.classList.add("has-code-findings");
    const label = line.querySelector(".tcard-label");
    if (label) {
      label.classList.remove("shimmer");
      label.textContent = toolResultLabel(evt.name, evt.result);
    }
    if (isErr) {
      const detail = toolFailureDetail(evt.result);
      if (detail) line.setAttribute("aria-label", `${label?.textContent || "Tool unavailable"}. Details: ${detail}`);
    }
    const elapsed = line.dataset.t0 ? Date.now() - Number(line.dataset.t0) : 0;
    // A merged "×N" row shows the total time of all its runs.
    const total = (Number(line.dataset.totalMs) || 0) + Math.max(0, elapsed);
    line.dataset.totalMs = String(total);
    const meta = line.querySelector(".tcard-meta");
    if (meta) meta.innerHTML = tcardMetaHtml([tcardResultFact(evt.result), total > 0 ? tcardDuration(total) : ""].filter(Boolean).join(" · "));
    if (line.classList.contains("open")) tcardToggleRow(line, true);
    const callId = String(evt.call_id || "");
    if (callId) toolCards.delete(callId);
    tcardSync(line.closest(".tcard"));
    return line;
  }

  function updateToolGroupHead(stack) {
    tcardSync(stack?.querySelector(".tcard"));
  }

  // File-kind wording for tool labels: "Reading the HTML file index.html…"
  // reads as a sentence, where the bare path reads as a filename dump. The
  // kind is dropped when the extension isn't one we can name honestly.
  const FILE_KIND_LABELS = {
    html: "HTML file", htm: "HTML file", css: "stylesheet", scss: "stylesheet",
    js: "script", mjs: "script", cjs: "script", jsx: "script",
    ts: "script", tsx: "script", py: "Python file", json: "JSON file",
    md: "Markdown file", txt: "text file", yml: "YAML file", yaml: "YAML file",
    toml: "TOML file", ini: "config file", cfg: "config file", conf: "config file",
    ps1: "PowerShell script", bat: "batch script", sh: "shell script",
    sql: "SQL file", log: "log file", lock: "lockfile",
  };

  function fileKindPhrase(path) {
    const name = shortPath(path);
    if (!name) return "";
    const extMatch = String(path || name).match(/\.([A-Za-z0-9]+)$/);
    const kind = extMatch && FILE_KIND_LABELS[extMatch[1].toLowerCase()];
    return kind ? `the ${kind} ${name}` : name;
  }

  // Unknown tools still get a sentence instead of a raw identifier: strip the
  // MCP prefix, turn underscores into spaces, capitalize.
  function humanizeToolName(name) {
    const cleaned = String(name || "tool").replace(/^mcp_[^_]+_/, "").replaceAll("_", " ").trim();
    return cleaned ? cleaned[0].toUpperCase() + cleaned.slice(1) : "Tool";
  }

  function toolLabel(name, args) {
    args = args || {};
    // MCP tools register as `mcp_<server>_<tool>`; show "server · tool" instead.
    // The MCP badge in the tool head is what marks the MCP origin.
    if (name && name.startsWith("mcp_")) {
      const m = name.match(/^mcp_([^_]+)_(.+)$/);
      const server = m ? m[1] : "";
      const tool = m ? m[2] : name.slice(4);
      return server ? `${server} · ${tool}…` : `${tool}…`;
    }
    switch (name) {
      case "list_directory": return `Looking in ${shortPath(args.path) || "folder"}…`;
      case "read_file":      return `Reading ${fileKindPhrase(args.path)}…`;
      case "read_skeleton":  return `Reading the structure of ${fileKindPhrase(args.path)}…`;
      case "project_map":    return `Mapping ${shortPath(args.path) || "project"}…`;
      case "find_files":     return `Searching ${shortPath(args.path) || "files"}${args.pattern ? ` for ${String(args.pattern).slice(0, 48)}` : ""}…`;
      case "grep_files":     return `Searching ${shortPath(args.path) || "files"}${args.pattern ? ` for ${String(args.pattern).slice(0, 48)}` : ""}…`;
      case "write_file":     return `Writing ${fileKindPhrase(args.path)}…`;
      case "edit_file":      return `Editing ${fileKindPhrase(args.path)}…`;
      case "replace_ast_node": return `Editing the structure of ${fileKindPhrase(args.path)}…`;
      case "delete_file":    return `Deleting ${fileKindPhrase(args.path)}…`;
      case "run_powershell": return `Running on host…`;
      case "run_tests":      return `Running project tests…`;
      case "check_syntax":   return `Checking ${fileKindPhrase(args.path)}…`;
      case "research_plan": return "Planning the investigation…";
      case "research_note": return "Saving an evidence note…";
      case "research_notebook": return "Reading saved research…";
      case "research_publish": return "Preparing the research presentation…";
      case "pin_note":       return `Pinning a session note…`;
      case "unpin_note":     return `Unpinning a session note…`;
      case "remember":       return `Saving that to memory…`;
      case "search_memories": return `Searching saved memories…`;
      case "edit_memory":    return `Updating a saved memory…`;
      case "forget":         return `Removing a saved memory…`;
      case "load_skill":     return `Loading skill${args.name ? ` ${args.name}` : ""}…`;
      case "save_skill":     return `Saving skill${args.name ? ` ${args.name}` : ""}…`;
      case "list_more_tools": return `Looking for more tools…`;
      case "compact_history": return `Compacting the conversation…`;
      case "git_status":     return `Checking repository status…`;
      case "git_log":        return `Reading repository history…`;
      case "git_diff":       return `Reviewing repository changes…`;
      case "git_show":       return `Inspecting a commit…`;
      case "git_commit":     return `Committing changes…`;
      case "git_push":       return `Pushing to the remote…`;
      case "git_pull":       return `Pulling from the remote…`;
      case "git_fetch":      return `Fetching from the remote…`;
      case "git_add":        return `Staging changes…`;
      case "git_branch":     return `Creating a branch…`;
      case "git_checkout":   return `Switching branches…`;
      case "git_remote":     return `Checking remotes…`;
      case "git_clone":      return `Cloning the repository…`;
      case "git_init":       return `Initializing a repository…`;
      case "git_restore":    return `Restoring files…`;
      case "git_reset":      return `Resetting the repository…`;
      case "update_plan":    return `Updating the task plan…`;
      case "capability_report": return `Checking available capabilities…`;
      case "switch_execution_target": return `Switching command target…`;
      case "sandbox_run":    return `Running in WSL guest…`;
      case "session_start":  return `Starting an interactive session…`;
      case "session_list":   return `Listing sessions…`;
      case "session_send":   return `Sending input to the session…`;
      case "session_read":   return `Reading session output…`;
      case "session_stop":   return `Stopping the session…`;
      case "remote_shell":   return `Running on Mac…`;
      case "remote_read_file": return `Reading ${fileKindPhrase(args.path)} on Mac…`;
      case "remote_list_directory": return `Looking in ${shortPath(args.path) || "a folder"} on Mac…`;
      case "remote_file_abort": return `Cancelling the Mac file transfer…`;
      case "remote_write_file": return `Writing ${fileKindPhrase(args.path)} on Mac…`;
      case "remote_file_begin": return `Preparing ${fileKindPhrase(args.path)} on Mac…`;
      case "remote_file_append": return `Sending the next file chunk to Mac…`;
      case "remote_file_commit": return `Finishing the Mac file…`;
      case "remote_copy_to": return `Copying ${shortPath(args.destination)} to Mac…`;
      case "remote_copy_from": return `Copying ${shortPath(args.source)} from Mac…`;
      case "open_program":   return `Opening ${args.name || args.path || "program"}…`;
      case "web_fetch":      return `Fetching ${args.url || "the web"}…`;
      case "web_search":     return `Searching the web${args.query ? ` for ${String(args.query).slice(0, 56)}` : ""}…`;
      case "spawn_subagent": {
        const n = Array.isArray(args.tasks) ? args.tasks.length : 0;
        return n ? `Dispatching ${n} sub-agent${n === 1 ? "" : "s"}…` : "Dispatching sub-agents…";
      }
      case "web_image_search": return `Searching for images${args.query ? ` of ${String(args.query).slice(0, 48)}` : ""}…`;
      case "network_snapshot": return `Scanning network…`;
      case "parse_event_logs": return `Reading Windows event logs…`;
      case "persistence_hunt": return `Hunting for persistence…`;
      case "record_finding": return `Recording a finding…`;
      case "list_findings":  return `Listing findings…`;
      case "describe_screen": return `Looking at the screen…`;
      case "screenshot":     return `Taking a screenshot…`;
      case "list_windows":   return `Listing open windows…`;
      case "desktop_snapshot": return `Inspecting the active window…`;
      case "desktop_click":  return `Clicking on screen…`;
      case "desktop_drag":   return `Dragging on screen…`;
      case "desktop_open_url": return `Opening a page in the browser…`;
      case "view_image":     return `Looking at an image…`;
      case "show_image":     return args.path ? `Showing ${shortPath(args.path)}…` : `Capturing the screen to show you…`;
      case "desktop_type_text": return `Typing on screen…`;
      case "desktop_press_keys": return `Pressing keys…`;
      case "desktop_invoke": return `Activating a control…`;
      case "desktop_set_value": return `Setting a field value…`;
      case "desktop_scroll": return `Scrolling the window…`;
      case "desktop_launch_app": return `Launching an app…`;
      case "desktop_focus_window": return `Focusing a window…`;
      case "desktop_close_window": return `Closing a window…`;
      case "scan_apk":         return `Scanning APK${args.path ? " " + shortPath(args.path) : ""}…`;
      case "decompile_apk":    return `Decompiling APK${args.path ? " " + shortPath(args.path) : ""}…`;
      case "ghidra_analyze":   return `Analyzing with Ghidra${args.path ? " · " + shortPath(args.path) : ""}…`;
      case "binary_inspect":   return `Inspecting binary${args.path ? " · " + shortPath(args.path) : ""}…`;
      case "yara_scan":        return `Scanning with YARA${args.path ? " · " + shortPath(args.path) : ""}…`;
      default:               return `${humanizeToolName(name)}…`;
    }
  }
  function toolResultLabel(name, res) {
    res = res || {};
    if (toolResultFailed(res)) return toolFailureLabel(name, res);
    if (res.code_check?.status === "issues_found") return "Edit saved · Code check found issues";
    switch (name) {
      case "list_directory": {
        const n = (res.entries || []).length;
        return `Found ${n} item${n === 1 ? "" : "s"}${res.path ? " in " + shortPath(res.path) : ""}`;
      }
      case "read_file":      return `Read ${fileKindPhrase(res.path)}${res.bytes != null ? ` (${res.bytes} bytes)` : ""}`;
      case "read_skeleton":  return `Read the structure of ${fileKindPhrase(res.path)}`;
      case "project_map":    return `Mapped the project`;
      case "find_files":     return `Found ${Number(res.count || 0)} matching file${Number(res.count || 0) === 1 ? "" : "s"}`;
      case "grep_files":     return `Found ${Number(res.match_count || 0)} match${Number(res.match_count || 0) === 1 ? "" : "es"} in ${Number(res.files_scanned || 0)} file${Number(res.files_scanned || 0) === 1 ? "" : "s"}`;
      case "write_file":     return `Wrote ${fileKindPhrase(res.path)}`;
      case "edit_file":      return `Edited ${fileKindPhrase(res.path)} · ${res.edits_applied || 0} change${(res.edits_applied || 0) === 1 ? "" : "s"}`;
      case "delete_file":    return `Deleted ${fileKindPhrase(res.path)}`;
      case "check_syntax":   return res.check_status === "unchecked" ? "Syntax not checked" : "Syntax check passed";
      case "research_plan": return "Research questions saved";
      case "research_note": return `Evidence note ${res.note_id || ""} saved`.replace(/\s+/g, " ");
      case "research_notebook": return "Read saved research";
      case "research_publish": return res.presentation_ready ? "Research presentation ready" : "Research publication checked";
      case "git_status":     return `Checked repository status`;
      case "git_log":        return `Read repository history`;
      case "git_diff":       return `Reviewed repository changes`;
      case "update_plan":    return `Updated the task plan`;
      case "capability_report": return `Checked available capabilities`;
      case "switch_execution_target": return `Commands now target ${res.target_label || "the selected device"}`;
      case "remote_shell":   return `Mac command completed`;
      case "remote_read_file": return res.path ? `Read ${fileKindPhrase(res.path)} on Mac` : "Read a file on Mac";
      case "remote_list_directory": return `Looked in ${shortPath(res.path) || "a folder"} on Mac`;
      case "remote_file_abort": return `Cancelled the Mac file transfer`;
      case "remote_write_file": return `Wrote ${fileKindPhrase(res.path)} on Mac${res.bytes != null ? ` (${res.bytes} bytes)` : ""}`;
      case "remote_file_begin": return `Mac file ready for chunks`;
      case "remote_file_append": return `Sent chunk ${res.chunks || ""}${res.bytes != null ? ` · ${res.bytes} bytes total` : ""}`;
      case "remote_file_commit": return `Finished ${shortPath(res.path)} on Mac${res.bytes != null ? ` (${res.bytes} bytes)` : ""}`;
      case "remote_copy_to": return `Copied ${shortPath(res.destination)} to Mac`;
      case "remote_copy_from": return `Copied ${shortPath(res.destination)} from Mac`;
      case "run_powershell": {
        const out = (res.stdout || "").trim();
        const first = out.split(/\r?\n/)[0] || "(no output)";
        return `Host done · ${first.slice(0, 120)}`;
      }
      case "run_tests": {
        const summary = res.result || res.details || {};
        return summary.passed ? "Host tests passed" : "Host tests failed";
      }
      case "sandbox_run": {
        const out = (res.output || "").trim();
        const first = out.split(/\r?\n/)[0] || "(no output)";
        return `WSL guest done · ${first.slice(0, 120)}`;
      }
      case "open_program":   return `Opened ${res.name || ""}`;
      case "web_fetch":      return `Fetched ${shortPath(res.url)}`;
      case "web_search":     return `Found ${(res.results || []).length} web result${(res.results || []).length === 1 ? "" : "s"}`;
      case "web_image_search": return `Found ${(res.results || []).length} image${(res.results || []).length === 1 ? "" : "s"}`;
      case "show_image":     return `Showed you ${res.image?.caption ? `“${String(res.image.caption).slice(0, 60)}”` : "a picture"}`;
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
      default:               return `${humanizeToolName(name)} finished`;
    }
  }

  function toolFailureLabel(name, result = {}) {
    if (name === "check_syntax") return "Syntax check failed";
    if (result.expected_type === "directory" && result.actual_type === "file") return "Expected a folder, received a file";
    if (/not a directory:/i.test(String(result.error || ""))) return "Expected a folder path";
    if (/path outside workspace/i.test(String(result.error || ""))) return "Path is outside the selected workspace";
    if (name === "tool_unavailable" || name === "tools_unavailable") {
      const missing = result.tool || result.name || result.requested_name;
      return missing ? `${String(missing).replaceAll("_", " ")} is unavailable` : "Requested tool is unavailable";
    }
    if (name && name.startsWith("mcp_")) {
      const match = name.match(/^mcp_[^_]+_(.+)$/);
      const tool = (match ? match[1] : name.slice(4)).replaceAll("_", " ");
      return `${tool || "Connected tool"} unavailable`;
    }
    switch (name) {
      case "web_fetch": return "Web fetch unavailable";
      case "web_search": return "Web search unavailable";
      case "web_image_search": return "Image search unavailable";
      case "run_powershell":
      case "sandbox_run":
      case "remote_shell": return "Command did not complete";
      case "run_tests": return "Tests did not complete";
      case "read_file":
      case "read_skeleton": return "File could not be read";
      case "list_directory": return "Folder could not be listed";
      case "project_map": return "Project map could not be read";
      case "write_file":
      case "edit_file":
      case "delete_file": return "File action did not complete";
      default: {
        const readable = String(name || "action").replaceAll("_", " ");
        return `${readable[0].toUpperCase()}${readable.slice(1)} could not be completed`;
      }
    }
  }

  // The first failure field with text in it. A blank stderr must not hide the
  // output after it, or the exit code of a command that ran and returned non-zero.
  function toolFailureRaw(res) {
    return [res?.error, res?.message, res?.reason, res?.unavailable_reason,
      res?.stderr, res?.output, res?.details, res?.detail]
      .find(v => v != null && (typeof v !== "string" || v.trim() !== ""));
  }

  function toolFailureDetail(res) {
    res = res || {};
    const raw = toolFailureRaw(res);
    if (raw == null) {
      const code = res.exit_code ?? res.exit;
      return code != null ? `Exited with code ${code}.` : "The requested tool could not be run.";
    }
    if (typeof raw === "string") return raw.slice(0, 600);
    try { return JSON.stringify(raw).slice(0, 600); } catch { return String(raw).slice(0, 600); }
  }
  return {
    tcardSync, tcardSetOpen, finalizeToolGroup, startToolActivity, findToolActivity,
    setToolActivityLabel, finishToolActivity, updateToolGroupHead, toolLabel, toolResultLabel,
  };
};
