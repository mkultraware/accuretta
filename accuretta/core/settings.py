# SPDX-License-Identifier: AGPL-3.0-only
"""Accuretta's settings: the defaults, reading settings.json with every value
normalized, and the offline-mode switch read once at startup.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import re
from accuretta.core.effort import _normalize_effort_choice
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import _normalize_composer_mode
from accuretta.core.persistence import load_json


DEFAULT_SETTINGS = {
    "offline_mode": False,
    "model": "",
    "vision_model": "lighton-ocr",
    # desktop automation (off by default — opt-in, gated behind approvals)
    "desktop_enabled": False,
    # red-team recon suite (off by default — opt-in; keeps ~13 recon tools out
    # of the prompt for normal coding turns)
    "red_team_enabled": False,
    # reverse-engineering / forensics suite (off by default — opt-in; keeps the
    # APK, binary, firmware & forensics scanners out of the prompt for normal
    # coding turns)
    "analysis_tools_enabled": False,
    # durable mission state for authorized red-team runs. Auto-captures the
    # target/scope/objective + confirmed-access facts into per-chat state and
    # re-injects them (as a recency-end tail message — see run_chat_turn) so the
    # model can't drift off-task on long runs. See tool_pin_note / _rt_mission_*.
    "rt_mission_state": True,        # master: auto-capture + recency-end tail inject
    # legacy: the fill-gated echo and its knobs no longer fire (the mission always
    # rides the tail); kept so existing settings files load cleanly.
    "rt_tail_echo": True,
    "rt_tail_trigger_frac": 0,
    "rt_tail_budget_frac": 0,
    # discord remote bridge (off by default — DM accuretta from your phone with
    # push notifications + reaction approvals; outbound only, locked to owner id)
    "discord_enabled": False,
    "discord_bot_token": "",
    "discord_owner_id": "",
    # scheduled security overviews: background read-only scans while the app
    # runs, with notable alerts routed to the in-app popup and (optionally)
    # the owner's Discord DM with presence-aware suppression.
    "security_scheduled": True,
    "security_interval_minutes": 30,
    "security_notify_threshold": "medium",  # low | medium | high
    "security_discord_notify": True,
    # private remote access. Tailscale remains the network identity layer;
    # SSH keys below are scoped per paired machine and never leave ./data.
    "remote_access_enabled": True,
    "remote_command_timeout": 120,
    "remote_file_max_mb": 100,
    "desktop_app_allowlist": [],            # e.g. ["notepad", "chrome", "code"]; matched case-insensitive against exe/launch target
    "desktop_max_actions_per_minute": 30,   # hard rate limit for agent-driven actions
    "desktop_auto_approve_read": True,      # screenshot/describe/list_windows never require approval
    "num_ctx": 8192,
    "num_gpu": 99,
    "num_batch": 512,
    "num_thread": 0,
    "num_predict": -1,
    "temperature": 0.5,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "repeat_penalty": 1.1,
    "presence_penalty": 0.0,
    "frequency_penalty": 0.0,
    "keep_alive": "30m",
    "theme": "light",
    "custom_palettes": {},
    "composer_mode": "agent",     # last selected composer mode: agent | ide
    "agent_prompt_variant": "compact",  # compact default; classic remains the hidden regression fallback
    "tool_verbosity": "lean",   # lean = balanced ~24-tool core + lazy bundles (default for all ctx);
                                # full = legacy ~50-tool core (opt-in for power users / huge ctx)
    "repos_baseline_unique": 0,     # repos worked on before stats existed (seed per machine, 0 = fresh)
    "repos_baseline_sessions": 0,   # branch sessions before stats existed (seed per machine, 0 = fresh)
    "sound_notifications": True,    # smooth chimes for approvals + long-task completion
    # desktop notch overlay: a small always-on-top island at the top edge of the
    # screen that mirrors agent state, gates approvals and takes prompts while
    # the main window is minimized. Off by default; Windows only.
    "notch_enabled": False,
    "keyboard_shortcuts": {},       # user-defined UI key combinations, action -> canonical chord
    "profile_avatar": "",           # small data: URL picture for the Usage stats share card
    "profile_name": "",             # name at the top of the sidebar (Settings > Profile)
    "auto_approve_read": True,
    "auto_approve_write": False,    # trust writes: skip approval for in-workspace file writes/edits (registry/system/powershell still always prompt)
    "approval_mode": "",            # soft|medium|hard; empty = derive from auto_approve_write (legacy)
    "rt_proxy": "",                 # optional proxy for red-team HTTP tools + sqlmap, e.g. http://127.0.0.1:8080
    "rt_spoof_xff": False,          # random X-Forwarded-For/X-Real-IP on recon requests (log obfuscation)
    "rt_jitter": True,              # small random delays between recon requests (timing anti-fingerprint)
    "rt_evidence_retention_days": 30, # keep proof across restarts for report verification; 0 = wipe on boot
    "shodan_api_key": "",           # optional; recon_shodan uses keyless InternetDB when blank
    "allow_web_preview": True,
    # memory / performance
    "kv_cache_type": "q8_0",        # K cache: q4_0 | q8_0 | f16 — lower = less VRAM, slightly lower quality.
                                    # Policy (user-confirmed): auto-tune stays q8_0 (or f16 with headroom);
                                    # q4_0 only as a ≤12 GB VRAM escape hatch.
    "kv_cache_type_v": "",          # V cache: "" = same as K (mirrored at spawn). Measured on this machine
                                    # (Qwen3.6-27B, llama-perplexity): q8_0/q8_0 is +0.2% PPL (lossless);
                                    # q8_0/q4_0 costs +1.6% AND hits a slow mixed-dtype path — the tuner
                                    # never emits split K/V; manual override only.
    # IDE preview extras (composer toolbar toggles)
    "use_tailwind_cdn": False,      # inject Tailwind Play CDN into preview + ask model to use tailwind classes
    "ide_multifile": False,         # tell the model to emit a small folder structure (index.html / style.css / script.js / assets/)
    # reasoning / thinking (Qwen3-family and other reasoner models)
    "enable_thinking": True,        # when False, suppress <think> blocks entirely via chat_template_kwargs
    "thinking_budget": 4096,        # HARNESS-enforced cap on thinking tokens (we count + force-close; the model's template can't be trusted to honor it). -1 = unlimited
    "reasoning_capability_override": "auto",  # auto | off | budget | native_effort; escape hatch for new/odd templates
    "reasoning_effort": "auto",    # last effort for the active model: auto | low | medium | high
    "verification_mode": "auto",   # auto | on | off: Sage-style in-CoT self-review for templates that
                                   # take a `verification` kwarg. auto = only on Deep (high) effort turns
    "subagent_max_tasks": 5,       # spawn_subagent batch cap (oversized batches are rejected, not truncated)
    "subagent_max_turns": 20,      # tool rounds per sub-agent before it must answer
    "max_tool_rounds": 120,        # how many tool-call rounds the model may run per user turn before forced stop
    "debug_tool_parse_failures": False,  # keep raw model output of rounds whose tool call lost its arguments
                                    # in data/tool_parse_failures.jsonl. Stores model output, so off by default.
    "preserve_prior_thinking": False,# replay prior <think> verbatim: bloats context + re-feeds doubt on long runs, so default off
    "summarize_history": True,      # rolling-summary compaction: fold the oldest turns into a summary
                                    # when the context overflows instead of deleting them. False silently
                                    # disables ALL folds — every overflow becomes a plain drop (see the
                                     # trim-triggered fold in run_chat_turn).
    "summary_fidelity_gate": True,  # after a fold writes the note, deterministically re-inject any sticky
                                    # section (DECISIONS/ROADBLOCKS/FILES) or load-bearing anchor the new
                                    # note silently dropped vs the prior note. Add-only, no model call; it
                                    # can never make a note worse. False = exact pre-gate behavior.
    "passive_model_telemetry": True, # content-free local observations from real turns; no extra benchmark/inference
    # llama-server lifecycle (bridge spawns it for us)
    "watchdog_enabled": True,       # auto-respawn llama-server on silent crash (OOM, segfault).
                                    # Circuit breaker stops trying after 3 crashes in 60s — fix
                                    # config and pick the model again in the model menu to resume. /api/models/stop
                                    # disables auto-restart until next /api/models/load.
    "models_dir": "",               # folder containing .gguf files (set via Settings -> Models folder)
    "model_path": "",               # full path to the currently loaded .gguf
    "llama_bin": "",                # override path to llama-server.exe (auto-detected if blank)
    "mmproj_path": "",              # full path to a vision multimodal projector (.gguf). When set,
                                    # llama-server boots with --mmproj <path> and the chat handler
                                    # sends images straight to the loaded model instead of routing
                                    # through the small OCR/vision side-model. Leave blank for
                                    # text-only models or when you've already pruned the vision
                                    # tower from the GGUF to fit more layers in VRAM.
    "mmproj_mode": "auto",         # off | auto | manual. "off" always suppresses --mmproj,
                                    # even when a matching projector exists beside the model.
    "mmproj_auto": True,            # legacy mirror of mmproj_mode for older settings files.
    "mmproj_device": "cpu",         # cpu | gpu: where the vision projector runs. cpu leaves its
                                    # ~1 GB of VRAM to context (~7-12 s more per image); gpu reads
                                    # images in about a second, for cards or multi-GPU setups with
                                    # VRAM to spare. The tuner reserves VRAM only for gpu.
    # llama-server tuning (all map to llama-server CLI flags). Restart required.
    "n_cpu_moe": 0,                 # --n-cpu-moe: how many MoE experts to keep on CPU. 0 = all on GPU.
                                    # The killer flag for fitting big MoE models (Qwen3 35B-A3B, GLM 4.7) on small VRAM.
    "ram_bandwidth_gbps": 30,       # System RAM bandwidth for the auto-tune MoE offload speed model.
                                    # 30 (the default) = AUTO: probe once via memcpy benchmark and cache the result.
                                    # Any other value = manual override (DDR4 ~20, dual-channel DDR5 ~40-60, Apple Silicon 100+).
    "min_tok_s_estimate": 6,        # LEGACY — superseded by min_decode_tok_s below. Kept so old settings
                                    # files still parse; the tuner no longer reads it.
    "min_decode_tok_s": 45,         # Minimum generation speed (tok/s) the auto-tuner plans for. It picks the
                                    # largest context (and MoE offload) whose predicted decode speed at the user's
                                    # usual chat size stays at or above this. Never changes the model's quantization.
    "spec_draft_n_max": 0,          # --spec-draft-n-max for MTP drafting. 0 = auto (3, then tuned per model from
                                    # measured draft acceptance). Measured on a 4090/27B: 1 → 70 tok/s, 3 → 93.
    "moe_cache_mib": 0,             # --moe-cache-mib: GPU cache for MoE experts kept in system RAM (0 = off).
                                    # The tuner sizes it when experts are offloaded and the PCIe link pays for it.
    "exact_fit": True,              # Check the tuner's plan against llama-fit-params (llama.cpp's own memory
                                    # estimate) and spend the measured slack on context. Off = analytic estimate only.
    "safety_reserve_gb": 1.0,       # SYSTEM RAM kept free for the OS — the floor under available RAM for CPU
                                    # expert/layer offload. Keeps Windows 11 responsive (no thrash/bluescreen)
                                    # when big models push the machine to the edge. Never touches the VRAM budget.
    "vram_reserve_gb": 0.25,        # Minimal GPU-side safety (Windows compositor/desktop spikes on the same card).
                                    # Shaved off the auto-tune VRAM budget only; 0 = give the LLM everything
                                    # (the ~8% headroom still guards CUDA workspace + alignment).
    "ctx_checkpoints_enabled": False, # Advanced override: pass explicit --ctx-checkpoints flags. Off does NOT
                                      # turn checkpoints off. It only leaves llama.cpp's own defaults (build
                                      # 12121: 32 per slot, 8192 tokens apart), which hybrid/SWA models fall
                                      # back to whenever the prompt stops matching the cache.
    "ctx_checkpoints": 1,           # Number of checkpoints when the explicit override above is enabled.
    "checkpoint_min_step": 4096,    # --checkpoint-min-step (newer builds) / --checkpoint-every-n-tokens
                                    # (older): minimum spacing between snapshots. 4096 beats the 8192 default
                                    # for mid-prompt restores.
    "auto_tune_on_load": True,      # When a model loads with no saved per-model config, run the auto-tuner
                                    # automatically first (per-model results are remembered in models_config.json).
    "flash_attn": True,             # --flash-attn on/off. Off only if your GPU/build doesn't support it.
    "n_parallel": 1,                # --parallel: concurrent sequences. 1 is fine for chat. 2+ wastes ctx.
    "n_ubatch": 0,                  # --ubatch-size. 0 = auto (half of batch, clamped 512..1024).
    "enable_speculative": True,     # DEPRECATED — kept for backward-compat. Read by older configs only;
                                    # new field `spec_strategy` supersedes it. True maps to "ngram-mod", False to "off".
    "spec_strategy": "ngram-mod",   # Speculative decoding strategy. One of:
                                    #   "off"        — no speculative decoding (best for unusual models that confuse the drafter)
                                    #   "ngram-mod"  — n-gram modular draft, works on any model (default, current behavior)
                                    #   "draft-mtp"  — uses the model's built-in MTP heads (Qwen 3.5/3.6, DeepSeek V3/R1),
                                    #                  chained with n-gram for back-up drafts. ~1.85x decode speedup on
                                    #                  MTP-capable models; fails at startup on others.
                                    #                  Requires llama.cpp built from master after 2026-05-16 (PR #22673).
                                    #   "dflash"/"dspark" — self-speculative (drafts from the model's own hidden states,
                                    #                  llama.cpp #21930). Qwen3/Llama-4-family wins; skipped on builds
                                    #                  without the flag.
    "spec_strategy_source": "auto", # auto | manual. Manual choices are not overwritten by background tuning.
    "spec_draft_model": "",         # matching DFlash/DSpark draft GGUF, required by those strategies.
    "spec_draft_mode": "auto",      # auto = pair a validated draft beside the model (or honor the
                                    # path above); off = decoder explicitly unloaded — never auto-pair
                                    # any model, so its VRAM goes back to context. Set by the
                                    # "Unload draft" action; re-armed by picking a draft or a
                                    # DFlash/DSpark strategy again.
    "no_warmup": False,             # --no-warmup. Saves a few seconds at startup.
    "enable_metrics": False,        # --metrics. Exposes Prometheus metrics on /metrics. Off by default.
    "llama_extra_args": "",         # Free-form extra flags appended verbatim, e.g. "--alias my-model --rope-scaling linear".
                                    # Power-user escape hatch. Whitespace-split. Use with care.
    # Auto-tune helper (UI persistence only — does not get sent to llama-server directly).
    "vram_tier_gb": 0,              # 0 = auto-detect via nvidia-smi. Otherwise a fixed VRAM budget the suggester targets.
    "autotune_kv_cache": "auto",    # auto | f16 | q8_0 | q4_0: K cache type auto-tune sizes context for
    "autotune_kv_cache_v": "",      # "" = same as K | f16 | q8_0 | q4_0: V cache type auto-tune sizes context for
    # APK analysis (Phase 2 decompile_apk tool — Phase 1 scan_apk needs no config).
    "jadx_path": "",                # Full path to jadx.bat / jadx. Blank = auto-detect via PATH + common install dirs.
    # Native-binary analysis via Ghidra (in-process through pyghidra).
    "ghidra_path": "",              # Ghidra install root, e.g. C:\Program Files\ghidra_12.0.4_PUBLIC.
                                    # Blank = use $GHIDRA_INSTALL_DIR. Requires JDK 21+ + `pip install pyghidra`.
    "ghidra_max_heap_gb": 0,        # Ghidra's memory limit (JVM max heap) in GB. 0 = auto: an eighth of RAM,
                                    # 2-8 GB. Ghidra runs in its own process (ghidra_worker.py).
    "ghidra_idle_minutes": 5,       # Close the Ghidra process this many minutes after its last analysis,
                                    # handing its memory back. The next analysis restarts it (~10-20s).
}


_SHORTCUT_ACTIONS = {
    "approve_pending", "deny_pending", "open_settings", "open_faq",
    "open_usage", "new_chat", "focus_composer", "stop_generation",
    # Workbench navigation. These ship with defaults in the client; a stored
    # "" means the user switched that default off.
    "open_palette", "quick_open", "toggle_sidebar", "toggle_explorer",
    "toggle_terminal", "toggle_preview", "next_tab", "prev_tab", "close_tab",
}


_SHORTCUT_MODIFIERS = {"Ctrl", "Alt", "Shift", "Meta"}


_SHORTCUT_KEY_RE = re.compile(
    r"^(?:Key[A-Z]|Digit[0-9]|F(?:[1-9]|1[0-2])|Arrow(?:Up|Down|Left|Right)|"
    r"Home|End|PageUp|PageDown|Space|Slash|Backslash|BracketLeft|BracketRight|"
    r"Semicolon|Quote|Comma|Period|Minus|Equal)$")


def _normalized_keyboard_shortcuts(value) -> dict:
    """Keep only small, known action/chord pairs from the settings file or API."""
    if not isinstance(value, dict):
        return {}
    clean = {}
    for action, chord in value.items():
        if action not in _SHORTCUT_ACTIONS or not isinstance(chord, str):
            continue
        chord = chord.strip()
        if not chord:
            clean[action] = ""   # a default the user turned off
            continue
        parts = chord.split("+")
        if not parts or len(parts) > 5 or len(chord) > 64:
            continue
        modifiers, key = parts[:-1], parts[-1]
        if len(modifiers) != len(set(modifiers)):
            continue
        if any(part not in _SHORTCUT_MODIFIERS for part in modifiers):
            continue
        if not _SHORTCUT_KEY_RE.fullmatch(key):
            continue
        if not ({"Ctrl", "Alt", "Meta"} & set(modifiers)) and not re.fullmatch(r"F(?:[1-9]|1[0-2])", key):
            continue
        clean[action] = chord
    return clean


def _normalized_mmproj_mode(settings: dict, raw: dict | None = None) -> str:
    """Resolve the explicit projector state while preserving old settings files."""
    source = raw if isinstance(raw, dict) else settings
    if "mmproj_mode" in source:
        mode = str(source.get("mmproj_mode") or "").strip().lower()
        if mode in {"off", "auto", "manual"}:
            return mode
    if str(source.get("mmproj_path") or settings.get("mmproj_path") or "").strip():
        return "manual"
    return "auto" if bool(source.get("mmproj_auto", settings.get("mmproj_auto", True))) else "off"


def get_settings() -> dict:
    s = load_json(SETTINGS_FILE, {})
    raw = s if isinstance(s, dict) else {}
    out = {**DEFAULT_SETTINGS, **raw}
    out["offline_active"] = _offline_active
    out["mmproj_mode"] = _normalized_mmproj_mode(out, raw)
    out["mmproj_auto"] = out["mmproj_mode"] == "auto"
    out["mmproj_device"] = "gpu" if str(out.get("mmproj_device") or "").strip().lower() == "gpu" else "cpu"
    out["composer_mode"] = _normalize_composer_mode(out.get("composer_mode"))
    prompt_variant = str(out.get("agent_prompt_variant") or "classic").strip().lower()
    out["agent_prompt_variant"] = prompt_variant if prompt_variant in {"classic", "compact"} else "classic"
    tool_verbosity = str(out.get("tool_verbosity") or "lean").strip().lower()
    out["tool_verbosity"] = tool_verbosity if tool_verbosity in {"lean", "full"} else "lean"
    draft_mode = str(out.get("spec_draft_mode") or "auto").strip().lower()
    out["spec_draft_mode"] = draft_mode if draft_mode in {"auto", "off"} else "auto"
    reasoning_effort = str(out.get("reasoning_effort") or "auto").strip().lower()
    out["reasoning_effort"] = _normalize_effort_choice(reasoning_effort)
    for _base_key in ("repos_baseline_unique", "repos_baseline_sessions"):
        try:
            out[_base_key] = max(0, int(out.get(_base_key) or 0))
        except (TypeError, ValueError):
            out[_base_key] = 0
    out["keyboard_shortcuts"] = _normalized_keyboard_shortcuts(out.get("keyboard_shortcuts"))
    return out


_offline_active = (os.environ.get("ACCURETTA_OFFLINE", "").lower() in {"1", "true", "yes"}
                   or load_json(SETTINGS_FILE, {}).get("offline_mode") is True)
