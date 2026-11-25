/* SPDX-License-Identifier: AGPL-3.0-only */
/* console forwarder: moved out of app.js unchanged. app.js calls this where
   the section used to be, passing in the names it uses and taking back the ones it shares. */
window.Acc = window.Acc || {};
window.Acc.sections = window.Acc.sections || {};
window.Acc.sections["console-forwarder"] = ({ state }) => {
  // Injected into every preview: console.log / warn / error / info and uncaught
  // errors are posted to the parent, which shows them in the console pane.
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
    const runtime = `<script src="/web/js/preview-runtime.js"><\/script>`;
    const injected = runtime + CONSOLE_FORWARDER;
    if (/<head[^>]*>/i.test(html)) return html.replace(/<head[^>]*>/i, m => m + injected);
    if (/<html[^>]*>/i.test(html)) return html.replace(/<html[^>]*>/i, m => m + "<head>" + injected + "</head>");
    return injected + html;
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
    if (e.source !== document.getElementById("preview-frame")?.contentWindow || e.origin !== "null") return;
    if (!d || d.__acc !== "console" || typeof d.text !== "string") return;
    if (!["log", "info", "warn", "error", "debug"].includes(d.level)) return;
    pushConsoleLog(d.level, d.text.slice(0, 16000));
  });
  return { injectConsoleForwarder, clearConsole };
};
