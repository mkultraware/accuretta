@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title accuretta

REM ============================================================
REM   accuretta launcher — opens the desktop app (no browser,
REM   no lingering console). Falls back to browser mode if the
REM   desktop UI dependency can't be installed.
REM ============================================================

REM The bridge reserves its port before loading a model. Never kill a process
REM merely because it listens on a port used by Accuretta or llama.cpp.

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
REM Pick a pythonw that can ACTUALLY import webview: PATH may contain
REM other pythons (e.g. a venv whose pythonw has no pywebview), and
REM launching the app with one of those crashes it instantly. Use the
REM full exe path so `start` spawns it directly (the Python launcher
REM stub can silently fail under `start`, and a cmd /c wrapper would
REM flash a console window — both are avoided by using the real exe).
set "PYWINEXE="
for /f "delims=" %%p in ('where pythonw 2^>nul') do if not defined PYWINEXE set "PYWINEXE=%%p"
if defined PYWINEXE (
    "%PYWINEXE%" -c "import webview" >nul 2>&1
    if errorlevel 1 set "PYWINEXE="
)
if not defined PYWINEXE (
    for /f "delims=" %%p in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PYBASE=%%p"
    if defined PYBASE (
        set "PYWINEXE=!PYBASE:~0,-4!w.exe"
        "!PYWINEXE!" -c "import webview" >nul 2>&1
        if errorlevel 1 set "PYWINEXE="
    )
)
if not defined PYWINEXE set "PYWINEXE=%PYEXE%"

REM launch detached; this console closes immediately.
if "%PYWINEXE%"=="%PYEXE%" (
    REM console-python fallback (no windowed python available): run
    REM through cmd /c so the launch can't silently fail.
    start "" cmd /c "%PYWINEXE% accuretta_app.py 2> bridge_error.log"
) else (
    start "" "%PYWINEXE%" accuretta_app.py
)
exit /b 0
