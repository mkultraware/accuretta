# SPDX-License-Identifier: AGPL-3.0-only
<#
.SYNOPSIS
  Build the ready-made Maya engine package that Accuretta's "Install Maya" button downloads:
  tools\maya-package\dist\Accuretta-Maya-<version>-win-x64.zip

.DESCRIPTION
  Maya (github.com/alesha-pro/maya-inkling-mini, MIT) runs Inkling 2 Mini far faster than
  llama.cpp, but it ships as source only: building it needs NVIDIA's CUDA toolkit and Visual
  Studio's C++ tools. This script builds it once, for every recent NVIDIA card, so users don't
  have to:

    * maya-ik2.exe for RTX 30 / 40 / 50 (sm_86, sm_89, sm_120) with Maya's portable options
      (AVX2 CPU baseline, the C++ runtime linked in), plus NVIDIA's cuBLAS DLLs beside it
    * maya-vision.exe, the image encoder, portable (AVX2, no OpenMP DLL)
    * Maya's Python server and tokenizer tool, and the three Python libraries they need beyond
      Accuretta's own (jinja2, markupsafe, regex: regex per Python 3.10 - 3.14)
    * the licenses, and accuretta-maya.json (version, source commit, cards, every file's SHA-256)

  It then runs the engine and the tokenizer from the staged folder, zips it, and prints the
  zip's SHA-256. Attach the zip to an Accuretta GitHub release; the app finds the newest one.

  Needs: Visual Studio 2022+ C++ build tools, CUDA Toolkit 13.x, CMake, Ninja, Python with pip,
  and a built Maya checkout with third_party\llama.cpp-b12590 (Maya's docs: INKLING2-MINI.md).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\maya-package\build.ps1

.PARAMETER MayaSource
  The Maya checkout to build from (its working tree, as it is). Default: ~\maya-inkling-mini.

.PARAMETER TestModel
  An Inkling 2 GGUF in Maya's layout, for the tokenizer self-test. Skipped when empty.
#>
param(
    [string]$MayaSource = (Join-Path $HOME "maya-inkling-mini"),
    [string]$Archs = "86;89;120",
    [string]$CudaRoot = "",
    [string]$TestModel = "",
    [switch]$SkipBuild
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Work = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
function Fail($msg) { Write-Host "FAILED: $msg" -ForegroundColor Red; exit 1 }
function Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
# Programs that write progress to stderr: PowerShell 5.1 would stop on that under
# "Stop", so they run under "Continue" and each one's exit code is checked instead.
function Invoke-Native([scriptblock]$Block) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Block } finally { $ErrorActionPreference = $saved }
}

# ---- 1. tools -------------------------------------------------------------------
Step "tools"
$MayaSource = (Resolve-Path $MayaSource).Path
if (-not (Test-Path (Join-Path $MayaSource "src\ik2\ik2_main.cu"))) { Fail "$MayaSource is not a Maya checkout." }
$LlamaDir = Join-Path $MayaSource "third_party\llama.cpp-b12590"
if (-not (Test-Path (Join-Path $LlamaDir "tools\mtmd"))) { Fail "$LlamaDir is missing (see Maya's INKLING2-MINI.md)." }
if (-not $CudaRoot) {
    $CudaRoot = Get-ChildItem "$env:ProgramFiles\NVIDIA GPU Computing Toolkit\CUDA" -Directory -Filter "v13.*" -ErrorAction SilentlyContinue |
        Sort-Object { [version]($_.Name.TrimStart("v")) } | Select-Object -Last 1 -ExpandProperty FullName
}
if (-not $CudaRoot -or -not (Test-Path (Join-Path $CudaRoot "bin\nvcc.exe"))) { Fail "CUDA Toolkit 13.x not found (pass -CudaRoot)." }
$CudaVersion = (Split-Path $CudaRoot -Leaf).TrimStart("v")
$CudaDllDir = @((Join-Path $CudaRoot "bin\x64"), (Join-Path $CudaRoot "bin")) | Where-Object { Test-Path (Join-Path $_ "cublas64_13.dll") } | Select-Object -First 1
if (-not $CudaDllDir) { Fail "cublas64_13.dll not found under $CudaRoot\bin." }
$VsWhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$VsPath = & $VsWhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
$VcVars = Join-Path $VsPath "VC\Auxiliary\Build\vcvarsall.bat"
if (-not (Test-Path $VcVars)) { Fail "Visual Studio C++ build tools not found." }
Write-Host "Maya:  $MayaSource"
Write-Host "CUDA:  $CudaRoot ($CudaVersion)"
Write-Host "MSVC:  $VsPath"
Write-Host "Cards: $Archs"

# ---- 2. build ---------------------------------------------------------------------
$EngineBuild = Join-Path $MayaSource "build-accuretta-pkg"
$VisionBuild = Join-Path $MayaSource "build-accuretta-pkg-vision"
if (-not $SkipBuild) {
    Step "building maya-ik2 and maya-vision (several minutes per card generation)"
    $nvcc = (Join-Path $CudaRoot "bin\nvcc.exe") -replace "\\", "/"
    $bat = Join-Path $env:TEMP "accuretta-maya-build.bat"
    @"
@echo off
call "$VcVars" x64 >nul || exit /b 1
cd /d "$MayaSource"
cmake -S . -B "$EngineBuild" -G Ninja -DCMAKE_BUILD_TYPE=Release -DSTRATA_ENABLE_CUDA=ON -DSTRATA_BUILD_TESTS=OFF -DSTRATA_PORTABLE=ON "-DCMAKE_CUDA_ARCHITECTURES=$Archs" "-DCUDAToolkit_ROOT=$CudaRoot" "-DCMAKE_CUDA_COMPILER=$nvcc" || exit /b 2
cmake --build "$EngineBuild" --target maya-ik2 || exit /b 3
cmake -S tools/vision -B "$VisionBuild" -G Ninja -DCMAKE_BUILD_TYPE=Release "-DLLAMA_DIR=$LlamaDir" -DSTRATA_PORTABLE=ON -DGGML_NATIVE=OFF -DGGML_OPENMP=OFF -DCMAKE_POLICY_DEFAULT_CMP0091=NEW -DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded || exit /b 4
cmake --build "$VisionBuild" --target maya-vision || exit /b 5
"@ | Set-Content -Encoding ascii $bat
    Invoke-Native { & cmd /c $bat }
    if ($LASTEXITCODE -ne 0) { Fail "build step $LASTEXITCODE failed (see the output above)." }
}
$EngineExe = Get-ChildItem $EngineBuild -Recurse -Filter "maya-ik2.exe" | Select-Object -First 1 -ExpandProperty FullName
$VisionExe = Get-ChildItem $VisionBuild -Recurse -Filter "maya-vision.exe" | Select-Object -First 1 -ExpandProperty FullName
if (-not $EngineExe) { Fail "maya-ik2.exe wasn't built." }
if (-not $VisionExe) { Fail "maya-vision.exe wasn't built." }

# ---- 3. stage ---------------------------------------------------------------------
Step "staging"
if (Test-Path $Work) { Remove-Item -Recurse -Force $Work }
$Stage = Join-Path $Work "maya"
New-Item -ItemType Directory -Force (Join-Path $Stage "build"), (Join-Path $Stage "build-vision\bin"), (Join-Path $Stage "pylib\common") | Out-Null
Copy-Item $EngineExe (Join-Path $Stage "build\maya-ik2.exe")
foreach ($dll in "cublas64_13.dll", "cublasLt64_13.dll") { Copy-Item (Join-Path $CudaDllDir $dll) (Join-Path $Stage "build\$dll") }
Copy-Item $VisionExe (Join-Path $Stage "build-vision\bin\maya-vision.exe")
# Maya's server and tools (no tests, fixtures or caches), its entry scripts, the licenses
Invoke-Native { & robocopy (Join-Path $MayaSource "serve") (Join-Path $Stage "serve") /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ fixtures /XF "test_*" "*fake*" | Out-Null }
Invoke-Native { & robocopy (Join-Path $MayaSource "tools") (Join-Path $Stage "tools") *.py *.jinja *.json *.md /E /NFL /NDL /NJH /NJS /NP /XD __pycache__ tests /XF "test_*" | Out-Null }
foreach ($f in "maya.py", "setup.py") { Copy-Item (Join-Path $MayaSource $f) (Join-Path $Stage $f) }
Copy-Item (Join-Path $MayaSource "LICENSE") (Join-Path $Stage "LICENSE-maya.txt")
Copy-Item (Join-Path $LlamaDir "LICENSE") (Join-Path $Stage "LICENSE-llama.cpp.txt")
@"
cublas64_13.dll and cublasLt64_13.dll (in build\) are NVIDIA CUDA Toolkit $CudaVersion runtime libraries,
redistributed unmodified under the NVIDIA CUDA Toolkit End User License Agreement, Attachment A
(Redistributable Software): https://docs.nvidia.com/cuda/eula/
"@ | Set-Content -Encoding utf8 (Join-Path $Stage "NOTICE-NVIDIA.txt")

# ---- 4. Python libraries ------------------------------------------------------------
Step "Python libraries (jinja2, markupsafe, regex)"
$Wheels = Join-Path $Work "wheels"
New-Item -ItemType Directory -Force $Wheels | Out-Null
$PyVersions = "3.10", "3.11", "3.12", "3.13", "3.14"
Invoke-Native { & python -m pip download --quiet --disable-pip-version-check --only-binary=:all: --no-deps --platform win_amd64 --python-version 3.14 --implementation cp -d (Join-Path $Wheels "common") jinja2 markupsafe }
if ($LASTEXITCODE -ne 0) { Fail "pip couldn't download jinja2 / markupsafe." }
foreach ($v in $PyVersions) {
    Invoke-Native { & python -m pip download --quiet --disable-pip-version-check --only-binary=:all: --no-deps --platform win_amd64 --python-version $v --implementation cp -d (Join-Path $Wheels ("cp" + $v.Replace(".", ""))) regex }
    if ($LASTEXITCODE -ne 0) { Fail "pip couldn't download regex for Python $v." }
}
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Expand-Wheel($whl, $dest) {
    $zip = [System.IO.Compression.ZipFile]::OpenRead($whl)
    try {
        foreach ($e in $zip.Entries) {
            if (-not $e.Name) { continue }
            $target = Join-Path $dest $e.FullName
            New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
        }
    } finally { $zip.Dispose() }
}
Get-ChildItem (Join-Path $Wheels "common") -Filter *.whl | ForEach-Object { Expand-Wheel $_.FullName (Join-Path $Stage "pylib\common") }
# markupsafe falls back to pure Python without its speed-up, so one copy serves every version
Get-ChildItem (Join-Path $Stage "pylib\common") -Recurse -Filter "_speedups*.pyd" | Remove-Item -Force
foreach ($v in $PyVersions) {
    $tag = "cp" + $v.Replace(".", "")
    Get-ChildItem (Join-Path $Wheels $tag) -Filter *.whl | ForEach-Object { Expand-Wheel $_.FullName (Join-Path $Stage "pylib\$tag") }
}

# ---- 5. self-test -----------------------------------------------------------------------
Step "self-test"
$usage = Invoke-Native { & (Join-Path $Stage "build\maya-ik2.exe") 2>&1 | Out-String }
if ($usage -notmatch "maya-ik2: usage") { Fail "the staged maya-ik2.exe didn't start: $usage" }
Write-Host "maya-ik2 starts: ok"
$pyTag = "cp" + (& python -c "import sys; print(f'{sys.version_info[0]}{sys.version_info[1]}')")
$env:PYTHONPATH = (Join-Path $Stage "pylib\common") + ";" + (Join-Path $Stage "pylib\$pyTag")
Invoke-Native { & python -I -c "import sys; sys.path[:0] = __import__('os').environ['PYTHONPATH'].split(';'); import jinja2, markupsafe, regex; print('python libraries: ok', jinja2.__version__, regex.__version__)" }
if ($LASTEXITCODE -ne 0) { Fail "the staged Python libraries don't import." }
if ($TestModel) {
    $tok = Join-Path $Work "tokenizer-test"
    Invoke-Native { & python (Join-Path $Stage "tools\strata_tokenizer.py") --gguf $TestModel --out $tok --chat-template (Join-Path $Stage "tools\ik2\chat_template.jinja") --check }
    if ($LASTEXITCODE -ne 0) { Fail "the tokenizer tool failed on $TestModel." }
    Remove-Item -Recurse -Force $tok
}
Remove-Item Env:PYTHONPATH

# ---- 6. manifest and zip ------------------------------------------------------------
Step "manifest and zip"
$Version = Get-Date -Format "yyyy.MM.dd.HHmm"
Push-Location $MayaSource
$Commit = (Invoke-Native { & git rev-parse --short HEAD }).Trim()
$Changed = [bool](Invoke-Native { & git status --porcelain --untracked-files=no })
Pop-Location
$files = [ordered]@{}
Get-ChildItem $Stage -Recurse -File | Sort-Object FullName | ForEach-Object {
    $rel = $_.FullName.Substring($Stage.Length + 1).Replace("\", "/")
    $files[$rel] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower()
}
$manifest = [ordered]@{
    name = "maya-ik2"
    version = $Version
    built = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    source = "https://github.com/alesha-pro/maya-inkling-mini"
    source_commit = $Commit
    source_changed = $Changed
    cuda = $CudaVersion
    compute_capabilities = @($Archs.Split(";") | ForEach-Object { "{0}.{1}" -f $_.Substring(0, $_.Length - 1), $_.Substring($_.Length - 1) })
    min_driver = 580
    min_vram_gb = 20
    engine = "build/maya-ik2.exe"
    vision = "build-vision/bin/maya-vision.exe"
    python_libs = "pylib/common"
    python_libs_per_version = "pylib/cp{0}{1}"
    python_versions = $PyVersions
    files = $files
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $Stage "accuretta-maya.json")
# (PowerShell 5.1 writes a BOM with utf8: rewrite it without one so any JSON reader takes it)
$json = Get-Content -Raw (Join-Path $Stage "accuretta-maya.json")
[System.IO.File]::WriteAllText((Join-Path $Stage "accuretta-maya.json"), $json, (New-Object System.Text.UTF8Encoding $false))
New-Item -ItemType Directory -Force $Dist | Out-Null
$ZipPath = Join-Path $Dist "Accuretta-Maya-$Version-win-x64.zip"
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
[System.IO.Compression.ZipFile]::CreateFromDirectory($Stage, $ZipPath, [System.IO.Compression.CompressionLevel]::Optimal, $true)
$sha = (Get-FileHash $ZipPath -Algorithm SHA256).Hash.ToLower()
"$sha  $(Split-Path $ZipPath -Leaf)" | Set-Content -Encoding ascii "$ZipPath.sha256"
$mb = [math]::Round((Get-Item $ZipPath).Length / 1MB)
Step "done"
Write-Host "$ZipPath ($mb MB)"
Write-Host "SHA-256 $sha"
Write-Host "Maya $Commit$(if ($Changed) { ' + local changes' }), CUDA $CudaVersion, cards $Archs"
