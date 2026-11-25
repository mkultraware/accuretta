# SPDX-License-Identifier: AGPL-3.0-only
"""Looking at this machine for the blue-team tools: the network snapshot (connections,
owning processes, known ranges), Windows event logs, and the persistence hunt.

Moved out of bridge.py unchanged; bridge.py re-exports every name here.
"""
from __future__ import annotations

import ipaddress
import json
import os
import time
from accuretta.core.paths import DATA
from accuretta.security.approvals import request_approval
from accuretta.tools.blue_baseline import _network_baseline_apply
from accuretta.tools.netsnap_labels import _netsnap_process_label
from accuretta.tools.powershell import _run_powershell


_NETSNAP_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$conns = Get-NetTCPConnection -State Established,Listen |
    Select-Object LocalAddress, LocalPort, RemoteAddress, RemotePort, State, OwningProcess
$udp = Get-NetUDPEndpoint |
    Select-Object LocalAddress, LocalPort, OwningProcess
$dns = Get-DnsClientCache |
    Where-Object { $_.Type -eq 'A' -or $_.Type -eq 'AAAA' -or $_.Type -eq 1 -or $_.Type -eq 28 } |
    Sort-Object TimeToLive -Descending |
    Select-Object -First 60 -Property Entry, Name, Type, TimeToLive
