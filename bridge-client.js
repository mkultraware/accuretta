(() => {
  "use strict";
  const tokenMeta = () => document.querySelector('meta[name="accuretta-request-token"]');
  const nativeFetch = window.fetch.bind(window);

  // The bridge makes a new request token every time it starts. A page opened
  // before a restart (a phone left on the app) kept the old one, and every
  // write was refused with 403: chats couldn't be sent or deleted until the
  // page was reloaded. On a 403, read the current token from a fresh copy of
  // the page and retry once. A 403 is refused before anything runs, so the
  // retry can't do anything twice.
  let refreshing = null;
  function refreshToken() {
    if (!refreshing) {
      refreshing = nativeFetch("/", { cache: "no-store", credentials: "same-origin" })
        .then(r => (r.ok ? r.text() : ""))
        .then(html => {
          const m = /name="accuretta-request-token" content="([^"]+)"/.exec(html);
          const meta = tokenMeta();
          if (m && meta && meta.content !== m[1]) {
            meta.setAttribute("content", m[1]);
            return true;
          }
          return false;
        })
        .catch(() => false)
        .finally(() => { setTimeout(() => { refreshing = null; }, 0); });
    }
    return refreshing;
  }

  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (options.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (!(url.origin === location.origin && url.pathname.startsWith("/api/") && !["GET", "HEAD"].includes(method))) {
      return nativeFetch(input, options);
    }
    const send = () => {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Accuretta-Token", tokenMeta()?.content || "");
      if (!headers.has("Content-Type") && (options.body == null || typeof options.body === "string")) headers.set("Content-Type", "application/json");
      return nativeFetch(input, { ...options, headers });
    };
    const res = await send();
    // A Request object's body can only be read once; those aren't retried.
    if (res.status !== 403 || input instanceof Request) return res;
    return (await refreshToken()) ? send() : res;
  };
})();
