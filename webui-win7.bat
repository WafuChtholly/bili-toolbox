@echo off
title Bilibili Toolbox (Win7 / Python 3.8)
cd /d "%~dp0"

REM --- 选择 Python：优先 python38 目录，其次系统 PATH 中的 python ---
set "PY="
if exist "python38\python.exe" set "PY=python38\python.exe"
if not defined PY (
    where python >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [!] 未找到 Python。
    echo     Win7 请先安装 Python 3.8.x（3.9 及以上版本不支持 Win7，最后版本是 3.8.20）:
    echo     https://www.python.org/downloads/release/python-3820/
    echo     安装时勾选 "Add Python 3.8 to PATH"。
    pause
    exit /b 1
)

REM --- 检查 Python 版本必须为 3.8 ---
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)" >nul 2>&1
if errorlevel 1 (
    echo [!] 当前 Python 不是 3.8 版本。Win7 只能使用 Python 3.8.x，
    echo     请安装 Python 3.8 或将其放到 python38 目录后重试。
    pause
    exit /b 1
)

REM --- 首次运行：安装 Win7 兼容依赖 ---
%PY% -c "import flask, httpx, bilibili_api, fake_useragent" >nul 2>&1
if errorlevel 1 (
    echo.
    echo ========================================
    echo   首次运行 - 安装 Win7 兼容依赖
    echo ========================================
    echo.
    %PY% -m pip install --disable-pip-version-check -r requirements-win7.txt
    if errorlevel 1 (
        echo.
        echo [!] 依赖安装失败，请检查网络后重试，或手动执行:
        echo     %PY% -m pip install -r requirements-win7.txt
        pause
        exit /b 1
    )
)

echo ========================================
echo   Bilibili Toolbox (Win7 版)
echo   http://localhost:5678
echo   注: 浏览器模拟播放(Playwright)在 Win7 不可用
echo ========================================
echo.
%PY% app.py

pause