$procs = @{}
$ids = @($conns.OwningProcess) + @($udp.OwningProcess) | Sort-Object -Unique
# One CIM pass for all pids (command lines + paths), then one signature pass
# per UNIQUE path (capped; hashing big exes is the slow part).
$cmdlines = @{}
$pidToPath = @{}
try {
    $cim = Get-CimInstance Win32_Process | Where-Object { $ids -contains $_.ProcessId }
    foreach ($p in $cim) {
        $cmdlines[[string]$p.ProcessId] = [string]$p.CommandLine
        if ($p.ExecutablePath) { $pidToPath[[string]$p.ProcessId] = [string]$p.ExecutablePath }
    }
} catch {}
$sigCache = @{}
$sigCount = 0
$sigSw = [System.Diagnostics.Stopwatch]::StartNew()
foreach ($path in @($pidToPath.Values | Sort-Object -Unique)) {
    if ($sigCount -ge 20 -or $sigSw.ElapsedMilliseconds -gt 12000) { break }
    $vi = $null
    try { $vi = (Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).VersionInfo } catch {}
    $signed = $null
    if ($vi) {
        try {
            $len = (Get-Item -LiteralPath $path -ErrorAction SilentlyContinue).Length
            if ($len -lt 500MB) {
                $sigCount++
                $sig = Get-AuthenticodeSignature -LiteralPath $path -ErrorAction SilentlyContinue
                if ($sig -and $sig.Status -eq 'Valid') { $signed = $true }
                elseif ($sig -and $sig.Status -eq 'NotSigned') { $signed = $false }
            }
        } catch {}
    }
    $sigCache[$path] = @{
        company = if ($vi -and $vi.CompanyName) { [string]$vi.CompanyName } else { '' }
        desc = if ($vi -and $vi.FileDescription) { [string]$vi.FileDescription } else { '' }
        signed = $signed
    }
}
foreach ($procId in $ids) {
    if (-not $procId) { continue }
    $path = $pidToPath[[string]$procId]
    $meta = if ($path -and $sigCache.ContainsKey($path)) { $sigCache[$path] } else { $null }
    $procs[[string]$procId] = @{
        name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
        path = $path
        company = $(if ($meta) { $meta.company } else { '' })
        desc = $(if ($meta) { $meta.desc } else { '' })
        signed = $(if ($meta) { $meta.signed } else { $null })
        cmdline = $(if ($cmdlines[[string]$procId]) { $cmdlines[[string]$procId] } else { '' })
    }
}
@{ 
    tcp = @($conns)
    udp = @($udp)
    dns_cache = @($dns)
    processes = $procs
} | ConvertTo-Json -Depth 5 -Compress
"""


# Port → well-known service, so the model doesn't have to guess 443 = HTTPS.
_NET_PORT_SERVICES = {
    20: "FTP-data", 21: "FTP", 22: "SSH", 23: "Telnet", 25: "SMTP", 53: "DNS",
    67: "DHCP", 68: "DHCP", 80: "HTTP", 110: "POP3", 123: "NTP", 135: "RPC",
    137: "NetBIOS", 138: "NetBIOS", 139: "NetBIOS", 143: "IMAP", 161: "SNMP",
    389: "LDAP", 443: "HTTPS", 445: "SMB", 465: "SMTPS", 514: "Syslog",
    587: "SMTP-submission", 636: "LDAPS", 853: "DoT (DNS over TLS)",
    993: "IMAPS", 995: "POP3S", 1433: "MSSQL", 1900: "SSDP", 3074: "Xbox Live",
    3306: "MySQL", 3389: "RDP", 5060: "SIP", 5353: "mDNS", 5355: "LLMNR",
    5432: "PostgreSQL", 5985: "WinRM", 5986: "WinRM-TLS", 6379: "Redis",
    8080: "HTTP-alt", 8443: "HTTPS-alt", 27015: "Steam/Game", 27036: "Steam P2P",
    45000: "game (Steam), auth port", 47984: "Steam", 47989: "Steam",
}


# Well-known remote IP ranges so "is this Microsoft/Google/random-VPS" has an
# answer beyond the model's priors. Kept to stable, commonly-seen blocks.
_NET_KNOWN_RANGES = (
    ("Microsoft", (
        "13.104.0.0/14", "13.107.0.0/16", "20.0.0.0/8", "40.0.0.0/8",
        "51.0.0.0/8", "52.0.0.0/8", "52.96.0.0/12", "52.112.0.0/14",
        "52.120.0.0/14", "168.61.0.0/16", "20.190.128.0/18",
    )),
    ("Google", (
        "8.8.8.0/24", "8.8.4.0/24", "142.250.0.0/15", "172.217.0.0/16",
        "216.58.0.0/16", "35.0.0.0/8",
    )),
    ("AWS", ("3.0.0.0/8", "13.32.0.0/15", "15.0.0.0/8", "18.0.0.0/8",
             "52.94.0.0/15", "54.0.0.0/8")),
    ("Cloudflare", ("104.16.0.0/13", "172.64.0.0/13", "1.1.1.0/24",
                    "162.159.0.0/16", "104.17.0.0/16", "173.245.48.0/20")),
    ("Akamai", ("23.32.0.0/11", "104.64.0.0/10", "184.24.0.0/13")),
    ("OpenAI", ("170.63.0.0/16", "23.235.32.0/20")),
    ("Valve/Steam", ("155.133.0.0/16", "162.254.192.0/21", "208.64.200.0/22")),
    ("Discord", ("162.159.128.0/18", "104.16.0.0/13")),
    ("Meta", ("157.240.0.0/16", "31.13.24.0/21", "69.171.224.0/19")),
    ("Netflix", ("45.57.128.0/17", "64.120.128.0/17")),
    ("Apple", ("17.0.0.0/8", "144.178.0.0/16", "103.119.240.0/22")),
    ("Epic Games", ("34.199.0.0/16", "54.148.0.0/15")),
    ("Riot Games", ("192.64.0.0/16", "104.160.128.0/20")),
    ("Blizzard", ("24.105.0.0/16", "137.221.0.0/16")),
    ("Ubisoft", ("63.35.0.0/16", "15.188.0.0/16")),
    ("GitHub", ("140.82.0.0/16", "185.199.108.0/22")),
)


# Keep per-machine cost bounded: unique process paths only, and only the first
# few thousand bytes of any command line (models don't need argv[50]).
_NETSNAP_MAX_CMDLINE = 300


def _netsnap_ip_hint(ip: str) -> str:
    """Who owns this IP? Matches against well-known ranges, returns '' if
    unknown (public/unassigned) or 'private/LAN' for RFC1918/link-local."""
    if not ip or ip in ("0.0.0.0", "::", "*", ""):
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return "private/LAN"
    if addr.is_multicast or addr.is_reserved:
        return ""
    for vendor, cidrs in _NET_KNOWN_RANGES:
        for cidr in cidrs:
            if ipaddress.ip_address(ip) in ipaddress.ip_network(cidr):
                return vendor
    return ""


def _netsnap_debug_dump(stdout: str, stderr: str, exit_code=None, reason: str = "") -> str:
    """Write the raw PowerShell stdout/stderr to ./data/netsnap-debug.log so we
    can see exactly what came back when the JSON parse fails. Returns the path."""
    try:
        log_dir = DATA
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "netsnap-debug.log"
        sep = "=" * 70
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        body = (
            f"\n{sep}\n[{ts}]  reason={reason}  exit_code={exit_code}\n"
            f"--- stdout (len={len(stdout)}) ---\n{stdout}\n"
            f"--- stderr (len={len(stderr)}) ---\n{stderr}\n"
        )
        with open(path, "a", encoding="utf-8") as f:
            f.write(body)
        return str(path)
    except Exception as e:
        return f"<debug-dump failed: {e}>"


# ---- host event log queries (blue team) ------------------------------------
# Live Get-WinEvent triage for the four highest-signal event families:
#  4624/4625  logon success/failure (Security — needs admin to read)
#  4688       process creation (Security — needs admin)
#  4698       scheduled task created (Security — needs admin)
#  7045       service installed (System — readable by standard users)
#  1074/6005/6006  System boot/shutdown/restart (System — readable)
#  1001       crash (Application — readable)
# The script reads whatever it can and marks each log with its access status so
# the tool can tell the model "Security log not readable — run elevated" while
# still delivering the System/Application half of the triage.
_EVTLOG_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
# The caller supplies explicit variables before this script runs. Do not use
# PowerShell's automatic $args variable here; -Command treats it differently
# across host/elevated invocations.
# The app can inherit a PSModulePath that contains Accuretta's bundled
# PowerShell modules before Windows' own modules. That copy can fail its
# AuthorizationManager check, which leaves Get-WinEvent undefined and used to
# make the collector return a misleading empty response. Restrict this query
# to the trusted Windows module directory.
$env:PSModulePath = Join-Path $PSHOME 'Modules'
Import-Module Microsoft.PowerShell.Diagnostics -ErrorAction SilentlyContinue
$ids = @(4624, 4625, 4688, 4698, 7045, 1074, 6005, 6006, 1001)
$since = (Get-Date).AddHours(-$hours)
$out = @()
try {
    # Probe access first: a plain read throws 'unauthorized operation' when the
    # log ACL blocks us (FilterHashtable silently returns zero events instead,
    # which would masquerade as 'no events' — bad triage data).
    $null = Get-WinEvent -LogName $log -MaxEvents 1 -ErrorAction Stop
} catch {
    $msg = "$($_.Exception.Message)"
    $status = if ($msg -match '(?i)unauthorized|access is denied|access denied|privilege|beh.righet|.tkomst') { 'denied' } else { 'error' }
    [ordered]@{ log = $log; access = $status; error = $msg; events = @() } | ConvertTo-Json -Compress -Depth 4
    exit 0
}
try {
    # Filter by the small event family up front. Reading thousands of unrelated
    # Security events and discarding them afterwards can hit the 60s collector
    # timeout on an active machine.
    $filter = @{ LogName = $log; StartTime = $since; Id = $ids }
    $events = Get-WinEvent -FilterHashtable $filter -MaxEvents $limit -ErrorAction Stop
} catch {
    $msg = "$($_.Exception.Message)"
    if ($msg -match 'No events were found') {
        [ordered]@{ log = $log; access = 'ok'; events = @() } | ConvertTo-Json -Compress -Depth 4
    } else {
        $status = if ($msg -match '(?i)unauthorized|access is denied|access denied|privilege|beh.righet|.tkomst') { 'denied' } else { 'error' }
        [ordered]@{ log = $log; access = $status; error = $msg; events = @() } | ConvertTo-Json -Compress -Depth 4
    }
    exit 0
}
foreach ($e in $events) {
    if ($ids -and ($ids -notcontains $e.Id)) { continue }
    $msg = [string]$e.Message
    if ($msg.Length -gt 900) { $msg = $msg.Substring(0, 900) + '…' }
    $out += @{
        id = $e.Id
        time = $e.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss')
        provider = [string]$e.ProviderName
        msg = $msg
    }
    if ($out.Count -ge $limit) { break }
}
[ordered]@{ log = $log; access = 'ok'; events = @($out) } | ConvertTo-Json -Compress -Depth 4
'''


# ---- persistence hunt (blue team) -------------------------------------------
# Read-only sweep of every place software hides to survive a reboot:
#  - Run / RunOnce keys (HKLM + HKCU, incl. Wow6432Node)
#  - Startup folders (user + all users)
#  - Scheduled tasks (created recently, or whose action lives in a suspicious
#    folder like Temp/Downloads/AppData)
#  - Services whose binary lives outside C:\Windows (a service that launches
#    from Temp or a user folder is the classic persistence move)
#  - WMI event subscriptions, separating harmless filters and logging consumers
#    from bindings that can execute a command or script
# The script returns raw data; the Python side does the known-good filtering
# so the model sees a short list of "worth a look" items, not a wall of noise.
_PERSIST_PS = r'''
$ErrorActionPreference = 'SilentlyContinue'
$script:items = @()
function Add-Item($cat, $loc, $name, $value, $bound = $null, $consumerClass = '', $consumerPayload = '') {
    $row = @{ cat = $cat; loc = $loc; name = $name; value = $value }
    if ($null -ne $bound) { $row.bound = [bool]$bound }
    if ($consumerClass) { $row.consumer_class = $consumerClass }
    if ($consumerPayload) { $row.consumer_payload = $consumerPayload }
    $script:items += $row
}
# --- Run/RunOnce keys ---
$runKeys = @(
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
)
foreach ($k in $runKeys) {
    try {
        $props = Get-ItemProperty -Path $k -ErrorAction Stop
        foreach ($p in $props.PSObject.Properties) {
            if ($p.Name -match '^(PSPath|PSParentPath|PSChildName|PSDrive|PSProvider)$') { continue }
            $v = [string]$p.Value
            if ($v.Length -gt 300) { $v = $v.Substring(0, 300) + '…' }
            Add-Item 'run_key' $k $p.Name $v
        }
    } catch {}
}
# --- Startup folders: resolve the real .lnk target for ground truth ---
$startupDirs = @(
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup",
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup"
)
$shell = New-Object -ComObject WScript.Shell
foreach ($d in $startupDirs) {
    try {
        foreach ($f in Get-ChildItem -LiteralPath $d -File -ErrorAction Stop) {
            $target = $f.FullName
            try { $target = $shell.CreateShortcut($f.FullName).TargetPath } catch {}
            Add-Item 'startup_file' $d $f.Name $target
        }
    } catch {}
}
# --- Scheduled tasks: action path in a suspicious folder. NOT AppData/
# ProgramData — those are where legitimate updaters and Defender live.
$suspectParts = @('\Temp\', '\Downloads\', '\Desktop\', '$Recycle.Bin', '\Windows\Temp\')
$tasks = @()
try { $tasks = @(Get-ScheduledTask -ErrorAction Stop) } catch {}
foreach ($t in $tasks) {
    $path = ''
    foreach ($a in $t.Actions) {
        if ($a.Execute) { $path = [string]$a.Execute; break }
    }
    if (-not $path) { continue }
    $isSuspect = $false
    foreach ($s in $suspectParts) { if ($path -match [regex]::Escape($s)) { $isSuspect = $true; break } }
    if (-not $isSuspect) { continue }
    Add-Item 'scheduled_task' ($t.TaskPath + $t.TaskName) $path ($t.State.ToString())
}
# --- Services launching from outside C:\Windows / Program Files (incl. x86) ---
$services = @()
try { $services = @(Get-CimInstance Win32_Service -ErrorAction Stop) } catch {}
foreach ($s in $services) {
    $bin = [string]$s.PathName
    if (-not $bin) { continue }
    if ($bin -match '^"?C:\\Windows\\' -or $bin -match '^"?C:\\Program Files( \(x86\))?\\' -or $bin -match '^"?C:\\ProgramData\\Microsoft\\Windows Defender\\' -or $bin -match '^\\SystemRoot\\') { continue }
    if ($bin.Length -gt 300) { $bin = $bin.Substring(0, 300) + '…' }
    Add-Item 'service' $s.Name $s.State $bin
}
# --- WMI event subscriptions ---
# A filter by itself does nothing. Only a filter bound to an event consumer is
# an active subscription, and only executable/script consumers are a useful
# persistence signal. Keep unbound filters in inventory without flagging them.
try {
    $filters = @(Get-WmiObject -Namespace 'root\subscription' -Class __EventFilter -ErrorAction Stop)
    $bindings = @(Get-WmiObject -Namespace 'root\subscription' -Class __FilterToConsumerBinding -ErrorAction Stop)
    $consumers = @(Get-WmiObject -Namespace 'root\subscription' -Class __EventConsumer -ErrorAction Stop)
    foreach ($f in $filters) {
        $q = [string]$f.Query
        if ($q.Length -gt 400) { $q = $q.Substring(0, 400) + '…' }
        $filterName = [string]$f.Name
        $escapedName = [regex]::Escape($filterName)
        $matches = @($bindings | Where-Object { ([string]$_.Filter) -match ('Name="' + $escapedName + '"') })
        if ($matches.Count -eq 0) {
            Add-Item 'wmi_filter_unbound' $filterName '' $q $false
            continue
        }
        foreach ($binding in $matches) {
            $consumerRef = [string]$binding.Consumer
            $consumerName = ''
            $nameMatch = [regex]::Match($consumerRef, 'Name="([^\"]+)"')
            if ($nameMatch.Success) { $consumerName = $nameMatch.Groups[1].Value }
            $consumer = $consumers | Where-Object { [string]$_.Name -eq $consumerName } | Select-Object -First 1
            $consumerClass = if ($consumer) { [string]$consumer.__CLASS } else { '' }
            $consumerPayload = ''
            if ($consumer) {
                foreach ($field in @('CommandLineTemplate', 'ExecutablePath', 'ScriptText')) {
                    $candidate = [string]$consumer.$field
                    if ($candidate) { $consumerPayload = $candidate; break }
                }
            }
            if ($consumerPayload.Length -gt 400) { $consumerPayload = $consumerPayload.Substring(0, 400) + '…' }
            Add-Item 'wmi_subscription' $filterName $consumerName $q $true $consumerClass $consumerPayload
        }
    }
} catch {}
[Console]::WriteLine((ConvertTo-Json -InputObject @($script:items) -Compress -Depth 4))
'''


# Event IDs the blue tools reference, so labels stay consistent across tool
# output and the model's writeup.
_HOST_EVT_LABELS = {
    4624: "logon success",
    4625: "logon failure",
    4688: "process created",
    4698: "scheduled task created",
    7045: "service installed",
    1074: "system shutdown/restart",
    6005: "event log service started",
    6006: "clean shutdown",
    1001: "application crash",
}


def tool_network_snapshot(args: dict, *, approved: bool = False) -> dict:
    """Snapshot the host's network state (no admin, no install): active TCP
    connections with owning processes, UDP listeners and the DNS resolver cache.
    Processes are enriched with identity ground truth (exe path, publisher,
    description, Authenticode status, truncated command line) and known remote
    endpoints get vendor/port hints, so the model doesn't guess."""
    if os.name != "nt":
        return {"error": "network_snapshot currently only supports Windows (uses Get-NetTCPConnection)"}
    if not approved:
        approval = request_approval(
            title="Network snapshot",
            command="Get-NetTCPConnection / Get-NetUDPEndpoint / Get-DnsClientCache",
            details={"kind": "network_snapshot"},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied snapshot ({approval.get('status')})"}

    # 2 MiB cap: busy machines easily produce more than 16 KiB of JSON. The model
    # only sees the aggregated and bounded views, so keeping the full payload
    # costs no context.
    res = _run_powershell(_NETSNAP_PS, timeout=30, max_stdout=2_000_000)
    raw_stdout = res.get("stdout") or ""
    raw_stderr = res.get("stderr") or ""
    ok_flag = res.get("ok")
    if not ok_flag:
        # dump everything we know about the failure so we can diagnose
        _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="powershell-not-ok")
        return {"error": (raw_stderr or raw_stdout or "snapshot failed").strip()[:400]}
    raw = raw_stdout.lstrip("\ufeff").strip()
    if not raw:
        _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="empty-stdout")
        return {"error": "empty output from PowerShell"}
    data = None
    try:
        # Windows version-resource strings occasionally contain literal control
        # characters. PowerShell's ConvertTo-Json leaves some of them unescaped,
        # so strict parsing can reject an otherwise complete snapshot.
        data = json.loads(raw, strict=False)
    except Exception:
        start = -1
        for k, ch in enumerate(raw):
            if ch in "{[":
                start = k
                break
        if start >= 0:
            opener = raw[start]
            closer = "}" if opener == "{" else "]"
            end = raw.rfind(closer)
            if end > start:
                candidate = raw[start:end + 1]
                try:
                    data = json.loads(candidate, strict=False)
                except Exception:
                    pass
    if data is None:
        debug_path = _netsnap_debug_dump(raw_stdout, raw_stderr, exit_code=res.get("returncode"), reason="json-parse-failed")
        return {
            "error": "parse failed: no valid JSON in PowerShell output",
            "raw": raw[:600],
            "debug_dump": debug_path,
            "stderr_head": raw_stderr[:300],
        }

    tcp = data.get("tcp") or []
    udp = data.get("udp") or []
    procs = data.get("processes") or {}
    if isinstance(tcp, dict): tcp = [tcp]
    if isinstance(udp, dict): udp = [udp]

    # Processes arrive as {pid: {name, path, company, desc, signed, cmdline}}.
    # Fold the enriched fields onto each connection and keep a per-process detail
    # map so the model gets identity ground truth instead of bare names.
    def _proc_meta(pid):
        p = procs.get(str(pid))
        return p if isinstance(p, dict) else None

    for c in tcp:
        m = _proc_meta(c.get("OwningProcess"))
        c["process"] = (m or {}).get("name") or "?"
    for c in udp:
        m = _proc_meta(c.get("OwningProcess"))
        c["process"] = (m or {}).get("name") or "?"

    # group by remote endpoint so destinations with many connections rise to the top.
    from collections import Counter
    remotes = Counter()
    for c in tcp:
        addr = c.get("RemoteAddress") or ""
        port = c.get("RemotePort") or 0
        if addr and addr not in ("0.0.0.0", "::", "127.0.0.1", "::1"):
            remotes[(addr, int(port))] += 1
    top_remotes = []
    for (a, p), n in remotes.most_common(30):
        owner = _netsnap_ip_hint(a)
        entry = {"address": a, "port": p, "count": n}
        svc = _NET_PORT_SERVICES.get(int(p))
        if svc:
            entry["service"] = svc
        if owner:
            entry["known_owner"] = owner
        top_remotes.append(entry)

    # connections per process — handy for "what is svchost talking to"
    by_proc = Counter()
    for c in tcp:
        by_proc[c.get("process") or "?"] += 1
    top_procs = []
    for k, v in by_proc.most_common(20):
        entry = {"process": k, "connections": v}
        # find one pid for this name so we can attach identity
        for pid_str, meta in procs.items():
            if isinstance(meta, dict) and (meta.get("name") or "?") == k:
                label = _netsnap_process_label(k, meta.get("company"))
                if label:
                    entry["identity"] = label
                if meta.get("signed") is True:
                    entry["signed"] = True
                elif meta.get("signed") is False:
                    entry["signed"] = False
                break
        top_procs.append(entry)

    listeners = []
    for c in udp:
        la = c.get("LocalAddress") or ""
        if la in ("0.0.0.0", "::"):
            listeners.append(c)
    listeners = listeners[:30]

    # compact per-process details for the top talkers (bounded; full cmdline
    # truncated — models need identity, not argv). Sorted by connection count.
    proc_details = {}
    for pid_str, meta in procs.items():
        if not isinstance(meta, dict):
            continue
        name = meta.get("name") or "?"
        cnt = by_proc.get(name, 0)
        if not cnt:
            continue
        detail = {
            "process": name,
            "connections": cnt,
            "signed": meta.get("signed"),
        }
        label = _netsnap_process_label(name, meta.get("company"))
        if label:
            detail["identity"] = label
        if meta.get("company"):
            detail["company"] = str(meta.get("company"))[:64]
        if meta.get("path"):
            detail["path"] = str(meta.get("path"))[:160]
        if meta.get("desc"):
            detail["description"] = str(meta.get("desc"))[:80]
        cmdline = str(meta.get("cmdline") or "").strip()
        if cmdline:
            detail["cmdline"] = cmdline[:_NETSNAP_MAX_CMDLINE]
        proc_details[name] = detail
    proc_list = sorted(proc_details.values(), key=lambda d: d["connections"], reverse=True)[:40]

    snapshot = {
        "platform": "windows",
        "tcp_count": len(tcp),
        "udp_count": len(udp),
        "tcp_connections": tcp[:80],
        "udp_listeners": listeners,
        "top_remotes": top_remotes,
        "top_processes": top_procs,
        "process_details": proc_list,
        "recent_dns": data.get("dns_cache") or [],
    }
    try:
        _network_baseline_apply(snapshot, args or {})
    except Exception as e:
        snapshot["baseline_error"] = str(e)[:240]
    return snapshot


