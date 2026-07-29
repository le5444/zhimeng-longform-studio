@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop_author_workbench.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
