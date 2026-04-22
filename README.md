# Accuretta

A local AI agent and micro-IDE that runs entirely on your own hardware. No cloud APIs, no subscriptions, no data leaving your machine.

[![License](https://img.shields.io/badge/license-MIT-orange)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows-blue)](https://github.com)
[![Backend](https://img.shields.io/badge/backend-Ollama-black)](https://ollama.com)
[![Python](https://img.shields.io/badge/python-3.10%2B-green)](https://python.org)

---

## What it is

Accuretta connects a locally hosted LLM (via [Ollama](https://ollama.com)) to your operating system and serves a full chat and code editor UI from a single Python process. You run `start.bat`, the browser opens automatically, and you have a capable AI assistant that can read and write files, run PowerShell commands, browse the web, take screenshots, and drive your desktop — all without touching a third-party server.

The frontend is plain HTML, CSS, and JavaScript with no build step. The backend is a single Python file using only the standard library. There is no Electron, no Node.js, no Docker, and no framework to install or update.

---

## Architecture

```
Browser  (index.html + app.js + app.css)
   |
   |  HTTP + Server-Sent Events
   |  localhost:8787  (or LAN via Tailscale)
   |
bridge.py  (Python stdlib HTTP server)
   |
   +--- Ollama  (localhost:11434, your GPU)
   +--- File system
   +--- PowerShell
   +--- Desktop (pyautogui, optional)
```

Everything lives on your machine. The only outbound traffic is what you explicitly ask the model to fetch.

---

## What makes it different

Most local AI tools are wrappers. They proxy your request to Ollama, show you the text, and stop there. Accuretta goes further in a few specific ways.

**Hardware auto-tune.** The settings panel can scan your GPU, CPU, and RAM and configure Ollama's context window, batch size, GPU layers, and thread count for your specific hardware. It targets 30 or more tokens per second while keeping as much context as your VRAM allows. You can still override everything manually.

**Approval gates on every destructive action.** Writing a file, deleting something, running a PowerShell command, launching an application — each of these pauses the agent and shows you a card with a preview of exactly what it is about to do. You approve or deny. The model cannot bypass this. Read-only actions like listing a directory or reading a file run without interruption.

**Machine context injection.** On first run, the bridge scans your machine and writes a markdown file describing your folder structure, drives, and system details. This file is injected into every chat so the model already knows where your Desktop, Documents, and Screenshots folders are without you having to explain it. You can edit or rescan it at any time from Settings.

**Persistent memory across sessions.** The model can save facts about you across conversations using a `remember` tool. These memories are ranked by recency and usage frequency, deduped automatically, and injected into the system prompt on every new session. You can view, add, or delete individual memories from the Settings panel.

**Per-session desktop kill switch.** Desktop automation can be enabled or disabled per chat without touching the global setting. A panic button in Settings stops all desktop actions immediately, regardless of what the model is doing.

**Live preview with version history.** When the model generates an HTML page or web app, it appears in a resizable preview pane on the right. Every version is saved automatically. You can step back through history, re-run any version, take a screenshot, export as a zip, or copy the whole thing as a data URL. The Tailwind CDN and multi-file output mode are optional toggles in the toolbar.

**Accessible from your phone.** The server binds to all interfaces. If you have [Tailscale](https://tailscale.com) installed, you can reach your desktop Accuretta instance from your phone or laptop over your private mesh network without any port forwarding.

---

## Tools the agent can use

| Tool | Approval required | Notes |
|---|---|---|
| `list_directory` | No | Respects workspace bounds and `.accurettaignore` |
| `read_file` | No | 512KB cap, workspace-gated |
| `write_file` | Yes | Blocked outside workspace and Windows system paths |
| `delete_file` | Yes | Permanent, shown clearly in approval card |
| `run_powershell` | Yes for writes | Read-only commands run automatically |
| `open_program` | Yes | Must be in your app allowlist |
| `web_fetch` | No | Python-side HTTP, bypasses browser CORS |
| `remember` | No | Saves a fact to persistent memory |
| `forget` | No | Removes a memory by ID |
| `screenshot` | No | Returns a base64 image of your screen |
| `describe_screen` | No | Screenshot plus a vision model description |
| `list_windows` | No | Returns open window titles and positions |
| `desktop_launch_app` | Yes | Allowlist-gated |
| `desktop_focus_window` | Yes | By window title substring |
| `desktop_click` | Yes | Pixel coordinates derived from screenshot |
| `desktop_type_text` | Yes | 2000 character cap |
| `desktop_press_keys` | Yes | Combos like `ctrl+s`, `alt+tab` |
| `desktop_close_window` | Yes | By window title substring |

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10 or 11 | Desktop automation requires Windows. The rest runs on any OS with minor changes. |
| Python 3.10 or later | No third-party packages required for core features |
| [Ollama](https://ollama.com) | Any version that supports `/api/chat` |
| 8GB VRAM minimum | 16GB recommended for models at 14B parameters and above |

**Optional packages for desktop automation:**

```
pip install pyautogui Pillow pygetwindow
```

The bridge starts cleanly without these. Desktop tools report clearly when a dependency is missing rather than crashing.

---

## Quick start

**1. Pull a model**

```
ollama pull qwen2.5:14b
```

For lower VRAM:

```
ollama pull qwen2.5:7b
```

**2. Clone the repo**

```
git clone https://github.com/mkultraware/accuretta
cd accuretta
```

**3. Start**

```
start.bat
```

The launcher frees port 8787, stops any stale bridge process, finds your Python installation, prints your LAN addresses, and opens the browser once the server is ready.

**Manual start if needed:**

```
python bridge.py
```

Then open `http://localhost:8787` in your browser.

---

## Remote access via Tailscale

1. Install [Tailscale](https://tailscale.com) on your PC and your remote device
2. Sign in to the same account on both
3. Run `start.bat` on your PC
4. On your phone or laptop, open `http://YOUR_PC_TAILSCALE_IP:8787`

No port forwarding needed. The bridge serves over your private Tailscale network. Do not forward port 8787 on your router. The bridge has no authentication layer beyond the network. Tailscale is the access control.

---

## Recommended models

Any Ollama model with tool call support will work. These have been tested:

| Model | VRAM needed | Strengths |
|---|---|---|
| `qwen2.5:14b` | ~10GB | Good all-rounder |
| `qwen2.5-coder:14b` | ~10GB | Code and file tasks |
| `qwen3:8b` | ~6GB | Fast, lower VRAM |
| `qwen3:32b` | ~20GB | Strong reasoning |

Models without native tool call support can still work through the fallback `<tool_call>` XML parser built into the bridge.

---

## Modes

The composer toolbar has three mode chips:

**Auto** (default) — The model decides whether to use tools or generate text. Best for most tasks.

**IDE** — Tool use is disabled. The model focuses on generating code for the preview pane. Faster for pure code generation.

**Agent** — Tool use is enabled with full agentic looping. The model chains tool calls until the task is done.

---

## Project structure

```
accuretta/
  index.html     UI shell
  app.js         All frontend logic, streaming, tool rendering, IDE, preview
  app.css        Styles
  bridge.py      Python backend, Ollama proxy, all tool implementations
  start.bat      Windows launcher
  data/          Created on first run (chats, settings, versions, memories)
```

---

## Who this is for

**Developers who want a local code assistant with real file access.** Not a chat window that suggests edits you paste in manually. The agent reads your actual files, writes changes, and runs your tests with your approval on every step.

**Privacy-conscious users who work with sensitive material.** Legal documents, medical records, personal finances. The model sees your data and nothing else does. There is no telemetry, no usage logging, no API key tied to an account.

**People who travel and want access to their home workstation.** Tailscale lets you run a heavy local model on your desktop GPU and reach it from a phone or laptop anywhere without exposing anything to the internet.

**AI enthusiasts who want to understand how agentic systems work.** Every tool call, every approval gate, and every streaming chunk is visible in the UI. The codebase is two files with no framework abstraction in the way.

**Power users who want to automate repetitive desktop tasks.** The desktop tools let the model take screenshots, identify what is on screen, and drive your mouse and keyboard to work through GUI applications that have no API.

---

## Use cases

**File and project work**

Dump a folder into the workspace, ask the agent to read everything, then have it refactor code across multiple files, rename variables project-wide, or write a summary of what the codebase does. Every write requires your approval.

**Building web UIs**

Switch to IDE mode and describe what you want. The preview pane shows the result instantly. Ask for changes, the model patches the code, and the preview updates. Version history lets you go back if an iteration went wrong. Export the final result as a zip or as a single self-contained HTML file.

**Research and summarization**

Give the agent a list of URLs. It fetches each one server-side, strips the markup, and works through the text without you hitting CORS restrictions or copy-pasting anything.

**Desktop automation**

Enable desktop automation in Settings, add the applications you want the agent to be able to launch to the allowlist, and then describe the task. The agent takes a screenshot to orient itself, derives coordinates, and clicks through the UI. Every action needs your approval before it runs.

**System administration**

Ask the agent to check what is running, query disk usage, find large files, clean up a directory, or pull and install something from a repo. PowerShell commands that modify the system surface an approval card. Read-only commands run without interruption.

---

## Known limitations

**Windows-first.** Desktop automation, some path handling, and the launcher script are Windows-specific. The core chat and IDE features work on other platforms but are not tested there.

**No authentication.** Anyone who can reach port 8787 on your network has full access. Use Tailscale or a firewall rule. Do not expose the port publicly.

**Stop halts the stream, not the generation.** Pressing Stop cuts the UI stream immediately but the current Ollama generation completes in the background before token output actually stops. For most models this gap is under a second and unnoticeable. The previous approach killed and restarted Ollama entirely on every stop, which was faster on paper but added 5 to 10 seconds of dead time and broke any other sessions sharing the instance. The current behavior is a deliberate tradeoff. The only case where it is meaningfully frustrating is a model looping on tool calls with no exit condition, which on consumer hardware tends to hit the context ceiling before it becomes a practical problem anyway.

**Single-threaded tool execution.** Tool calls run synchronously in the worker thread. Long-running PowerShell commands block that thread for the duration.

**No tests.** There is no test suite. The project is a solo build in active development.

---

## License

MIT. Use it, fork it, build on it.

---
