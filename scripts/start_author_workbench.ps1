[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$SkipBookshelfScan
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $Root '.runtime'
$ManagedProjectRoot = Join-Path $RuntimeDir 'author-projects'
$StatePath = Join-Path $RuntimeDir 'author-workbench-processes.json'
$DatabasePath = Join-Path $RuntimeDir 'workbench.sqlite3'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$ViteScript = Join-Path $Root 'apps\workbench\node_modules\vite\bin\vite.js'
$ViteConfig = Join-Path $Root 'apps\workbench\vite.config.ts'
$RuntimePort = 8787
$WorkbenchPort = 5173
$RuntimeUrl = "http://127.0.0.1:$RuntimePort"
$WorkbenchUrl = "http://127.0.0.1:$WorkbenchPort"

. (Join-Path $PSScriptRoot 'author_workbench_processes.ps1')

function Wait-AuthorWorkbenchEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 3
            if ($response.StatusCode -eq 200) {
                return $response
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $deadline)

    throw "等待服务超时：$Uri"
}

function Get-NormalizedPathKey {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [IO.Path]::GetFullPath($Path).TrimEnd('\', '/').ToLowerInvariant()
}

function Test-DevelopmentProjectName {
    param([Parameter(Mandatory = $true)][string]$Name)

    return (
        $Name -match '^(链路验证[_-]|压测[_-]|r\d+[_-]|phase\d+[_-]|四系统联动[_-])' -or
        $Name -match '(验收|压力测试)'
    )
}

function Test-HiddenOrTempDirectoryName {
    param([Parameter(Mandatory = $true)][string]$Name)

    # 以 . 或 _ 开头的是隐藏/临时目录（.context_extract、_tmp_… 这类机器产物），不是书。
    # 2026-09-20 实测：inspect 放开「任何文件夹都能当书」之后，它们会被这里扫上书架。
    return $Name -match '^[._]'
}

function Get-AuthorWorkbenchJson {
    param([Parameter(Mandatory = $true)][string]$Uri)

    # Windows PowerShell 5.1 的 Invoke-RestMethod 对不带 charset 的 JSON 按 ISO-8859-1 解码，
    # 中文书名和路径全变乱码，「已经在书架上」的比对永远不命中，每次启动都把每本书重扫一遍
    # （974 章那本一次十几秒）。这里按字节取回、明确用 UTF-8 解码。
    $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 15
    $bytes = $response.RawContentStream.ToArray()
    return ([Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json)
}

function Sync-AuthorBookshelf {
    param([Parameter(Mandatory = $true)][string]$BooksRoot)

    if (-not (Test-Path -LiteralPath $BooksRoot -PathType Container)) {
        return [pscustomobject]@{ total = 0; development = 0; added = 0; skipped = 0 }
    }

    $current = Get-AuthorWorkbenchJson -Uri "$RuntimeUrl/api/v1/projects"
    $knownRoots = @{}
    foreach ($project in @($current.projects)) {
        $knownRoots[(Get-NormalizedPathKey -Path ([string]$project.root_path))] = $true
    }

    $added = 0
    $skipped = 0
    $directories = @(Get-ChildItem -LiteralPath $BooksRoot -Directory -ErrorAction Stop)
    foreach ($directory in $directories) {
        if (Test-HiddenOrTempDirectoryName -Name $directory.Name) {
            $skipped++
            Write-Host "      [隐藏/临时目录] $($directory.Name)（不放进书架）" -ForegroundColor DarkGray
            continue
        }
        if (Test-DevelopmentProjectName -Name $directory.Name) {
            $skipped++
            Write-Host "      [开发项目] $($directory.Name)（不放进默认书架）" -ForegroundColor DarkGray
            continue
        }
        $pathKey = Get-NormalizedPathKey -Path $directory.FullName
        if ($knownRoots.ContainsKey($pathKey)) {
            continue
        }

        Write-Host "      检查：$($directory.Name)"
        try {
            $body = @{ root_path = $directory.FullName } | ConvertTo-Json
            $inspection = Invoke-RestMethod `
                -Method Post `
                -Uri "$RuntimeUrl/api/v1/projects/inspect" `
                -ContentType 'application/json; charset=utf-8' `
                -Body $body `
                -TimeoutSec 180
            $knownRoots[$pathKey] = $true
            $added++
            Write-Host "      [已上架] $($directory.Name)" -ForegroundColor Green
        }
        catch {
            $skipped++
            Write-Host "      [跳过] $($directory.Name)（不是可识别的小说项目）" -ForegroundColor DarkGray
        }
    }

    $after = Get-AuthorWorkbenchJson -Uri "$RuntimeUrl/api/v1/projects"
    $visibleBookCount = 0
    $developmentProjectCount = 0
    foreach ($project in @($after.projects)) {
        if (Test-DevelopmentProjectName -Name ([string]$project.display_name)) {
            $developmentProjectCount++
        }
        else {
            $visibleBookCount++
        }
    }
    return [pscustomobject]@{
        total = $visibleBookCount
        development = $developmentProjectCount
        added = $added
        skipped = $skipped
    }
}

function Stop-NewAuthorWorkbenchProcesses {
    param($RuntimeProcess, $WorkbenchProcess)

    foreach ($process in @($WorkbenchProcess, $RuntimeProcess)) {
        if ($null -eq $process) {
            continue
        }
        try {
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction Stop
            }
        }
        catch {
        }
    }
}

try {
    # 按作者登录时的环境起服务（见 Use-AuthorLoginEnvironment）：AI 从自己的会话里重启时，别把会话的令牌、代理这些带进作者那套。
    # 跑完（包括出错、已经在运行直接退出）在最后的 finally 里还原，调用方自己的环境不受影响。
    $callerEnvironment = Use-AuthorLoginEnvironment
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "找不到后端环境：$Python"
    }
    if (-not (Test-Path -LiteralPath $ViteScript -PathType Leaf)) {
        throw "找不到前端依赖：$ViteScript`n请先在项目目录执行 npm install。"
    }
    $nodeCommand = Get-Command node.exe -ErrorAction Stop
    $Node = $nodeCommand.Source

    $BooksRoot = Join-Path $env:USERPROFILE 'Documents\写小说工作区'
    $BooksFile = Join-Path $Root '小说目录.txt'
    if (Test-Path -LiteralPath $BooksFile -PathType Leaf) {
        $configuredBooksRoot = (Get-Content -LiteralPath $BooksFile -Raw -Encoding UTF8).Trim()
        if (-not [string]::IsNullOrWhiteSpace($configuredBooksRoot)) {
            $BooksRoot = $configuredBooksRoot
        }
    }

    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $ManagedProjectRoot -Force | Out-Null

    $existingState = Read-AuthorWorkbenchState -StatePath $StatePath
    $runtimeListeners = @(Get-AuthorWorkbenchListenerPids -Port $RuntimePort)
    $workbenchListeners = @(Get-AuthorWorkbenchListenerPids -Port $WorkbenchPort)

    if ($null -ne $existingState) {
        $sameRoot = (
            (Get-NormalizedPathKey -Path ([string]$existingState.project_root)) -eq
            (Get-NormalizedPathKey -Path $Root)
        )
        $runtimeOwned = $sameRoot -and (Test-AuthorWorkbenchProcessRecord -Record $existingState.runtime)
        $workbenchOwned = $sameRoot -and (Test-AuthorWorkbenchProcessRecord -Record $existingState.workbench)
        $portsOwned = (
            $runtimeOwned -and
            $workbenchOwned -and
            $runtimeListeners -contains [int]$existingState.runtime.pid -and
            $workbenchListeners -contains [int]$existingState.workbench.pid
        )

        if ($portsOwned) {
            try {
                Wait-AuthorWorkbenchEndpoint -Uri "$RuntimeUrl/health/ready" -TimeoutSeconds 3 | Out-Null
                Wait-AuthorWorkbenchEndpoint -Uri $WorkbenchUrl -TimeoutSeconds 3 | Out-Null
                Write-Host ""
                Write-Host "织梦已经在运行。" -ForegroundColor Green
                Write-Host "地址：$WorkbenchUrl"
                if (-not $NoBrowser) {
                    Start-Process $WorkbenchUrl
                }
                exit 0
            }
            catch {
                Write-Host "发现本项目上次留下的未就绪进程，正在安全重启 ..." -ForegroundColor Yellow
                Stop-AuthorWorkbenchProcessRecord -Record $existingState.workbench | Out-Null
                Stop-AuthorWorkbenchProcessRecord -Record $existingState.runtime | Out-Null
                Start-Sleep -Milliseconds 800
                $runtimeListeners = @(Get-AuthorWorkbenchListenerPids -Port $RuntimePort)
                $workbenchListeners = @(Get-AuthorWorkbenchListenerPids -Port $WorkbenchPort)
            }
        }
    }

    if ($runtimeListeners.Count -gt 0) {
        throw "端口 $RuntimePort 已被其他进程占用：`n$(Get-AuthorWorkbenchPortOwnerText -Port $RuntimePort)"
    }
    if ($workbenchListeners.Count -gt 0) {
        throw "端口 $WorkbenchPort 已被其他进程占用：`n$(Get-AuthorWorkbenchPortOwnerText -Port $WorkbenchPort)"
    }

    $env:PYTHONPATH = Join-Path $Root 'services\writing-runtime\src'
    $expectedSchema = [int](& $Python -c 'from zhimeng_runtime.database import SCHEMA_VERSION; print(SCHEMA_VERSION)')
    $env:ZHIMENG_ENVIRONMENT = 'development'
    $env:ZHIMENG_HOST = '127.0.0.1'
    $env:ZHIMENG_PORT = [string]$RuntimePort
    $env:ZHIMENG_DATABASE_PATH = $DatabasePath
    $trustedProjectRoots = @($ManagedProjectRoot)
    if (Test-Path -LiteralPath $BooksRoot -PathType Container) {
        $trustedProjectRoots += $BooksRoot
    }
    $env:ZHIMENG_TRUSTED_PROJECT_ROOTS = (
        $trustedProjectRoots -join [IO.Path]::PathSeparator
    )
    $env:ZHIMENG_ALLOWED_ORIGINS = "$WorkbenchUrl,http://localhost:$WorkbenchPort"
    # 常用中转站首包普遍偏慢，默认 120 秒会把长上下文请求误判为超时。
    if (-not $env:ZHIMENG_AGENT_PROVIDER_TIMEOUT_SECONDS) {
        $env:ZHIMENG_AGENT_PROVIDER_TIMEOUT_SECONDS = '300'
    }
    # 「接受的上报名」不再写死在这里——2026-09-12 实测这条硬编码已经变成一个洞：
    # 它是 2026-09-05 加的，当时作者的默认模型就是 deepseek-v4-flash，中转只是
    # 加了厂商前缀和日期后缀（请求 flash / 上报 deepseek-ai/deepseek-v4-flash-0731），
    # 属于正常改名。但作者 9-10 之后把默认模型换成了 pro，这条别名的效果就变成了
    # 「pro 被降级成这个 flash 时自动放行」——正是 AGENTS.md 3.49/3.58 明令禁止的
    # 「把 flash 名字加进接受列表等于默许降级」。
    # 现在名单按渠道配在设置 → 模型服务 →「接受的上报名」里（3.58 建的入口），
    # 作者自己就能加，不必改脚本；环境变量仍然可用，只是不再预置任何名字。
    #
    # 环境变量名单与界面名单是合并生效的，核对逻辑一个字没改。
    $env:VITE_RUNTIME_URL = $RuntimeUrl
    $env:VITE_RUNTIME_PROXY_TARGET = $RuntimeUrl

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $runtimeOut = Join-Path $RuntimeDir "author-workbench-runtime-$stamp.out.log"
    $runtimeErr = Join-Path $RuntimeDir "author-workbench-runtime-$stamp.err.log"
    $workbenchOut = Join-Path $RuntimeDir "author-workbench-frontend-$stamp.out.log"
    $workbenchErr = Join-Path $RuntimeDir "author-workbench-frontend-$stamp.err.log"

    Write-Host ""
    Write-Host "[1/4] 启动写作后端 ..."
    $runtimeProcess = Start-Process `
        -FilePath $Python `
        -ArgumentList @('-m', 'zhimeng_runtime') `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $runtimeOut `
        -RedirectStandardError $runtimeErr `
        -WindowStyle Hidden `
        -PassThru

    Write-Host "[2/4] 启动作者界面 ..."
    $workbenchProcess = Start-Process `
        -FilePath $Node `
        -ArgumentList @(
            $ViteScript,
            '--host', '127.0.0.1',
            '--port', [string]$WorkbenchPort,
            '--strictPort',
            '--config', $ViteConfig
        ) `
        -WorkingDirectory (Join-Path $Root 'apps\workbench') `
        -RedirectStandardOutput $workbenchOut `
        -RedirectStandardError $workbenchErr `
        -WindowStyle Hidden `
        -PassThru

    $state = [ordered]@{
        schema = 'zhimeng.author-workbench-processes.v1'
        status = 'starting'
        failure = $null
        project_root = $Root
        books_root = $BooksRoot
        database_path = $DatabasePath
        expected_schema_version = $expectedSchema
        project_count = 0
        started_at = (Get-Date).ToUniversalTime().ToString('o')
        runtime = [ordered]@{
            pid = $runtimeProcess.Id
            started_at = $runtimeProcess.StartTime.ToUniversalTime().ToString('o')
            command_token = '-m zhimeng_runtime'
            port = $RuntimePort
            stdout_log = $runtimeOut
            stderr_log = $runtimeErr
        }
        workbench = [ordered]@{
            pid = $workbenchProcess.Id
            started_at = $workbenchProcess.StartTime.ToUniversalTime().ToString('o')
            command_token = $ViteScript
            port = $WorkbenchPort
            stdout_log = $workbenchOut
            stderr_log = $workbenchErr
        }
    }
    Write-AuthorWorkbenchState -StatePath $StatePath -State $state

    Write-Host "[3/4] 等待后端和界面真正就绪 ..."
    $ready = Wait-AuthorWorkbenchEndpoint -Uri "$RuntimeUrl/health/ready" -TimeoutSeconds 90
    $readyPayload = $ready.Content | ConvertFrom-Json
    if ([int]$readyPayload.database_schema_version -ne $expectedSchema) {
        throw "数据库版本不匹配：实际 $($readyPayload.database_schema_version)，预期 $expectedSchema"
    }
    if (-not (Test-Path -LiteralPath $DatabasePath -PathType Leaf)) {
        throw "后端已响应，但数据库文件不存在：$DatabasePath"
    }

    $runtimeListeners = @(Get-AuthorWorkbenchListenerPids -Port $RuntimePort)
    if ($runtimeListeners.Count -ne 1) {
        throw "后端已响应，但端口 $RuntimePort 的监听进程数量异常。"
    }
    $runtimeListenerPid = [int]$runtimeListeners[0]
    $runtimeListener = Get-AuthorWorkbenchProcess -ProcessId $runtimeListenerPid
    if (
        $null -eq $runtimeListener -or
        [string]$runtimeListener.CommandLine -notlike '*-m zhimeng_runtime*'
    ) {
        throw "后端端口不是本次命令启动的 Runtime 在监听。"
    }

    # Windows venv 的 python.exe 可能把执行转交给基础解释器；记录真正监听端口的 PID。
    if ($runtimeListenerPid -ne [int]$state.runtime.pid) {
        $actualRuntimeProcess = Get-Process -Id $runtimeListenerPid -ErrorAction Stop
        $state.runtime.pid = $runtimeListenerPid
        $state.runtime.started_at = $actualRuntimeProcess.StartTime.ToUniversalTime().ToString('o')
        Write-AuthorWorkbenchState -StatePath $StatePath -State $state
    }

    Wait-AuthorWorkbenchEndpoint -Uri $WorkbenchUrl -TimeoutSeconds 90 | Out-Null

    $runtimeListeners = @(Get-AuthorWorkbenchListenerPids -Port $RuntimePort)
    $workbenchListeners = @(Get-AuthorWorkbenchListenerPids -Port $WorkbenchPort)
    if ($runtimeListeners -notcontains [int]$state.runtime.pid) {
        throw "后端端口不是本次启动的进程在监听。"
    }
    if ($workbenchListeners -notcontains $workbenchProcess.Id) {
        throw "前端端口不是本次启动的进程在监听。"
    }

    Write-Host "[4/4] 核对书架 ..."
    if ($SkipBookshelfScan) {
        $projects = Get-AuthorWorkbenchJson -Uri "$RuntimeUrl/api/v1/projects"
        $visibleBookCount = 0
        $developmentProjectCount = 0
        foreach ($project in @($projects.projects)) {
            if (Test-DevelopmentProjectName -Name ([string]$project.display_name)) {
                $developmentProjectCount++
            }
            else {
                $visibleBookCount++
            }
        }
        $bookshelf = [pscustomobject]@{
            total = $visibleBookCount
            development = $developmentProjectCount
            added = 0
            skipped = 0
        }
    }
    else {
        $bookshelf = Sync-AuthorBookshelf -BooksRoot $BooksRoot
    }

    $state.status = 'ready'
    $state.project_count = $bookshelf.total
    Write-AuthorWorkbenchState -StatePath $StatePath -State $state

    Write-Host ""
    Write-Host "织梦已就绪。" -ForegroundColor Green
    Write-Host "地址：$WorkbenchUrl"
    Write-Host "小说目录：$BooksRoot"
    Write-Host "书架作品：$($bookshelf.total) 本（本次新增 $($bookshelf.added) 本）"
    if ($bookshelf.development -gt 0) {
        Write-Host "开发项目：$($bookshelf.development) 个（已从默认书架收起）"
    }
    Write-Host "数据库版本：$expectedSchema"
    Write-Host "要完全退出，请双击“关闭.cmd”。"
    Write-Host ""

    if (-not $NoBrowser) {
        Start-Process $WorkbenchUrl
    }
}
catch {
    if (Get-Variable -Name state -ErrorAction SilentlyContinue) {
        foreach ($record in @($state.workbench, $state.runtime)) {
            try {
                Stop-AuthorWorkbenchProcessRecord -Record $record | Out-Null
            }
            catch {
            }
        }
    }
    if (Get-Variable -Name runtimeProcess -ErrorAction SilentlyContinue) {
        $runtimeForCleanup = $runtimeProcess
    }
    else {
        $runtimeForCleanup = $null
    }
    if (Get-Variable -Name workbenchProcess -ErrorAction SilentlyContinue) {
        $workbenchForCleanup = $workbenchProcess
    }
    else {
        $workbenchForCleanup = $null
    }
    Stop-NewAuthorWorkbenchProcesses `
        -RuntimeProcess $runtimeForCleanup `
        -WorkbenchProcess $workbenchForCleanup

    if (Get-Variable -Name state -ErrorAction SilentlyContinue) {
        $state.status = 'failed'
        $state.failure = $_.Exception.Message
        Write-AuthorWorkbenchState -StatePath $StatePath -State $state
    }

    Write-Host ""
    Write-Host "[启动失败] $($_.Exception.Message)" -ForegroundColor Red
    if (Get-Variable -Name runtimeErr -ErrorAction SilentlyContinue) {
        Write-Host "后端错误日志：$runtimeErr"
    }
    if (Get-Variable -Name workbenchErr -ErrorAction SilentlyContinue) {
        Write-Host "前端错误日志：$workbenchErr"
    }
    exit 1
}
finally {
    if (Get-Variable -Name callerEnvironment -ErrorAction SilentlyContinue) {
        Restore-AuthorWorkbenchEnvironment -Saved $callerEnvironment
    }
}
