#requires -Version 7.0
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'author_workbench_processes.ps1')

$current = Get-Process -Id $PID
$identity = Get-AuthorWorkbenchProcess -ProcessId $PID
if ($identity.ProcessId -ne $PID -or [string]::IsNullOrWhiteSpace($identity.CommandLine)) { throw '进程身份读取失败' }
$record = [pscustomobject]@{ pid = $PID; started_at = $current.StartTime.ToUniversalTime().ToString('o'); command_token = $identity.CommandLine }
if (-not (Test-AuthorWorkbenchProcessRecord -Record $record)) { throw '正确进程身份没有匹配' }
$record.command_token = 'zhimeng-impossible-process-token-' + [guid]::NewGuid()
if (Test-AuthorWorkbenchProcessRecord -Record $record) { throw '错误命令行被当成同一进程' }
$record.command_token = $identity.CommandLine
$record.started_at = $current.StartTime.AddMinutes(-10).ToUniversalTime().ToString('o')
if (Test-AuthorWorkbenchProcessRecord -Record $record) { throw '旧时间戳被当成同一进程' }
if ($null -ne (Get-AuthorWorkbenchProcess -ProcessId 2147483647)) { throw '不存在的进程返回了身份' }

$ipv4 = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
$ipv6 = [Net.Sockets.TcpListener]::new([Net.IPAddress]::IPv6Loopback, 0)
try {
    $ipv4.Start()
    $port4 = $ipv4.LocalEndpoint.Port
    if (@(Get-AuthorWorkbenchListenerPids -Port $port4) -notcontains $PID) { throw '没有找到 IPv4 监听进程' }
    $ipv6.Server.DualMode = $false
    $ipv6.Start()
    $port6 = $ipv6.LocalEndpoint.Port
    if (@(Get-AuthorWorkbenchListenerPids -Port $port6) -notcontains $PID) { throw '没有找到 IPv6 监听进程' }
    $ipv4.Stop()
    $ipv6.Stop()
    if (@(Get-AuthorWorkbenchListenerPids -Port $port4) -contains $PID) { throw '停止后的 IPv4 端口仍被报告为监听' }
    if (@(Get-AuthorWorkbenchListenerPids -Port $port6) -contains $PID) { throw '停止后的 IPv6 端口仍被报告为监听' }
    $invalidRejected = $false
    try { [Zhimeng.Workbench.NativeProcesses]::ListenerPids(0) | Out-Null } catch { $invalidRejected = $true }
    if (-not $invalidRejected) { throw '无效端口没有报错' }
    Write-Output 'Passed: process identity, command/start mismatch, missing PID, IPv4/IPv6 listeners, released ports, invalid port (9 checks).'
} finally { $ipv4.Stop(); $ipv6.Stop() }
