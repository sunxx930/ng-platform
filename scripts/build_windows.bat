@echo off
REM ============================================================
REM NG-AI-Platform Windows 一键打包脚本
REM 用法: 双击本文件，或在 PowerShell/cmd 里运行
REM        build_windows.bat
REM 产物: dist\NG-AI-Platform.exe
REM 前提: 已安装 Python 3.12（安装时勾选 Add to PATH）
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0\.."

echo [1/4] 检查 Python...
where python >nul 2>nul
if errorlevel 1 (
    echo ❌ 未找到 Python。请先安装 Python 3.12 并勾选 "Add to PATH"。
    echo    下载: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version

echo [2/4] 创建虚拟环境并安装依赖（首次较慢，请耐心）...
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo ❌ 依赖安装失败。请检查网络后重试。
    pause
    exit /b 1
)

echo [3/4] 安装 PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 (
    echo ❌ PyInstaller 安装失败。
    pause
    exit /b 1
)

echo [4/4] 开始打包...
pyinstaller ng-platform.spec --noconfirm
if errorlevel 1 (
    echo ❌ 打包失败。
    pause
    exit /b 1
)

echo.
echo ✅ 打包完成！产物: dist\NG-AI-Platform.exe
echo    双击即可运行（自动打开浏览器到 http://127.0.0.1:8001）
pause
