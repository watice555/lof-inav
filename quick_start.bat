@echo off
setlocal

cd /d "%~dp0"
set "URL=http://127.0.0.1:8000"
set "DB_PATH=data\lof_inav.sqlite3"
set "VENV_PY=.venv\Scripts\python.exe"

if "%~1"=="--help" goto help
if "%~1"=="-h" goto help

if not exist ".venv\" (
    call :find_python || goto failed
    echo Creating virtual environment...
    %PYTHON% -m venv .venv || goto failed
)

if not exist "%VENV_PY%" (
    echo Virtual environment is incomplete. Delete .venv and run this script again.
    goto failed
)

echo Installing dependencies...
"%VENV_PY%" -m pip install -r requirements.txt || goto failed

if "%~1"=="--rebuild" (
    echo Rebuilding current valuation data...
    "%VENV_PY%" build.py --current-only || goto failed
) else (
    if not exist "%DB_PATH%" (
        echo First run: building current valuation data...
        "%VENV_PY%" build.py --current-only || goto failed
    ) else (
        echo Local database already exists. Skipping initial build.
    )
)

echo Starting LOF iNAV at %URL%
start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process '%URL%'"
"%VENV_PY%" serve.py

echo.
echo Server stopped.
pause
exit /b 0

:find_python
where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=python"
    exit /b 0
)
where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON=py -3"
    exit /b 0
)
echo Python was not found. Please install Python 3.10+ or add it to PATH.
exit /b 1

:help
echo Usage:
echo   quick_start.bat
echo   quick_start.bat --rebuild
echo.
echo The default command creates .venv, installs dependencies, runs a quick
echo current-only build when data\lof_inav.sqlite3 is missing, then starts
echo the local website.
echo.
echo --rebuild runs the quick current-only build even when the database exists.
exit /b 0

:failed
echo.
echo quick_start failed. Check the error above.
pause
exit /b 1
