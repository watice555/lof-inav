@echo off
setlocal

cd /d "%~dp0"
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

set "LOF_INAV_OPEN_BROWSER=1"
echo Starting LOF iNAV...
%PYTHON% "%SERVE%"

echo.
echo Server stopped.
pause
