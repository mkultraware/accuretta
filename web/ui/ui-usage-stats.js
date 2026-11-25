/* SPDX-License-Identifier: AGPL-3.0-only */
/* usage stats: profile picture + share card: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["usage-stats"] = ({
  $, persistUiPreference, renderSettingsProfile, state, toast, usageCompactNumber, usageDuration,
}) => {
  // A profile picture unlocks Share, which copies a card of the local usage
  // (tokens, autonomous time, longest run, the activity matrix and a link to
  // download Accuretta). Drawn straight onto a canvas: the theme tokens use
  // color-mix(), which html2canvas cannot parse.
  function renderUsageProfile() {
    const url = state.settings?.profile_avatar || "";
    const btn = $("#usage-avatar");
    if (!btn) return;
    const img = $("#usage-avatar-img");
    if (img) {
      img.hidden = !url;
      if (url) { if (img.getAttribute("src") !== url) img.src = url; }
      else img.removeAttribute("src");
    }
    btn.classList.toggle("has-photo", !!url);
    btn.title = url ? "Change your profile picture" : "Add a profile picture";
    btn.setAttribute("aria-label", btn.title);
    const share = $("#usage-share");
    if (share) {
      share.setAttribute("aria-disabled", String(!url));
      share.title = url ? "Copy a card of your local AI usage to the clipboard" : "";
    }
    const remove = $("#usage-avatar-remove");
    if (remove) remove.hidden = !url;
    renderSettingsProfile();
  }

  // Center-crop to a 192px square JPEG so the picture stays small in settings.
  async function usageAvatarFromFile(file) {
    const objectUrl = URL.createObjectURL(file);
    try {
      const img = await new Promise((resolve, reject) => {
        const el = new Image();
        el.onload = () => resolve(el);
        el.onerror = () => reject(new Error("the file is not a readable image"));
        el.src = objectUrl;
      });
      const size = 192;
      const side = Math.min(img.naturalWidth, img.naturalHeight);
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = size;
      canvas.getContext("2d").drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, size, size);
      return canvas.toDataURL("image/jpeg", 0.86);
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  }

  function initUsageProfile() {
    const btn = $("#usage-avatar");
    const input = $("#usage-avatar-file");
    if (!btn || !input) return;
    btn.addEventListener("click", () => input.click());
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.value = "";
      if (!file) return;
      try {
        const dataUrl = await usageAvatarFromFile(file);
        await persistUiPreference({ profile_avatar: dataUrl });
        renderUsageProfile();
        toast("Profile picture saved. Share is unlocked.", "ok", 2200);
      } catch (e) {
        toast("Could not use that image: " + (e.message || e), "err", 3500);
      }
    });
    $("#usage-avatar-remove")?.addEventListener("click", async () => {
      await persistUiPreference({ profile_avatar: "" });
      renderUsageProfile();
    });
    $("#usage-share")?.addEventListener("click", () => {
      if (!state.settings?.profile_avatar) {
        // Locked: nudge the tooltip that explains how to unlock it.
        const wrap = $(".usage-share-wrap");
        wrap?.classList.remove("nudge");
        void wrap?.offsetWidth;
        wrap?.classList.add("nudge");
        clearTimeout(wrap?._nudgeTimer);
        if (wrap) wrap._nudgeTimer = setTimeout(() => wrap.classList.remove("nudge"), 2600);
        return;
      }
      shareUsageCard();
    });
    renderUsageProfile();
  }

  // The share card wears the active theme, so a card reads like the app it
  // came from. Theme token -> [r, g, b]: a probe resolves var() and
  // color-mix() chains, then a 1px canvas turns any color syntax into bytes.
  let usageColorCtx = null;
  function usageCardColor(varName, fallback) {
    const probe = document.createElement("span");
    probe.style.cssText = `position:absolute;visibility:hidden;color:var(${varName}, ${fallback})`;
    document.body.appendChild(probe);
    const value = getComputedStyle(probe).color;
    probe.remove();
    if (!usageColorCtx) usageColorCtx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
    usageColorCtx.clearRect(0, 0, 1, 1);
    usageColorCtx.fillStyle = fallback;
    usageColorCtx.fillStyle = value;
    usageColorCtx.fillRect(0, 0, 1, 1);
    const [r, g, b] = usageColorCtx.getImageData(0, 0, 1, 1).data;
    return [r, g, b];
  }
  const usageRgb = (c, alpha = 1) => alpha >= 1 ? `rgb(${c[0]}, ${c[1]}, ${c[2]})` : `rgba(${c[0]}, ${c[1]}, ${c[2]}, ${alpha})`;
  const usageMix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
  function usageLuminance(c) {
    const [r, g, b] = c.map(v => {
      const s = v / 255;
      return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  function usageContrast(a, b) {
    const la = usageLuminance(a), lb = usageLuminance(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  }

  // Whole Monday-first weeks for the card's calendar: it starts on the first
  // Monday and days still to come this week stay null, so the grid is a clean
  // rectangle instead of a ragged first and last column.
  function usageCardWeeks(days) {
    if (!days.length) return [];
    const first = new Date(`${days[0].day}T12:00:00`);
    const skip = (7 - ((first.getDay() + 6) % 7)) % 7;
    const weeks = [];
    for (let i = skip; i < days.length; i += 7) {
      const week = [];
      for (let d = 0; d < 7; d += 1) week.push(days[i + d] || null);
      weeks.push(week);
    }
    return weeks;
  }

  // Copied confirmation: a thumbnail of the card that was just copied tilts
  // in under Share, an accent check draws itself, then it all fades away.
  function showUsageCopied(blob) {
    const wrap = $(".usage-share-wrap");
    if (!wrap) { toast("Copied! Just paste it on social media to share.", "ok", 2600, "share"); return; }
    let pop = $("#usage-copied");
    if (!pop) {
      pop = document.createElement("div");
      pop.id = "usage-copied";
      pop.className = "usage-copied";
      pop.setAttribute("role", "status");
      pop.setAttribute("aria-live", "polite");
      wrap.appendChild(pop);
    }
    if (pop._url) URL.revokeObjectURL(pop._url);
    pop._url = URL.createObjectURL(blob);
    pop.innerHTML = `<img class="usage-copied-thumb" src="${pop._url}" alt="">`
      + `<div class="usage-copied-text"><span class="usage-copied-head">`
      + `<svg class="usage-copied-check" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="10"></circle><path d="M7.5 12.5l3 3 6-6.5"></path></svg>`
      + `Copied!</span><span>Just paste it on social media to share.</span></div>`;
    pop.classList.remove("show");
    void pop.offsetWidth;
    pop.classList.add("show");
    wrap.classList.add("copied");
    clearTimeout(pop._timer);
    pop._timer = setTimeout(() => {
      pop.classList.remove("show");
      wrap.classList.remove("copied");
    }, 3400);
  }

  async function shareUsageCard() {
    const data = state._usageData;
    if (!data) { toast("Usage stats are still loading.", "info", 1800); return; }
    const totals = data.totals || {};
    const tokens = (Number(totals.input_tokens ?? data.savings?.tok_in) || 0) + (Number(totals.output_tokens ?? data.savings?.tok_out) || 0);
    const days = Array.isArray(data.activity?.days) ? data.activity.days : [];
    const activeDays = days.filter(day => day.turns > 0).length;
    const since = totals.since ? new Date(totals.since * 1000).toLocaleDateString(undefined, { month: "long", year: "numeric" }) : "";

    // the active theme's palette, adjusted so small type stays legible
    const bg = usageCardColor("--bg", "#111");
    const fg = usageCardColor("--fg", "#eee");
    // light text on a darker ground; retro's mid-grey with dark ink is light
    const dark = usageLuminance(fg) > usageLuminance(bg);
    const muted = usageCardColor("--fg-muted", dark ? "#aaa" : "#555");
    const accent = usageCardColor("--accent", "#7c6cff");
    const raised = usageCardColor("--bg-raised", "#1a1a1a");
    const c = {
      bg, fg, muted, accent,
      backdrop: dark ? usageMix(bg, [0, 0, 0], 0.45) : usageMix(bg, fg, 0.08),
      panel: usageContrast(raised, bg) < 1.04 ? usageMix(bg, fg, dark ? 0.045 : 0.035) : raised,
      line: usageMix(bg, fg, dark ? 0.12 : 0.13),
      faint: usageMix(usageCardColor("--fg-faint", "#666"), muted, 0.4),
      ink: usageContrast(accent, bg) < 3 ? usageMix(accent, fg, 0.5) : accent,
    };
    const css = getComputedStyle(document.body);
    const family = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
    const sans = family("--font-sans", `"Inter Tight", "Segoe UI", sans-serif`);
    const mono = family("--font-mono", `"JetBrains Mono", Consolas, monospace`);
    const display = family("--font-display", sans);
    try {
      await Promise.all([`600 30px ${sans}`, `500 14px ${sans}`, `600 120px ${display}`, `500 12px ${mono}`]
        .map(font => document.fonts.load(font)));
    } catch {}
    const loadImage = src => new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = src;
    });
    const [logo, avatar] = await Promise.all([
      loadImage("/assets/brand/logo-mark-dark.png").catch(() => null),
      loadImage(state.settings.profile_avatar).catch(() => null),
    ]);

    // 16:9 so X, LinkedIn and Discord show it uncropped. Two columns on
    // purpose: the numbers on the left, a profile plate on the right.
    const W = 1200, H = 675, S = 2, M = 24;
    const L = 72, LR = 720;
    const PX = 768, PY = 48, PW = 384, PH = H - PY * 2;
    const PC = PX + PW / 2;
    const canvas = document.createElement("canvas");
    canvas.width = W * S;
    canvas.height = H * S;
    const ctx = canvas.getContext("2d");
    ctx.scale(S, S);
    const text = (str, x, y, size, weight, color, { align = "left", font = sans, spacing = 0, alpha = 1 } = {}) => {
      ctx.font = `${weight} ${size}px ${font}`;
      ctx.fillStyle = usageRgb(color, alpha);
      ctx.textAlign = align;
      ctx.letterSpacing = `${spacing}px`;
      ctx.fillText(str, x, y);
      const width = ctx.measureText(str).width;
      ctx.letterSpacing = "0px";
      return width;
    };
    const hairline = (x0, y0, x1, y1) => {
      ctx.fillStyle = usageRgb(c.line);
      ctx.fillRect(x0, y0, Math.max(1, x1 - x0), Math.max(1, y1 - y0));
    };
    const glow = (x, y, radius, color, alpha) => {
      const gradient = ctx.createRadialGradient(x, y, 0, x, y, radius);
      gradient.addColorStop(0, usageRgb(color, alpha));
      gradient.addColorStop(1, usageRgb(color, 0));
      ctx.fillStyle = gradient;
      ctx.fillRect(x - radius, y - radius, radius * 2, radius * 2);
    };

    // backdrop: every pixel painted (transparent corners paste as white),
    // with a faint dot matrix around the card
    ctx.fillStyle = usageRgb(c.backdrop);
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = usageRgb(c.fg, dark ? 0.075 : 0.09);
    for (let y = 6; y < H; y += 12) {
      for (let x = 6; x < W; x += 12) {
        ctx.beginPath();
        ctx.arc(x, y, 0.7, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    // the card, lifted off the backdrop
    const card = new Path2D();
    card.roundRect(M, M, W - M * 2, H - M * 2, 28);
    ctx.save();
    ctx.shadowColor = `rgba(0, 0, 0, ${dark ? 0.5 : 0.14})`;
    ctx.shadowBlur = 40;
    ctx.shadowOffsetY = 14;
    ctx.fillStyle = usageRgb(c.bg);
    ctx.fill(card);
    ctx.restore();
    ctx.save();
    ctx.clip(card);
    glow(L + 190, 250, 420, c.accent, dark ? 0.1 : 0.07);
    ctx.restore();
    ctx.strokeStyle = usageRgb(c.line);
    ctx.lineWidth = 1;
    ctx.stroke(card);

    // ---- left column: who made it, the number, the year, the tagline ----
    if (logo) ctx.drawImage(logo, L, 64, 40 * logo.naturalWidth / logo.naturalHeight, 40);
    text("Accuretta", L + 70, 96, 30, 600, c.fg, { spacing: -0.6 });

    text("TOKENS PROCESSED LOCALLY", L, 166, 11, 500, c.faint, { font: mono, spacing: 2.2 });
    text(usageCompactNumber(tokens), L - 5, 284, 132, 600, c.fg, { font: display, spacing: -5 });
    const claim = text("All on my own machine.", L, 326, 20, 500, c.muted);
    text("No cloud, no API bill.", L + claim + 7, 326, 20, 500, c.ink);

    // the calendar: whole weeks, Monday on top, one column per week
    const weeks = usageCardWeeks(days);
    const gridTop = 402;
    const step = weeks.length ? (LR - L + 3) / weeks.length : 12;
    const cell = step - 3;
    const values = days.map(day => Number(day.turns) || 0);
    const peak = Math.max(1, ...values);
    const base = usageMix(c.bg, c.fg, dark ? 0.08 : 0.075);
    const levels = [base, usageMix(base, c.accent, 0.3), usageMix(base, c.accent, 0.54), usageMix(base, c.accent, 0.78), c.accent];
    const levelOf = (value, top) => value ? Math.max(1, Math.ceil(Math.sqrt(value / top) * 4)) : 0;
    let lastMonth = -1;
    let lastLabel = -9;
    weeks.forEach((week, col) => {
      const x = L + col * step;
      const monday = week[0] && new Date(`${week[0].day}T12:00:00`);
      if (monday && monday.getMonth() !== lastMonth) {
        if (lastMonth !== -1 && col - lastLabel >= 3) {
          text(monday.toLocaleDateString(undefined, { month: "short" }).toUpperCase(), x, gridTop - 10, 9.5, 500, c.faint, { font: mono, spacing: 1.2 });
          lastLabel = col;
        }
        lastMonth = monday.getMonth();
      }
      week.forEach((day, row) => {
        const y = gridTop + row * step;
        ctx.beginPath();
        ctx.roundRect(x, y, cell, cell, Math.min(3, cell * 0.25));
        if (!day) {
          // still to come this week: an outline keeps the rectangle whole
          ctx.strokeStyle = usageRgb(c.line);
          ctx.lineWidth = 1;
          ctx.stroke();
          return;
        }
        ctx.fillStyle = usageRgb(levels[levelOf(Number(day.turns) || 0, peak)]);
        ctx.fill();
      });
    });
    const gridBottom = gridTop + step * 7 - 3;
    text("PAST YEAR", L, gridBottom + 24, 9.5, 500, c.faint, { font: mono, spacing: 1.6 });
    const moreW = text("MORE", LR, gridBottom + 24, 9.5, 500, c.faint, { align: "right", font: mono, spacing: 1.2 });
    const swatch = 10;
    const legendX = LR - moreW - 10 - levels.length * (swatch + 3) + 3;
    levels.forEach((level, i) => {
      ctx.fillStyle = usageRgb(level);
      ctx.beginPath();
      ctx.roundRect(legendX + i * (swatch + 3), gridBottom + 15, swatch, swatch, 2);
      ctx.fill();
    });
    text("LESS", legendX - 10, gridBottom + 24, 9.5, 500, c.faint, { align: "right", font: mono, spacing: 1.2 });

    hairline(L, 548, LR, 549);
    text("YOUR MODEL. YOUR MACHINE.", L, 584, 13, 600, c.fg, { font: mono, spacing: 3 });
    text("github.com/mkultraware/accuretta", L, 606, 13, 500, c.muted, { font: mono });

    // ---- right column: the profile plate ----
    const plate = new Path2D();
    plate.roundRect(PX, PY, PW, PH, 20);
    ctx.fillStyle = usageRgb(c.panel);
    ctx.fill(plate);
    ctx.strokeStyle = usageRgb(c.line);
    ctx.stroke(plate);
    ctx.save();
    ctx.clip(plate);
    const AX = PC, AY = 186, AR = 78, RR = 102;
    glow(AX, AY, 170, c.accent, dark ? 0.16 : 0.1);
    ctx.restore();

    // the year ring: 52 dots round the picture, one per week, oldest at the
    // bottom left and running clockwise to this week, with a gap below right
    const ring = weeks.slice(-52).map(week => week.reduce((sum, day) => sum + (Number(day?.turns) || 0), 0));
    const ringPeak = Math.max(1, ...ring);
    const ringBase = usageMix(c.panel, c.fg, dark ? 0.16 : 0.18);
    const ringLevels = [ringBase, usageMix(ringBase, c.accent, 0.35), usageMix(ringBase, c.accent, 0.6), usageMix(ringBase, c.accent, 0.82), c.accent];
    const arcStart = 200, arcSpan = 300;
    const polar = (degrees, radius) => {
      const a = (degrees - 90) * Math.PI / 180;
      return [AX + Math.cos(a) * radius, AY + Math.sin(a) * radius];
    };
    ctx.beginPath();
    ctx.arc(AX, AY, RR, (arcStart - 90) * Math.PI / 180, (arcStart + arcSpan - 90) * Math.PI / 180);
    ctx.strokeStyle = usageRgb(c.line);
    ctx.lineWidth = 1;
    ctx.stroke();
    ring.forEach((value, i) => {
      const level = levelOf(value, ringPeak);
      const [x, y] = polar(arcStart + arcSpan * i / Math.max(1, ring.length - 1), RR);
      ctx.fillStyle = usageRgb(ringLevels[level]);
      ctx.beginPath();
      ctx.arc(x, y, level ? 2.2 + level * 0.6 : 1.7, 0, Math.PI * 2);
      ctx.fill();
    });
    if (ring.length) {
      // this week, circled
      const [x, y] = polar(arcStart + arcSpan, RR);
      ctx.beginPath();
      ctx.arc(x, y, 7.5, 0, Math.PI * 2);
      ctx.strokeStyle = usageRgb(c.accent);
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    const [gapX, gapY] = polar(arcStart + arcSpan + (360 - arcSpan) / 2, RR);
    text("52 WK", gapX, gapY + 3.5, 9, 600, c.faint, { align: "center", font: mono, spacing: 1.4 });

    // the picture, cropped square from its middle so it never stretches
    ctx.beginPath();
    ctx.arc(AX, AY, AR + 5, 0, Math.PI * 2);
    ctx.fillStyle = usageRgb(c.panel);
    ctx.fill();
    if (avatar) {
      const side = Math.min(avatar.naturalWidth, avatar.naturalHeight);
      ctx.save();
      ctx.beginPath();
      ctx.arc(AX, AY, AR, 0, Math.PI * 2);
      ctx.clip();
      ctx.drawImage(avatar, (avatar.naturalWidth - side) / 2, (avatar.naturalHeight - side) / 2, side, side, AX - AR, AY - AR, AR * 2, AR * 2);
      ctx.restore();
    }
    ctx.beginPath();
    ctx.arc(AX, AY, AR + 1.5, 0, Math.PI * 2);
    ctx.strokeStyle = usageRgb(c.accent);
    ctx.lineWidth = 3;
    ctx.stroke();

    if (since) {
      text("RUNNING LOCAL SINCE", PC, 328, 9.5, 500, c.faint, { align: "center", font: mono, spacing: 1.8 });
      text(since, PC, 352, 19, 600, c.fg, { align: "center", spacing: -0.3 });
    }
    hairline(PX + 32, 378, PX + PW - 32, 379);
    const stats = [
      [usageDuration(totals.elapsed_ms), "working on its own"],
      [data.longest ? usageDuration(data.longest.elapsed_ms) : "—", "longest solo run"],
      [usageCompactNumber(totals.turns), "model turns"],
      [String(activeDays), "active days"],
    ];
    stats.forEach(([value, label], i) => {
      const x = PX + 32 + (i % 2) * (PW - 64) / 2 + (i % 2 ? 18 : 0);
      const y = 422 + Math.floor(i / 2) * 66;
      text(value, x, y, 28, 600, c.fg, { spacing: -0.7 });
      text(label, x, y + 21, 12.5, 500, c.muted);
    });
    hairline(PC, 394, PC + 1, 512);
    hairline(PX + 32, 528, PX + PW - 32, 529);

    // where to get it: a scannable code and a line of plain words
    try {
      if (typeof qrcode === "function") {
        const qr = qrcode(0, "M");
        qr.addData("https://github.com/mkultraware/accuretta");
        qr.make();
        const qrImg = await loadImage(qr.createDataURL(4, 8));
        const QS = 64, QX = PX + PW - 32 - QS, QY = PH + PY - 24 - QS;
        ctx.fillStyle = "#fff";
        ctx.beginPath();
        ctx.roundRect(QX, QY, QS, QS, 9);
        ctx.fill();
        if (!dark) {
          ctx.strokeStyle = usageRgb(c.line);
          ctx.lineWidth = 1;
          ctx.stroke();
        }
        ctx.drawImage(qrImg, QX + 4, QY + 4, QS - 8, QS - 8);
        text("Run your own", PX + 32, QY + 27, 17, 600, c.fg, { spacing: -0.3 });
        text("Scan to download Accuretta", PX + 32, QY + 48, 12.5, 500, c.muted);
      }
    } catch {}
    const blob = await new Promise(resolve => canvas.toBlob(resolve, "image/png"));
    if (!blob) { toast("Could not render the card.", "err", 3000); return; }
    try {
      if (!navigator.clipboard || !window.ClipboardItem) throw new Error("no clipboard");
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      showUsageCopied(blob);
    } catch {
      // Same fallback as the savings card: save the PNG instead.
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `accuretta-usage-${Date.now()}.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Clipboard unavailable, so the card was saved as a PNG.", "info", 3000, "share");
    }
  }
  return { renderUsageProfile, initUsageProfile };
};
