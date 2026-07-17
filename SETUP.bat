@echo off
REM Quick setup for Click2Connect portable build
REM Just run this, then run BUILD.bat

echo Installing build dependencies...
pip install pyinstaller -q

if errorlevel 1 (
    echo Error: Failed to install PyInstaller
    echo Try: pip install pyinstaller
    pause
    exit /b 1
)

echo Done! Now run: BUILD.bat
pause
