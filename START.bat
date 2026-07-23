@echo off
REM Click2Connect Portable Launcher
REM Starts the Django development server

echo.
echo =========================================================
echo   Click2Connect v{APP_VERSION} - PLC/OPC UA Bridge
echo =========================================================
echo.

REM Get the directory where this script is located
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Wait a moment for any previous instance to close
timeout /t 2 /nobreak > nul

REM Start the server on localhost:8000
echo [Server] Starting Click2Connect...
echo [Server] UI available at http://localhost:8000
echo.

REM Start server. Open http://localhost:8000 manually if needed.
python manage.py runserver 0.0.0.0:8000

echo.
echo [Server] Click2Connect is running
echo [Server] Press Ctrl+C to stop
pause
