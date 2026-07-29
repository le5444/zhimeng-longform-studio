[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$StatePath = Join-Path $Root '.runtime\author-workbench-processes.json'

. (Join-Path $PSScriptRoot 'author_workbench_processes.ps1')

try {
    $state = Read-AuthorWorkbenchState -StatePath $StatePath
    if ($null -eq $state) {
        Write-Host ""
        Write-Host "没有本项目的运行记录，织梦作者工作台已经关闭。"
        Write-Host "未扫描、未关闭任何其他 Python 或 Node 进程。"
        exit 0
    }

    $stateRoot = [IO.Path]::GetFullPath([string]$state.project_root).TrimEnd('\', '/')
    $actualRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (-not $stateRoot.Equals($actualRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "状态文件不属于当前项目，拒绝关闭任何进程。"
    }

    Write-Host ""
    Write-Host "正在关闭织梦作者工作台 ..."
    $stopped = 0
    $identityMismatches = 0

    foreach ($entry in @(
        [pscustomobject]@{ name = '作者界面'; record = $state.workbench },
        [pscustomobject]@{ name = '写作后端'; record = $state.runtime }
    )) {
        $process = Get-AuthorWorkbenchProcess -ProcessId ([int]$entry.record.pid)
        if ($null -eq $process) {
            Write-Host "  $($entry.name)：已经停止"
            continue
        }
        if (-not (Test-AuthorWorkbenchProcessRecord -Record $entry.record)) {
            Write-Host "  $($entry.name)：PID 已被其他程序占用，未关闭" -ForegroundColor Yellow
            $identityMismatches++
            continue
        }

        Stop-Process -Id ([int]$entry.record.pid) -Force -ErrorAction Stop
        $stopped++
        Write-Host "  $($entry.name)：已关闭（PID $($entry.record.pid)）" -ForegroundColor Green
    }

    Start-Sleep -Milliseconds 500
    if ($identityMismatches -eq 0) {
        Remove-Item -LiteralPath $StatePath -Force
    }

    Write-Host ""
    Write-Host "已精确关闭 $stopped 个本项目进程。"
    if ($identityMismatches -gt 0) {
        throw "有 $identityMismatches 个 PID 身份不匹配，已为安全起见保留状态文件。"
    }
}
catch {
    Write-Host ""
    Write-Host "[关闭失败] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
