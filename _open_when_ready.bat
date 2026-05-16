@echo off
REM Polls /api/healthz every 3 seconds; opens the browser once the
REM server responds.  Spawned in the background by Start IFU Artwork.bat.

:WAIT
timeout /t 3 /nobreak > nul
curl -s -o nul --max-time 1 http://127.0.0.1:5000/api/healthz
if errorlevel 1 goto WAIT

REM Server's up.  Open the user's default browser.
start "" "http://127.0.0.1:5000/"
exit
