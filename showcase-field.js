(() => {
  "use strict";

  // SHOWCASE — the scene behind an empty "New session" screen.
  // The theme draws its frame in lines of white light. On an empty session
  // those lines reach across the window: from the top left corner of the
  // poster (the glass plate and the prompt box under it) one line runs left
  // over the sidebar and one runs up over the top row, and from its bottom
  // right corner one runs right and one runs down, each with a point of
  // light leaving along it. The name is set in lettering as wide as the
  // window behind the glass. The lines draw themselves out from their
  // corners; when a chat starts they pull back, the lettering fades and a
  // copy of the plate's glass lifts away.
  //
  // Nothing here paints: this script only builds a handful of elements,
  // measures where the plate and the prompt box stand, and hands those
  // numbers to showcase.css as custom properties. All the motion is CSS
  // transforms and opacity, so it runs on the compositor and costs nothing
  // while a chat is open. The scene sits behind the app's panels (they are
  // transparent in this theme), so it never covers what you read or type.
  // It also places the one point the frame is lit from, where the sidebar's
  // edge meets the rule under the top row.

  const THEME = "showcase";
  const WORD = "Accuretta";
  const LEAVE_MS = 700;

  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  const wide = matchMedia("(min-width: 601px)");

  let scene = null, drift = null, vertex = null;
  let on = false, armTimer = 0, leaveTimer = 0, clock = 0, raf = 0, queued = false;
  const aim = { x: 0, y: 0 }, at = { x: 0, y: 0 };

  function themeActive() {
    return (document.documentElement.dataset.theme || "") === THEME && wide.matches;
  }
  function plateEl() {
    return themeActive() ? document.querySelector(".center .chat.is-welcome .wv-head") : null;
  }

  function ensureScene() {
    if (scene) return true;
    const app = document.getElementById("app");
    if (!app) return false;
    scene = document.createElement("div");
    scene.id = "showcase-scene";
    scene.setAttribute("aria-hidden", "true");
    Object.assign(scene.style, { pointerEvents: "none", display: "none" });
    scene.innerHTML =
      '<div class="sc-drift"><div class="sc-word"></div></div>' +
      '<div class="sc-ghost"></div>' +
      ["tl", "br"].map(k =>
        `<div class="sc-ray is-h is-${k}"><i></i></div><div class="sc-ray is-v is-${k}"><i></i></div>` +
        `<div class="sc-node is-${k}"></div>`).join("") +
      '<div class="sc-vertex"></div>';
    scene.querySelector(".sc-word").textContent = WORD;
    drift = scene.querySelector(".sc-drift");
    vertex = scene.querySelector(".sc-vertex");
    app.appendChild(scene);
    return true;
  }

  // Where things stand, relative to the app and in its own pixels (the two
  // differ if the app is ever scaled).
  function measure() {
    if (!scene || !themeActive()) return;
    const app = document.getElementById("app");
    const base = app.getBoundingClientRect();
    const scale = app.offsetWidth ? base.width / app.offsetWidth : 1;
    const set = (name, value) => scene.style.setProperty(name, Math.round(value / (scale || 1)) + "px");

    // The point the frame is lit from: only when the sidebar is showing.
    const side = document.querySelector("#app > .sidebar")?.getBoundingClientRect();
    const bar = document.querySelector("#app .topbar")?.getBoundingClientRect();
    const meets = side && bar && side.width > 8 && bar.height > 8 && Math.abs(side.right - bar.left) < 3;
    vertex.style.display = meets ? "" : "none";
    if (meets) {
      set("--vx", side.right - base.left);
      set("--vy", bar.bottom - base.top);
    }

    // The poster: the plate on top, the prompt box below it.
    const plate = plateEl()?.getBoundingClientRect();
    const box = document.querySelector(".center .composer-wrap > .composer")?.getBoundingClientRect();
    if (plate && box && plate.width && box.width) {
      set("--xl", Math.min(plate.left, box.left) - base.left);
      set("--xr", Math.max(plate.right, box.right) - base.left);
      set("--yt", plate.top - base.top);
      set("--yb", box.bottom - base.top);
      set("--ph", plate.height);
    }
  }

  // The plate is dated like a printed sheet, in the reader's own locale.
  function stamp() {
    const plate = plateEl();
    if (!plate) return;
    const clean = text => String(text).replace(/["\\\r\n]/g, "");
    const now = new Date();
    let day = "", time = "";
    try {
      day = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" })
        .format(now).replace(/[.,]/g, "");
      time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" }).format(now);
    } catch { /* keep the plain caption */ }
    const line = [day, time].filter(Boolean).map(clean).join(" · ");
    plate.style.setProperty("--sc-stamp", `"New session${line ? "\\A " + line : ""}"`);
  }

  function setClock(run) {
    clearInterval(clock);
    clock = run ? setInterval(stamp, 20000) : 0;
  }

  // The lettering leans a few pixels away from the pointer, so the glass
  // reads as standing in front of it.
  function ease() {
    raf = 0;
    at.x += (aim.x - at.x) * 0.07;
    at.y += (aim.y - at.y) * 0.07;
    drift.style.transform = `translate3d(${(at.x * -16).toFixed(2)}px, ${(at.y * -9).toFixed(2)}px, 0)`;
    if (on && (Math.abs(aim.x - at.x) > 0.003 || Math.abs(aim.y - at.y) > 0.003)) raf = requestAnimationFrame(ease);
  }
  function onPointer(ev) {
    if (!on || motion.matches || document.hidden) return;
    aim.x = (ev.clientX / Math.max(1, window.innerWidth)) * 2 - 1;
    aim.y = (ev.clientY / Math.max(1, window.innerHeight)) * 2 - 1;
    if (!raf) raf = requestAnimationFrame(ease);
  }

  function sync() {
    queued = false;
    if (!themeActive()) {
      on = false;
      clearTimeout(armTimer); clearTimeout(leaveTimer); setClock(false);
      if (scene) {
        scene.classList.remove("is-on", "is-leaving");
        scene.style.display = "none";
      }
      return;
    }
    if (!ensureScene()) return;
    scene.style.display = "";
    measure();

    if (plateEl()) {
      stamp();
      if (on) return;
      on = true;
      clearTimeout(leaveTimer);
      scene.classList.remove("is-leaving");
      setClock(!document.hidden);
      // One beat later, so the measured positions are in place before the
      // lines draw out from them. A timer, not a frame callback: frames do
      // not run while the window is hidden.
      clearTimeout(armTimer);
      armTimer = setTimeout(() => {
        if (!on) return;
        measure();
        scene.classList.add("is-on");
      }, 140);
      // The page can still shift the poster without resizing anything
      // (the lists under it arrive late), so look again while it settles.
      for (const ms of [400, 900, 1600]) setTimeout(() => { if (on) measure(); }, ms);
    } else if (on) {
      on = false;
      clearTimeout(armTimer);
      setClock(false);
      scene.classList.remove("is-on");
      scene.classList.add("is-leaving");
      clearTimeout(leaveTimer);
      leaveTimer = setTimeout(() => scene && scene.classList.remove("is-leaving"), LEAVE_MS);
    }
  }

  // Several things can change in one go (class, contents, size).
  function queue() {
    if (queued) return;
    queued = true;
    setTimeout(sync, 0);
  }

  function start() {
    const chat = document.querySelector(".center .chat");
    if (chat) new MutationObserver(queue).observe(chat, { attributes: true, attributeFilter: ["class"] });
    const inner = document.getElementById("chat-inner");
    if (inner) new MutationObserver(queue).observe(inner, { childList: true });
    new MutationObserver(queue).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    if (window.ResizeObserver) {
      const sizes = new ResizeObserver(measure);
      for (const sel of ["#app", "#app > .sidebar", ".center .chat", ".center .chat-scroll", ".center .composer-wrap > .composer"]) {
        const el = document.querySelector(sel);
        if (el) sizes.observe(el);
      }
    }
    window.addEventListener("resize", measure);
    window.addEventListener("pointermove", onPointer, { passive: true });
    document.addEventListener("visibilitychange", () => { setClock(on && !document.hidden); if (on && !document.hidden) stamp(); });
    motion.addEventListener?.("change", queue);
    wide.addEventListener?.("change", queue);
    // Type metrics move the plate a little once the faces have loaded.
    document.fonts?.ready?.then(measure).catch(() => {});
    sync();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
