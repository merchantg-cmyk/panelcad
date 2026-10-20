# Run the full PanelCAD pytest suite headless (Qt offscreen).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:QT_QPA_PLATFORM = if ($env:QT_QPA_PLATFORM) { $env:QT_QPA_PLATFORM } else { "offscreen" }
$env:PYDANTIC_AI_NO_BANNER = if ($env:PYDANTIC_AI_NO_BANNER) { $env:PYDANTIC_AI_NO_BANNER } else { "1" }

$candidates = @(
    (Join-Path (Get-Location) ".venv\Scripts\python.exe"),
    (Join-Path $env:USERPROFILE ".venvs\panelcad\Scripts\python.exe")
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) { throw "python.exe not found. Create a venv first (scripts\setup.ps1)." }

Write-Host "Using $py"
& $py -m pytest -q @args
exit $LASTEXITCODE
