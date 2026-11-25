# SPDX-License-Identifier: AGPL-3.0-only
<#
.SYNOPSIS
  Build the Windows installer: tools\installer\dist\Accuretta-Setup-<version>.exe

.DESCRIPTION
  The installer carries the official Python embeddable package, a small set of
  Python packages (requirements-installer.txt) and Accuretta's source files.
  llama.cpp and models are NOT bundled: the first-run wizard downloads the
  right llama.cpp build for the user's GPU.

  Steps: take the app files from a release tag (git archive, the same files a
  clone gets), download and verify Python, install the packages, write
  install.json, fetch the WebView2 bootstrapper, compile accuretta.iss with
  Inno Setup 6 and print the installer's SHA-256.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\installer\build.ps1 -Tag v1.1.0

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\installer\build.ps1 -Dev
  A test build from the working tree (tracked + new, not ignored files). Never publish it.

.PARAMETER TrustNewPython
  Pin the SHA-256 of a Python embeddable zip that isn't in python.sha256 yet.
  Compare the printed hash with the one on python.org's release page first.

.PARAMETER NoInstaller
  Stop after preparing build\ (skip Inno Setup). Useful to test build\app with
  build\python directly.
#>
param(
    [string]$Tag = "",
    [switch]$Dev,
    [switch]$TrustNewPython,
    [switch]$NoInstaller
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Repo = (Resolve-Path (Join-Path $Here "..\..")).Path
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
$Cache = Join-Path $Here "cache"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message" -ForegroundColor Cyan
}

# ---- 1. what to build -------------------------------------------------------
Push-Location $Repo
try {
    if (-not $Dev) {
        if (-not $Tag) { Fail "Give a release tag, e.g. -Tag v1.1.0 (or -Dev for a test build)." }
        $dirty = git status --porcelain
        if ($dirty) { Fail "The working tree has uncommitted changes. Commit or stash them first." }
        git rev-parse --verify --quiet "refs/tags/$Tag" | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "Tag $Tag doesn't exist. Create it first." }
    }
} finally {
    Pop-Location
}

foreach ($dir in @($Build, $Dist, $Cache)) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
}
if (Test-Path (Join-Path $Build "app")) { Remove-Item -Recurse -Force (Join-Path $Build "app") }
if (Test-Path (Join-Path $Build "python")) { Remove-Item -Recurse -Force (Join-Path $Build "python") }

# ---- 2. app files -------------------------------------------------------------
Step "Copying Accuretta's files"
$AppDir = Join-Path $Build "app"
New-Item -ItemType Directory -Force $AppDir | Out-Null
Push-Location $Repo
try {
    if ($Dev) {
        $files = git ls-files --cached --others --exclude-standard
        foreach ($rel in $files) {
            if (-not $rel) { continue }
            $src = Join-Path $Repo $rel
            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) { continue }   # deleted, not committed yet
            $dst = Join-Path $AppDir $rel
            $parent = Split-Path -Parent $dst
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    } else {
        $zip = Join-Path $Cache "app-$Tag.zip"
        git archive --format=zip -o $zip "refs/tags/$Tag"
        if ($LASTEXITCODE -ne 0) { Fail "git archive failed." }
        Expand-Archive -LiteralPath $zip -DestinationPath $AppDir -Force
    }
} finally {
    Pop-Location
}
# Developer-only folders never ship.
foreach ($rel in @("tests", ".github", "tools\installer\build", "tools\installer\dist", "tools\installer\cache")) {
    $p = Join-Path $AppDir $rel
    if (Test-Path -LiteralPath $p) { Remove-Item -Recurse -Force -LiteralPath $p }
}
# A clone carries only data\.gitkeep; anything else in data\ is someone's real data.
$DataDir = Join-Path $AppDir "data"
if (Test-Path -LiteralPath $DataDir) {
    $real = Get-ChildItem -LiteralPath $DataDir -Force -Recurse -File | Where-Object { $_.Name -ne ".gitkeep" }
    if ($real) { Fail "build\app\data contains real files ($($real[0].Name)), which must never ship. Check .gitignore." }
    Remove-Item -Recurse -Force -LiteralPath $DataDir   # installer builds keep data in the user folder
}
foreach ($private in @("bench", "bridge_mcp_config.json", "email_plugin\email_config.json")) {
    if (Test-Path -LiteralPath (Join-Path $AppDir $private)) {
        Fail "build\app contains '$private', which must never ship. Check .gitignore."
    }
}

