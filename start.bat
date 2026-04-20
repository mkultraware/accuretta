@echo off
cd /d "%~dp0"
title accuretta

echo.
echo ============================================================
echo   accuretta - local LLM bridge + IDE
echo ============================================================
echo.

REM ---- kill anything holding port 8787 -------------------------
echo freeing port 8787 ...
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr ":8787" ^| findstr "LISTENING"') do (
    echo   killing pid %%p
    taskkill /F /PID %%p >nul 2>&1
)

REM ---- kill any stale bridge.py python/pythonw processes -------
echo killing stale bridge.py processes ...
for /f "skip=1 tokens=1 delims=," %%p in ('wmic process where "CommandLine like '%%bridge.py%%' and not CommandLine like '%%wmic%%'" get ProcessId /format:csv 2^>nul') do (
    if not "%%p"=="" if not "%%p"=="Node" (
        taskkill /F /PID %%p >nul 2>&1
    )
)

REM ---- find python ---------------------------------------------
set "PYEXE="
where py >nul 2>&1
if not errorlevel 1 set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>&1
    if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
    echo [error] python not found on PATH. install Python 3.10+ from python.org.
    echo.
    pause
    exit /b 1
)
echo python: %PYEXE%
echo.

echo network addresses (use one of these from your phone):
ipconfig | findstr /c:"IPv4"
echo.
echo the browser will open automatically once the bridge is ready.
echo ctrl+c to stop.
echo.

REM bridge starts ollama, binds 8787, opens browser, then serves.
%PYEXE% -u bridge.py

echo.
echo server stopped.
pause
