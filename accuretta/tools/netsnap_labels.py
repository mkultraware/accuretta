# SPDX-License-Identifier: AGPL-3.0-only
"""Friendly labels for processes seen in the network snapshot, by process name or
company.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations


# ---- network snapshot enrichment -------------------------------------------
# The raw snapshot carries only process names, and models invent identities
# from names alone ("EADesktop" reads like ESET antivirus). These tables give
# the model ground truth about what a process or endpoint is.
_NET_KNOWN_PROCESSES = {
    # Windows core (case-insensitive match)
    "svchost": "Windows service host — normal; check which service via pid/port",
    "csrss": "Windows Client/Server Runtime — OS core, normal",
    "lsass": "Windows Local Security Authority — OS core, normal",
    "winlogon": "Windows logon host — OS core, normal",
    "services": "Windows Service Control Manager — OS core, normal",
    "smss": "Windows Session Manager — OS core, normal",
    "dwm": "Desktop Window Manager — normal",
    "explorer": "Windows Explorer — normal",
    "runtimebroker": "Windows runtime broker — normal",
    "searchindexer": "Windows Search indexer — normal",
    "sihost": "Windows Shell Infrastructure Host — normal",
    "taskhostw": "Windows task host — normal",
    "fontdrvhost": "Windows font driver host — normal",
    "ctfmon": "Windows text input framework — normal",
    "conhost": "Windows console host — normal",
    "msmpeng": "Microsoft Defender antivirus engine — normal",
    "msmpsvc": "Microsoft Defender service — normal",
    "sense": "Microsoft Defender for Endpoint — normal",
    "spoolsv": "Windows print spooler — normal",
    "wmiPrvSE": "Windows Management Instrumentation provider — normal",
    "audiodg": "Windows audio device graph — normal",
    "dllhost": "Windows COM surrogate — normal",
    "werfault": "Windows Error Reporting — normal",
    "dwm.exe": "Desktop Window Manager — normal",
    "msteams": "Microsoft Teams — normal",
    "onedrive": "Microsoft OneDrive — normal",
    # Browsers
    "msedge": "Microsoft Edge browser — normal",
    "chrome": "Google Chrome browser — normal",
    "firefox": "Mozilla Firefox browser — normal",
    "brave": "Brave browser — normal",
    "opera": "Opera browser — normal",
    # Security products
    "ekrn": "ESET security kernel — antivirus, normal",
    "egui": "ESET GUI — antivirus, normal",
    "MBAMService": "Malwarebytes service — normal",
    "mbamtray": "Malwarebytes tray — normal",
    "360tray": "360 Security tray — normal",
    "kaspersky": "Kaspersky — normal",
    "avp": "Kaspersky engine — normal",
    "norton": "Norton Security — normal",
    # Game launchers / publishers — the "EADesktop misread" class
    "eadesktop": "EA Desktop — Electronic Arts' game launcher, NOT antivirus",
    "eabackgroundservice": "EA background service — normal",
    "ea anticheat": "EA AntiCheat — game protection, normal",
    "epicgameslauncher": "Epic Games Launcher — normal",
    "steam": "Steam — Valve's game client, normal",
    "steamwebhelper": "Steam web helper — normal",
    "galaxyclient": "GOG Galaxy — normal",
    "battle.net": "Battle.net — Blizzard launcher, normal",
    "riotclientservices": "Riot Client — normal",
    "xboxappservices": "Xbox app services — normal",
    "gaming services": "Xbox Gaming Services — normal",
    "ubisoft connect": "Ubisoft Connect — normal",
    "playnite": "Playnite game library — normal",
    "razer central": "Razer Central — normal",
    "nvidia web helper": "NVIDIA Web Helper — normal",
    "nvcontainer": "NVIDIA container — normal",
    "nvbackend": "NVIDIA backend — normal",
    # Dev / cloud / remote tools
    "node": "Node.js runtime — normal",
    "python": "Python interpreter — normal",
    "code": "Visual Studio Code — normal",
    "docker": "Docker Desktop — normal",
    "git": "Git — normal",
    "ngrok": "ngrok tunnel — dev tool, normal",
    "cloudflared": "Cloudflare tunnel — dev tool, normal",
    "teamviewer": "TeamViewer remote access — normal",
    "anydesk": "AnyDesk remote access — normal",
    "rustdesk": "RustDesk remote access — normal",
    "discord": "Discord — chat app, normal",
    "slack": "Slack — chat app, normal",
    "telegram": "Telegram — chat app, normal",
    "whatsapp": "WhatsApp desktop — normal",
    "zoom": "Zoom — meetings, normal",
    "spotify": "Spotify — normal",
    "obs64": "OBS Studio — streaming, normal",
    "videoconference": "conferencing app — normal",
    "dropbox": "Dropbox — normal",
    "boxsync": "Box Sync — normal",
    "citrix": "Citrix Workspace — normal",
    "vpnui": "VPN UI — normal",
    "openvpn": "OpenVPN — normal",
    "wireguard": "WireGuard — normal",
    "mullvad-daemon": "Mullvad VPN — normal",
    "ipsec": "IPsec service — normal",
    "lldaemon": "Logi tech daemon — normal",
    "lgcentral": "Logitech G HUB — normal",
    "synapse": "Razer Synapse — normal",
    "icue": "Corsair iCUE — normal",
    "osu": "osu! game — normal",
    "java": "Java runtime — normal",
    "dotnet": ".NET runtime — normal",
    "powershell": "PowerShell — normal",
    "pwsh": "PowerShell 7 — normal",
    "cmd": "Command Prompt — normal",
    "windbg": "Windows debugger — normal",
    "procmon": "Process Monitor — normal",
    "wireshark": "Wireshark — normal",
    "nmap": "Nmap scanner — recon tool, normal",
    "sqlmap": "sqlmap — recon tool, normal",
    "metasploit": "Metasploit framework — normal",
    "burp": "Burp Suite — normal",
    "ffmpeg": "FFmpeg — normal",
    "7z": "7-Zip — normal",
    "winrar": "WinRAR — normal",
    "pcman": "PCMan — normal",
    "telemetry": "telemetry service — normal",
}


def _netsnap_process_label(name: str, company: str) -> str:
    """Canonical label for a process name (case-insensitive), plus company
    fallback. Returns "" when unknown — the model keeps raw evidence then."""
    if not name:
        return ""
    key = name.strip().lower()
    label = _NET_KNOWN_PROCESSES.get(key)
    if label:
        return label
    comp = (company or "").strip()
    if not comp or len(comp) > 48:
        return ""
    return f"vendor: {comp}"
