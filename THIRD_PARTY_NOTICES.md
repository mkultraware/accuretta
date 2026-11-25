# Third-party notices

Accuretta bundles the components below. Each keeps its own license; the full license texts
sit next to the files, in `assets/vendor/`. `assets/vendor/manifest.json` records where each
vendored file came from and its SHA-256.

## Bundled in this repository

| Component | Version | License | Where |
|---|---|---|---|
| Phosphor Icons (web) | 2.1.1 | MIT | `assets/vendor/web/` (regular, bold and fill weights, woff2 only) |
| highlight.js | 11.9.0 | BSD-3-Clause | `assets/vendor/highlight.js/` |
| html2canvas | 1.4.1 | MIT | `assets/vendor/html2canvas/` |
| JSZip | 3.10.1 | MIT (dual MIT/GPLv3; used under MIT) | `assets/vendor/jszip/` |
| qrcode-generator (Kazuhiko Arase) | 1.4.4 | MIT | `assets/vendor/qrcode-generator/` |
| Lobe Icons (LobeHub) | — | MIT | `assets/vendor/lobe-icons/` (except `thinkingmachines.svg`, drawn for Accuretta after the lab's site icon) |
| PostCSS parser bundle: postcss, nanoid, picocolors, source-map-js | — | MIT, MIT, ISC, BSD-3-Clause | `assets/vendor/postcss/` |
| Inter Tight | — | SIL OFL 1.1 | `assets/vendor/fonts/` |
| JetBrains Mono | — | SIL OFL 1.1 | `assets/vendor/fonts/` |
| Space Mono | — | SIL OFL 1.1 | `assets/vendor/fonts/` |
| Fragment Mono, Newsreader, Schibsted Grotesk | — | SIL OFL 1.1 | `assets/vendor/fonts/atelier/` |

"QR Code" is a registered trademark of DENSO WAVE INCORPORATED.

## Not bundled: installed separately

These Python packages are listed in `requirements.txt` and installed by the user from PyPI.
They aren't part of this repository. Accuretta imports them only when the feature that needs
them is used.

pillow, pyautogui, pygetwindow, uiautomation, pywebview, playwright, PySquashfsImage,
pyelftools, capstone, androguard, pefile, yara-python, pyghidra, pypdf, pdfplumber,
tree-sitter (and its Python, JavaScript and TypeScript grammars), flake8, evtx, dpkt,
cryptography and discord.py. On macOS: pyobjc-framework-WebKit and pyobjc-framework-Quartz.

External programs Accuretta can start, such as llama.cpp's `llama-server`, the Maya engine,
Ghidra and WSL, are installed separately and keep their own licenses.

If a Windows installer is published, it will bundle Python (PSF License), Tcl/Tk 8.6
from the same Python release (Tcl/Tk license, BSD-style; used by the folder and file
pickers), and a small set of these packages. It will list their licenses in this file
at that point.
