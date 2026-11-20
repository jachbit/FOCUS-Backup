@echo off
title F.O.C.U.S. Backup - Build the .exe
cd /d "%~dp0"

echo.
echo ========================================
echo   F.O.C.U.S. Backup - Build FOCUS-Backup.exe
echo   Jachbit 2026
echo ========================================
echo.

python -c "import PyInstaller, flask, webview" >nul 2>nul
if errorlevel 1 (
    echo  Installing the build tools - one time only...
    python -m pip install -r requirements.txt pyinstaller
    echo.
)

python -m PyInstaller --onefile --noconsole --clean --noconfirm --name FOCUS-Backup --icon assets\icon.ico --add-data "templates;templates" server.py
if errorlevel 1 (
    echo.
    echo  BUILD FAILED - see the messages above.
    pause
    exit /b 1
)

echo.
echo  Done: dist\FOCUS-Backup.exe
echo  Upload it to the GitHub release - do not commit it to the repo.
echo.
pause
