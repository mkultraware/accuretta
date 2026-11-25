# SPDX-License-Identifier: AGPL-3.0-only
"""Setting up the WSL sandbox: the provisioning thread, the tool packages it installs,
the security profile it applies, running bash inside it, and the self-test.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import base64 as _b64
import os
import subprocess
import threading
import time
from accuretta.core.chats import _current_chat_id
from accuretta.core.chats import _current_tool_call_id
from accuretta.core.chats import _get_current_chat
from accuretta.core.events import broadcast_event
from accuretta.core.paths import DATA
from accuretta.core.persistence import load_json
from accuretta.core.persistence import save_json
from accuretta.core.tool_stream import _append_cmd_history
from accuretta.core.tool_stream import _emit_execution_event
from accuretta.core.tool_stream import _emit_tool_stream
from accuretta.core.tool_stream import _execution_tool_name
from accuretta.core.windows_bin import _NO_WINDOW
from accuretta.runtime.command_kill import _killed_procs
from accuretta.runtime.command_kill import _running_cmd_targets
from accuretta.runtime.command_kill import _running_cmds
from accuretta.runtime.command_kill import _running_cmds_lock
from accuretta.runtime.process_tree import _terminate_process_tree
from accuretta.runtime.sandbox_rootfs import SANDBOX_LOCK
from accuretta.runtime.sandbox_rootfs import SANDBOX_PROGRESS
from accuretta.runtime.sandbox_rootfs import _sbx_download_rootfs
from accuretta.runtime.sandbox_rootfs import _sbx_log
from accuretta.runtime.sandbox_rootfs import _sbx_set
from accuretta.runtime.sandbox_state import SANDBOX_SECURITY_PROFILE
from accuretta.runtime.sandbox_state import SANDBOX_STATE_FILE
from accuretta.runtime.sandbox_state import _SBX_READY_CACHE
from accuretta.runtime.sandbox_state import _sbx_stream_provision
from accuretta.runtime.sandbox_state import _write_wsl_script
from accuretta.runtime.sandbox_state import _wsl_distros
from accuretta.runtime.sandbox_state import wsl_probe
from accuretta.runtime.wsl_sandbox import SANDBOX_DISTRO
from accuretta.runtime.wsl_sandbox import _note_sandbox_use
from accuretta.runtime.wsl_sandbox import _wsl_env
from accuretta.runtime.wsl_sandbox import _wsl_exe
from accuretta.runtime.wsl_sandbox import _wsl_run


SANDBOX_DIR = DATA / "sandbox"                  # holds the distro's ext4.vhdx


# Kept modest so the first provision is minutes, not an hour. The agent can
# apt-install more on demand once the guest is up.
SANDBOX_APT_TOOLS = [
    "ca-certificates", "curl", "wget", "git", "python3", "python3-pip",
    "file", "unzip", "zip", "xxd", "binutils", "binwalk", "sqlmap", "nmap",
    # Extraction backends binwalk needs. They are apt "recommends", which
    # --no-install-recommends drops, leaving `binwalk -e` unable to unpack
    # (p7zip -> 7z, mtd-utils -> jffs2, squashfs-tools -> unsquashfs). jq parses
    # tool output.
    "p7zip-full", "mtd-utils", "squashfs-tools", "jq",
    # red-team expansion set: web fuzzing, default-cred testing, SSL and
    # SMB/NetBIOS enumeration, plus SecLists wordlists (/usr/share/seclists).
    # Install is resilient per-package, so an unavailable name just warns.
    "gobuster", "ffuf", "nikto", "hydra", "whatweb", "wafw00f",
    "masscan", "testssl.sh", "enum4linux", "smbclient", "seclists",
]


# binwalk 2.x refuses to run its extraction utilities as root without
# --run-as=root, and the sandbox runs as root. This wrapper, placed ahead of
# /usr/bin in PATH, injects the flag for extraction so `binwalk -e` just works.
_SBX_BINWALK_WRAPPER = (
    "#!/bin/bash\n"
    "need=0; has_runas=0\n"
    "for a in \"$@\"; do case \"$a\" in\n"
    "  -e|--extract|-M|--matryoshka|--dd|--dd=*) need=1 ;;\n"
    "  --run-as=*) has_runas=1 ;;\n"
    "esac; done\n"
    "if [ \"$need\" = 1 ] && [ \"$has_runas\" = 0 ] && [ \"$(id -u)\" = 0 ]; then\n"
    "  exec /usr/bin/binwalk --run-as=root \"$@\"\n"
    "fi\n"
    "exec /usr/bin/binwalk \"$@\"\n"
)


_sandbox_hardening_lock = threading.Lock()


def _wsl_bash(distro, script, timeout=120, *, announce=False,
              command="", cwd=""):
    """Run a bash script as root inside `distro`, passing it over stdin.

    wsl.exe performs its own $VAR substitution on command-line arguments
    (undefined vars become empty, bash quoting is ignored), so `bash -lc
    "<script>"` silently corrupts anything using shell variables. stdin avoids
    that mangling. Returns (returncode, output, killed, spawned)."""
    env = _wsl_env()
    t_start = time.time()
    stream_chat_id = _current_chat_id.get() or _get_current_chat()
    stream_tool_name = _execution_tool_name("sandbox_run")
    stream_call_id = _current_tool_call_id.get()
    executable = _wsl_exe()
    if not executable:
        message = "trusted wsl.exe was not found in the Windows system directory"
        if announce:
            _emit_execution_event("not_executed", "sandbox", command or script,
                                  cwd=cwd, reason="spawn_error", message=message)
        return 127, message, False, False
    if distro == SANDBOX_DISTRO:
        _note_sandbox_use()
    try:
        p = subprocess.Popen([
            executable, "-d", distro, "-u", "root", "--",
            "/usr/bin/env", "-u", "BASH_ENV", "-u", "ENV",
            "/bin/bash", "--noprofile", "--norc",
        ],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, env=env, creationflags=_NO_WINDOW, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if announce:
            _emit_execution_event("not_executed", "sandbox", command or script,
                                  cwd=cwd, reason="spawn_error", message="wsl not found")
        return 127, "wsl not found", False, False
    except Exception as e:
        if announce:
            _emit_execution_event("not_executed", "sandbox", command or script,
                                  cwd=cwd, reason="spawn_error", message=str(e))
        return 1, str(e), False, False

    output: list[str] = []
    if announce and stream_chat_id:
        with _running_cmds_lock:
            _running_cmds[stream_chat_id] = p
            _running_cmd_targets[stream_chat_id] = "sandbox"
        _emit_execution_event("spawned", "sandbox", command or script, cwd=cwd,
                              chat_id=stream_chat_id, pid=p.pid, distro=distro)
    try:
        _write_wsl_script(p.stdin, script)
        p.stdin.close()

        def _reader():
            for line in iter(p.stdout.readline, ''):
                output.append(line)
                try:
                    clean = line.rstrip("\r\n")
                    if announce and stream_chat_id:
                        _emit_tool_stream(stream_tool_name, clean, stream_chat_id,
                                          target="sandbox", call_id=stream_call_id)
                    elif not stream_chat_id:
                        broadcast_event({"type": "sandbox:stream", "line": clean})
                except Exception:
                    pass

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        
        timed_out = False
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(p, "sandbox")
            timed_out = True
            try:
                p.wait(timeout=5)
            except Exception:
                pass
            rc = 124
            output.append("timeout\\n")

        t.join(timeout=1.0)
        with _running_cmds_lock:
            killed = p in _killed_procs
        if p.returncode is not None and p.returncode < 0:
            killed = True
        if timed_out:
            killed = False
        combined = "".join(output)
        duration_ms = int((time.time() - t_start) * 1000)
        if announce:
            _append_cmd_history({
                "ts": int(t_start * 1000), "chat_id": stream_chat_id,
                "command": command or script, "cwd": cwd, "exit": rc,
                "ok": rc == 0 and not killed and not timed_out,
                "duration_ms": duration_ms, "stdout": combined[-20000:],
                "stderr": "", "killed": killed, "timed_out": timed_out,
                "target": "sandbox", "distro": distro,
                "tool": stream_tool_name, "call_id": stream_call_id,
            })
            _emit_execution_event("finished", "sandbox", command or script,
                                  cwd=cwd, chat_id=stream_chat_id, exit_code=rc,
                                  killed=killed, timed_out=timed_out,
                                  duration_ms=duration_ms, distro=distro)
        return rc, combined, killed, True
    except Exception as e:
        try:
            _terminate_process_tree(p, "sandbox")
        except Exception:
            pass
        if announce:
            _emit_execution_event("finished", "sandbox", command or script,
                                  cwd=cwd, chat_id=stream_chat_id, exit_code=1,
                                  error=str(e), duration_ms=int((time.time() - t_start) * 1000),
                                  distro=distro)
        return 1, str(e), False, True
    finally:
        with _running_cmds_lock:
            if stream_chat_id and _running_cmds.get(stream_chat_id) is p:
                _running_cmds.pop(stream_chat_id, None)
                _running_cmd_targets.pop(stream_chat_id, None)
            _killed_procs.discard(p)


def _wsl_in(distro, script, timeout=120):
    """Run a bash script as root inside a distro (stdin-fed, see _wsl_bash)."""
    return _wsl_bash(distro, script, timeout=timeout)


_SANDBOX_WSL_CONF = "[interop]\nenabled=false\nappendWindowsPath=false\n"


_SANDBOX_INTEROP_VERIFY = r"""
set -eu
# Some WSL releases ignore interop.enabled=false even though they honor
# appendWindowsPath=false. Disable the guest's current binfmt handler as the
# supported runtime fallback, then verify it actually stayed disabled.
if [ -e /proc/sys/fs/binfmt_misc/WSLInterop ] && grep -q '^enabled' /proc/sys/fs/binfmt_misc/WSLInterop; then
  if ! echo 0 > /proc/sys/fs/binfmt_misc/WSLInterop; then
    echo INTEROP_HANDLER_DISABLE_FAILED
    exit 40
  fi
