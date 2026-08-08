@echo off
title bili-toolbox - Chromium 国内镜像安装

echo ==================================================
echo   bili-toolbox - Chromium 国内镜像手动安装脚本
echo ==================================================
echo.

rem ===== 配置国内镜像 (npmmirror, 淘宝源) =====
set PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
rem 浏览器安装到 playwright 包内 (.local-browsers), 与程序检测位置保持一致
set PLAYWRIGHT_BROWSERS_PATH=0

rem ===== 查找装有 playwright 的 Python (可用第一个参数指定自定义 python.exe 路径) =====
echo [1/2] 检测 Python 环境 (需已安装 Playwright) ...
set PYCMD=
if "%~1" neq "" (set CANDIDATES="%~1") else (set CANDIDATES=python py)
for %%P in (%CANDIDATES%) do (
    if not defined PYCMD (
        %%P -c "import playwright" >nul 2>nul
        if not errorlevel 1 set PYCMD=%%P
    )
)
if defined PYCMD goto found_python
echo [ERROR] 未找到装有 playwright 的 Python 环境.
echo     已安装过的话, 请指定 python 路径运行: install_chromium.bat "python.exe的完整路径"
echo     未安装请先执行: pip install playwright -i https://mirrors.aliyun.com/pypi/simple
pause
exit /b 1

:found_python
echo       使用 Python: %PYCMD%
echo.

echo [2/2] 开始下载 Chromium (约 170MB, 走国内镜像) ...
echo       镜像地址: %PLAYWRIGHT_DOWNLOAD_HOST%
echo       安装位置: playwright 包内 .local-browsers 目录
echo       下载过程会实时显示进度, 请耐心等待.
echo.
%PYCMD% -m playwright install chromium
if errorlevel 1 (
    echo.
    echo [ERROR] Chromium 下载/安装失败, 请检查网络后重试.
    pause
    exit /b 1
)

echo.
echo ==================================================
echo   Chromium 安装完成! 现在可以回到程序启动播放任务.
echo ==================================================
pause
