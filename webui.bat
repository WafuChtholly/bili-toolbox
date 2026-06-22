@echo off
title Bilibili Toolbox
cd /d "%~dp0"

if not exist "python\python.exe" (
    echo [!] python not found
    pause
    exit /b 1
)

REM --- Check if Playwright Chromium is installed ---
echo Checking Playwright Chromium ...
python\python.exe -c "import os; import playwright; d=os.path.dirname(playwright.__file__); browsers=os.path.join(d,'.local-browsers'); exit(0 if os.path.isdir(browsers) and any('chromium' in x for x in os.listdir(browsers)) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo ========================================
    echo   First run - Installing Chromium (~150MB)
    echo   This only happens once.
    echo ========================================
    echo.
    python\python.exe -m playwright install chromium
    if %errorlevel% neq 0 (
        echo.
        echo [!] Install failed. Please run manually:
        echo     python\python.exe -m playwright install chromium
        pause
        exit /b 1
    )
    echo.
    echo   Chromium installed!
    echo.
)

echo ========================================
echo   Bilibili Toolbox
echo   http://localhost:5678
echo ========================================
echo.
python\python.exe app.py

pause