def tool_parse_event_logs(args: dict, *, approved: bool = False) -> dict:
    """Live read of Windows event logs (no .evtx file needed): logon
    success/failure (4624/4625), process creation (4688), scheduled-task creation
    (4698), service installs (7045), plus boot/shutdown and crash events.
    Read-only. The Security log needs an elevated session; when it isn't
    readable the tool says so and still returns the other logs."""
    if os.name != "nt":
        return {"error": "event logs are Windows-only"}
    log = str(args.get("log") or "System").strip().lower()
    if log not in ("security", "system", "application"):
        return {"error": "log must be one of: security, system, application"}
    hours = max(1, min(int(args.get("hours") or 24), 168))
    limit = max(1, min(int(args.get("limit") or 150), 500))

    if not approved:
        approval = request_approval(
            title="Read event log",
            command=f"Get-WinEvent -LogName {log} (last {hours}h)",
            details={"kind": "event_log", "log": log, "hours": hours},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied event log read ({approval.get('status')})"}

    res = _run_powershell(
        f"$log = '{log}'; $hours = {hours}; $limit = {limit}; {_EVTLOG_PS}",
        timeout=60, max_stdout=600_000,
    )
    raw = (res.get("stdout") or "").lstrip("\ufeff").strip()
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        pass
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        detail = res.get("error") or res.get("stderr") or raw or "empty"
        return {"error": f"unexpected PowerShell output: {str(detail)[:300]}"}

    access = data.get("access") or "ok"
    events = data["events"]
    if not events:
        note = (
            "Security log requires elevation" if access == "denied"
            else f"Windows could not read this log: {str(data.get('error') or 'unknown error')[:220]}"
            if access == "error" else "no matching events in the window"
        )
        return {
            "log": log,
            "access": access,
            "note": note,
            "events": [],
        }
    out = []
    for e in events[:limit]:
        if not isinstance(e, dict):
            continue
        rec = {
            "event_id": e.get("id"),
            "label": _HOST_EVT_LABELS.get(int(e.get("id") or 0), "other"),
            "time": e.get("time"),
            "provider": e.get("provider"),
            "details": e.get("msg") or "",
        }
        out.append(rec)
    return {"log": log, "access": access, "event_count": len(out), "events": out}


def tool_persistence_hunt(args: dict, *, approved: bool = False) -> dict:
    """Read-only persistence sweep: Run/RunOnce keys, startup folders, scheduled
    tasks and services launching from suspicious folders (Temp, Downloads,
    AppData, user dirs), and WMI event subscriptions. Returns a short list of
    evidence-backed items; ordinary startup entries, unbound WMI filters and
    logging-only consumers stay in inventory instead of becoming alerts."""
    if os.name != "nt":
        return {"error": "persistence hunt is Windows-only"}
    if not approved:
        approval = request_approval(
            title="Persistence sweep",
            command="Read startup keys, startup folders, scheduled tasks, service paths, WMI subscriptions (read-only)",
            details={"kind": "persistence_hunt"},
        )
        if approval.get("decision") != "approve":
            return {"error": f"user denied persistence sweep ({approval.get('status')})"}

    res = _run_powershell(_PERSIST_PS, timeout=60, max_stdout=400_000)
    raw = (res.get("stdout") or "").lstrip("\ufeff").strip()
    data = None
    try:
        data = json.loads(raw)
    except Exception:
        pass
    if not isinstance(data, list):
        return {"error": f"unexpected PowerShell output: {(res.get('stderr') or raw or 'empty')[:300]}"}

    # Flag rules require at least two useful signals; a non-standard location,
    # unknown vendor or unbound WMI filter alone is inventory, not an alert.
    from collections import Counter
    by_cat = Counter(i.get("cat") for i in data)
    flagged = []
    plain = []
    _SUSPICIOUS_PATHS = ("\\temp\\", "\\downloads\\", "\\desktop\\", "$recycle.bin", "\\windows\\temp\\")

    for i in data:
        if not isinstance(i, dict):
            continue
        item = {
            "category": i.get("cat"),
            "location": str(i.get("loc") or "")[:200],
            "name": str(i.get("name") or "")[:120],
            "value": str(i.get("value") or "")[:240],
            "bound": bool(i.get("bound")) if "bound" in i else None,
            "consumer_class": str(i.get("consumer_class") or "")[:100],
            "consumer_payload": str(i.get("consumer_payload") or "")[:400],
        }
        plain.append(item)
        cat = i.get("cat")
        if cat == "wmi_subscription":
            consumer_class = item["consumer_class"].lower()
            payload = item["consumer_payload"].lower()
            executable_consumer = consumer_class in {
                "commandlineeventconsumer", "activescripteventconsumer",
            }
            suspicious_payload = any(part in payload for part in (
                "\\temp\\", "\\downloads\\", "\\desktop\\", "$recycle.bin",
                "powershell", "pwsh", "cmd.exe", "wscript", "cscript",
                "-enc", "frombase64string",
            ))
            score = int(item["bound"] is True) + int(executable_consumer) + (2 if suspicious_payload else 0)
            item["evidence_score"] = score
            if score >= 2:
                flagged.append(item)
        elif cat == "scheduled_task":
            item["evidence_score"] = 2
            flagged.append(item)
        elif cat == "service":
            val = str(i.get("value") or "").lower()
            if any(part in val for part in _SUSPICIOUS_PATHS):
                item["evidence_score"] = 2
                flagged.append(item)
        elif cat in ("run_key", "startup_file"):
            val = str(i.get("value") or "").lower()
            suspect = any(k in val for k in _SUSPICIOUS_PATHS)
            if suspect:
                item["evidence_score"] = 2
                flagged.append(item)

    return {
        "swept": dict(by_cat),
        "flagged": flagged[:40],
        "all_entries": plain[:200],
        "note": ("flagged = items with at least two useful signals; all_entries = "
                 "everything found for context. Unbound WMI filters and non-standard "
                 "locations without corroborating evidence stay inventory-only."),
    }
