"""
Accuretta desktop launcher.

Turns Accuretta into a real windowed application with NO stray console windows:
it starts the existing bridge in-process (a background thread, not a separate
console) and opens the live app in a native OS webview. The window loads the
exact same URL your browser loads, rendered by the same engine (WebView2 =
Chromium on Windows 11), so it looks and behaves pixel-for-pixel identical to
the browser. No frontend or bridge code is reimplemented here.

Run it directly during development (no console window):
    pythonw accuretta_app.py

Package it into a single windowed .exe (no console) with PyInstaller:
    pip install pywebview pyinstaller
    pyinstaller --noconfirm --windowed --name Accuretta ^
        --add-data "index.html;." --add-data "app.js;." --add-data "app.css;." ^
        --add-data "colors_and_type.css;." --add-data "manifest.webmanifest;." ^
        --add-data "accuMOTION;accuMOTION" --add-data "media;media" ^
        --add-data "logo-mark-light.png;." --add-data "logo-mark-dark.png;." ^
        --add-data "favicon.png;." --add-data "app-icon-512.png;." ^
        --icon accuretta.ico accuretta_app.py

Note: the bridge serves its static files relative to its own directory. When
frozen by PyInstaller (onefile), those live under sys._MEIPASS, so the bridge's
static-file root should resolve there when frozen. That is the one packaging
touch-up; running directly with python needs nothing.
"""

import base64
import os
import re
import socket
import sys
import threading
import time

# App mode: hide the llama-server console (bridge reads this), and do not let the
# bridge auto-open a browser -- the webview window IS the UI.
os.environ.setdefault("ACCURETTA_APP", "1")
os.environ.setdefault("ACCURETTA_BROWSER", "none")

# When frozen, run from the extracted bundle dir so bridge finds its assets.
if getattr(sys, "frozen", False):
    os.chdir(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)))

import webview  # pip install pywebview
import bridge   # the existing server; module-level init runs on import

PORT = int(os.environ.get("ACCURETTA_PORT", "8787"))


def _wait_ready(host: str = "127.0.0.1", port: int = PORT, timeout: float = 40.0) -> bool:
    """Block until the bridge is accepting connections (or give up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.25)
    return False


def _run_bridge() -> None:
    # bridge.main() creates the HTTP server and blocks in serve_forever(). It runs
    # in a daemon thread so it dies with the app; the bridge's own atexit handlers
    # clean up llama-server on shutdown.
    try:
        bridge.main()
    except Exception as exc:  # keep the window usable even if the bridge stumbles
        print(f"bridge exited: {exc}", file=sys.stderr)


# Single-instance lock: hold a bound socket for the process lifetime so a second
# launch detects the first and bows out, instead of two windows fighting over the
# bridge port (the class of bug behind the earlier "window lingered" shutdown).
_lock_sock = None


def _port_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _acquire_single_instance(lock_port: int = 8799) -> bool:
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", lock_port))  # fails if another instance holds it
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


# Themes whose splash background needs the dark-colored logo mark. Keep this in
# sync with the actual palette luminance, including the softer and pastel sets.
_LIGHT_THEMES = {"", "light", "soft", "pastel", "retro", "neumorphic", "neobrutalism"}


def _read_saved_theme() -> str:
    """The last theme the user saved (settings.json). '' / unknown -> light."""
    try:
        return (bridge.get_settings().get("theme") or "").strip().lower()
    except Exception:
        return ""


def _splash_palette(theme: str) -> dict:
    """Pull bg/fg/muted/accent for `theme` straight from colors_and_type.css so
    the splash matches the app rather than hardcoding a second copy of the palette.
    Falls back to the light default if the file or a token can't be read."""
    pal = {"bg": "#FAFAFB", "fg": "#09090B", "muted": "#52525B", "accent": "#0066FF"}
    try:
        css = (bridge.ROOT / "colors_and_type.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # strip comments so [^{}] is safe
        blocks = re.findall(r"([^{}]+)\{([^{}]*)\}", css)

        # Merge every custom property whose selector matches the scope, in document
        # order (later rules win) — so the theme's palette block overrides the
        # earlier `body` gradient rule, and :root supplies shared shades.
        def vars_for(target: str) -> dict:
            out = {}
            for sel, body in blocks:
                if target in sel:
                    for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body):
                        out[name.strip()] = val.strip()
            return out

        vmap = vars_for(":root")
        if theme:
            vmap.update(vars_for(f'[data-theme="{theme}"]'))

        # Follow var(--x) references (some accents alias a shared shade) to a literal.
        def resolve(val: str, depth: int = 0) -> str:
            val = (val or "").strip()
            m = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,\s*(.+))?\)", val)
            if m and depth < 8:
                if m.group(1) in vmap:
                    return resolve(vmap[m.group(1)], depth + 1)
                if m.group(2):  # var(--x, fallback)
                    return resolve(m.group(2), depth + 1)
            return val

        for key, var in (("bg", "--bg"), ("fg", "--fg"),
                         ("muted", "--fg-muted"), ("accent", "--accent")):
            if var in vmap:
                pal[key] = resolve(vmap[var])
    except Exception:
        pass
    return pal


def _logo_data_uri(theme: str) -> str:
    """Base64-embed the theme-appropriate logo. The splash renders before the
    bridge serves files, so an http path (/logo-mark-*.png) can't load here."""
    fname = "logo-mark-dark.png" if theme in _LIGHT_THEMES else "logo-mark-light.png"
    try:
        raw = (bridge.ROOT / fname).read_bytes()
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    except Exception:
        return ""


