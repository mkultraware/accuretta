(() => {
  "use strict";
  const token = document.querySelector('meta[name="accuretta-request-token"]')?.content;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    const method = (options.method || (input instanceof Request ? input.method : "GET")).toUpperCase();
    if (url.origin === location.origin && url.pathname.startsWith("/api/") && !["GET", "HEAD"].includes(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      headers.set("X-Accuretta-Token", token || "");
      if (!headers.has("Content-Type") && (options.body == null || typeof options.body === "string")) headers.set("Content-Type", "application/json");
      options = { ...options, headers };
    }
    return nativeFetch(input, options);
  };
})();
