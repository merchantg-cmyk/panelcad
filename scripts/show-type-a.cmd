@echo off
set QT_QPA_PLATFORM=
cd /d "%~dp0.."
set PY=%USERPROFILE%\.venvs\panelcad\Scripts\python.exe
if not exist "%PY%" (
  echo PanelCAD python not found: %PY%
  pause
  exit /b 1
)
set JOB=%~dp0..\.sharpen\ladder-elementary.pcad.yaml
if not exist "%JOB%" set JOB=%~dp0..\examples\dol_starter.pcad.yaml
start "PanelCAD" "%PY%" "%~dp0open_gui.py" "%JOB%"
