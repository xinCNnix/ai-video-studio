@echo off
title AI Video Studio Launcher

rem ===============================================
rem  AI Video Studio - one-click launcher
rem  Backend : FastAPI  on http://127.0.0.1:8000
rem  Frontend: Next.js  on http://127.0.0.1:3000
rem  NOTE: keep this file ASCII-only (cmd uses GBK)
rem ===============================================

if not exist "%~dp0backend\.venv\Scripts\python.exe" (
    echo [ERROR] backend\.venv not found.
    echo Run first:
    echo   cd backend
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "%~dp0frontend\node_modules\next" (
    echo [ERROR] frontend\node_modules not found.
    echo Run first:
    echo   cd frontend
    echo   npm install
    pause
    exit /b 1
)

echo Starting backend on port 8000 ...
start "AI Video Studio - Backend (8000)" /D "%~dp0backend" cmd /k ".venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000"

echo Starting frontend on port 3000 ...
rem Prefer production build when present, fallback to dev mode
if exist "%~dp0frontend\.next\BUILD_ID" (
    start "AI Video Studio - Frontend (3000)" /D "%~dp0frontend" cmd /k "npm run start"
) else (
    start "AI Video Studio - Frontend (3000)" /D "%~dp0frontend" cmd /k "npm run dev"
)

echo Waiting for servers, browser will open shortly ...
powershell -NoProfile -Command "Start-Sleep -Seconds 8; Start-Process 'http://127.0.0.1:3000'"

exit /b 0
