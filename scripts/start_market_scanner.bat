@echo off

taskkill /FI "WINDOWTITLE eq Market Scanner API*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Market Scanner Frontend*" /T /F >nul 2>&1

echo Market Scanner services stopped.

setlocal
set "ROOT=%~dp0.."

if exist "%ROOT%.venv\Scripts\python.exe" (
    set "PYTHON=%ROOT%.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)

start "Market Scanner API" "%ComSpec%" /k "cd /d "%ROOT%" && "%PYTHON%" -m uvicorn api.main:app --reload --port 8000"
start "Market Scanner Frontend" "%ComSpec%" /k "cd /d "%~dp0..\frontend" && npm.cmd run dev"

timeout /t 3 /nobreak >nul
start "" http://localhost:3000
endlocal
