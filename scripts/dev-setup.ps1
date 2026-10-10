# One-command setup for Windows.
#
#   .\scripts\dev-setup.ps1              # create/use .\.venv
#   .\scripts\dev-setup.ps1 --here       # install into the ACTIVE env instead
#   $env:PYTHON="C:\Python312\python.exe"; .\scripts\dev-setup.ps1
#
# Requires PowerShell 5.1+ (ships with Windows 10/11).
# If execution policy blocks this, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
param(
    [switch]$Here
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ---- cd to repo root -------------------------------------------------------
$REPO = Split-Path -Parent $PSScriptRoot
Set-Location $REPO
$VENV = Join-Path $REPO ".venv"

# ---- resolve a base interpreter --------------------------------------------
$PY = if ($env:PYTHON) { $env:PYTHON } else { $null }

if (-not $PY) {
    foreach ($candidate in @("python", "python3", "py")) {
        try {
            $found = (Get-Command $candidate -ErrorAction Stop).Source
            # quick sanity: must actually be Python
            & $found -c "import sys" 2>$null
            if ($LASTEXITCODE -eq 0) { $PY = $found; break }
        } catch { }
    }
}

if (-not $PY) {
    Write-Error @"
error: could not find a Python executable.
Install Python 3.10+ from https://python.org and make sure it is on PATH,
or set `$env:PYTHON to its full path before running this script.
"@
    exit 1
}

$pyVersion = & $PY -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
$pyExe     = & $PY -c "import sys; print(sys.executable)"
Write-Host "==> base interpreter"
Write-Host "    $pyExe  ($pyVersion)"

# ---- pick the target environment -------------------------------------------
if ($Here) {
    $TARGET = $PY
    Write-Host "==> installing into the ACTIVE environment (--here)"
} elseif ($env:VIRTUAL_ENV -and (Test-Path "$env:VIRTUAL_ENV\Scripts\python.exe")) {
    $TARGET = "$env:VIRTUAL_ENV\Scripts\python.exe"
    Write-Host "==> using the already-active virtualenv"
    Write-Host "    $env:VIRTUAL_ENV"
} else {
    $venvPython = Join-Path $VENV "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "==> creating virtualenv at .venv"
        & $PY -m venv $VENV
        if ($LASTEXITCODE -ne 0) {
            Write-Error "error: could not create a virtualenv. Make sure python3-venv is available."
            exit 1
        }
    } else {
        Write-Host "==> reusing existing .venv"
    }
    $TARGET = $venvPython
}

$targetExe = & $TARGET -c "import sys; print(sys.executable)"
Write-Host "==> target: $targetExe"

# ---- upgrade pip quietly ---------------------------------------------------
& $TARGET -m pip install -q --upgrade pip 2>$null

# ---- check for shadowing hypothesise package ------------------------------------
Write-Host "==> checking for a shadowing package"
$SITE = & $TARGET -c "import site; print(site.getsitepackages()[0])" 2>$null
if ($SITE -and (Test-Path "$SITE\hypothesise") -and -not (Test-Path "$SITE\hypothesise\__main__.py")) {
    Write-Error @"
error: a different package named 'hypothesise' is installed and will shadow this one.

    $SITE\hypothesise

That is a stale or foreign copy, not this repo. Remove it:

    Remove-Item -Recurse -Force "$SITE\hypothesise"
    Get-Item "$SITE\hypothesise-*.dist-info" | Remove-Item -Recurse -Force
    .\scripts\dev-setup.ps1
"@
    exit 1
}
Write-Host "    none found"

# ---- install all editable packages -----------------------------------------
Write-Host "==> installing hypothesise packages (editable)"
$packages = @(
    "packages\core", "packages\store", "packages\proxy",
    "packages\api",  "packages\cli",
    "packages\adapters\langgraph"
)
$missing = @()
foreach ($m in $packages) {
    if (-not (Test-Path "$m\pyproject.toml")) { $missing += $m }
}
if ($missing.Count -gt 0) {
    Write-Host "error: these packages have no pyproject.toml and cannot be installed:"
    $missing | ForEach-Object { Write-Host "    $_" }
    exit 1
}

$installArgs = @()
foreach ($m in $packages) { $installArgs += "-e"; $installArgs += $m }
& $TARGET -m pip install @installArgs
if ($LASTEXITCODE -ne 0) {
    Write-Error @"

error: pip refused to install. Read its output above for the real cause.

Common causes on Windows:
  * Long path errors — enable long paths:
        New-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" ``
            -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force
  * Missing build tools for a native extension — install from:
        https://visualstudio.microsoft.com/visual-cpp-build-tools/
"@
    exit 1
}

# ---- dev tools -------------------------------------------------------------
Write-Host "==> installing dev tools"
& $TARGET -m pip install -q pytest pytest-asyncio httpx ruff mypy pyyaml

# ---- verify from outside the repo ------------------------------------------
Write-Host "==> verifying"
$verifyScript = @"
import hypothesise, hypothesise_core, hypothesise_store, hypothesise_proxy, hypothesise_api
print('    hypothesise      ', hypothesise.__version__)
print('    hypothesise_core ', hypothesise_core.__version__)
print('    hypothesise_store  ok')
print('    hypothesise_proxy', hypothesise_proxy.__version__)
print('    hypothesise_api    ok')
"@

$tmpDir = [System.IO.Path]::GetTempPath()
Push-Location $tmpDir
try {
    & $TARGET -c $verifyScript
    if ($LASTEXITCODE -ne 0) {
        Write-Host "!! verification failed. Run: $TARGET scripts\doctor.py"
        exit 1
    }
} finally {
    Pop-Location
}

# ---- tell the user what to run ---------------------------------------------
$VENV_BIN = Split-Path $TARGET
Write-Host @"

==> done.

Activate the environment:

    $VENV_BIN\Activate.ps1

Then:

    pytest
    python -m hypothesise_api --demo
    python scripts\replay.py --all

Or run without activating, using the full path:

    $TARGET -m pytest
    $TARGET -m hypothesise_api --demo
    hypothesise api

"@