$IndexHtml = [System.IO.File]::ReadAllText((Join-Path $AppDir "index.html"))
$VersionMatch = [regex]::Match($IndexHtml, 'id=["'']brand-v["''][^>]*>\s*v?([^<\s]+)')
if (-not $VersionMatch.Success) { Fail "Couldn't read the version from index.html (id=brand-v)." }
$Version = $VersionMatch.Groups[1].Value
if ($Dev) { $Version = "$Version-dev" }
Write-Host "Version: $Version"

# ---- 3. Python embeddable package ----------------------------------------------
Step "Preparing Python"
$PyVersion = (& python -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0 -or -not $PyVersion) { Fail "A full Python must be on PATH to build (it installs the packages)." }
$PyParts = $PyVersion.Split(".")
$PyShort = "$($PyParts[0])$($PyParts[1])"
$PyArch = (& python -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($PyArch -ne "64") { Fail "The build Python must be 64-bit." }
$PyZipName = "python-$PyVersion-embed-amd64.zip"
$PyZip = Join-Path $Cache $PyZipName
if (-not (Test-Path $PyZip)) {
    $url = "https://www.python.org/ftp/python/$PyVersion/$PyZipName"
    Write-Host "Downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $PyZip -UseBasicParsing
}
$Actual = (Get-FileHash $PyZip -Algorithm SHA256).Hash.ToLower()
$LockFile = Join-Path $Here "python.sha256"
$Pinned = $null
if (Test-Path $LockFile) {
    foreach ($line in Get-Content $LockFile) {
        $parts = $line.Trim() -split "\s+"
        if ($parts.Count -ge 2 -and $parts[0] -eq $PyZipName) { $Pinned = $parts[1].ToLower() }
    }
}
if ($Pinned) {
    if ($Pinned -ne $Actual) { Remove-Item $PyZip; Fail "SHA-256 mismatch for $PyZipName. The download was removed; try again." }
} elseif ($TrustNewPython) {
    [System.IO.File]::AppendAllText($LockFile, "$PyZipName $Actual`r`n", $Utf8NoBom)
    Write-Host "Pinned $PyZipName $Actual"
} else {
    Fail "No pinned SHA-256 for $PyZipName. Its hash is $Actual. Compare it with python.org's release page, then run again with -TrustNewPython."
}
$PyDir = Join-Path $Build "python"
Expand-Archive -LiteralPath $PyZip -DestinationPath $PyDir -Force
$Pth = Join-Path $PyDir "python$PyShort._pth"
if (-not (Test-Path $Pth)) { Fail "python$PyShort._pth not found in the embeddable package." }
# The standard library zip, the Python folder, Lib (tkinter, below), the bundled
# packages and the app, and `import site` so .pth files in site-packages work.
$PthText = "python$PyShort.zip`r`n.`r`nLib`r`nLib\site-packages`r`n..\app`r`nimport site`r`n"
[System.IO.File]::WriteAllText($Pth, $PthText, $Utf8NoBom)

# The embeddable package has no tkinter, and the folder and file pickers in
# Settings use it. Copy it from the build Python, which is the same version.
Step "Adding tkinter (the folder and file pickers)"
$PyPrefix = (& python -c "import sys; print(sys.prefix)").Trim()
$TkFiles = @(
    @{ From = "Lib\tkinter"; To = "Lib\tkinter" },
    @{ From = "DLLs\_tkinter.pyd"; To = "_tkinter.pyd" },
    @{ From = "DLLs\tcl86t.dll"; To = "tcl86t.dll" },
    @{ From = "DLLs\tk86t.dll"; To = "tk86t.dll" },
    @{ From = "DLLs\zlib1.dll"; To = "zlib1.dll" },     # tcl86t.dll needs it
    @{ From = "tcl\tcl8"; To = "tcl\tcl8" },
    @{ From = "tcl\tcl8.6"; To = "tcl\tcl8.6" },
    @{ From = "tcl\tk8.6"; To = "tcl\tk8.6" }
)
foreach ($item in $TkFiles) {
    $src = Join-Path $PyPrefix $item.From
    if (-not (Test-Path -LiteralPath $src)) {
        Fail "The build Python has no $($item.From). Reinstall it with 'tcl/tk and IDLE' ticked."
    }
    $dst = Join-Path $PyDir $item.To
    $parent = Split-Path -Parent $dst
    if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
    Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
}
foreach ($rel in @("tcl\tk8.6\demos", "Lib\tkinter\__pycache__")) {
    $p = Join-Path $PyDir $rel
    if (Test-Path -LiteralPath $p) { Remove-Item -Recurse -Force -LiteralPath $p }
}
$TkCheck = (& (Join-Path $PyDir "python.exe") -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))")
if ($LASTEXITCODE -ne 0) { Fail "The bundled tkinter doesn't load (see the error above)." }
Write-Host "Tcl/Tk $TkCheck"

