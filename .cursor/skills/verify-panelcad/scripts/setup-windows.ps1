<#
.SYNOPSIS
  One-time bench setup inside %USERPROFILE%\panelcad-test only. Never touches the main install.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File setup-windows.ps1 -Zip $env:USERPROFILE\panelcad-test\src-<rev>.zip
#>
param([Parameter(Mandatory = $true)][string]$Zip, [string]$Rev = "", [string]$Python = "py",
      [string]$Bench = (Join-Path $env:USERPROFILE "panelcad-test"))
$ErrorActionPreference = "Stop"
# Folder name: -Rev <rev> -> src-<rev>; otherwise the zips base name (src-abc123.zip -> src-abc123).
$Name = if ($Rev) { "src-$Rev" } else { [IO.Path]::GetFileNameWithoutExtension($Zip) }
if (-not $Name -or $Name -match '[\\/:]') { throw "bad folder name '$Name'" }
$Src = Join-Path $Bench $Name
if (Test-Path $Src) { Remove-Item -Recurse -Force $Src }
Expand-Archive -Path $Zip -DestinationPath $Src
$Venv = Join-Path $Bench "venv"
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
  if ($Python -eq "py") { & py -3 -m venv $Venv } else { & $Python -m venv $Venv }
}
$Py = Join-Path $Venv "Scripts\python.exe"
$ErrorActionPreference = "Continue"
& $Py -m pip install --disable-pip-version-check -q -e $Src pypdf 2>&1 | Select-Object -Last 5
if ($LASTEXITCODE -ne 0) { throw "pip install failed ($LASTEXITCODE)" }
& $Py -c "import panelcad,PySide6,pypdf;print('ok',panelcad.__file__,PySide6.__version__,pypdf.__version__)"
