@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv 2>nul
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.10 or newer is required.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed. Check your internet connection.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" server.py
pause