fi
if [ -e /proc/sys/fs/binfmt_misc/WSLInterop ] && grep -q '^enabled' /proc/sys/fs/binfmt_misc/WSLInterop; then
  echo INTEROP_HANDLER_ENABLED
  exit 41
fi
case ":$PATH:" in
  *:/mnt/[A-Za-z]/*) echo WINDOWS_PATH_PRESENT; exit 42 ;;
esac
if [ -x /mnt/c/Windows/System32/cmd.exe ] && /mnt/c/Windows/System32/cmd.exe /c exit 0 >/dev/null 2>&1; then
  echo WINDOWS_PE_LAUNCHED
  exit 43
fi
echo ACCURETTA_INTEROP_DISABLED
"""


def _sandbox_apply_security_profile(log: bool = False) -> tuple[bool, str]:
    """Disable Windows process interop, restart only our distro, then verify."""
    with _sandbox_hardening_lock:
        encoded = _b64.b64encode(_SANDBOX_WSL_CONF.encode("utf-8")).decode("ascii")
        rc, out, _, _ = _wsl_bash(
            SANDBOX_DISTRO,
            f"set -eu; echo {encoded} | base64 -d > /etc/wsl.conf; chmod 644 /etc/wsl.conf",
            timeout=60,
        )
        if rc != 0:
            return False, (out or "could not write /etc/wsl.conf").strip()[-500:]
        if log:
            _sbx_log("Windows executable interop disabled; restarting this WSL guest.")
        verify_error = ""
        for attempt in range(3):
            rc, out = _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=60)
            if rc != 0:
                return False, (out or "could not restart WSL guest").strip()[-500:]
            if attempt:
                time.sleep(min(float(attempt), 2.0))
            rc, out, _, _ = _wsl_bash(
                SANDBOX_DISTRO, _SANDBOX_INTEROP_VERIFY, timeout=60)
            if rc == 0 and "ACCURETTA_INTEROP_DISABLED" in out:
                break
            verify_error = (
                out or f"interop verification failed with exit {rc}"
            ).strip()[-500:]
            if log and attempt < 2:
                _sbx_log(
                    f"Security verification did not settle; retrying guest restart "
                    f"({attempt + 2}/3).")
        else:
            return False, verify_error or "interop verification failed"
        state = load_json(SANDBOX_STATE_FILE, {})
        state.update({
            "security_profile": SANDBOX_SECURITY_PROFILE,
            "interop_disabled": True,
            "updated": time.time(),
        })
        save_json(SANDBOX_STATE_FILE, state)
        _SBX_READY_CACHE.update(t=0.0, ready=False)
        return True, ""


def _sbx_decompress_xz(src, dst) -> None:
    import lzma
    with lzma.open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(1024 * 1024)
            if not chunk:
                break
            fo.write(chunk)


def _sbx_provision_thread(reinstall: bool = False) -> None:
    """Background orchestrator: download -> decompress -> import -> apt-provision.
    Idempotent and resumable - skips stages already complete."""
    try:
        _sbx_set(status="downloading", step="Checking WSL...", pct=0, error="")
        probe = wsl_probe()
        if not probe["wsl_installed"]:
            _sbx_set(status="failed", step="WSL not installed",
                     error="WSL is not installed. Open an elevated PowerShell, run "
                           "`wsl --install`, reboot once, then retry.")
            return

        os.makedirs(SANDBOX_DIR, exist_ok=True)
        have = SANDBOX_DISTRO in _wsl_distros()
        previous_state = load_json(SANDBOX_STATE_FILE, {})
        needs_full_provision = reinstall or not (
            have and bool(previous_state.get("provisioned"))
        )

        if have and reinstall:
            _sbx_log(f"Removing existing {SANDBOX_DISTRO} for a clean reinstall...")
            _wsl_run(["--terminate", SANDBOX_DISTRO], timeout=60)
            _wsl_run(["--unregister", SANDBOX_DISTRO], timeout=120)
            have = False

        if not have:
            tar_xz = SANDBOX_DIR / "rootfs.tar.xz"
            tar_path = SANDBOX_DIR / "rootfs.tar"
            _sbx_download_rootfs(tar_xz)
            _sbx_set(status="extracting", step="Decompressing rootfs (xz -> tar)...", pct=0)
            _sbx_log("Decompressing rootfs...")
            _sbx_decompress_xz(tar_xz, tar_path)
            try:
                os.remove(tar_xz)
            except Exception:
                pass
            _sbx_set(status="importing", step="Creating WSL guest...", pct=0)
            _sbx_log(f"wsl --import {SANDBOX_DISTRO}")
            rc, out = _wsl_run(["--import", SANDBOX_DISTRO, str(SANDBOX_DIR),
                                str(tar_path), "--version", "2"], timeout=600)
            _sbx_log((out or "").strip() or f"import rc={rc}")
            if rc != 0 or SANDBOX_DISTRO not in _wsl_distros():
                _sbx_set(status="failed", step="Import failed",
                         error=f"wsl --import failed (rc={rc}): {(out or '').strip()[:400]}")
                return
            try:
                os.remove(tar_path)
            except Exception:
                pass
        else:
            _sbx_log(f"{SANDBOX_DISTRO} already imported - provisioning only.")

        _sbx_set(status="provisioning", step="Applying guest security profile...", pct=0)
        if needs_full_provision:
            save_json(SANDBOX_STATE_FILE, {
                "provisioned": False, "distro": SANDBOX_DISTRO,
                "security_profile": 0, "interop_disabled": False,
                "updated": time.time(),
            })
            _sbx_set(status="provisioning", step="Installing toolset (apt)...", pct=0)
            tools = " ".join(SANDBOX_APT_TOOLS)
            wrap_b64 = _b64.b64encode(_SBX_BINWALK_WRAPPER.encode("utf-8")).decode("ascii")
            script = (
                "set -e; export DEBIAN_FRONTEND=noninteractive; "
                "rm -f /etc/resolv.conf; "
                "printf 'nameserver 1.1.1.1\\nnameserver 8.8.8.8\\n' > /etc/resolv.conf; "
                "echo '== apt-get update =='; apt-get update; "
                f"echo '== installing: {tools} =='; "
                f"apt-get install -y --no-install-recommends {tools} || "
                f"{{ echo '== batch failed; installing individually =='; "
                f"for p in {tools}; do apt-get install -y --no-install-recommends \"$p\" "
                f"|| echo \"WARN: $p unavailable\"; done; }}; "
                f"echo {wrap_b64} | base64 -d > /usr/local/bin/binwalk "
                "&& chmod +x /usr/local/bin/binwalk && echo '== binwalk wrapper installed =='; "
                "apt-get clean; echo PROVISION_DONE"
            )
            if _sbx_stream_provision(script, timeout=1800) != 0:
                _sbx_set(status="failed", step="Tool installation failed",
                         error="The WSL guest tool installation failed. Review the setup log and retry.")
                return
        else:
            _sbx_log("Existing toolset found; applying the current security profile only.")

        hardened, hardening_error = _sandbox_apply_security_profile(log=True)
        if not hardened:
            _sbx_set(status="failed", step="Security profile failed", error=hardening_error)
            return

        rc, out, _, _ = _wsl_in(SANDBOX_DISTRO, "echo ACCURETTA_SBX_OK", timeout=60)
        if "ACCURETTA_SBX_OK" not in out:
            _sbx_set(status="failed", step="Verification failed",
                     error=(out or "").strip()[:400])
            return
        save_json(SANDBOX_STATE_FILE, {
            "provisioned": True,
            "distro": SANDBOX_DISTRO,
            "tools": SANDBOX_APT_TOOLS,
            "security_profile": SANDBOX_SECURITY_PROFILE,
            "interop_disabled": True,
            "updated": time.time(),
        })
        _sbx_set(status="done", step="Sandbox ready.", pct=100)
        _sbx_log("Sandbox ready.")
        broadcast_event({"type": "sandbox:update"})
    except Exception as e:
        _sbx_set(status="failed", step="Error", error=str(e))
        _sbx_log(f"ERROR: {e}")


def start_sandbox_setup(reinstall: bool = False) -> dict:
    """Kick off provisioning in a background thread unless one is already live."""
    with SANDBOX_LOCK:
        busy = SANDBOX_PROGRESS["status"] in ("downloading", "extracting", "importing", "provisioning")
    if busy:
        return {"started": False, "reason": "already running"}
    _sbx_set(status="downloading", step="Starting...", pct=0, error="", log=[])
    threading.Thread(target=_sbx_provision_thread, args=(reinstall,), daemon=True).start()
    return {"started": True}


def sandbox_selftest() -> dict:
    """Canned diagnostic used by the Settings 'Test' button - never runs
    client-supplied commands, just proves the guest is alive and tooled."""
    probe = wsl_probe()
    if not probe["ready"]:
        return {"ok": False, "state": probe["state"],
                "error": "sandbox not ready - set it up first."}
    rc, out, _, _ = _wsl_in(SANDBOX_DISTRO,
                      "uname -a; echo '--- tools ---'; "
                      "for t in python3 nmap sqlmap binwalk 7z unsquashfs file xxd jq git curl gobuster ffuf nikto hydra whatweb wafw00f masscan testssl.sh enum4linux smbclient; do "
                      "printf '%-10s ' \"$t\"; (command -v $t >/dev/null && echo ok || echo MISSING); done; "
                      # prove binwalk extraction actually works as root (the wrapper fix)
                      "echo '--- binwalk extract ---'; d=$(mktemp -d); echo probe | gzip > \"$d/t.gz\"; "
                      "(cd \"$d\" && binwalk -e t.gz >/dev/null 2>&1 && ls _t.gz.extracted >/dev/null 2>&1 "
                      "&& echo 'binwalk -e OK' || echo 'binwalk -e FAILED'); rm -rf \"$d\"",
                      timeout=90)
    return {"ok": rc == 0, "output": (out or "").strip()[-4000:], "exit_code": rc}
