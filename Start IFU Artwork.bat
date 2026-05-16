@echo off
REM ===========================================================
REM  IFU Artwork launcher
REM  - Starts the local server (Python + Flask + OCCT)
REM  - Opens http://localhost:5000 in the default browser as
REM    soon as the server reports healthy
REM  - Closing this window stops the server
REM ===========================================================
cd /d "%~dp0"
title IFU Artwork (close this window to stop)

REM Sanity check: Python on PATH
where python > nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python is not on your PATH.
    echo   Install Python 3.10 or newer from https://www.python.org/
    echo   and tick "Add to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting IFU Artwork server...
echo.
echo   First boot loads 3 STEP files into memory; that takes
echo   1-3 minutes (Contesa is the slow one).  The browser
echo   will open automatically once the server is ready.
echo.
echo   To stop the tool, just close this window.
echo.

REM Fire off a helper that polls healthz and opens the browser
REM as soon as the server responds.  /b = no new window.
start "" /b cmd /c "%~dp0_open_when_ready.bat"

REM Server runs in the foreground in THIS window so closing the
REM window kills it cleanly.
python serve.py

REM If serve.py crashes (or the user hits Ctrl+C), give them a
REM chance to read the error instead of disappearing.
echo.
echo Server stopped.  Press any key to close this window.
pause > nul
