/* SPDX-License-Identifier: AGPL-3.0-only */
/* shareable savings card: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["savings-card"] = ({
  API_RATE_NOTE, CLOUD_PRICING, _fmtSince, calcCost, esc, state, toast,
}) => {
  function _fmtTokensShort(n) {
    n = Math.max(0, Math.round(n || 0));
    if (n >= 1e6) return +(n / 1e6).toFixed(2) + "M";
    if (n >= 1e3) return +(n / 1e3).toFixed(1) + "k";
    return String(n);
  }

  // Build an offscreen 540x540 card in the app's own font (var(--font-sans)) for
  // html2canvas. Colors are resolved from the active theme's CSS tokens at
  // render time (getComputedStyle has already substituted var() chains), so the
  // image matches the theme; fallbacks use the espresso palette.
  function _themeColor(varName, fallback) {
    try {
      const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim();
      return v || fallback;
    } catch { return fallback; }
  }
  function _savingsCardEl(logoDataUrl) {
    const provider = CLOUD_PRICING[state.costProvider];
    const saved = calcCost(state.costProvider);
    const savedStr = saved < 0.005
      ? "$0.00"
      : "$" + saved.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const tokens = _fmtTokensShort((state._allTimeTokIn || 0) + (state._allTimeTokOut || 0));
    const sessions = (state.chats && state.chats.order && state.chats.order.length) || 0;
    const providerLabel = provider ? provider.label : state.costProvider;
    const sinceStr = _fmtSince(state._savingsSince);
    const cBg      = _themeColor("--bg", "#2B2722");
    const cFg      = _themeColor("--fg", "#EAE1D0");
    const cMuted   = _themeColor("--fg-muted", "#8A8170");
    const cSubtle  = _themeColor("--fg-subtle", "#B5AB95");
    const cFaint   = _themeColor("--fg-faint", "#6E6555");
    const cAccent  = _themeColor("--accent", "#B5544A");
    const cSuccess = _themeColor("--success", "#67C28C");
    const cBorder  = _themeColor("--border-strong", "#3D372E");
    const cQrBg    = "#FFFFFF"; // QR always needs a light field to scan — black-on-white is universal
    let qrDataUrl = "";
    try {
      if (typeof qrcode === "function") {
        const qr = qrcode(0, "M"); // 0 = auto-size, M = error tolerance
        qr.addData("https://github.com/mkultraware/accuretta");
        qr.make();
        qrDataUrl = qr.createDataURL(6, 24); // 6px modules, 24px quiet zone — big source, downscales sharp
      }
    } catch { qrDataUrl = ""; }
    const wrap = document.createElement("div");
    wrap.style.cssText = "position:fixed;left:-10000px;top:0;z-index:-1;";
    wrap.innerHTML = `
      <div style="width:540px;height:540px;box-sizing:border-box;background:${cBg};border-radius:24px;padding:46px;display:flex;flex-direction:column;font-family:var(--font-sans);">
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <div style="display:flex;align-items:center;gap:11px;">
            ${logoDataUrl
              ? `<img src="${logoDataUrl}" alt="" style="height:30px;width:auto;display:block;">`
              : `<span style="width:14px;height:14px;border-radius:4px;background:${cAccent};display:inline-block;"></span>`}
            <span style="color:${cFg};font-size:18px;font-weight:500;letter-spacing:-0.01em;">accuretta</span>
          </div>
          <span style="color:${cMuted};font-size:13px;font-weight:500;letter-spacing:0.01em;">github.com/mkultraware/accuretta</span>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;justify-content:center;gap:2px;">
          <span style="color:${cMuted};font-size:13px;font-weight:500;letter-spacing:0.18em;text-transform:uppercase;">saved by going local</span>
          <div style="display:flex;align-items:flex-start;gap:6px;">
            <span style="color:${cSuccess};font-size:82px;font-weight:500;letter-spacing:-0.035em;line-height:1.05;">${savedStr}</span>
            <span style="color:${cSuccess};font-size:26px;line-height:1.25;">*</span>
          </div>
          <span style="color:${cSubtle};font-size:17px;margin-top:8px;">vs running the same prompts on ${esc(providerLabel)}${sinceStr ? ` · since ${esc(sinceStr)}` : ""}</span>
          <span style="color:${cFaint};font-size:11px;margin-top:8px;line-height:1.4;">* ${esc(API_RATE_NOTE)}</span>
        </div>
        <div style="display:flex;border-top:1px solid ${cBorder};padding-top:18px;margin-bottom:20px;">
          <div style="flex:1;"><div style="color:${cFg};font-size:19px;font-weight:500;">${tokens}</div><div style="color:${cMuted};font-size:12px;">tokens run</div></div>
          <div style="flex:1;"><div style="color:${cFg};font-size:19px;font-weight:500;">${sessions}</div><div style="color:${cMuted};font-size:12px;">sessions</div></div>
          <div style="flex:1;"><div style="color:${cFg};font-size:19px;font-weight:500;">$0.00</div><div style="color:${cMuted};font-size:12px;">sent to a cloud</div></div>
        </div>
        <div style="display:flex;align-items:center;justify-content:space-between;">
          <span style="color:${cFaint};font-size:13px;">your model, your machine</span>
          ${qrDataUrl
            ? `<div style="background:${cQrBg};padding:10px;border-radius:12px;">
                 <img src="${qrDataUrl}" alt="Scan me — github.com/mkultraware/accuretta" style="width:78px;height:78px;display:block;">
               </div>`
            : ""}
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
      const blob = await (await fetch("/assets/brand/logo-mark-light.png")).blob();
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
    const cardBg = _themeColor("--bg", "#2B2722");
    try {
      const canvas = await window.html2canvas(card, {
        backgroundColor: cardBg,
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
  return { shareSavingsCard };
};
