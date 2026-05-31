@echo off
setlocal

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_server.ps1" -ProjectRoot "%~dp0" -Port 8000

echo.
pause
