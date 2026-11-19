Set-StrictMode -Version Latest

function Initialize-AuthorWorkbenchNativeProcesses {
    if (-not ('Zhimeng.Workbench.NativeProcesses' -as [type])) {
        Add-Type -Path (Join-Path $PSScriptRoot 'workbench_native_processes.cs')
    }
}

function Get-AuthorWorkbenchListenerPids {
    param([Parameter(Mandatory = $true)][int]$Port)

    Initialize-AuthorWorkbenchNativeProcesses
    return @([Zhimeng.Workbench.NativeProcesses]::ListenerPids($Port))
}

function Get-AuthorWorkbenchProcess {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    Initialize-AuthorWorkbenchNativeProcesses
    return [pscustomobject]@{
        ProcessId = $ProcessId
        Name = $process.ProcessName + '.exe'
        CommandLine = [Zhimeng.Workbench.NativeProcesses]::CommandLine($ProcessId)
    }
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

# 作者登录时的那份环境（2026-09-28）：注册表里系统级 + 用户级的变量，登录时 Windows 给的那几个（HKCU\Volatile Environment），
# 加上每个进程 Windows 自己生成的那几个（ProgramFiles 之类）；PATH 按系统级 + 用户级重拼；PSModulePath 照当前的留着（PowerShell 自己要用）。
# 为什么：AI 从自己的会话里跑启动脚本时（改完要重启），会话里的环境变量会被后端和前端继承——9-28 查到作者那套的后端带着宿主的
# 43 个：ANTHROPIC_API_KEY、MIRASIM_*_TOKEN 这些令牌，宿主的代理，NoDefaultCurrentDirectoryInExePath（cmd 里不在当前文件夹找命令，
# 跑 gen.cmd 直接报「不是内部或外部命令」）。作者那套的模型跑命令时就和作者双击「启动.cmd」时不一样，`set` 还看得见那些令牌。
$AuthorWorkbenchGeneratedVariables = @(
    'ALLUSERSPROFILE', 'APPDATA', 'CommonProgramFiles', 'CommonProgramFiles(x86)', 'CommonProgramW6432', 'COMPUTERNAME',
    'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA', 'LOGONSERVER', 'ProgramData', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432',
    'PSModulePath', 'PUBLIC', 'SESSIONNAME', 'SystemDrive', 'SystemRoot', 'USERDOMAIN', 'USERDOMAIN_ROAMINGPROFILE',
    'USERNAME', 'USERPROFILE', 'windir'
)

function Get-AuthorLoginEnvironment {
    $wanted = @{}
    foreach ($scope in @('Machine', 'User')) {
        $variables = [Environment]::GetEnvironmentVariables($scope)
        foreach ($name in @($variables.Keys)) {
            $wanted[[string]$name] = [string]$variables[$name]
        }
    }
    $volatile = Get-ItemProperty -LiteralPath 'HKCU:\Volatile Environment' -ErrorAction SilentlyContinue
    if ($null -ne $volatile) {
        foreach ($property in $volatile.PSObject.Properties) {
            if ($property.Name -notlike 'PS*') {
                $wanted[$property.Name] = [string]$property.Value
            }
        }
    }
    foreach ($name in $AuthorWorkbenchGeneratedVariables) {
        $value = [Environment]::GetEnvironmentVariable($name, 'Process')
        if (-not [string]::IsNullOrEmpty($value)) {
            $wanted[$name] = $value
        }
    }
    $paths = @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) | Where-Object { -not [string]::IsNullOrEmpty($_) }
    $wanted['Path'] = @($paths) -join ';'
    return $wanted
}

function Use-AuthorLoginEnvironment {
    # 把这个进程的环境换成作者登录时的那一份，返回换之前的那份（交给 Restore-AuthorWorkbenchEnvironment 还原）。
    # 删变量要传 [NullString]::Value：PowerShell 会把 $null 转成空字符串，PowerShell 7.6（新版 .NET）遇到空字符串
    # 留下一个空值的变量、不删（9-28 实测，子进程里名字都还在）；Windows PowerShell 5.1 两种都删。
    $saved = @{}
    $current = [Environment]::GetEnvironmentVariables('Process')
    foreach ($name in @($current.Keys)) {
        $saved[[string]$name] = [string]$current[$name]
    }
    $wanted = Get-AuthorLoginEnvironment
    foreach ($name in @($saved.Keys)) {
        if (-not $wanted.ContainsKey($name)) {
            [Environment]::SetEnvironmentVariable($name, [NullString]::Value, 'Process')
        }
    }
    foreach ($name in @($wanted.Keys)) {
        [Environment]::SetEnvironmentVariable($name, $wanted[$name], 'Process')
    }
    return $saved
}

function Restore-AuthorWorkbenchEnvironment {
    param([Parameter(Mandatory = $true)][hashtable]$Saved)

    $current = [Environment]::GetEnvironmentVariables('Process')
    foreach ($name in @($current.Keys)) {
        if (-not $Saved.ContainsKey([string]$name)) {
            [Environment]::SetEnvironmentVariable([string]$name, [NullString]::Value, 'Process')
        }
    }
    foreach ($name in @($Saved.Keys)) {
        [Environment]::SetEnvironmentVariable($name, $Saved[$name], 'Process')
    }
}
