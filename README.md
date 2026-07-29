<div align="center">

<picture>
  <source srcset="logo-mark-dark.png" media="(prefers-color-scheme: dark)">
  <source srcset="logo-mark-light.png" media="(prefers-color-scheme: light)">
  <img src="logo-mark-light.png" alt="Accuretta logo" width="130" />
</picture>

# Accuretta

**A local AI workspace. Your model, your machine, your files.**

[![License: personal use only](https://img.shields.io/badge/license-personal%20use%20only-B5544A.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![Powered by llama.cpp](https://img.shields.io/badge/backend-llama.cpp-orange.svg)](https://github.com/ggml-org/llama.cpp)
[![Runs local](https://img.shields.io/badge/runs-100%25%20local-brightgreen.svg)](#privacy)

</div>

<p align="center">
  <img src="media/accuretta-demo-loop.gif" alt="Accuretta � local AI workspace demo" width="900">
</p>

<p align="center">
  <img src="welcome_screen.png" alt="The Accuretta chat UI: session list on the left, a workspace tree, the chat with mode chips (IDE, Agent, Auto, Image, Trust writes), and a preview pane with Terminal, Backend, Shell, and Agent log tabs on the right." width="880" />
</p>

## What it is

You drop a GGUF model file in a folder, point Accuretta at a llama.cpp binary, and you get a chat UI with real tool use: a live HTML preview, a workspace the model can read and write, a Python syntax checker, interactive shells, web search, and a full security-analysis toolkit. Everything runs on your computer. The model stays on your disk, and your prompts never touch anyone else's server.

Under the hood it's a few static files and one Python script (`bridge.py`) sitting on top of `llama-server`. No build step, no npm, no Electron. You can read the whole thing in an afternoon.

## Why I built it

I got tired of renting AI. I was paying for cloud subscriptions and watching the terms move every few weeks. One service cut quotas. Another swapped the model behind the same name without saying so. I tried Google Antigravity, decided I didn't want tools that could change under me, and started building something I actually own.

Two rules from day one:

1. The model lives on my disk. Nothing leaves the computer unless I ask for it.
2. No subscriptions. I already paid for the GPU.

I came from Ollama and expected llama.cpp to be a sidegrade. It wasn't. Same hardware, same model file, faster generation, and real control over KV cache quantization, flash attention, and speculative decoding. The catch is that you wire it up yourself. Accuretta is that wiring with a usable UI on top.

---

## Setup (start here)

This is the whole thing, step by step. You need three items: Python, one model file, and a llama.cpp binary. Accuretta can fetch the binary for you, so really you need Python and a model.

### 1. Install Python

Get Python 3.10 or newer from [python.org](https://www.python.org/downloads/). On Windows, tick **"Add Python to PATH"** in the installer. Confirm it worked:

```
python --version
```

### 2. Get the code

Download or clone this repository into a folder, for example `C:\accuretta`.

### 3. Install dependencies

Open a terminal in that folder and run:

```
pip install -r requirements.txt
```

Only Pillow is genuinely needed. Everything else is optional and loads only if it's present, so you can skip this and the app still runs. The optional packages just turn on extra tools (screenshots, APK/binary analysis, PDF parsing, the Discord bridge, and so on) when you install them.

### 4. Get a model

You need one GGUF model file on disk. Grab one from Hugging Face (search "GGUF"; **unsloth** and **bartowski** publish reliable ones). A `Q4_K_M` quant of a 7B to 35B model is a sane first pick. Put it anywhere, for example `D:\MODELS\`.

### 5. Start it

- **Windows, the easy way:** double-click `start.bat`. It frees the port, installs the desktop-window dependency once, and opens Accuretta as its own application window. No browser, no leftover console.
- **Any OS:** run `python bridge.py`, then open the URL it prints (usually `http://localhost:8787`) in your browser.

### 6. Let the setup wizard finish the job

On first launch a wizard opens and walks you through the rest.

<p align="center">
  <img src="media/setup_process.png" alt="The Accuretta setup wizard scanning system hardware, offering a one-click llama.cpp binary download matched to the detected GPU, and listing detected GGUF models." width="820" />
</p>

It does four things:

- scans your GPU and picks the right llama.cpp build (CUDA for NVIDIA, Vulkan for AMD/Intel, CPU otherwise),
- downloads that binary for you if you don't already have one,
- lists the GGUF models it found on your drives,
- auto-tunes the model you choose (context size, GPU offload split, cache type) before it starts the backend.

Click **Save & Start** and you're chatting. Your sessions, settings, workspace pointers, and memories land in a `data/` folder next to `bridge.py`. Back it up to keep them, delete it for a clean slate.

### When something breaks

A few failures are common enough to name:

- **llama-server crashes instantly on NVIDIA.** You're missing the CUDA runtime DLLs. Download the `cudart-llama-bin-win-cuda-*.zip` that matches your build and extract it into the same folder as `llama-server.exe`. Match the CUDA version to your driver: run `nvidia-smi` and read the CUDA Version in the top right. A CUDA 13 build needs a 13.x driver.
- **`error loading model: missing tensor 'blk.NN.ssm_conv1d.weight'`.** Your llama.cpp is too old for that model, usually a new MTP or hybrid GGUF. Use a non-MTP version of the same model, or update your binary.
- **llama-server exits the moment speculative decoding turns on.** Set Settings → Speculative decoding to `off` (or `ngram-mod`) and reload. `draft-mtp` only works on models that ship MTP heads and a recent build.
- **Port 8787 already in use.** Something else is on it. `start.bat` clears it for you; in manual mode set `ACCURETTA_PORT` to another number.

---

## What you can do with it

### Build things and watch them render

Ask for a webpage and you see it render next to the chat as the model writes it. Switch between the rendered view and the source with one click. IDE mode keeps the model in "emit an HTML fence, don't wrap it in a tool call" behavior so the preview stays live.

<p align="center">
  <img src="Coding_Agent.png" alt="Accuretta in IDE mode: the model has written an HTML page for a PR firm, the source is visible in the chat, and a polished dark 'AURELIUS' site renders live in the preview pane. The agent log streams tool activity underneath." width="880" />
</p>

<p align="center"><em>IDE mode. The model wrote the page, the preview pane renders it live from the HTML fence, and the agent log tracks every tool call.</em></p>

### Let the agent touch the machine, with approvals in the way

The agent has hands. It reads and writes files, runs shell commands, opens interactive shells, fetches web pages, searches the web, takes screenshots, and inspects processes and network state. Anything that changes your system (file writes, shell commands, registry edits) is gated by an approval card, so nothing destructive happens silently. Read-only work like web fetches can run on its own when you flip on **Trust writes** or approve a class of action.

<p align="center">
  <img src="Approval_Gate_Plus_Discord.png" alt="An approval card in Accuretta showing a pending command, alongside the same approval arriving as a Discord message that can be approved with a reaction." width="880" />
</p>

<p align="center"><em>The approval gate holds every write and command. The same prompt reaches you over Discord, where a reaction runs or denies it, so the safety gate works from your phone.</em></p>

### Authorized red-team tooling

Turn on **Red team tools** in Settings and the model gains a recon and exploitation suite: stealth port scanning, TLS audit, HTTP fingerprinting, passive subdomain enumeration, DNS recon, content discovery, exposure checks, subdomain-takeover detection, CVE matching, an injection and SQL-injection prober, a request fuzzer, an auth-spray primitive, a raw HTTP client, JWT decode/forge/crack, an encoder/decoder, front-end secret scanning, CORS probing, raw TCP, and evidence capture. It's off by default so a normal coding turn doesn't carry a dozen tools it will never use.

The suite is gated twice: the Settings toggle, and an authorization prompt before any run. Point it only at systems you own or have written permission to test.

<p align="center">
  <img src="RedTeam_Start.png" alt="The start of an authorized red-team run in Accuretta: the model confirms authorization and begins reconnaissance against a target." width="880" />
</p>

<p align="center">
  <img src="RedTeam_Finish.png" alt="The end of a red-team run: the model has finished the flow and summarized findings." width="880" />
</p>

<p align="center"><em>An authorized run, start to finish. Authorization first, then recon and exploitation, then a written summary of what it found.</em></p>

### See what's talking to your network

Ask for a network snapshot and the model groups active TCP and UDP connections by process, flags anything odd, and summarizes recent DNS activity in a real table. No round trip to a cloud, no API key, no rate limit.

<p align="center">
  <img src="Blue_Team_Recon_NetworkSnapshot.png" alt="Accuretta running a local network snapshot and analyzing active connections grouped by process, with a summary of recent DNS activity." width="880" />
</p>

### Take apart untrusted files

The bridge ships a reverse-engineering toolkit. Every scanner returns one structured report the model can reason over instead of a raw dump.

- **APKs.** `scan_apk` does pure-Python triage (package metadata, signing certs, dangerous permissions, exported components, and a secret hunt over DEX and native libs). `decompile_apk` shells out to [JADX](https://github.com/skylot/jadx) for Java sources when you've narrowed down what matters.
- **Native binaries.** `binary_inspect` gives fast PE/ELF/Mach-O triage (sections with entropy, imports, packer hints, signature presence) in about 50ms. `ghidra_analyze` runs [Ghidra](https://github.com/NationalSecurityAgency/ghidra) in-process via [pyghidra](https://pypi.org/project/pyghidra/) for a full report plus C-like decompilation of a named function.
- **Firmware.** Squashfs extraction, ELF parsing, and a multi-architecture disassembler (via `PySquashfsImage`, `pyelftools`, `capstone`) for router and IoT images.
- **Pattern matching.** `yara_scan` runs YARA rules (a bundled malware-tell set, or your own `.yar`) over a file or directory in tens of milliseconds.

Optional packages, installed only if you want the feature:

```
pip install androguard              # full APK manifest / permissions / signing
pip install pefile pyelftools yara-python
pip install pyghidra                # needs Ghidra + JDK 21 (adoptium.net)
```

If a package is missing, the tool returns a "install with: pip install ..." note instead of crashing.

### Run risky things in a throwaway Linux box

Set up the optional **sandbox** and Accuretta provisions an isolated Ubuntu guest (`accuretta-sbx`) over WSL2. The model runs offensive tools and, more to the point, unpacks untrusted files pulled back from a target inside that guest, so a booby-trapped sample can't touch your host. Your workspace is visible inside at `/mnt/...`, and the guest is kernel-isolated from Windows. One click in the Setup Wizard or Settings builds it; there's no reboot on a machine that already has WSL2.

### Drive a persistent shell

`run_powershell` is one-shot and forgets everything between calls. When you need state (a reverse shell you caught, an SSH session, a Python REPL, a database client, a debugger), the interactive **session** tools hold a live process across turns. You and the model share the same shell in the Shell tab, so you can watch it work and type into it yourself.

### Agentic coding helpers

Three standard-library tools keep token cost low on big projects: `read_skeleton` pulls the classes, functions, and signatures out of a 5,000-line file for a few hundred tokens; `check_syntax` runs `ast.parse` (or `node --check`) so the model verifies its own edits; and `run_tests` runs `pytest`/`npm test` and hands back only the failures and tracebacks.

### Plug in MCP servers

Accuretta speaks the Model Context Protocol through a dependency-free stdio client built into `bridge.py`. Drop a `bridge_mcp_config.json` next to the bridge (same schema Claude Desktop uses), restart, and the server's tools appear in the model's toolbelt. Every MCP call is approval-gated by default, since an MCP server can run arbitrary commands.

Browser automation (playwright-mcp) works without any vision projector: accessibility snapshots are text, anonymous layout-container noise is stripped before they hit the context, and oversized results get head/tail elision instead of a dead-end "too large" stub — so big pages don't stall the agent. Screenshots come back as a note pointing the model at the text snapshot, so you never need to load an mmproj just for the browser.

```json
{
  "mcpServers": {
    "github": {
      "command": "npx.cmd",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_abc123..." }
    }
  }
}
```

### A real example, start to finish

I asked the agent to tune my in-ear monitors (Linsoul 7Hz x Crinacle Zero:2) with Peace Equalizer APO. It searched audio review sites, Reddit, and AutoEQ measurement databases, picked a target curve (Harman In-Ear 2019), generated ten parametric filters with the right gain, Q, and frequency for that specific IEM, and wrote a complete `.peace` profile straight into the EqualizerAPO config folder, including a PreAmp value to prevent clipping and a note that one boost was unusually aggressive so I could dial it back.

<p align="center">
  <img src="media/Sound_Question_and_search.png" alt="The agent researching IEM tuning across reference-audio-analyzer.pro, audiosciencereview.com, head-fi.org, and Reddit." width="780" />
</p>

<p align="center">
  <img src="media/sound_profile_applied_by_accuretta.png" alt="The agent writing a complete Peace Equalizer profile to disk with activation instructions." width="780" />
</p>

<p align="center"><em>Research, then ten filters written to disk with activation steps and a frank note about the aggressive corrections. No copy-pasting from a forum, no translating frequency tables into config syntax by hand.</em></p>

---

## Auto-tune

Picking a model in Settings (with a VRAM tier set) runs a tuner that reads the GGUF header for the model's real architecture (layer count, attention config, MoE expert count, KV head dimensions) and computes the largest context window plus the right CPU/GPU offload split for your card. No hand-picking `--n-cpu-moe`, `--ctx-size`, or `--batch-size`.

- **GGUF-direct math.** KV cost per token comes from the model's actual `2 x n_layer x head_count_kv x head_dim x dtype_bytes`, not a size bucket. A Q3 of an architecture gets more context than a Q4 of the same one, because the smaller weights leave more VRAM for cache. Split K/V cache (`--cache-type-k q8_0 --cache-type-v q4_0`) is used when it buys real context — K is the quantization-sensitive half (KIVI, ICML 2024), V shrugs off 4-bit.
- **MoE aware.** For mixture-of-experts models it works out the dense-vs-expert split and offloads only as many expert layers to CPU as needed to fit, gating the trade on a decode-speed estimate (RAM bandwidth ÷ active expert bytes) instead of a fixed cap. Speculative decoding is auto-disabled on MoE since it's net-negative there.
- **Swap-aware.** Re-tuning while another model is loaded counts the running instance's RAM/VRAM as free — the swap releases it — so a re-tune at boot or model-switch can't talk itself into a crippled config. A sibling vision projector (mmproj) is budgeted too, and configs that still don't fit are never saved over a working one.
- **Grow-only context.** If a re-tune comes back smaller than what you already had working, the larger value wins. Your saved context never shrinks behind your back.
- **Re-runs on boot.** Auto-tune quietly re-runs in the background at startup and updates flags if the algorithm improved since you last saved. One toast tells you what changed.

Bigger context isn't always better. Attention slows down as the window grows, even before the conversation fills it. If you care more about tokens per second than maximum length, lower the context manually for that task. On a 16 GB card with a small MoE, 32K to 65K is usually the sweet spot for sustained 30+ tok/s.

## Reach it from your phone

The bridge listens on your LAN, so any device on the same network can open `http://<your-machine-name>:8787`. Pair that with [Tailscale](https://tailscale.com) and you have a private AI server reachable from anywhere: same UI, same model, same history, nothing leaving your tailnet. No cloud relay, no port forwarding, no open hole to the internet. Install Tailscale on the host and on whatever you want to chat from, and the URL just works. The mobile UI is built for exactly this.

The Discord bridge is the other remote path, and it's better for firing off a task from your lock screen. Accuretta runs as a bot that connects outbound to Discord, so your machine stays closed. It obeys exactly one Discord user id (yours), and every write still needs approval, which you give by reacting.

```
pip install discord.py
```

Then create an app at [discord.com/developers](https://discord.com/developers/applications), copy the bot token, turn on the Message Content Intent, paste the token and your user id into Settings → Discord remote bridge, toggle it on, and restart. DM the bot and it does anything you'd ask in the web UI, prompting for approval on anything that touches the machine.

## Privacy

Nothing about you, your prompts, or your files leaves your computer. The bridge talks to two things on localhost: your llama-server instance and your browser. There's no telemetry, no analytics, no account, no cloud sync.

The one outbound channel is the agent's own web fetch and search. When the model reads a URL, that request goes from your machine to that site, the same way your browser would. Some models ask first, others just do it as part of answering. Nothing is sent unless the model decided it needed something off the open web for the task you gave it.

If you want to check, run Wireshark next to it. The only outbound traffic you'll see is what the agent fetched. Want silence? Block the bridge process at the firewall, or unplug the network. The model runs fully offline once loaded, so you can chat all day with no internet.

## What it doesn't do

- It isn't a polished commercial product. There are rough edges, and the docs are mostly this file.
- A 24B model on a laptop won't beat Claude or GPT-5. Local is local. Pick the right tool for the job.
- It doesn't replace llama.cpp. It's a front end on `llama-server`.
- It isn't a code editor. It's a chat workspace that happens to render code, preview HTML, and check Python syntax.

## Repository layout

```
accuretta/
  bridge.py              the Python bridge (model launcher, tool runtime, HTTP server)
  accuretta_app.py       desktop launcher (native window via pywebview, no console)
  start.bat              Windows one-click launcher
  index.html             the UI shell
  app.js                 all UI logic
  app.css                main stylesheet
  colors_and_type.css    theme tokens
  requirements.txt       optional Python dependencies
  data/                  runtime state, created on first run
  media/                 readme assets (screenshots, demo video)
```

## Status

Personal project. I work on it when I feel like it. Pull requests are welcome, but I'm not building a roadmap or chasing stars. If you fork it and make it your own, that's the point.

## License

Free for personal use. Run it, poke at it, build on it for yourself. It's not for commercial use though: if you want it in or for a business, or shipped as part of something you sell, ask me first. And please don't pretend you wrote the parts you didn't.

Formally, it's under the [PolyForm Noncommercial License 1.0.0](LICENSE), a real drafted license that permits any noncommercial use and blocks commercial use.
