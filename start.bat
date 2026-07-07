@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title accuretta

REM ============================================================
REM   accuretta launcher — opens the desktop app (no browser,
REM   no lingering console). Falls back to browser mode if the
REM   desktop UI dependency can't be installed.
REM ============================================================

REM ---- free port 8787 (kill any stale bridge) ------------------
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8787" ^| findstr "LISTENING"') do taskkill /F /PID %%p >nul 2>&1

REM ---- find console python (for pip) --------------------------
set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE ( where python >nul 2>&1 && set "PYEXE=python" )
if not defined PYEXE (
    echo [error] Python not found on PATH. Install Python 3.10+ from python.org.
    pause
    exit /b 1
)

REM ---- ensure the desktop UI dependency (one-time) ------------
%PYEXE% -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo Installing the desktop app dependency ^(one-time^)...
    %PYEXE% -m pip install --quiet --disable-pip-version-check pywebview
)

REM ---- launch the desktop app if we can, else browser mode ----
%PYEXE% -c "import webview" >nul 2>&1
if errorlevel 1 (
    echo Desktop UI unavailable — falling back to browser mode.
    set ACCURETTA_BROWSER=edge
    %PYEXE% -u bridge.py
    echo.
    echo bridge stopped.
    pause
    exit /b 0
)

REM ---- windowed python so there's no console window -----------
set "PYWEXE="
where pythonw >nul 2>&1 && set "PYWEXE=pythonw"
if not defined PYWEXE ( where pyw >nul 2>&1 && set "PYWEXE=pyw -3" )
if not defined PYWEXE set "PYWEXE=%PYEXE%"

REM launch detached; this console closes immediately.
start "" %PYWEXE% accuretta_app.py
exit /b 0
