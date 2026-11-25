# SPDX-License-Identifier: AGPL-3.0-only
"""Starting Accuretta: error hooks, the one-instance lock, bundled skills, the boot-time
scans and services, the web server, and the browser window.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import webbrowser
from accuretta.agent.discord_chat import _discord_start
from accuretta.agent.mcp_servers import _load_mcp_servers
from accuretta.agent.system_prompt import SYSTEM_CONTEXT_FILE
from accuretta.agent.system_prompt import ensure_system_context
from accuretta.agent.turn_journal import _recover_turn_journals
from accuretta.core.endpoints import LLAMA
from accuretta.core.endpoints import PORT
from accuretta.core.errors import _error_store
from accuretta.core.paths import ROOT
from accuretta.core.paths import SETTINGS_FILE
from accuretta.core.persistence import save_json
from accuretta.core.safe_fs import safe_exists
from accuretta.core.settings import _offline_active
from accuretta.core.settings import get_settings
from accuretta.core.state import STATE
from accuretta.core.workspace import get_workspace
from accuretta.core.workspace_native_watch import _start_workspace_watching
from accuretta.core.workspace_watch import _stop_workspace_watching
from accuretta.redteam.mission_flow import _prune_red_team_evidence
from accuretta.redteam.mission_flow import _rt_identities_sweep
from accuretta.redteam.mission_logs import _rt_log_sweep
from accuretta.runtime.llama_bin import find_llama_bin
from accuretta.runtime.llama_http import llama_get
from accuretta.runtime.llama_http import llama_ping
from accuretta.runtime.llama_process import _llama
from accuretta.runtime.model_sampling import _apply_model_sampling
from accuretta.runtime.notch_process import notch_start
from accuretta.runtime.notch_process import notch_stop
from accuretta.runtime.vision_server import _start_vision_fallback_if_needed
from accuretta.runtime.vision_server import _vision_llama
from accuretta.security.child_env import _ENV_TOOLS
from accuretta.security.child_env import _child_env
from accuretta.server.http_handler import AccurettaHTTPServer
from accuretta.server.http_handler import Handler
from accuretta.tools.github_account import _github_forget_saved_keys
from accuretta.tools.security_watch import _security_apply_schedule
from accuretta.tools.security_watch import _security_boot_scan
from accuretta.tools.security_watch import _security_overview
from accuretta.tools.sessions import _session_mgr
from network_policy import install_offline_guard
from pathlib import Path


def _install_error_hooks() -> None:
    import sys as _sys

    def _hook(exc_type, exc_value, exc_tb):
        try:
            if exc_value is not None:
                _error_store.record_exception("bridge", exc_value)
        except Exception:
            pass
        _sys.__excepthook__(exc_type, exc_value, exc_tb)

    def _thread_hook(args):
        try:
            if args.exc_value is not None:
                _error_store.record_exception("bridge", args.exc_value)
        except Exception:
            pass

    try:
        _sys.excepthook = _hook
        threading.excepthook = _thread_hook
    except Exception:
        pass


def _bootstrap_skill_assets() -> None:
    """Ship bundled skill templates (skill_assets/<name>/) into the user's
    workspace, copy-if-missing so user edits are never clobbered. Kept out of the
    skills/ catalogue: this is raw file material the agent copies per project
    (the code-driven-motion-graphics engine), not a loadable .md procedure."""
    try:
        src_root = ROOT / "skill_assets"
        if not src_root.is_dir():
            return
        folders = [f for f in (get_workspace().get("folders") or []) if str(f).strip()]
        if not folders:
            return
        target_root = Path(folders[0])
        for src_dir in sorted(src_root.iterdir()):
            if not src_dir.is_dir():
                continue
            dst_dir = target_root / src_dir.name
            if dst_dir.exists():
                continue
            shutil.copytree(
                src_dir, dst_dir,
                ignore=shutil.ignore_patterns("node_modules", "out", "__pycache__", ".venv"),
            )
            print(f"  [skills] provisioned template into workspace: {dst_dir}", flush=True)
    except Exception as exc:
        print(f"  [skills] template provision failed: {exc}", flush=True)


def main():
    # Binding is the process-wide ownership check, before MCP or model startup.
    try:
        httpd = AccurettaHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use. No model was started. Check the existing Accuretta window or choose another port.")
        return
    try:
        _main_owned(httpd)
    finally:
        httpd.server_close()


def _main_owned(httpd):
    if _offline_active:
        install_offline_guard()
    else:
        _load_mcp_servers()
    print("accuretta bridge")
    print(f"  root:    {ROOT}")
    print(f"  llama:   {LLAMA}")
    if STATE.vision_llama and STATE.vision_llama != LLAMA:
        print(f"  vision:  {STATE.vision_llama}")
    print(f"  port:    {PORT}")
    print("  bind:    0.0.0.0  (reachable over LAN / Tailscale)")

    _jr = _recover_turn_journals()
    if _jr.get("recovered") or _jr.get("stale") or _jr.get("invalid"):
        print(f"  recovery: {_jr['recovered']} interrupted turn(s), "
              f"{_jr['entries']} tool entries; {_jr['stale']} stale, {_jr['invalid']} invalid")

    # Evidence must survive a restart long enough to verify the report that cites
    # it: kept 30 days by default; 0 wipes it on boot.
    _ev_ret = _prune_red_team_evidence(get_settings())
    if _ev_ret.get("days") is not None:
        print(f"  evidence: kept {_ev_ret['kept']}, removed {_ev_ret['removed']} "
              f"(retention {_ev_ret['days']} day(s))")
    try:
        _unkept = _rt_log_sweep()
        if _unkept:
            print(f"  evidence: deleted logs of {_unkept} ended engagement(s) set to not keep logs")
    except Exception as e:
        print(f"  evidence: could not sweep unkept engagement logs: {e}")
    try:
        _stale_accounts = _rt_identities_sweep()
        if _stale_accounts:
            print(f"  accounts: deleted the test-account sign-ins of {_stale_accounts} ended engagement(s)")
    except Exception as e:
        print(f"  accounts: could not sweep ended engagements' test-account sign-ins: {e}")

    # first-run system context scan (creates data/ACCURETTA.md if missing)
    if not SYSTEM_CONTEXT_FILE.exists():
        print("  scanning system (first run) ...")
        ensure_system_context()
        print(f"  wrote:   {SYSTEM_CONTEXT_FILE}")
    else:
        print(f"  context: {SYSTEM_CONTEXT_FILE} (edit or delete to rescan)")

    # Make the spawned llama-server die with us. atexit runs callbacks in reverse
    # order, so stop() registers first (runs last) and shutdown_watchdog() second
    # (runs first): the watchdog must not be polling when the subprocess is killed,
    # or it races to respawn it. Registered before the boot spawn below, which can
    # block up to 120s, so quitting during it doesn't orphan llama-server.
    import atexit
    try:
        _install_error_hooks()
    except Exception:
        pass
    atexit.register(_session_mgr.stop_all)   # kill any interactive sessions on exit
    atexit.register(_stop_workspace_watching)
    atexit.register(notch_stop)              # the overlay dies with the bridge
    atexit.register(_llama.stop)
    atexit.register(_llama.shutdown_watchdog)
    atexit.register(_vision_llama.stop)
    atexit.register(_vision_llama.shutdown_watchdog)

    # Auto-spawn llama-server with the last-loaded model, unless something already
    # answers on LLAMA_HOST (a user-launched server is left alone).
    s_initial = get_settings()
    last_model = (s_initial.get("model_path") or "").strip()
    if llama_ping(timeout=1.0):
        print(f"  llama-server: already running at {LLAMA} (using existing instance)")
        try:
            info = llama_get("/v1/models")
            names = [m.get("id") for m in (info.get("data") or []) if m.get("id")]
            if names and not s_initial.get("model"):
                s_initial["model"] = names[0]
                save_json(SETTINGS_FILE, s_initial)
        except Exception:
            pass
    elif last_model and safe_exists(last_model):
        model_name = os.path.basename(last_model)
        # The model's sampling preset, as a load from the menu applies it: an
        # update that adds a preset for the model already loaded (Inkling,
        # 2026-11-22) otherwise only took effect after switching models.
        # Idempotent; the user's own edits for this model win.
        _apply_model_sampling(last_model)
        print(f"  spawning llama-server with {model_name} ...")
        res = _llama.start(last_model, wait=True, wait_seconds=120)
        if res.get("ok"):
            print(f"  llama-server: ready (pid {res.get('pid')})")
            _start_vision_fallback_if_needed(s_initial)
        else:
            print(f"  [warn] llama-server didn't start: {res.get('error')}")
            print("         pick a different model or update settings via the UI.")
    elif last_model:
        # The remembered model was moved or deleted. Say so where the user
        # looks (Backend tab, model pill) instead of a console nobody sees.
        print(f"  [warn] last model not found: {last_model}")
        print("         pick another model from the model menu.")
        _llama._append_log(f"[accuretta] last model not found: {last_model}")
        _llama._append_log("[accuretta] it was moved or deleted, so no model was started. "
                           "Pick a model from the model menu to start the backend.")
    else:
        bin_path = find_llama_bin()
        if not bin_path:
            print("  [warn] llama-server.exe not found.")
            print("         install llama.cpp or set llama_bin in Settings.")
        elif not s_initial.get("models_dir"):
            print("  [info] no models folder set yet.")
            print("         open Settings -> Models folder, pick where your .gguf files live.")
        else:
            print("  [info] no model loaded yet. Pick one in Settings -> Models.")

    httpd.daemon_threads = True
    url = f"http://localhost:{PORT}"
    print(f"\nopen {url}  — or from phone, http://<tailscale-ip>:{PORT}")

    # Open the browser shortly after bind, in a thread so serve_forever owns the
    # main thread. ACCURETTA_BROWSER picks the browser:
    #   chrome | firefox | edge | brave | opera | vivaldi | none | default
    def _open_browser():
        time.sleep(0.8)
        choice = (os.environ.get("ACCURETTA_BROWSER") or "").strip().lower()
        if choice in ("none", "off", "skip", "no"):
            print(f"  [info] ACCURETTA_BROWSER={choice} — not auto-opening a browser. Visit {url} in any browser.")
            return
        # Map short names to common Windows install paths + executable names.
        candidates = {
            "chrome":  ["chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
            "firefox": ["firefox.exe", r"C:\Program Files\Mozilla Firefox\firefox.exe", r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"],
            "edge":    ["msedge.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
            "brave":   ["brave.exe", r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"],
            "opera":   ["opera.exe", os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera\opera.exe")],
            "vivaldi": ["vivaldi.exe", os.path.expandvars(r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe")],
        }
        if choice and choice in candidates:
            import shutil, subprocess
            exe = None
            for c in candidates[choice]:
                p = shutil.which(c) if not os.path.sep in c else (c if os.path.exists(c) else None)
                if p:
                    exe = p
                    break
            if exe:
                try:
                    subprocess.Popen([exe, url], close_fds=True, env=_child_env(keep=_ENV_TOOLS))
                    print(f"  [info] opened {choice} -> {url}")
                    return
                except Exception as e:
                    print(f"  [warn] failed to launch {choice} ({e}); falling back to default browser.")
            else:
                print(f"  [warn] ACCURETTA_BROWSER={choice} but {choice} not found; falling back to default browser.")
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open_browser, daemon=True).start()

    _start_workspace_watching()
    _bootstrap_skill_assets()
    # GitHub keys older versions saved into cloned repos (see _github_git_login).
    threading.Thread(target=_github_forget_saved_keys, name="github-key-cleanup", daemon=True).start()

    # Notch overlay: spawn the host process when enabled. It waits patiently
    # for this server to start answering, so launching before serve_forever
    # is fine.
    try:
        if get_settings().get("notch_enabled"):
            if notch_start():
                print("  notch:   overlay host started")
            else:
                print("  notch:   enabled but host unavailable (Windows + WebView2, or macOS + PyObjC/WebKit)")
    except Exception:
        pass

    # Discord remote bridge — outbound only, owner-locked. Daemon thread so it
    # never blocks shutdown; fully optional and self-disabling if misconfigured.
    if not _offline_active and get_settings().get("discord_enabled"):
        threading.Thread(target=_discord_start, daemon=True).start()
        print("  discord: bridge starting (DM the bot from your phone)")

    # Scheduled security overviews — background read-only scans while the app
    # runs; notable alerts surface in the popup near the security button and
    # (optionally) the owner's Discord DM.
    if not _offline_active:
        _security_apply_schedule(get_settings())
        _security_overview.start_scheduler()
        if _security_overview.schedule_seconds():
            print(f"  security: scheduled overview every "
                  f"{_security_overview.schedule_seconds() // 60} min")
        threading.Thread(target=_security_boot_scan, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping.")
    finally:
        _stop_workspace_watching()
        # Stop the watchdog FIRST so it doesn't observe the impending
        # subprocess death and try to "heal" it during shutdown.
        try:
            _llama.shutdown_watchdog()
            _vision_llama.shutdown_watchdog()
        except Exception:
            pass
        try:
            _llama.stop()
            _vision_llama.stop()
        except Exception:
            pass
