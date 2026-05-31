@echo off
setlocal

cd /d "%~dp0"
set "URL=http://127.0.0.1:8000"
set "SERVE=%~dp0serve.py"

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON=python"
    ) else (
        where py >nul 2>nul
        if not errorlevel 1 (
            set "PYTHON=py -3"
        ) else (
            echo Python was not found. Please install Python or add it to PATH.
            pause
            exit /b 1
        )
    )
)

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%URL%'"
echo Starting LOF iNAV at %URL%
%PYTHON% "%SERVE%"

echo.
echo Server stopped.
pause
