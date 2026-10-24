<#
.SYNOPSIS
  Drive one PanelCAD feature on a Windows test PC with the real Windows Qt platform.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File run-windows.ps1 -Feature print-package -Run my-run
  Uses %USERPROFILE%\panelcad-test only: venv\, run\<run>\ (scratch, removed), evidence\<run>\ (kept).
  HKCU\Software\PanelCAD (QSettings) is exported before and restored after the run.
#>
param(
  [Parameter(Mandatory = $true)][string]$Feature,
  [Parameter(Mandatory = $true)][string]$Run,
  [string]$Bench = (Join-Path $env:USERPROFILE "panelcad-test")
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$Py = Join-Path $Bench "venv\Scripts\python.exe"
$Scratch = Join-Path $Bench "run\$Run"
$Evidence = Join-Path $Bench "evidence\$Run"
if (-not (Test-Path $Py)) { throw "venv missing: $Py" }
if (Test-Path $Scratch) { Remove-Item -Recurse -Force $Scratch }
foreach ($d in "home","home\AppData\Roaming","home\AppData\Local","data","config","work") { New-Item -ItemType Directory -Force (Join-Path $Scratch $d) | Out-Null }
New-Item -ItemType Directory -Force $Evidence | Out-Null

# QSettings lives in the registry on Windows: snapshot it.
$RegKey = "HKCU\Software\PanelCAD"
$RegBackup = Join-Path $Evidence "registry-before.reg"
cmd /c "reg query $RegKey >nul 2>&1"
$HadKey = ($LASTEXITCODE -eq 0)
$BackupOk = $false
if ($HadKey) {
  cmd /c "reg export $RegKey `"$RegBackup`" /y >nul 2>&1"
  $BackupOk = ($LASTEXITCODE -eq 0) -and (Test-Path $RegBackup) -and ((Get-Item $RegBackup).Length -gt 0)
  if (-not $BackupOk) {
    Remove-Item -Recurse -Force $Scratch -ErrorAction SilentlyContinue
    throw "reg export of $RegKey failed (exit $LASTEXITCODE); aborting before the run, registry untouched"
  }
}

$saved = @{}
$vars = @{
  HOME = (Join-Path $Scratch "home"); USERPROFILE = (Join-Path $Scratch "home")
  APPDATA = (Join-Path $Scratch "home\AppData\Roaming"); LOCALAPPDATA = (Join-Path $Scratch "home\AppData\Local")
  VERIFY_ROOT = $Bench; REPO = $Repo; RUN = $Run; SCRATCH = $Scratch; EVIDENCE = $Evidence
  PANELCAD_DATA_DIR = (Join-Path $Scratch "data"); PANELCAD_CONFIG = (Join-Path $Scratch "config")
  PANELCAD_BUG_REPORTS = "off"; PANELCAD_UPDATE_FEED = ""; PANELCAD_AI_LOOKUP = "off"; PANELCAD_WEB_ACCESS = "off"
  PANELCAD_NO_SPLASH = "1"; PANELCAD_NO_WELCOME = "1"; PANELCAD_AUTOSAVE_SECONDS = "0"
  PYTHONPATH = (Join-Path $Repo "src"); PYTHONIOENCODING = "utf-8"
  QT_QPA_PLATFORM = $null; PANELCAD_KEEP_OFFSCREEN = $null
  PANELCAD_AI_API_KEY = $null; PANELCAD_SLACK_BOT_TOKEN = $null; PANELCAD_BUG_WEBHOOK = $null; GROK_HOME = $null
}
foreach ($k in $vars.Keys) {
  $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process")
  [Environment]::SetEnvironmentVariable($k, $vars[$k], "Process")
}
$rc = 1
try {
  $log = Join-Path $Evidence "drive.log"
  "RUN=$Run REPO=$Repo EVIDENCE=$Evidence platform=windows(native)" | Tee-Object -FilePath $log
  $ErrorActionPreference = "Continue"
  & $Py -c "import panelcad,PySide6,pypdf,sys;from panelcad.version import __version__ as v;print('panelcad',v,panelcad.__file__);print('PySide6',PySide6.__version__,'pypdf',pypdf.__version__,'python',sys.version.split()[0])" 2>&1 |
    Tee-Object -FilePath (Join-Path $Evidence "doctor.txt")
  $loaded = (& $Py -c "import panelcad,os;print(os.path.realpath(panelcad.__file__))" 2>$null | Select-Object -Last 1)
  $want = (Join-Path $Repo "src\panelcad")
  if (-not $loaded -or -not $loaded.ToLower().StartsWith($want.ToLower())) {
    "ABORT: panelcad loads from '$loaded', not the bench tree $want" | Tee-Object -FilePath $log -Append
    throw "panelcad not loaded from bench tree"
  }
  "isolation: panelcad from bench tree; HOME/USERPROFILE/APPDATA/LOCALAPPDATA -> $(Join-Path $Scratch 'home')" | Tee-Object -FilePath $log -Append
  $p = Start-Process -FilePath $Py -ArgumentList @("`"$PSScriptRoot\drive.py`"", $Feature) -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $Scratch "out.txt") -RedirectStandardError (Join-Path $Scratch "err.txt")
  $null = $p.Handle  # cache handle so ExitCode is populated (PS 5.1 quirk)
  if (-not $p.WaitForExit(180000)) { Stop-Process -Id $p.Id -Force; "TIMEOUT after 180 s, killed pid $($p.Id)" | Tee-Object -FilePath $log -Append }
  $p.WaitForExit(); $rc = $p.ExitCode; if ($null -eq $rc) { $rc = 1 }
  Get-Content (Join-Path $Scratch "out.txt"), (Join-Path $Scratch "err.txt") -ErrorAction SilentlyContinue | Tee-Object -FilePath $log -Append
  "exit=$rc" | Tee-Object -FilePath $log -Append
}
finally {
  foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], "Process") }
  if ($HadKey -and $BackupOk) {
    cmd /c "reg delete $RegKey /f >nul 2>&1"
    cmd /c "reg import `"$RegBackup`" >nul 2>&1"
    if ($LASTEXITCODE -ne 0) {
      "registry: restore FAILED for $RegKey (reg import exit $LASTEXITCODE); backup kept at $RegBackup" | Tee-Object -FilePath (Join-Path $Evidence "drive.log") -Append
      $rc = 3
    } else {
      "registry: restored $RegKey from snapshot" | Tee-Object -FilePath (Join-Path $Evidence "drive.log") -Append
    }
  } elseif (-not $HadKey) {
    cmd /c "reg delete $RegKey /f >nul 2>&1"
    "registry: $RegKey did not exist before; removed what the run created" | Tee-Object -FilePath (Join-Path $Evidence "drive.log") -Append
  }
  Remove-Item -Recurse -Force $Scratch -ErrorAction SilentlyContinue
  $n = (Get-ChildItem $Evidence).Count
  "cleanup: removed $Scratch; evidence kept: $Evidence ($n files)" | Tee-Object -FilePath (Join-Path $Evidence "drive.log") -Append
}
exit $rc
