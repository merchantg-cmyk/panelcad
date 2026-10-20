# Windows setup for PanelCAD
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Write-Host "OK. Activate with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then:  .\scripts\check.ps1"
Write-Host "Then:  panelcad gui"
