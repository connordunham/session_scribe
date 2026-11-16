# Session Scribe installer for Windows. Run once, while online:
#
#   Set-ExecutionPolicy -Scope Process Bypass
#   irm https://raw.githubusercontent.com/connordunham/session_scribe/main/install.ps1 | iex
#
# Safe to re-run: it skips what is already installed and updates the code.
# Other folder: run the downloaded file as `.\install.ps1 "D:\Scribe"`, or set
# $env:SCRIBE_INSTALL_DIR first. (A param() block would break the irm | iex form.)
$InstallDir = if ($args.Count -gt 0 -and $args[0]) { $args[0] } elseif ($env:SCRIBE_INSTALL_DIR) { $env:SCRIBE_INSTALL_DIR } else { "$env:USERPROFILE\Documents\DND SCRIBE" }

$ErrorActionPreference = 'Stop'
$Repo = 'https://github.com/connordunham/session_scribe'

function Have($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }

if (-not (Have winget)) { throw "winget not found. Install 'App Installer' from the Microsoft Store, then run this again." }

Step 'Installing prerequisites (skipping any already present)'
foreach ($p in @(@{cmd = 'git'; id = 'Git.Git' }, @{cmd = 'ffmpeg'; id = 'Gyan.FFmpeg' }, @{cmd = 'ollama'; id = 'Ollama.Ollama' })) {
    if (Have $p.cmd) { Write-Host "$($p.cmd): already installed" }
    else { winget install --id $p.id -e --accept-source-agreements --accept-package-agreements; Check "winget install $($p.id)"; Refresh-Path }
}

# Use the py launcher, not `python`: on a fresh Windows `python` is a
# Microsoft Store stub that opens the Store instead of running anything.
# Windows PowerShell 5.1 turns a native command's redirected stderr into an
# error record, which $ErrorActionPreference = 'Stop' makes fatal. A probe that
# is expected to fail ("No suitable Python runtime found") must not stop us.
function PyOk($v) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & py "-$v" -c "1" *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false } finally { $ErrorActionPreference = $old }
}
$PyVer = $null
if (Have py) { foreach ($v in '3.12', '3.11', '3.10') { if (PyOk $v) { $PyVer = "-$v"; break } } }
if (-not $PyVer) {
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements; Check 'winget install Python'
    Refresh-Path; $PyVer = '-3.12'
    if (-not (PyOk '3.12')) { throw 'Python 3.12 was installed but py cannot find it yet. Close PowerShell, open a new one, and run this again.' }
}
foreach ($c in 'git', 'ffmpeg', 'py') { if (-not (Have $c)) { throw "$c is installed but not on PATH yet. Close PowerShell, open a new one, and run this again." } }

Step "Getting the code into $InstallDir"
if (Test-Path "$InstallDir\.git") { git -C "$InstallDir" pull --ff-only; Check 'git pull' }
elseif ((Test-Path $InstallDir) -and (Get-ChildItem -Force $InstallDir | Where-Object { $_.Name -ne 'vault' })) {
    throw "$InstallDir already has files in it. Empty it (or pick another folder) and run this again."
}
else {
    $vaultBackup = $null
    if (Test-Path "$InstallDir\vault") { $vaultBackup = "$InstallDir.vault-tmp"; Move-Item "$InstallDir\vault" $vaultBackup }
    # Clone into the folder as-is: git accepts an existing empty folder, and
    # deleting it fails when the shell is sitting in it.
    git clone $Repo "$InstallDir"; Check 'git clone'
    if ($vaultBackup) { Move-Item $vaultBackup "$InstallDir\vault" }
}

Step 'Creating the Python environment and installing Session Scribe'
Push-Location $InstallDir
try {
    if (-not (Test-Path '.venv\Scripts\python.exe')) { & py $PyVer -m venv .venv; Check 'python -m venv' }
    & .venv\Scripts\python.exe -m pip install --upgrade pip --quiet; Check 'pip upgrade'
    & .venv\Scripts\python.exe -m pip install '.[stt]'; Check 'pip install'
}
finally { Pop-Location }

# Every launch uses this one vault, wherever it is started from.
$Vault = Join-Path $InstallDir 'vault'
New-Item -ItemType Directory -Force -Path "$Vault\campaign" | Out-Null
$env:SCRIBE_VAULT = $Vault

Step 'Making sure Ollama is running'
function OllamaUp { try { Invoke-RestMethod http://127.0.0.1:11434/api/tags -TimeoutSec 3 | Out-Null; $true } catch { $false } }
if (-not (OllamaUp)) {
    Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden
    for ($i = 0; $i -lt 20 -and -not (OllamaUp); $i++) { Start-Sleep 1 }
}
if (-not (OllamaUp)) { Write-Warning 'Ollama did not start. Open the Ollama app from the Start menu, then run: scribe setup' }

Step 'Downloading models (several GB; this takes a while)'
& "$InstallDir\.venv\Scripts\scribe.exe" setup
if ($LASTEXITCODE -ne 0) { Write-Warning "Model download hit an error (see above). Fix it, then re-run this script or run setup again from the shortcut's folder." }

Step 'Creating the launcher and desktop shortcut'
$launcher = Join-Path $InstallDir 'Start Session Scribe.cmd'
@"
@echo off
cd /d "%~dp0"
set "SCRIBE_VAULT=%~dp0vault"
start "" cmd /c "timeout /t 5 >nul & start http://localhost:8765"
".venv\Scripts\scribe.exe" run
pause
"@ | Set-Content -Encoding ASCII $launcher
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Session Scribe.lnk'))
$lnk.TargetPath = $launcher
$lnk.WorkingDirectory = $InstallDir
$lnk.Save()

Write-Host "`nDone." -ForegroundColor Green
Write-Host "1. Copy your Campaign Hub markdown export into: $Vault\campaign"
Write-Host "2. Double-click 'Session Scribe' on your desktop. The browser opens http://localhost:8765"
Write-Host "3. On that page: Test recording, then Run check. Everything should be green (also with Wi-Fi off)."
