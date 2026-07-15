@echo off
chcp 65001 >nul
cls

echo ========================================
echo       Bilibili BoFangliang Booster
echo ========================================
echo.

if exist .\python\python.exe (
    echo [OK] Found embedded Python
    set PYTHON_EXE=.\python\python.exe
    goto found_python
)

echo Error: Cannot find .\python\python.exe
echo.
echo Please make sure you extracted all files
echo The embedded Python should be in python\ folder
echo.
pause
exit /b 1

:found_python
echo.

set PATH=.\python;%PATH%

start http://127.0.0.1:5000

echo ========================================
echo Bilibili BoFangliang Booster Started
echo URL: http://127.0.0.1:5000
echo Press Ctrl+C to stop and exit
echo ========================================
echo.
%PYTHON_EXE% .\app.py

if errorlevel 1 (
    echo.
    echo Program exited with error code: %errorlevel%
    pause
)
