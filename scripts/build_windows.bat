@echo off
REM ============================================
REM NG-AI-Platform Windows build script (onefile)
REM Usage: double-click this file, or run in cmd:
REM        build_windows.bat
REM Output: dist\NG-AI-Platform.exe
REM Prereq: Python 3.12 installed with "Add to PATH"
REM ============================================
cd /d "%~dp0\.."

echo [1/4] Checking Python...
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.12 and tick "Add to PATH".
    echo         Download: https://www.python.org/downloads/
    pause
    exit /b 1
)
python --version

echo [2/4] Creating venv and installing dependencies (first run may be slow)...
if not exist .venv (
    python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check network and retry.
    pause
    exit /b 1
)

echo [3/4] Installing PyInstaller...
python -m pip install pyinstaller
if errorlevel 1 (
    echo [ERROR] PyInstaller install failed.
    pause
    exit /b 1
)

echo [4/4] Building onefile exe...
pyinstaller ng-platform.spec --noconfirm
if errorlevel 1 (
    echo [ERROR] Build failed.
    pause
    exit /b 1
)

echo.
echo ============================================
echo BUILD OK. Output: dist\NG-AI-Platform.exe
echo Double-click it to run (opens browser at http://127.0.0.1:8001)
echo ============================================
pause
