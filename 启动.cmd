@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo.
echo ============================================
echo    织梦作者工作台
echo ============================================
echo.

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_author_workbench.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
