@echo off
cd /d "%~dp0"
start "" cmd /c "timeout /t 1 >nul & start http://127.0.0.1:8770"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" dashboard\server.py
) else (
  python dashboard\server.py
)
pause
