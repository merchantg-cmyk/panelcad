@echo off
set QT_QPA_PLATFORM=
cd /d "%~dp0.."
REM pythonw/panelcad.exe failed with 0x800700e8 on one PC — use python.exe
set PY=%USERPROFILE%\.venvs\panelcad\Scripts\python.exe
if not exist "%PY%" (
  echo PanelCAD python not found: %PY%
  pause
  exit /b 1
)
"%PY%" -c "from panelcad.gui.app import run_gui; run_gui(r'examples\dol_starter.pcad.yaml')"
