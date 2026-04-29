@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Accuretta

echo.
echo ============================================================
echo   Accuretta - local llama.cpp bridge + IDE
echo ============================================================
echo.

REM ---- find python --------------------------------------------
set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [error] Python is not installed or not on PATH.
    echo.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo During install, tick "Add Python to PATH".
    echo Then run start.bat again.
    echo.
    pause
    exit /b 1
)
echo Python: %PYEXE%

REM ---- create / reuse virtual env -----------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    %PYEXE% -m venv .venv
    if errorlevel 1 (
        echo [error] failed to create venv. is the venv module available?
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"

REM ---- install / upgrade deps when requirements changed -------
set "REQ_HASH_FILE=.venv\.req-hash"
set "CURRENT_HASH="
for /f %%H in ('certutil -hashfile requirements.txt SHA256 ^| findstr /v ":"') do (
    if not defined CURRENT_HASH set "CURRENT_HASH=%%H"
)
set "STORED_HASH="
if exist "%REQ_HASH_FILE%" set /p STORED_HASH=<"%REQ_HASH_FILE%"

if not "%CURRENT_HASH%"=="%STORED_HASH%" (
    echo Installing Python dependencies ...
    "%VENV_PY%" -m pip install --upgrade pip >nul 2>&1
    "%VENV_PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [error] pip install failed.
        pause
        exit /b 1
    )
    echo %CURRENT_HASH%>"%REQ_HASH_FILE%"
)

REM ---- free port 8787 -----------------------------------------
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8787" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
)

echo.
echo Starting Accuretta on http://127.0.0.1:8787 ...
echo.
echo Local network addresses (use one of these from your phone or
echo another device on the same Wi-Fi or Tailscale):
ipconfig | findstr /c:"IPv4"
echo.
echo If this is your first run:
echo   1. The browser will open automatically.
echo   2. Open Settings (gear icon) and pick your Models folder.
echo   3. Pick a .gguf model from the dropdown to load it.
echo.
echo See README.md for where to download llama-server.exe and a model.
echo Press Ctrl+C to stop.
echo.

"%VENV_PY%" -u bridge.py

echo.
echo Bridge stopped.
pause
exit /b 0
