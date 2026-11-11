# Windows setup for PanelCAD
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .\.venv\Scripts\python.exe)) {
    python -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# See Install-PanelCAD.ps1: keep cv2 headless after paddleocr's contrib pin.
.\.venv\Scripts\python.exe -m pip install --force-reinstall --no-deps "opencv-python-headless>=4.9,<5"
Write-Host "OK. Activate with:  .\.venv\Scripts\Activate.ps1"
Write-Host "Then:  .\scripts\check.ps1"
Write-Host "Then:  panelcad gui"
