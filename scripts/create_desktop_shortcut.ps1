#requires -Version 5.1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Desktop = [Environment]::GetFolderPath("Desktop")
$Wsh = New-Object -ComObject WScript.Shell

# Prefer EXE, fallback to python launcher
$exe = Join-Path $Root "dist\zhimeng-workbench\zhimeng-workbench.exe"
$launchPs1 = Join-Path $PSScriptRoot "launch_workbench.ps1"
$shortcutPath = Join-Path $Desktop "织梦长篇工作台.lnk"

$sc = $Wsh.CreateShortcut($shortcutPath)
if (Test-Path -LiteralPath $exe) {
  $sc.TargetPath = $exe
  $sc.Arguments = "--writing-root `"$env:USERPROFILE\Documents\写小说工作区`" --writing-root `"$env:USERPROFILE\.codex\skills\novel-kb-manager\library\projects`" --port 8765"
  $sc.WorkingDirectory = Split-Path -Parent $exe
  $sc.Description = "织梦长篇工作台 (EXE)"
} else {
  $sc.TargetPath = "powershell.exe"
  $sc.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launchPs1`""
  $sc.WorkingDirectory = $Root
  $sc.Description = "织梦长篇工作台 (Python launch)"
}
$sc.IconLocation = "%SystemRoot%\System32\shell32.dll,13"
$sc.Save()

# open page helper shortcut
$page = Join-Path $Desktop "织梦工作台页面.url"
@(
"[InternetShortcut]"
"URL=http://127.0.0.1:8765/workbench/"
) | Set-Content -Encoding ascii -LiteralPath $page

Write-Host "shortcut=$shortcutPath"
Write-Host "page=$page"
Write-Host "target=$($sc.TargetPath)"
