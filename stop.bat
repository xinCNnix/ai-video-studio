@echo off
title AI Video Studio - Stop Servers

echo Stopping AI Video Studio servers ...

rem Kill launcher windows by title (visible start.bat mode)
taskkill /FI "WINDOWTITLE eq AI Video Studio - Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq AI Video Studio - Frontend*" /T /F >nul 2>&1

rem Fallback: kill whatever still listens on the ports (hidden mode)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000" ^| findstr "LISTENING"') do taskkill /PID %%p /T /F >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do taskkill /PID %%p /T /F >nul 2>&1

echo All servers stopped.
pause
