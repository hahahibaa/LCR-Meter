@echo off
title PSM Temp Logger
setlocal EnableExtensions

REM Always run from the folder this .bat file lives in
cd /d "%~dp0"

REM ---- find Python ----
set "PY="
where py >nul 2>nul
if not errorlevel 1 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
    )
)
if not defined PY (
    echo.
    echo [ERROR] Python is not installed or not on PATH.
    echo         Install Python 3.9+ from https://www.python.org/downloads/
    echo         and tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)
echo Using Python: %PY%

REM ---- install / update libraries (quiet, ignore "already satisfied") ----
echo.
echo Installing / updating Python libraries...
%PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo [WARN] pip install reported an error. Trying to launch anyway...
)

REM ---- launch the app ----
echo.
echo Launching PSM Temp Logger...
%PY% app\main.py
set "RC=%ERRORLEVEL%"

REM ---- always pause so the user can see any error ----
echo.
echo App exited with code %RC%.
pause
endlocal
exit /b %RC%
