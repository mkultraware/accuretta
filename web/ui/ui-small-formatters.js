/* SPDX-License-Identifier: AGPL-3.0-only */
/* small formatters: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["small-formatters"] = ({ fileIconFor }) => {
  function wbModelShort(name) {
    const base = String(name || "").split(/[\\/]/).pop().replace(/\.gguf$/i, "");
    const short = base.replace(/[-_.](?:I?Q\d\w*|F16|BF16|F32|FP16|MXFP4\w*)(?:[-_.].*)?$/i, "");
    return (short || base).slice(0, 22);
  }
  function wbModelColor(name) {
    const key = wbModelShort(name).toLowerCase();
    if (!key) return "var(--fg-faint)";
    let h = 0;
    for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) % 360;
    return `hsl(${(h + 230) % 360} 72% 64%)`;
  }
  function wbShortTime(t) {
    if (!t) return "";
    const diff = Math.max(0, Date.now() / 1000 - t);
    if (diff < 60) return "now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
    if (diff < 7 * 86400) return `${Math.floor(diff / 86400)}d`;
    return new Date(t * 1000).toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function wbDuration(secs) {
    const s = Math.max(0, Math.round(Number(secs) || 0));
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  }
  function wbClock(t) {
    if (!t) return "";
    const d = new Date(t * 1000);
    const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    return d.toDateString() === new Date().toDateString()
      ? time
      : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`;
  }
  function wbFormatBytes(n) {
    const b = Number(n) || 0;
    if (b < 1024) return `${b} B`;
    if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`;
    return `${(b / 1024 / 1024).toFixed(1)} MB`;
  }
  const WB_IMAGE_EXT = new Set(["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "ico", "avif"]);
  function wbFileKind(name, isDir) {
    if (isDir) return "dir";
    const lower = String(name || "").toLowerCase();
    const ext = lower.includes(".") ? lower.split(".").pop() : "";
    if (lower.startsWith(".git")) return "git";
    if (["html", "htm"].includes(ext)) return "html";
    if (["css", "scss", "less"].includes(ext)) return "css";
    if (["js", "mjs", "cjs", "jsx"].includes(ext)) return "js";
    if (["ts", "tsx"].includes(ext)) return "ts";
    if (ext === "py") return "py";
    if (["md", "mdx", "txt", "rst"].includes(ext)) return "md";
    if (["json", "yml", "yaml", "toml", "ini", "cfg", "env"].includes(ext)) return "data";
    if (WB_IMAGE_EXT.has(ext)) return "img";
    if (["sh", "ps1", "bat", "cmd"].includes(ext)) return "shell";
    if (["rs", "go", "java", "kt", "swift", "c", "cpp", "h", "hpp", "cs", "rb", "php", "lua", "sql"].includes(ext)) return "code";
    return "file";
  }
  function wbFileIcon(name) {
    const ext = String(name || "").toLowerCase().split(".").pop();
    return fileIconFor(name, ext);
  }
  return {
    wbModelShort, wbModelColor, wbShortTime, wbDuration, wbClock, wbFormatBytes, WB_IMAGE_EXT,
    wbFileKind, wbFileIcon,
  };
};
