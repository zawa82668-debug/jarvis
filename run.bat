@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONPATH=%~dp0libs"
if not defined TEMP set "TEMP=%LOCALAPPDATA%\Temp"
if not defined TMP set "TMP=%TEMP%"
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" assistant.py %*
) else (
  python assistant.py %*
)
pause
