@echo off
REM Double-click this file after extracting PanelCAD-<version>-setup.zip.
REM It installs or upgrades PanelCAD for the current user and opens the window.
REM
REM Unattended (no pause, and the drawing window is not opened):
REM   Install-PanelCAD.cmd /quiet
REM   set PANELCAD_INSTALL_QUIET=1
title PanelCAD Setup
cd /d "%~dp0"

set "QUIET="
if /I "%~1"=="/quiet" set "QUIET=1"
if /I "%~1"=="-quiet" set "QUIET=1"
if /I "%~1"=="-Quiet" set "QUIET=1"
if "%PANELCAD_INSTALL_QUIET%"=="1" set "QUIET=1"

if defined QUIET (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Install-PanelCAD.ps1" -Quiet
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Install-PanelCAD.ps1"
)
set "PS_EXIT=%ERRORLEVEL%"

echo.
if not "%PS_EXIT%"=="0" (
  echo Install failed. Leave this window open and read the message above.
  if not defined QUIET pause
  exit /b %PS_EXIT%
)
if defined QUIET (
  echo Installed. Quiet mode: the drawing window was not opened.
  echo Next time, double-click PanelCAD on the desktop.
) else (
  echo PanelCAD should be open. You can close this window.
  pause
)
exit /b 0
