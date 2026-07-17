@echo off
REM Simple build script - uses Python directly instead of PyInstaller module
REM This is more reliable for pip-installed PyInstaller

setlocal enabledelayedexpansion

echo Ensuring PyInstaller is installed...
python -m pip install pyinstaller -q >nul 2>&1

echo Building Click2Connect Portable EXE...
python build.py

pause