def _hex_rgb(h: str):
    """'#abc'/'#aabbcc' -> (r, g, b) ints, or None if it isn't hex."""
    h = (h or "").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return None


def _hue_shift(hex_color: str, dh: float) -> str:
    """Rotate a hex colour's hue by `dh` degrees (saturation/lightness kept), so
    the splash's colour field can derive per-theme companions from the accent.
    colorsys is imported here — this runs exactly once per launch."""
    import colorsys
    rgb = _hex_rgb(hex_color)
    if not rgb:
        return hex_color
    r, g, b = (c / 255 for c in rgb)
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    h = (h + dh / 360.0) % 1.0
    r2, g2, b2 = colorsys.hls_to_rgb(h, l, s)
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (r2, g2, b2)))


def _build_legacy_splash_html() -> str:
    """Branded boot splash honoring the last saved theme. Rebuilt each launch,
    so a theme change shows next boot.

    Design: "precision instrument coming online". A 60-tick dial surrounds the
    logo mark and the ticks IGNITE in sync with real boot progress (__splashStage
    pct) — the progress bar made physical. Behind the dial a slow conic sheen
    rotates; a dashed orbit ring carries a satellite dot; a single light sweep
    passes over the instrument once on entry. Backdrop is a quiet dot-grid over
    a drifting theme-colour field with film grain — techy, no gimmicks. _boot()
    pushes live stages into window.__splashStage(), so the status line, tick
    ring, pct readout and progress bar are all real."""
    import math
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo = _logo_data_uri(theme)
    accent = pal["accent"]

    is_light = theme in _LIGHT_THEMES
    track_bg = "rgba(0, 0, 0, 0.10)" if is_light else "rgba(255, 255, 255, 0.10)"
    # Same idea as the welcome screen's --wb-strength: full field on light
    # themes, turned down on dark ones.
    field_opacity = "0.9" if is_light else "0.4"
    dot_color = "rgba(0, 0, 0, 0.10)" if is_light else "rgba(255, 255, 255, 0.10)"

    if accent.startswith("#") and len(accent) == 7:
        glow_color = accent + "44"
        # Per-theme colour field: the accent plus two hue-rotated companions —
        # every theme gets its own palette with no hardcoded hex.
        blob_a = accent
        blob_b = _hue_shift(accent, 42)
        blob_c = _hue_shift(accent, -42)
    else:
        glow_color = "rgba(56, 189, 248, 0.27)"
        blob_a = blob_b = blob_c = "#38bdf8"

    # 60-tick progress dial around the logo — the instrument face. Major tick
    # every 5th. Ticks fade in staggered on load, then __splashStage ignites
    # them one by one as boot progress lands. Base stroke-opacity rides as a
    # presentation attribute so the .lit class can override it freely.
    ticks = []
    for i in range(60):
        a = math.radians(i * 6 - 90)
        major = (i % 5 == 0)
        r_in, r_out = (100.0, 113.0) if major else (106.0, 113.0)
        x1, y1 = 120 + r_in * math.cos(a), 120 + r_in * math.sin(a)
        x2, y2 = 120 + r_out * math.cos(a), 120 + r_out * math.sin(a)
        w = 2.4 if major else 1.4
        ticks.append(
            f'<line class="tick" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke-width="{w}" stroke-opacity="0.16" '
            f'style="animation-delay:{0.35 + i * 0.012:.2f}s"/>'
        )
    ticks_svg = "".join(ticks)

    # Wordmark with a per-letter staggered reveal (starts as the arc lands).
    letters = "".join(
        f'<span style="animation-delay:{0.6 + i * 0.045:.2f}s">{ch}</span>'
        for i, ch in enumerate("accuretta")
    )

    logo_html = (
        f'<div class="dial-wrap">'
        f'<div class="sheen"></div>'
        f'<svg class="dial" viewBox="0 0 240 240" aria-hidden="true">'
        f'<circle class="orbit" cx="120" cy="120" r="86"/>'
        f'<g class="sat"><circle cx="120" cy="34" r="2.6"/></g>'
        f'<circle class="arc" cx="120" cy="120" r="94"/>'
        f'{ticks_svg}'
        f'</svg>'
        f'<div class="halo"></div>'
        f'<img src="{logo}" class="logo-img" alt="">'
        f'</div>'
        if logo else ""
    )

    return f"""<!doctype html><html><head><meta charset="utf-8"></head>
<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:{pal['bg']};color:{pal['fg']};font-family:system-ui,-apple-system,'Segoe UI',sans-serif;user-select:none;overflow:hidden;position:relative">
  <div class="field">
    <div class="dots"></div>
    <div class="wb-blob a"></div>
    <div class="wb-blob b"></div>
    <div class="wb-blob c"></div>
    <div class="grain"></div>
  </div>
  <div class="content">
    {logo_html}
    <h1 class="title">{letters}</h1>
    <div class="readout">
      <span class="status" id="status">warming up…</span>
      <span class="pct" id="pct">000</span>
    </div>
    <div class="track"><div class="fill" id="fill"></div></div>
  </div>
  <div class="foot">your model · your machine</div>
  <style>
    /* --- Backdrop: dot-grid blueprint over drifting colour field + grain --- */
    .field {{
      position: absolute; inset: 0; z-index: 0; overflow: hidden;
      pointer-events: none;
      opacity: {field_opacity};
      -webkit-mask-image: radial-gradient(ellipse 80% 65% at 50% 40%, #000 55%, transparent 100%);
      mask-image: radial-gradient(ellipse 80% 65% at 50% 40%, #000 55%, transparent 100%);
    }}
    .dots {{
      position: absolute; inset: -60px;
      background-image: radial-gradient({dot_color} 1px, transparent 1.3px);
      background-size: 26px 26px;
      animation: dots-in 1.6s ease both, dots-drift 46s linear infinite;
    }}
    @keyframes dots-in {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    @keyframes dots-drift {{ from {{ background-position: 0 0; }} to {{ background-position: 52px 26px; }} }}
    .wb-blob {{
      position: absolute; border-radius: 50%;
      will-change: transform, opacity;
    }}
    .wb-blob.a {{
      top: -25%; left: -15%; width: 70vmax; height: 70vmax;
      background: {blob_a}; filter: blur(120px);
      animation: drift-a 15s ease-in-out infinite;
    }}
    .wb-blob.b {{
      top: 5%; right: -15%; width: 60vmax; height: 60vmax;
      background: {blob_b}; filter: blur(110px);
      animation: drift-b 18s ease-in-out infinite;
    }}
    .wb-blob.c {{
      bottom: -20%; left: 20%; width: 65vmax; height: 65vmax;
      background: {blob_c}; filter: blur(120px);
      animation: drift-c 22s ease-in-out infinite;
    }}
    @keyframes drift-a {{
      0%, 100% {{ transform: translate(0, 0) rotate(0deg) scale(1); opacity: 0.30; }}
      33%      {{ transform: translate(3vmax, -4vmax) rotate(8deg) scale(1.1); opacity: 0.46; }}
      66%      {{ transform: translate(-2vmax, 2vmax) rotate(-4deg) scale(0.92); opacity: 0.30; }}
    }}
    @keyframes drift-b {{
      0%, 100% {{ transform: translate(0, 0) rotate(0deg) scale(1); opacity: 0.34; }}
      33%      {{ transform: translate(-4vmax, 3vmax) rotate(-12deg) scale(0.88); opacity: 0.22; }}
      66%      {{ transform: translate(3vmax, -3vmax) rotate(8deg) scale(1.12); opacity: 0.48; }}
    }}
    @keyframes drift-c {{
      0%, 100% {{ transform: translate(0, 0) rotate(0deg) scale(1); opacity: 0.26; }}
      33%      {{ transform: translate(2vmax, 2vmax) rotate(16deg) scale(1.18); opacity: 0.42; }}
      66%      {{ transform: translate(-4vmax, -2vmax) rotate(-8deg) scale(0.85); opacity: 0.18; }}
    }}
    .grain {{
      position: absolute; inset: 0;
      opacity: 0.05; mix-blend-mode: multiply;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 200 200'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.8' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    }}

    /* --- The instrument: 60-tick dial + orbit + satellite + conic sheen --- */
    .content {{
      position: relative; z-index: 2;
      display: flex; flex-direction: column; align-items: center; justify-content: center;
    }}
    .dial-wrap {{
      position: relative; width: 240px; height: 240px;
      display: flex; align-items: center; justify-content: center;
      animation: dial-in 0.9s cubic-bezier(0.34, 1.4, 0.64, 1) both;
    }}
    /* One vertical light sweep across the instrument on entry — then never again. */
    .dial-wrap::after {{
      content: ""; position: absolute; inset: -8%;
      background: linear-gradient(115deg, transparent 32%, rgba(255,255,255,0.30) 50%, transparent 68%);
      transform: translateX(-130%);
      animation: sweep 1.1s cubic-bezier(0.4, 0, 0.2, 1) 0.75s both;
      pointer-events: none;
    }}
    @keyframes sweep {{ to {{ transform: translateX(130%); }} }}
    .sheen {{
      position: absolute; width: 254px; height: 254px; border-radius: 50%;
      background: conic-gradient(from 0deg, transparent 0deg 295deg, {accent}38 340deg, transparent 360deg);
      -webkit-mask-image: radial-gradient(farthest-side, transparent calc(100% - 26px), #000 calc(100% - 25px));
      mask-image: radial-gradient(farthest-side, transparent calc(100% - 26px), #000 calc(100% - 25px));
      animation: sheen-spin 7s linear infinite;
      opacity: 0.8;
    }}
    @keyframes sheen-spin {{ to {{ transform: rotate(360deg); }} }}
    .dial {{ position: absolute; width: 240px; height: 240px; overflow: visible; }}
    .tick {{
      stroke: {pal['fg']};
      animation: tick-in 0.5s ease both;
      transition: stroke 0.3s ease, stroke-opacity 0.3s ease, filter 0.3s ease;
    }}
    @keyframes tick-in {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}
    .tick.lit {{
      stroke: {accent}; stroke-opacity: 1;
      filter: drop-shadow(0 0 3px {glow_color});
    }}
    .orbit {{
      fill: none; stroke: {pal['fg']}; stroke-opacity: 0.26; stroke-width: 1;
      stroke-dasharray: 1 7;
      transform-origin: 120px 120px;
      animation: orbit-spin 30s linear infinite;
    }}
    @keyframes orbit-spin {{ to {{ transform: rotate(360deg); }} }}
    .sat {{
      transform-origin: 120px 120px;
      animation: sat-spin 9s linear infinite;
    }}
    .sat circle {{ fill: {accent}; filter: drop-shadow(0 0 4px {glow_color}); }}
    @keyframes sat-spin {{ to {{ transform: rotate(360deg); }} }}
    /* Draw-on arc: paints once around the dial, then holds at half-strength. */
    .arc {{
      fill: none; stroke: {accent}; stroke-width: 2; stroke-linecap: round;
      opacity: 0.55;
      stroke-dasharray: 590.6; stroke-dashoffset: 590.6;
      transform: rotate(-90deg); transform-origin: 120px 120px;
      animation: arc-draw 1.2s cubic-bezier(0.65, 0, 0.35, 1) 0.25s forwards;
      filter: drop-shadow(0 0 5px {glow_color});
    }}
    @keyframes arc-draw {{ to {{ stroke-dashoffset: 0; }} }}
    .halo {{
      position: absolute; width: 118px; height: 118px; border-radius: 50%;
      background: radial-gradient(circle, {glow_color} 0%, transparent 70%);
      animation: halo-pulse 3.2s ease-in-out infinite;
    }}
    .logo-img {{
      position: relative; z-index: 3;
      width: 84px; height: 84px; object-fit: contain;
      filter: drop-shadow(0 0 18px {glow_color});
      animation: breathe 3.2s ease-in-out infinite;
    }}
    .dial-wrap.done .dial {{ animation: dial-pulse 0.9s cubic-bezier(0.34, 1.4, 0.64, 1); }}
    @keyframes dial-pulse {{
      0% {{ transform: scale(1); }} 40% {{ transform: scale(1.035); }} 100% {{ transform: scale(1); }}
    }}

    /* --- Wordmark + live readout --- */
    .title {{
      margin: 8px 0 0 0;
      font-size: 27px; font-weight: 700;
      letter-spacing: 0.22em; text-indent: 0.22em;
      text-align: center;
      animation: title-in 1.2s cubic-bezier(0.19, 1, 0.22, 1) 0.55s both;
    }}
    @keyframes title-in {{
      from {{ opacity: 0; letter-spacing: 0.34em; }}
      to   {{ opacity: 1; letter-spacing: 0.22em; }}
    }}
    .title span {{
      display: inline-block;
      opacity: 0;
      transform: translateY(12px);
      filter: blur(5px);
      animation: letter-in 0.75s cubic-bezier(0.19, 1, 0.22, 1) both;
    }}
    .readout {{
      margin-top: 16px; width: 240px;
      display: flex; justify-content: space-between; align-items: baseline;
      opacity: 0;
      animation: fade-in 0.8s ease-out 1.1s both;
    }}
    .status {{
      font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
      font-size: 11.5px;
      letter-spacing: 0.08em;
      color: {pal['muted']};
      min-height: 15px;
      transition: opacity 0.15s ease;
    }}
    /* Blinking accent caret after the stage label — echoes the brand wordmark. */
    .status::after {{
      content: "_";
      margin-left: 2px;
      color: {accent};
      animation: caret-blink 1.1s steps(1) infinite;
    }}
    @keyframes caret-blink {{
      0%, 55% {{ opacity: 1; }}
      56%, 100% {{ opacity: 0; }}
    }}
    /* !important: the fade-in animation's forward fill would otherwise pin
       opacity at 1 and swallow the swap dim. */
    .status.swap {{ opacity: 0.25 !important; }}
    .pct {{
      font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
      font-size: 11.5px; font-weight: 600;
      letter-spacing: 0.14em;
      color: {accent};
    }}
    .track {{
      margin-top: 12px; width: 240px; height: 2px;
      background: {track_bg}; border-radius: 2px; overflow: hidden;
      opacity: 0;
      animation: fade-in 0.8s ease-out 1.3s both;
    }}
    .fill {{
      position: relative; overflow: hidden;
      height: 100%; width: 4%;
      background: linear-gradient(90deg, {accent}66, {accent});
      border-radius: 2px;
      box-shadow: 0 0 10px {glow_color};
      transition: width 0.7s cubic-bezier(0.22, 1, 0.36, 1);
    }}
    /* Sheen sweeping along the fill so progress reads as alive between stages. */
    .fill::after {{
      content: ""; position: absolute; inset: 0;
      background: linear-gradient(90deg, transparent, rgba(255,255,255,0.5), transparent);
      transform: translateX(-110%);
      animation: fill-sheen 1.8s ease-in-out infinite;
    }}
    @keyframes fill-sheen {{
      60%, 100% {{ transform: translateX(110%); }}
    }}
    .foot {{
      position: absolute; bottom: 26px; left: 0; width: 100%;
      text-align: center; z-index: 2;
      font-family: ui-monospace, 'Cascadia Mono', Consolas, monospace;
      font-size: 10px; letter-spacing: 0.22em; text-indent: 0.22em;
      color: {pal['muted']};
      opacity: 0;
      animation: fade-in 1.2s ease-out 1.5s both;
    }}
    @keyframes dial-in {{
      from {{ transform: scale(0.86); opacity: 0; }}
      to   {{ transform: scale(1); opacity: 1; }}
    }}
    @keyframes halo-pulse {{
      0%, 100% {{ transform: scale(1); opacity: 0.7; }}
      50%      {{ transform: scale(1.25); opacity: 1; }}
    }}
    @keyframes breathe {{
      0%, 100% {{ transform: scale(1); }}
      50%      {{ transform: scale(1.05); }}
    }}
    @keyframes letter-in {{
      to {{ opacity: 1; transform: translateY(0); filter: blur(0); }}
    }}
    @keyframes fade-in {{ to {{ opacity: 1; }} }}
    @media (prefers-reduced-motion: reduce) {{
      .wb-blob, .dots, .sheen, .orbit, .sat, .halo, .logo-img,
      .fill::after, .status::after, .dial-wrap::after {{ animation: none; }}
      .arc {{ animation: none; stroke-dashoffset: 0; }}
    }}
  </style>
  <script>
    window.__splashStage = function (label, pct) {{
      var s = document.getElementById('status');
      var f = document.getElementById('fill');
      var p = document.getElementById('pct');
      if (s && s.textContent !== label) {{
        s.classList.add('swap');
        setTimeout(function () {{ s.textContent = label; s.classList.remove('swap'); }}, 150);
      }}
      if (typeof pct === 'number') {{
        var v = Math.max(0, Math.min(100, pct));
        if (f) f.style.width = Math.max(4, v) + '%';
        if (p) p.textContent = ('00' + Math.round(v)).slice(-3);
        // Ignite the instrument: ticks light in sync with real boot progress.
        var ticks = document.querySelectorAll('.tick');
        var lit = Math.round(v / 100 * ticks.length);
        for (var i = 0; i < ticks.length; i++) {{
          ticks[i].classList.toggle('lit', i < lit);
        }}
        if (v >= 100) {{
          var d = document.querySelector('.dial-wrap');
          if (d) d.classList.add('done');
        }}
      }}
    }};
  </script>
</body></html>"""