# ---- 4. Python packages ----------------------------------------------------------
Step "Installing the bundled Python packages"
$Site = Join-Path $PyDir "Lib\site-packages"
New-Item -ItemType Directory -Force $Site | Out-Null
& python -m pip install --disable-pip-version-check --no-warn-script-location --prefer-binary `
    --target $Site -r (Join-Path $Here "requirements-installer.txt")
if ($LASTEXITCODE -ne 0) { Fail "pip install failed." }
# Byte-code caches are rebuilt on the user's PC.
Get-ChildItem -Path $Build -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# ---- 5. install marker ------------------------------------------------------------
$InstallJson = '{"type": "installer", "version": "' + $Version + '", "python": "' + $PyVersion + '"}'
[System.IO.File]::WriteAllText((Join-Path $Build "install.json"), $InstallJson, $Utf8NoBom)

# ---- 6. WebView2 bootstrapper (for Windows 10 PCs without WebView2) -----------------
$WebView2 = Join-Path $Build "MicrosoftEdgeWebview2Setup.exe"
if (-not (Test-Path $WebView2)) {
    Step "Fetching the WebView2 bootstrapper"
    try {
        Invoke-WebRequest -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" -OutFile $WebView2 -UseBasicParsing
    } catch {
        Write-Host "WARNING: couldn't download the WebView2 bootstrapper; the installer will skip it." -ForegroundColor Yellow
    }
}

if ($NoInstaller) {
    Write-Host ""
    Write-Host "build\ is ready (no installer compiled). Test it with:"
    Write-Host "  $PyDir\python.exe $AppDir\launch.pyw"
    exit 0
}

# ---- 7. compile the installer ---------------------------------------------------------
Step "Compiling the installer"
$Iscc = @(
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { Fail "Inno Setup 6 not found. Install it from https://jrsoftware.org/isdl.php" }
& $Iscc "/DAppVersion=$Version" "/DBuildDir=$Build" "/DDistDir=$Dist" (Join-Path $Here "accuretta.iss")
if ($LASTEXITCODE -ne 0) { Fail "Inno Setup failed." }

$Setup = Join-Path $Dist "Accuretta-Setup-$Version.exe"
$SetupHash = (Get-FileHash $Setup -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "Built $Setup" -ForegroundColor Green
Write-Host "SHA-256: $SetupHash"
Write-Host "Paste that hash into the release notes next to the download."
