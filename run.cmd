@echo off
rem Double-click to start AutoPoker (no bots). Close this window or run scripts\stop.ps1 to stop everything.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\run.ps1" %*
echo.
pause
