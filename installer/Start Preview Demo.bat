@echo off
setlocal
cd /d "%~dp0"
title Anvil Server Installer - Preview Demo

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo Python was not found on your PATH.
    echo Install it from https://www.python.org/downloads and make sure you tick
    echo "Add python.exe to PATH" during setup, then double-click this file again.
    echo.
    pause
    exit /b 1
)

if not exist ".venv" (
    echo Setting up ^(first run only, this takes a minute^)...
    python -m venv .venv
)

call ".venv\Scripts\activate.bat"
python -m pip install -q --upgrade pip
pip install -q flask

echo.
echo Starting Anvil Server Installer in PREVIEW MODE.
echo No real system commands run (no apt/ufw/docker/systemctl) — every step
echo streams fake output so you can click through the whole wizard safely,
echo including the optional "Install Anvil Mod Manager" step.
echo Close this window to stop the server.
echo.

set ANVIL_INSTALLER_PREVIEW=1
start "" cmd /c "call .venv\Scripts\activate.bat && set ANVIL_INSTALLER_PREVIEW=1 && python app.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:8090

pause
