@echo off
REM Click2Connect - Build EXE Installer
REM This script installs dependencies and builds the portable EXE

setlocal enabledelayedexpansion

echo.
echo =========================================================
echo   Click2Connect - Build Setup & EXE Generation
echo =========================================================
echo.

REM Check if Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH
    echo Please install Python 3.10+ and add to PATH
    pause
    exit /b 1
)

echo [1/5] Python found
python --version

echo.
echo [2/5] Ensuring PyInstaller is installed...
python -m pip install pyinstaller -q
if errorlevel 1 (
    echo WARNING: First attempt failed, trying again...
    python -m pip install pyinstaller
)
python -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo ERROR: PyInstaller still not available
    echo Try manually: pip install pyinstaller
    pause
    exit /b 1
)
echo Γö╡ PyInstaller ready

echo.
echo [3/5] Preparing app directories...
python -c "from pathlib import Path; dirs = ['cache', 'cache/yaml', 'cache/backups', 'logs', 'connectapp/migrations']; [Path(d).mkdir(parents=True, exist_ok=True) for d in dirs]; print('Γö╡ Directories created')"

echo.
echo [4/5] Creating migrations __init__.py...
python -c "from pathlib import Path; Path('connectapp/migrations/__init__.py').touch(); print('Γö╡ Created')"

echo.
echo [5/5] Building portable EXE (this takes 2-5 minutes)...
python build_portable.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed
    pause
    exit /b 1
)

echo.
echo =========================================================
echo   ✓ BUILD COMPLETE!
echo =========================================================
echo.
echo Next steps:
echo   1. Open: dist_portable\Click2Connect_v2026.07.10\
echo   2. Copy entire folder to another system
echo   3. Run: Click2Connect_v2026.07.10.exe
echo.
pause
