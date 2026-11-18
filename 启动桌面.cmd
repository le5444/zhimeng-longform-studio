@echo off
chcp 65001 >nul
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  echo 需要 PowerShell 7。安装后再打开织梦桌面。
  pause
  exit /b 1
)
pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_desktop_workbench.ps1"
if errorlevel 1 pause