def _build_splash_html() -> str:
    """Theme-aware launch sequence built around an opening threshold.

    The saved theme supplies color only; the composition deliberately avoids
    borrowing any in-app theme's visual language. Live bridge progress drives
    one signal line and three honest milestones.
    """
    theme = _read_saved_theme()
    pal = _splash_palette(theme)
    logo = _logo_data_uri(theme)
    accent = pal["accent"]
    is_light = theme in _LIGHT_THEMES
    edge = "rgba(18, 18, 20, 0.14)" if is_light else "rgba(255, 255, 255, 0.11)"
    soft_edge = "rgba(18, 18, 20, 0.07)" if is_light else "rgba(255, 255, 255, 0.055)"
    shade = "rgba(12, 12, 14, 0.10)" if is_light else "rgba(0, 0, 0, 0.30)"
    theme_label = re.sub(r"[^a-z0-9 -]", "", (theme or "light").replace("-", " ")).strip() or "light"
    logo_html = (f'<img src="{logo}" class="logo-img" alt="">' if logo
                 else '<span class="logo-fallback">A</span>')

    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body>
  <div class="scene" aria-hidden="true">
    <div class="atmosphere"></div>
    <div class="plane plane-left"></div>
    <div class="plane plane-right"></div>
    <div class="horizon"><span></span></div>
    <div class="scan"></div>
    <div class="grain"></div>
  </div>

  <header class="mast">
    <div class="runtime"><span class="runtime-dot"></span><span>local runtime</span></div>
    <div class="theme-label">visual profile / {theme_label}</div>
  </header>

  <main class="content">
    <div class="brand">
      <div class="mark">{logo_html}</div>
      <h1>accuretta</h1>
      <p>your model. your machine.</p>
    </div>

    <section class="boot" aria-live="polite">
      <div class="readout">
        <span class="status"><i></i><span id="status">Preparing local runtime</span></span>
        <span class="pct" id="pct">0%</span>
      </div>
      <div class="rail"><div class="fill" id="fill"><b></b></div></div>
      <div class="milestones">
        <span class="milestone" data-at="18"><i></i>bridge</span>
        <span class="milestone" data-at="55"><i></i>engine</span>
        <span class="milestone" data-at="100"><i></i>workspace</span>
      </div>
    </section>
  </main>

  <footer><span>private by location</span><span>accuretta desktop</span></footer>

  <style>
    :root {{
      --bg: {pal['bg']}; --fg: {pal['fg']}; --muted: {pal['muted']}; --accent: {accent};
      --edge: {edge}; --edge-soft: {soft_edge}; --shade: {shade};
      --ease: cubic-bezier(.16, 1, .3, 1);
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; }}
    body {{
      margin: 0; position: relative; overflow: hidden; display: grid; place-items: center;
      color: var(--fg); background: var(--bg);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI Variable", "Segoe UI", sans-serif;
      user-select: none; -webkit-font-smoothing: antialiased;
    }}

    /* A threshold, not a dashboard: broad refractive planes part around one seam. */
    .scene {{ position: absolute; inset: 0; overflow: hidden; pointer-events: none; }}
    .atmosphere {{
      position: absolute; inset: 0;
      background:
        radial-gradient(ellipse 62% 58% at 50% 48%, color-mix(in srgb, var(--accent) 11%, transparent), transparent 70%),
        linear-gradient(180deg, color-mix(in srgb, var(--fg) 2.2%, transparent), transparent 34% 72%, var(--shade));
      animation: atmosphere-in 1.4s var(--ease) both;
    }}
    .plane {{
      position: absolute; top: -24%; bottom: -24%; width: 64%; opacity: .72;
      will-change: transform; transition: transform .55s var(--ease), opacity .55s ease;
    }}
    .plane::before {{
      content: ""; position: absolute; inset: 0;
      background:
        linear-gradient(112deg, transparent 16%, color-mix(in srgb, var(--accent) 8%, transparent) 46%, color-mix(in srgb, var(--fg) 5%, transparent) 50%, transparent 72%),
        linear-gradient(90deg, transparent, var(--edge-soft), transparent);
      border: 1px solid var(--edge-soft);
      box-shadow: inset 0 1px 0 color-mix(in srgb, var(--fg) 5%, transparent), 0 40px 120px var(--shade);
    }}
    .plane-left {{
      left: -14%; clip-path: polygon(0 0, 100% 10%, 83% 100%, 8% 88%);
      transform: translate3d(-8%, -1%, 0) rotate(-5deg);
      animation: plane-left-in 1.25s var(--ease) both;
    }}
    .plane-right {{
      right: -14%; clip-path: polygon(17% 9%, 100% 0, 92% 89%, 0 100%);
      transform: translate3d(8%, 1%, 0) rotate(5deg);
      animation: plane-right-in 1.25s var(--ease) both;
    }}
    .plane-right::before {{ transform: scaleX(-1); }}
    .horizon {{
      position: absolute; z-index: 2; top: 50%; left: 50%;
      width: min(76vw, 1120px); height: 1px;
      transform: translate(-50%, -50%) scaleX(0);
      background: linear-gradient(90deg, transparent, var(--edge) 20%, var(--edge) 80%, transparent);
      animation: horizon-open 1.05s var(--ease) .18s forwards;
    }}
    .horizon::before {{
      content: ""; position: absolute; left: 50%; top: -18px; width: 1px; height: 37px;
      background: linear-gradient(transparent, var(--accent), transparent);
      box-shadow: 0 0 28px color-mix(in srgb, var(--accent) 40%, transparent);
    }}
    .horizon span {{
      position: absolute; inset: -36px 18% -36px;
      background: radial-gradient(ellipse at center, color-mix(in srgb, var(--accent) 12%, transparent), transparent 72%);
      filter: blur(14px);
    }}
    .scan {{
      position: absolute; z-index: 3; top: 0; bottom: 0; left: -24%; width: 18%; opacity: 0;
      background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--fg) 8%, transparent), transparent);
      transform: skewX(-10deg); animation: scan-once 1.5s ease-in-out .48s both;
    }}
    .grain {{
      position: absolute; inset: 0; opacity: .026; mix-blend-mode: soft-light;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 180 180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
    }}

    .mast {{
      position: absolute; z-index: 5; top: 0; left: 0; right: 0;
      display: flex; align-items: center; justify-content: space-between; padding: 28px 32px;
      color: var(--muted); font: 600 9px/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .16em; text-transform: uppercase; opacity: 0;
      animation: chrome-in .8s ease .55s forwards;
    }}
    .runtime {{ display: flex; align-items: center; gap: 9px; }}
    .runtime-dot {{
      width: 5px; height: 5px; border-radius: 50%; background: var(--accent);
      box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 10%, transparent);
      animation: runtime-breathe 2.4s ease-in-out infinite;
    }}
    .theme-label {{ opacity: .72; }}

    .content {{
      position: relative; z-index: 4; width: min(520px, calc(100vw - 64px));
      display: flex; flex-direction: column; align-items: center; transform: translateY(-1.8vh);
    }}
    .brand {{ display: flex; flex-direction: column; align-items: center; text-align: center; }}
    .mark {{
      position: relative; width: 104px; height: 104px; display: grid; place-items: center;
      opacity: 0; transform: translateY(18px) scale(.94); filter: blur(14px);
      animation: mark-resolve 1.05s var(--ease) .25s forwards;
    }}
    .mark::before {{
      content: ""; position: absolute; inset: -62%;
      background: radial-gradient(circle, color-mix(in srgb, var(--accent) 20%, transparent), transparent 67%);
      animation: mark-aura 4.8s ease-in-out infinite;
    }}
    .mark::after {{
      content: ""; position: absolute; left: 50%; top: 50%; width: 142%; height: 1px;
      transform: translate(-50%, -50%) scaleX(0);
      background: linear-gradient(90deg, transparent, var(--accent), transparent);
      box-shadow: 0 0 18px color-mix(in srgb, var(--accent) 45%, transparent);
      animation: mark-seam .85s var(--ease) .42s forwards;
    }}
    .logo-img {{
      position: relative; z-index: 1; width: 88px; height: 88px; object-fit: contain;
      filter: drop-shadow(0 18px 34px var(--shade));
    }}
    .logo-fallback {{ position: relative; z-index: 1; font-size: 68px; font-weight: 800; color: var(--accent); }}
    h1 {{
      margin: 11px 0 0; font-size: clamp(38px, 4.2vw, 54px); line-height: .96;
      font-weight: 720; letter-spacing: -.057em; opacity: 0;
      transform: translateY(12px); filter: blur(8px);
      animation: copy-resolve .9s var(--ease) .52s forwards;
    }}
    .brand p {{
      margin: 14px 0 0; color: var(--muted); font-size: 13px; font-weight: 450; letter-spacing: .025em;
      opacity: 0; transform: translateY(8px); animation: copy-in .7s var(--ease) .72s forwards;
    }}

    .boot {{
      width: 100%; margin-top: 62px; opacity: 0; transform: translateY(10px);
      animation: copy-in .75s var(--ease) .86s forwards;
    }}
    .readout {{
      display: flex; align-items: center; justify-content: space-between; gap: 20px; min-height: 18px;
      font: 500 10px/1.4 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .075em; text-transform: uppercase;
    }}
    .status {{ display: inline-flex; align-items: center; gap: 9px; min-width: 0; color: var(--muted); }}
    .status i {{
      width: 14px; height: 1px; flex: 0 0 auto; background: var(--accent);
      box-shadow: 0 0 8px color-mix(in srgb, var(--accent) 35%, transparent);
    }}
    #status {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; transition: opacity .16s ease, transform .16s ease; }}
    #status.swap {{ opacity: 0; transform: translateY(-4px); }}
    .pct {{ color: var(--fg); font-variant-numeric: tabular-nums; letter-spacing: .04em; }}
    .rail {{ position: relative; margin-top: 13px; width: 100%; height: 1px; background: var(--edge); }}
    .fill {{
      position: absolute; left: 0; top: -1px; width: 3%; height: 3px; background: var(--accent);
      box-shadow: 0 0 14px color-mix(in srgb, var(--accent) 36%, transparent);
      transition: width .72s var(--ease);
    }}
    .fill b {{
      position: absolute; right: -2px; top: -3px; width: 7px; height: 7px; border-radius: 50%;
      background: var(--fg); border: 2px solid var(--accent);
      box-shadow: 0 0 0 5px color-mix(in srgb, var(--accent) 9%, transparent), 0 0 20px color-mix(in srgb, var(--accent) 45%, transparent);
    }}
    .milestones {{ display: grid; grid-template-columns: repeat(3, 1fr); margin-top: 14px; }}
    .milestone {{
      display: flex; align-items: center; gap: 7px; color: var(--muted);
      font: 500 8px/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .14em; text-transform: uppercase; opacity: .42;
      transition: color .3s ease, opacity .3s ease, transform .3s var(--ease);
    }}
    .milestone:nth-child(2) {{ justify-self: center; }}
    .milestone:nth-child(3) {{ justify-self: end; }}
    .milestone i {{ width: 3px; height: 3px; border-radius: 50%; background: currentColor; }}
    .milestone.passed {{ color: var(--accent); opacity: 1; transform: translateY(-1px); }}

    footer {{
      position: absolute; z-index: 5; left: 0; right: 0; bottom: 0;
      display: flex; justify-content: space-between; padding: 26px 32px;
      color: var(--muted); font: 500 8px/1 ui-monospace, "SFMono-Regular", Consolas, monospace;
      letter-spacing: .15em; text-transform: uppercase; opacity: 0;
      animation: chrome-in .8s ease .72s forwards;
    }}

    body.ready .plane-left {{ transform: translate3d(-15%, -1%, 0) rotate(-6deg); opacity: .48; }}
    body.ready .plane-right {{ transform: translate3d(15%, 1%, 0) rotate(6deg); opacity: .48; }}
    body.ready .mark {{ animation: ready-mark .48s var(--ease) both; }}
    body.ready .horizon {{ opacity: .72; transition: opacity .32s ease; }}

    @keyframes atmosphere-in {{ from {{ opacity: 0; transform: scale(1.08); }} to {{ opacity: 1; transform: scale(1); }} }}
    @keyframes plane-left-in {{ from {{ transform: translate3d(12%, 2%, 0) rotate(-2deg); opacity: 0; }} to {{ transform: translate3d(-8%, -1%, 0) rotate(-5deg); opacity: .72; }} }}
    @keyframes plane-right-in {{ from {{ transform: translate3d(-12%, -2%, 0) rotate(2deg); opacity: 0; }} to {{ transform: translate3d(8%, 1%, 0) rotate(5deg); opacity: .72; }} }}
    @keyframes horizon-open {{ to {{ transform: translate(-50%, -50%) scaleX(1); }} }}
    @keyframes scan-once {{ 0% {{ left: -24%; opacity: 0; }} 20% {{ opacity: .72; }} 100% {{ left: 112%; opacity: 0; }} }}
    @keyframes chrome-in {{ to {{ opacity: .78; }} }}
    @keyframes runtime-breathe {{ 0%,100% {{ opacity: .55; }} 50% {{ opacity: 1; }} }}
    @keyframes mark-resolve {{ to {{ opacity: 1; transform: translateY(0) scale(1); filter: blur(0); }} }}
    @keyframes mark-seam {{ 55% {{ transform: translate(-50%, -50%) scaleX(1); opacity: 1; }} 100% {{ transform: translate(-50%, -50%) scaleX(.2); opacity: 0; }} }}
    @keyframes mark-aura {{ 0%,100% {{ opacity: .52; transform: scale(.94); }} 50% {{ opacity: .82; transform: scale(1.06); }} }}
    @keyframes copy-resolve {{ to {{ opacity: 1; transform: translateY(0); filter: blur(0); }} }}
    @keyframes copy-in {{ to {{ opacity: 1; transform: translateY(0); }} }}
    @keyframes ready-mark {{ 0% {{ transform: scale(1); }} 45% {{ transform: scale(1.055); }} 100% {{ transform: scale(1); }} }}

    @media (max-width: 640px) {{
      .mast {{ padding: 20px; }}
      .theme-label {{ display: none; }}
      .content {{ width: min(460px, calc(100vw - 40px)); transform: translateY(-2vh); }}
      .mark {{ width: 86px; height: 86px; }}
      .logo-img {{ width: 72px; height: 72px; }}
      h1 {{ margin-top: 8px; font-size: 38px; }}
      .boot {{ margin-top: 46px; }}
      footer {{ padding: 20px; }}
      footer span:first-child {{ display: none; }}
      footer span:last-child {{ margin-left: auto; }}
      .plane {{ width: 82%; }}
      .plane-left {{ left: -32%; }}
      .plane-right {{ right: -32%; }}
      .horizon {{ width: 92vw; }}
    }}
    @media (max-height: 620px) {{
      .content {{ transform: scale(.9); }}
      .boot {{ margin-top: 38px; }}
      .mast, footer {{ padding-top: 18px; padding-bottom: 18px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ animation: none !important; transition-duration: .01ms !important; }}
      .mast, footer, .mark, h1, .brand p, .boot {{ opacity: 1; transform: none; filter: none; }}
      .horizon {{ transform: translate(-50%, -50%) scaleX(1); }}
    }}
  </style>
  <script>
    window.__splashStage = function (label, pct) {{
      var status = document.getElementById('status');
      var fill = document.getElementById('fill');
      var readout = document.getElementById('pct');
      if (status && status.textContent !== label) {{
        clearTimeout(window.__splashStatusTimer);
        status.classList.add('swap');
        window.__splashStatusTimer = setTimeout(function () {{
          status.textContent = label;
          status.classList.remove('swap');
        }}, 160);
      }}
      if (typeof pct === 'number') {{
        var value = Math.max(0, Math.min(100, pct));
        if (fill) fill.style.width = Math.max(3, value) + '%';
        if (readout) readout.textContent = Math.round(value) + '%';
        var marks = document.querySelectorAll('.milestone');
        for (var i = 0; i < marks.length; i++) {{
          marks[i].classList.toggle('passed', value >= Number(marks[i].getAttribute('data-at')));
        }}
        document.body.classList.toggle('ready', value >= 100);
      }}
    }};
  </script>
