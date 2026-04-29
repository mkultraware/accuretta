# Accuretta

A local AI agent and IDE that runs on your own computer. No accounts. No subscriptions. No monthly quota emails. You point it at a model file on your disk and it just works.

This folder is the portable build. Drop it anywhere, run `start.bat`, and the rest takes care of itself.

![Accuretta in action](screenshot.png)

## Why this exists

Honestly this started as a side project that came out of a frustration that kept building. I had been paying for various AI subscriptions and watching the goalposts move every few weeks. One company would cut quotas. Another would silently downgrade the model behind the same name. Then Google released Antigravity IDE and I tried it for a while, and that was the moment I got fed up. The whole thing felt like another rented relationship with a tool I was supposed to depend on.

So I started building Accuretta for myself, with two rules:

1. Everything runs locally. The model lives on your disk. The bridge runs on your machine. Nothing leaves the computer unless you explicitly tell it to.
2. No subscriptions. Ever. You already paid for the GPU.

I am very new to llama.cpp. Before this I was an Ollama user and I figured Ollama was the easy way to run local models. After switching to llama.cpp directly through llama-server, the performance jump was bigger than I expected. Same hardware, same model file, noticeably faster generation and cleaner control over things like KV cache quantization, flash attention, and speculative decoding. The tradeoff is that you have to wire it up yourself instead of letting Ollama abstract it away. Accuretta is partly the wiring.

## Who this is for

* People who want a Cursor or Antigravity style experience without the subscription
* People who already have a decent GPU and would rather use it than rent one through an API
* Tinkerers who want to swap models around (Qwen, Llama, Gemma, anything llama.cpp supports) and see what works best for their box
* Anyone who got tired of watching big AI companies decide what their tool is allowed to do this week

## Use cases

* Writing code with the model right next to a live preview, using your own files as context
* Drafting prose, marketing copy, documentation, anything where you do not want it sent to a server
* Quick agent tasks like "rename these files based on their contents", or "write a small landing page and save it", or "scrape this URL and pull out the prices"
* Spinning up a model briefly to ask a single question without burning an API quota
* Running on an offline machine. Once a model is on disk, no internet is required.

## What you need

Three pieces. The launcher handles the Python side automatically. You install the other two once.

1. **Python 3.10 or newer.** This runs the bridge.
2. **`llama-server.exe`.** This is the actual model runtime. It comes from the llama.cpp project.
3. **A .gguf model file.** These are the model weights.

## Step 1. Install Python

If `python --version` already prints 3.10 or higher in a terminal, skip this.

Otherwise:

1. Download the Windows installer from https://www.python.org/downloads/
2. Run it. **Tick "Add Python to PATH"** on the first screen. This is the most common thing people forget.
3. Click "Install Now".

That is it. Open a fresh terminal and `python --version` should now print a version.

## Step 2. Get llama-server.exe

Pick whichever path is easiest for you.

### Option A. Unsloth Studio (easiest, includes CUDA)

If you have an NVIDIA GPU, the Unsloth Studio app at https://unsloth.ai installs a working CUDA enabled `llama-server.exe` you can reuse. After Unsloth installs, you will find it at:

```
C:\Users\<you>\.unsloth\llama.cpp\build\bin\Release\llama-server.exe
```

Accuretta searches that path automatically.

### Option B. Official llama.cpp release

Go to https://github.com/ggml-org/llama.cpp/releases and download the build that matches your hardware:

* NVIDIA GPU: pick a `*-bin-win-cuda-*.zip`
* CPU only, AMD, or Intel iGPU: pick `*-bin-win-vulkan-*.zip`
* AMD GPU on ROCm: pick `*-bin-win-hip-*.zip`

