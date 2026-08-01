@echo off
setlocal
cd /d "%~dp0"
title Anvil Server Manager - Preview Demo

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
pip install -q -r requirements.txt

echo.
echo Starting Anvil Server Manager in PREVIEW MODE.
echo A demo fleet (two servers) loads automatically. No real system commands
echo run (no docker/systemctl/restic) - Crafty/Docker/Cockpit update checks,
echo backups, restores, and Discord notifications all stream fake output so
echo you can click through the whole dashboard safely.
echo No access token is required on this machine, since none has been set up.
echo Close this window to stop the server.
echo.

set ANVIL_SERVER_MANAGER_PREVIEW=1
start "" cmd /c "call .venv\Scripts\activate.bat && set ANVIL_SERVER_MANAGER_PREVIEW=1 && python app.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:6161

pause
