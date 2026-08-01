@echo off
setlocal
cd /d "%~dp0"
title Anvil Mod Manager

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
echo Starting Anvil Mod Manager...
echo Close this window to stop the server.
echo.

start "" cmd /c "call .venv\Scripts\activate.bat && python app.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:5151

pause
