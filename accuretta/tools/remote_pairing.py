# SPDX-License-Identifier: AGPL-3.0-only
"""Pairing a remote machine over Tailscale and SSH: finding the tools, the key pair,
Tailscale serve, and refreshing a paired machine.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import time
from accuretta.core.endpoints import PORT
from accuretta.core.persistence import save_json
from accuretta.core.settings import get_settings
from accuretta.security.child_env import _child_env
from accuretta.tools.remote_helpers import _normalize_pairing_roots
from accuretta.tools.remote_helpers import _public_remote_machine
from accuretta.tools.remote_machines import REMOTE_DIR
from accuretta.tools.remote_machines import REMOTE_MACHINES_FILE
from accuretta.tools.remote_machines import _load_remote_machines
from pathlib import Path
from typing import Any
from accuretta.tools.remote_machines import _remote_machine


REMOTE_KEYS_DIR = REMOTE_DIR / "keys"


_REMOTE_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")


def _save_remote_machines(data: dict) -> None:
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    save_json(REMOTE_MACHINES_FILE, data)


def _find_executable(names: tuple[str, ...]) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return ""


def _tailscale_exe() -> str:
    candidates = (
        "tailscale.exe" if os.name == "nt" else "tailscale",
        r"C:\Program Files\Tailscale\tailscale.exe",
    )
    for item in candidates:
        found = shutil.which(item) if not os.path.isabs(item) else (item if os.path.isfile(item) else "")
        if found:
            return found
    return ""


def _ssh_keygen_exe() -> str:
    candidates = ("ssh-keygen.exe", "ssh-keygen") if os.name == "nt" else ("ssh-keygen",)
    return _find_executable(candidates)


def _run_small(argv: list[str], timeout: int = 8) -> dict:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            # Decode as UTF-8 regardless of the locale codepage: text=True on Windows
            # uses cp1252, which mojibakes non-ASCII tool output such as machine names
            # from `tailscale status --json`.
            encoding="utf-8", errors="replace", env=_child_env(),
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        return {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "stdout": (proc.stdout or "")[:200_000],
            "stderr": (proc.stderr or "")[:40_000],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit": -1, "stdout": "", "stderr": "timed out"}
    except Exception as exc:
        return {"ok": False, "exit": -1, "stdout": "", "stderr": str(exc)}


def _tailscale_snapshot() -> dict:
    exe = _tailscale_exe()
    if not exe:
        return {"ok": False, "error": "Tailscale CLI not found", "peers": []}
    result = _run_small([exe, "status", "--json"], timeout=8)
    if not result.get("ok") and not result.get("stdout"):
        return {"ok": False, "error": result.get("stderr") or "Tailscale status failed", "peers": []}
    try:
        raw = json.loads(result.get("stdout") or "{}")
    except Exception:
        return {"ok": False, "error": "Tailscale returned invalid status data", "peers": []}
    peers = []
    for peer in (raw.get("Peer") or {}).values():
        if not isinstance(peer, dict):
            continue
        ips = [str(x) for x in (peer.get("TailscaleIPs") or [])]
        peers.append({
            "node_id": str(peer.get("ID") or peer.get("NodeID") or ""),
            "hostname": str(peer.get("HostName") or ""),
            "dns_name": str(peer.get("DNSName") or "").rstrip("."),
            "os": str(peer.get("OS") or "unknown"),
            "ip": next((x for x in ips if ":" not in x), ips[0] if ips else ""),
            "ips": ips,
            "online": bool(peer.get("Online")),
            "last_seen": str(peer.get("LastSeen") or ""),
        })
    self_row = raw.get("Self") if isinstance(raw.get("Self"), dict) else {}
    self_ips = [str(x) for x in (self_row.get("TailscaleIPs") or raw.get("TailscaleIPs") or [])]
    return {
        "ok": not bool(raw.get("Health")),
        "state": str(raw.get("BackendState") or "unknown"),
        "health": [str(x) for x in (raw.get("Health") or [])],
        "dns_suffix": str(raw.get("MagicDNSSuffix") or ""),
        "self_dns": str(self_row.get("DNSName") or "").rstrip("."),
        "self_ips": self_ips,
        "peers": peers,
    }


def _tailscale_serve_status(snapshot: dict | None = None) -> dict:
    exe = _tailscale_exe()
    snap = snapshot or _tailscale_snapshot()
    if not exe:
        return {"configured": False, "url": "", "error": "Tailscale CLI not found"}
    result = _run_small([exe, "serve", "status"], timeout=6)
    output = ((result.get("stdout") or "") + "\n" + (result.get("stderr") or "")).strip()
    configured = result.get("ok") and "No serve config" not in output and bool(output)
    dns_name = str(snap.get("self_dns") or "")
    return {
        "configured": bool(configured),
        "url": f"https://{dns_name}" if configured and dns_name else "",
        "detail": output[:2000],
        "error": "" if result.get("ok") else (result.get("stderr") or "Serve status failed"),
    }


def enable_tailscale_serve() -> dict:
    exe = _tailscale_exe()
    if not exe:
        return {"ok": False, "error": "Tailscale CLI not found"}
    result = _run_small([exe, "serve", "--bg", str(PORT)], timeout=30)
    status = _tailscale_serve_status()
    if not result.get("ok"):
        detail = result.get("stderr") or result.get("stdout") or "Serve setup failed"
        consent_match = re.search(r"https://login\.tailscale\.com/[^\s]+", detail)
        if consent_match:
            consent_url = consent_match.group(0).rstrip(".,);]")
            return {
                **status,
                "ok": False,
                "error": "Approve Tailscale Serve once, then click Enable HTTPS address again.",
                "consent_url": consent_url,
            }
        if os.name == "nt" and "access is denied" in detail.lower():
            detail = (
                "Windows denied access to Tailscale control. Open Administrator PowerShell once and run: "
                f"& '{exe}' serve --bg {PORT}"
            )
        return {**status, "ok": False, "error": detail}
    return {"ok": True, **status}


def _protect_remote_private_key(path: Path) -> None:
    """Best-effort owner-only permissions for a generated SSH private key."""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    if os.name != "nt":
        return
    account = getpass.getuser().strip()
    if not account:
        return
    icacls = shutil.which("icacls.exe") or shutil.which("icacls")
    if not icacls:
        return
    _run_small([icacls, str(path), "/inheritance:r", "/grant:r", f"{account}:(F)"], timeout=10)


def prepare_remote_pairing(node_id: str, username: str, label: str = "",
                           allowed_roots: Any = None) -> dict:
    """Create a dedicated key and a single command the user runs on the Mac."""
    if not get_settings().get("remote_access_enabled", True):
        return {"ok": False, "error": "Remote access is disabled in Settings."}
    username = str(username or "").strip()
    if not _REMOTE_USER_RE.fullmatch(username):
        return {"ok": False, "error": "Enter the short macOS account name, for example jane."}
    snap = _tailscale_snapshot()
    peer = next((p for p in snap.get("peers", []) if p.get("node_id") == str(node_id or "")), None)
    if not peer:
        return {"ok": False, "error": "That Mac is not present in the current Tailscale device list."}
    if str(peer.get("os") or "").lower() not in {"macos", "darwin"}:
        return {"ok": False, "error": "The selected Tailscale device is not a Mac."}
    if not peer.get("ip"):
        return {"ok": False, "error": "The selected Mac has no Tailscale IPv4 address."}
    source_ip = next((ip for ip in snap.get("self_ips", []) if ":" not in ip), "")
    if not source_ip:
        return {"ok": False, "error": "This PC has no Tailscale IPv4 address yet."}
    keygen = _ssh_keygen_exe()
    if not keygen:
        return {"ok": False, "error": "Windows OpenSSH key generator is not installed."}

    machine_id = "mac-" + hashlib.sha256(
        f"{peer.get('node_id')}\0{username}".encode("utf-8")
    ).hexdigest()[:12]
    REMOTE_KEYS_DIR.mkdir(parents=True, exist_ok=True)
    key_path = REMOTE_KEYS_DIR / machine_id
    pub_path = Path(str(key_path) + ".pub")
    if not key_path.exists() or not pub_path.exists():
        result = _run_small([
            keygen, "-q", "-t", "ed25519", "-N", "", "-C",
            f"accuretta-{machine_id}", "-f", str(key_path),
        ], timeout=20)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("stderr") or "Could not create the SSH key."}
    _protect_remote_private_key(key_path)
    try:
        public_key = pub_path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        return {"ok": False, "error": f"Could not read the generated public key: {exc}"}
    if not public_key.startswith("ssh-ed25519 "):
        return {"ok": False, "error": "The generated public key is invalid."}

    authorized_line = f'restrict,from="{source_ip}" {public_key}'
    quoted = shlex.quote(authorized_line)
    pair_command = (
        "umask 077; mkdir -p \"$HOME/.ssh\"; touch \"$HOME/.ssh/authorized_keys\"; "
        "chmod 700 \"$HOME/.ssh\"; chmod 600 \"$HOME/.ssh/authorized_keys\"; "
        f"grep -qxF -- {quoted} \"$HOME/.ssh/authorized_keys\" || "
        f"printf '%s\\n' {quoted} >> \"$HOME/.ssh/authorized_keys\""
    )
    safe_label = re.sub(r"[\r\n\t]+", " ", str(label or "").strip())[:80]
    if not safe_label:
        safe_label = str(peer.get("hostname") or "Mac")[:80]
    row = {
        "id": machine_id,
        "node_id": str(peer.get("node_id") or ""),
        "label": safe_label,
        "hostname": str(peer.get("hostname") or ""),
        "dns_name": str(peer.get("dns_name") or ""),
        "ip": str(peer.get("ip") or ""),
        "os": "macOS",
        "username": username,
        "allowed_roots": _normalize_pairing_roots(allowed_roots),
        "home": "",
        "key_path": str(key_path),
        "status": "prepared",
        "enabled": True,
        "source_ip": source_ip,
        "created": int(time.time()),
        "last_verified": 0,
    }
    data = _load_remote_machines()
    data["machines"] = [m for m in data["machines"] if m.get("id") != machine_id]
    data["machines"].append(row)
    _save_remote_machines(data)
    return {
        "ok": True,
        "machine": _public_remote_machine(row),
        "pair_command": pair_command,
        "note": "Run this once in Terminal on the Mac, then click Verify.",
    }


def _refresh_remote_machine(row: dict) -> dict:
    peer = next((p for p in _tailscale_snapshot().get("peers", [])
                 if p.get("node_id") == row.get("node_id")), None)
    if peer:
        row = dict(row)
        row["ip"] = str(peer.get("ip") or row.get("ip") or "")
        row["dns_name"] = str(peer.get("dns_name") or row.get("dns_name") or "")
        row["hostname"] = str(peer.get("hostname") or row.get("hostname") or "")
        row["online"] = bool(peer.get("online"))
    return row


def remove_remote_pairing(machine_id: str) -> dict:
    row = _remote_machine(machine_id)
    if not row:
        return {"ok": False, "error": "Pairing not found."}
    data = _load_remote_machines()
    data["machines"] = [m for m in data["machines"] if m.get("id") != machine_id]
    _save_remote_machines(data)
    for raw in (row.get("key_path"), str(row.get("key_path") or "") + ".pub"):
        try:
            path = Path(str(raw or ""))
            if path.parent.resolve() == REMOTE_KEYS_DIR.resolve() and path.is_file():
                path.unlink()
        except Exception:
            pass
    return {
        "ok": True,
        "note": "Pairing removed from this PC. Remove the matching accuretta key line from ~/.ssh/authorized_keys on the Mac if you want to revoke it there too.",
    }