Extract the zip somewhere permanent. A common landing spot is `C:\llama.cpp\` since Accuretta auto detects that location. If you put it elsewhere, you will point Accuretta at it from Settings.

If you have an NVIDIA GPU, use the CUDA build. The CPU only build will work but will be 5 to 10 times slower and will dump everything into system RAM.

### Option C. Already have it from somewhere else

Anywhere is fine. You will point Accuretta at it from Settings.

## Step 3. Get a .gguf model

Models are large (2 to 20 GB or more). Pick one that fits your VRAM and RAM. Two solid sources:

* https://huggingface.co/unsloth has well quantized models that work great with llama-server
* https://huggingface.co/bartowski has a huge selection of quants

Rough guide for picking a quant by VRAM:

| Your VRAM   | Pick                          | Example file                          |
| ----------- | ----------------------------- | ------------------------------------- |
| 8 GB        | a 4 to 8B model at `Q4_K_M`   | `Qwen2.5-7B-Instruct-Q4_K_M.gguf`     |
| 12 GB       | 7 to 14B model at `Q4_K_M` to `Q5` | `Qwen2.5-14B-Instruct-Q4_K_M.gguf` |
| 16 GB       | 14B at `Q5_K_M` to `Q6_K`     | `Qwen2.5-14B-Instruct-Q5_K_M.gguf`    |
| 24 GB+      | 32B at `Q4` to `Q5`           | `Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf` |

Drop the .gguf file anywhere. Accuretta scans the folder you pick recursively. A common spot is `C:\Users\<you>\MODELS\`.

## Step 4. First run

1. Double click `start.bat`. It will:
   * Create a virtual environment in `.venv\` (one time, about 10 seconds)
   * Install Python dependencies (one time, about 30 seconds)
   * Start the bridge on http://127.0.0.1:8787
   * Open your browser automatically
2. Click the gear icon (top right) to open Settings.
3. Under Model:
   * **Models folder.** Click the folder icon and pick the folder that contains your .gguf files.
   * **Chat model.** Pick one from the dropdown. Accuretta will spawn `llama-server` with it.
4. Optional. If your `llama-server.exe` is in an unusual place, scroll down to the advanced section and paste its full path into `llama_bin`.

That is the whole setup. From here you chat in the main window like any other IDE or chat app, but everything runs locally.

## Where things live

* `start.bat` is the launcher. It creates a venv, installs dependencies, then runs the bridge.
* `bridge.py` is the Python bridge that wraps llama-server.
* `index.html`, `app.js`, `app.css`, `colors_and_type.css`, `logo-mark.png` are the web UI.
* `data\settings.json` holds your settings. The app creates and updates it.
* `data\chats.json` holds your chat history. Created on first send.
* `data\workspace\` is where files the agent writes go.
* `.venv\` is the virtual environment created on first run.

You can move this folder anywhere and it keeps working. Delete `.venv\` to force a clean reinstall on next run.

## Troubleshooting

**"Python is not installed"** Install from python.org and tick "Add Python to PATH". Open a new terminal afterwards.

**Browser opens but the page is blank.** Wait 5 seconds and refresh. The bridge needs a moment on first start.

**Models dropdown is empty.** Pick a Models folder in Settings that actually contains .gguf files. The scan is recursive, so subfolders count.

**"llama-server not found"** Open Settings, scroll to advanced, paste the full path to `llama-server.exe` into the `llama_bin` field, save.

**Model loads into system RAM instead of GPU.** You are using a CPU only `llama-server.exe`. Re download with the CUDA, Vulkan, or ROCm variant for your GPU. See Step 2.

**Generation is unbearably slow.** Open Task Manager, Performance, GPU. If GPU usage is near zero during a reply, the model is on CPU. Same fix as above.

## Remote access from your phone or another machine

The bridge listens on every local network interface, so anything that can reach your PC can open the chat. The easiest way to do this from outside the house is Tailscale (https://tailscale.com), which gives every device you own a private address that just works without port forwarding or a public IP.

Once you have Tailscale running on both your PC and your phone:

1. Find your PC's Tailscale IP (it usually looks like `100.x.x.x`).
2. From your phone or another laptop, open `http://<ip>:8787/index.html` in any browser, where `<ip>` is the address from step 1.
3. Chat normally.

A few things to know:

* Your PC has to be powered on and the bridge has to be running. There is no cloud component to fall back on.
* The IDE preview pane is hidden on mobile because it is not really usable on a small screen. You still get the chat, the agent, tool use, and approvals. The preview comes back when you open the same URL on a tablet or laptop.
* Anything you type still runs through your local llama-server. Nothing goes to a cloud.

If you do not want to use Tailscale, the same trick works on your home Wi-Fi by browsing to `http://<ip>:8787/index.html` from any device on the same network. The launcher prints the local IP addresses when it starts so you can copy one over.

## Privacy

Nothing leaves your machine. The bridge only talks to:

* `127.0.0.1:8001`, which is your local llama-server
* `127.0.0.1:8787`, which is your browser
* Any URL the agent fetches at your explicit request. You will see an approval card before that happens.

No telemetry, no accounts, no cloud calls.
