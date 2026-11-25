/* SPDX-License-Identifier: AGPL-3.0-only */
/* unified-diff rendering (```diff fences): moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["diff-render"] = ({ esc }) => {
  // Parse a unified diff once and render two layouts into one card: a split
  // (before | after) grid and a single unified column. CSS shows split above the
  // 600px breakpoint and unified below. Big or very wide diffs render unified
  // everywhere, since half-width columns are miserable for long lines.
  function parseDiffRows(raw) {
    const src = String(raw).replace(/\n+$/, "").split("\n");
    const rows = [];
    let oldN = 1, newN = 1;
    for (const line of src) {
      if (/^@@/.test(line)) {
        const m = line.match(/@@\s*-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s*@@/);
        if (m) { oldN = +m[1]; newN = +m[2]; }
        rows.push({ t: "hunk", text: line });
        continue;
      }
      if (/^(diff --git |index |--- |\+\+\+ )/.test(line)) continue;   // file headers
      const c = line[0];
      if (c === "+") { rows.push({ t: "add", text: line.slice(1), n: newN }); newN++; }
      else if (c === "-") { rows.push({ t: "del", text: line.slice(1), o: oldN }); oldN++; }
      else { rows.push({ t: "ctx", text: c === " " ? line.slice(1) : line, o: oldN, n: newN }); oldN++; newN++; }
    }
    return rows;
  }

  const _dc = (txt) => esc(txt) || "​";   // escaped code cell; zero-width so empty lines keep height

  function renderDiffUnified(rows) {
    return rows.map(r => {
      if (r.t === "hunk")
        return `<div class="dl dl-hunk"><span class="dl-g"></span><span class="dl-g"></span><span class="dl-m"></span><span class="dl-c">${esc(r.text)}</span></div>`;
      if (r.t === "add")
        return `<div class="dl dl-add"><span class="dl-g"></span><span class="dl-g">${r.n}</span><span class="dl-m">+</span><span class="dl-c">${_dc(r.text)}</span></div>`;
      if (r.t === "del")
        return `<div class="dl dl-del"><span class="dl-g">${r.o}</span><span class="dl-g"></span><span class="dl-m">−</span><span class="dl-c">${_dc(r.text)}</span></div>`;
      return `<div class="dl"><span class="dl-g">${r.o}</span><span class="dl-g">${r.n}</span><span class="dl-m"></span><span class="dl-c">${_dc(r.text)}</span></div>`;
    }).join("");
  }

  function renderDiffSplit(rows) {
    const out = [];
    let del = [], add = [];

    const getSimilarity = (s1, s2) => {
      const t1 = (s1 || "").trim();
      const t2 = (s2 || "").trim();
      if (!t1 && !t2) return 1.0;
      if (!t1 || !t2) return 0.0;
      if (t1 === t2) return 1.0;

      const m = t1.length;
      const n = t2.length;
      const dp = Array(n + 1).fill(0);
      for (let j = 0; j <= n; j++) dp[j] = j;

      for (let i = 1; i <= m; i++) {
        let prev = dp[0];
        dp[0] = i;
        for (let j = 1; j <= n; j++) {
          const temp = dp[j];
          if (t1[i - 1] === t2[j - 1]) {
            dp[j] = prev;
          } else {
            dp[j] = Math.min(prev + 1, dp[j] + 1, dp[j - 1] + 1);
          }
          prev = temp;
        }
      }
      return 1.0 - (dp[n] / Math.max(m, n));
    };

    const renderRow = (l, r) => {
      return `<div class="dl-row">` +
        (l ? `<div class="dl dl-l dl-del"><span class="dl-g">${l.o}</span><span class="dl-m">−</span><span class="dl-c">${_dc(l.text)}</span></div>`
           : `<div class="dl dl-l dl-empty"></div>`) +
        (r ? `<div class="dl dl-r dl-add"><span class="dl-g">${r.n}</span><span class="dl-m">+</span><span class="dl-c">${_dc(r.text)}</span></div>`
           : `<div class="dl dl-r dl-empty"></div>`) +
        `</div>`;
    };

    const flush = () => {
      const N = del.length;
      const M = add.length;
      if (N === 0 && M === 0) return;

      if (N === 0) {
        for (let j = 0; j < M; j++) {
          out.push(renderRow(null, add[j]));
        }
        add = [];
        return;
      }
      if (M === 0) {
        for (let i = 0; i < N; i++) {
          out.push(renderRow(del[i], null));
        }
        del = [];
        return;
      }

      // Compute similarity matrix
      const sim = Array(N).fill(null).map(() => Array(M).fill(0));
      for (let i = 0; i < N; i++) {
        for (let j = 0; j < M; j++) {
          sim[i][j] = getSimilarity(del[i].text, add[j].text);
        }
      }

      // Needleman-Wunsch style DP alignment
      const gapPenalty = -0.2;
      const dp = Array(N + 1).fill(null).map(() => Array(M + 1).fill(0));

      for (let i = 1; i <= N; i++) dp[i][0] = i * gapPenalty;
      for (let j = 1; j <= M; j++) dp[0][j] = j * gapPenalty;

      for (let i = 1; i <= N; i++) {
        for (let j = 1; j <= M; j++) {
          const s = sim[i - 1][j - 1];
          const matchScore = s >= 0.45 ? s : -0.5;
          dp[i][j] = Math.max(
            dp[i - 1][j - 1] + matchScore,
            dp[i - 1][j] + gapPenalty,
            dp[i][j - 1] + gapPenalty
          );
        }
      }

      const alignment = [];
      let i = N, j = M;
      while (i > 0 || j > 0) {
        if (i > 0 && j > 0) {
          const s = sim[i - 1][j - 1];
          const matchScore = s >= 0.45 ? s : -0.5;
          const score = dp[i][j];
          if (Math.abs(score - (dp[i - 1][j - 1] + matchScore)) < 1e-9) {
            alignment.push({ l: del[i - 1], r: add[j - 1] });
            i--; j--;
            continue;
          }
        }
        if (i > 0 && Math.abs(dp[i][j] - (dp[i - 1][j] + gapPenalty)) < 1e-9) {
          alignment.push({ l: del[i - 1], r: null });
          i--;
        } else {
          alignment.push({ l: null, r: add[j - 1] });
          j--;
        }
      }

      alignment.reverse();
      for (const cell of alignment) {
        out.push(renderRow(cell.l, cell.r));
      }

      del = []; add = [];
    };

    for (const r of rows) {
      if (r.t === "hunk") {
        flush();
        out.push(`<div class="dl-row dl-hunk-row"><div class="dl dl-hunk"><span class="dl-c">${esc(r.text)}</span></div></div>`);
        continue;
      }
      if (r.t === "del") { del.push(r); continue; }
      if (r.t === "add") { add.push(r); continue; }
      flush();
      out.push(`<div class="dl-row">` +
        `<div class="dl dl-l"><span class="dl-g">${r.o}</span><span class="dl-m"></span><span class="dl-c">${_dc(r.text)}</span></div>` +
        `<div class="dl dl-r"><span class="dl-g">${r.n}</span><span class="dl-m"></span><span class="dl-c">${_dc(r.text)}</span></div>` +
        `</div>`);
    }
    flush();
    return out.join("");
  }

  function renderDiffCard(raw, filename) {
    const rows = parseDiffRows(raw);
    const codeRows = rows.filter(r => r.t !== "hunk");
    const adds = codeRows.filter(r => r.t === "add").length;
    const dels = codeRows.filter(r => r.t === "del").length;
    const maxLen = codeRows.reduce((m, r) => Math.max(m, (r.text || "").length), 0);
    const tooBig = codeRows.length > 500 || maxLen > 100;   // wide/long -> unified everywhere
    const fname = filename || "diff";
    const head =
      `<div class="diff-head"><i class="ph ph-git-diff" aria-hidden="true"></i>` +
      `<span class="diff-file">${esc(fname)}</span>` +
      `<span class="diff-stat"><span class="diff-plus">+${adds}</span> <span class="diff-minus">−${dels}</span></span>` +
      `<button type="button" class="cc-act cc-copy" title="Copy diff"><i class="ph ph-copy"></i></button></div>`;
    const rawHolder = `<textarea class="diff-raw" hidden>${esc(raw)}</textarea>`;
    const unified = `<div class="diff-unified">${renderDiffUnified(rows)}</div>`;
    if (tooBig) return `<div class="diff-card only-unified">${head}${rawHolder}${unified}</div>`;
    return `<div class="diff-card">${head}${rawHolder}<div class="diff-split">${renderDiffSplit(rows)}</div>${unified}</div>`;
  }
  return { renderDiffCard };
};
