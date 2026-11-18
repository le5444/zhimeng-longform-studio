#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$WorkbenchUrl = 'http://127.0.0.1:5173',
    [string]$BrowserData = '',
    [switch]$NoStartServices
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DesktopRoot = Join-Path $ProjectRoot 'apps/desktop'
. (Join-Path $PSScriptRoot 'author_workbench_processes.ps1')
$PreviousEnvironment = Use-AuthorLoginEnvironment
try {
    $Electron = Join-Path $DesktopRoot 'node_modules/.bin/electron.cmd'
    if (-not (Test-Path -LiteralPath $Electron)) {
        Push-Location $DesktopRoot
        try {
            & npm.cmd ci --workspaces=false --no-audit --no-fund
            if ($LASTEXITCODE -ne 0) { throw '桌面组件安装失败，请检查网络和 Node.js 后重试。' }
        }
        finally { Pop-Location }
    }
    if (-not $NoStartServices) {
        $Ready = $false
        try { $Ready = (Invoke-WebRequest 'http://127.0.0.1:8787/health/ready' -TimeoutSec 3).StatusCode -eq 200 -and (Invoke-WebRequest $WorkbenchUrl -TimeoutSec 3).StatusCode -eq 200 } catch { }
        if (-not $Ready) { & (Join-Path $PSScriptRoot 'start_author_workbench.ps1') -NoBrowser -SkipBookshelfScan }
    }
    $env:ZHIMENG_WORKBENCH_URL = $WorkbenchUrl
    if ($BrowserData) { $env:ZHIMENG_BROWSER_DATA = [IO.Path]::GetFullPath($BrowserData) }
    # Electron's own BrowserWindow shows the product; the command helper has no visible console.
    $RuntimeDirectory = Join-Path $ProjectRoot '.runtime'
    New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
    $LaunchLog = Join-Path $RuntimeDirectory ('desktop-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    $DesktopProcess = Start-Process -FilePath $Electron -ArgumentList '.' -WorkingDirectory $DesktopRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput ($LaunchLog + '.out.log') -RedirectStandardError ($LaunchLog + '.err.log')
    Write-Host "正在打开织梦桌面。启动进程：$($DesktopProcess.Id)；日志：$LaunchLog"
}
finally { Restore-AuthorWorkbenchEnvironment -Saved $PreviousEnvironment }
