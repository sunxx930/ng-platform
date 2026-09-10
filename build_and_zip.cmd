@echo off
REM ============================================
REM NG-AI-Platform Windows one-click packager
REM Usage (on Windows, after installing Python 3.12):
REM   1. unzip this bundle
REM   2. double-click this file -> build exe -> zip it
REM Output: NG-AI-Platform-win.zip in this folder
REM ============================================
cd /d "%~dp0"
echo [1/2] Building exe (may take minutes on first run)...
call scripts\build_windows.bat
if errorlevel 1 (
  echo BUILD FAILED. See messages above.
  pause
  exit /b 1
)
echo [2/2] Zipping dist\NG-AI-Platform.exe ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\NG-AI-Platform.exe' -DestinationPath 'NG-AI-Platform-win.zip' -Force"
if exist NG-AI-Platform-win.zip (
  echo.
  echo ============================================
  echo DONE. Distribute: NG-AI-Platform-win.zip
  echo ============================================
) else (
  echo ZIP FAILED.
)
pause