</body></html>"""


_ALREADY_RUNNING_HTML = """<body style="margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;background:#12151c;color:#e6e6e6;font-family:system-ui,sans-serif">
  <div style="font-size:22px;font-weight:600">accuretta</div>
  <div style="margin-top:8px;font-size:13px;color:#8b93a3">already running — check your taskbar.</div>
</body>"""

_FAILED_HTML = ("<body style='font-family:system-ui,sans-serif;background:#12151c;color:#e6e6e6;padding:2rem'>"
                "<h2>Accuretta could not start its engine</h2>"
                f"<p>The local bridge did not come up on port {PORT}. Check the logs, then relaunch.</p></body>")


def _watch_bridge(win) -> None:
    """Close the window (exit the app) once the bridge stops responding, e.g.
    right after the in-app Shutdown button stops it."""
    time.sleep(3)
    misses = 0
    while True:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                misses = 0
        except OSError:
            misses += 1
            if misses >= 2:
                try:
                    win.destroy()
                except Exception:
                    pass
                return
        time.sleep(1)


def _set_app_icon() -> None:
    """Windows: replace the inherited python icon with accuretta.ico on the live
    window (titlebar + taskbar). Packaged builds get this from PyInstaller
    --icon; this covers running from source via pythonw."""
    if sys.platform != "win32":
        return
    ico = os.path.abspath("accuretta.ico")
    if not os.path.exists(ico):
        return
    try:
        import ctypes
        # Give the process its own taskbar identity so it doesn't ride under python.
        try:
            aumid = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
            aumid.argtypes = [ctypes.c_wchar_p]
            aumid("Accuretta.Desktop")
        except Exception:
            pass
        u = ctypes.windll.user32
        u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        u.FindWindowW.restype = ctypes.c_void_p
        u.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        u.LoadImageW.restype = ctypes.c_void_p
        u.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
        IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
        WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
        hwnd = None
        for _ in range(80):  # wait up to ~8s for the window to exist
            hwnd = u.FindWindowW(None, "Accuretta")
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return
        big = u.LoadImageW(None, ico, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
        small = u.LoadImageW(None, ico, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
        if big:
            u.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
        if small:
            u.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
    except Exception:
        pass


def main() -> int:
    if not _acquire_single_instance():
        webview.create_window("Accuretta", html=_ALREADY_RUNNING_HTML, width=440, height=220)
        webview.start(gui="edgechromium" if sys.platform == "win32" else None)
        return 0

    # Show the branded splash immediately; boot the bridge in the background and
    # swap to the live app the moment it's ready.
    # text_select=True: pywebview disables page text selection by default, which
    # meant you couldn't highlight-and-copy part of a reply in the desktop app
    # (only the whole message via the copy button). Turning it on restores normal
    # selection + Ctrl+C + right-click copy. UI chrome stays unselectable via the
    # `user-select: none` CSS on buttons, the sidebar, and code line numbers.
    splash_shown_at = time.monotonic()
    win = webview.create_window("Accuretta", html=_build_splash_html(),
                                width=1440, height=920, min_size=(960, 640),
                                text_select=True)

    def _boot() -> None:
        def _stage(label: str, pct: int) -> None:
            # Push a live boot stage into the splash (label + progress fill).
            try:
                win.evaluate_js(f"window.__splashStage && window.__splashStage({label!r}, {pct})")
            except Exception:
                pass

        if not _port_in_use(PORT):
            _stage("Starting local bridge", 18)
            threading.Thread(target=_run_bridge, daemon=True).start()
        else:
            _stage("Bridge already online", 40)
        _stage("Waiting for local engine", 55)
        if _wait_ready():
            _stage("Opening workspace", 100)
            # Warm launches can finish before the opening choreography becomes
            # legible. Guarantee a short total presentation, but never pad a
            # genuinely slow boot beyond the final ready transition.
            elapsed = time.monotonic() - splash_shown_at
            time.sleep(max(0.38, 1.45 - elapsed))
            win.load_url(f"http://127.0.0.1:{PORT}")
            threading.Thread(target=_watch_bridge, args=(win,), daemon=True).start()
        else:
            win.load_html(_FAILED_HTML)

    # Swap the python icon for accuretta.ico once the window exists.
    threading.Thread(target=_set_app_icon, daemon=True).start()

    # gui='edgechromium' forces WebView2 on Windows for identical Chromium rendering.
    # private_mode=False + a stable storage_path: pywebview defaults to private
    # mode, which tells WebView2 to DISCARD localStorage/cookies between launches
    # — that wiped the cost widget's all-time token counters every restart (the
    # card read $0.00 until you chatted again). Pin the profile under the app's
    # own data dir (same place chats.json lives) so it persists like everything else.
    _storage = str(bridge.DATA / "webview")
    try:
        os.makedirs(_storage, exist_ok=True)
    except Exception:
        _storage = None
    webview.start(_boot, gui="edgechromium" if sys.platform == "win32" else None,
                  private_mode=False, storage_path=_storage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
