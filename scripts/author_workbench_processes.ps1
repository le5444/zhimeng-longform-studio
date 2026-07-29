Set-StrictMode -Version Latest

function Get-AuthorWorkbenchListenerPids {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Get-AuthorWorkbenchProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    return Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-AuthorWorkbenchProcessRecord {
    param([Parameter(Mandatory = $true)]$Record)

    $process = Get-AuthorWorkbenchProcess -ProcessId ([int]$Record.pid)
    if ($null -eq $process) {
        return $false
    }

    if ([string]::IsNullOrWhiteSpace([string]$process.CommandLine)) {
        return $false
    }
    if (
        $process.CommandLine.IndexOf(
            [string]$Record.command_token,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -lt 0
    ) {
        return $false
    }

    try {
        $actualStart = (Get-Process -Id ([int]$Record.pid) -ErrorAction Stop).StartTime.ToUniversalTime()
        $expectedStart = ([datetime]$Record.started_at).ToUniversalTime()
        return [Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -lt 3
    }
    catch {
        return $false
    }
}

function Stop-AuthorWorkbenchProcessRecord {
    param([Parameter(Mandatory = $true)]$Record)

    if (-not (Test-AuthorWorkbenchProcessRecord -Record $Record)) {
        return $false
    }
    Stop-Process -Id ([int]$Record.pid) -Force -ErrorAction Stop
    return $true
}

function Get-AuthorWorkbenchPortOwnerText {
    param([Parameter(Mandatory = $true)][int]$Port)

    $descriptions = foreach ($processId in @(Get-AuthorWorkbenchListenerPids -Port $Port)) {
        $process = Get-AuthorWorkbenchProcess -ProcessId $processId
        if ($null -eq $process) {
            "PID $processId"
        }
        else {
            "PID $processId ($($process.Name)): $($process.CommandLine)"
        }
    }
    if (@($descriptions).Count -eq 0) {
        return "没有监听进程"
    }
    return $descriptions -join [Environment]::NewLine
}

function Read-AuthorWorkbenchState {
    param([Parameter(Mandatory = $true)][string]$StatePath)

    if (-not (Test-Path -LiteralPath $StatePath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "无法读取启动状态文件：$StatePath"
    }
}

function Write-AuthorWorkbenchState {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath,
        [Parameter(Mandatory = $true)]$State
    )

    $State | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8
}
