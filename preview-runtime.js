(() => {
  "use strict";
  if (window.__accPreviewRuntime) return;
  window.__accPreviewRuntime = true;

  // Sandboxed previews have no persistent app storage. Supply private,
  // temporary storage so ordinary theme toggles can still render.
  for (const name of ["localStorage", "sessionStorage"]) {
    try { void window[name].length; } catch {
      const values = new Map();
      Object.defineProperty(window, name, { value: {
        get length() { return values.size; },
        getItem: key => values.get(String(key)) ?? null,
        setItem: (key, value) => values.set(String(key), String(value)),
        removeItem: key => values.delete(String(key)),
        clear: () => values.clear(),
        key: index => [...values.keys()][index] ?? null,
      }});
    }
  }

  if (!window.__accConsoleWired) {
    window.__accConsoleWired = true;
    for (const level of ["log", "warn", "error", "info", "debug"]) {
      const original = console[level].bind(console);
      console[level] = (...args) => {
        try { parent.postMessage({ __acc: "console", level, text: args.map(String).join(" ").slice(0, 16000) }, "*"); } catch {}
        original(...args);
      };
    }
  }
  async function snapshotImage(scale) {
    const width = Math.min(4096, document.documentElement.scrollWidth);
    const height = Math.min(4096, document.documentElement.scrollHeight);
    const copy = document.body.cloneNode(true);
    const originals = [document.body, ...document.body.querySelectorAll("*")];
    const copies = [copy, ...copy.querySelectorAll("*")];
    for (let index = 0; index < originals.length; index++) {
      const original = originals[index], clone = copies[index];
      const computed = getComputedStyle(original);
      const style = [...computed].map(name => `${name}:${computed.getPropertyValue(name)};`).join("");
      clone.setAttribute("style", style);
      for (const attribute of [...clone.attributes]) {
        if (/^on/i.test(attribute.name)) clone.removeAttribute(attribute.name);
      }
      if (original instanceof HTMLInputElement) clone.setAttribute("value", original.value);
      if (original instanceof HTMLTextAreaElement) clone.textContent = original.value;
      if (original instanceof HTMLImageElement) {
        const source = original.currentSrc || original.src;
        if (source && !source.startsWith("data:")) {
          const response = await fetch(source);
          if (!response.ok) throw new Error("An image could not be included in the screenshot");
          const blob = await response.blob();
          clone.src = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(blob);
          });
        } else clone.src = source;
        clone.removeAttribute("srcset");
      }
      if (original instanceof HTMLCanvasElement) {
        const picture = document.createElement("img");
        picture.src = original.toDataURL("image/png");
        picture.setAttribute("style", style);
        clone.replaceWith(picture);
      }
      if (/url\((?!["']?data:)/i.test(computed.backgroundImage)) {
        throw new Error("Capture currently requires embedded background images");
      }
    }
    copy.querySelectorAll("script, iframe, object, embed, link, style").forEach(el => el.remove());
    copy.style.margin = "0";
    const markup = new XMLSerializer().serializeToString(copy);
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><foreignObject width="100%" height="100%">${markup}</foreignObject></svg>`;
    const picture = new Image();
    picture.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    await picture.decode();
    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(width * scale);
    canvas.height = Math.ceil(height * scale);
    const context = canvas.getContext("2d");
    context.scale(scale, scale);
    context.fillStyle = getComputedStyle(document.body).backgroundColor || "#fff";
    context.fillRect(0, 0, width, height);
    context.drawImage(picture, 0, 0);
    return canvas.toDataURL("image/png");
  }

  let capturing = false;
  window.addEventListener("message", async event => {
    const request = event.data;
    if (event.source !== parent || request?.__acc !== "capture" || typeof request.id !== "string" || capturing) return;
    capturing = true;
    try {
      const image = await snapshotImage(Math.max(0.25, Math.min(2, Number(request.scale) || 1)));
      parent.postMessage({ __acc: "capture-result", id: request.id, image }, "*");
    } catch (error) {
      parent.postMessage({ __acc: "capture-result", id: request.id, error: String(error.message || error) }, "*");
    } finally { capturing = false; }
  });
})();
