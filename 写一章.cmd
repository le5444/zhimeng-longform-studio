@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ============================================
echo   文渊阁 / 新项目  ——  写一章
echo ============================================
echo.

if not exist "本地模型配置.json" (
  echo [!] 还没有「本地模型配置.json」。
  echo     请先把「本地模型配置.example.json」复制一份，
  echo     改名成「本地模型配置.json」，把 api_key 填进去。
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [!] 找不到 .venv\Scripts\python.exe
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" "scripts\write_chapter_live.py" %*

echo.
pause
